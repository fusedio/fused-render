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
/daemon.py under a known templates root. `ensure_background` adds the other
kind of child — a folder's own resident daemon (`background_apps.py`,
docs/ENGINE_HOST_DESIGN.md) — validated against that folder's own manifest.
A folder that names `main =` rather than `daemon =` gets `DEFAULT_DAEMON`,
the shipped worker that calls the module's `main(**params)` and is otherwise
an ordinary background child: same validation, same idle-timeout policy,
same reap path.

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

# --- the default daemon (fused_render/engine_worker.py) -----------------------
#: The daemon a `main =` manifest gets: a fixed shipped path, not under a
#: templates root and not inside any app folder, so `_validate`'s daemon-path
#: allowlist never applies and `_validate_background`'s folder-containment
#: check needs its own narrow exemption for exactly this path.
DEFAULT_DAEMON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "engine_worker.py")
#: How often the idle sweeper wakes.
_REAP_INTERVAL_S = 60.0
#: A proxied call to a child with a bounded lifetime (`idle_timeout_s > 0`,
#: e.g. a `main =` daemon running someone's `main(**params)`) gets this budget
#: instead of riding a request indefinitely — the same 60s `/api/run` bounds a
#: cold call to. A resident child (`idle_timeout_s == 0`: `daemon =`, or any
#: template daemon) is unaffected; its proxied calls have no host-imposed
#: budget beyond the connection-level timeout.
CALL_TIMEOUT_S = 60.0


class EngineError(RuntimeError):
    """Something the caller can be told verbatim."""


@dataclass
class Child:
    engine_id: str
    python: str
    daemon: str
    cache: str
    version: str
    #: The module a `main =` background daemon serves (DEFAULT_DAEMON calls
    #: its `main(**params)`); "" for a template daemon or a `daemon =`
    #: background daemon serving its own HTTP surface. When set, `_spawn`
    #: passes `--module <this>`.
    module: str = ""
    #: The declaring folder, for a background app's own daemon or shipped
    #: worker — `""` for a built-in template. Threaded through the same way
    #: `cache`/`version` already are (the caller resolved it from the
    #: manifest; re-deriving it from `daemon`'s dirname would be wrong
    #: whenever the manifest's `daemon` names a nested path). `_spawn_env`
    #: exports it as `FUSED_RENDER_APP_DIR` so a background daemon can
    #: address the background-apps API about itself without knowing its own
    #: page's `html` path — a template daemon never has a `folder`, so it
    #: never gets one.
    folder: str = ""
    #: How long this child may sit idle before `reap_idle_children` retires
    #: it; `0` (the default, and always the value for a template child) means
    #: resident. A background child carries whatever its manifest's
    #: `idle_timeout_s` resolved to (see `background_apps.load_manifest`) —
    #: `0` for a written `daemon =`, positive for a `main =` app unless its
    #: manifest overrides it.
    idle_timeout_s: float = 0.0
    #: Whether a proxied POST to this daemon is safely re-runnable — the
    #: manifest's own `retry_post` (`background_apps.Manifest.retry_post`),
    #: carried onto the child so the proxy's at-most-once decision is a
    #: property of THIS daemon's HTTP surface, not of how it was brought up.
    #: `False` (the default, and at-most-once the safe reading of it) unless
    #: the manifest opted in; a built-in template's own daemon (map's tile
    #: daemon is the first) declares `retry_post = true`.
    retry_post: bool = False
    #: Unique per spawn so two bring-ups never share a status file (the same
    #: overlap the AI workers hit — see ai/supervisor.Worker.uid).
    uid: str = field(default_factory=lambda: secrets.token_hex(4))
    port: int = 0
    token: str = ""
    pid: int = 0
    #: Last call routed to this child (monotonic); drives idle-retire for a
    #: child whose `idle_timeout_s` is non-zero.
    last_used: float = field(default_factory=time.monotonic)
    #: When this child's bring-up began (monotonic) — stamped at construction,
    #: so it counts from the moment the spawn started rather than from the
    #: first ping, and it is NOT restamped the way `last_used` is. Read only by
    #: `running_engines`, for the uptime the status bar's Activity panel shows;
    #: a heal-restart builds a new `Child`, so the uptime is this PROCESS's,
    #: which is what "pid 1234, up 3m" has to mean.
    started_at: float = field(default_factory=time.monotonic)
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
#: engine_id -> count of calls in flight. Keyed by id (not the Child object) so a
#: heal-restart mid-call still counts the live call; guards warm-app idle-retire.
_busy: dict[str, int] = {}

