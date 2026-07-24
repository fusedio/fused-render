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
