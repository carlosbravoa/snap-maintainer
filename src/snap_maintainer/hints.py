"""Rebuild plans: how a person (or an agent) can rebuild and release a snap.

Nothing here runs a build or touches the store. Building and releasing need
someone watching, so the tool stops at writing down the steps.
"""

from __future__ import annotations

import platform

from snap_maintainer import checks

NEEDS_WORK = (checks.VERDICT_REBUILD, checks.VERDICT_UPDATE)

_HOST_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armhf", "ppc64le": "ppc64el",
              "s390x": "s390x", "riscv64": "riscv64"}


def host_arch() -> str:
    return _HOST_ARCH.get(platform.machine(), platform.machine())


def _channel(t: dict) -> str:
    return t["risk"] if t["track"] == "latest" else f"{t['track']}/{t['risk']}"


def _why(res: dict) -> list[str]:
    reasons = res.get("reasons", [])
    out = []
    sec = [r for r in reasons if r["kind"] == "security"]
    if sec:
        pkgs = sorted({r["source"] for r in sec})
        out.append(f"{len(sec)} security notice(s) for {', '.join(pkgs)}")
    out += [r["detail"] for r in reasons if r["kind"] in ("source", "upstream")]
    return out


def _step(title: str, commands: list[str] | None = None, note: str | None = None) -> dict:
    return {"title": title, "commands": commands or [], "note": note}


def _upload_steps(snap: str, channels: str, arches: list[str], version: str | None) -> list[dict]:
    files = [f"{snap}_{version or '<version>'}_{a}.snap" for a in arches]
    return [
        _step("Check the snap embeds its build manifest (exact security checks need it)",
              [f"unsquashfs -l {f} | grep snap/manifest.yaml" for f in files]),
        _step(f"Upload and release to {channels}",
              [f"snapcraft upload --release={channels} {f}" for f in files]),
    ]