SPAWN_KWARGS = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    if os.name == "nt" else {"start_new_session": True}
)

_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

#: Called with a Child right after it is terminated, so consumers (the proxy's
#: per-child connection pool) can release resources tied to that child.
_terminate_hooks: list = []


def register_terminate_hook(fn) -> None:
    _terminate_hooks.append(fn)


def _validate_interpreter(python: str) -> None:
    """The interpreter must be one of ours: this app's own `sys.executable`, or a
    python from the home venv store; anything else is refused. Shared by the
    template path (`_validate`) and the background/daemon path
    (`ensure_background`); the server resolves it in both, so this is an
    invariant check, not a trust boundary."""
    from fused_render.shell.storage import home_dir

    venvs = os.path.realpath(os.path.join(home_dir(), "venvs"))
    requested = os.path.realpath(python)
    if (requested != os.path.realpath(sys.executable)
            and not requested.startswith(venvs + os.sep)):
        raise EngineError(
            f"refusing to spawn {python!r}: not an interpreter from the "
            f"project venv store ({venvs})")


def _validate(engine_id: str, python: str, daemon: str) -> None:
    from fused_render import core_templates
    from fused_render.shell.storage import home_dir

    if not _ENGINE_ID.match(engine_id):
        raise EngineError(f"refusing engine id {engine_id!r}: not a bare identifier")
    _validate_interpreter(python)
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


def _inflight(child: Child) -> int:
    """Calls the worker reports still running, or 0 if it can't be reached (an
    unreachable worker is reaped, not protected). A warm worker keeps running
    main() after a call's client gives up on a 504, so idle-retire consults this
    rather than kill a worker mid-call."""
    try:
        with urllib.request.urlopen(_url(child, "/ping"), timeout=PING_TIMEOUT_S) as r:
            payload = json.load(r)
        return payload.get("inflight", 0)
    except (OSError, ValueError):
        return 0


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
    for hook in _terminate_hooks:
        try:
            hook(child)
        except Exception:  # noqa: BLE001 — a hook must not block reaping
            logger.exception("engine terminate hook failed")


def _tail(path: str, limit: int = 2000) -> str:
    try:
        with open(path, errors="replace") as handle:
            return handle.read()[-limit:]
    except OSError:
        return ""


def _spawn_env(child: Child) -> dict:
    """The environment to launch *child* with.

    A venv-based child must not inherit this app's own Python env (it would
    break the venv's hermeticity), so PYTHONHOME/PYTHONPATH/PYTHONEXECUTABLE/
    PYTHONSTARTUP are stripped — UNLESS the child runs on this app's own
    `sys.executable`, in which case they are left intact: like the built-in
    executor (which spawns [sys.executable, _child.py] with the environment
    intact), a packaged/bundled interpreter needs PYTHONHOME to locate its own
    runtime at all, and that is a fact about the INTERPRETER, not about the
    caller.

    Keyed purely on `child.python == sys.executable`, so it applies uniformly
    to every child that runs on this app's own interpreter — a template
    daemon, a `main =` warm worker, or a `daemon =` background app all get
    the same treatment when that's the interpreter they were resolved to."""
    env = dict(os.environ)
    if child.python != sys.executable:
        for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP"):
            env.pop(name, None)
    # A background daemon otherwise has no way to learn its own app folder —
    # the background-apps API keys every endpoint off the page's `html` path
    # (see routers/background_apps.py), which the daemon never sees. A
    # template daemon never has a `folder` (see Child.folder), so this is a
    # no-op for it.
    if child.folder:
        env["FUSED_RENDER_APP_DIR"] = child.folder
    return env


