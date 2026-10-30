"""Canvas sync observability: the per-canvas event ring, the change notifier
behind `WS /api/canvases/sync/events`, and the one-writer pid lock.

Split out of canvases.py so the watcher module keeps the sync mechanics and
this one keeps the plumbing that reports on them. Nothing here imports
canvases.py (canvases imports this), and nothing imports fused_render.server
(the server includes canvases' router — keep it acyclic, same rule as
canvases.py).

THE RING (`SyncLog`): 200 structured events per canvas — `{seq, t, leg,
action, ms, ok, detail}` — each also written as one `logger.info` line, so a
diag bundle and the page's drawer tell the same story. `seq` only grows; the
page pages with `after=<last seq it saw>`.

THE NOTIFIER: one registry keyed by CANVAS, not by manager — the key is
canvases.py's `_sync_key(env, name)` (`"<env>/<name>"`: prod's and unstable's
same-named canvases sync at once and must not wake each other's sockets). A
stop/start (or a server-side self-heal) replaces the manager under the same
key; a socket subscribed by key keeps receiving without re-subscribing, and
reads the new manager's epoch on its next wake. A wake is
`loop.call_soon_threadsafe(event.set)` — the sync threads never touch the
event loop, and the socket handler never parks a thread.

THE WRITER LOCK: `<canvases_root(env)>/.sync/<name>.lock`. Two fused-render
servers (the desktop app and a dev server, two branches) watching the same
clone each push its own view of the folder over the other's and each answer
the embed — one writer per clone. On POSIX the lock is an `flock` held on an
open fd for the claim's lifetime: the kernel releases it when the holder
dies, so a lock file left by a quit that never ran stop() (or a crash, or a
reboot with pid reuse) never refuses anyone. The file's JSON `{pid, port,
since}` only names the owner for the 409 `owned` answer and the UI. A claim
already held by this process (a manager replaced in-process) is handed over.
Windows falls back to probing the JSON's pid.
"""
from __future__ import annotations

import asyncio
import collections
import errno
import json
import logging
import os
import sys
import threading
import time
from urllib.parse import urlsplit

try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None

logger = logging.getLogger("fused_render.canvases")

LOG_RING_MAX = 200


class SyncLog:
    """Bounded, thread-safe event ring for one canvas. `name` is the label its
    log lines carry (canvases.py passes the `<env>/<name>` key)."""

    def __init__(self, name: str, notify) -> None:
        self.name = name
        self._notify = notify
        self._lock = threading.Lock()
        self._events: collections.deque = collections.deque(maxlen=LOG_RING_MAX)
        self._seq = 0

    def add(self, leg: str, action: str, *, ms: float | int | None = None,
            ok: bool = True, detail: str = "") -> dict:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "t": time.time(),
                "leg": leg,
                "action": action,
                "ms": None if ms is None else int(ms),
                "ok": bool(ok),
                "detail": detail or "",
            }
            self._events.append(event)
        logger.info("canvas %s: %s %s %s", self.name, leg, action,
                    (detail or "") + ("" if ms is None else f" ({int(ms)} ms)"))
        self._notify()
        return event

    def since(self, after: int = 0) -> dict:
        """Events with seq > `after`, oldest first, and the `after` to pass
        next time. An `after` beyond this ring's last seq came from a previous
        manager (seq restarts with a new watcher_epoch): read it as 0, so the
        page re-reads the new ring instead of waiting for it to catch up."""
        with self._lock:
            if after > self._seq or after < 0:
                after = 0
            events = [dict(e) for e in self._events if e["seq"] > after]
        return {"events": events, "next": events[-1]["seq"] if events else after}

    @property
    def last_seq(self) -> int:
        return self._seq


# -- the change notifier ------------------------------------------------------

_LISTENERS_LOCK = threading.Lock()
_LISTENERS: dict[str, set] = {}


def subscribe(key: str) -> tuple:
    """Register the CALLING event loop for wakes on canvas `key`. Returns the
    token `unsubscribe` takes; `token[1]` is the asyncio.Event to await (clear
    it after each wake)."""
    token = (asyncio.get_running_loop(), asyncio.Event())
    with _LISTENERS_LOCK:
        _LISTENERS.setdefault(key, set()).add(token)
    return token


def unsubscribe(key: str, token) -> None:
    with _LISTENERS_LOCK:
        subs = _LISTENERS.get(key)
        if subs is not None:
            subs.discard(token)
            if not subs:
                del _LISTENERS[key]


def notify(key: str) -> None:
    """Wake every socket watching canvas `key`. Safe from any thread."""
    with _LISTENERS_LOCK:
        subs = list(_LISTENERS.get(key, ()))
    for loop, event in subs:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass  # that loop closed; its handler's finally unsubscribes



