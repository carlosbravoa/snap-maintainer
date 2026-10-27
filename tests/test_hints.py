from snap_maintainer import checks, hints

SEC = {"kind": "security", "usn": "USN-1-1", "source": "nginx", "severity": "Medium",
       "issued": "2026-10-26T00:00:00Z", "fixed_version": "1.2", "arches": ["amd64"]}
SRC = {"kind": "source", "detail": "3 commit(s) on main since stable r4 was uploaded on 2026-10-13"}


def _entry(name="demo", verdict=checks.VERDICT_REBUILD, reasons=(SEC,), build=None,
           arches=("amd64",), version="2.2.0"):
    return {
        "name": name,
        "repo": {"url": f"https://github.com/o/{name}", "owner": "o", "name": name,
                 "default_branch": "main"},
        "build": build or {"method": "local", "evidence": "no workflow"},
        "update_check": {
            "verdict": verdict, "reasons": list(reasons), "notes": [],
            "targets": [{"track": "latest", "risk": "stable", "arch": a, "revision": 4,
                         "version": version} for a in arches],
        },
    }


def _commands(p):
    return [c for s in p["steps"] for c in s["commands"]]


def test_local_security_only_uses_published_version(monkeypatch):
    monkeypatch.setattr(hints, "host_arch", lambda: "amd64")
    p = hints.plan(_entry())
    cmds = _commands(p)
    assert "SNAPCRAFT_BUILD_INFO=1 snapcraft pack" in cmds
    assert "snapcraft upload --release=stable demo_2.2.0_amd64.snap" in cmds
    assert cmds[-1] == "snap-maintainer check demo"
    assert "No code changes are needed" in p["notes"][0]


def test_local_foreign_arch_suggests_remote_build(monkeypatch):
    monkeypatch.setattr(hints, "host_arch", lambda: "amd64")
    p = hints.plan(_entry(arches=("amd64", "arm64"), reasons=(SEC, SRC)))
    cmds = _commands(p)
    assert "snapcraft remote-build --build-for=amd64,arm64" in cmds
    assert "snapcraft upload --release=stable demo_<version>_arm64.snap" in cmds  # version unknown
    assert "Review them before releasing" in p["notes"][0]


def test_tag_workflow_that_uploads():
    wf = {"path": ".github/workflows/release.yml", "uploads": True, "channels": [],
          "triggers": {"tags": ["v*"], "dispatch": False}}
    p = hints.plan(_entry(build={"method": "github-workflow", "workflow": wf}))
    cmds = _commands(p)
    assert cmds[:2] == ["git tag <new-tag>", "git push origin <new-tag>"]
    assert "gh run watch -R o/demo" in cmds
    assert not any(c.startswith("snapcraft upload") for c in cmds)


def test_dispatch_workflow_that_only_builds():
    wf = {"path": ".github/workflows/ci.yml", "uploads": False, "channels": [],
          "triggers": {"dispatch": True}}
    cmds = _commands(hints.plan(_entry(build={"method": "github-workflow", "workflow": wf})))
    assert cmds[0] == "gh workflow run ci.yml -R o/demo"
    assert "gh run download <run-id> -R o/demo" in cmds
    assert "snapcraft upload --release=stable demo_2.2.0_amd64.snap" in cmds


def test_launchpad_and_unknown():
    rec = {"url": "https://launchpad.net/~o/+snap/demo", "store_upload": True, "auto_build": False}
    cmds = _commands(hints.plan(_entry(build={"method": "launchpad", "recipe": rec})))
    assert cmds[0] == "https://launchpad.net/~o/+snap/demo/+request-builds"
    p = hints.plan(_entry(build={"method": "unknown"}))
    assert "set-build demo" in p["notes"][-1]


def test_plans_filter_and_order():
    snaps = {
        "ok": _entry("ok", checks.VERDICT_OK, reasons=()),
        "upd": _entry("upd", checks.VERDICT_UPDATE, reasons=(SRC,)),
        "reb": _entry("reb"),
    }
    assert [p["snap"] for p in hints.plans(snaps)] == ["reb", "upd"]
    assert [p["snap"] for p in hints.plans(snaps, include_all=True)] == ["reb", "upd", "ok"]
    assert [p["snap"] for p in hints.plans(snaps, ["ok"])] == ["ok"]  # named snaps always shown


def test_markdown():
    md = hints.to_markdown(hints.plans({"reb": _entry("reb")}))
    assert md.startswith("# Snap rebuild plan") and "## reb (rebuild)" in md
    assert "```sh" in md and "snap-maintainer check reb" in md
    assert "Nothing needs a rebuild" in hints.to_markdown([])