def _spawn(child: Child) -> None:
    """Start the daemon and wait for it to publish its port and answer /ping."""
    os.makedirs(child.cache, exist_ok=True)
    status = os.path.join(child.cache, f"engine-{child.uid}.json")
    log = os.path.join(child.cache, "daemon.log")
    env = _spawn_env(child)
    argv = [child.python, child.daemon,
            "--status", status, "--cache", child.cache, "--version", child.version]
    # A warm app worker is told which module to serve; a template daemon isn't.
    if child.module:
        argv += ["--module", child.module]
    proc = subprocess.Popen(
        argv,
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


def _matches(child: Child, python: str, daemon: str, cache: str, version: str,
             module: str = "") -> bool:
    return (child.version == version and child.python == python
            and child.daemon == daemon and child.cache == cache
            and child.module == module)


def current(engine_id: str) -> Child | None:
    return _children.get(engine_id)


def _retry_post_for(daemon: str) -> bool:
    """The declaring folder's own `retry_post` flag, read for its declaration
    value ONLY — not as a trust boundary. `_validate` has already confirmed
    `daemon` resolves under a blessed template root before this is called, so
    reading `pyproject.toml` beside it cannot smuggle in a folder `_validate`
    would have rejected; this never routes through `_validate_background`'s
    folder-self-declaration trust model. A template with no
    `[tool.fused-render.app]` table at all (or none of one) reads as the safe
    default, `False`."""
    from fused_render import background_apps

    manifest = background_apps.load_manifest(os.path.dirname(daemon))
    return manifest.retry_post if manifest is not None else False


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
                      cache=cache, version=version,
                      retry_post=_retry_post_for(daemon))
        _spawn(child)
        with _lock:
            _children[engine_id] = child
        return child


# --- background apps (background_apps.py, SPEC.md §46) ------------------------


def _validate_background(engine_id: str, python: str, daemon: str,
                         folder: str = "", module: str = "") -> None:
    """Same invariant-check stance as `_validate`/`_validate_interpreter`: the
    caller (the start/restart endpoints, the startup resurrection hook)
    already resolved `daemon` from the folder's own manifest, so this is not
    the trust boundary either — it is a check that the caller did not hand
    over a daemon belonging to some folder whose OWN manifest does not
    declare it.

    Deliberately independent of the autostart store (D511): autostart is a
    separate, opt-in flag, and `start()` must work whether or not it is set,
    so this asks the declaring folder for ITS manifest
    (`background_apps.load_manifest`, self-contained — no store lookup) and
    checks simply "does that folder's manifest declare exactly this daemon
    file", which needs nothing but the daemon path itself.

    `folder` is the caller's own resolved declaring folder — the same one
    `ensure_background` threads onto `Child.folder` — used here instead of
    `os.path.dirname(daemon)` (D513): `load_manifest` only enforces
    containment, not flatness, so a manifest's `daemon` naming a NESTED path
    (`daemon = "src/daemon.py"`) is legal, and `dirname(daemon)` for such an
    app is a subfolder with no `pyproject.toml` of its own — re-deriving the
    folder that way made every such app un-startable (see `Child.folder`'s
    docstring, which already called this hazard out). Falls back to
    `os.path.dirname(daemon)` when `folder` is empty, preserving today's
    behavior for flat layouts and for the few direct callers (tests only —
    every production call site passes `folder`) that still don't pass one.

    A `main =` manifest never sets `manifest.daemon` — it is served by
    `DEFAULT_DAEMON`, which lives in this package, outside every app folder,
    so the folder-containment check above cannot apply to it. This is the
    one narrow exemption (mirroring `_validate`'s own templates-root
    allowlist): `daemon` is accepted here ONLY when it resolves to the exact
    `DEFAULT_DAEMON` path AND `module` resolves to the exact file the
    folder's own manifest declares as `main =` — never "any module", which
    would turn this from an invariant check back into a trust boundary."""
    from fused_render import background_apps

    if not _ENGINE_ID.match(engine_id):
        raise EngineError(f"refusing engine id {engine_id!r}: not a bare identifier")
    _validate_interpreter(python)
    target = os.path.realpath(daemon)
    folder = folder or os.path.dirname(daemon)
    manifest = background_apps.load_manifest(folder)
    if manifest is not None:
        if manifest.daemon and os.path.realpath(manifest.daemon) == target:
            return
        if (manifest.main and target == os.path.realpath(DEFAULT_DAEMON)
                and module and os.path.realpath(module) == os.path.realpath(manifest.main)):
            return
    raise EngineError(
        f"refusing to run {daemon!r}: not the declared daemon of its "
        "folder's own [tool.fused-render.app] background manifest")


