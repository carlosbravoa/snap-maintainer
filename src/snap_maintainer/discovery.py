"""Milestone 1: build an inventory of published snaps and their source repos."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from snap_maintainer import builders
from snap_maintainer import repo as repo_mod
from snap_maintainer import store

# Repos set explicitly by the user are never overwritten by discovery.
USER_SOURCES = ("user", "prompt")

STATUS_OK = "ok"
STATUS_NO_YAML = "no-snapcraft-yaml"
STATUS_UNREACHABLE = "repo-unreachable"
STATUS_NEEDS_REPO = "needs-repo"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"snaps": {}}


def save(path: Path, inventory: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(inventory, indent=2, sort_keys=False) + "\n")
    tmp.replace(path)


def status_of(r: dict | None) -> str:
    if not r:
        return STATUS_NEEDS_REPO
    if not r.get("verified"):
        return STATUS_UNREACHABLE
    return STATUS_OK if r.get("snapcraft_yaml") else STATUS_NO_YAML


def resolve_repo(
    name: str,
    links: dict[str, list[str]],
    previous: dict | None,
    gh_login: str | None,
    ask: Callable[[str, str | None], str | None] | None,
) -> repo_mod.Repo | None:
    """Pick the source repo for a snap, trying candidates in order of trust."""
    if previous and previous.get("source") in USER_SOURCES:
        return repo_mod.inspect(repo_mod.Repo(url=previous["url"], source=previous["source"]))

    candidates = repo_mod.candidates_from_store(links)
    if gh_login:
        guess = f"https://github.com/{gh_login}/{name}"
        if guess not in (c[0] for c in candidates):
            candidates.append((guess, "guess"))

    first_failure = None
    for url, source in candidates:
        r = repo_mod.inspect(repo_mod.Repo(url=url, source=source))
        if r.verified:
            return r
        # a failed guess is noise; a failed store link is worth reporting
        if source != "guess" and first_failure is None:
            first_failure = r

    if ask:
        hint = first_failure.error if first_failure else "no repo link in the store"
        url = ask(name, hint)
        if url:
            return repo_mod.inspect(repo_mod.Repo(url=repo_mod.normalize(url), source="prompt"))
    return first_failure


def discover_snap(
    entry: dict,
    previous: dict | None,
    gh_login: str | None,
    ask: Callable[[str, str | None], str | None] | None,
    lp_recipes: Callable[[str], list[dict] | None] = lambda owner: [],
) -> dict:
    name = entry["name"]
    errors: list[str] = []
    info = None
    try:
        info = store.public_info(name)
    except store.StoreError as e:
        errors.append(str(e))
    if info is None:
        # private or unreleased: fall back to the authenticated CLI
        info = store.StoreInfo()
        try:
            info.channels = store.private_channels(name)
        except store.StoreError as e:
            errors.append(str(e))

    r = resolve_repo(name, info.links, (previous or {}).get("repo"), gh_login, ask)
    repo = r.to_dict() if r else None
    record = {
        **_snap_record(entry, info, repo, errors),
        "build": detect_build(name, repo, (previous or {}).get("build"), lp_recipes),
    }
    # keep the last check's results; `check` refreshes them
    if previous and previous.get("update_check"):
        record["update_check"] = previous["update_check"]
    return record


def detect_build(name: str, repo: dict | None, previous: dict | None,
                 lp_recipes: Callable[[str], list[dict] | None]) -> dict:
    if previous and previous.get("source") == "user":
        return previous
    if not repo or not repo.get("verified"):
        return {"method": "unknown", "source": "detected",
                "evidence": "the repo isn't resolved yet", "alternatives": [], "notes": []}
    return builders.detect(name, repo, lp_recipes(repo["owner"]))


def _snap_record(entry: dict, info, repo: dict | None, errors: list[str]) -> dict:
    name = entry["name"]
    return {
        "name": name,
        "visibility": entry.get("visibility"),
        "registered": entry.get("since"),
        "title": info.title,
        "summary": info.summary,
        "links": info.links,
        "channels": [c.__dict__ for c in info.channels],
        "repo": repo,
        "status": status_of(repo),
        "errors": errors,
        "discovered_at": now(),
    }


def discover(
    path: Path,
    only: list[str] | None = None,
    ask: Callable[[str, str | None], str | None] | None = None,
    progress: Callable[[str, int, int], None] = lambda *_: None,
) -> dict:
    inventory = load(path)
    snaps = inventory.setdefault("snaps", {})
    login = repo_mod.gh_login()
    lp_cache: dict[str, list[dict] | None] = {}

    def lp_recipes(owner: str) -> list[dict] | None:
        if owner not in lp_cache:
            lp_cache[owner] = builders.launchpad_recipes(owner)
        return lp_cache[owner]

    registered = store.registered_snaps()
    if only:
        unknown = set(only) - {s["name"] for s in registered}
        if unknown:
            raise store.StoreError(f"not registered to you: {', '.join(sorted(unknown))}")
        registered = [s for s in registered if s["name"] in only]

    for i, entry in enumerate(registered):
        progress(entry["name"], i, len(registered))
        snaps[entry["name"]] = discover_snap(entry, snaps.get(entry["name"]), login, ask,
                                             lp_recipes)
        save(path, inventory)  # persist as we go so an interrupted run isn't wasted

    if not only:
        # names no longer registered to this account
        current = {s["name"] for s in registered}
        for gone in set(snaps) - current:
            snaps[gone]["status"] = "unregistered"
    inventory["updated_at"] = now()
    save(path, inventory)
    return inventory


def set_repo(path: Path, name: str, url: str) -> dict:
    inventory = load(path)
    snap = inventory.setdefault("snaps", {}).setdefault(name, {"name": name})
    r = repo_mod.inspect(repo_mod.Repo(url=repo_mod.normalize(url), source="user")).to_dict()
    snap["repo"], snap["status"] = r, status_of(r)
    snap["build"] = detect_build(name, r, snap.get("build"), builders.launchpad_recipes)
    save(path, inventory)
    return snap


def set_build(path: Path, name: str, method: str, workflow: str | None = None) -> dict:
    """Pin a snap's builder by hand; discovery never overwrites it."""
    inventory = load(path)
    snap = inventory.get("snaps", {}).get(name)
    if snap is None:
        raise store.StoreError(f"{name} isn't in the inventory; run discover first")
    build = {"method": method, "source": "user", "evidence": "set by you",
             "alternatives": [], "notes": []}
    if method == "github-workflow":
        repo = snap.get("repo") or {}
        if not repo.get("verified"):
            raise store.StoreError(f"{name} has no verified GitHub repo; set it with set-repo first")
        found = {w["path"]: w for w in builders.workflow_candidates(repo)}
        if workflow is None:
            uploading = [w for w in found.values() if w["uploads"]] or list(found.values())
            if not uploading:
                raise store.StoreError(f"no workflow in {repo['url']} builds a snap; "
                                       "pass --workflow to choose one anyway")
            workflow = uploading[0]["path"]
        elif not workflow.startswith(".github/"):
            workflow = f".github/workflows/{workflow}"
        build["workflow"] = found.get(workflow) or {"path": workflow, "uploads": None}
        build["evidence"] = f"set by you: {workflow}"
    snap["build"] = build
    save(path, inventory)
    return snap