def plan(entry: dict) -> dict:
    """The rebuild plan for one inventory entry (after ``check``)."""
    name = entry["name"]
    res = entry.get("update_check") or {}
    repo = entry.get("repo") or {}
    build = entry.get("build") or {"method": "unknown"}
    targets = res.get("targets") or []
    channels = ",".join(sorted({_channel(t) for t in targets})) or "<channel>"
    arches = sorted({t["arch"] for t in targets}) or [host_arch()]
    versions = sorted({t["version"] for t in targets if t.get("version")})
    reasons = res.get("reasons", [])
    security_only = bool(reasons) and all(r["kind"] == "security" for r in reasons)
    # a security-only rebuild keeps the published version, so file names are known
    file_version = versions[0] if security_only and len(versions) == 1 else None
    has_version_reason = any(r["kind"] == "source" and "declares version" in r.get("detail", "")
                             for r in reasons)

    p = {
        "snap": name,
        "verdict": res.get("verdict"),
        "why": _why(res),
        "builder": build.get("method"),
        "builder_detail": build.get("evidence"),
        "channels": channels,
        "arches": arches,
        "published_version": "/".join(versions) or None,
        "repo": repo.get("url"),
        "steps": [],
        "notes": [],
    }
    steps, notes = p["steps"], p["notes"]

    if security_only:
        notes.append("No code changes are needed: rebuilding picks up the fixed packages "
                     "from the Ubuntu archive. Keeping the same version is fine.")
    elif reasons:
        notes.append("The repo has changes that aren't published yet. Review them before "
                     "releasing, and bump `version:` in snapcraft.yaml if they're user-facing."
                     + ("" if not has_version_reason else " The repo already declares a new version."))

    owner_repo = f"{repo.get('owner')}/{repo.get('name')}" if repo.get("owner") else None
    method = build.get("method")

    if method == "github-workflow":
        wf = build.get("workflow") or {}
        file = (wf.get("path") or "<workflow>").rsplit("/", 1)[-1]
        trig = wf.get("triggers") or {}
        r = f" -R {owner_repo}" if owner_repo else ""
        if trig.get("dispatch"):
            steps.append(_step(f"Run the {file} workflow", [f"gh workflow run {file}{r}"]))
        elif trig.get("tags"):
            pattern = trig["tags"][0]
            steps.append(_step(
                f"Push a new tag matching {pattern} to run {file}",
                ["git tag <new-tag>", "git push origin <new-tag>"],
                note=f"Published version: {p['published_version'] or 'unknown'}. "
                     "A tag can't be reused, so even a security-only rebuild needs a new one."))
        elif trig.get("release"):
            steps.append(_step(f"Publish a GitHub release to run {file}",
                               [f"gh release create <new-tag>{r} --generate-notes"]))
        elif trig.get("push") or trig.get("branches"):
            branch = (trig.get("branches") or [repo.get("default_branch") or "main"])[0]
            steps.append(_step(f"Push a commit to {branch} to run {file}",
                               [f"git commit --allow-empty -m 'Rebuild snap' && git push origin {branch}"]
                               if security_only else []))
        else:
            steps.append(_step(f"Start the {file} workflow the way it's configured"))
        steps.append(_step("Watch the run", [f"gh run watch{r}"]))
        if wf.get("uploads") is False:
            steps.append(_step("Download the built snaps",
                               [f"gh run download <run-id>{r}"],
                               note="This workflow builds but doesn't upload."))
            steps += _upload_steps(name, channels, arches, file_version)
        elif wf.get("channels"):
            notes.append(f"The workflow releases to {', '.join(wf['channels'])}; "
                         f"the published channel is {channels}.")
        else:
            notes.append(f"The workflow decides the channel; the published channel is {channels}.")

    elif method == "launchpad":
        rec = build.get("recipe") or {}
        url = rec.get("url") or "<recipe page>"
        if rec.get("auto_build") and not security_only:
            notes.append("This recipe builds automatically when the repo changes.")
        steps.append(_step("Request a build on Launchpad", [f"{url}/+request-builds"],
                           note="Open the page, pick the architectures, and request builds."))
        if not rec.get("store_upload"):
            steps.append(_step("Download the built snaps from the build pages"))
            steps += _upload_steps(name, channels, arches, file_version)
        else:
            steps.append(_step(f"Launchpad uploads to the store; release to {channels} if it "
                               "doesn't do that itself",
                               [f"snapcraft release {name} <revision> {channels}"]))

    elif method == "local":
        dirname = repo.get("name") or name
        clone = (f"git clone {repo['url']} && cd {dirname}" if repo.get("url")
                 else "cd <your checkout>")
        steps.append(_step("Get the latest source", [clone]))
        foreign = [a for a in arches if a != host_arch()]
        if foreign:
            steps.append(_step(
                f"Build for {', '.join(arches)} on Launchpad (this machine is {host_arch()})",
                [f"snapcraft remote-build --build-for={','.join(arches)}"],
                note="remote-build needs a Launchpad account and makes the source public "
                     "on Launchpad."))
        else:
            steps.append(_step("Build with the manifest embedded",
                               ["SNAPCRAFT_BUILD_INFO=1 snapcraft pack"],
                               note="Set the variable on the same command line; a separate "
                                    "`export` is easy to lose."))
        steps += _upload_steps(name, channels, arches, file_version)

    elif method == "manual":
        notes.append("You marked this snap as built outside snap-maintainer.")
        steps += _upload_steps(name, channels, arches, file_version)

    else:
        notes.append("The build method isn't known: run `snap-maintainer discover` "
                     f"or `snap-maintainer set-build {name} <method>`.")

    steps.append(_step("Confirm the new revision is clean", [f"snap-maintainer check {name}"]))
    return p


def plans(snaps: dict[str, dict], names: list[str] | None = None,
          include_all: bool = False) -> list[dict]:
    """Plans for the named snaps, or for every snap whose verdict needs work."""
    out = []
    for name in names or sorted(snaps):
        verdict = (snaps[name].get("update_check") or {}).get("verdict")
        if names or include_all or verdict in NEEDS_WORK:
            out.append(plan(snaps[name]))
    order = [checks.VERDICT_REBUILD, checks.VERDICT_UPDATE]
    return sorted(out, key=lambda p: (order.index(p["verdict"]) if p["verdict"] in order else 9,
                                      p["snap"]))


def to_markdown(items: list[dict]) -> str:
    lines = ["# Snap rebuild plan", ""]
    if not items:
        return "\n".join(lines + ["Nothing needs a rebuild or release.", ""])
    for p in items:
        verdict = (p["verdict"] or "unchecked").replace("-", " ")
        lines += [f"## {p['snap']} ({verdict})", ""]
        if p["why"]:
            lines += ["Why:", ""] + [f"- {w}" for w in p["why"]] + [""]
        builder = p["builder_detail"] or p["builder"]
        lines += [f"Builder: {p['builder']} ({builder})  ",
                  f"Release to: `{p['channels']}` for {', '.join(p['arches'])}"
                  + (f"  \nRepo: {p['repo']}" if p["repo"] else ""), ""]
        for i, s in enumerate(p["steps"], 1):
            lines.append(f"{i}. {s['title']}")
            if s["commands"]:
                lines += ["", "   ```sh"] + [f"   {c}" for c in s["commands"]] + ["   ```", ""]
            if s["note"]:
                lines += [f"   {s['note']}", ""]
        lines += [f"> {n}" for n in p["notes"]] + [""]
    return "\n".join(lines)