def ensure_background(engine_id: str, python: str, daemon: str, cache: str,
                      version: str, folder: str = "",
                      idle_timeout_s: float = 0.0, module: str = "",
                      retry_post: bool = False) -> Child:
    """A live child for a background app's engine_id, reusing the current one
    when it matches and answers — the same double-checked reuse/spawn dance as
    `ensure`, but validated against its own declaring folder's manifest
    rather than the (now-autostart-only) store.

    `folder` is the manifest's declaring folder (every caller already has it
    in scope, the same way it already has `cache`/`version`) — stored on the
    `Child` so `_spawn_env` can export `FUSED_RENDER_APP_DIR` to the daemon,
    and passed to `_validate_background` so it validates `daemon` against the
    manifest of the folder that actually declared it, not a re-derived one
    (D513 — re-deriving via `os.path.dirname(daemon)` breaks any manifest
    whose `daemon` names a nested path). Optional (defaults to `""`) only so
    existing direct callers that don't care about it need not pass one; every
    production call site does. The reuse check below is `_matches` alone,
    same as `ensure`'s own — a background app's engine_id and a template's
    are drawn from disjoint namespaces (`background_apps.engine_id_for`'s
    `bg_`-prefixed digest vs. a template's bare name), and `_matches` already
    compares `daemon`, so a same-id-different-protocol collision could not
    pass it anyway.

    `idle_timeout_s` is the manifest's own policy, threaded onto `Child`
    unchanged; a non-zero value starts the idle sweeper (see
    `_ensure_reaper`) so this child actually gets reaped. `module` is set
    only for a `main =` manifest (`daemon` is then `DEFAULT_DAEMON`), telling
    `_spawn` which file to pass as `--module`. `retry_post` is the manifest's
    own `retry_post` (`background_apps.Manifest.retry_post`), threaded onto
    `Child` unchanged."""
    _validate_background(engine_id, python, daemon, folder, module)
    if idle_timeout_s > 0:
        _ensure_reaper()
    existing = _children.get(engine_id)
    if (existing is not None
            and _matches(existing, python, daemon, cache, version, module)
            and _alive(existing) and _ping(existing)):
        return existing
    with _spawn_lock:
        existing = _children.get(engine_id)
        if (existing is not None
                and _matches(existing, python, daemon, cache, version, module)
                and _alive(existing) and _ping(existing)):
            return existing
        if existing is not None:
            _terminate(existing)
        child = Child(engine_id=engine_id, python=python, daemon=daemon,
                      cache=cache, version=version, module=module,
                      folder=folder, idle_timeout_s=idle_timeout_s,
                      retry_post=retry_post)
        _spawn(child)
        # `last_used` defaults to the moment `Child` was constructed, BEFORE
        # `_spawn` runs; `_spawn` can block up to BOOTSTRAP_TIMEOUT_S (120s)
        # waiting for the child's first ping, so a short idle_timeout_s could
        # already be mostly exhausted before this child has ever served a
        # call. Re-stamp it now that bring-up is actually done.
        child.last_used = time.monotonic()
        with _lock:
            _children[engine_id] = child
        return child


def background_running_folders() -> set[str]:
    """The declaring folders of every currently-live background child — the
    run-state source for the `/apps` grid's running badge. Reads
    `_children` rather than `background_apps.autostart_paths()`: `start()`
    doesn't persist anything (D511 keeps run state and autostart
    independent), so a running-but-not-autostart daemon has no row in the
    autostart store to appear in. `_children`/`folder`/`_alive` are all
    already in memory, so this is a snapshot over a dict plus a
    `Popen.poll()` per child — no folder walk, no toml read, cheap enough to
    call once per grid render exactly like the endpoint's docstring
    promises."""
    with _lock:
        children = list(_children.values())
    return {c.folder for c in children if c.folder and _alive(c)}


