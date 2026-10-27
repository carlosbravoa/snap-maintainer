"""Debian version comparison (the dpkg algorithm), so we don't depend on python3-apt."""

from __future__ import annotations

import re
from functools import cmp_to_key


def _order(c: str) -> int:
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _cmp_part(a: str, b: str) -> int:
    while a or b:
        # non-digit prefix, compared char by char with dpkg's ordering
        na = re.match(r"^\D*", a).group(0)
        nb = re.match(r"^\D*", b).group(0)
        for i in range(max(len(na), len(nb))):
            oa = _order(na[i]) if i < len(na) else 0
            ob = _order(nb[i]) if i < len(nb) else 0
            if oa != ob:
                return -1 if oa < ob else 1
        a, b = a[len(na):], b[len(nb):]
        da = re.match(r"^\d*", a).group(0)
        db = re.match(r"^\d*", b).group(0)
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
        a, b = a[len(da):], b[len(db):]
    return 0


def _split(v: str) -> tuple[int, str, str]:
    epoch, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
    return int(epoch or 0), upstream, revision


def compare(a: str, b: str) -> int:
    ea, ua, ra = _split(a)
    eb, ub, rb = _split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


sort_key = cmp_to_key(compare)
