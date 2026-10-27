import json
import types

from snap_maintainer import archive, checks, debver, snapcraft_yaml, usn
from snap_maintainer.checks import newer_tags, reference_targets
from snap_maintainer.store import parse_revisions_table


def test_debver_compare():
    assert debver.compare("1.0", "1.0") == 0
    assert debver.compare("1.0~rc1", "1.0") < 0
    assert debver.compare("2:1.0", "1:9.9") > 0
    assert debver.compare("2.3.4-1ubuntu0.1~esm1", "2.3.4-1ubuntu0.1") < 0
    assert debver.compare("2.3.4-1build3", "2.3.4-1ubuntu0.1~esm1") < 0
    assert debver.compare("1.24.0-2ubuntu7.19", "1.24.0-2ubuntu7.9") > 0
    assert debver.compare("1.0a", "1.0+") < 0


PACKAGES = """\
Package: nginx
Version: 1.24.0-2ubuntu7
Depends: nginx-common (= 1.24.0-2ubuntu7), libc6 (>= 2.34), libssl3t64 (>= 3.0.0) | libssl-alt

Package: nginx-common
Source: nginx
Version: 1.24.0-2ubuntu7
Depends: debconf (>= 0.5) | debconf-2.0

Package: libssl3t64
Source: openssl (3.0.13-0ubuntu3)
Version: 3.0.13-0ubuntu3.1

Package: nginx
Version: 1.24.0-2ubuntu7.19
Depends: nginx-common (= 1.24.0-2ubuntu7), libc6 (>= 2.34), libssl3t64 (>= 3.0.0)

Package: nginx
Version: 1.24.0-2ubuntu7.5
Depends: something-old
"""


def test_closure_and_sources():
    index: dict = {}
    archive.parse_packages(PACKAGES, index)
    assert index["nginx"]["bin_version"] == "1.24.0-2ubuntu7.19"  # newest wins, in any order
    pkgs, missing = archive.closure(["nginx"], index, exclude={"libc6", "debconf"})
    assert pkgs == {"nginx", "nginx-common", "libssl3t64"}
    assert missing == []
    srcs = archive.sources_of(pkgs, index)
    assert srcs["openssl"]["version"] == "3.0.13-0ubuntu3"
    assert srcs["nginx"] == {"binaries": ["nginx", "nginx-common"], "version": "1.24.0-2ubuntu7.19"}


YAML = """
name: demo
base: core24
version: '1.2'
parts:
  app:
    plugin: nil
    source: .
    stage-packages:
      - libfoo1:amd64
      - on amd64: [only-amd64]
      - else: [not-amd64]
  lib:
    plugin: cmake
    source: https://github.com/example/lib.git
    source-tag: v1.2.0
  tarball:
    plugin: make
    source: https://example.com/x-1.0.tar.gz
"""


def test_snapcraft_yaml():
    sy = snapcraft_yaml.parse(YAML)
    assert sy.series == "noble"
    assert sy.stage_packages("amd64") == ["libfoo1", "only-amd64"]
    assert sy.stage_packages("arm64") == ["libfoo1", "not-amd64"]
    srcs = {s.part: s for s in sy.remote_sources()}
    assert srcs["lib"].type == "git" and srcs["lib"].tag == "v1.2.0"
    assert srcs["tarball"].type == "tarball"
    assert "app" not in srcs


def test_newer_tags():
    tags = ["v1.1.0", "v1.2.0", "v1.3.0", "v1.10.0", "v2.0.0-rc1", "1.9.9", "nightly"]
    assert newer_tags("v1.2.0", tags) == ["v1.10.0", "v1.3.0"]


OVAL = b"""<oval_definitions xmlns="http://oval.mitre.org/XMLSchema/oval-definitions-5">
<definitions>
<definition id="d1" class="patch"><metadata><title>USN-1-1 -- nginx vulnerabilities</title>
<advisory><severity>Medium</severity><issued date="2026-10-26T15:34:05Z" /></advisory></metadata>
<criteria><criterion test_ref="t1" comment="nginx source package in noble, is affected and has been fixed (note: '1.24.0-2ubuntu7.19')." /></criteria>
</definition>
<definition id="d2" class="patch"><metadata><title>LSN-0104-1 -- Kernel Live Patch</title>
<advisory><issued date="2024-06-10T07:15:31Z" /></advisory></metadata></definition>
</definitions></oval_definitions>"""


def test_parse_oval():
    assert usn.parse_oval(OVAL) == [{
        "usn": "USN-1-1", "title": "nginx vulnerabilities", "severity": "Medium",
        "issued": "2026-10-26T15:34:05Z", "sources": {"nginx": "1.24.0-2ubuntu7.19"},
    }]


