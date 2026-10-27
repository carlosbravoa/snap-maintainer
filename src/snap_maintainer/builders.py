"""Milestone 3a: work out how each snap gets built and uploaded (detection only).

Builders, in order of preference when several apply:
  github-workflow  a GitHub Actions workflow builds the snap (and usually uploads it)
  launchpad        a Launchpad snap recipe builds from the repo (and usually uploads it)
  local            nothing automated found: build with snapcraft on this machine
  manual           set by the user: rebuilds happen outside this tool
"""

from __future__ import annotations

import json
import re
import urllib.request

import yaml

from snap_maintainer import repo as repo_mod

METHODS = ("github-workflow", "launchpad", "local", "manual")

LP_SNAPS_BY_PREFIX = ("https://api.launchpad.net/devel/+snaps?ws.op=findByURLPrefix"
                      "&url_prefix={prefix}&ws.size=300")

_BUILD_ACTIONS = ("snapcore/action-build", "canonical/action-build")
_PUBLISH_ACTIONS = ("snapcore/action-publish", "canonical/action-publish")
_BUILD_CMD = re.compile(r"\bsnapcraft(\s+(pack|snap|remote-build)\b|\s*$|\s+--(?!classic))")
_UPLOAD_CMD = re.compile(r"\bsnapcraft\s+(upload|push)\b")
_RELEASE_ARG = re.compile(r"--release[= ]+[\"']?([\w/,.-]+)")


def _runs_build(run: str) -> bool:
    """A `run:` script that builds a snap (`snapcraft`, `snapcraft pack`, ...), ignoring
    lines that merely install snapcraft."""
    return any(_BUILD_CMD.search(line) for line in run.splitlines()
               if "install" not in line and not line.lstrip().startswith("#"))


def _triggers(doc: dict) -> dict:
    """Normalise a workflow's `on:` (which YAML 1.1 parses as the key True)."""
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = {k: None for k in on}
    elif not isinstance(on, dict):
        on = {}
    out = {"dispatch": "workflow_dispatch" in on, "tags": [], "branches": [],
           "release": "release" in on, "schedule": "schedule" in on, "push": "push" in on}
    push = on.get("push") or {}
    if isinstance(push, dict):
        out["tags"] = [str(t) for t in push.get("tags") or []]
        out["branches"] = [str(b) for b in push.get("branches") or []]
    return out


def analyze_workflow(path: str, text: str) -> dict | None:
    """What a workflow does with snaps, or None if it doesn't build one."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    builds = uploads = False
    how_build = how_upload = None
    channels: set[str] = set()
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            run = str(step.get("run") or "")
            if uses.startswith(_BUILD_ACTIONS):
                builds, how_build = True, uses.split("@")[0]
            elif _runs_build(run):
                builds, how_build = True, how_build or "snapcraft"
            if uses.startswith(_PUBLISH_ACTIONS):
                uploads, how_upload = True, uses.split("@")[0]
                rel = str((step.get("with") or {}).get("release") or "")
                if rel and "$" not in rel:
                    channels.add(rel)
            elif _UPLOAD_CMD.search(run):
                uploads, how_upload = True, how_upload or "snapcraft upload"
                for m in _RELEASE_ARG.finditer(run):
                    if "$" not in m.group(1):
                        channels.add(m.group(1))
    if not builds:
        return None
    return {
        "path": path,
        "name": str(doc.get("name") or path.rsplit("/", 1)[-1]),
        "builds_with": how_build,
        "uploads": uploads,
        "uploads_with": how_upload,
        "channels": sorted(channels),  # empty: none, or decided at run time
        "triggers": _triggers(doc),
    }


def describe_trigger(t: dict) -> str:
    """Short human description of how a workflow gets started."""
    parts = []
    if t.get("tags"):
        parts.append("tag " + ", ".join(t["tags"]))
    if t.get("release"):
        parts.append("GitHub release")
    if t.get("dispatch"):
        parts.append("manual dispatch")
    if t.get("branches"):
        parts.append("push to " + ", ".join(t["branches"]))
    elif t.get("push") and not t.get("tags"):
        parts.append("any push")
    if t.get("schedule"):
        parts.append("schedule")
    return " or ".join(parts) or "unknown trigger"


def workflow_candidates(repo: dict) -> list[dict]:
    out = []
    for path in repo.get("workflows") or []:
        code, text = repo_mod.run_gh([
            "api", "-H", "Accept: application/vnd.github.raw",
            f"repos/{repo['owner']}/{repo['name']}/contents/{path}"
            f"?ref={repo.get('default_branch') or 'HEAD'}",
        ])
        if code == 0:
            wf = analyze_workflow(path, text)
            if wf:
                out.append(wf)
    return out


def launchpad_recipes(owner: str) -> list[dict] | None:
    """Launchpad snap recipes building from github.com/<owner>/*, or None if LP is unreachable."""
    url = LP_SNAPS_BY_PREFIX.format(prefix=f"https://github.com/{owner}/")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.load(resp)
    except Exception:
        return None
    return [
        {
            "name": e.get("name"),
            "owner": (e.get("owner_link") or "").rsplit("~", 1)[-1] or None,
            "url": e.get("web_link"),
            "git_url": e.get("git_repository_url"),
            "git_path": e.get("git_path"),
            "store_name": e.get("store_name"),
            "store_upload": bool(e.get("store_upload")),
            "auto_build": bool(e.get("auto_build")),
            "channels": e.get("store_channels") or [],
        }
        for e in data.get("entries", [])
    ]


def _same_repo(a: str | None, b: str) -> bool:
    return bool(a) and repo_mod.normalize(a).lower() == repo_mod.normalize(b).lower()


def detect(snap_name: str, repo: dict, lp: list[dict] | None) -> dict:
    """Pick a builder for one snap and record the evidence and the alternatives."""
    candidates: list[dict] = []
    notes: list[str] = []

    for wf in workflow_candidates(repo) if repo.get("verified") else []:
        what = "builds and uploads" if wf["uploads"] else "builds (no upload)"
        candidates.append({
            "method": "github-workflow",
            "rank": 0 if wf["uploads"] else 2,
            "workflow": wf,
            "evidence": f"{wf['path']} {what}; runs on {describe_trigger(wf['triggers'])}",
        })

    if lp is None:
        notes.append("Launchpad couldn't be reached; Launchpad recipes weren't checked")
    for r in lp or []:
        if not (_same_repo(r["git_url"], repo.get("url", "")) or
                (repo.get("redirected_from") and _same_repo(r["git_url"], repo["redirected_from"]))):
            continue
        if r["store_name"] and r["store_name"] != snap_name:
            continue
        what = "builds and uploads" if r["store_upload"] else "builds (no upload)"
        auto = ", automatically on new commits" if r["auto_build"] else ""
        candidates.append({
            "method": "launchpad",
            "rank": 1 if r["store_upload"] else 3,
            "recipe": r,
            "evidence": f"Launchpad recipe {r['name']} {what}{auto}",
        })

    candidates.sort(key=lambda c: c["rank"])
    if candidates:
        best = candidates[0]
        chosen = {k: v for k, v in best.items() if k != "rank"}
    else:
        chosen = {"method": "local",
                  "evidence": "no workflow or Launchpad recipe builds this snap"}
        if not repo.get("snapcraft_yaml"):
            notes.append("no snapcraft.yaml found, so a local build isn't possible yet")
    chosen["source"] = "detected"
    chosen["alternatives"] = [c["evidence"] for c in candidates[1:]]
    chosen["notes"] = notes
    return chosen
