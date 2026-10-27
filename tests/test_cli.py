import io

from rich.console import Console

from snap_maintainer import checks, preflight, ui


def _capture(monkeypatch):
    buf = io.StringIO()
    console = Console(file=buf, width=120, color_system=None)
    monkeypatch.setattr(ui, "out", console)
    monkeypatch.setattr(ui, "err", console)
    return buf


def test_preflight_missing_tools(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: None)
    results = preflight.run("check")
    assert [(c.name, c.ok, c.required) for c in results] == [
        ("snapcraft", False, True), ("gh", False, True),
        ("unsquashfs", False, False), ("git", False, False)]
    buf = _capture(monkeypatch)
    assert ui.preflight(results) is False
    text = buf.getvalue()
    assert "Can't continue" in text and "sudo snap install snapcraft --classic" in text
    assert "squashfs-tools" in text  # optional tools are warnings with a fix


def test_preflight_logged_out(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout=60: (
        (1, "Credentials could not be parsed.") if cmd[0] == "snapcraft"
        else (4, "To get started with GitHub CLI, please run:  gh auth login")))
    snap, gh = preflight.run("discover")
    assert not snap.ok and snap.fix == "snapcraft login"
    assert not gh.ok and gh.fix == "gh auth login"


def test_preflight_ok(monkeypatch):
    monkeypatch.setattr(preflight.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(preflight, "_run", lambda cmd, timeout=60: (
        (0, "email: x\nusername: me\nexpires: 2027-08-31T21:17:54.000Z") if cmd[0] == "snapcraft"
        else (0, "octocat")))
    buf = _capture(monkeypatch)
    assert ui.preflight(preflight.run("discover")) is True
    assert "me · login expires 2027-08-31" in buf.getvalue() and "octocat" in buf.getvalue()


def test_check_report_renders(monkeypatch):
    buf = _capture(monkeypatch)
    target = {"track": "latest", "risk": "stable", "arch": "amd64", "revision": 4,
              "version": "1.0", "uploaded": "2026-10-13T00:00:00Z", "security_method": "manifest"}
    snaps = {
        "a": {"update_check": {"verdict": checks.VERDICT_REBUILD, "targets": [target], "notes": [],
              "reasons": [{"kind": "security", "usn": "USN-1-1", "severity": "High",
                           "issued": "2026-10-26T00:00:00Z", "source": "nginx",
                           "fixed_version": "1.2", "staged_version": "1.1", "arches": ["amd64"]},
                          {"kind": "source", "detail": "2 commit(s) on main",
                           "commits": [{"sha": "abcdef1234", "message": "x" * 100}]}]}},
        "b": {"update_check": {"verdict": checks.VERDICT_OK, "targets": [target], "notes": [],
                               "reasons": []}},
    }
    ui.check_report(snaps)
    text = buf.getvalue()
    assert "REBUILD" in text and "UP TO DATE" in text
    assert "1.1 → 1.2" in text and "USN-1-1" in text and "…" in text
    assert text.index(" a ") < text.index(" b ")  # rebuilds sort first
