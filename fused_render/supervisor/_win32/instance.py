"""Single-instance mutex + named-pipe IPC — port of
windows/supervisor/src/instance.rs (feat/windows-desktop-foundation, PR
#162).
"""
from __future__ import annotations

import queue
import struct
import threading
import time
from dataclasses import dataclass

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import winerror

from fused_render.supervisor import protocol

_PIPE_BUFFER_SIZE = 65_548
_SYNCHRONIZE = 0x0010_0000
_FILE_FLAG_FIRST_PIPE_INSTANCE = 0x0008_0000

# CreateNamedPipe retry (same resilience rule as tray.py's retry loop, at
# pipe scale: the handle-close race resolves in milliseconds, not seconds).
_RETRY_START = 0.05
_RETRY_CAP = 2.0
_GIVE_UP_AFTER_ATTEMPTS = 10


@dataclass(frozen=True)
class InstanceNames:
    mutex: str
    pipe: str
    sid: str

    @classmethod
    def current_user(cls) -> "InstanceNames":
        return cls.with_suffix("v1")

    @classmethod
    def with_suffix(cls, suffix: str) -> "InstanceNames":
        sid = _current_user_sid()
        return cls(
            mutex=rf"Local\FusedRender.Supervisor.{suffix}.{sid}",
            pipe=rf"\\.\pipe\FusedRender.Supervisor.{suffix}.{sid}",
            sid=sid,
        )


@dataclass
class Request:
    command: protocol.Command
    # Handler calls .put(status) where status is 0 (ok) or 1 (rejected).
    response: "queue.Queue[int]"


class CommandRejected(OSError):
    """The primary supervisor received our command and answered with a
    non-zero status (e.g. a forwarded Open failed) — distinct from a
    communication/timeout failure. A healthy primary IS running in this
    case; only the specific command failed, so callers must not report it
    the same way as "the app could not start"."""


class SecondaryInstance:
    def __init__(self, names: InstanceNames):
        self.names = names

    def send(self, command: protocol.Command, timeout: float) -> None:
        frame = protocol.encode(command)
        deadline = time.monotonic() + timeout
        while True:
            try:
                data = win32pipe.CallNamedPipe(self.names.pipe, frame, 4, 250)
                if len(data) == 4:
                    (status,) = struct.unpack("<I", data)
                    if status == 0:
                        return
                    raise CommandRejected("supervisor rejected the command")
            except pywintypes.error:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("supervisor did not respond")
            time.sleep(0.05)

    def wait_for_exit(self, timeout: float) -> None:
        try:
            mutex = win32event.OpenMutex(_SYNCHRONIZE, False, self.names.mutex)
        except pywintypes.error:
            return  # primary already gone
        try:
            result = win32event.WaitForSingleObject(mutex, int(timeout * 1000))
            # 0x80 = WAIT_ABANDONED (raw value; not on every pywin32 build).
            # Abandoned is normal here: the primary's upgrade teardown exits
            # without release(), so Windows marks the mutex abandoned.
            if result in (win32event.WAIT_OBJECT_0, 0x80):
                try:
                    win32event.ReleaseMutex(mutex)
                except pywintypes.error:
                    pass
                return
            raise TimeoutError("supervisor did not exit")
        finally:
            mutex.Close()


class PrimaryInstance:
    def __init__(self, mutex, names: InstanceNames):
        self._mutex = mutex
        self.names = names
        self._stop = threading.Event()

    def serve(self, requests: "queue.Queue[Request]", log=None) -> threading.Thread:
        thread = threading.Thread(
            target=_serve_pipe, args=(self.names, requests, self._stop, log), daemon=True
        )
        thread.start()
        return thread

    def stop_serving(self) -> None:
        """Idempotent stop for the pipe thread: set the stop flag, then poke
        the pipe with a single non-protocol byte to unblock a parked
        ConnectNamedPipe. Deliberately not a ShutdownForUpgrade frame, so a
        real installer request stays distinguishable from this self-unblock
        (the poke is answered status 1 and never reaches the queue)."""
        self._stop.set()
        try:
            win32pipe.CallNamedPipe(self.names.pipe, b"\x00", 4, 250)
        except pywintypes.error:
            pass

    def release(self) -> None:
        try:
            win32event.ReleaseMutex(self._mutex)
        except pywintypes.error:
            pass


