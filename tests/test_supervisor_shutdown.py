"""Exit-status contract for the secondary --shutdown-for-upgrade path: the
installer's ShutdownSupervisor() requires a non-zero exit whenever teardown
did not actually happen, and must never block on a dialog."""
import ctypes
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("win32event")

from fused_render.supervisor import __main__ as entry
from fused_render.supervisor import core, protocol
from fused_render.supervisor._win32 import instance

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
        core.run(protocol.ShutdownForUpgrade())
    assert not sec.waited


def test_rejected_open_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(
        instance, "acquire",
        lambda names: _Secondary(send_error=instance.CommandRejected("rejected")),
    )
    shown = []
    monkeypatch.setattr(core.ui, "report_open_rejected", shown.append)
    core.run(protocol.Open(r"C:\missing.csv"))
    assert shown == [r"C:\missing.csv"]


def test_shutdown_wait_timeout_propagates(monkeypatch):
    sec = _Secondary(wait_error=TimeoutError("supervisor did not exit"))
    monkeypatch.setattr(instance, "acquire", lambda names: sec)
    with pytest.raises(TimeoutError):
        core.run(protocol.ShutdownForUpgrade())
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
    monkeypatch.setattr(sys, "argv", ["supervisor", "--shutdown-for-upgrade"])
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

    core._spawn_exit_confirm(results)  # returns without blocking
    core._spawn_exit_confirm(results)  # dropped: dialog already open
    assert results.empty()  # loop thread is free while the dialog is up

    answered.set()
    assert results.get(timeout=5) is True
    assert results.empty()  # the dropped second click produced no result


def test_mid_session_failure_dialog_says_stopped_not_could_not_start(monkeypatch):
    # Bugbot: a server that died AFTER a successful startup was reported as
    # "FusedRender could not start", misreporting the failure mode.
    def dying_run(command):
        raise core.SupervisorStoppedError("Python server exited unexpectedly")

    monkeypatch.setattr(core, "run", dying_run)
    monkeypatch.setattr(entry, "DesktopPaths", _NoPaths)
    monkeypatch.setattr(sys, "argv", ["supervisor"])
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

    monkeypatch.setattr(core, "run", failing_run)
    monkeypatch.setattr(entry, "DesktopPaths", _NoPaths)
    monkeypatch.setattr(sys, "argv", ["supervisor"])
    dialogs = []
    monkeypatch.setattr(
        ctypes.windll.user32, "MessageBoxW", lambda *a: dialogs.append(a) or 1
    )
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    [(_, text, _, _)] = dialogs
    assert "could not start" in text


def test_start_ready_server_closes_job_when_spawn_raises(monkeypatch):
    import pywintypes

    closed = []

    class _FakeJob:
        def close(self):
            closed.append(self)

    monkeypatch.setattr(core, "Job", _FakeJob)
    monkeypatch.setattr(core, "_available_port", lambda: 12345)

    def failing_start(job, paths, port, token):
        raise pywintypes.error(5, "AssignProcessToJobObject", "access denied")

    monkeypatch.setattr(core, "_start_server", failing_start)
    with pytest.raises(pywintypes.error):
        core._start_ready_server(object(), "tok")
    assert len(closed) == 3  # every retry attempt's job was closed


class _FakePrimary:
    """Stands in for PrimaryInstance in _stop_pipe tests: records that the
    stop-event/poke path was used instead of a self-sent protocol frame."""
    def __init__(self):
        self.stop_calls = 0

    def stop_serving(self):
        self.stop_calls += 1


