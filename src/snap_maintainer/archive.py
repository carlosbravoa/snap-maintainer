"""Ubuntu archive metadata: which binary packages a snap stages, and their source packages.

snapcraft doesn't record staged package versions in the snap any more, so we
reconstruct *which* packages are in it: the stage-packages plus their
Depends/Pre-Depends closure, minus what the base snap already ships (craft-parts
leaves those out too).
"""

from __future__ import annotations

import lzma
import re
from pathlib import Path

from snap_maintainer import debver
from snap_maintainer.cache import cached_json, fetch

COMPONENTS = ("main", "universe", "restricted", "multiverse")
POCKETS = ("", "-updates", "-security")


def _mirror(arch: str) -> str:
    if arch in ("amd64", "i386"):
        return "http://archive.ubuntu.com/ubuntu"
    return "http://ports.ubuntu.com/ubuntu-ports"


def _parse_relations(field: str) -> list[list[str]]:
    """'a (>= 1) | b, c:any' -> [['a', 'b'], ['c']]"""
    groups = []
    for group in field.split(","):
        alts = [re.split(r"[\s(:]", alt.strip(), maxsplit=1)[0] for alt in group.split("|")]
        alts = [a for a in alts if a]
        if alts:
            groups.append(alts)
    return groups


def parse_packages(text: str, index: dict) -> None:
    """Merge a Packages file into ``index`` =
    {pkg: {"source": str, "version": source version, "deps": [[alts]]},
     "__provides__": {virtual: [pkgs]}}, keeping the newest version of each package."""
    provides = index.setdefault("__provides__", {})
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        key = None
        for line in stanza.splitlines():
            if line.startswith((" ", "\t")) and key:
                fields[key] += " " + line.strip()
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        name = fields.get("Package")
        if not name:
            continue
        version = fields.get("Version", "")
        old = index.get(name)
        if old and debver.compare(old["bin_version"], version) >= 0:
            continue
        # "Source: foo (1.2-3)" when the source version differs from the binary's
        src_field = fields.get("Source", name)
        source = src_field.split(" ")[0]
        m = re.search(r"\(([^)]+)\)", src_field)
        deps = _parse_relations(fields.get("Pre-Depends", "")) + _parse_relations(fields.get("Depends", ""))
        index[name] = {"source": source, "version": m.group(1) if m else version,
                       "bin_version": version, "deps": deps}
        for virt in _parse_relations(fields.get("Provides", "")):
            lst = provides.setdefault(virt[0], [])
            if name not in lst:
                lst.append(name)


def package_index(series: str, arch: str, refresh: bool = False) -> dict:
    def build() -> dict:
        index: dict = {}
        for pocket in POCKETS:
            for comp in COMPONENTS:
                url = f"{_mirror(arch)}/dists/{series}{pocket}/{comp}/binary-{arch}/Packages.xz"
                try:
                    data = lzma.decompress(fetch(url))
                except Exception:
                    continue  # e.g. an empty pocket/component
                parse_packages(data.decode("utf-8", "replace"), index)
        if len(index) < 100:
            raise RuntimeError(f"could not download the {series}/{arch} package index")
        return index
    return cached_json(f"packages-{series}-{arch}", build, refresh=refresh)


def base_packages(base: str) -> set[str] | None:
    """Packages shipped in the base snap, from its dpkg.list (None if the base isn't installed)."""
    path = Path(f"/snap/{base}/current/usr/share/snappy/dpkg.list")
    if not path.exists():
        return None
    pkgs = set()
    for line in path.read_text().splitlines():
        if line.startswith("ii "):
            pkgs.add(line.split()[1].split(":")[0])
    return pkgs


def closure(roots: list[str], index: dict, exclude: set[str]) -> tuple[set[str], list[str]]:
    """Binary packages pulled in by ``roots``; returns (packages, names not found)."""
    provides = index.get("__provides__", {})
    seen: set[str] = set()
    missing: list[str] = []
    todo = list(roots)
    explicit = set(roots)
    while todo:
        pkg = todo.pop()
        if pkg in seen:
            continue
        if pkg not in index:
            real = provides.get(pkg)
            if not real:
                missing.append(pkg)
                continue
            pkg = real[0]
            if pkg in seen:
                continue
        if pkg in exclude and pkg not in explicit:
            continue
        seen.add(pkg)
        for alts in index[pkg]["deps"]:
            # already satisfied by the base or by something we're staging
            if any(a in exclude or a in seen for a in alts):
                continue
            todo.append(next((a for a in alts if a in index), alts[0]))
    return seen, missing


def sources_of(packages: set[str], index: dict) -> dict[str, dict]:
    """{source package: {"binaries": [...], "version": newest source version in the archive}}"""
    out: dict[str, dict] = {}
    for pkg in packages:
        entry = out.setdefault(index[pkg]["source"], {"binaries": [], "version": None})
        entry["binaries"].append(pkg)
        v = index[pkg]["version"]
        if entry["version"] is None or debver.compare(v, entry["version"]) > 0:
            entry["version"] = v
    for entry in out.values():
        entry["binaries"].sort()
    return out
