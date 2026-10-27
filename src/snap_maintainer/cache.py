"""Small on-disk cache for slow-to-fetch, slow-to-parse upstream data."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Callable

DEFAULT_TTL = 24 * 3600


def cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    d = base / "snap-maintainer"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "snap-maintainer"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def cached_json(key: str, build: Callable[[], object], ttl: int = DEFAULT_TTL,
                refresh: bool = False) -> object:
    """Return the cached JSON value for ``key``, rebuilding it when stale."""
    path = cache_dir() / f"{key}.json"
    if not refresh and path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text())
    value = build()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    tmp.replace(path)
    return value
