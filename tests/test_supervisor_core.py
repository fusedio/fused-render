"""Platform-neutral supervisor loop contract: tray actions dispatched via
`_safe_call` must run OFF the main loop, never on the thread that services
pipe_requests.

This imports `fused_render.supervisor.core` directly (no win32 gate): the
`_spawn_call`/`_safe_call` helpers under test are platform-neutral, so on Linux
CI — where a supervisor backend exists — core imports cleanly and these tests
run. Where no backend exists for the running OS (e.g. darwin) the import raises
at module load; we skip rather than ERROR at collection, matching the sibling
convention in tests/test_supervisor_linux_instance.py.
"""
import queue
import threading
from pathlib import Path

import pytest

try:
    from fused_render.supervisor import core, protocol
except Exception:  # noqa: BLE001 - no supervisor backend on this OS (e.g. darwin)
    core = None
    protocol = None

pytestmark = pytest.mark.skipif(core is None, reason="no supervisor backend on this OS")


class _Paths:
    """Minimal stand-in for DesktopPaths: `_safe_call` only ever touches
    `.log(msg)`."""

    def __init__(self):
        self.messages = []

    def log(self, message):
        self.messages.append(message)


def test_spawn_call_never_blocks_the_loop_thread():
    # Bugbot: OPEN_LOGS / DEFAULT_APPS called `_safe_call` DIRECTLY on the loop
    # thread. On Linux, `ui.open_path` -> `_xdg_open` now waits up to 5s on the
    # child, so a slow/foreground xdg-open blocked the loop that services
    # pipe_requests — a concurrent ShutdownForUpgrade could then time out inside
    # the IPC server's 20s window. `_spawn_call` must return immediately and run
    # the action off-loop, exactly like `_spawn_exit_confirm`.
    started = threading.Event()
    release = threading.Event()
    ran = queue.Queue()

    def blocking_action():
        started.set()
        release.wait(5)
        ran.put(True)

    paths = _Paths()
    core._spawn_call(paths, blocking_action)  # must return without blocking

    assert started.wait(5)  # the action began on its own thread
    assert ran.empty()      # ...but the loop thread is free while it blocks

    release.set()
    assert ran.get(timeout=5) is True


# ---- deep-link / file routing through _open_command + _absolute_command ------


def test_open_command_routes_deep_link_to_clone(monkeypatch):
    opened = []
    monkeypatch.setattr(core, "_open_browser", opened.append)
    core._open_command(9000, protocol.Open("fused-render://open?git=https://github.com/o/r"))
    assert opened == [
        "http://127.0.0.1:9000/clone?src="
        "fused-render%3A%2F%2Fopen%3Fgit%3Dhttps%3A%2F%2Fgithub.com%2Fo%2Fr"
    ]


def test_open_command_routes_file_uri_to_view(monkeypatch, tmp_path):
    f = tmp_path / "a.parquet"
    f.write_text("x")
    opened = []
    monkeypatch.setattr(core, "_open_browser", opened.append)
    # Path.as_uri() — not f"file://{f}" — builds a well-formed file URI on every
    # platform: on Windows the drive path becomes file:///C:/... (three slashes),
    # whereas f"file://{f}" would read C:\... as the netloc and be rejected as a
    # remote host. This mirrors what the OS actually hands the app (macOS
    # openURLs:, RFC 8089 §2).
    core._open_command(9000, protocol.Open(f.as_uri()))
    assert opened == [f"http://127.0.0.1:9000" + _view_path(str(f))]


def test_open_command_routes_plain_file_to_view(monkeypatch, tmp_path):
    f = tmp_path / "report.parquet"
    f.write_text("x")
    opened = []
    monkeypatch.setattr(core, "_open_browser", opened.append)
    core._open_command(9000, protocol.Open(str(f)))
    assert opened == [f"http://127.0.0.1:9000" + _view_path(str(f))]


def test_open_command_missing_file_still_errors(monkeypatch):
    monkeypatch.setattr(core, "_open_browser", lambda url: None)
    with pytest.raises(FileNotFoundError):
        core._open_command(9000, protocol.Open("/nope/does/not/exist.parquet"))


def test_absolute_command_leaves_urls_untouched():
    for raw in (
        "fused-render://open?git=https://github.com/o/r",
        "file:///home/u/a.parquet",
        "https://example.com/x",
    ):
        cmd = protocol.Open(raw)
        assert core._absolute_command(cmd) is cmd or core._absolute_command(cmd) == cmd
        assert core._absolute_command(cmd).path == raw


