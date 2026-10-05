"""Wire protocol between the native-window host and whoever asks it for a window.

Linux only in practice (the host is `supervisor/_linux/window_host.py`, a GTK +
WebKitGTK process), but this module is stdlib-only and platform-neutral so the
three parties that talk to it — the supervisor, the server (`linux_windows.py`),
and the host itself — share one definition, and so it runs under pytest on any
OS with AF_UNIX.

One request per connection: a single JSON line in, a single JSON line out.

    {"cmd": "ping"}                          -> {"ok": true}
    {"cmd": "open", "url": "...",
     "activation_token": "..."}              -> {"ok": true} | {"ok": false, "reason": "..."}
    {"cmd": "set_enabled", "on": true}       -> {"ok": true}
    {"cmd": "quit"}                          -> {"ok": true}

``activation_token`` is optional (an XDG activation token / startup id the
supervisor forwards from its own environment, e.g. a launcher or deep-link
activation); its absence is the old behavior — the window still opens, just
without a request to be raised ahead of focus-stealing prevention.

``ok: false`` is an answer, not an error: the host is up but declines (windows
switched off, nothing to show), and the caller falls back to a browser tab. A
host that cannot be reached at all raises `HostUnavailable`, which callers treat
the same way. Either way the app is never left with no way to show its UI.
"""
from __future__ import annotations

import errno
import json
import os
import select
import socket
import threading
import time
from pathlib import Path

#: The supervisor sets this in the SERVER's environment; the server installs its
#: native hooks only when it is set, so `fused-render serve` and every other
#: platform are untouched.
ENV_SOCKET = "FUSED_RENDER_WINDOW_HOST_SOCKET"

#: The supervisor sets this (to "1") when a host's dependencies and display are
#: present, regardless of whether the preference that would start it is on.
#: The server's `usable` hook trusts this over a failed ping, so a Preferences
#: page restarted with the pref off still shows the switch instead of hiding
#: it because nothing answers.
ENV_LAUNCHABLE = "FUSED_RENDER_WINDOW_HOST_LAUNCHABLE"

SOCKET_NAME = "window-host.sock"
MAX_LINE = 64 * 1024
_CLIENT_DEADLINE_S = 5.0
_SELECT_TICK_S = 0.25

#: How long every caller waits for one `request()` to answer. Kept above the
#: host's main-thread budget (`window_host._MAIN_DEADLINE_S`) so a caller never
#: gives up before the host has answered.
CALLER_TIMEOUT_S = 5.0

#: accept() errno values that mean "this one connection attempt failed, the
#: listener is still fine" (an exhausted fd/memory limit, a client that reset
#: before accept() completed, a caught signal) — never the reason to stop
#: answering the rest of the session.
_TRANSIENT_ACCEPT_ERRNOS = frozenset({
    errno.EMFILE, errno.ENFILE, errno.ECONNABORTED, errno.EINTR,
    errno.ENOBUFS, errno.ENOMEM,
})
_TRANSIENT_ACCEPT_BACKOFF_S = 0.1


def _is_transient_accept_error(error: OSError) -> bool:
    return error.errno in _TRANSIENT_ACCEPT_ERRNOS


class HostUnavailable(OSError):
    """The window host could not be reached or did not answer in time."""


def socket_path(runtime_dir: Path) -> Path:
    return Path(runtime_dir) / SOCKET_NAME


def request(path, payload: dict, timeout: float = 2.0) -> dict:
    """Send one command and return the decoded reply. Raises `HostUnavailable`
    for anything that is not a well-formed answer (no socket, refused,
    timeout, bad JSON) — the caller never has to know which."""
    data = json.dumps(payload).encode("utf-8") + b"\n"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect(os.fspath(path))
        sock.sendall(data)
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_LINE:
                raise HostUnavailable("window host reply too large")
        reply = json.loads(bytes(buf).split(b"\n", 1)[0] or b"null")
    except HostUnavailable:
        raise
    except (OSError, ValueError) as error:  # timeout is an OSError subclass
        raise HostUnavailable(str(error)) from error
    finally:
        sock.close()
    if not isinstance(reply, dict):
        raise HostUnavailable("window host sent a malformed reply")
    return reply


