import os
import subprocess
import sys

import pytest

from fused_render import winopen


def test_progid():
    assert winopen._progid(".csv") == "FusedRender.csv"


def test_type_name():
    assert winopen._type_name(".csv") == "CSV File (fused-render)"


def test_extensions_are_single_suffix_and_clean():
    exts = winopen.extensions()
    assert exts
    assert all(e.startswith(".") and e.count(".") == 1 for e in exts)
    assert not (winopen._NOT_EXTENSIONS & set(exts))
    assert exts == sorted(set(exts))


def test_view_url_no_path():
    assert winopen._view_url(1777, None) == "http://127.0.0.1:1777/"


def test_view_url_encodes_drive_path(monkeypatch):
    # stub abspath so the encoding is exercised the same on POSIX CI as on Windows
    monkeypatch.setattr(winopen.os.path, "abspath", lambda p: p)
    assert winopen._view_url(1777, r"C:\data\sales.csv") == (
        "http://127.0.0.1:1777/view/C%3A/data/sales.csv"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="probes a Windows process handle")
def test_pid_alive():
    assert winopen._pid_alive(os.getpid()) is True
    reaped = subprocess.Popen([sys.executable, "-c", ""])
    reaped.wait()
    assert winopen._pid_alive(reaped.pid) is False


def test_find_running_server_ignores_stale_portfile(monkeypatch):
    # portfile points at a live port a foreign app holds; our recorded server is
    # gone (dead pid) -> we must fall through, never waiting the boot grace period
    monkeypatch.setattr(winopen, "_read_int", lambda path: 1777 if path == winopen.PORTFILE else 4242)
    monkeypatch.setattr(winopen, "_fused_server", lambda port: False)
    monkeypatch.setattr(winopen, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(winopen, "_port_in_use", lambda port: True)
    monkeypatch.setattr(winopen, "_wait_until_ready", lambda *a, **k: pytest.fail("waited on stale portfile"))
    assert winopen.find_running_server() is None


def test_ensure_server_racing_guard_waits_without_spawning(monkeypatch):
    # a peer double-click bound the picked port and is still booting -> settle on
    # it rather than spawning a second server onto the occupied port
    monkeypatch.setattr(winopen, "find_running_server", lambda: None)
    monkeypatch.setattr(winopen, "pick_port", lambda *a, **k: 1780)
    monkeypatch.setattr(winopen, "_settle", lambda port: port == 1780)
    monkeypatch.setattr(winopen, "_spawn", lambda port: pytest.fail("spawned onto occupied port"))
    assert winopen._ensure_server(None) == 1780
