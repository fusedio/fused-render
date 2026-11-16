"""D1482: a daemon-only app's unbuilt venv is built by start/restart/autostart
and the daemon is moved onto it once it exists."""
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import background_apps, envinstall
from fused_render.server import create_app, engine_host
from fused_render.shell.storage import home_dir

HDRS = {"X-Fused": "1"}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture(autouse=True)
def fast_watcher(monkeypatch):
    monkeypatch.setattr(background_apps, "_ENV_POLL_S", 0.01)
    monkeypatch.setattr(background_apps, "_ENV_MAX_WAIT_S", 5.0)
    background_apps._env_watchers.clear()
    yield
    # Drain any watcher a test left polling while the fakes are still in
    # place, so none outlives the test and fires into the next one's fakes.
    monkeypatch.setattr(envinstall, "progress",
                        lambda k: {"done": True, "error": "test teardown"})
    end = time.monotonic() + 3
    while background_apps._env_watchers and time.monotonic() < end:
        time.sleep(0.01)
    background_apps._env_watchers.clear()


def _folder(tmp_path, name="app"):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "pyproject.toml").write_text(
        '[tool.fused-render.app]\ndaemon = "daemon.py"\n')
    (folder / "daemon.py").write_text("# daemon\n")
    (folder / "index.html").write_text("<html></html>")
    return folder


def _venv_python(tmp_path):
    """A python path under the home venv store (passes `_validate_interpreter`),
    not created yet."""
    return os.path.join(home_dir(), "venvs", tmp_path.name, "bin", "python")


def _child(engine_id, python, folder, version="v1"):
    return engine_host.Child(engine_id=engine_id, python=python,
                             daemon=str(folder / "daemon.py"), cache="c",
                             version=version, pid=1)


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class FakeEnv:
    """Stands in for envinstall.start/is_installed/progress/venv_key_for."""

    def __init__(self, monkeypatch, venv_python):
        self.venv_python = venv_python
        self.installed = False
        self.error = None
        self.starts = []
        monkeypatch.setattr(envinstall, "start", self.start)
        monkeypatch.setattr(envinstall, "is_installed", lambda p: self.installed)
        monkeypatch.setattr(envinstall, "progress", self.progress)
        monkeypatch.setattr(envinstall, "venv_key_for", lambda p: "key")
        monkeypatch.setattr(background_apps, "interpreter_for",
                            lambda f: self.venv_python)

    def start(self, project_dir, **kw):
        self.starts.append(project_dir)
        return {"stage": "spawn", "pct": 1, "done": False, "error": None}

    def progress(self, key):
        if self.error:
            return {"done": True, "error": self.error}
        return {"done": False, "error": None}

    def finish(self):
        os.makedirs(os.path.dirname(self.venv_python), exist_ok=True)
        open(self.venv_python, "w").close()
        self.installed = True


# ---------------------------------------------------------- engine_host.restart


def test_restart_with_python_swaps_the_interpreter(monkeypatch, tmp_path):
    folder = _folder(tmp_path)
    old = _child("bg_swap", sys.executable, folder)
    monkeypatch.setitem(engine_host._children, "bg_swap", old)
    spawned = []
    monkeypatch.setattr(engine_host, "_terminate", lambda c: None)
    monkeypatch.setattr(engine_host, "_spawn", lambda c: spawned.append(c))
    monkeypatch.setattr(engine_host, "_replay", lambda c: None)
    venv_py = _venv_python(tmp_path)

    child = engine_host.restart("bg_swap", version="v2", python=venv_py)
    assert child.python == venv_py and child.version == "v2"
    assert spawned == [child]

    # default keeps the existing interpreter
    child2 = engine_host.restart("bg_swap")
    assert child2.python == venv_py


