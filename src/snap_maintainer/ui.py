"""Console rendering. Colours and live progress only appear on a terminal; piped
output and NO_COLOR get plain text (rich handles both)."""

from __future__ import annotations

from contextlib import contextmanager

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from snap_maintainer import checks
from snap_maintainer.preflight import Check

out = Console()
err = Console(stderr=True)

VERDICT_STYLE = {
    checks.VERDICT_REBUILD: ("bold white on red", "REBUILD", "red"),
    checks.VERDICT_UPDATE: ("bold black on yellow", "UPDATE", "yellow"),
    checks.VERDICT_OK: ("bold black on green", "UP TO DATE", "green"),
    checks.VERDICT_UNKNOWN: ("bold white on magenta", "UNKNOWN", "magenta"),
    checks.VERDICT_UNPUBLISHED: ("bold white on grey37", "UNPUBLISHED", "grey50"),
}
VERDICT_ORDER = list(VERDICT_STYLE)

STATUS_STYLE = {
    "ok": ("green", "●", "ok"),
    "no-snapcraft-yaml": ("yellow", "●", "no snapcraft.yaml"),
    "repo-unreachable": ("red", "●", "repo unreachable"),
    "needs-repo": ("red", "●", "needs repo"),
    "unregistered": ("grey50", "○", "unregistered"),
}

SEVERITY_STYLE = {
    "critical": "bold bright_red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "negligible": "grey50",
}

# every detail box gets the same width (or the terminal's, if narrower)
PANEL_WIDTH = 100

SOURCE_LABEL = {
    "user": "set by you",
    "prompt": "entered by you",
    "guess": "guessed",
    "store-website": "from store website",
}


def badge(verdict: str) -> Text:
    style, label, _ = VERDICT_STYLE.get(verdict, ("bold", verdict.upper(), "white"))
    return Text(f" {label} ", style=style)


def _repo_cell(repo: dict) -> Text:
    if not repo or not repo.get("url"):
        return Text("—", style="grey50")
    url = repo["url"]
    short = url.removeprefix("https://").removeprefix("github.com/")
    t = Text().append(short, style=f"link {url}")  # link only the name, not the labels
    label = SOURCE_LABEL.get(repo.get("source") or "")
    if label:
        t.append(f"  {label}", style="grey50 italic")
    if repo.get("redirected_from"):
        t.append(f"\n↪ moved from {repo['redirected_from'].rsplit('/', 1)[-1]}", style="yellow")
    if repo.get("error") and not repo.get("verified"):
        t.append(f"\n{repo['error'][:80]}", style="red")
    return t


def _published_cell(channels: list[dict]) -> Text:
    stable = [c for c in channels if c["risk"] == "stable"]
    shown = stable or channels
    if not shown:
        return Text("not published", style="grey50")
    versions = sorted({c.get("version") or "?" for c in shown})
    arches = sorted({c["arch"] for c in shown})
    t = Text(f"{shown[0]['risk']} ", style="grey50")
    t.append(" / ".join(versions), style="bold")
    t.append(f"  {' '.join(arches)}", style="grey50")
    return t


# ---------------------------------------------------------------- preflight

def preflight(results: list[Check]) -> bool:
    """Print the tool/login status line; return False if a required check failed."""
    line = Text()
    for c in results:
        if c.ok and c.required:
            line.append("✓ ", style="green").append(c.name, style="bold")
            line.append(f" {c.detail}   ", style="grey50")
    if line:
        err.print(line)
    for c in results:
        if not c.ok and not c.required:
            err.print(Text.assemble(("! ", "yellow"), (c.name, "bold yellow"),
                                    f" {c.detail}: {c.impact}. ", ("Fix: ", "grey50"),
                                    (c.fix or "", "cyan")))
    failed = [c for c in results if not c.ok and c.required]
    if failed:
        body = Table.grid(padding=(0, 2))
        body.add_column(style="bold")
        body.add_column()
        for c in failed:
            body.add_row(Text(f"✗ {c.name}", style="bold red"), c.detail)
            if c.fix:
                body.add_row("", Text.assemble(("fix: ", "grey50"), (c.fix, "bold cyan")))
        err.print(Panel(body, title="[bold red]Can't continue", title_align="left",
                        border_style="red", box=box.ROUNDED, padding=(0, 1), expand=False))
    return not failed


# ---------------------------------------------------------------- progress

@contextmanager
def progress(label: str):
    """Yields (update, pause): update(name, i, total) moves the bar; pause() is a
    context manager that hides the bar while prompting the user."""
    bar = Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=24, complete_style="cyan", finished_style="green"),
        MofNCompleteColumn(),
        TextColumn("[grey50]{task.fields[current]}"),
        TimeElapsedColumn(),
        console=err, transient=True,
    )
    task = bar.add_task(label, total=None, current="")

    def update(name: str, i: int, total: int) -> None:
        bar.update(task, completed=i, total=total, current=name)

    @contextmanager
    def pause():
        bar.stop()
        try:
            yield
        finally:
            bar.start()

    with bar:
        yield update, pause


