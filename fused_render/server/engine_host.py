"""Managed template daemons, owned by this server (docs/ENGINE_HOST_DESIGN.md).

A template that needs a long-lived worker (the map viewer's tile daemon is the
first) hands one over here and the :1777 process owns its whole lifecycle. Each
engine_id names one child: it binds :0 and publishes {port, token, pid, version}
to a status file — binding in the child and reporting back is the only version
with no race — and the host keeps the Popen so it can reap via poll() (never
os.kill(pid, 0), which kills the process on Windows), replace a wedged child,
and kill the whole tree from the app's shutdown event. The browser never sees
the port or token: routers/engines.py proxies everything through the stable
server origin.

The interpreter is the caller's, not this process's. A template's render entry
point runs inside its project venv (PY-16/D276) — the only interpreter on the
machine holding that template's extra stack — so it hands over its own
sys.executable. The handoff is validated rather than trusted: the python must
live in the home venv store, and the daemon must be <templates-root>/<engine_id>
/daemon.py under a known templates root.

A restarted child starts empty, so any descriptor the pages hold would 404.
`reinit()` records the requests that registered a template's state, and a restart
replays them into the fresh child before any request is retried — that replay is
what makes a daemon death invisible to the page. The host never reads what it
replays: the request path and body are opaque, chosen by the template.

Nothing here knows what a "tile" is; the engine_id and the reinit requests are
the only template-specific data, and both are supplied by the caller.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import quote

logger = logging.getLogger(__name__)

BOOTSTRAP_TIMEOUT_S = 120.0
BOOTSTRAP_POLL_S = 0.25
PING_TIMEOUT_S = 2.0
#: How many reinit requests a restart will replay per engine. Bounded so a long
#: session (e.g. scrubbing through hundreds of timesteps) cannot turn one restart
#: into an unbounded replay storm; the page re-registers anything evicted.
REINIT_LIMIT = 64
#: engine_id is joined into a filesystem path, so it must be a bare identifier —
#: no separators or dots that could climb out of a templates root.
_ENGINE_ID = re.compile(r"^[a-z0-9_]+$")


class EngineError(RuntimeError):
    """Something the caller can be told verbatim."""


@dataclass
class Child:
    engine_id: str
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


#: Guards the _children pointers and the _reinit registry only — both fast, and
#: never held across the blocking spawn/ping/replay I/O below, so reinit() and
#: current() never wait on a cold start.
_lock = threading.Lock()
#: Serializes bring-up so two callers can never spawn two children for one
#: engine; the only lock held across network I/O, and only ensure()/restart()
#: contend on it.
_spawn_lock = threading.Lock()
#: engine_id -> its one live child.
_children: dict[str, Child] = {}
#: engine_id -> {key -> {"path": str, "payload": dict}}, in insertion order.
_reinit: dict[str, "OrderedDict[str, dict]"] = {}

SPAWN_KWARGS = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt" else {"start_new_session": True}
)

_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _validate(engine_id: str, python: str, daemon: str) -> None:
    from fused_render import core_templates
    from fused_render.shell.storage import home_dir

    if not _ENGINE_ID.match(engine_id):
        raise EngineError(f"refusing engine id {engine_id!r}: not a bare identifier")
    # The fused engine runs the render entry point in the template's project
    # venv; the builtin executor owns no venv machinery and runs it on this
    # app's own interpreter. Both are ours; anything else is refused.
    venvs = os.path.realpath(os.path.join(home_dir(), "venvs"))
    requested = os.path.realpath(python)
    if (requested != os.path.realpath(sys.executable)
            and not requested.startswith(venvs + os.sep)):
        raise EngineError(
            f"refusing to spawn {python!r}: not an interpreter from the "
            f"project venv store ({venvs})")
    roots = [core_templates.PACKAGE_TEMPLATES_DIR,
             core_templates.core_templates_dir(),
             os.path.join(home_dir(), "templates")]
    override = os.environ.get("FUSED_RENDER_CORE_TEMPLATES")
    if override:
        roots.append(override)
    target = os.path.realpath(daemon)
    allowed = {os.path.realpath(os.path.join(root, engine_id, "daemon.py"))
               for root in roots}
    if target not in allowed:
        raise EngineError(
            f"refusing to run {daemon!r}: not the {engine_id!r} template's daemon")


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
            raise EngineError(
                f"the {child.engine_id} engine exited before it started "
                f"(code {proc.returncode})"
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
            raise EngineError(
                f"the {child.engine_id} engine exited while starting "
                f"(code {proc.returncode})")
        time.sleep(BOOTSTRAP_POLL_S)
    _terminate(child)
    raise EngineError(f"the {child.engine_id} engine never became reachable")


def _replay(child: Child) -> None:
    """Re-issue every remembered reinit request into a fresh child, best-effort —
    one that fails to replay simply 404s until the page re-registers it. The
    path and body are the template's; the host only re-POSTs them."""
    with _lock:
        requests = list(_reinit.get(child.engine_id, {}).values())
    for entry in requests:
        body = json.dumps(entry["payload"]).encode("utf-8")
        req = urllib.request.Request(
            _url(child, entry["path"]), data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=60).close()
        except (OSError, ValueError):
            logger.warning("%s engine: could not replay a reinit after restart",
                           child.engine_id)


