from snap_maintainer import builders

RELEASE = """
name: Release
on:
  push:
    tags: ["v*"]
jobs:
  snap:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: snapcore/action-build@v1
  snap-store:
    runs-on: ubuntu-24.04
    steps:
      - run: |
          sudo snap install snapcraft --classic
          channel=stable
          snapcraft upload "$f" --release="$channel"
"""

CI_BUILD_ONLY = """
on:
  push:
    branches: [main]
  pull_request:
  workflow_dispatch:
jobs:
  snap:
    runs-on: ubuntu-24.04
    steps:
      - uses: snapcore/action-build@v1
      - uses: actions/upload-artifact@v4
"""

PUBLISH_ACTION = """
on: workflow_dispatch
jobs:
  b:
    steps:
      - run: snapcraft pack
      - uses: snapcore/action-publish@v1
        with:
          release: edge
"""

INSTALL_ONLY = """
on: [push, pull_request]
jobs:
  test:
    steps:
      - run: sudo snap install snapcraft --classic
      - run: make test
"""


def test_tag_triggered_release_workflow():
    wf = builders.analyze_workflow(".github/workflows/release.yml", RELEASE)
    assert wf["builds_with"] == "snapcore/action-build"
    assert wf["uploads"] and wf["uploads_with"] == "snapcraft upload"
    assert wf["channels"] == []  # "$channel" is decided at run time
    assert wf["triggers"]["tags"] == ["v*"] and not wf["triggers"]["dispatch"]
    assert builders.describe_trigger(wf["triggers"]) == "tag v*"


def test_build_only_workflow():
    wf = builders.analyze_workflow("ci.yml", CI_BUILD_ONLY)
    assert wf["uploads"] is False
    assert builders.describe_trigger(wf["triggers"]) == "manual dispatch or push to main"


def test_publish_action_and_string_trigger():
    wf = builders.analyze_workflow("p.yml", PUBLISH_ACTION)
    assert wf["builds_with"] == "snapcraft"
    assert wf["uploads_with"] == "snapcore/action-publish" and wf["channels"] == ["edge"]
    assert wf["triggers"]["dispatch"]


def test_installing_snapcraft_is_not_building():
    assert builders.analyze_workflow("t.yml", INSTALL_ONLY) is None
    assert builders.analyze_workflow("bad.yml", "on: [") is None


def _repo(**kw):
    return {"url": "https://github.com/o/r", "owner": "o", "name": "r", "verified": True,
            "snapcraft_yaml": "snap/snapcraft.yaml", "workflows": [], **kw}


def test_detect_prefers_uploading_workflow_then_launchpad(monkeypatch):
    wfs = [builders.analyze_workflow("ci.yml", CI_BUILD_ONLY),
           builders.analyze_workflow("release.yml", RELEASE)]
    monkeypatch.setattr(builders, "workflow_candidates", lambda repo: wfs)
    lp = [{"name": "r-snap", "git_url": "https://github.com/o/r.git", "store_name": "demo",
           "store_upload": True, "auto_build": True, "url": "https://launchpad.net/~o/+snap/r-snap"},
          {"name": "other", "git_url": "https://github.com/o/other", "store_name": "demo",
           "store_upload": True, "auto_build": False, "url": None}]
    b = builders.detect("demo", _repo(), lp)
    assert b["method"] == "github-workflow" and b["workflow"]["path"] == "release.yml"
    assert len(b["alternatives"]) == 2  # the Launchpad recipe for this repo, and ci.yml
    assert "Launchpad recipe r-snap" in b["alternatives"][0]


def test_detect_launchpad_and_local(monkeypatch):
    monkeypatch.setattr(builders, "workflow_candidates", lambda repo: [])
    lp = [{"name": "r-snap", "git_url": "https://github.com/o/r", "store_name": "demo",
           "store_upload": True, "auto_build": False, "url": None}]
    assert builders.detect("demo", _repo(), lp)["method"] == "launchpad"
    # a recipe for a different snap name doesn't count
    assert builders.detect("other-snap", _repo(), lp)["method"] == "local"
    b = builders.detect("demo", _repo(), None)
    assert b["method"] == "local" and "Launchpad couldn't be reached" in b["notes"][0]
