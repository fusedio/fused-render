"""The tile service must outlive the page that holds its tile URLs (daemon.py).

Every raster tile URL embeds this service's port and token, so an idle exit
breaks every layer already on the map with a bare fetch failure the page cannot
distinguish from a real tile error.

Run explicitly:
  PYTHONPATH=<checkout> python -m pytest \
    fused_render/templates/map/tests/test_daemon_lifetime.py -o addopts=""
"""
import importlib.util
import os
import sys

import pytest

_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHARED = os.path.join(os.path.dirname(_MAP), "shared")


def _load(name, filename, env=None):
    for path in (_SHARED, _MAP):
        if path not in sys.path:
            sys.path.insert(0, path)
    previous = {}
    for key, value in (env or {}).items():
        previous[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        spec = importlib.util.spec_from_file_location(name, os.path.join(_MAP, filename))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def daemon():
    return _load("map_daemon", "daemon.py")


def test_idle_exit_is_disabled_by_default(daemon):
    assert daemon.IDLE_TIMEOUT == 0


def test_idle_monitor_is_not_started_when_disabled(daemon, monkeypatch):
    started = []
    monkeypatch.setattr(daemon.threading, "Thread",
                        lambda *a, **k: started.append(k.get("target")) or _NoThread())
    assert daemon.IDLE_TIMEOUT == 0
    assert daemon._idle_monitor not in started


class _NoThread:
    def start(self):
        pass


def test_idle_exit_still_honours_an_explicit_opt_in():
    # An operator (or a test) can still ask for the old behaviour.
    opted_in = _load("map_daemon_idle", "daemon.py", {"MAP_VIEWER_IDLE_TIMEOUT": "60"})
    assert opted_in.IDLE_TIMEOUT == 60
