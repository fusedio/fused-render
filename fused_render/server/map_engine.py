"""The map template's tile daemon, owned by this server (docs/MAP_ENGINE_SERVER_DESIGN.md).

One supervisor in the :1777 process holding exactly one templates/map/daemon.py
child. The child binds :0 and publishes {port, token, pid, version} to a status
file — binding in the child and reporting back is the only version with no
race — and the supervisor keeps the Popen so it can reap via poll() (never
os.kill(pid, 0), which kills the process on Windows), replace a wedged child,
and kill the whole tree from the app's shutdown event. The browser never sees
the port or token: routers/map_tiles.py proxies everything through the stable
server origin.

The interpreter is the caller's, not this process's. map_render.py runs inside
the map template's project venv (PY-16/D276) — the only interpreter on the
machine holding the geo stack — so it hands over its own sys.executable via
POST /api/map/ensure. The handoff is validated rather than trusted: the python
must live in the home venv store and the daemon inside a known templates root.

A restarted child starts with an empty source registry, so the descriptors the
pages hold would all 404. `remember()` keeps the describe request that produced
each source id, and a restart replays them into the fresh child before any tile
is retried — that replay is what makes a daemon death invisible to the page.

Map-specific for now, but the supervisor half (spawn/status-poll/reap/kill/
restart) is the liftable seam for geotiff/zarr_aoi/netcdf when they follow.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import quote

logger = logging.getLogger(__name__)

BOOTSTRAP_TIMEOUT_S = 120.0
BOOTSTRAP_POLL_S = 0.25
PING_TIMEOUT_S = 2.0
#: How many describe requests a restart will replay. Bounded so a long session
#: scrubbing through hundreds of timesteps cannot turn one restart into an
#: unbounded re-describe storm; the page re-describes anything evicted.
REMEMBER_LIMIT = 64


class MapEngineError(RuntimeError):
    """Something the caller can be told verbatim."""


@dataclass
class Child:
    python: str
    daemon: str
    cache: str
    version: str
    #: Unique per spawn so two bring-ups never share a status file (the same
    #: overlap the AI workers hit — see ai/supervisor.Worker.uid).
    uid: str = field(default_factory=lambda: secrets.token_hex(4))
    port: int = 0
    token: str = ""
    pid: int = 0
    proc: subprocess.Popen | None = field(default=None, repr=False)


#: Guards the _child pointer and the _described registry only — both fast, and
#: never held across the blocking spawn/ping/replay I/O below, so remember() and
#: current() never wait on a cold start.
_lock = threading.Lock()
#: Serializes bring-up so two callers can never spawn two children; the only
#: lock held across network I/O, and only ensure()/restart() contend on it.
_spawn_lock = threading.Lock()
_child: Child | None = None
#: source_id -> the describe request that registered it, in insertion order.
_described: dict[str, dict] = {}

SPAWN_KWARGS = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt" else {"start_new_session": True}
)

_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _validate(python: str, daemon: str) -> None:
    from fused_render import core_templates
    from fused_render.shell.storage import home_dir

    # The fused engine runs map_render in the template's project venv; the
    # builtin executor owns no venv machinery and runs it on this app's own
    # interpreter. Both are ours; anything else is refused.
    venvs = os.path.realpath(os.path.join(home_dir(), "venvs"))
    requested = os.path.realpath(python)
    if (requested != os.path.realpath(sys.executable)
            and not requested.startswith(venvs + os.sep)):
        raise MapEngineError(
            f"refusing to spawn {python!r}: not an interpreter from the "
            f"project venv store ({venvs})")
    roots = [core_templates.PACKAGE_TEMPLATES_DIR,
             core_templates.core_templates_dir(),
             os.path.join(home_dir(), "templates")]
    override = os.environ.get("FUSED_RENDER_CORE_TEMPLATES")
    if override:
        roots.append(override)
    target = os.path.realpath(daemon)
    allowed = {os.path.realpath(os.path.join(root, "map", "daemon.py"))
               for root in roots}
    if target not in allowed:
        raise MapEngineError(
            f"refusing to run {daemon!r}: not the map template's daemon")


def _alive(child: Child) -> bool:
    proc = child.proc
    return proc is not None and proc.poll() is None


def _url(child: Child, path: str) -> str:
    separator = "&" if "?" in path else "?"
    return (f"http://127.0.0.1:{child.port}{path}{separator}"
            f"t={quote(child.token, safe='')}")


def _ping(child: Child) -> bool:
    try:
        with urllib.request.urlopen(_url(child, "/ping"), timeout=PING_TIMEOUT_S) as r:
            payload = json.load(r)
        return payload.get("ok") is True and payload.get("version") == child.version
    except (OSError, ValueError):
        return False


def _kill_tree(child: Child) -> None:
    """Stop the child and everything it started; see ai/supervisor._kill_tree
    for why each platform needs its own mechanism."""
    pid = child.pid
    if not pid:
        return
    if os.name == "nt":
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except (OSError, AttributeError, ValueError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _alive(child):
            time.sleep(0.05)
        if _alive(child):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        return
    try:
        leader = os.getpgid(pid) == pid
    except OSError:
        leader = False
    for sig in (signal.SIGTERM, _SIGKILL):
        if not _alive(child):
            return
        try:
            os.killpg(pid, sig) if leader else os.kill(pid, sig)
        except OSError:
            return
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _alive(child):
            time.sleep(0.05)


def _terminate(child: Child) -> None:
    _kill_tree(child)
    if child.proc is not None:
        try:
            child.proc.wait(timeout=3.0)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _tail(path: str, limit: int = 2000) -> str:
    try:
        with open(path, errors="replace") as handle:
            return handle.read()[-limit:]
    except OSError:
        return ""


def _spawn(child: Child) -> None:
    """Start the daemon and wait for it to publish its port and answer /ping."""
    os.makedirs(child.cache, exist_ok=True)
    status = os.path.join(child.cache, f"engine-{child.uid}.json")
    log = os.path.join(child.cache, "daemon.log")
    env = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP"):
        env.pop(name, None)
    proc = subprocess.Popen(
        [child.python, child.daemon,
         "--status", status, "--cache", child.cache, "--version", child.version],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=open(log, "ab"),
        cwd=os.path.dirname(child.daemon),
        env=env,
        close_fds=True,
        **SPAWN_KWARGS,
    )
    child.pid = proc.pid
    child.proc = proc

    deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_S
    while time.monotonic() < deadline and not child.port:
        if proc.poll() is not None:
            stderr = _tail(log)
            raise MapEngineError(
                f"the map engine exited before it started (code {proc.returncode})"
                + (f"\n{stderr}" if stderr.strip() else ""))
        try:
            with open(status, encoding="utf-8") as handle:
                published = json.load(handle)
        except (OSError, ValueError):
            time.sleep(BOOTSTRAP_POLL_S)
            continue
        port = published.get("port")
        if isinstance(port, int) and port > 0 and published.get("token"):
            child.port = port
            child.token = str(published["token"])
    try:
        os.unlink(status)
    except OSError:
        pass
    # The status file lands just before serve_forever; wait out that last gap
    # so the first proxied request never races the accept loop.
    while time.monotonic() < deadline:
        if _ping(child):
            return
        if proc.poll() is not None:
            raise MapEngineError(
                f"the map engine exited while starting (code {proc.returncode})")
        time.sleep(BOOTSTRAP_POLL_S)
    _terminate(child)
    raise MapEngineError("the map engine never became reachable")


def _replay_describes(child: Child) -> None:
    """Re-register every remembered source into a fresh child, best-effort —
    a source that fails to replay simply 404s until the page re-describes it."""
    with _lock:
        requests = list(_described.values())
    for request in requests:
        body = json.dumps(request).encode("utf-8")
        req = urllib.request.Request(
            _url(child, "/describe"), data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=60).close()
        except (OSError, ValueError):
            logger.warning("map engine: could not replay a describe after restart")


def remember(source_id: str, request: dict) -> None:
    """Keep the describe request that registered `source_id`, for replay."""
    with _lock:
        _described.pop(source_id, None)
        _described[source_id] = request
        while len(_described) > REMEMBER_LIMIT:
            _described.pop(next(iter(_described)))


def forget(source_id: str) -> None:
    """Drop a source the page has removed, so a restart does not replay a
    describe (a real remote open) for a layer nothing will ask tiles for."""
    with _lock:
        _described.pop(source_id, None)


def _matches(child: Child, python: str, daemon: str, cache: str, version: str) -> bool:
    return (child.version == version and child.python == python
            and child.daemon == daemon and child.cache == cache)


def current() -> Child | None:
    return _child


def ensure(python: str, daemon: str, cache: str, version: str) -> Child:
    """A live child matching the request, reusing the current one when it answers.

    The reuse check compares python/daemon/cache as well as version so a caller
    asking for a different bring-up is never handed a mismatched child.
    """
    _validate(python, daemon)
    global _child
    existing = _child
    if (existing is not None and _matches(existing, python, daemon, cache, version)
            and _alive(existing) and _ping(existing)):
        return existing
    # Only the spawn serializes; _child/_described stay reachable meanwhile.
    with _spawn_lock:
        existing = _child
        if (existing is not None and _matches(existing, python, daemon, cache, version)
                and _alive(existing) and _ping(existing)):
            return existing
        if existing is not None:
            _terminate(existing)
        child = Child(python=python, daemon=daemon, cache=cache, version=version)
        _spawn(child)
        with _lock:
            _child = child
        return child


def restart(failed: Child | None = None) -> Child:
    """Kill and respawn the child, replaying its described sources.

    `failed` is the child the caller's request died against: when another
    request already replaced it, the fresh child is returned as-is instead of
    being restarted again — a broken viewport fails a whole burst of tiles at
    once, and each of them calls here.

    The new child's sources are replayed BEFORE it is published, so a tile
    retried mid-replay waits on the spawn lock rather than seeing a child whose
    registry is still empty.
    """
    global _child
    existing = _child
    if (failed is not None and existing is not None and existing is not failed
            and _alive(existing) and _ping(existing)):
        return existing
    with _spawn_lock:
        existing = _child
        if existing is None:
            raise MapEngineError("the map engine has never been started")
        if (failed is not None and existing is not failed
                and _alive(existing) and _ping(existing)):
            return existing
        _terminate(existing)
        child = Child(python=existing.python, daemon=existing.daemon,
                      cache=existing.cache, version=existing.version)
        _spawn(child)
        _replay_describes(child)
        with _lock:
            _child = child
        return child


def stop() -> None:
    """Kill the child; called from the app's shutdown event."""
    global _child
    with _lock:
        child = _child
        _child = None
    if child is not None:
        _terminate(child)
