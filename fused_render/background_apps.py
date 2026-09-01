"""Background apps: a folder can declare a long-running daemon that this
server supervises — single instance, the folder's own venv, killed with the
server (see engine_host.py's "background" child kind and
server/routers/background_apps.py).

A folder opts in with a manifest table in its own `pyproject.toml`, declaring
exactly one of two protocols:

    [tool.fused-render.app]
    daemon = "daemon.py"   # your own HTTP surface; resident by default

    [tool.fused-render.app]
    main = "compute.py"    # the shipped worker calls main(**params); reaped
                           # after DEFAULT_MAIN_IDLE_TIMEOUT_S idle seconds

`idle_timeout_s` overrides that per-protocol default; `0` means resident
(never reaped). Either filename is resolved inside the folder, following
registered_apps.py's containment-guard style: a value that resolves outside
the folder (e.g. via `../`) is refused rather than trusted.

Run state and autostart are two independent, orthogonal things (D511, code
review that produced this module's current shape): whether the daemon is
alive RIGHT NOW is `engine_host`'s own live-child bookkeeping, while
autostart is only "should the server bring this up at every launch" — a
persisted opt-in flag, and nothing else. The autostart store
(`<home_dir()>/background_apps.json`) is that sticky "bring this back at
startup" list, following registered_apps.py's read/write discipline: a
folder that is temporarily missing or unreadable drops out of
`autostart_paths()` (read-only — it may come back), and only `set_autostart`
rewrites the store. **Autostart is opt-in and starting an app never touches
it** — `server/routers/background_apps.py`'s `start` endpoint spawns a
daemon without persisting anything; only an explicit `autostart` call (or a
direct `set_autostart(path, True)`) turns the flag on. `resurrect_autostart`
(the startup hook) only ever brings up paths explicitly present in the
store.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from dataclasses import dataclass

from fused_render.index.ignore import MountGuard
from fused_render.shell import storage

logger = logging.getLogger(__name__)

#: engine_id prefix for a background app; the rest is a hash of the folder's
#: realpath (engine_host.py's `_ENGINE_ID` requires a bare identifier).
_ENGINE_ID_PREFIX = "bg_"

#: Default `idle_timeout_s` for a `daemon =` manifest: resident, never reaped
#: — it holds connections and its own state, which a periodic restart would
#: silently drop.
DEFAULT_DAEMON_IDLE_TIMEOUT_S = 0.0
#: Default `idle_timeout_s` for a `main =` manifest: the shipped worker is a
#: warm-cache optimization over a fresh subprocess per call, so its restart is
#: invisible to the caller — reap it after 15 idle minutes, same as the
#: warm-worker default this replaces.
DEFAULT_MAIN_IDLE_TIMEOUT_S = 15 * 60.0


@dataclass(frozen=True)
class Manifest:
    #: Absolute path to the app folder (the `pyproject.toml`'s directory).
    folder: str
    #: Absolute path to the daemon file, or "" when the folder declares `main`
    #: instead. Exactly one of `daemon`/`main` is ever set.
    daemon: str
    #: Absolute path to the `main()`-exposing file the shipped worker serves,
    #: or "" when the folder declares `daemon` instead.
    main: str
    #: How long the child may sit idle before the reaper retires it; `0`
    #: means resident. Defaults per protocol (see the two constants above),
    #: overridable with an explicit `idle_timeout_s` key in the manifest.
    idle_timeout_s: float


def load_manifest(folder: str) -> Manifest | None:
    """The folder's background-app manifest, or None when the folder does not
    declare one, declares neither or both of `daemon`/`main`, or the
    declared file does not resolve to a FILE inside the folder. Never raises
    — a missing or corrupt `pyproject.toml`, an unreadable folder, or a value
    naming a directory (`daemon = "."` passes the containment check below on
    its own) all simply read as "no manifest" rather than reaching
    engine_host as a `python <folder>` bring-up that fails opaquely."""
    # tomllib is 3.11+ stdlib and `requires-python` is >=3.10, so on 3.10 the
    # `tomli` dependency supplies it (same fallback as projectenv._load_manifest
    # — both names expose TOMLDecodeError, so aliasing keeps the except below
    # unchanged). A missing parser is not a user-actionable error (every
    # install of fused-render has one), so it's a warning, not a raise.
    try:
        import tomllib
    except ImportError:
        try:
            import tomli as tomllib
        except ImportError:
            logger.warning(
                "neither tomllib (Python 3.11+) nor tomli is available; "
                "pyproject.toml files cannot be read"
            )
            return None
    folder = os.path.abspath(folder)
    pyproject = os.path.join(folder, "pyproject.toml")
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    tool = data.get("tool")
    table = tool.get("fused-render") if isinstance(tool, dict) else None
    app = table.get("app") if isinstance(table, dict) else None
    if not isinstance(app, dict):
        return None
    daemon_name = app.get("daemon")
    main_name = app.get("main")
    has_daemon = isinstance(daemon_name, str) and bool(daemon_name)
    has_main = isinstance(main_name, str) and bool(main_name)
    # Exactly one of the two protocols — a manifest declaring both or
    # neither is ambiguous rather than defaulted (Key decision: explicit
    # beats inferred for a subprocess-spawn path).
    if has_daemon == has_main:
        return None

    def _resolve(name: str) -> str | None:
        target = os.path.normpath(os.path.join(folder, name))
        # Containment, realpath-resolved (registered_apps.py's guard shape):
        # a value that climbs out of the folder via `../` or a symlink must
        # not be trusted just because the string join looked contained.
        real_folder = os.path.realpath(folder)
        real_target = os.path.realpath(target)
        if real_target != real_folder and not real_target.startswith(real_folder + os.sep):
            return None
        # A FILE, not merely a path inside the folder — `daemon = "."` (or
        # any other directory) passes containment trivially (a folder is
        # "inside" itself), and os.stat succeeds on a directory just as it
        # does on a file, so version_for would not catch this either;
        # bring-up would then run `python <folder>` and fail with an opaque
        # bootstrap error instead of a clean manifest rejection here.
        if not os.path.isfile(real_target):
            return None
        return target

    idle_timeout_s = app.get("idle_timeout_s")
    if not isinstance(idle_timeout_s, (int, float)) or isinstance(idle_timeout_s, bool):
        idle_timeout_s = None

    if has_daemon:
        daemon = _resolve(daemon_name)
        if daemon is None:
            return None
        default_idle = DEFAULT_DAEMON_IDLE_TIMEOUT_S
        return Manifest(folder=folder, daemon=daemon, main="",
                        idle_timeout_s=(idle_timeout_s if idle_timeout_s is not None
                                        else default_idle))
    main = _resolve(main_name)
    if main is None:
        return None
    return Manifest(folder=folder, daemon="", main=main,
                    idle_timeout_s=(idle_timeout_s if idle_timeout_s is not None
                                    else DEFAULT_MAIN_IDLE_TIMEOUT_S))


def engine_id_for(folder: str) -> str:
    """The stable engine_id for a background app's folder, distinct from a
    template's own engine_id prefix so the two kinds can never collide. Keyed
    by realpath so a symlinked folder and its target share one engine (and
    thus one running instance)."""
    digest = hashlib.sha1(os.path.realpath(folder).encode("utf-8")).hexdigest()
    return _ENGINE_ID_PREFIX + digest[:12]


def version_for(folder: str, interpreter: str) -> str:
    """Digest of the manifest's declaring bytes, the daemon file's mtime/size
    (for a `main =` manifest, `engine_host.DEFAULT_DAEMON`'s mtime/size too),
    and the INTERPRETER's own path-plus-identity (mtime/size at that path, not
    a realpath). Changing any of these must retire a running child rather
    than reuse it: a `pyproject.toml` edit, a daemon.py/compute.py edit, an
    `engine_worker.py` upgrade, or a bundled-CPython swap across an app
    upgrade (this is what fixes the OpenWhisper upgrade-rot class — a stale
    venv reused against a new interpreter).

    D514 revised (2026-08-26 code review): the interpreter component used to
    be `os.path.realpath(interpreter)` alone, which broke two ways. First, a
    realpath DESTROYS venv identity rather than naming it: a venv's `bin/
    python` is a symlink to its base CPython, so realpath collapses every
    venv built on the same base interpreter into one identity, and two
    different app folders' venvs contributed an IDENTICAL digest component —
    caught by CI (Linux's `/usr/bin/python3` and `/usr/bin/python3.12` are
    symlinks to the same file; macOS/Windows runners happened not to alias
    the two paths the test used, so it passed everywhere else). Second, a
    path alone — realpath'd or not — cannot see the exact upgrade-rot case
    D514 exists for: the packaged app's own interpreter gets rewritten IN
    PLACE at the same path on upgrade (confirmed against a real install: same
    path, same `--version` string, different bytes, mtime moved). So the
    interpreter now gets the identical treatment the daemon file already gets
    two lines above it — the RAW path (no realpath) plus an `os.stat` of the
    file actually at that path, mtime and size both — which catches an
    in-place rewrite the same way it already catches a daemon.py edit.

    Raises OSError if the manifest is missing/invalid, the daemon file does
    not exist, or the interpreter does not exist — all "dead manifest" /
    "dead bring-up" cases, which callers (the enable endpoint's `_resolve`,
    the startup resurrection hook) must treat as a failure to skip, not fall
    back on a stale version for. Both already `os.path.isfile(interpreter)`
    BEFORE calling this, so the interpreter `os.stat` below should not raise
    in practice — this is the same TOCTOU-tolerant stance the daemon stat
    already has, not a new trust assumption."""
    manifest = load_manifest(folder)
    if manifest is None:
        raise OSError(f"{folder!r} has no valid background-app manifest")
    with open(os.path.join(manifest.folder, "pyproject.toml"), "rb") as f:
        pyproject_bytes = f.read()
    daemon_st = os.stat(manifest.daemon or manifest.main)
    interp_st = os.stat(interpreter)
    h = hashlib.sha256()
    h.update(pyproject_bytes)
    h.update(f"{daemon_st.st_mtime_ns}:{daemon_st.st_size}".encode("utf-8"))
    if manifest.main:
        # A `main =` manifest is served by the shipped worker
        # (engine_host.DEFAULT_DAEMON), not by anything inside the app's own
        # folder — daemon_st above only ever sees the user's own module
        # (manifest.main). Without also stating DEFAULT_DAEMON itself, a
        # changed engine_worker.py would not change this digest at all,
        # silently losing the guard the deleted APP_WORKER_VERSION constant
        # used to provide: a running child built against an older worker
        # would be reused across an upgrade instead of retired.
        from fused_render.server import engine_host

        worker_st = os.stat(engine_host.DEFAULT_DAEMON)
        h.update(f"{worker_st.st_mtime_ns}:{worker_st.st_size}".encode("utf-8"))
    h.update(interpreter.encode("utf-8"))
    h.update(f"{interp_st.st_mtime_ns}:{interp_st.st_size}".encode("utf-8"))
    return h.hexdigest()


# -------------------------------------------------------- autostart store


def _store_path() -> str:
    return os.path.join(storage.home_dir(), "background_apps.json")


def autostart_paths() -> list[str]:
    """Folder paths the user has opted into autostart, in stored order,
    realpath-normalized. A folder that is behind a blocked mount, missing,
    or otherwise unreadable is skipped from the result (read-only — the
    store itself is untouched, so the folder reappears here the moment it's
    readable again).

    realpath, not abspath (D512, folded in from the deferred half of D509):
    `engine_id_for` and the router's `_folder_for` both key identity off
    `os.path.realpath`, so a symlinked folder's autostart entry must
    normalize the same way — an abspath-only store could disagree with
    `engine_id_for` about whether a symlinked alias and its target are "the
    same" folder, exactly the bug D509 fixed for the router's own
    `_folder_for`."""
    data = storage.read_json(_store_path())
    if not isinstance(data, dict):
        return []
    raw = data.get("autostart")
    if not isinstance(raw, list):
        return []
    guard = MountGuard()
    out = []
    for path in raw:
        if not isinstance(path, str) or not os.path.isabs(path):
            continue
        if guard.blocks(path):
            continue
        try:
            if not os.path.isdir(path):
                continue
        except OSError:
            continue
        out.append(os.path.realpath(path))
    return out


def set_autostart(path: str, autostart: bool) -> None:
    """Persist *path*'s autostart flag. Idempotent: turning autostart on for
    an already-on path (or off for an already-off one) is a no-op write, not
    a duplicate entry. Does NOT start or stop anything — see the module
    docstring: autostart is opt-in and orthogonal to run state."""
    path = os.path.realpath(path)
    data = storage.read_json(_store_path())
    raw = data.get("autostart") if isinstance(data, dict) else None
    current = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
    current = [p for p in current if os.path.realpath(p) != path]
    if autostart:
        current.append(path)
    storage.write_json(_store_path(), {"autostart": current})


# --------------------------------------------------------- bring-up helpers


def interpreter_for(folder: str) -> str:
    """The interpreter a background app in *folder* runs on: that folder's OWN
    project venv python when it declares one AND the fused engine is
    effective, else this app's own `sys.executable`. Does not check the
    interpreter exists; a caller that cares (`routers/background_apps.py`'s
    `_resolve`) falls back to `sys.executable` itself rather than refuse to
    start.

    Deliberately does NOT reuse `projectenv.project_env_for`/`project_root_for`,
    which walk UPWARD from a `.py` FILE to find the enclosing project — a
    decision, not an oversight (D503, 2026-08-26 code review). Walking upward
    is correct for a bare script, which has no project boundary of its own,
    so the nearest ancestor `pyproject.toml` IS its project. A background
    app's FOLDER is already the project boundary — it declares itself
    unambiguously via `[tool.fused-render.app]` — so walking past it would
    silently adopt whatever unrelated ancestor project happens to sit above
    it on disk (measured: the shipped fixture, `tests/fixtures/background_app`,
    nested inside this repo, resolved to the repo's own venv, an interpreter
    this app has no business running on). `has_project_env(folder)` checks
    ONLY the app's own manifest — a `[project]` table with at least one
    applicable dependency — so a manifest-only app (no deps of its own, like
    the fixture) runs on `sys.executable`, full stop, regardless of what any
    ancestor folder declares."""
    from fused_render import projectenv
    from fused_render.shell import prefs as shell_prefs

    if shell_prefs.effective_engine() != "fused":
        return sys.executable
    folder = os.path.abspath(folder)
    if projectenv.has_project_env(folder):
        return projectenv.interpreter_for(folder)
    return sys.executable


def bring_up_args(manifest: Manifest) -> tuple[str, str]:
    """The `(daemon, module)` pair `engine_host.ensure_background` needs for
    *manifest*: a `daemon =` manifest passes its own file with no module; a
    `main =` manifest passes the shipped worker (`engine_host.DEFAULT_DAEMON`)
    with `module` set to the file it serves. One place for every caller
    (the startup resurrection hook, the start/restart endpoints) to turn a
    manifest into a bring-up rather than re-deriving the branch each time."""
    from fused_render.server import engine_host

    if manifest.daemon:
        return manifest.daemon, ""
    return engine_host.DEFAULT_DAEMON, manifest.main


def cache_dir_for(engine_id: str) -> str:
    """Where a background app's status/log files live — under the home dir,
    never beside the user's code (MD-7)."""
    return os.path.join(storage.home_dir(), "apps", engine_id)


def resurrect_autostart(shutdown_event=None) -> None:
    """Start every autostart-opted-in app's daemon, best-effort: a folder
    that fails (dead manifest, project venv not built, a spawn error) is
    logged and skipped, never allowed to stop the rest. Only ever brings up
    paths explicitly present in the autostart store (`autostart_paths()`) —
    a `start()` call that never set autostart must never come back here.
    Meant to run on a daemon thread
    from the server's startup event (app.py) — never on the pre-bind path
    (D228): each bring-up is a subprocess spawn plus a bootstrap wait.

    `shutdown_event` (a `threading.Event`, or None to run to completion
    unconditionally — the direct-call/test default) closes a startup/shutdown
    race: a bring-up (`ensure_background`) only registers its child into
    `engine_host._children` AFTER `_spawn` returns, which can take up to
    `BOOTSTRAP_TIMEOUT_S` (120s); if the server starts shutting down during
    that window, `engine_host.stop_all()` walks a `_children` that does not
    yet hold this child, and the child would land in the registry — and keep
    running — only after `stop_all()` has already finished. Checked BEFORE
    each folder (skip starting more once shutdown has begun) and AFTER each
    `ensure_background` call (stop whatever just came up, if shutdown began
    while it was spawning) — the same thread that is already blocked in the
    (possibly slow) spawn call does its own cleanup immediately on return,
    with no separate pass needed."""
    from fused_render.server import engine_host

    for folder in autostart_paths():
        if shutdown_event is not None and shutdown_event.is_set():
            return  # server is shutting down: stop starting more apps
        try:
            manifest = load_manifest(folder)
            if manifest is None:
                logger.warning(
                    "background app %s: no valid manifest at startup, skipping",
                    folder)
                continue
            interpreter = interpreter_for(folder)
            if not os.path.isfile(interpreter):
                logger.warning(
                    "background app %s: project environment not built yet, "
                    "skipping (open it once to install it)", folder)
                continue
            version = version_for(folder, interpreter)
            engine_id = engine_id_for(folder)
            daemon, module = bring_up_args(manifest)
            engine_host.ensure_background(
                engine_id, interpreter, daemon,
                cache_dir_for(engine_id), version, folder,
                idle_timeout_s=manifest.idle_timeout_s, module=module)
            if shutdown_event is not None and shutdown_event.is_set():
                # Shutdown began WHILE this one was spawning — stop_all() may
                # already have run (and missed it, per the docstring above),
                # so tear it down explicitly rather than leave an orphan with
                # no owner.
                logger.info(
                    "background app %s: server is shutting down; stopping "
                    "the child that just finished spawning", folder)
                engine_host.stop(engine_id)
        except Exception:  # noqa: BLE001 — one folder's failure must not skip the rest
            logger.exception(
                "background app %s failed to start at startup", folder)