# -- WebSocket origin guard ---------------------------------------------------


def ws_origin_ok(ws) -> bool:
    """server/common.py's `ws_origin_ok`, duplicated (canvases may not import
    fused_render.server — see the module docstring). Same rule: the Origin
    must be exactly this server (scheme + Host) and the Host a loopback name.
    A socket has no CORS preflight, so without this any website could read a
    canvas's file names and push errors off this feed."""
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if not origin or not host:
        return False
    hostname = urlsplit("//" + host).hostname or ""
    if hostname not in ("127.0.0.1", "localhost", "::1") and not hostname.endswith(".localhost"):
        return False
    scheme = "https" if ws.url.scheme == "wss" else "http"
    return origin.lower() == f"{scheme}://{host}".lower()


# -- the one-writer lock ------------------------------------------------------

# path -> (fd holding the flock, the claim's since). One claim per clone per
# process; a replacement manager in this process inherits the fd.
_HELD: dict[str, tuple[int, float]] = {}
_HELD_LOCK = threading.Lock()


def server_port() -> int:
    """The port this server is bound to, as server/app.py publishes it in
    FUSED_RENDER_ORIGIN before serving; 0 when unknown (a test client, an
    embedding that never set it)."""
    origin = os.environ.get("FUSED_RENDER_ORIGIN") or ""
    try:
        return int(urlsplit(origin).port or 0)
    except ValueError:
        return 0


def pid_alive(pid: int) -> bool:
    """Is `pid` running. A probe that cannot answer says alive: a wrong
    "alive" refuses a start (the user sees who owns it), a wrong "dead" puts
    two writers on one clone."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) on Windows is CTRL_C_EVENT, not a probe.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel32.CloseHandle(handle)
            return code.value == 259  # STILL_ACTIVE
        except Exception:  # noqa: BLE001
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # EPERM: alive, not ours
    return True


def card_names_this_server(card: dict | None) -> bool:
    """Does the lock's business card name THIS server's port (and not this
    pid)? Two processes cannot bind one loopback port, so a card whose port is
    ours was written by a predecessor that is gone — a quit that never released
    (the pid-probe lock on Windows), or an update's restart that reused the
    port. Such a card can never be a live peer: asking it to release would ask
    ourselves, and a pid probe that says "alive" is pid reuse. False when the
    port is unknown here (0) or the card is empty."""
    if not isinstance(card, dict):
        return False
    port = server_port()
    return port > 0 and card.get("port") == port and card.get("pid") != os.getpid()


def process_command(pid: int) -> str | None:
    """The command line (or executable path) of `pid`, or None when it cannot
    be read (no such process, no permission). POSIX asks `ps`; Windows asks
    the kernel for the image path. For deciding whether a lock holder that no
    longer answers on its port is one of OUR processes before it is signalled —
    never for anything that runs."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return None
            try:
                size = wintypes.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    return None
                return buf.value
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            return None
    import subprocess

    try:
        out = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5, close_fds=False)
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.stdout.strip()
    return line or None


LOCK_READ_RETRY_S = 0.02


def read_lock(path: str) -> dict | None:
    """The lock file's `{pid, port, since}`, or None (no file, or no valid
    record). An empty or torn read — the holder rewriting it this instant —
    is retried once after LOCK_READ_RETRY_S, so an owner is not reported as
    `{}` / pid None just for being caught mid-write."""
    for attempt in range(2):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("pid"), int):
            return data
        if attempt == 0:
            time.sleep(LOCK_READ_RETRY_S)
    return None


