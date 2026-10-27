"""Milestone 2: decide whether each discovered snap needs a rebuild or a new release.

Signals, most urgent first:
  security  a USN was issued after the published revision was uploaded, for a
            source package the snap stages (stage-packages + dependencies)
  source    the snap's repo has commits, or declares a different version,
            since the published revision was uploaded
  upstream  a part pulls a remote git source that has moved on (newer tag than
            the pinned source-tag, or new commits on the tracked branch)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone

from snap_maintainer import archive, debver, manifests, repo as repo_mod, snapcraft_yaml, store, usn

RISK_ORDER = ("stable", "candidate", "beta", "edge")

VERDICT_REBUILD = "rebuild"  # security fixes waiting
VERDICT_UPDATE = "update-available"  # new code or upstream release
VERDICT_OK = "up-to-date"
VERDICT_UNKNOWN = "unknown"
VERDICT_UNPUBLISHED = "unpublished"

# commits this close after an upload may be the very change that was uploaded
# (built from the working tree, committed afterwards)
SAME_CHANGE_WINDOW_S = 24 * 3600


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _hm(s: str) -> str:
    return _ts(s).strftime("%Y-%m-%d %H:%M UTC")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reference_targets(channels: list[dict], revisions: dict[int, dict]) -> list[dict]:
    """The most stable published revision per (track, arch), with its upload time.

    Only the default 'latest' track is considered when it exists.
    """
    tracks = {c["track"] for c in channels}
    track = "latest" if "latest" in tracks else min(tracks, default=None)
    best: dict[str, dict] = {}
    for c in channels:
        if c["track"] != track or c.get("revision") is None:
            continue
        cur = best.get(c["arch"])
        if cur is None or RISK_ORDER.index(c["risk"]) < RISK_ORDER.index(cur["risk"]):
            best[c["arch"]] = c
    out = []
    for arch, c in sorted(best.items()):
        rev = revisions.get(c["revision"], {})
        out.append({**c, "uploaded": rev.get("uploaded") or c.get("released_at")})
    return out


def staged_sources(staged: dict[str, str], index: dict) -> dict[str, dict]:
    """Group a manifest's {binary: version} by source package, adding the archive's
    current source version. ``staged`` is the oldest staged version of that source."""
    out: dict[str, dict] = {}
    for pkg, version in staged.items():
        entry = index.get(pkg)
        src = entry["source"] if entry else pkg
        s = out.setdefault(src, {"binaries": [], "version": None, "staged": version})
        s["binaries"].append(pkg)
        if debver.compare(version, s["staged"]) < 0:
            s["staged"] = version
        if entry and (s["version"] is None or debver.compare(entry["version"], s["version"]) > 0):
            s["version"] = entry["version"]
    for s in out.values():
        s["binaries"].sort()
    return out


def match_notices(notices: list[dict], sources: dict[str, dict], uploaded: datetime | None,
                  series: str, notes: list[str]) -> list[tuple[dict, str, str]]:
    """(notice, source, fixed version) for each USN a rebuild would fix.

    With exact staged versions (``"staged"`` in a source entry), a USN applies when
    the staged version is older than the fix. Without them, we can only assume it
    applies when it was issued after the revision was uploaded. Either way the fix
    must be in the public archive, or a rebuild wouldn't pick it up.
    """
    hits = []
    for n in notices:
        recent = uploaded is None or _ts(n["issued"]) > uploaded
        for src, fixed in n["sources"].items():
            s = sources.get(src)
            if s is None:
                continue
            if "staged" in s:
                if debver.compare(s["staged"], fixed) >= 0:
                    continue
            elif not recent:
                continue
            if s["version"] is None or debver.compare(s["version"], fixed) < 0:
                # e.g. fixed only in Ubuntu Pro/ESM
                note = (f"{n['usn']} ({src} {fixed}) affects this snap but the fix isn't "
                        f"in the public {series} archive (has {s['version'] or 'nothing'})")
                if note not in notes:
                    notes.append(note)
                continue
            hits.append((n, src, fixed))
    return hits


def security_reasons(name: str, sy, targets: list[dict], refresh: bool,
                     notes: list[str]) -> list[dict]:
    series = sy.series or snapcraft_yaml.BASE_SERIES.get(targets[0].get("base") or "")
    if not series:
        notes.append(f"unknown base {sy.base!r}; security check skipped")
        return []
    notices = usn.notices(series, refresh=refresh)
    base = sy.base or targets[0].get("base")
    exclude = None

    found: dict[str, dict] = {}
    for t in targets:
        index = archive.package_index(series, t["arch"], refresh=refresh)
        try:
            manifest = manifests.staged_packages(name, t["revision"], t.get("download_url"))
        except manifests.ManifestUnavailable as e:
            notes.append(f"{t['arch']} r{t['revision']}: couldn't read the build manifest ({e})")
            manifest = None

        if manifest is not None:
            t["security_method"] = "manifest"
            sources = staged_sources(manifest["packages"], index)
            uploaded = None
        else:
            roots = sy.stage_packages(t["arch"])
            if not roots or not t.get("uploaded"):
                t["security_method"] = "none"
                continue
            t["security_method"] = "inferred"
            if exclude is None:
                exclude = archive.base_packages(base)
                if exclude is None:
                    notes.append(f"{base} isn't installed locally, so its packages couldn't be "
                                 "excluded (results may include packages the base already ships)")
                    exclude = set()
            pkgs, missing = archive.closure(roots, index, exclude)
            if missing:
                notes.append(f"{t['arch']}: not in the {series} archive: {', '.join(sorted(missing))}")
            sources = archive.sources_of(pkgs, index)
            uploaded = _ts(t["uploaded"])

        for n, src, fixed in match_notices(notices, sources, uploaded, series, notes):
            r = found.setdefault(f"{n['usn']}/{src}", {
                "kind": "security",
                "usn": n["usn"],
                "title": n["title"],
                "severity": n["severity"],
                "issued": n["issued"],
                "source": src,
                "fixed_version": fixed,
                "staged_version": sources[src].get("staged"),
                "binaries": sources[src]["binaries"],
                "arches": [],
                "method": t["security_method"],
            })
            r["arches"].append(t["arch"])
    return sorted(found.values(), key=lambda r: r["issued"], reverse=True)


def source_reasons(repo: dict, sy, targets: list[dict]) -> list[dict]:
    reasons = []
    ref = targets[0]
    since = min(t["uploaded"] for t in targets if t.get("uploaded"))
    code, out = repo_mod.run_gh([
        "api", f"repos/{repo['owner']}/{repo['name']}/commits"
        f"?sha={repo['default_branch']}&since={since}&per_page=100",
    ])
    if code == 0:
        commits = json.loads(out)
        if commits:
            last = commits[0]["commit"]["committer"]["date"]
            reason = {
                "kind": "source",
                "detail": f"{len(commits)}{'+' if len(commits) == 100 else ''} commit(s) on "
                          f"{repo['default_branch']} since {ref['risk']} r{ref['revision']} "
                          f"was uploaded on {since[:10]}",
                "commits": [
                    {"sha": c["sha"][:10], "date": c["commit"]["committer"]["date"],
                     "message": c["commit"]["message"].splitlines()[0]}
                    for c in commits[:10]
                ],
            }
            if (_ts(last) - _ts(since)).total_seconds() <= SAME_CHANGE_WINDOW_S:
                reason["warning"] = (
                    f"may be a false positive: snap uploaded at {_hm(since)}, "
                    f"last commit at {_hm(last)}; if the upload was built from these "
                    "changes before they were committed, it is already up to date"
                )
            reasons.append(reason)
    if sy.version and ref.get("version") and sy.version != ref["version"]:
        reasons.append({
            "kind": "source",
            "detail": f"repo declares version {sy.version}, {ref['risk']} has {ref['version']}",
        })
    return reasons


def _version_key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", tag))


def _is_prerelease(tag: str) -> bool:
    return bool(re.search(r"(alpha|beta|rc|pre|dev)", tag, re.I))


def newer_tags(pinned: str, tags: list[str]) -> list[str]:
    """Tags that look like later releases than ``pinned`` (same prefix style, no pre-releases)."""
    prefix = re.match(r"^\D*", pinned).group(0)
    key = _version_key(pinned)
    if not key:
        return []
    out = [t for t in tags
           if t.startswith(prefix) and _version_key(t) > key
           and (_is_prerelease(pinned) or not _is_prerelease(t))]
    return sorted(out, key=_version_key, reverse=True)


def _ls_remote_tags(url: str) -> list[str] | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(["git", "ls-remote", "--tags", "--refs", url],
                              capture_output=True, text=True, timeout=60, env=env)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    return [l.split("refs/tags/", 1)[1] for l in proc.stdout.splitlines() if "refs/tags/" in l]


def upstream_reasons(sy, targets: list[dict], notes: list[str]) -> list[dict]:
    reasons = []
    since = min(t["uploaded"] for t in targets if t.get("uploaded"))
    for src in sy.remote_sources():
        label = f"part '{src.part}' ({src.url})"
        if src.type != "git":
            notes.append(f"{label}: {src.type} source not checked")
            continue
        if src.commit:
            notes.append(f"{label}: pinned to commit {src.commit[:10]}, not checked")
            continue
        if src.tag:
            tags = _ls_remote_tags(src.url)
            if tags is None:
                notes.append(f"{label}: couldn't list tags")
                continue
            newer = newer_tags(src.tag, tags)
            if newer:
                reasons.append({
                    "kind": "upstream", "part": src.part, "url": src.url,
                    "detail": f"part '{src.part}' pins {src.tag}; latest is {newer[0]}",
                    "pinned": src.tag, "newer": newer[:5],
                })
            continue
        # tracks a branch: we can only tell whether it moved on GitHub
        gh = repo_mod.parse_github(src.url)
        if not gh:
            notes.append(f"{label}: tracks a branch on a non-GitHub host, not checked")
            continue
        q = f"repos/{gh[0]}/{gh[1]}/commits?since={since}&per_page=100"
        if src.branch:
            q += f"&sha={src.branch}"
        code, out = repo_mod.run_gh(["api", q])
        if code == 0 and json.loads(out):
            n = len(json.loads(out))
            reasons.append({
                "kind": "upstream", "part": src.part, "url": src.url,
                "detail": f"part '{src.part}' tracks {src.branch or 'the default branch'}, "
                          f"which has {n}{'+' if n == 100 else ''} new commit(s) since {since[:10]}",
            })
    return reasons


def check_snap(entry: dict, refresh: bool = False) -> dict:
    result = {"checked_at": _now(), "verdict": VERDICT_UNKNOWN, "reasons": [], "notes": [],
              "targets": []}
    notes = result["notes"]
    repo = entry.get("repo") or {}
    if entry.get("status") != "ok":
        notes.append(f"discovery status is {entry.get('status')!r}; fix the repo first")
        return result

    # the channel map may have moved since discovery
    try:
        info = store.public_info(entry["name"])
        channels = info.channels if info else store.private_channels(entry["name"])
        entry["channels"] = [c.__dict__ for c in channels]
    except store.StoreError as e:
        notes.append(f"using the channel map from discovery: {e}")
    try:
        revs = store.revisions(entry["name"])
    except store.StoreError as e:
        notes.append(str(e))
        revs = {}
    targets = reference_targets(entry.get("channels", []), revs)

    def summarize_targets() -> list[dict]:
        return [{k: t.get(k) for k in ("track", "risk", "arch", "revision", "version",
                                       "uploaded", "security_method")} for t in targets]

    result["targets"] = summarize_targets()
    if not targets:
        result["verdict"] = VERDICT_UNPUBLISHED
        return result
    if not any(t.get("uploaded") for t in targets):
        notes.append("couldn't determine when the published revisions were uploaded")
        return result

    code, text = repo_mod.run_gh([
        "api", "-H", "Accept: application/vnd.github.raw",
        f"repos/{repo['owner']}/{repo['name']}/contents/{repo['snapcraft_yaml']}"
        f"?ref={repo['default_branch']}",
    ])
    if code != 0:
        notes.append(f"couldn't read {repo['snapcraft_yaml']}: {text.strip()[:200]}")
        return result
    try:
        sy = snapcraft_yaml.parse(text)
    except Exception as e:
        notes.append(f"couldn't parse {repo['snapcraft_yaml']}: {e}")
        return result

    reasons = result["reasons"]
    for step in (lambda: security_reasons(entry["name"], sy, targets, refresh, notes),
                 lambda: source_reasons(repo, sy, targets),
                 lambda: upstream_reasons(sy, targets, notes)):
        try:
            reasons.extend(step())
        except Exception as e:  # one failing signal shouldn't hide the others
            notes.append(f"check failed: {type(e).__name__}: {e}")
    result["targets"] = summarize_targets()

    kinds = {r["kind"] for r in reasons}
    if "security" in kinds:
        result["verdict"] = VERDICT_REBUILD
    elif kinds:
        result["verdict"] = VERDICT_UPDATE
    elif not any(n.startswith("check failed") for n in notes):
        result["verdict"] = VERDICT_OK
    return result
