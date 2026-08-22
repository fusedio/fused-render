"""The server owns exactly one map tile daemon (fused_render/server/map_engine.py).

The old architecture spawned the daemon detached from a short-lived runPython
worker: nothing health-checked it, nothing could restart it, and orphans
accumulated (18 at the last count). The supervisor holds the Popen, so a dead
or wedged child is detected and replaced, and stop() leaves nothing behind.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_map_engine.py -o addopts=""
"""
import json
import sys
import time

import pytest


def _ensure(map_engine, stub_python, stub_daemon, tmp_path, version="v1"):
    return map_engine.ensure(python=stub_python, daemon=stub_daemon,
                             cache=str(tmp_path / "cache"), version=version)


def _wait_exit(proc, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    return proc.poll()


def test_ensure_reuses_a_live_child(map_engine, stub_python, stub_daemon, tmp_path):
    first = _ensure(map_engine, stub_python, stub_daemon, tmp_path)
    second = _ensure(map_engine, stub_python, stub_daemon, tmp_path)
    assert second is first
    assert second.pid == first.pid


def test_a_version_bump_forces_a_new_child(map_engine, stub_python, stub_daemon, tmp_path):
    first = _ensure(map_engine, stub_python, stub_daemon, tmp_path, version="v1")
    second = _ensure(map_engine, stub_python, stub_daemon, tmp_path, version="v2")
    assert second.pid != first.pid
    assert second.version == "v2"
    assert _wait_exit(first.proc) is not None, "the superseded child kept running"


def test_restart_yields_a_working_child(map_engine, stub_python, stub_daemon, tmp_path, child_get):
    first = _ensure(map_engine, stub_python, stub_daemon, tmp_path)
    second = map_engine.restart()
    assert second.pid != first.pid
    assert _wait_exit(first.proc) is not None
    with child_get(second, "/ping") as response:
        assert json.load(response)["ok"] is True


def test_restart_replays_the_described_sources(
    map_engine, stub_python, stub_daemon, tmp_path, child_get, child_post,
):
    # A fresh child has an empty source registry, so a bare restart would 404
    # every tile URL the page holds. The supervisor replays what was described.
    child = _ensure(map_engine, stub_python, stub_daemon, tmp_path)
    descriptor = child_post(child, "/describe", {"target": "scene.tif"})
    source = descriptor["data"]["source_id"]
    map_engine.remember(source, {"target": "scene.tif"})

    healed = map_engine.restart()
    with child_get(healed, f"/tiles/{source}/0/0/0.png") as response:
        assert response.read().startswith(b"\x89PNG")


def test_stop_leaves_no_process(map_engine, stub_python, stub_daemon, tmp_path):
    child = _ensure(map_engine, stub_python, stub_daemon, tmp_path)
    map_engine.stop()
    assert _wait_exit(child.proc) is not None
    assert map_engine.current() is None


def test_a_wedged_child_is_replaced(
    map_engine, stub_python, stub_daemon, tmp_path, child_get, child_post,
):
    # "Listening but not answering" — the port stays bound while /ping times
    # out, which is exactly what the Azure HLS investigation measured.
    first = _ensure(map_engine, stub_python, stub_daemon, tmp_path)
    child_post(first, "/wedge")
    second = _ensure(map_engine, stub_python, stub_daemon, tmp_path)
    assert second.pid != first.pid
    with child_get(second, "/ping") as response:
        assert json.load(response)["ok"] is True


def test_a_python_outside_the_venv_store_is_refused(map_engine, stub_daemon, tmp_path):
    rogue = tmp_path / "python.exe"
    rogue.write_text("", encoding="utf-8")
    with pytest.raises(map_engine.MapEngineError):
        map_engine.ensure(python=str(rogue), daemon=stub_daemon,
                          cache=str(tmp_path), version="v1")


def test_the_apps_own_interpreter_is_accepted(map_engine, stub_daemon, tmp_path, child_get):
    # The builtin executor runs map_render on the app's own interpreter (it has
    # no venv machinery), so that interpreter is as trusted as a project venv's.
    child = map_engine.ensure(python=sys.executable, daemon=stub_daemon,
                              cache=str(tmp_path / "cache"), version="v1")
    with child_get(child, "/ping") as response:
        assert json.load(response)["ok"] is True


def test_a_daemon_outside_a_templates_root_is_refused(map_engine, stub_python, tmp_path):
    rogue = tmp_path / "daemon.py"
    rogue.write_text("raise SystemExit\n", encoding="utf-8")
    with pytest.raises(map_engine.MapEngineError):
        map_engine.ensure(python=stub_python, daemon=str(rogue),
                          cache=str(tmp_path), version="v1")