def running_engines() -> list[dict]:
    """Every currently-live child, as plain dicts — what
    `GET /api/engines/running` reports so the status bar can list what is
    running and offer a Stop per engine (D591).

    Modelled on `background_running_folders` above, and for the same reasons:
    the list is taken UNDER `_lock` and `_alive()` is called OUTSIDE it, so a
    `Popen.poll()` per child never happens while the lock is held. Everything
    is already in memory, so this is a dict snapshot plus one poll per child —
    no folder walk, no toml read.

    A helper HERE rather than the router reaching into `_children`: the router
    must not touch this module's lock or its private dict (the same boundary
    `background_running_folders` exists to keep).

    `folder` and `module` are reported as-is — `folder` is set for a
    background child only, and `module` for a `main =` daemon (a `daemon =`
    app leaves it empty), so the caller can label a row without having to
    guess the manifest's protocol.

    UPTIME AND IDLE_FOR ANSWER "WHY IS THIS STILL HERE, AND FOR HOW LONG"
    (user call: a daemon row that says only its name leaves the panel
    unreadable — "if a daemon has a timeout, lets also mention that"). Both
    are derived from the same `now`, so one row cannot report an uptime and
    a countdown taken a poll apart:

    * `uptime_s` — seconds since this child's bring-up began.
    * `idle_timeout_s` — the manifest's own policy, `0` for a resident child
      (`daemon =`, and every template daemon), which the caller renders as
      "no idle timeout" rather than as "retires in 0s".
    * `idle_for_s` — seconds since the last call was routed here. Reported
      for every child, but only meaningful against a non-zero timeout.
    * `busy` — idle-retire is currently skipping this child, so a stalled-
      looking countdown has an explanation rather than reading as a bug.

    `busy` is not simply `_busy`'s own gate: a call that outran the 60s proxy
    budget gets a 504, whose `finally` calls `mark_idle` (routers/engines.py)
    — `_busy` drops to 0 and `last_used` is stamped as though the call had
    ended, but the worker's `main()` keeps running. `reap_idle_children`
    knows this and consults `_inflight` (a ping to the worker) before
    reaping; a row that only echoed `_busy` would read "retiring now" for
    exactly the state this field exists to explain. So a child past its own
    idle timeout with `_busy` already clear is pinged the same way
    `reap_idle_children` pings it — rare by construction, since only a
    reap CANDIDATE is ever pinged — before its `busy` is decided.

    `_busy` is snapshotted under the same `_lock` hold as `_children` so the
    two cannot disagree about a child, and `_alive()`/`_inflight()` still run
    outside it.
    """
    now = time.monotonic()
    with _lock:
        children = list(_children.values())
        busy = dict(_busy)
    result = []
    for c in children:
        if not _alive(c):
            continue
        idle_for_s = max(0.0, now - c.last_used)
        is_busy = busy.get(c.engine_id, 0) > 0
        if not is_busy and c.idle_timeout_s > 0 and idle_for_s >= c.idle_timeout_s:
            is_busy = _inflight(c) > 0
        result.append({
            "engine_id": c.engine_id,
            "pid": c.pid,
            "version": c.version,
            "folder": c.folder,
            "module": c.module,
            "uptime_s": max(0.0, now - c.started_at),
            "idle_timeout_s": c.idle_timeout_s,
            "idle_for_s": idle_for_s,
            "busy": is_busy,
        })
    return result


#: Set once the first bring-up starts the (daemon) idle sweeper thread.
_reaper_started = threading.Event()


def _ensure_reaper() -> None:
    """Start the idle sweeper thread once, the first time any child is brought
    up with a non-zero `idle_timeout_s`. Callers already gate on that, so this
    has nothing left to check beyond "already running"."""
    if _reaper_started.is_set():
        return
    with _spawn_lock:
        if _reaper_started.is_set():
            return
        _reaper_started.set()
        threading.Thread(target=_reap_loop, name="engine-idle-reaper",
                         daemon=True).start()


def _reap_loop() -> None:
    while True:
        time.sleep(_REAP_INTERVAL_S)
        try:
            reap_idle_children()
        except Exception:  # noqa: BLE001 — a sweep failure must not kill the loop
            logger.exception("idle child sweep failed")


def mark_busy(engine_id: str) -> None:
    """Register a call in flight for *engine_id* so idle-retire skips it. Keyed by
    id, so a heal-restart mid-call keeps the live call counted."""
    with _lock:
        _busy[engine_id] = _busy.get(engine_id, 0) + 1


def mark_idle(engine_id: str) -> None:
    """Balance a `mark_busy`; stamp the current child's last_used so idle is timed
    from the call's end, whichever child served it after a heal."""
    with _lock:
        remaining = _busy.get(engine_id, 0) - 1
        if remaining > 0:
            _busy[engine_id] = remaining
        else:
            _busy.pop(engine_id, None)
        child = _children.get(engine_id)
        if child is not None:
            child.last_used = time.monotonic()