def test_absolute_command_resolves_relative_plain_path():
    cmd = protocol.Open("some/rel/path.parquet")
    resolved = core._absolute_command(cmd)
    assert resolved.path == str(Path.cwd() / "some/rel/path.parquet")


def _view_path(fs_path: str) -> str:
    from fused_render._view_url_codec import view_url_path

    return view_url_path(fs_path)


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_available_port_prefers_branch_base_then_next(monkeypatch):
    # The desktop server binds the branch base (1777 for a shipped build) so its
    # origin — and the browser tabs / per-origin localStorage keyed to it —
    # survives a restart, instead of the old ephemeral :0 that moved every launch.
    import socket

    base = _free_port()
    monkeypatch.setattr(core, "branch_port", lambda: base)
    assert core._available_port() == base  # base free -> reuse it
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", base))
        taken.listen()  # a live listener answers the connect probe -> in use
        assert core._available_port() == base + 1  # base taken -> next in range


# ---- tray UNINSTALL: confirm -> deintegrate -> TRAY_EXIT ----------------------


class _FakeProcess:
    """Stand-in for the supervised server process: `wait(0)` reports "still
    running" (0) until `die_after` loop polls have passed, then "exited" (1) so
    the run loop terminates deterministically in a declined-uninstall test."""

    def __init__(self, die_after=None):
        self.calls = 0
        self.die_after = die_after

    def wait(self, timeout):
        self.calls += 1
        if self.die_after is not None and self.calls > self.die_after:
            return 1  # truthy: the server exited
        return 0  # falsy: still running


def test_uninstall_confirmed_deintegrates_and_exits(monkeypatch):
    monkeypatch.setattr(core.ui, "confirm_uninstall", lambda: True)
    deintegrated = []
    monkeypatch.setattr(core, "deintegrate", lambda paths: deintegrated.append(paths))

    paths = _Paths()
    tray_actions = queue.Queue()
    tray_actions.put(core.tray.TrayAction.UNINSTALL)
    process = _FakeProcess()  # never dies on its own

    reason, upgrade = core._event_loop(
        9000, process, paths, tray_actions, queue.Queue()
    )

    assert reason is core._ExitReason.TRAY_EXIT
    assert upgrade is None
    assert deintegrated == [paths]  # the backend hook ran, with paths


def test_uninstall_declined_neither_deintegrates_nor_exits(monkeypatch):
    monkeypatch.setattr(core.ui, "confirm_uninstall", lambda: False)
    deintegrated = []
    monkeypatch.setattr(core, "deintegrate", lambda paths: deintegrated.append(paths))

    paths = _Paths()
    tray_actions = queue.Queue()
    tray_actions.put(core.tray.TrayAction.UNINSTALL)
    process = _FakeProcess(die_after=6)  # loop continues, then the server exits

    reason, _upgrade = core._event_loop(
        9000, process, paths, tray_actions, queue.Queue()
    )

    # A declined uninstall must NOT deintegrate and must NOT be a TRAY_EXIT: the
    # loop resumed and only ended because the fake server later died.
    assert deintegrated == []
    assert reason is core._ExitReason.SERVER_DIED


def test_uninstall_confirmed_exits_cleanly_without_deintegrate_hook(monkeypatch):
    # On a backend with no deintegrate hook, a confirmed uninstall still tears
    # down cleanly (guarded call) rather than raising AttributeError.
    monkeypatch.setattr(core.ui, "confirm_uninstall", lambda: True)
    monkeypatch.setattr(core, "deintegrate", None)

    paths = _Paths()
    tray_actions = queue.Queue()
    tray_actions.put(core.tray.TrayAction.UNINSTALL)

    reason, _upgrade = core._event_loop(
        9000, _FakeProcess(), paths, tray_actions, queue.Queue()
    )

    assert reason is core._ExitReason.TRAY_EXIT


def test_uninstall_and_exit_both_confirmed_still_deintegrates(monkeypatch):
    # Bugbot regression: with BOTH the Exit and Uninstall dialogs confirmed,
    # the confirmed uninstall must still deintegrate. Uninstall is the superset
    # (it must clean up first) and is polled before exit, so a plain exit can't
    # win the race and silently skip the cleanup the user asked for.
    monkeypatch.setattr(core.ui, "confirm_exit", lambda: True)
    monkeypatch.setattr(core.ui, "confirm_uninstall", lambda: True)
    deintegrated = []
    monkeypatch.setattr(core, "deintegrate", lambda paths: deintegrated.append(paths))

    paths = _Paths()
    tray_actions = queue.Queue()
    tray_actions.put(core.tray.TrayAction.EXIT)
    tray_actions.put(core.tray.TrayAction.UNINSTALL)
    process = _FakeProcess()  # never dies on its own

    reason, _upgrade = core._event_loop(
        9000, process, paths, tray_actions, queue.Queue()
    )

    assert reason is core._ExitReason.TRAY_EXIT
    assert deintegrated == [paths]  # cleanup ran despite exit also being confirmed


