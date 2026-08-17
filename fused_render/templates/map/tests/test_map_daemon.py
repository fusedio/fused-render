"""Tests for the map template's daemon lifecycle in map_render.py.

Hermetic: no fused-render server and no real daemon are spawned — the spawn is
monkeypatched. Loaded via importlib with templates/shared on sys.path, like the
latex daemon tests.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_map_daemon.py -o addopts=""
"""
import importlib.util
import os
import subprocess
import sys
import threading
import time

import pytest

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")


def _load(name, filename):
    for path in (_SHARED, _MAP):
        if path not in sys.path:
            sys.path.insert(0, path)
    spec = importlib.util.spec_from_file_location(name, os.path.join(_MAP, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mr(tmp_path, monkeypatch):
    m = _load("map_render", "map_render.py")
    monkeypatch.setattr(m, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(m, "STATE", tmp_path / "daemon.json")
    monkeypatch.setattr(m, "START_LOCK", tmp_path / "daemon.spawn.lock")
    monkeypatch.setattr(m, "LOG", tmp_path / "daemon.log")
    return m


def test_ensure_service_reuses_a_live_daemon_without_spawning(mr, monkeypatch):
    state = {"port": 7, "token": "t", "version": mr.VERSION}
    monkeypatch.setattr(mr, "_read_state", lambda: state)
    monkeypatch.setattr(mr, "_ping", lambda st, timeout=2.0: True)
    spawns = []
    monkeypatch.setattr(mr, "_spawn_daemon", lambda: spawns.append(1))
    assert mr._ensure_service() == state
    assert spawns == []  # a healthy daemon is never re-spawned


def test_ensure_service_serializes_the_spawn(mr, monkeypatch):
    # Two concurrent ensures: exactly one daemon is spawned; the waiter blocks on
    # the kernel file_lock, then reuses what the winner started — the storm fix.
    box = {"state": None}
    monkeypatch.setattr(mr, "_read_state", lambda: box["state"])
    monkeypatch.setattr(mr, "_ping", lambda st, timeout=2.0: bool(st))
    spawns = []

    def slow_spawn():
        spawns.append(1)
        time.sleep(0.3)
        box["state"] = {"port": 5, "token": "t", "version": mr.VERSION}

    monkeypatch.setattr(mr, "_spawn_daemon", slow_spawn)
    monkeypatch.setattr(mr, "_wait_for_service", lambda timeout: box["state"])

    results = []
    threads = [
        threading.Thread(target=lambda: results.append(mr._ensure_service()))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert len(spawns) == 1  # only one server ever spawned
    assert all(r == box["state"] for r in results)  # both callers got that server


def test_spawn_daemon_detaches_and_never_flashes_a_window(mr, monkeypatch):
    captured = {}

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(mr.subprocess, "Popen", FakePopen)
    mr._spawn_daemon()

    # Detached children launch via the shared spawn_python (pythonw on Windows),
    # not raw sys.executable, so a fallback python.exe never flashes a console.
    assert captured["cmd"][0] == mr.spawn_python()
    assert str(mr.DAEMON) in captured["cmd"]
    kwargs = captured["kwargs"]
    assert kwargs["close_fds"] is True
    if sys.platform == "win32":
        flags = kwargs["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        # CREATE_NO_WINDOW + DETACHED_PROCESS fail to launch together on Windows.
        assert not (flags & subprocess.CREATE_NO_WINDOW)
    else:
        assert kwargs["start_new_session"] is True