def claim_lock(path: str) -> tuple[str, dict]:
    """Take the clone's writer lock for this process.

    Returns `(outcome, record)`: outcome "taken" (no previous holder),
    "ours" (this process already held it — a manager replaced in-process —
    or the file names this pid), "taken_over" (record is the previous
    holder's JSON: it died without releasing), or "refused" (record is the
    live owner's JSON; nothing written).

    POSIX: the lock IS an `flock(LOCK_EX)` on an fd held open for as long as
    the claim lives (module-level `_HELD`, released by `release_lock`). The
    kernel drops it when the process dies however it dies — a packaged-app
    quit that never ran stop(), a crash, a reboot — so a stale file never
    refuses anyone, pid reuse after a reboot cannot fake an owner, and there
    is no check-then-replace race to lose. The JSON inside is only the
    owner's business card for the UI. Windows has no flock here: the pid
    probe decides, as before."""
    if _fcntl is None:
        return _claim_lock_by_pid(path)
    me = {"pid": os.getpid(), "port": server_port(), "since": time.time()}
    with _HELD_LOCK:
        held = _HELD.get(path)
        if held is not None:
            # Already this process's (the replaced manager's fd): hand it over.
            _write_fd(held[0], me)
            _HELD[path] = (held[0], me["since"])
            return "ours", me
        os.makedirs(os.path.dirname(path), exist_ok=True)
        busy = 0
        for _attempt in range(8):
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                    raise
                # A few short retries: `lock_holder`'s probe holds the lock for
                # microseconds, and must not read as an owner to a real claim.
                busy += 1
                if busy >= 3:
                    return "refused", read_lock(path) or {}
                time.sleep(0.02)
                continue
            # Locked — but the file may have been unlinked by its releasing
            # holder between our open and our flock: then we hold a lock on
            # an orphaned inode while the next claimer creates a fresh file.
            # Only a lock on the inode the path names right now counts.
            try:
                same = os.fstat(fd).st_ino == os.stat(path).st_ino
            except OSError:
                same = False
            if not same:
                os.close(fd)
                continue
            try:
                prior = _read_fd(fd)
                _write_fd(fd, me)
            except BaseException:
                # A disk-full write must not leave a LOCKED fd outside _HELD:
                # nothing would ever release it, and every later claim in
                # this process (a second fd) would read as "refused".
                os.close(fd)
                raise
            _HELD[path] = (fd, me["since"])
            if prior is None:
                return "taken", me
            if prior.get("pid") == os.getpid():
                return "ours", me
            return "taken_over", prior
    return "refused", read_lock(path) or {}


def release_lock(path: str, since: float | None) -> bool:
    """Drop the claim if it is still the one made at `since` (a newer claim
    by a replacement manager in this process is left alone). The file is
    unlinked while still locked, then the fd closed — a claimer that opened
    the old path sees the inode mismatch in claim_lock and retries."""
    if _fcntl is None:
        return _release_lock_by_pid(path, since)
    with _HELD_LOCK:
        held = _HELD.get(path)
        if held is None or (since is not None and held[1] != since):
            return False
        del _HELD[path]
        fd = held[0]
        try:
            if os.fstat(fd).st_ino == os.stat(path).st_ino:
                os.remove(path)
        except OSError:
            pass
        os.close(fd)
        return True


def lock_holder(path: str) -> dict | None:
    """The lock file's JSON when ANOTHER process holds the claim right now,
    else None (free, or ours). POSIX probes the flock itself (a stale file
    from a dead holder is free whatever pid it names); Windows probes the
    JSON's pid."""
    if _fcntl is None:
        held = read_lock(path)
        if held is None or held["pid"] == os.getpid() or not pid_alive(held["pid"]):
            return None
        return held
    with _HELD_LOCK:
        if path in _HELD:
            return None
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None  # no file, no holder
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            return read_lock(path) or {"pid": None, "port": None, "since": None}
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def release_all() -> None:
    """Close every claim this process holds (the quit rung, after the
    managers stopped — a belt for a manager whose stop() never ran)."""
    if _fcntl is None:
        return
    with _HELD_LOCK:
        paths = list(_HELD)
    for path in paths:
        release_lock(path, None)


def _read_fd(fd: int) -> dict | None:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            raw += chunk
        data = json.loads(raw.decode("utf-8")) if raw.strip() else None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("pid"), int) else None


def _write_fd(fd: int, record: dict) -> None:
    """Overwrite in place, THEN cut to length: a reader never sees the empty
    file a truncate-first write leaves for an instant (no temp + rename here:
    the flock lives on this inode). It may still catch a torn mix of old and
    new bytes — invalid JSON, which read_lock retries."""
    payload = json.dumps(record).encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    written = 0
    while written < len(payload):
        written += os.write(fd, payload[written:])
    os.ftruncate(fd, len(payload))


def _claim_lock_by_pid(path: str) -> tuple[str, dict]:
    """Windows: no flock. The JSON's pid, probed — exposed to pid reuse and
    to a takeover race, which is why POSIX does not use it."""
    me = {"pid": os.getpid(), "port": server_port(), "since": time.time()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    held = read_lock(path)
    if (held is not None and held["pid"] != os.getpid() and pid_alive(held["pid"])
            and not card_names_this_server(held)):
        return "refused", held
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(me, f)
    os.replace(tmp, path)
    if held is None:
        return "taken", me
    return ("ours", me) if held["pid"] == os.getpid() else ("taken_over", held)


def _release_lock_by_pid(path: str, since: float | None) -> bool:
    held = read_lock(path)
    if held is None or held.get("pid") != os.getpid():
        return False
    if since is not None and held.get("since") != since:
        return False
    try:
        os.remove(path)
    except OSError:
        return False
    return True