# ---- fused-render://relaunch (Task 5, Linux) ----------------------------------


class _FakeStartupWithAppImage:
    """Stand-in for the Linux `startup` module: has `appimage_path()`, the
    capability probe both `_open_command` and `_respawn_after_relaunch` use to
    tell the Linux backend (has the hook) from win32 (doesn't)."""

    def __init__(self, path="/opt/FusedRender/FusedRender.AppImage"):
        self._path = path

    def appimage_path(self):
        return self._path


class _FakeStartupNoAppImage:
    """Stand-in for the win32 `startup` module: `enabled()`/`set_enabled()`
    only, no `appimage_path` attribute at all — matching the real win32
    backend, which never had a reason to grow one."""

    def enabled(self):
        return False


def test_open_command_signals_relaunch_on_linux_backend(monkeypatch):
    # The Linux capability probe (appimage_path present) + a relaunch queue:
    # a bare fused-render://relaunch link puts on the queue and opens no tab,
    # instead of tearing the process down from this call's own thread.
    monkeypatch.setattr(core, "startup", _FakeStartupWithAppImage())
    opened = []
    monkeypatch.setattr(core, "_open_browser", opened.append)
    relaunch = queue.Queue()

    core._open_command(9000, protocol.Open("fused-render://relaunch"), relaunch)

    assert relaunch.get_nowait() is None
    assert opened == []


def test_open_command_relaunch_is_noop_without_appimage_path_hook(monkeypatch):
    # No appimage_path hook (e.g. the win32 backend): a relaunch link
    # degrades to exactly the same no-tab no-op as fused-render://launch —
    # the queue is never signalled, so run() never attempts a respawn.
    monkeypatch.setattr(core, "startup", _FakeStartupNoAppImage())
    opened = []
    monkeypatch.setattr(core, "_open_browser", opened.append)
    relaunch = queue.Queue()

    core._open_command(9000, protocol.Open("fused-render://relaunch"), relaunch)

    assert relaunch.empty()
    assert opened == []


def test_open_command_leaves_fda_relaunch_untouched(monkeypatch):
    # ?reason=fda (is_fda_relaunch_url) is a DIFFERENT, disjoint URL form from
    # the bare relaunch link (is_relaunch_url) — it must never signal the
    # queue, matching "leave ?reason=fda alone" (it falls through to the
    # is_launch_url branch below it, same as before Task 5).
    monkeypatch.setattr(core, "startup", _FakeStartupWithAppImage())
    opened = []
    monkeypatch.setattr(core, "_open_browser", opened.append)
    relaunch = queue.Queue()

    core._open_command(
        9000, protocol.Open("fused-render://relaunch?reason=fda"), relaunch
    )

    assert relaunch.empty()


def test_event_loop_returns_relaunch_reason_for_forwarded_relaunch_link(monkeypatch):
    # A relaunch link forwarded over the pipe (the realistic path: a
    # secondary instance relays the OS-delivered deep link to the primary) —
    # the loop must return _ExitReason.RELAUNCH once _open_command's worker
    # thread signals the queue it owns, without teardown running from that
    # worker thread.
    monkeypatch.setattr(core, "startup", _FakeStartupWithAppImage())
    monkeypatch.setattr(core, "_open_browser", lambda url: None)

    paths = _Paths()
    tray_actions = queue.Queue()
    pipe_requests = queue.Queue()
    response = queue.Queue()
    pipe_requests.put(
        core.instance.Request(protocol.Open("fused-render://relaunch"), response)
    )

    reason, upgrade = core._event_loop(
        9000, _FakeProcess(), paths, tray_actions, pipe_requests
    )

    assert reason is core._ExitReason.RELAUNCH
    assert upgrade is None


def test_respawn_after_relaunch_spawns_the_appimage(monkeypatch):
    monkeypatch.setattr(core, "startup", _FakeStartupWithAppImage("/x/FusedRender.AppImage"))
    spawned = []
    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))

    core._respawn_after_relaunch(_Paths())

    assert len(spawned) == 1
    args, kwargs = spawned[0]
    assert args == (["/x/FusedRender.AppImage"],)
    assert kwargs == {"start_new_session": True, "close_fds": True}


