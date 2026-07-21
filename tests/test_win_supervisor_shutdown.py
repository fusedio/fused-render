"""Exit-status contract for the secondary --shutdown-for-upgrade path: the
installer's ShutdownSupervisor() requires a non-zero exit whenever teardown
did not actually happen, and must never block on a dialog."""
import ctypes
import queue
import sys
import threading

import pytest

pytest.importorskip("win32event")

from fused_render.win_supervisor import __main__ as entry
from fused_render.win_supervisor import instance, protocol, supervisor

_NAMES = instance.InstanceNames(mutex="m", pipe="p", sid="s")


class _Secondary(instance.SecondaryInstance):
    def __init__(self, send_error=None, wait_error=None):
        super().__init__(_NAMES)
        self._send_error = send_error
        self._wait_error = wait_error
        self.waited = False

    def send(self, command, timeout):
        if self._send_error:
            raise self._send_error

    def wait_for_exit(self, timeout):
        self.waited = True
        if self._wait_error:
            raise self._wait_error


def test_rejected_shutdown_for_upgrade_raises(monkeypatch):
    # The ORIGINAL bug: CommandRejected was swallowed and run() returned
    # cleanly, reporting a shutdown that never happened.
    sec = _Secondary(send_error=instance.CommandRejected("rejected"))
    monkeypatch.setattr(instance, "acquire", lambda names: sec)
    with pytest.raises(instance.CommandRejected):
        supervisor.run(protocol.ShutdownForUpgrade())
    assert not sec.waited


def test_rejected_open_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(
        instance, "acquire",
        lambda names: _Secondary(send_error=instance.CommandRejected("rejected")),
    )
    shown = []
    monkeypatch.setattr(supervisor, "_report_open_rejected", shown.append)
    supervisor.run(protocol.Open(r"C:\missing.csv"))
    assert shown == [r"C:\missing.csv"]


def test_shutdown_wait_timeout_propagates(monkeypatch):
    sec = _Secondary(wait_error=TimeoutError("supervisor did not exit"))
    monkeypatch.setattr(instance, "acquire", lambda names: sec)
    with pytest.raises(TimeoutError):
        supervisor.run(protocol.ShutdownForUpgrade())
    assert sec.waited


class _NoPaths:
    @staticmethod
    def discover():
        raise RuntimeError("no desktop paths in tests")


def test_main_exits_nonzero_without_dialog_on_rejected_shutdown(monkeypatch):
    monkeypatch.setattr(
        instance, "acquire",
        lambda names: _Secondary(send_error=instance.CommandRejected("rejected")),
    )
    monkeypatch.setattr(entry, "DesktopPaths", _NoPaths)
    monkeypatch.setattr(sys, "argv", ["win_supervisor", "--shutdown-for-upgrade"])
    dialogs = []
    monkeypatch.setattr(
        ctypes.windll.user32, "MessageBoxW", lambda *a: dialogs.append(a) or 1
    )
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert dialogs == []  # ewWaitUntilTerminated must never block on a dialog


def test_exit_confirm_never_blocks_the_loop_thread(monkeypatch):
    # Bugbot: a modal exit-confirm on the loop thread starved pipe_requests,
    # so ShutdownForUpgrade timed out (status 1) and failed the installer.
    # The dialog must run off-thread: the spawn call returns immediately,
    # a second Exit click while the dialog is up is dropped, and the answer
    # arrives via the queue once the user responds.
    answered = threading.Event()

    def fake_messagebox(hwnd, text, caption, flags):
        answered.wait(5)
        return 6  # IDYES

    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", fake_messagebox)
    results: "queue.Queue[bool]" = queue.Queue()

    supervisor._spawn_exit_confirm(results)  # returns without blocking
    supervisor._spawn_exit_confirm(results)  # dropped: dialog already open
    assert results.empty()  # loop thread is free while the dialog is up

    answered.set()
    assert results.get(timeout=5) is True
    assert results.empty()  # the dropped second click produced no result


def test_mid_session_failure_dialog_says_stopped_not_could_not_start(monkeypatch):
    # Bugbot: a server that died AFTER a successful startup was reported as
    # "FusedRender could not start", misreporting the failure mode.
    def dying_run(command):
        raise supervisor.SupervisorStoppedError("Python server exited unexpectedly")

    monkeypatch.setattr(supervisor, "run", dying_run)
    monkeypatch.setattr(entry, "DesktopPaths", _NoPaths)
    monkeypatch.setattr(sys, "argv", ["win_supervisor"])
    dialogs = []
    monkeypatch.setattr(
        ctypes.windll.user32, "MessageBoxW", lambda *a: dialogs.append(a) or 1
    )
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    [(_, text, _, _)] = dialogs
    assert "stopped unexpectedly" in text
    assert "could not start" not in text


def test_startup_failure_dialog_still_says_could_not_start(monkeypatch):
    def failing_run(command):
        raise TimeoutError("Python server did not become ready")

    monkeypatch.setattr(supervisor, "run", failing_run)
    monkeypatch.setattr(entry, "DesktopPaths", _NoPaths)
    monkeypatch.setattr(sys, "argv", ["win_supervisor"])
    dialogs = []
    monkeypatch.setattr(
        ctypes.windll.user32, "MessageBoxW", lambda *a: dialogs.append(a) or 1
    )
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    [(_, text, _, _)] = dialogs
    assert "could not start" in text
