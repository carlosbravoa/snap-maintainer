"""Build manifests (``snap/manifest.yaml``) of published revisions.

snapcraft embeds a manifest only when the build ran with SNAPCRAFT_BUILD_INFO=1
(Launchpad and snapcore/action-build do this by default). When present, its
``primed-stage-packages`` gives the exact version of every staged deb, which
makes the security check exact instead of inferred from upload dates.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from snap_maintainer.cache import cache_dir, cached_json, fetch

# A revision's contents never change, so its manifest can be cached forever.
FOREVER = 100 * 365 * 24 * 3600


class ManifestUnavailable(RuntimeError):
    pass


def parse_staged(manifest: dict) -> dict[str, str]:
    """{binary package: version} from a manifest's primed-stage-packages."""
    out = {}
    for item in manifest.get("primed-stage-packages") or []:
        name, sep, version = str(item).partition("=")
        if sep:
            out[name.split(":")[0]] = version
    return out


def _extract(snap_file: Path, workdir: Path) -> dict | None:
    if not shutil.which("unsquashfs"):
        raise ManifestUnavailable("unsquashfs not installed (squashfs-tools)")
    dest = workdir / "root"
    proc = subprocess.run(
        ["unsquashfs", "-q", "-n", "-d", str(dest), str(snap_file), "snap/manifest.yaml"],
        capture_output=True, text=True,
    )
    path = dest / "snap" / "manifest.yaml"
    if not path.exists():
        if proc.returncode not in (0, 1):  # 1 just means some requested paths didn't exist
            raise ManifestUnavailable(f"unsquashfs failed: {proc.stderr.strip()[:200]}")
        return None
    return yaml.safe_load(path.read_text()) or {}


def staged_packages(name: str, revision: int, download_url: str | None) -> dict | None:
    """Staged packages of a published revision, or None if it was built without a manifest.

    Raises ManifestUnavailable when the snap can't be fetched or read (nothing is cached then).
    """
    def build() -> dict:
        if not download_url:
            raise ManifestUnavailable("no public download (private snap?)")
        with tempfile.TemporaryDirectory(dir=cache_dir()) as tmp:
            snap_file = Path(tmp) / f"{name}_{revision}.snap"
            try:
                snap_file.write_bytes(fetch(download_url, timeout=600))
            except Exception as e:
                raise ManifestUnavailable(f"download failed: {e}") from e
            manifest = _extract(snap_file, Path(tmp))
        if manifest is None:
            return {"has_manifest": False}
        return {
            "has_manifest": True,
            "snapcraft_version": manifest.get("snapcraft-version"),
            "built_at": manifest.get("snapcraft-started-at"),
            "packages": parse_staged(manifest),
        }

    safe = re.sub(r"[^\w.-]", "_", name)
    result = cached_json(f"manifest-{safe}-{revision}", build, ttl=FOREVER)
    return result if result.get("has_manifest") else None
