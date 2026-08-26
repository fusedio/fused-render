"""Background apps: a folder can declare a long-running daemon that this
server supervises — single instance, the folder's own venv, exempt from
idle-retire, killed with the server, and restarted at every server startup
while the user has it enabled (see engine_host.py's "background" child kind
and server/routers/background_apps.py).

A folder opts in with a manifest table in its own `pyproject.toml`:

    [tool.fused-render.app]
    kind = "background"
    daemon = "daemon.py"   # filename, resolved inside the folder

Nothing reads this table today — greenfield, following registered_apps.py's
containment-guard style: a `daemon` value that resolves outside the folder
(e.g. via `../`) is refused rather than trusted.

The enabled store (`<home_dir()>/background_apps.json`) is the user's sticky
"keep this running" list, following registered_apps.py's read/write
discipline: a folder that is temporarily missing or unreadable drops out of
`enabled_paths()` (read-only — it may come back), and only `set_enabled`
rewrites the store.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
import tomllib
from dataclasses import dataclass

from fused_render.index.ignore import MountGuard
from fused_render.shell import storage

logger = logging.getLogger(__name__)

#: engine_id prefix for a background app; the rest is a hash of the folder's
#: realpath (engine_host.py's `_ENGINE_ID` requires a bare identifier).
_ENGINE_ID_PREFIX = "bg_"


@dataclass(frozen=True)
class Manifest:
    #: Absolute path to the app folder (the `pyproject.toml`'s directory).
    folder: str
    #: Absolute path to the daemon file, guaranteed to resolve inside `folder`.
    daemon: str


def load_manifest(folder: str) -> Manifest | None:
    """The folder's background-app manifest, or None when the folder does not
    declare one, declares a different kind, or its `daemon` does not resolve
    to a file inside the folder. Never raises — a missing or corrupt
    `pyproject.toml`, or an unreadable folder, simply reads as "no manifest"."""
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
    if not isinstance(app, dict) or app.get("kind") != "background":
        return None
    daemon_name = app.get("daemon")
    if not isinstance(daemon_name, str) or not daemon_name:
        return None
    daemon = os.path.normpath(os.path.join(folder, daemon_name))
    # Containment, realpath-resolved (registered_apps.py's guard shape): a
    # `daemon` value that climbs out of the folder via `../` or a symlink must
    # not be trusted just because the string join looked contained.
    real_folder = os.path.realpath(folder)
    real_daemon = os.path.realpath(daemon)
    if real_daemon != real_folder and not real_daemon.startswith(real_folder + os.sep):
        return None
    return Manifest(folder=folder, daemon=daemon)


def engine_id_for(folder: str) -> str:
    """The stable engine_id for a background app's folder — same shape as
    `engine_host.app_engine_id`, distinct prefix so the two kinds can never
    collide. Keyed by realpath so a symlinked folder and its target share one
    engine (and thus one running instance)."""
    digest = hashlib.sha1(os.path.realpath(folder).encode("utf-8")).hexdigest()
    return _ENGINE_ID_PREFIX + digest[:12]


def version_for(folder: str, interpreter: str) -> str:
    """Digest of the manifest's declaring bytes, the daemon file's mtime/size,
    and the interpreter path. Changing any of the three must retire a running
    child rather than reuse it: a `pyproject.toml` edit, a daemon.py edit, or
    a bundled-CPython swap across an app upgrade (the interpreter is what
    fixes the OpenWhisper upgrade-rot class — a stale venv reused against a
    new interpreter).

    Raises OSError if the manifest is missing/invalid or the daemon file does
    not exist — a "dead manifest", which callers (the enable endpoint, the
    startup resurrection hook) must treat as a failure to skip, not fall back
    on a stale version for."""
    manifest = load_manifest(folder)
    if manifest is None:
        raise OSError(f"{folder!r} has no valid background-app manifest")
    with open(os.path.join(manifest.folder, "pyproject.toml"), "rb") as f:
        pyproject_bytes = f.read()
    st = os.stat(manifest.daemon)
    h = hashlib.sha256()
    h.update(pyproject_bytes)
    h.update(f"{st.st_mtime_ns}:{st.st_size}".encode("utf-8"))
    h.update(os.path.realpath(interpreter).encode("utf-8"))
    return h.hexdigest()


# --------------------------------------------------------- enabled store


def _store_path() -> str:
    return os.path.join(storage.home_dir(), "background_apps.json")


def enabled_paths() -> list[str]:
    """Absolute folder paths the user has enabled, in stored order. A folder
    that is behind a blocked mount, missing, or otherwise unreadable is
    skipped from the result (read-only — the store itself is untouched, so
    the folder reappears here the moment it's readable again)."""
    data = storage.read_json(_store_path())
    if not isinstance(data, dict):
        return []
    raw = data.get("enabled")
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
        out.append(os.path.abspath(path))
    return out


def set_enabled(path: str, enabled: bool) -> None:
    """Persist *path*'s enabled state. Idempotent: enabling an already-enabled
    path (or disabling an already-disabled one) is a no-op write, not a
    duplicate entry."""
    path = os.path.abspath(path)
    data = storage.read_json(_store_path())
    raw = data.get("enabled") if isinstance(data, dict) else None
    current = [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []
    current = [p for p in current if os.path.abspath(p) != path]
    if enabled:
        current.append(path)
    storage.write_json(_store_path(), {"enabled": current})


# --------------------------------------------------------- bring-up helpers


def interpreter_for(folder: str) -> str:
    """The interpreter a background app in *folder* runs on: the same
    fused-vs-builtin dispatch `/api/engine` uses for its warm worker
    (routers/app_engine.py) — the folder's own project venv python when the
    fused engine is effective, else this app's own `sys.executable`. Does not
    check the interpreter exists; callers do (the 409 stance, PY-17)."""
    from fused_render import projectenv
    from fused_render.shell import prefs as shell_prefs

    if shell_prefs.effective_engine() == "fused":
        project = projectenv.project_env_for(folder)
        return projectenv.interpreter_for(project)
    return sys.executable


def cache_dir_for(engine_id: str) -> str:
    """Where a background app's status/log files live — mirrors
    engine_host._app_cache_dir's "under the home dir, never beside the user's
    code" stance (MD-7)."""
    return os.path.join(storage.home_dir(), "apps", engine_id)


def resurrect_enabled(shutdown_event=None) -> None:
    """Start every enabled app's daemon, best-effort: a folder that fails
    (dead manifest, project venv not built, a spawn error) is logged and
    skipped, never allowed to stop the rest. Meant to run on a daemon thread
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

    for folder in enabled_paths():
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
            engine_host.ensure_background(
                engine_id, interpreter, manifest.daemon,
                cache_dir_for(engine_id), version)
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
