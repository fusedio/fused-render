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
