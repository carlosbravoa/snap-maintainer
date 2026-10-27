"""Discover and maintain published snaps."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("snap-maintainer")
except PackageNotFoundError:  # running from a checkout without an install
    __version__ = "unknown"