def test_stop_pipe_has_an_overall_deadline_under_request_flood(monkeypatch):
    # Finding: every non-shutdown request reset the 5s get() timer, so a
    # steady stream of secondary launches during teardown hung _stop_pipe
    # (and therefore job.close()) forever.
    monkeypatch.setattr(core, "_STOP_PIPE_TIMEOUT_S", 1.0)
    requests: "queue.Queue[instance.Request]" = queue.Queue()
    stop_feeding = threading.Event()
    responses = []

    def feed():
        while not stop_feeding.is_set():
            resp: "queue.Queue[int]" = queue.Queue(maxsize=1)
            responses.append(resp)
            requests.put(instance.Request(protocol.Open(r"C:\x.csv"), resp))
            time.sleep(0.05)

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    stuck_pipe_thread = threading.Thread(target=lambda: threading.Event().wait(30), daemon=True)
    stuck_pipe_thread.start()

    primary = _FakePrimary()
    started = time.monotonic()
    core._stop_pipe(primary, requests, stuck_pipe_thread)
    elapsed = time.monotonic() - started
    stop_feeding.set()

    assert elapsed < 5.0  # deadline honored despite the flood
    assert primary.stop_calls >= 1
    assert responses[0].get_nowait() == 1  # drained opens are rejected, not dropped


def test_stop_pipe_answers_genuine_concurrent_upgrade_with_zero():
    # Finding: the old self-sent ShutdownForUpgrade frame was indistinguishable
    # from a real installer request. Now nothing is self-sent through the
    # queue, so a ShutdownForUpgrade seen here IS the installer — answered 0
    # because teardown is in progress and completes right after.
    requests: "queue.Queue[instance.Request]" = queue.Queue()
    resp: "queue.Queue[int]" = queue.Queue(maxsize=1)
    answered: "queue.Queue[int]" = queue.Queue()

    def fake_pipe_thread():
        requests.put(instance.Request(protocol.ShutdownForUpgrade(), resp))
        answered.put(resp.get(timeout=10))

    t = threading.Thread(target=fake_pipe_thread, daemon=True)
    t.start()
    core._stop_pipe(_FakePrimary(), requests, t)
    assert answered.get(timeout=1) == 0
    assert not t.is_alive()


def test_forwarded_open_never_blocks_the_loop_thread(monkeypatch):
    # Bugbot: a forwarded Open ran Path.exists()/os.startfile inline on the
    # loop, so a hung UNC stat stalled pipe servicing and a concurrent
    # ShutdownForUpgrade timed out. The open must run on a worker; its pipe
    # response is put from that worker once the open finishes.
    release_open = threading.Event()
    open_started = threading.Event()

    def hung_open(port, command):
        open_started.set()
        release_open.wait(30)  # stands in for a disconnected-UNC Path.exists()
        raise OSError("host unreachable")

    monkeypatch.setattr(core, "_open_command", hung_open)

    class _Process:
        def wait(self, timeout_ms):
            return False

    class _Paths:
        @staticmethod
        def log(message):
            pass

    tray_actions: "queue.Queue" = queue.Queue()
    pipe_requests: "queue.Queue[instance.Request]" = queue.Queue()
    open_resp: "queue.Queue[int]" = queue.Queue(maxsize=1)
    upgrade_resp: "queue.Queue[int]" = queue.Queue(maxsize=1)
    result: "queue.Queue" = queue.Queue()

    loop = threading.Thread(
        target=lambda: result.put(
            core._event_loop(1, _Process(), _Paths(), tray_actions, pipe_requests)
        ),
        daemon=True,
    )
    loop.start()

    pipe_requests.put(instance.Request(protocol.Open(r"\dead-host\share\x.csv"), open_resp))
    assert open_started.wait(5)  # the open is in flight — and hung — on its worker
    pipe_requests.put(instance.Request(protocol.ShutdownForUpgrade(), upgrade_resp))

    reason, response = result.get(timeout=2)  # loop answered while the open still hangs
    assert reason is core._ExitReason.UPGRADE
    assert response is upgrade_resp
    assert open_resp.empty()  # no premature answer for the stuck open

    release_open.set()
    assert open_resp.get(timeout=5) == 1  # worker still answers the pipe when done
    loop.join(timeout=1)


