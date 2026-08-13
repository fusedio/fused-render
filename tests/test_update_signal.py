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