def prompt_repo(name: str, hint: str | None) -> str | None:
    err.print(Text.assemble(("? ", "bold cyan"), (name, "bold"),
                            (f"  couldn't find its source repo ({hint})", "grey50")))
    try:
        return err.input("  [cyan]Repo URL[/] [grey50](blank to skip)[/]: ").strip() or None
    except EOFError:
        return None


# ---------------------------------------------------------------- inventory

def inventory_table(inventory: dict) -> None:
    snaps = inventory.get("snaps", {})
    if not snaps:
        out.print("[grey50]The inventory is empty. Run [bold cyan]snap-maintainer discover[/] first.")
        return
    table = Table(box=box.SIMPLE_HEAD, header_style="bold grey70", pad_edge=False,
                  title=f"[bold]Your snaps[/]  [grey50]{len(snaps)} registered", title_justify="left")
    table.add_column("Snap", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Published")
    table.add_column("Repository")
    table.add_column("snapcraft.yaml", style="grey50")
    for name in sorted(snaps):
        s = snaps[name]
        color, dot, label = STATUS_STYLE.get(s.get("status"), ("white", "●", s.get("status", "?")))
        name_cell = Text(name)
        if s.get("visibility") == "private":
            name_cell.append(" 🔒", style="grey50")
        table.add_row(name_cell, Text(f"{dot} {label}", style=color),
                      _published_cell(s.get("channels", [])), _repo_cell(s.get("repo") or {}),
                      (s.get("repo") or {}).get("snapcraft_yaml") or "—")
    out.print(table)

    by_repo: dict[str, list[str]] = {}
    for n, s in snaps.items():
        if (s.get("repo") or {}).get("url"):
            by_repo.setdefault(s["repo"]["url"], []).append(n)
    for url, names in sorted(by_repo.items()):
        if len(names) > 1:
            out.print(f"[yellow]![/] [bold]{', '.join(sorted(names))}[/] share "
                      f"[link {url}]{url}[/]. If that's wrong, point each at its own repo with "
                      "[cyan]snap-maintainer set-repo <snap> <url>[/].")
    problems = sorted(n for n, s in snaps.items() if s.get("status") not in ("ok", "unregistered"))
    if problems:
        out.print(f"[red]●[/] {len(problems)} snap(s) need a repo fixed: [bold]{', '.join(problems)}[/]. "
                  "Use [cyan]snap-maintainer set-repo <snap> <url>[/].")


# ---------------------------------------------------------------- check report

def _targets_text(targets: list[dict]) -> Text:
    t = Text()
    for i, tg in enumerate(targets):
        if i:
            t.append("\n")
        t.append(f"{tg['risk']} ", style="grey50").append(f"r{tg['revision']}", style="bold")
        t.append(f" {tg['arch']}", style="grey50")
    return t if targets else Text("—", style="grey50")


def _method_text(targets: list[dict]) -> Text:
    methods = {t.get("security_method") for t in targets} - {None}
    if not methods:
        return Text("—", style="grey50")
    if methods == {"manifest"}:
        return Text("✓ exact", style="green")
    if "manifest" in methods:
        return Text("partly exact", style="yellow")
    if methods == {"none"}:
        return Text("nothing staged", style="grey50")
    return Text("~ inferred", style="grey50")


def _count(reasons: list[dict], kind: str, style: str) -> Text:
    n = sum(1 for r in reasons if r["kind"] == kind)
    return Text(str(n), style=style) if n else Text("·", style="grey50")


def _security_count(reasons: list[dict]) -> Text:
    sec = [r for r in reasons if r["kind"] == "security"]
    if not sec:
        return Text("·", style="grey50")
    order = list(SEVERITY_STYLE)
    sevs = [(r.get("severity") or "").lower() for r in sec]
    worst = min(sevs, key=lambda s: order.index(s) if s in order else len(order))
    t = Text(str(len(sec)), style="bold")
    t.append(f" {worst or '?'}", style=SEVERITY_STYLE.get(worst, "white"))
    return t


def summary_line(snaps: dict[str, dict]) -> Text:
    t = Text()
    for v in VERDICT_ORDER:
        n = sum(1 for s in snaps.values() if s["update_check"]["verdict"] == v)
        if n:
            _, label, color = VERDICT_STYLE[v]
            t.append("● ", style=color).append(f"{n} ", style="bold").append(f"{label.lower()}   ")
    return t


def check_report(snaps: dict[str, dict], verbose: bool = False) -> None:
    order = sorted(snaps, key=lambda n: (VERDICT_ORDER.index(snaps[n]["update_check"]["verdict"]), n))

    table = Table(box=box.SIMPLE_HEAD, header_style="bold grey70", pad_edge=False,
                  title=Text.assemble(("Update check  ", "bold"), summary_line(snaps)),
                  title_justify="left")
    table.add_column("Snap", style="bold", no_wrap=True)
    table.add_column("Verdict", no_wrap=True)
    table.add_column("Published")
    table.add_column("Security", justify="right", no_wrap=True)
    table.add_column("Source", justify="right")
    table.add_column("Upstream", justify="right")
    table.add_column("Packages", no_wrap=True)
    for name in order:
        res = snaps[name]["update_check"]
        r = res["reasons"]
        table.add_row(name, badge(res["verdict"]), _targets_text(res["targets"]),
                      _security_count(r), _count(r, "source", "yellow"),
                      _count(r, "upstream", "yellow"), _method_text(res["targets"]))
    out.print(table)

    for name in order:
        res = snaps[name]["update_check"]
        panel = _detail_panel(name, res, verbose)
        if panel:
            out.print(panel)
    if not verbose:
        out.print("[grey50]Run with [cyan]-v[/] for every notice, the commit messages and all notes.")


def _detail_panel(name: str, res: dict, verbose: bool) -> Panel | None:
    reasons, notes = res["reasons"], res["notes"]
    if not verbose:
        notes = [n for n in notes if n.startswith("check failed")
                 or res["verdict"] == checks.VERDICT_UNKNOWN]
    if not reasons and not notes:
        return None
    parts = []

    sec = [r for r in reasons if r["kind"] == "security"]
    if sec:
        limit = len(sec) if verbose else 4
        # fixed widths so the columns line up from one box to the next
        t = Table(box=None, pad_edge=False, show_header=True, header_style="grey50", expand=False)
        t.add_column("Notice", width=11, no_wrap=True)
        t.add_column("Severity", width=8, no_wrap=True)
        t.add_column("Package", width=16, style="bold", overflow="ellipsis", no_wrap=True)
        t.add_column("Fix", width=40, overflow="fold")
        t.add_column("Issued", width=10, style="grey50", no_wrap=True)
        for r in sec[:limit]:
            usn_id = r["usn"].removeprefix("USN-")
            sev = r.get("severity") or "?"
            fix = Text()
            if r.get("staged_version"):
                fix.append(r["staged_version"], style="red").append(" → ", style="grey50")
            fix.append(r["fixed_version"], style="green")
            t.add_row(Text(r["usn"], style=f"link https://ubuntu.com/security/notices/USN-{usn_id}"),
                      Text(sev, style=SEVERITY_STYLE.get(sev.lower(), "white")),
                      r["source"], fix, r["issued"][:10])
        parts.append(t)
        if len(sec) > limit:
            parts.append(Text(f"  + {len(sec) - limit} more security notice(s)", style="grey50"))

    # label column + text column, so long lines wrap under the text, not the label
    grid = Table.grid(padding=(0, 2))
    grid.add_column(width=10, no_wrap=True)
    grid.add_column(overflow="fold")
    limit = 10 if verbose else 3
    for r in reasons:
        if r["kind"] == "security":
            continue
        icon = "✎" if r["kind"] == "source" else "⬆"
        grid.add_row(Text(f"{icon} {r['kind']}", style="yellow"), r["detail"])
        for cm in (r.get("commits") or [])[:limit]:
            msg = cm["message"] if len(cm["message"]) <= 72 else cm["message"][:71] + "…"
            grid.add_row("", Text.assemble((cm["sha"][:7], "cyan"), "  ", msg))
        extra = len(r.get("commits") or []) - limit
        if extra > 0:
            grid.add_row("", Text(f"+ {extra} more commit(s)", style="grey50"))
    for n in notes:
        grid.add_row(Text("ℹ note", style="blue"), Text(n, style="grey50"))
    if grid.row_count:
        if parts:
            parts.append(Text(""))
        parts.append(grid)

    _, label, color = VERDICT_STYLE.get(res["verdict"], ("", res["verdict"].upper(), "white"))
    title = Text.assemble((name, "bold"), "  ", ("● ", color), (label.lower(), color))
    return Panel(Group(*parts), title=title, title_align="left", border_style="grey35",
                 box=box.ROUNDED, padding=(0, 1), width=min(out.width, PANEL_WIDTH))


# ---------------------------------------------------------------- messages

def error(msg: str, hint: str | None = None) -> None:
    err.print(Text.assemble(("✗ ", "bold red"), (msg, "red")))
    if hint:
        err.print(Text.assemble(("  ", ""), (hint, "grey50")))


def success(msg: str) -> None:
    out.print(Text.assemble(("✓ ", "bold green"), msg))
