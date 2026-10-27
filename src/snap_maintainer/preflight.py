"""Check the external tools and logins a command needs before doing any work."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str  # who you're logged in as, or what's wrong
    fix: str | None = None  # command or hint shown when not ok
    impact: str | None = None  # what degrades when an optional tool is missing


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                              timeout=timeout)
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _first_line(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip() and not l.startswith("Full execution log")]
    return lines[0] if lines else text


def check_snapcraft() -> Check:
    if not shutil.which("snapcraft"):
        return Check("snapcraft", False, True, "not installed",
                     fix="sudo snap install snapcraft --classic")
    code, out = _run(["snapcraft", "whoami"])
    if code != 0:
        via_env = " (from SNAPCRAFT_STORE_CREDENTIALS)" if os.environ.get(
            "SNAPCRAFT_STORE_CREDENTIALS") else ""
        return Check("snapcraft", False, True,
                     f"not logged in to the Snap Store{via_env}: {_first_line(out)}",
                     fix="snapcraft login")
    user = re.search(r"^username:\s*(\S+)", out, re.M)
    expires = re.search(r"^expires:\s*(\S+)", out, re.M)
    detail = user.group(1) if user else "logged in"
    if expires:
        detail += f" · login expires {expires.group(1)[:10]}"
    return Check("snapcraft", True, True, detail)


def check_gh() -> Check:
    if not shutil.which("gh"):
        return Check("gh", False, True, "GitHub CLI not installed",
                     fix="sudo snap install gh   (or: sudo apt install gh)")
    code, out = _run(["gh", "api", "user", "--jq", ".login"])
    if code == 0 and out:
        return Check("gh", True, True, out.splitlines()[-1])
    if "gh auth login" in out or "Bad credentials" in out:
        via_env = " (GH_TOKEN/GITHUB_TOKEN is set; check it)" if (
            os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")) else ""
        return Check("gh", False, True, f"not logged in to GitHub{via_env}", fix="gh auth login")
    return Check("gh", False, True, f"can't reach GitHub: {_first_line(out)}",
                 fix="check your network connection, then run: gh auth status")


def check_unsquashfs() -> Check:
    if shutil.which("unsquashfs"):
        return Check("unsquashfs", True, False, "available")
    return Check("unsquashfs", False, False, "not installed",
                 fix="sudo apt install squashfs-tools",
                 impact="build manifests can't be read; security checks fall back to inference")


def check_git() -> Check:
    if shutil.which("git"):
        return Check("git", True, False, "available")
    return Check("git", False, False, "not installed", fix="sudo apt install git",
                 impact="parts pinned to a source-tag can't be compared with upstream tags")


def run(command: str) -> list[Check]:
    """Checks needed by ``command`` (discover, check, set-repo)."""
    checks = [check_snapcraft(), check_gh()]
    if command in ("set-repo", "set-build"):
        checks = [check_gh()]
    if command == "check":
        checks += [check_unsquashfs(), check_git()]
    return checks
