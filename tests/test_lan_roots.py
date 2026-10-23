"""LAN allow-list: a registered app at a filesystem/drive root never exposes the volume."""
import os

from fused_render import lan


def _as_windows(monkeypatch):
    import ntpath
    monkeypatch.setattr(os, "path", ntpath)
    monkeypatch.setattr(os, "sep", "\\")


def test_posix_slash_root_does_not_open_everything(monkeypatch):
    monkeypatch.setattr(lan, "allowed_roots", lambda: ["/"])
    assert not lan._inside_root("/etc/passwd")


def test_windows_drive_root_does_not_open_the_volume(monkeypatch):
    _as_windows(monkeypatch)
    monkeypatch.setattr(lan, "allowed_roots", lambda: ["D:\\"])
    monkeypatch.setattr(os.path, "realpath", lambda p: p)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p)
    assert not lan._inside_root("D:\\secrets\\x")


def test_normal_root_still_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(lan, "allowed_roots", lambda: [str(tmp_path)])
    assert lan._inside_root(str(tmp_path / "a.txt"))


def test_zip_slip_check_refuses_a_filesystem_root_staging_dir():
    import zipfile

    from fused_render import zip_import

    assert zip_import.reject_reason(zipfile.ZipInfo("etc/x"), "/") is not None
    assert zip_import.reject_reason(zipfile.ZipInfo("a/x"), "/tmp/stage") is None
