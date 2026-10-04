"""fused_render/installed.py's Linux stamp: write_linux_stamp() /
_linux_installed_version() / installed_version(). The mac Info.plist path is
covered by tests/test_update_signal.py; these cover what's new for the
AppImage swap — a match, a moved/replaced file, and mtime drift all reading
back as the honest "no restart signal available" (None) rather than a stale
banner.
"""
import sys

from fused_render import installed


def test_no_stamp_reads_as_none(tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"x")
    assert installed._linux_installed_version(str(appimage)) is None


def test_stamp_matches_the_current_file(tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"payload")
    installed.write_linux_stamp(str(appimage), "1.2.3")
    assert installed._linux_installed_version(str(appimage)) == "1.2.3"


def test_stamp_is_stale_once_the_file_is_replaced(tmp_path):
    """A hand-replaced AppImage (different size/mtime at the same path) must
    not keep showing a restart banner for a swap that never happened to it."""
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"payload")
    installed.write_linux_stamp(str(appimage), "1.2.3")
    appimage.write_bytes(b"a completely different, longer payload")
    assert installed._linux_installed_version(str(appimage)) is None


def test_stamp_is_stale_once_the_file_moves(tmp_path):
    """A moved AppImage no longer matches the stamped path even if the moved
    file's bytes are identical."""
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"payload")
    installed.write_linux_stamp(str(appimage), "1.2.3")
    moved = tmp_path / "moved" / "FusedRender.AppImage"
    moved.parent.mkdir()
    appimage.rename(moved)
    assert installed._linux_installed_version(str(moved)) is None


def test_missing_target_reads_as_none(tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"payload")
    installed.write_linux_stamp(str(appimage), "1.2.3")
    appimage.unlink()
    assert installed._linux_installed_version(str(appimage)) is None


def test_no_target_reads_as_none():
    assert installed._linux_installed_version(None) is None


def test_installed_version_reads_the_stamp_on_linux(monkeypatch, tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"payload")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("APPIMAGE", str(appimage))
    installed.write_linux_stamp(str(appimage), "9.9.9")
    assert installed.installed_version() == "9.9.9"


def test_installed_version_is_none_without_an_appimage_env(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert installed.installed_version() is None


def test_macosx_app_marker_wins_over_the_host_platform(monkeypatch, tmp_path):
    """sys.frozen == "macosx_app" is checked first regardless of the real
    host platform — the unit tests fake a py2app bundle while running on
    whatever CI host is actually Linux, and that must keep reading the
    bundle's Info.plist, not the (nonexistent) Linux stamp."""
    import os
    import plistlib

    executable = tmp_path / "FusedRender.app" / "Contents" / "MacOS" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    with open(executable.parent.parent / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleShortVersionString": "9.9.9"}, f)
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert installed.installed_version() == "9.9.9"
