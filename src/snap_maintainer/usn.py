"""Ubuntu Security Notices for a release, from Canonical's per-release OVAL feed."""

from __future__ import annotations

import bz2
import io
import re
import xml.etree.ElementTree as ET

from snap_maintainer.cache import cached_json, fetch

OVAL_URL = "https://security-metadata.canonical.com/oval/com.ubuntu.{series}.usn.oval.xml.bz2"

_FIXED = re.compile(r"^(\S+) source package in \S+, is affected and has been fixed \(note: '([^']+)'\)")


def parse_oval(data: bytes) -> list[dict]:
    """[{usn, title, severity, issued, sources: {source: fixed_version}}]"""
    notices = []
    for _, el in ET.iterparse(io.BytesIO(data)):
        if not el.tag.endswith("}definition"):
            continue
        meta = {c.tag.split("}")[1]: c for c in el.iter()}
        title = meta.get("title")
        issued = meta.get("issued")
        if title is None or issued is None or not title.text.startswith("USN-"):
            el.clear()
            continue
        sources = {}
        for crit in el.iter():
            if crit.tag.endswith("}criterion"):
                m = _FIXED.match(crit.get("comment", ""))
                if m:
                    sources[m.group(1)] = m.group(2)
        if sources:
            usn, _, desc = title.text.partition(" -- ")
            sev = meta.get("severity")
            notices.append({
                "usn": usn,
                "title": desc,
                "severity": sev.text if sev is not None else None,
                "issued": issued.get("date"),
                "sources": sources,
            })
        el.clear()
    return notices


def notices(series: str, refresh: bool = False) -> list[dict]:
    return cached_json(
        f"usn-{series}",
        lambda: parse_oval(bz2.decompress(fetch(OVAL_URL.format(series=series)))),
        refresh=refresh,
    )
