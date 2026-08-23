"""The server owns each template's daemon (fused_render/server/engine_host.py).

The old architecture spawned the daemon detached from a short-lived runPython
worker: nothing health-checked it, nothing could restart it, and orphans
accumulated (18 at the last count). The host holds the Popen, so a dead or
wedged child is detected and replaced, and stop_all() leaves nothing behind.
These tests drive it through the map engine id, its first user.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_engine_host.py -o addopts=""
"""
import json
import sys
import time

import pytest

ENGINE = "map"


def _ensure(engine_host, stub_python, stub_daemon, tmp_path, version="v1"):
    return engine_host.ensure(engine_id=ENGINE, python=stub_python,
                              daemon=stub_daemon,
                              cache=str(tmp_path / "cache"), version=version)


def _wait_exit(proc, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.05)
    return proc.poll()


def test_ensure_reuses_a_live_child(engine_host, stub_python, stub_daemon, tmp_path):
    first = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    second = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    assert second is first
    assert second.pid == first.pid


def test_a_version_bump_forces_a_new_child(engine_host, stub_python, stub_daemon, tmp_path):
    first = _ensure(engine_host, stub_python, stub_daemon, tmp_path, version="v1")
    second = _ensure(engine_host, stub_python, stub_daemon, tmp_path, version="v2")
    assert second.pid != first.pid
    assert second.version == "v2"
    assert _wait_exit(first.proc) is not None, "the superseded child kept running"


def test_restart_yields_a_working_child(engine_host, stub_python, stub_daemon, tmp_path, child_get):
    first = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    second = engine_host.restart(ENGINE)
    assert second.pid != first.pid
    assert _wait_exit(first.proc) is not None
    with child_get(second, "/ping") as response:
        assert json.load(response)["ok"] is True


def test_restart_replays_the_reinit_requests(
    engine_host, stub_python, stub_daemon, tmp_path, child_get, child_post,
):
    # A fresh child has an empty registry, so a bare restart would 404 every URL
    # the page holds. The host replays what the template registered as reinit.
    child = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    descriptor = child_post(child, "/describe", {"target": "scene.tif"})
    source = descriptor["data"]["source_id"]
    engine_host.reinit(ENGINE, source, "/describe", {"target": "scene.tif"})

    healed = engine_host.restart(ENGINE)
    with child_get(healed, f"/tiles/{source}/0/0/0.png") as response:
        assert response.read().startswith(b"\x89PNG")


def test_forget_stops_a_reinit_from_replaying(
    engine_host, stub_python, stub_daemon, tmp_path, child_post, child_get,
):
    child = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    descriptor = child_post(child, "/describe", {"target": "scene.tif"})
    source = descriptor["data"]["source_id"]
    engine_host.reinit(ENGINE, source, "/describe", {"target": "scene.tif"})
    engine_host.forget(ENGINE, source)

    healed = engine_host.restart(ENGINE)
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as caught:
        child_get(healed, f"/tiles/{source}/0/0/0.png")
    assert caught.value.code == 404


def test_stop_leaves_no_process(engine_host, stub_python, stub_daemon, tmp_path):
    child = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    engine_host.stop(ENGINE)
    assert _wait_exit(child.proc) is not None
    assert engine_host.current(ENGINE) is None


def test_a_wedged_child_is_replaced(
    engine_host, stub_python, stub_daemon, tmp_path, child_get, child_post,
):
    # "Listening but not answering" — the port stays bound while /ping times
    # out, which is exactly what the Azure HLS investigation measured.
    first = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    child_post(first, "/wedge")
    second = _ensure(engine_host, stub_python, stub_daemon, tmp_path)
    assert second.pid != first.pid
    with child_get(second, "/ping") as response:
        assert json.load(response)["ok"] is True


def test_a_python_outside_the_venv_store_is_refused(engine_host, stub_daemon, tmp_path):
    rogue = tmp_path / "python.exe"
    rogue.write_text("", encoding="utf-8")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure(engine_id=ENGINE, python=str(rogue), daemon=stub_daemon,
                           cache=str(tmp_path), version="v1")


def test_the_apps_own_interpreter_is_accepted(engine_host, stub_daemon, tmp_path, child_get):
    # The builtin executor runs map_render on the app's own interpreter (it has
    # no venv machinery), so that interpreter is as trusted as a project venv's.
    child = engine_host.ensure(engine_id=ENGINE, python=sys.executable,
                               daemon=stub_daemon,
                               cache=str(tmp_path / "cache"), version="v1")
    with child_get(child, "/ping") as response:
        assert json.load(response)["ok"] is True


def test_a_daemon_outside_a_templates_root_is_refused(engine_host, stub_python, tmp_path):
    rogue = tmp_path / "daemon.py"
    rogue.write_text("raise SystemExit\n", encoding="utf-8")
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure(engine_id=ENGINE, python=stub_python, daemon=str(rogue),
                           cache=str(tmp_path), version="v1")


def test_a_daemon_from_another_engine_folder_is_refused(engine_host, stub_python, stub_daemon, tmp_path):
    # The daemon must live at <root>/<engine_id>/daemon.py; the map stub under a
    # different engine id must be rejected, or the id would be a free-form path.
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure(engine_id="other", python=stub_python, daemon=stub_daemon,
                           cache=str(tmp_path), version="v1")


def test_an_engine_id_with_separators_is_refused(engine_host, stub_python, stub_daemon, tmp_path):
    with pytest.raises(engine_host.EngineError):
        engine_host.ensure(engine_id="../map", python=stub_python, daemon=stub_daemon,
                           cache=str(tmp_path), version="v1")