def test_restart_refuses_a_foreign_python(monkeypatch, tmp_path):
    folder = _folder(tmp_path)
    monkeypatch.setitem(engine_host._children, "bg_bad",
                        _child("bg_bad", sys.executable, folder))
    monkeypatch.setattr(engine_host, "_terminate",
                        lambda c: pytest.fail("must validate before killing"))
    with pytest.raises(engine_host.EngineError):
        engine_host.restart("bg_bad", python="/usr/bin/evil")


# ------------------------------------------------------------------ routes


def test_restart_route_passes_the_fresh_interpreter(client, tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    engine_id = background_apps.engine_id_for(str(folder))
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    live = _child(engine_id, sys.executable, folder)
    monkeypatch.setattr(engine_host, "current", lambda eid: live)
    got = {}

    def fake_restart(eid, failed=None, version=None, *, python=None):
        got["python"] = python
        return _child(eid, python or sys.executable, folder, version or "v")

    monkeypatch.setattr(engine_host, "restart", fake_restart)
    resp = client.post("/api/apps/background/restart",
                       json={"html": str(folder / "index.html")}, headers=HDRS)
    assert resp.status_code == 200, resp.text
    assert got["python"] == sys.executable
    assert "installing" not in resp.json()


def test_start_with_unbuilt_venv_builds_once_and_reports_installing(
        client, tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda eid, py, *a, **k: _child(eid, py, folder))
    monkeypatch.setattr(engine_host, "current", lambda eid: None)

    resp = client.post("/api/apps/background/start",
                       json={"html": str(folder / "index.html")}, headers=HDRS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["installing"] is True
    assert body["install"]["stage"] == "spawn"
    assert env.starts == [os.path.realpath(str(folder))]
    env.error = "boom"  # let the watcher finish


def test_start_with_built_venv_does_not_touch_envinstall(
        client, tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    monkeypatch.setattr(envinstall, "start",
                        lambda *a, **k: pytest.fail("nothing to build"))
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda eid, py, *a, **k: _child(eid, py, folder))
    resp = client.post("/api/apps/background/start",
                       json={"html": str(folder / "index.html")}, headers=HDRS)
    assert resp.status_code == 200
    assert "installing" not in resp.json()


# ------------------------------------------------------------------ watcher


def test_watcher_respawns_on_venv_after_install(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    f = os.path.realpath(str(folder))
    eid = background_apps.engine_id_for(f)
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    live = _child(eid, sys.executable, folder)
    monkeypatch.setattr(engine_host, "current", lambda e: live)
    monkeypatch.setattr(engine_host, "_alive", lambda c: True)
    calls = []
    monkeypatch.setattr(engine_host, "restart",
                        lambda e, failed=None, version=None, *, python=None:
                        calls.append((e, version, python)))

    assert background_apps.build_env_then_respawn(f)["stage"] == "spawn"
    time.sleep(0.05)
    assert calls == []  # not installed yet
    env.finish()
    assert _wait(lambda: calls)
    assert calls == [(eid, background_apps.version_for(f, env.venv_python),
                      env.venv_python)]
    assert _wait(lambda: f not in background_apps._env_watchers)


def test_watcher_does_nothing_when_no_child_is_running(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    f = os.path.realpath(str(folder))
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    monkeypatch.setattr(engine_host, "current", lambda e: None)
    monkeypatch.setattr(engine_host, "restart",
                        lambda *a, **k: pytest.fail("nothing to restart"))
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: pytest.fail("not start_if_stopped"))
    background_apps.build_env_then_respawn(f)
    env.finish()
    assert _wait(lambda: f not in background_apps._env_watchers)


def test_watcher_leaves_the_fallback_alone_on_install_error(
        tmp_path, monkeypatch, caplog):
    folder = _folder(tmp_path)
    f = os.path.realpath(str(folder))
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    env.error = "no wheel for pypdf"
    monkeypatch.setattr(engine_host, "current",
                        lambda e: pytest.fail("must not look at the engine"))
    with caplog.at_level("WARNING"):
        background_apps.build_env_then_respawn(f)
        assert _wait(lambda: f not in background_apps._env_watchers)
    assert "no wheel for pypdf" in caplog.text


def test_two_starts_share_one_watcher(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    f = os.path.realpath(str(folder))
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    monkeypatch.setattr(engine_host, "current", lambda e: None)
    def watchers():
        return [t for t in threading.enumerate()
                if t.name == f"bg-env-{os.path.basename(f)}"]

    background_apps.build_env_then_respawn(f)
    background_apps.build_env_then_respawn(f)
    assert len(watchers()) == 1
    assert len(env.starts) == 2  # envinstall.start itself is idempotent
    env.error = "stop"
    assert _wait(lambda: f not in background_apps._env_watchers)


def test_helper_never_raises(tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    FakeEnv(monkeypatch, _venv_python(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("uv missing")

    monkeypatch.setattr(envinstall, "start", boom)
    assert background_apps.build_env_then_respawn(str(folder)) is None


def test_helper_ignores_a_folder_that_runs_on_sys_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(background_apps, "interpreter_for", lambda f: sys.executable)
    monkeypatch.setattr(envinstall, "start",
                        lambda *a, **k: pytest.fail("nothing to build"))
    assert background_apps.build_env_then_respawn(str(tmp_path)) is None


# -------------------------------------------------------------- autostart


def test_resurrect_autostart_builds_then_starts(tmp_path, workspace, monkeypatch):
    folder = _folder(tmp_path)
    f = os.path.realpath(str(folder))
    background_apps.set_autostart(f, True)
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    started = []
    monkeypatch.setattr(engine_host, "current", lambda e: None)
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda eid, py, *a, **k: started.append(py))

    background_apps.resurrect_autostart()
    assert env.starts == [f]
    assert started == []  # skipped until the venv lands
    env.finish()
    assert _wait(lambda: started)
    assert started == [env.venv_python]


# ------------------------------------------- failed fallback still builds (D1482)


def _failing_spawn(*a, **k):
    raise engine_host.EngineError("no module named pypdf")


@pytest.mark.parametrize("route", ["start", "restart"])
def test_failed_fallback_spawn_still_starts_build_and_reports_installing(
        client, tmp_path, monkeypatch, route):
    folder = _folder(tmp_path)
    f = os.path.realpath(str(folder))
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    monkeypatch.setattr(engine_host, "ensure_background", _failing_spawn)
    monkeypatch.setattr(engine_host, "current", lambda eid: None)
    flags = []
    real = background_apps.build_env_then_respawn
    monkeypatch.setattr(
        background_apps, "build_env_then_respawn",
        lambda folder, start_if_stopped=False:
        (flags.append(start_if_stopped), real(folder, start_if_stopped))[1])

    resp = client.post(f"/api/apps/background/{route}",
                       json={"html": str(folder / "index.html")}, headers=HDRS)
    assert resp.status_code == 502, resp.text
    body = resp.json()
    assert "being installed" in body["error"]
    assert body["installing"] is True
    assert body["install"]["stage"] == "spawn"
    assert env.starts == [f]
    assert flags == [True]
    env.error = "stop"


def test_later_start_if_stopped_call_upgrades_an_existing_watcher(
        tmp_path, monkeypatch):
    folder = _folder(tmp_path)
    f = os.path.realpath(str(folder))
    eid = background_apps.engine_id_for(f)
    env = FakeEnv(monkeypatch, _venv_python(tmp_path))
    monkeypatch.setattr(engine_host, "current", lambda e: None)
    started = []
    monkeypatch.setattr(engine_host, "ensure_background",
                        lambda *a, **k: started.append(a[0]))

    background_apps.build_env_then_respawn(f)  # False watcher
    background_apps.build_env_then_respawn(f, start_if_stopped=True)
    background_apps.build_env_then_respawn(f)  # must not downgrade
    env.finish()
    assert _wait(lambda: started)
    assert started == [eid]
    assert _wait(lambda: f not in background_apps._env_watchers)