def test_initial_open_never_blocks_startup(monkeypatch):
    # Bugbot: the initial Open ran _safe_open inline in run() before tray.start
    # and inst.serve, so a hung Path.exists() (disconnected UNC path) left the
    # supervisor with no tray and no pipe server — the Job-owned server alive
    # but the app undiscoverable and unable to answer ShutdownForUpgrade.
    release_open = threading.Event()
    open_started = threading.Event()

    def hung_open(port, command):
        open_started.set()
        release_open.wait(30)  # stands in for a disconnected-UNC Path.exists()
        raise OSError("host unreachable")

    monkeypatch.setattr(core, "_open_command", hung_open)

    class _Paths:
        @staticmethod
        def discover():
            return _Paths()

        def create(self):
            pass

        @staticmethod
        def log(message):
            pass

    class _Primary:
        def serve(self, requests, log):
            t = threading.Thread(target=lambda: None, daemon=True)
            t.start()
            return t

    class _Tray:
        actions: "queue.Queue" = queue.Queue()

        def set_update_available(self, version):
            pass

    monkeypatch.setattr(instance, "acquire", lambda names: _Primary())
    monkeypatch.setattr(core, "DesktopPaths", _Paths)
    monkeypatch.setattr(
        core, "_start_ready_server", lambda paths, token: (object(), object(), 1)
    )
    monkeypatch.setattr(core.startup, "enabled", lambda: False)
    monkeypatch.setattr(core.tray, "start", lambda port, enabled, paths: _Tray())
    stages = []
    monkeypatch.setattr(
        core, "_event_loop",
        lambda *a: stages.append("loop") or (core._ExitReason.TRAY_EXIT, None),
    )
    monkeypatch.setattr(core, "_teardown", lambda *a, **k: stages.append("teardown"))

    done = threading.Event()

    def run_supervisor():
        core.run(protocol.Open(r"\dead-host\share\x.csv"))
        done.set()

    threading.Thread(target=run_supervisor, daemon=True).start()
    assert done.wait(5)  # startup reached the loop while the open still hangs
    assert open_started.wait(5)  # the open really was dispatched, on its worker
    assert stages == ["loop", "teardown"]
    release_open.set()


def _payload_with(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "payload.complete").write_text("")
    return payload


def _spawn_under(payload):
    # A real, several-second process whose image lives under the payload dir.
    # ping loads only System32 DLLs, so a copy runs fine from anywhere.
    exe = shutil.copy(os.path.join(os.environ["SystemRoot"], "System32", "ping.exe"),
                      str(payload / "sleeper.exe"))
    return exe, subprocess.Popen([exe, "-n", "30", "127.0.0.1"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_wait_payload_gone_noops_without_marker(tmp_path, monkeypatch):
    # No payload.complete → a dev running --shutdown-for-upgrade from a venv;
    # the sweep must not even enumerate, let alone kill anything.
    payload = tmp_path / "payload"
    payload.mkdir()
    called = []
    monkeypatch.setattr(instance.win32process, "EnumProcesses", lambda: called.append(1) or [])
    instance._wait_payload_gone(time.monotonic() + 5, payload=str(payload))
    assert not called


def test_wait_payload_gone_terminates_process_under_payload(tmp_path):
    payload = _payload_with(tmp_path)
    _exe, proc = _spawn_under(payload)
    try:
        instance._wait_payload_gone(time.monotonic() + 10, payload=str(payload))
        assert proc.poll() is not None  # a clean sweep means it's already dead
    finally:
        if proc.poll() is None:
            proc.kill()


def test_wait_payload_gone_times_out_when_process_survives(tmp_path, monkeypatch):
    payload = _payload_with(tmp_path)
    _exe, proc = _spawn_under(payload)
    # A payload process that won't die (TerminateProcess neutered) must make the
    # sweep raise at the deadline, so --shutdown-for-upgrade exits nonzero rather
    # than let the installer swap a still-locked payload.
    monkeypatch.setattr(instance.win32process, "TerminateProcess", lambda handle, code: None)
    try:
        with pytest.raises(TimeoutError):
            instance._wait_payload_gone(time.monotonic() + 1.0, payload=str(payload))
    finally:
        proc.kill()