def ping(path, timeout: float = 0.3) -> bool:
    try:
        return bool(request(path, {"cmd": "ping"}, timeout).get("ok"))
    except HostUnavailable:
        return False


def serve(path, handler, stop: threading.Event, log=None, on_listening=None) -> threading.Thread:
    """Listen on ``path`` until ``stop`` is set, answering each connection with
    ``handler(command_dict) -> reply_dict`` on a thread per connection, so a
    ``ping`` gets through while an ``open`` or ``set_enabled`` waits on the GTK
    main thread. Window state stays single-threaded: handlers that touch a
    window go through ``backend.run_on_main``. A handler that raises, or a client that sends junk or stalls, yields an
    ``ok: false`` reply for that client only — never a dead accept loop. The
    socket is created 0600 (the runtime dir is 0700 already; this is belt and
    braces).

    ``on_listening``, if given, is called synchronously in THIS (the caller's)
    thread after ``listener.listen()`` but before the accept-loop thread
    starts — so a caller that reads its own state (e.g. a preference from
    disk) there is guaranteed to finish before any connection is accepted and
    dispatched to ``handler``. A connection that arrives while it runs simply
    queues in the listen backlog; nothing is dropped, and no command jumps
    ahead of that read."""
    path = os.fspath(path)
    try:
        os.unlink(path)  # a crashed predecessor's leftover; bind would EADDRINUSE
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o600)
    listener.listen(8)
    listener.setblocking(False)
    if on_listening is not None:
        on_listening()

    def loop() -> None:
        warned_transient = False
        try:
            while not stop.is_set():
                ready, _, _ = select.select([listener], [], [], _SELECT_TICK_S)
                if not ready:
                    continue
                try:
                    client, _addr = listener.accept()
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError as error:
                    if not _is_transient_accept_error(error):
                        break  # the listener itself is gone (EBADF/EINVAL)
                    if not warned_transient and log is not None:
                        warned_transient = True
                        log(f"window host accept() failed, retrying: {error}")
                    time.sleep(_TRANSIENT_ACCEPT_BACKOFF_S)
                    continue
                threading.Thread(target=_serve_and_close, args=(client, handler, log),
                                 daemon=True, name="fused-render-window-host-client").start()
        finally:
            listener.close()
            try:
                os.unlink(path)
            except OSError:
                pass

    thread = threading.Thread(target=loop, daemon=True, name="fused-render-window-host-ipc")
    thread.start()
    return thread


def _serve_and_close(client: socket.socket, handler, log) -> None:
    try:
        _serve_client(client, handler, log)
    finally:
        client.close()


def _serve_client(client: socket.socket, handler, log) -> None:
    deadline = time.monotonic() + _CLIENT_DEADLINE_S
    buf = bytearray()
    try:
        while b"\n" not in buf and len(buf) <= MAX_LINE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            client.settimeout(remaining)
            chunk = client.recv(4096)
            if not chunk:
                break
            buf += chunk
        if len(buf) > MAX_LINE:
            reply = {"ok": False, "reason": "request too large"}
        else:
            reply = _answer(bytes(buf).split(b"\n", 1)[0], handler, log)
        client.settimeout(_CLIENT_DEADLINE_S)
        client.sendall(json.dumps(reply).encode("utf-8") + b"\n")
    except OSError:
        return  # the client went away; nothing to answer


def _answer(line: bytes, handler, log) -> dict:
    try:
        command = json.loads(line or b"null")
    except ValueError:
        return {"ok": False, "reason": "malformed request"}
    if not isinstance(command, dict):
        return {"ok": False, "reason": "malformed request"}
    try:
        reply = handler(command)
    except Exception as error:  # noqa: BLE001 - one bad command must not end the loop
        if log is not None:
            log(f"window host command {command.get('cmd')!r} failed: {error}")
        return {"ok": False, "reason": f"{type(error).__name__}: {error}"}
    return reply if isinstance(reply, dict) else {"ok": bool(reply)}
