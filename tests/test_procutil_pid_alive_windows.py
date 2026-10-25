"""procutil.pid_alive must never call a bare os.kill(pid, 0): on Windows signal 0
is CTRL_C_EVENT and Ctrl+C's the whole console, server included."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SHARED = Path(__file__).resolve().parent.parent / "fused_render" / "templates" / "shared"


def _load():
    spec = importlib.util.spec_from_file_location("_t_procutil", SHARED / "procutil.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _forbid_kill(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("os.kill called")
    monkeypatch.setattr(os, "kill", boom)


def test_windows_never_calls_os_kill(monkeypatch):
    procutil = _load()
    monkeypatch.setattr(procutil._platutil, "IS_WINDOWS", True)
    probed = []
    monkeypatch.setattr(procutil._platutil, "probe_pid", lambda pid: probed.append(pid))
    _forbid_kill(monkeypatch)
    assert procutil.pid_alive(os.getpid()) is True
    assert probed == [os.getpid()]


def test_windows_path_without_probe_stub_never_kills(monkeypatch):
    # Real probe_pid on a faked-Windows platutil must not fall to os.kill.
    procutil = _load()
    monkeypatch.setattr(procutil._platutil, "IS_WINDOWS", True)
    _forbid_kill(monkeypatch)
    try:
        procutil.pid_alive(os.getpid())
    except AssertionError:
        raise
    except Exception:
        pass  # no ctypes.windll off Windows: fine, only os.kill is forbidden


@pytest.mark.parametrize("bad", [0, -1, "x", None, "0"])
def test_invalid_pids_false_without_probing(monkeypatch, bad):
    procutil = _load()

    def boom(pid):
        raise AssertionError("probed")
    monkeypatch.setattr(procutil._platutil, "probe_pid", boom)
    _forbid_kill(monkeypatch)
    assert procutil.pid_alive(bad) is False


def test_posix_live_and_dead():
    procutil = _load()
    assert procutil.pid_alive(os.getpid()) is True
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert procutil.pid_alive(p.pid) is False
