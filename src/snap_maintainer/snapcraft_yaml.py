"""Read the bits of a snapcraft.yaml that matter for update detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

BASE_SERIES = {
    "core18": "bionic",
    "core20": "focal",
    "core22": "jammy",
    "core24": "noble",
    "core26": "resolute",
}

_GIT_URL = re.compile(r"(\.git/?$|^git[@:]|^git\+|^https?://(github\.com|gitlab\.com|git\.launchpad\.net|codeberg\.org)/)")


@dataclass
class PartSource:
    part: str
    url: str
    type: str  # git | tarball | other
    tag: str | None = None
    commit: str | None = None
    branch: str | None = None


@dataclass
class SnapcraftYaml:
    name: str | None
    version: str | None  # None when set at build time (adopt-info, git, ...)
    base: str | None
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def series(self) -> str | None:
        return BASE_SERIES.get(self.base or "")

    def stage_packages(self, arch: str) -> list[str]:
        """Stage packages for ``arch`` across all parts, with grammar resolved."""
        pkgs: list[str] = []
        for part in (self.raw.get("parts") or {}).values():
            for item in _resolve_grammar((part or {}).get("stage-packages") or [], arch):
                pkgs.append(_strip_pkg(item))
        return sorted(set(p for p in pkgs if p))

    def remote_sources(self) -> list[PartSource]:
        out = []
        for name, part in (self.raw.get("parts") or {}).items():
            src = (part or {}).get("source")
            if not isinstance(src, str) or not re.match(r"^(\w+(\+\w+)?://|git@)", src):
                continue  # local source, or grammar we don't evaluate here
            stype = part.get("source-type")
            if stype == "git" or (not stype and _GIT_URL.search(src)):
                kind = "git"
            elif stype in ("tar", "zip") or re.search(r"\.(tar(\.\w+)?|tgz|zip)$", src):
                kind = "tarball"
            else:
                kind = "other"
            out.append(PartSource(
                part=name, url=src, type=kind,
                tag=_str(part.get("source-tag")),
                commit=_str(part.get("source-commit")),
                branch=_str(part.get("source-branch")),
            ))
        return out


def parse(text: str) -> SnapcraftYaml:
    raw = yaml.safe_load(text) or {}
    version = raw.get("version")
    if version is not None:
        version = str(version)
    return SnapcraftYaml(name=raw.get("name"), version=version,
                         base=raw.get("base") or raw.get("build-base"), raw=raw)


def _str(v) -> str | None:
    return None if v is None else str(v)


def _strip_pkg(item: str) -> str:
    # "libfoo:amd64", "libfoo=1.2" -> "libfoo"
    return re.split(r"[:=]", item.strip(), maxsplit=1)[0]


def _resolve_grammar(items, arch: str) -> list[str]:
    """Flatten snapcraft's advanced grammar (on/to/else/try) for one architecture."""
    out: list[str] = []
    prev_matched = True
    for item in items if isinstance(items, list) else [items]:
        if isinstance(item, str):
            out.append(item)
            continue
        if not isinstance(item, dict):
            continue
        for key, body in item.items():
            key = str(key).strip()
            if key in ("on", "to") or key.startswith(("on ", "to ")):
                # "on amd64", "on amd64 to arm64", "to arm64": the target arch decides
                words = key.split()
                target = words[words.index("to") + 1] if "to" in words else words[-1]
                prev_matched = arch in target.split(",")
                if prev_matched:
                    out.extend(_resolve_grammar(body, arch))
            elif key == "else":
                if not prev_matched:
                    out.extend(_resolve_grammar(body, arch))
                prev_matched = True
            elif key == "try":
                out.extend(_resolve_grammar(body, arch))
    return out
