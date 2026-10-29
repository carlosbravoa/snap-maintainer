"""MCP server: lets an agent discover, check and plan rebuilds of the user's snaps.

Like the CLI, it never builds, uploads or releases. Run with `snap-maintainer mcp`
(stdio transport). Nothing here may print to stdout: that's the protocol channel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from snap_maintainer import checks, discovery, hints, preflight, store

INSTRUCTIONS = """\
snap-maintainer reports which of the user's published snaps (on the Snap Store) need a
rebuild or a new release, and how to do it. It never builds, uploads or releases
anything itself.

Typical flow:
1. get_inventory: see what's known. If it's empty, run discover_snaps.
2. check_snaps: verdicts per snap (rebuild for security fixes, update for unreleased
   changes, up-to-date). The first run can take a few minutes.
3. rebuild_plan: step-by-step commands to rebuild and release the snaps that need it.

Following a plan means uploading and releasing to the Snap Store, which is public and
hard to undo. Show the plan to the user and get their explicit confirmation before
running any `snapcraft upload`, `snapcraft release`, tag push or workflow run, one snap
at a time. After a release, run check_snaps on that snap to confirm it's clean.
"""

server = MCPServer(
    name="snap-maintainer",
    title="Snap maintainer",
    description="Find which published snaps need a rebuild or release, and how to do it.",
    instructions=INSTRUCTIONS,
)

_lock = anyio.Lock()  # one inventory writer at a time


INVENTORY = discovery.default_inventory()


def _require(command: str) -> None:
    """Raise a ToolError naming the fix if a required tool or login is missing."""
    failed = [c for c in preflight.run(command) if c.required and not c.ok]
    if failed:
        raise ToolError("; ".join(f"{c.name}: {c.detail} (fix: {c.fix})" for c in failed)
                        + ". The user needs to fix this in a terminal.")


def _snaps(names: list[str] | None) -> tuple[dict, list[str]]:
    inv = discovery.load(INVENTORY)
    known = inv.get("snaps", {})
    if not known:
        raise ToolError("The inventory is empty: run discover_snaps first.")
    unknown = [n for n in names or [] if n not in known]
    if unknown:
        raise ToolError(f"Not in the inventory: {', '.join(unknown)}. Known snaps: "
                        f"{', '.join(sorted(known))}. Run discover_snaps if they're new.")
    return inv, names or sorted(known)


def _summary(s: dict) -> dict[str, Any]:
    """A compact view of one inventory entry."""
    repo = s.get("repo") or {}
    build = s.get("build") or {}
    res = s.get("update_check") or {}
    return {
        "snap": s["name"],
        "visibility": s.get("visibility"),
        "status": s.get("status"),
        "published": sorted({f"{c['track']}/{c['risk']} {c.get('version')} r{c.get('revision')} "
                             f"{c['arch']}" for c in s.get("channels", [])}),
        "repo": repo.get("url"),
        "repo_found_via": repo.get("source"),
        "repo_error": None if repo.get("verified") else repo.get("error"),
        "builder": build.get("method"),
        "builder_evidence": build.get("evidence"),
        "verdict": res.get("verdict"),
        "checked_at": res.get("checked_at"),
    }


def _check_result(name: str, res: dict) -> dict[str, Any]:
    reasons = []
    for r in res.get("reasons", []):
        if r["kind"] == "security":
            reasons.append({k: r.get(k) for k in ("kind", "usn", "title", "severity", "issued",
                                                   "source", "staged_version", "fixed_version",
                                                   "arches", "method")})
        else:
            reasons.append({k: r[k] for k in ("kind", "detail", "commits") if k in r})
    return {"snap": name, "verdict": res.get("verdict"), "published": res.get("targets"),
            "reasons": reasons, "notes": res.get("notes", [])}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def get_inventory(snaps: list[str] | None = None) -> dict[str, Any]:
    """List the user's known snaps: published channels, source repo, how each is built,
    and the verdict of the last check. Reads the local inventory only (fast, no network).

    Args:
        snaps: only these snap names (default: all)
    """
    inv = discovery.load(INVENTORY)
    known = inv.get("snaps", {})
    names = [n for n in (snaps or sorted(known)) if n in known]
    return {
        "inventory": str(INVENTORY),
        "updated_at": inv.get("updated_at"),
        "snaps": [_summary(known[n]) for n in names],
        "hint": None if known else "The inventory is empty: run discover_snaps.",
    }


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=True, openWorldHint=True))
async def discover_snaps(snaps: list[str] | None = None, ctx: Context | None = None) -> dict[str, Any]:
    """Scan the user's Snap Store account: registered snaps, published channels, their
    source repos (GitHub) and how each is built. Saves the result to the inventory.
    Repos and builders the user set by hand are kept. Takes a few seconds per snap.

    Args:
        snaps: only these snap names (default: every snap registered to the account)
    """
    await anyio.to_thread.run_sync(_require, "discover")

    def progress(name: str, i: int, total: int) -> None:
        if ctx is not None:
            anyio.from_thread.run(ctx.report_progress, i, total, f"discovering {name}")

    async with _lock:
        try:
            inv = await anyio.to_thread.run_sync(
                lambda: discovery.discover(INVENTORY, only=snaps, ask=None, progress=progress))
        except store.StoreError as e:
            raise ToolError(str(e)) from e
    entries = [inv["snaps"][n] for n in (snaps or sorted(inv["snaps"]))]
    problems = [e["name"] for e in entries if e.get("status") not in ("ok", "unregistered")]
    return {
        "snaps": [_summary(e) for e in entries],
        "need_attention": problems,
        "hint": ("Some repos couldn't be resolved: ask the user for the right URL and call "
                 "set_repo." if problems else "Next: check_snaps."),
    }


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=True, openWorldHint=True))
async def check_snaps(snaps: list[str] | None = None, refresh: bool = False,
                      ctx: Context | None = None) -> dict[str, Any]:
    """Decide which snaps need a rebuild (security fixes for packages they stage) or a new
    release (unreleased commits, version bump, newer upstream), with the reasons. Saves
    the verdicts to the inventory. Downloads Ubuntu archive/security data and published
    snaps on first use, so it can take a few minutes; later runs are faster.

    Args:
        snaps: only these snap names (default: all in the inventory)
        refresh: re-download Ubuntu archive and security data even if cached today
    """
    inv, names = _snaps(snaps)
    await anyio.to_thread.run_sync(_require, "check")
    results = []
    async with _lock:
        for i, name in enumerate(names):
            if ctx is not None:
                await ctx.report_progress(i, len(names), f"checking {name}")
            entry = inv["snaps"][name]
            entry["update_check"] = await anyio.to_thread.run_sync(
                checks.check_snap, entry, refresh)
            await anyio.to_thread.run_sync(discovery.save, INVENTORY, inv)
            results.append(_check_result(name, entry["update_check"]))
        if ctx is not None:
            await ctx.report_progress(len(names), len(names), "done")
    order = [checks.VERDICT_REBUILD, checks.VERDICT_UPDATE, checks.VERDICT_UNKNOWN,
             checks.VERDICT_UNPUBLISHED, checks.VERDICT_OK]
    results.sort(key=lambda r: (order.index(r["verdict"]) if r["verdict"] in order else 9, r["snap"]))
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"counts": counts, "results": results,
            "hint": "Next: rebuild_plan for the steps to fix the snaps that need work."}


@server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def rebuild_plan(snaps: list[str] | None = None, include_up_to_date: bool = False,
                 format: Literal["markdown", "json"] = "markdown") -> str | list[dict[str, Any]]:
    """Step-by-step instructions to rebuild and release snaps, matched to how each is built
    (GitHub workflow, Launchpad recipe or local snapcraft): why it needs work, the target
    channel and architectures, and copy-paste commands. Runs nothing. Based on the last
    check_snaps results.

    Following a plan uploads and releases to the public Snap Store: show it to the user
    and get explicit confirmation before running any of its upload, release, tag or
    workflow commands.

    Args:
        snaps: only these snap names (default: every snap whose last check says rebuild or update)
        include_up_to_date: also include snaps that are up to date
        format: markdown (readable checklist) or json (structured steps)
    """
    inv, _ = _snaps(snaps)
    items = hints.plans(inv["snaps"], snaps, include_up_to_date)
    return hints.to_markdown(items) if format == "markdown" else items


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=True, openWorldHint=True))
async def set_repo(snap: str, url: str) -> dict[str, Any]:
    """Set a snap's source repository by hand (when discovery couldn't find it or found the
    wrong one). Verifies the repo on GitHub and re-detects how the snap is built.
    Discovery never overwrites a repo set this way.

    Args:
        snap: the snap name
        url: the repository URL, e.g. https://github.com/owner/repo
    """
    await anyio.to_thread.run_sync(_require, "set-repo")
    async with _lock:
        s = await anyio.to_thread.run_sync(discovery.set_repo, INVENTORY, snap, url)
    return _summary(s)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                         idempotentHint=True, openWorldHint=True))
async def set_build(snap: str,
                    method: Literal["github-workflow", "launchpad", "local", "manual"],
                    workflow: str | None = None) -> dict[str, Any]:
    """Set how a snap is built, overriding detection. Discovery keeps this choice.

    Args:
        snap: the snap name
        method: github-workflow, launchpad, local (snapcraft on the user's machine) or
            manual (built some other way)
        workflow: for github-workflow, the workflow file (default: the detected one that uploads)
    """
    await anyio.to_thread.run_sync(_require, "set-build")
    async with _lock:
        try:
            s = await anyio.to_thread.run_sync(discovery.set_build, INVENTORY, snap, method, workflow)
        except store.StoreError as e:
            raise ToolError(str(e)) from e
    return _summary(s)


@server.prompt(title="Maintain my snaps")
def maintain_snaps() -> str:
    """Check the user's snaps and walk them through rebuilding the ones that need it."""
    return ("Check my published snaps with snap-maintainer: refresh the inventory if it's "
            "empty or old, run check_snaps, and summarise which snaps need a rebuild or "
            "release and why. Then show the rebuild plan. Don't upload, release, push tags "
            "or run workflows without asking me first, one snap at a time.")


def run(inventory: Path | None = None) -> None:
    global INVENTORY
    if inventory is not None:
        INVENTORY = inventory
    INVENTORY.parent.mkdir(parents=True, exist_ok=True)
    server.run("stdio")
