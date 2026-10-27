"""Resolve and inspect the source repository behind a snap."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field

SNAPCRAFT_YAML_PATHS = (
    "snap/snapcraft.yaml",
    "snapcraft.yaml",
    ".snapcraft.yaml",
    "build-aux/snap/snapcraft.yaml",
)

_GITHUB_RE = re.compile(r"^(?:https?://|git@)github\.com[/:]([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?(?:[#?].*)?$")


@dataclass
class Repo:
    url: str
    source: str  # where the URL came from: user | store-links | store-website | guess | prompt
    host: str | None = None
    owner: str | None = None
    name: str | None = None
    default_branch: str | None = None
    private: bool | None = None
    snapcraft_yaml: str | None = None
    workflows: list[str] = field(default_factory=list)
    verified: bool = False
    redirected_from: str | None = None  # set when GitHub resolved the URL to a renamed/moved repo
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def parse_github(url: str) -> tuple[str, str] | None:
    """Return (owner, repo) for a GitHub URL, ignoring /issues etc. suffixes."""
    url = url.strip()
    m = re.match(r"^(https?://github\.com/[^/\s]+/[^/\s#?]+)", url)
    if m:
        url = m.group(1)
    m = _GITHUB_RE.match(url)
    return (m.group(1), m.group(2)) if m else None


def normalize(url: str) -> str:
    gh = parse_github(url)
    return f"https://github.com/{gh[0]}/{gh[1]}" if gh else url.strip().rstrip("/")


def candidates_from_store(links: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Repo URL candidates from store links, most trustworthy first."""
    out: list[tuple[str, str]] = []
    for key, source in (("source", "store-links"), ("website", "store-website"),
                        ("issues", "store-website"), ("contact", "store-website")):
        for url in links.get(key, []):
            # website/contact links only count when they point at a code host
            if key != "source" and not parse_github(url):
                continue
            norm = normalize(url)
            if norm not in (c[0] for c in out):
                out.append((norm, source))
    return out


def run_gh(args: list[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False,
                              stdin=subprocess.DEVNULL, timeout=60)
    except FileNotFoundError:
        return 127, "gh not installed"
    except subprocess.TimeoutExpired:
        return 124, "gh timed out"
    return proc.returncode, proc.stdout if proc.returncode == 0 else proc.stderr


def gh_login() -> str | None:
    code, out = run_gh(["api", "user", "--jq", ".login"])
    return out.strip() if code == 0 else None


def inspect(repo: Repo) -> Repo:
    """Fill in repo details. Only GitHub is inspected for now; other hosts stay unverified."""
    gh = parse_github(repo.url)
    if not gh:
        repo.host = re.sub(r"^\w+://", "", repo.url).split("/")[0] or None
        repo.error = "non-GitHub host; not inspected"
        return repo
    repo.host, (repo.owner, repo.name) = "github.com", gh
    code, out = run_gh(["api", f"repos/{repo.owner}/{repo.name}"])
    if code != 0:
        repo.error = f"repo not reachable: {out.strip()[:200]}"
        return repo
    meta = json.loads(out)
    # follow renames/transfers
    if meta["html_url"].lower() != f"https://github.com/{repo.owner}/{repo.name}".lower():
        repo.redirected_from = repo.url
    repo.owner, repo.name = meta["owner"]["login"], meta["name"]
    repo.url = meta["html_url"]
    repo.default_branch, repo.private = meta["default_branch"], meta["private"]
    repo.verified = True
    for path in SNAPCRAFT_YAML_PATHS:
        code, _ = run_gh(["api", f"repos/{repo.owner}/{repo.name}/contents/{path}", "--jq", ".path"])
        if code == 0:
            repo.snapcraft_yaml = path
            break
    code, out = run_gh(["api", f"repos/{repo.owner}/{repo.name}/contents/.github/workflows",
                     "--jq", ".[].path"])
    if code == 0:
        repo.workflows = out.split()
    return repo