def reinit(engine_id: str, key: str, path: str, payload: dict) -> None:
    """Record a request that registers template state, so a restart replays it."""
    with _lock:
        registry = _reinit.setdefault(engine_id, OrderedDict())
        registry.pop(key, None)
        registry[key] = {"path": path, "payload": payload}
        while len(registry) > REINIT_LIMIT:
            registry.popitem(last=False)


def forget(engine_id: str, key: str) -> None:
    """Drop a registration the page has removed, so a restart does not replay a
    request (a real remote open) for state nothing will ask for."""
    with _lock:
        registry = _reinit.get(engine_id)
        if registry is not None:
            registry.pop(key, None)


def _matches(child: Child, python: str, daemon: str, cache: str, version: str) -> bool:
    return (child.version == version and child.python == python
            and child.daemon == daemon and child.cache == cache)


def current(engine_id: str) -> Child | None:
    return _children.get(engine_id)


def ensure(engine_id: str, python: str, daemon: str, cache: str,
           version: str) -> Child:
    """A live child for engine_id matching the request, reusing the current one
    when it answers.

    The reuse check compares python/daemon/cache as well as version so a caller
    asking for a different bring-up is never handed a mismatched child.
    """
    _validate(engine_id, python, daemon)
    existing = _children.get(engine_id)
    if (existing is not None and _matches(existing, python, daemon, cache, version)
            and _alive(existing) and _ping(existing)):
        return existing
    # Only the spawn serializes; _children/_reinit stay reachable meanwhile.
    with _spawn_lock:
        existing = _children.get(engine_id)
        if (existing is not None and _matches(existing, python, daemon, cache, version)
                and _alive(existing) and _ping(existing)):
            return existing
        if existing is not None:
            _terminate(existing)
        child = Child(engine_id=engine_id, python=python, daemon=daemon,
                      cache=cache, version=version)
        _spawn(child)
        with _lock:
            _children[engine_id] = child
        return child


def restart(engine_id: str, failed: Child | None = None) -> Child:
    """Kill and respawn the child, replaying its reinit requests.

    `failed` is the child the caller's request died against: when another
    request already replaced it, the fresh child is returned as-is instead of
    being restarted again — a broken viewport fails a whole burst of requests at
    once, and each of them calls here.

    The new child's state is replayed BEFORE it is published, so a request
    retried mid-replay waits on the spawn lock rather than seeing a child whose
    registry is still empty.
    """
    existing = _children.get(engine_id)
    if (failed is not None and existing is not None and existing is not failed
            and _alive(existing) and _ping(existing)):
        return existing
    with _spawn_lock:
        existing = _children.get(engine_id)
        if existing is None:
            raise EngineError(f"the {engine_id} engine has never been started")
        if (failed is not None and existing is not failed
                and _alive(existing) and _ping(existing)):
            return existing
        _terminate(existing)
        child = Child(engine_id=engine_id, python=existing.python,
                      daemon=existing.daemon, cache=existing.cache,
                      version=existing.version)
        _spawn(child)
        _replay(child)
        with _lock:
            _children[engine_id] = child
        return child


def stop(engine_id: str) -> None:
    """Kill one engine's child."""
    with _lock:
        child = _children.pop(engine_id, None)
    if child is not None:
        _terminate(child)


def stop_all() -> None:
    """Kill every managed child; called from the app's shutdown event."""
    with _lock:
        children = list(_children.values())
        _children.clear()
    for child in children:
        _terminate(child)