def test_reference_targets_prefers_most_stable_per_arch():
    revs = parse_revisions_table(
        "Rev.    Uploaded              Arches    Version    Channels\n"
        "29      2026-10-16T12:30:34Z  arm64     0.9.1      latest/stable*\n"
        "28      2026-10-16T12:29:46Z  amd64     0.9.1      latest/stable*\n"
        "30      2026-10-20T00:00:00Z  amd64     1.0        latest/edge*\n"
    )
    channels = [
        {"track": "latest", "risk": "edge", "arch": "amd64", "revision": 30, "version": "1.0"},
        {"track": "latest", "risk": "stable", "arch": "amd64", "revision": 28, "version": "0.9.1"},
        {"track": "latest", "risk": "stable", "arch": "arm64", "revision": 29, "version": "0.9.1"},
    ]
    t = reference_targets(channels, revs)
    assert [(x["arch"], x["revision"], x["uploaded"]) for x in t] == [
        ("amd64", 28, "2026-10-16T12:29:46Z"), ("arm64", 29, "2026-10-16T12:30:34Z")]


def test_manifest_parse_staged():
    from snap_maintainer.manifests import parse_staged
    m = {"primed-stage-packages": ["git=1:2.43.0-1ubuntu7.3", "libfoo:amd64=1.0", "junk"]}
    assert parse_staged(m) == {"git": "1:2.43.0-1ubuntu7.3", "libfoo": "1.0"}


NOTICES = [
    {"usn": "USN-2-1", "title": "old", "severity": "Low", "issued": "2026-01-01T00:00:00Z",
     "sources": {"nginx": "1.24.0-2ubuntu7.5"}},
    {"usn": "USN-3-1", "title": "new", "severity": "Medium", "issued": "2026-10-26T00:00:00Z",
     "sources": {"nginx": "1.24.0-2ubuntu7.19"}},
    {"usn": "USN-4-1", "title": "esm", "severity": "Medium", "issued": "2026-10-26T00:00:00Z",
     "sources": {"openssl": "3.0.13-0ubuntu3.9~esm1"}},
]


def _index():
    index: dict = {}
    archive.parse_packages(PACKAGES, index)
    return index


def test_match_notices_exact_uses_staged_versions_not_dates():
    from snap_maintainer.checks import match_notices, staged_sources
    # staged before the old fix: both USNs apply, regardless of upload date
    srcs = staged_sources({"nginx": "1.24.0-2ubuntu7", "libssl3t64": "3.0.13-0ubuntu3"}, _index())
    assert srcs["nginx"]["staged"] == "1.24.0-2ubuntu7"
    notes: list = []
    hits = match_notices(NOTICES, srcs, None, "noble", notes)
    assert [h[0]["usn"] for h in hits] == ["USN-2-1", "USN-3-1"]
    assert any("USN-4-1" in n for n in notes)  # ESM-only fix becomes a note
    # staged with the newest fix: nothing applies
    srcs = staged_sources({"nginx": "1.24.0-2ubuntu7.19"}, _index())
    assert match_notices(NOTICES, srcs, None, "noble", []) == []


def test_match_notices_inferred_uses_upload_date():
    from datetime import datetime, timezone
    from snap_maintainer.checks import match_notices
    srcs = archive.sources_of({"nginx"}, _index())
    uploaded = datetime(2026, 10, 13, tzinfo=timezone.utc)
    assert [h[0]["usn"] for h in match_notices(NOTICES, srcs, uploaded, "noble", [])] == ["USN-3-1"]


def _commits(*dates):
    return json.dumps([{"sha": f"{i:040x}", "commit": {"committer": {"date": d}, "message": "m"}}
                       for i, d in enumerate(dates)])


def _source(monkeypatch, out):
    monkeypatch.setattr(checks.repo_mod, "run_gh", lambda args: (0, out))
    repo = {"owner": "o", "name": "n", "default_branch": "main"}
    sy = types.SimpleNamespace(version=None)
    targets = [{"risk": "stable", "revision": 5, "uploaded": "2026-10-27T20:04:26Z"}]
    return checks.source_reasons(repo, sy, targets)


def test_source_commit_soon_after_upload_warns(monkeypatch):
    [r] = _source(monkeypatch, _commits("2026-10-27T20:46:29Z"))
    assert "2026-10-27 20:04 UTC" in r["warning"] and "2026-10-27 20:46 UTC" in r["warning"]


def test_source_commit_long_after_upload_no_warning(monkeypatch):
    [r] = _source(monkeypatch, _commits("2026-11-02T09:00:00Z", "2026-10-27T20:46:29Z"))
    assert "warning" not in r