def test_respawn_after_relaunch_exits_quietly_without_an_appimage(monkeypatch):
    # Not running from an AppImage at all (an unpackaged dev supervisor) —
    # appimage_path() itself returns None: nothing to Popen, no error either.
    monkeypatch.setattr(core, "startup", _FakeStartupWithAppImage(path=None))
    spawned = []
    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))

    core._respawn_after_relaunch(_Paths())  # must not raise

    assert spawned == []


def test_respawn_after_relaunch_exits_quietly_without_the_hook(monkeypatch):
    # No appimage_path hook at all (e.g. the win32 backend) — same "nothing to
    # respawn, just exit" outcome as the no-AppImage case above.
    monkeypatch.setattr(core, "startup", _FakeStartupNoAppImage())
    spawned = []
    monkeypatch.setattr(core.subprocess, "Popen", lambda *a, **kw: spawned.append((a, kw)))

    core._respawn_after_relaunch(_Paths())  # must not raise

    assert spawned == []


# ---- run(): the relaunch respawn must not race the election flock -----------


class _FakePrimaryInstance:
    """Stand-in for `instance.PrimaryInstance`: never a SecondaryInstance (so
    `run()` takes the primary branch), records `release()` calls so tests can
    assert it happens BEFORE the respawn's Popen."""

    def __init__(self, calls):
        self._calls = calls

    def serve(self, requests, log=None):
        return threading.Thread(target=lambda: None)

    def release(self):
        self._calls.append("release")


class _FakeRunPaths:
    @classmethod
    def discover(cls) -> "_FakeRunPaths":
        return cls()

    def create(self) -> None:
        pass

    def log(self, message) -> None:
        pass


def _patch_run_up_to_the_event_loop(monkeypatch, calls, *, teardown):
    """Stub out every collaborator `run()` touches before and after
    `_event_loop` so the primary-instance branch runs for real, down to the
    finally block under test, without spawning a real server, tray or pipe."""
    inst = _FakePrimaryInstance(calls)
    monkeypatch.setattr(core.instance, "acquire", lambda names: inst)
    monkeypatch.setattr(core, "DesktopPaths", _FakeRunPaths)
    monkeypatch.setattr(core, "_spawn_desktop_integration", lambda paths: None)
    monkeypatch.setattr(core, "_start_ready_server",
                        lambda paths, token: (object(), object(), 9000))
    monkeypatch.setattr(core, "_spawn_open", lambda *a, **kw: None)
    monkeypatch.setattr(core.startup, "enabled", lambda: False)

    class _FakeTrayHandle:
        actions = queue.Queue()

        def stop(self) -> None:
            pass

    monkeypatch.setattr(core.tray, "start", lambda *a, **kw: _FakeTrayHandle())
    monkeypatch.setattr(core, "_event_loop",
                        lambda *a, **kw: (core._ExitReason.RELAUNCH, None))
    monkeypatch.setattr(core, "_teardown", teardown)
    monkeypatch.setattr(core, "_respawn_after_relaunch",
                        lambda paths: calls.append("respawn"))
    return inst


def test_relaunch_releases_the_lock_before_respawning(monkeypatch):
    # `PrimaryInstance.release()` is otherwise only called from the early
    # ShutdownForUpgrade branch — without releasing it here too, the flock
    # stays held for as long as this interpreter takes to unwind, and the
    # freshly-spawned AppImage can lose the race for it (see the module's
    # `_respawn_after_relaunch`/`run()` for the full story). The lock must
    # come off BEFORE the respawn is attempted, not after.
    calls = []
    _patch_run_up_to_the_event_loop(monkeypatch, calls, teardown=lambda *a, **kw: None)

    core.run(protocol.Open("fused-render://relaunch"))

    assert calls == ["release", "respawn"]


def test_relaunch_still_respawns_when_teardown_raises_supervisor_stopped_error(monkeypatch):
    # By the time `_teardown` can raise SupervisorStoppedError (the process
    # tree would not stop), the tray, pipe and graceful-shutdown request have
    # already run — only the child failing to actually exit is left. A
    # relaunch must still bring the new AppImage up rather than quitting with
    # nothing to show for it; the error still propagates afterwards, exactly
    # as it would for any other exit reason.
    calls = []

    def teardown(*a, **kw):
        raise core.SupervisorStoppedError("Python process tree did not stop")

    _patch_run_up_to_the_event_loop(monkeypatch, calls, teardown=teardown)

    with pytest.raises(core.SupervisorStoppedError):
        core.run(protocol.Open("fused-render://relaunch"))

    assert calls == ["release", "respawn"]
