"""Superseded services must be retired, not left running (map_render.py).

The service never exits on its own — a page's tile URLs point at its port, so
an idle exit would break the map. That makes it the caller's job to shut the
previous one down when the backend version changes, otherwise every upgrade
(and every edit during development) strands a process holding a warm
geospatial runtime until the machine reboots.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_daemon_retire.py -o addopts=""
"""
import importlib.util
import json
import os
import sys

import pytest

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")


def _load(name, filename, cache_dir):
    for path in (_SHARED, _MAP):
        if path not in sys.path:
            sys.path.insert(0, path)
    previous = os.environ.get("FUSED_RENDER_MAP_CACHE")
    os.environ["FUSED_RENDER_MAP_CACHE"] = str(cache_dir)
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_MAP, filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if previous is None:
            os.environ.pop("FUSED_RENDER_MAP_CACHE", None)
        else:
            os.environ["FUSED_RENDER_MAP_CACHE"] = previous


@pytest.fixture
def render(tmp_path):
    return _load("map_render", "map_render.py", tmp_path)


def _write_state(module, version, port, pid, token="tok", home=None):
    path = module.CACHE_DIR / f"daemon-{version}.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "home": str(module.HERE) if home is None else home,
                "port": port,
                "pid": pid,
                "token": token,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_a_stale_version_is_retired_and_forgotten(render, monkeypatch):
    stale = _write_state(render, "oldversion123", 4001, os.getpid())
    retired = []
    monkeypatch.setattr(render, "_retire", lambda state: retired.append(state["port"]))

    render._retire_superseded()

    assert retired == [4001]
    assert not stale.exists()


def test_the_current_service_is_left_alone(render, monkeypatch):
    current = _write_state(render, render.VERSION, 4002, os.getpid())
    retired = []
    monkeypatch.setattr(render, "_retire", lambda state: retired.append(state["port"]))

    render._retire_superseded()

    assert retired == []
    assert current.exists()


def test_a_state_file_whose_process_is_gone_is_only_deleted(render, monkeypatch):
    # PID 0 is never a real user process on either platform.
    stale = _write_state(render, "oldversion123", 4003, 0)
    retired = []
    monkeypatch.setattr(render, "_retire", lambda state: retired.append(state["port"]))
    monkeypatch.setattr(render, "pid_alive", lambda pid: False)

    render._retire_superseded()

    assert retired == []
    assert not stale.exists()


def test_another_checkout_sharing_the_cache_is_left_running(render, monkeypatch):
    # Two worktrees can share ~/.fused-render; the other one's service is a peer
    # serving its own map, not a leftover of ours.
    peer = _write_state(
        render, "otherversion1", 4004, os.getpid(), home=r"C:\elsewhere\map"
    )
    retired = []
    monkeypatch.setattr(render, "_retire", lambda state: retired.append(state["port"]))

    render._retire_superseded()

    assert retired == []
    assert peer.exists()


def test_starting_a_service_retires_the_previous_one(render, monkeypatch):
    calls = []
    monkeypatch.setattr(render, "_retire_superseded", lambda: calls.append("swept"))
    monkeypatch.setattr(render, "_spawn_daemon", lambda: calls.append("spawned"))
    monkeypatch.setattr(render, "_wait_for_service", lambda timeout: {"port": 1, "token": "t"})

    render._ensure_service()

    assert calls == ["swept", "spawned"]
