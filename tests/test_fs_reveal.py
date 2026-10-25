"""POST /api/fs/reveal: open a path in the OS file manager, per platform."""
import os
import sys

import pytest

from fused_render.server.routers import fs_read


@pytest.fixture
def spy(monkeypatch):
    calls = {"popen": [], "startfile": []}
    monkeypatch.setattr(fs_read.subprocess, "Popen",
                        lambda cmd, **kw: calls["popen"].append(cmd))
    monkeypatch.setattr(os, "startfile", lambda p: calls["startfile"].append(p),
                        raising=False)
    return calls


def _reveal(path):
    return fs_read.api_fs_reveal(body={"path": path}, x_fused="1")


def test_windows_directory_is_opened_with_startfile(monkeypatch, spy, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    assert _reveal(str(tmp_path)).status_code == 200
    assert spy["startfile"] == [str(tmp_path)]
    assert spy["popen"] == []


def test_windows_file_is_selected_in_explorer(monkeypatch, spy, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert _reveal(str(f)).status_code == 200
    assert spy["popen"] == [["explorer", f"/select,{os.path.normpath(str(f))}"]]
    assert spy["startfile"] == []


def test_windows_never_uses_xdg_open(monkeypatch, spy, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    f = tmp_path / "a.txt"
    f.write_text("x")
    _reveal(str(f))
    _reveal(str(tmp_path))
    assert all("xdg-open" not in c for c in spy["popen"])


def test_linux_and_macos_commands_unchanged(monkeypatch, spy, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    monkeypatch.setattr(sys, "platform", "linux")
    _reveal(str(f))
    _reveal(str(tmp_path))
    monkeypatch.setattr(sys, "platform", "darwin")
    _reveal(str(f))
    _reveal(str(tmp_path))
    assert spy["popen"] == [
        ["xdg-open", str(tmp_path)], ["xdg-open", str(tmp_path)],
        ["open", "-R", str(f)], ["open", str(tmp_path)],
    ]
