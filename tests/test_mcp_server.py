import json

import pytest

pytest.importorskip("mcp")

from snap_maintainer import checks, mcp_server, preflight  # noqa: E402
from snap_maintainer.preflight import Check  # noqa: E402

ENTRY = {
    "name": "demo", "visibility": "public", "status": "ok",
    "channels": [{"track": "latest", "risk": "stable", "arch": "amd64", "version": "1.0",
                  "revision": 3}],
    "repo": {"url": "https://github.com/o/demo", "owner": "o", "name": "demo",
             "verified": True, "source": "store-links"},
    "build": {"method": "local", "evidence": "no workflow"},
    "update_check": {
        "verdict": checks.VERDICT_REBUILD, "checked_at": "2026-10-27T00:00:00+00:00",
        "notes": [],
        "targets": [{"track": "latest", "risk": "stable", "arch": "amd64", "revision": 3,
                     "version": "1.0"}],
        "reasons": [{"kind": "security", "usn": "USN-1-1", "source": "nginx",
                     "severity": "Medium", "issued": "2026-10-26T00:00:00Z",
                     "fixed_version": "1.2", "arches": ["amd64"]}],
    },
}


@pytest.fixture
def inventory(tmp_path, monkeypatch):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"snaps": {"demo": ENTRY}}))
    monkeypatch.setattr(mcp_server, "INVENTORY", path)
    return path


def test_get_inventory(inventory):
    out = mcp_server.get_inventory()
    assert out["inventory"] == str(inventory)
    [s] = out["snaps"]
    assert s["snap"] == "demo" and s["verdict"] == "rebuild" and s["builder"] == "local"
    assert s["published"] == ["latest/stable 1.0 r3 amd64"]


def test_get_inventory_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "INVENTORY", tmp_path / "none.json")
    assert "discover_snaps" in mcp_server.get_inventory()["hint"]


def test_rebuild_plan_formats(inventory):
    md = mcp_server.rebuild_plan()
    assert md.startswith("# Snap rebuild plan") and "## demo (rebuild)" in md
    [plan] = mcp_server.rebuild_plan(format="json")
    assert plan["snap"] == "demo" and plan["steps"]


def test_unknown_snap_is_a_tool_error(inventory):
    with pytest.raises(mcp_server.ToolError, match="Known snaps: demo"):
        mcp_server.rebuild_plan(snaps=["nope"])


def test_missing_login_is_a_tool_error(monkeypatch):
    monkeypatch.setattr(preflight, "run", lambda command: [
        Check("snapcraft", False, True, "not logged in", fix="snapcraft login")])
    with pytest.raises(mcp_server.ToolError, match="fix: snapcraft login"):
        mcp_server._require("check")
