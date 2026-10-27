"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from snap_maintainer import checks, discovery, store


def _prompt(name: str, hint: str | None) -> str | None:
    msg = f"\n{name}: couldn't find its source repo ({hint}).\n  Repo URL (blank to skip): "
    try:
        return input(msg).strip() or None
    except EOFError:
        return None


def _channels_summary(channels: list[dict]) -> str:
    stable = [c for c in channels if c["risk"] == "stable"]
    shown = stable or channels
    if not shown:
        return "-"
    versions = sorted({c["version"] or "?" for c in shown})
    archs = sorted({c["arch"] for c in shown})
    return f"{shown[0]['risk']} {'/'.join(versions)} ({','.join(archs)})"


def print_table(inventory: dict) -> None:
    rows = [("SNAP", "STATUS", "PUBLISHED", "REPO", "SNAPCRAFT.YAML")]
    for name in sorted(inventory.get("snaps", {})):
        s = inventory["snaps"][name]
        r = s.get("repo") or {}
        repo = r.get("url", "-")
        if r.get("source") and r["source"] != "store-links":
            repo += f" [{r['source']}]"
        if r.get("redirected_from"):
            repo += f" (moved from {r['redirected_from'].rsplit('/', 1)[-1]})"
        rows.append((name, s.get("status", "?"), _channels_summary(s.get("channels", [])),
                     repo, r.get("snapcraft_yaml") or "-"))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip())
    by_repo: dict[str, list[str]] = {}
    for n, s in inventory.get("snaps", {}).items():
        if (s.get("repo") or {}).get("url"):
            by_repo.setdefault(s["repo"]["url"], []).append(n)
    for url, names in sorted(by_repo.items()):
        if len(names) > 1:
            print(f"\nnote: {', '.join(sorted(names))} share {url}; "
                  "if that's wrong, use set-repo to point each at its own repo")
    problems = [n for n, s in inventory.get("snaps", {}).items() if s.get("status") != "ok"]
    if problems:
        print(f"\n{len(problems)} snap(s) need attention. Fix a repo with: "
              "snap-maintainer set-repo <snap> <url>")


_VERDICT_ORDER = [checks.VERDICT_REBUILD, checks.VERDICT_UPDATE, checks.VERDICT_UNKNOWN,
                  checks.VERDICT_UNPUBLISHED, checks.VERDICT_OK]


def _reason_line(r: dict) -> str:
    if r["kind"] == "security":
        staged = f"has {r['staged_version']}, " if r.get("staged_version") else ""
        return (f"{r['usn']} ({r['severity']}, {r['issued'][:10]}): {r['source']} "
                f"{staged}fixed in {r['fixed_version']} [{', '.join(r['arches'])}]")
    return r["detail"]


def print_check_report(snaps: dict[str, dict], verbose: bool = False) -> None:
    order = sorted(snaps, key=lambda n: (_VERDICT_ORDER.index(snaps[n]["update_check"]["verdict"]), n))
    for name in order:
        res = snaps[name]["update_check"]
        pub = ", ".join(
            f"{t['risk']} r{t['revision']} {t['arch']}"
            + (f" [{t['security_method']}]" if t.get("security_method") else "")
            for t in res["targets"]) or "-"
        print(f"{name}: {res['verdict'].upper()}  ({pub})")
        reasons = res["reasons"] if verbose else res["reasons"][:3]
        for r in reasons:
            print(f"    {r['kind']:<9} {_reason_line(r)}")
            if verbose:
                for cm in r.get("commits", []):
                    print(f"              {cm['sha']} {cm['message'][:70]}")
        if len(res["reasons"]) > len(reasons):
            print(f"    ... and {len(res['reasons']) - len(reasons)} more (use -v)")
        notes = res["notes"] if verbose else [n for n in res["notes"] if n.startswith("check failed")
                                              or res["verdict"] == checks.VERDICT_UNKNOWN]
        for n in notes:
            print(f"    note      {n}")
    counts = {v: sum(1 for n in snaps if snaps[n]["update_check"]["verdict"] == v)
              for v in _VERDICT_ORDER}
    print("\n" + ", ".join(f"{c} {v}" for v, c in counts.items() if c))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="snap-maintainer", description=__doc__)
    p.add_argument("-i", "--inventory", type=Path, default=Path("inventory.json"),
                   help="inventory file (default: ./inventory.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="scan your published snaps and locate their repos")
    d.add_argument("snaps", nargs="*", help="only these snaps (default: all)")
    d.add_argument("--no-input", action="store_true",
                   help="never prompt for missing repos (default: prompt when on a TTY)")

    sr = sub.add_parser("set-repo", help="set a snap's source repo manually")
    sr.add_argument("snap")
    sr.add_argument("url")

    sh = sub.add_parser("show", help="print the current inventory")
    sh.add_argument("--json", action="store_true")

    c = sub.add_parser("check", help="decide which discovered snaps need a rebuild or release")
    c.add_argument("snaps", nargs="*", help="only these snaps (default: all)")
    c.add_argument("-v", "--verbose", action="store_true", help="show every reason and note")
    c.add_argument("--refresh", action="store_true",
                   help="re-download archive and security data even if cached today")

    args = p.parse_args(argv)
    try:
        if args.cmd == "check":
            inv = discovery.load(args.inventory)
            names = args.snaps or sorted(inv.get("snaps", {}))
            unknown = [n for n in names if n not in inv.get("snaps", {})]
            if not names or unknown:
                print(f"error: not in the inventory: {', '.join(unknown) or 'anything'}; "
                      "run 'snap-maintainer discover' first", file=sys.stderr)
                return 1
            for n in names:
                print(f"checking {n}...", file=sys.stderr)
                inv["snaps"][n]["update_check"] = checks.check_snap(inv["snaps"][n], args.refresh)
                discovery.save(args.inventory, inv)
            print(file=sys.stderr)
            print_check_report({n: inv["snaps"][n] for n in names}, args.verbose)
        elif args.cmd == "discover":
            ask = None if args.no_input or not sys.stdin.isatty() else _prompt
            inv = discovery.discover(
                args.inventory, only=args.snaps or None, ask=ask,
                progress=lambda n: print(f"scanning {n}...", file=sys.stderr),
            )
            print(file=sys.stderr)
            print_table(inv)
        elif args.cmd == "set-repo":
            s = discovery.set_repo(args.inventory, args.snap, args.url)
            r = s["repo"]
            print(f"{args.snap}: {r['url']} -> {s['status']}"
                  + (f" ({r['error']})" if r.get("error") else ""))
        elif args.cmd == "show":
            inv = discovery.load(args.inventory)
            print(json.dumps(inv, indent=2)) if args.json else print_table(inv)
    except store.StoreError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; partial results saved", file=sys.stderr)
        return 130
    return 0
