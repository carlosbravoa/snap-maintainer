"""Snap Store access: the publisher's registered names and per-snap channel maps.

Uses the ``snapcraft`` CLI for anything authenticated (so the user's existing
store login is reused) and the public store API for richer metadata.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

STORE_INFO_URL = "https://api.snapcraft.io/v2/snaps/info/{name}?fields=links,website,contact,title,summary,version,revision,base,download"


class StoreError(RuntimeError):
    pass


@dataclass
class Channel:
    track: str
    risk: str
    arch: str
    version: str | None = None
    revision: int | None = None
    base: str | None = None
    released_at: str | None = None
    download_url: str | None = None  # public snaps only


@dataclass
class StoreInfo:
    title: str | None = None
    summary: str | None = None
    links: dict[str, list[str]] = field(default_factory=dict)
    channels: list[Channel] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _run(cmd: list[str], timeout: int = 120) -> str:
    # stdin is closed so a logged-out snapcraft fails instead of prompting for an email
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False,
                              stdin=subprocess.DEVNULL, timeout=timeout)
    except FileNotFoundError as e:
        raise StoreError(f"{cmd[0]} not found; is it installed?") from e
    except subprocess.TimeoutExpired as e:
        raise StoreError(f"{' '.join(cmd)} timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise StoreError(f"{' '.join(cmd)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout


def registered_snaps() -> list[dict]:
    """Return the snaps registered to the logged-in publisher (name, since, visibility, notes)."""
    data = json.loads(_run(["snapcraft", "names", "--format", "json"]))
    return data.get("snaps", [])


def public_info(name: str, retries: int = 3) -> StoreInfo | None:
    """Fetch metadata from the public store API, or None if the snap isn't publicly visible.

    The API occasionally returns an empty body, so retry a few times.
    """
    req = urllib.request.Request(
        STORE_INFO_URL.format(name=name), headers={"Snap-Device-Series": "16"}
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
            data = json.loads(body)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            err = e
        time.sleep(1 + attempt)
    else:
        raise StoreError(f"store info for {name} unavailable: {err}")

    snap = data.get("snap", {})
    channels = []
    for entry in data.get("channel-map", []):
        ch = entry["channel"]
        channels.append(
            Channel(
                track=ch["track"],
                risk=ch["risk"],
                arch=ch["architecture"],
                version=entry.get("version"),
                revision=entry.get("revision"),
                base=entry.get("base"),
                released_at=ch.get("released-at"),
                download_url=(entry.get("download") or {}).get("url"),
            )
        )
    links = dict(snap.get("links") or {})
    if snap.get("website"):
        links.setdefault("website", [snap["website"]])
    return StoreInfo(
        title=snap.get("title"), summary=snap.get("summary"), links=links, channels=channels
    )


def parse_status_table(text: str) -> list[Channel]:
    """Parse ``snapcraft status`` table output into channels.

    Columns are positional; blank Track/Arch cells inherit from the row above,
    and ``↑``/``-`` mean "follows the channel above" / "closed or empty". Only
    channels with an actual revision are returned.
    """
    channels: list[Channel] = []
    track = arch = None
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines or not lines[0].lstrip().startswith("Track"):
        return channels
    header = lines[0]
    cols = ["Track", "Arch", "Channel", "Version", "Revision"]
    starts = [header.index(c) for c in cols]

    def cell(line: str, i: int) -> str:
        end = starts[i + 1] if i + 1 < len(starts) else None
        return line[starts[i]:end].strip() if len(line) > starts[i] else ""

    last_version = last_revision = None
    for line in lines[1:]:
        track = cell(line, 0) or track
        new_arch = cell(line, 1)
        if new_arch:
            arch, last_version, last_revision = new_arch, None, None
        risk = cell(line, 2)
        version = cell(line, 3)
        revision = cell(line, 4).split()[0] if cell(line, 4) else ""
        if version == "↑":
            version, revision = last_version, last_revision
        elif version in ("-", ""):
            version = revision = None
        last_version, last_revision = version, revision
        if revision is None or not risk:
            continue
        channels.append(
            Channel(
                track=track, risk=risk, arch=arch, version=version,
                revision=int(revision) if str(revision).isdigit() else None,
            )
        )
    return channels


def parse_revisions_table(text: str) -> dict[int, dict]:
    """Parse ``snapcraft revisions`` output: {revision: {uploaded, arches, version}}."""
    out = {}
    for line in text.splitlines()[1:]:
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) < 4 or not cols[0].isdigit():
            continue
        out[int(cols[0])] = {
            "uploaded": cols[1],
            "arches": [a.strip() for a in cols[2].split(",")],
            "version": cols[3],
        }
    return out


def revisions(name: str) -> dict[int, dict]:
    return parse_revisions_table(_run(["snapcraft", "revisions", name]))


def private_channels(name: str) -> list[Channel]:
    """Channel map via the authenticated CLI; works for private snaps too (no base info)."""
    return parse_status_table(_run(["snapcraft", "status", name]))
