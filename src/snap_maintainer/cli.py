"""Discover your published snaps and find out which need a rebuild or release."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from snap_maintainer import builders, checks, discovery, preflight, store, ui

EXIT_ERROR = 1
EXIT_PREFLIGHT = 2


def _check_targets(args) -> tuple[dict, list[str]] | None:
    """Validate the inventory and snap names before anything slow runs."""
    inv = discovery.load(args.inventory)
    known = inv.get("snaps", {})
    if not known:
        ui.error(f"No snaps in {args.inventory}.", "Run: snap-maintainer discover")
        return None
    names = args.snaps or sorted(known)
    unknown = [n for n in names if n not in known]
    if unknown:
        ui.error(f"Not in the inventory: {', '.join(unknown)}.",
                 "Run: snap-maintainer discover " + " ".join(unknown))
        return None
    return inv, names


def _check(args, inv: dict, names: list[str]) -> int:
    known = inv["snaps"]
    with ui.progress("Checking") as (update, _):
        for i, n in enumerate(names):
            update(n, i, len(names))
            known[n]["update_check"] = checks.check_snap(known[n], args.refresh)
            discovery.save(args.inventory, inv)
        update("", len(names), len(names))
    ui.check_report({n: known[n] for n in names}, args.verbose)
    return 0


def _discover(args) -> int:
    with ui.progress("Discovering") as (update, pause):
        def ask(name: str, hint: str | None) -> str | None:
            with pause():
                return ui.prompt_repo(name, hint)

        interactive = not args.no_input and sys.stdin.isatty()
        inv = discovery.discover(args.inventory, only=args.snaps or None,
                                 ask=ask if interactive else None, progress=update)
    ui.inventory_table(inv)
    return 0


def _set_repo(args) -> int:
    s = discovery.set_repo(args.inventory, args.snap, args.url)
    r = s["repo"]
    if s["status"] == "ok":
        ui.success(f"[bold]{args.snap}[/] → [link {r['url']}]{r['url']}[/] "
                   f"[grey50]({r['snapcraft_yaml']})[/]")
        return 0
    ui.error(f"{args.snap} → {r['url']}: {s['status']}", r.get("error"))
    return EXIT_ERROR


def _set_build(args) -> int:
    s = discovery.set_build(args.inventory, args.snap, args.method, args.workflow)
    ui.success(ui.Text.assemble((args.snap, "bold"), " builds via ", ui.build_cell(s["build"])))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="snap-maintainer", description=__doc__)
    p.add_argument("-i", "--inventory", type=Path, default=Path("inventory.json"),
                   help="inventory file (default: ./inventory.json)")
    p.add_argument("--debug", action="store_true", help="show tracebacks on errors")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="scan your published snaps and locate their repos")
    d.add_argument("snaps", nargs="*", help="only these snaps (default: all)")
    d.add_argument("--no-input", action="store_true",
                   help="never prompt for missing repos (default: prompt when on a TTY)")

    sr = sub.add_parser("set-repo", help="set a snap's source repo manually")
    sr.add_argument("snap")
    sr.add_argument("url")

    sb = sub.add_parser("set-build", help="set how a snap gets rebuilt (overrides detection)")
    sb.add_argument("snap")
    sb.add_argument("method", choices=builders.METHODS)
    sb.add_argument("--workflow", help="workflow file for github-workflow (default: the "
                                       "detected one that uploads)")

    sh = sub.add_parser("show", help="print the current inventory")
    sh.add_argument("--json", action="store_true")

    c = sub.add_parser("check", help="decide which discovered snaps need a rebuild or release")
    c.add_argument("snaps", nargs="*", help="only these snaps (default: all)")
    c.add_argument("-v", "--verbose", action="store_true", help="show every reason and note")
    c.add_argument("--refresh", action="store_true",
                   help="re-download archive and security data even if cached today")

    args = p.parse_args(argv)
    try:
        if args.cmd == "show":
            inv = discovery.load(args.inventory)
            if args.json:
                print(json.dumps(inv, indent=2))
            else:
                ui.inventory_table(inv)
            return 0
        if args.cmd == "check":
            targets = _check_targets(args)
            if targets is None:
                return EXIT_ERROR
        if not ui.preflight(preflight.run(args.cmd)):
            return EXIT_PREFLIGHT
        if args.cmd == "check":
            return _check(args, *targets)
        return {"discover": _discover, "set-repo": _set_repo,
                "set-build": _set_build}[args.cmd](args)
    except store.StoreError as e:
        ui.error(str(e))
        return EXIT_ERROR
    except KeyboardInterrupt:
        ui.error("Interrupted.", "Results so far are saved in the inventory.")
        return 130
    except json.JSONDecodeError as e:
        ui.error(f"{args.inventory} isn't valid JSON: {e}",
                 "Fix or delete it, then run: snap-maintainer discover")
        return EXIT_ERROR
    except Exception as e:
        if args.debug:
            traceback.print_exc()
        ui.error(f"Unexpected error: {type(e).__name__}: {e}",
                 None if args.debug else "Run again with --debug for the full traceback.")
        return EXIT_ERROR
