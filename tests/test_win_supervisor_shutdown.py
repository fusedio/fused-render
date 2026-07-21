"""Exit-status contract for the secondary --shutdown-for-upgrade path: the
installer's ShutdownSupervisor() requires a non-zero exit whenever teardown
did not actually happen, and must never block on a dialog."""
import ctypes
import sys

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