def reap_idle_children(now: float | None = None) -> int:
    """Terminate every child idle past its own `idle_timeout_s`, returning the
    count reaped. A child with `idle_timeout_s == 0` (the default for a
    `daemon =` app and every template child) is never a candidate —
    eligibility is the child's own policy, not what brought the child up.
    Exposed so a test can drive it directly.

    The Child record is left in `_children` (dead, but present) rather than
    popped: `engine_forward._forward`'s existing heal-on-proxy path calls
    `restart(engine_id, child)` using an existing record's own bring-up args
    whenever a proxied call finds it unreachable, and only 409s when
    `current(engine_id)` is `None` from the start. Popping the record here
    would defeat that path for every retired `main =` child, since the very
    next call to it would find no record at all instead of one `restart` can
    revive — the same reason autostart folders (no page ever calling here)
    still need the next call to re-warm them. `stop()` still fully clears the
    record: that IS the "quit this app right now, stay down" contract a
    `daemon =` app's explicit stop makes, which idle retirement (an invisible,
    resumable pause) must not share.

    Candidate selection also requires `_alive(c)`: a record left behind by an
    earlier sweep (or by `stop()`'s sibling paths) still carries its stale
    `last_used`, so without this check every later sweep would re-match the
    same already-dead child, re-log its retirement, and re-signal a pid the
    OS may since have recycled. `_alive` is a local `Popen.poll()`, not I/O,
    so it is checked here under `_lock` alongside the rest of the cheap
    candidate filter, unlike `_inflight`'s network ping below.
    """
    now = time.monotonic() if now is None else now
    with _lock:
        candidates = [c for c in _children.values()
                      if c.idle_timeout_s > 0 and _alive(c)
                      and _busy.get(c.engine_id, 0) == 0
                      and (now - c.last_used) >= c.idle_timeout_s]
    # A call that outran its budget got a 504 but its main() may still be running
    # in the worker (we never kill it); the worker reports that, so skip a worker
    # that is still mid-call rather than truncate it. Pinged outside _lock.
    stale = [c for c in candidates if _inflight(c) == 0]
    with _lock:
        stale = [c for c in stale if _children.get(c.engine_id) is c]
    for child in stale:
        logger.info("retiring idle child %s (module %s)",
                    child.engine_id, child.module)
        _terminate(child)
    return len(stale)


def restart(engine_id: str, failed: Child | None = None,
           version: str | None = None) -> Child:
    """Kill and respawn the child, replaying its reinit requests.

    `failed` is the child the caller's request died against: when another
    request already replaced it, the fresh child is returned as-is instead of
    being restarted again — a broken viewport fails a whole burst of requests at
    once, and each of them calls here.

    `version` overrides the respawned child's version digest; omitted (the
    default), the existing child's own `version` carries over unchanged, the
    original behavior every non-background caller (engine_forward.py's
    heal-on-proxy) still wants — a healed child should keep answering to the
    same version its caller already knows. `/api/apps/background/restart`
    (D510) passes the FRESHLY computed digest instead: a restart must tag
    the respawned child with the digest of the code it's actually running,
    so `fused.daemon.restart()` right after editing `daemon.py` respawns the
    new code tagged with the new version, and the next start()/server-start
    resurrection sees its own fresh digest agree with what's registered
    instead of tearing the just-restarted child down and spawning it a
    second time.

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
                      version=version if version is not None else existing.version,
                      module=existing.module,
                      folder=existing.folder,
                      idle_timeout_s=existing.idle_timeout_s,
                      retry_post=existing.retry_post)
        _spawn(child)
        _replay(child)
        # `last_used` defaults to the moment `Child` was constructed above,
        # BEFORE `_spawn`/`_replay` run; both can take real time (`_spawn`
        # waits on the child's status file, `_replay` re-POSTs every
        # registered reinit request), so a short `idle_timeout_s` could
        # already be mostly exhausted before this child has ever served a
        # real call. Re-stamp it now that bring-up is actually done, the same
        # restamp `ensure_background`'s freshly-spawned branch already makes.
        child.last_used = time.monotonic()
        with _lock:
            _children[engine_id] = child
        return child


def stop(engine_id: str) -> None:
    """Kill one engine's child and drop its replay registry."""
    with _lock:
        child = _children.pop(engine_id, None)
        _reinit.pop(engine_id, None)
        _busy.pop(engine_id, None)
    if child is not None:
        _terminate(child)


def stop_all() -> None:
    """Kill every managed child; called from the app's shutdown event."""
    with _lock:
        children = list(_children.values())
        _children.clear()
        _reinit.clear()
        _busy.clear()
    for child in children:
        _terminate(child)
