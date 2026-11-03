"""Update-signal plumbing: /api/config reports the version installed on disk
(Info.plist of the .app bundle) alongside the running __version__, so the
shell can tell the user "new version installed — restart" when the two drift
apart (a DMG install replaces the bundle under a still-running process).
"""
import os
import plistlib
import sys

from fastapi.testclient import TestClient

from fused_render import __version__
from fused_render.installed import installed_version
from fused_render.server.app import create_app


def _fake_bundle(tmp_path, version):
    """Lay out Contents/{MacOS/python,Info.plist} like a py2app bundle."""
    contents = tmp_path / "FusedRender.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    executable = contents / "MacOS" / "python"
    executable.write_bytes(b"")
    with open(contents / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version}, f)
    return str(executable)


def test_unpackaged_run_has_no_installed_version(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert installed_version() is None


def test_packaged_run_reads_bundle_plist(monkeypatch, tmp_path):
    executable = _fake_bundle(tmp_path, "9.9.9")
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    assert installed_version() == "9.9.9"


def test_missing_plist_reads_as_none(monkeypatch, tmp_path):
    executable = _fake_bundle(tmp_path, "9.9.9")
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    contents = os.path.dirname(os.path.dirname(executable))
    os.remove(os.path.join(contents, "Info.plist"))
    assert installed_version() is None


def test_corrupt_plist_reads_as_none(monkeypatch, tmp_path):
    # A truncated/corrupt plist mid-DMG-install must degrade to "no signal".
    executable = _fake_bundle(tmp_path, "9.9.9")
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    contents = os.path.dirname(os.path.dirname(executable))
    with open(os.path.join(contents, "Info.plist"), "wb") as f:
        f.write(b"not a plist")
    assert installed_version() is None


def test_config_exposes_installed_version(monkeypatch, tmp_path):
    executable = _fake_bundle(tmp_path, "9.9.9")
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    config = client.get("/api/config").json()
    assert config["version"] == __version__
    assert config["installed_version"] == "9.9.9"


def test_config_installed_version_is_null_when_unpackaged(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.get("/api/config").json()["installed_version"] is None


def _stage_beside(executable, version):
    """A staged update sibling (update/mac.py's `.FusedRender-update.app`)
    next to the bundle `executable` lives in."""
    contents = os.path.dirname(os.path.dirname(executable))
    bundle = os.path.dirname(contents)
    staged = os.path.join(os.path.dirname(bundle), ".FusedRender-update.app", "Contents")
    os.makedirs(staged)
    with open(os.path.join(staged, "Info.plist"), "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version}, f)


def test_a_staged_update_is_what_launches_next(monkeypatch, tmp_path):
    """The in-app updater stages beside the bundle and the quit applies it,
    so the staged version IS the installed version for the restart signal:
    /api/config drifts, the restart card appears, begin_relaunch acts."""
    executable = _fake_bundle(tmp_path, "9.9.9")
    _stage_beside(executable, "9.9.10")
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    assert installed_version() == "9.9.10"


def test_a_staged_update_that_is_not_newer_is_ignored(monkeypatch, tmp_path):
    """An older download left staged under a bundle the user upgraded by
    hand must not report itself as next — the quit refuses to apply it."""
    executable = _fake_bundle(tmp_path, "9.9.9")
    _stage_beside(executable, "9.9.0")
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    assert installed_version() == "9.9.9"