def acquire(names: InstanceNames) -> PrimaryInstance | SecondaryInstance:
    sa = _security_attributes(names.sid)
    mutex = win32event.CreateMutex(sa, True, names.mutex)
    already_exists = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    if already_exists:
        # Close explicitly, not via GC: the named mutex lives as long as any
        # handle does, so a lingering secondary handle would keep it alive
        # past the primary's exit and make a later launch a secondary too.
        mutex.Close()
        return SecondaryInstance(names)
    return PrimaryInstance(mutex, names)


def _serve_pipe(
    names: InstanceNames,
    requests: "queue.Queue[Request]",
    stop: threading.Event,
    log=None,
) -> None:
    sa = _security_attributes(names.sid)
    delay = _RETRY_START
    failures = 0
    while not stop.is_set():
        try:
            handle = win32pipe.CreateNamedPipe(
                names.pipe,
                win32pipe.PIPE_ACCESS_DUPLEX | _FILE_FLAG_FIRST_PIPE_INSTANCE,
                win32pipe.PIPE_TYPE_MESSAGE
                | win32pipe.PIPE_READMODE_MESSAGE
                | win32pipe.PIPE_WAIT
                | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
                1,
                4,
                _PIPE_BUFFER_SIZE,
                0,
                sa,
            )
        except pywintypes.error as error:
            # FIRST_PIPE_INSTANCE races a just-disconnected client that still
            # holds its handle (transient ERROR_ACCESS_DENIED). Retry with
            # backoff; give up loudly only if it never clears.
            failures += 1
            if failures >= _GIVE_UP_AFTER_ATTEMPTS:
                if log is not None:
                    log(f"pipe server giving up after {failures} CreateNamedPipe failures: {error}")
                return
            if log is not None and failures == 1:
                log(f"CreateNamedPipe failed, retrying: {error}")
            if stop.wait(delay):
                return
            delay = min(delay * 2, _RETRY_CAP)
            continue
        failures = 0
        delay = _RETRY_START
        try:
            try:
                try:
                    win32pipe.ConnectNamedPipe(handle, None)
                except pywintypes.error as e:
                    if e.winerror != winerror.ERROR_PIPE_CONNECTED:
                        raise

                # NOWAIT + poll (not a blocking ReadFile) bounds a slow/hung
                # client to a 5s deadline instead of stalling the accept loop.
                win32pipe.SetNamedPipeHandleState(
                    handle, win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_NOWAIT, None, None
                )
                deadline = time.monotonic() + 5.0
                frame = b""
                while True:
                    try:
                        _, frame = win32file.ReadFile(handle, _PIPE_BUFFER_SIZE)
                        break
                    except pywintypes.error as e:
                        if e.winerror != winerror.ERROR_NO_DATA:
                            frame = b""
                            break
                    if time.monotonic() >= deadline:
                        frame = b""
                        break
                    time.sleep(0.025)

                command = None
                if frame:
                    try:
                        command = protocol.decode(frame)
                    except protocol.ProtocolError:
                        command = None

                should_stop = isinstance(command, protocol.ShutdownForUpgrade)
                if command is not None:
                    response: "queue.Queue[int]" = queue.Queue(maxsize=1)
                    requests.put(Request(command, response))
                    try:
                        status = response.get(timeout=20)
                    except queue.Empty:
                        status = 1
                else:
                    status = 1

                try:
                    win32file.WriteFile(handle, struct.pack("<I", status))
                except pywintypes.error:
                    pass
                win32pipe.DisconnectNamedPipe(handle)
                if should_stop:
                    stop.set()
                    return
            except Exception as error:  # noqa: BLE001 - one broken client must not kill IPC
                # No caller to re-raise to: an uncaught exception here would
                # silently kill IPC for the rest of the session.
                if log is not None:
                    log(f"pipe client handling failed: {error}")
        finally:
            win32file.CloseHandle(handle)


def _security_attributes(sid: str):
    sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})"
    sd = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        sddl, win32security.SDDL_REVISION_1
    )
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    sa.bInheritHandle = False
    return sa


def _current_user_sid() -> str:
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid, _attrs = win32security.GetTokenInformation(token, win32security.TokenUser)
    return win32security.ConvertSidToStringSid(sid)
