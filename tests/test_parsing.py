from snap_maintainer.repo import candidates_from_store, normalize, parse_github
from snap_maintainer.store import parse_status_table

STATUS = """\
Track    Arch    Channel    Version    Revision    Progress
latest   amd64   stable     -          -           -
                 candidate  -          -           -
                 beta       1.4.8      13          -
                 edge       ↑          ↑           -
         arm64   stable     1.4.7      12          -
                 candidate  ↑          ↑           -
"""


def test_parse_status_table():
    chans = [(c.arch, c.risk, c.version, c.revision) for c in parse_status_table(STATUS)]
    assert chans == [
        ("amd64", "beta", "1.4.8", 13),
        ("amd64", "edge", "1.4.8", 13),
        ("arm64", "stable", "1.4.7", 12),
        ("arm64", "candidate", "1.4.7", 12),
    ]


def test_parse_status_table_garbage():
    assert parse_status_table("No revisions\n") == []


def test_parse_github():
    assert parse_github("https://github.com/a/b") == ("a", "b")
    assert parse_github("https://github.com/a/b.git") == ("a", "b")
    assert parse_github("https://github.com/a/b/issues") == ("a", "b")
    assert parse_github("git@github.com:a/b.git") == ("a", "b")
    assert parse_github("https://snapcraft.io/publisher/x") is None


def test_candidates_prefer_source_and_skip_non_code_websites():
    links = {
        "website": ["https://example.com", "https://github.com/a/site"],
        "source": ["https://github.com/a/b.git"],
        "issues": ["https://github.com/a/b/issues"],
    }
    assert candidates_from_store(links) == [
        ("https://github.com/a/b", "store-links"),
        ("https://github.com/a/site", "store-website"),
    ]
    assert normalize("https://launchpad.net/foo/") == "https://launchpad.net/foo"
