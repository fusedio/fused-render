"""Supervisor-side manager of the Linux window host (`_linux/windows.py`):
spawn it, wait for its socket, route opens to it, and degrade to "not
available" (so core falls back to xdg-open) without ever raising."""
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from fused_render import window_host_ipc as ipc
from fused_render.supervisor._linux import windows
from fused_render.supervisor.paths import DesktopPaths


@pytest.fixture
def paths():
    root = Path(tempfile.mkdtemp(prefix="fr"))
    p = DesktopPaths.under(root)
    p.create()
    try:
        yield p
    finally:
        shutil.rmtree(root, ignore_errors=True)


class FakeProcess:
    def __init__(self, exited=False):
        self.exited = exited

    def wait(self, ms):
        return self.exited


class FakeJob:
    instances = []

    def __init__(self):
        self.spawned = []
        self.closed = False
        self.process = FakeProcess()
        self.on_spawn = None
        FakeJob.instances.append(self)

    def spawn(self, application, arguments, environment=None, output=None):
        self.spawned.append((application, arguments, environment, output))
        if self.on_spawn:
            self.on_spawn(arguments, output)
        return self.process

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def display(monkeypatch):
    FakeJob.instances.clear()
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.delenv("FUSED_RENDER_SUPERVISOR_NO_BROWSER", raising=False)
    monkeypatch.delenv("FUSED_RENDER_NO_NATIVE_WINDOWS", raising=False)


def _serve_when_spawned(job, paths, handler=lambda c: {"ok": True}):
    stop = threading.Event()

    def spawn_hook(arguments, output):
        ipc.serve(ipc.socket_path(paths.runtime), handler, stop)

    job.on_spawn = spawn_hook
    return stop


def make(paths, **kw):
    return windows.WindowHost(paths, 8123, job_factory=FakeJob, start_timeout=2.0, **kw)


def test_spawn_command_follows_repo_conventions(paths):
    host = make(paths)
    stop = threading.Event()
    # Pre-create the server so the ping succeeds the instant it is spawned.
    ipc.serve(ipc.socket_path(paths.runtime), lambda c: {"ok": True}, stop)
    try:
        assert host.start() is True
    finally:
        stop.set()
    app, args, env, out = FakeJob.instances[0].spawned[0]
    assert Path(app).is_absolute() and str(app) == sys.executable
    assert args[:3] == ["-I", "-m", "fused_render.supervisor._linux.window_host"]
    assert args[args.index("--port") + 1] == "8123"
    assert args[args.index("--socket") + 1] == str(ipc.socket_path(paths.runtime))
    assert args[args.index("--state") + 1] == str(paths.state)
    assert "--disabled" not in args
    assert out == paths.logs / "window-host.log"


def test_start_then_open_goes_to_the_host(paths):
    seen = []
    host = make(paths)
    stop = threading.Event()
    ipc.serve(ipc.socket_path(paths.runtime), lambda c: (seen.append(c), {"ok": True})[1], stop)
    try:
        assert host.start() is True and host.available
        assert host.open("http://127.0.0.1:8123/") is True
    finally:
        stop.set()
    assert {"cmd": "open", "url": "http://127.0.0.1:8123/"} in seen


def test_preference_off_starts_the_host_disabled(paths):
    (paths.state / "prefs.json").write_text(json.dumps({"native_windows_enabled": False}))
    host = make(paths)
    stop = threading.Event()
    ipc.serve(ipc.socket_path(paths.runtime), lambda c: {"ok": True}, stop)
    try:
        host.start()
    finally:
        stop.set()
    assert "--disabled" in FakeJob.instances[0].spawned[0][1]


def test_declined_open_returns_false_so_core_uses_the_browser(paths):
    host = make(paths)
    stop = threading.Event()
    ipc.serve(ipc.socket_path(paths.runtime),
              lambda c: {"ok": True} if c["cmd"] == "ping" else {"ok": False, "reason": "disabled"},
              stop)
    try:
        host.start()
        assert host.open("http://127.0.0.1:8123/") is False
        assert host.available  # a decline is not a failure
    finally:
        stop.set()


def test_host_that_exits_early_is_unavailable_and_the_reason_is_logged_once(paths):
    logged = []
    host = make(paths)
    host.log = logged.append

    def die(arguments, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("window-host: GTK 3 / WebKit2 4.1 typelib not found\n")
        FakeJob.instances[-1].process.exited = True

    orig = FakeJob.spawn

    def spawn(self, *a, **k):
        self.on_spawn = die
        return orig(self, *a, **k)

    FakeJob.spawn = spawn
    try:
        assert host.start() is False
    finally:
        FakeJob.spawn = orig
    assert host.available is False
    assert host.open("http://127.0.0.1:8123/") is False
    assert host.open("http://127.0.0.1:8123/") is False
    assert len(logged) == 1 and "typelib" in logged[0]
    assert FakeJob.instances[0].closed


def test_socket_never_appearing_times_out_to_unavailable(paths):
    logged = []
    host = windows.WindowHost(paths, 8123, job_factory=FakeJob, start_timeout=0.3)
    host.log = logged.append
    assert host.start() is False
    assert FakeJob.instances[0].closed
    assert len(logged) == 1


def test_open_after_the_host_dies_degrades_and_logs_once(paths):
    logged = []
    host = make(paths)
    host.log = logged.append
    stop = threading.Event()
    thread = ipc.serve(ipc.socket_path(paths.runtime), lambda c: {"ok": True}, stop)
    assert host.start() is True
    stop.set()
    thread.join(3)
    assert host.open("http://127.0.0.1:8123/") is False
    assert host.open("http://127.0.0.1:8123/") is False
    assert host.available is False
    assert len(logged) == 1


@pytest.mark.parametrize("setup", ["no_display", "opt_out", "no_browser"])
def test_not_wanted_never_spawns(paths, monkeypatch, setup):
    if setup == "no_display":
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
    elif setup == "opt_out":
        monkeypatch.setenv("FUSED_RENDER_NO_NATIVE_WINDOWS", "1")
    else:
        monkeypatch.setenv("FUSED_RENDER_SUPERVISOR_NO_BROWSER", "1")
    host = make(paths)
    assert host.start() is False
    assert FakeJob.instances == [] and host.open("http://x/") is False


def test_stop_asks_the_host_to_quit_then_kills_the_tree(paths):
    seen = []
    host = make(paths)
    stop = threading.Event()
    ipc.serve(ipc.socket_path(paths.runtime), lambda c: (seen.append(c["cmd"]), {"ok": True})[1], stop)
    try:
        host.start()
        host.stop()
        host.stop()  # idempotent
    finally:
        stop.set()
    assert "quit" in seen and FakeJob.instances[0].closed
    assert host.available is False


def test_stop_before_start_is_a_noop(paths):
    make(paths).stop()


def test_server_socket_env_only_when_a_host_is_wanted(paths, monkeypatch):
    host = make(paths)
    assert host.server_environment() == {
        ipc.ENV_SOCKET: str(ipc.socket_path(paths.runtime))}
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("DISPLAY", raising=False)
    assert make(paths).server_environment() == {}
