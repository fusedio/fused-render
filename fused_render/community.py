"""Backend for the community marketplace (the /apps hub's Showcase tab).

One bare `main(action=...)` dispatcher, exposed by the server as
POST /api/community (server/routers/community.py). It began life as a mounted
core_app script run via fused.runPython, which could not import fused_render —
so the few pieces of shell logic it needs (home dir, workspace dir, atomic
dir-claim, git-init-with-first-commit) are vendored here, matching
shell/storage.home_dir, shell/seed.fused_dir, zip_import.move_into_new_dir and
app_git.init_repo behavior. That vendoring is kept: this module stays
self-contained and import-cheap, and the endpoint is a sync def so its git
subprocess calls run on FastAPI's threadpool, never the event loop.

Actions:
  catalog   — a scan of the showcase clone (one folder per app, each with its
              own metadata.json); {status:"no-cache"} before the clone
              exists, and never touches the network
  refresh   — clone the community repo into <workspace>/showcase if it isn't
              there yet, then return the same payload as `catalog`; once the
              clone exists this never fetches or syncs it again
  touch     — record that an app was opened; feeds the "last opened"
              ordering of community cards

The repo is a FULL clone living inside the user's workspace
(~/Fused/showcase). It is the user's tree: apps are edited in place, and
nothing here ever resets, deletes, fetches, or syncs it once cloned — that
is the whole point of "edit in the showcase" (there is no separate installed
copy to keep in sync). Opening an app there IS opening your copy.

Bookkeeping (opened.json) stays under ~/.fused-render/community/. Every git
call runs with GIT_TERMINAL_PROMPT=0 and a bounded timeout — a first clone
that can't finish in time surfaces as a friendly retry error rather than a
hang.
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from fused_render.shell.seed import fused_dir


# An ABSOLUTE git path is required to reach posix_spawn, not merely tidy: CPython
# forks unless `os.path.dirname(executable)` is truthy, and a fork in a process
# with libproj resident dies with SIGSEGV before exec (rc -11, no output, no
# exception). `close_fds=False` alone does NOT achieve this — see
# fused_render/server/gitignore.py and tests/test_git_posix_spawn.py.
_GIT_BIN = None


def _git_bin():
    global _GIT_BIN
    if _GIT_BIN is None:
        import shutil
        _GIT_BIN = shutil.which("git") or "git"
    return _GIT_BIN


REPO_URL = os.environ.get(
    "FUSED_RENDER_COMMUNITY_REPO",
    "https://github.com/fusedio/fused-render-community-apps.git",
)

# Mirrors shell/storage.home_dir()'s FUSED_RENDER_HOME override (branch
# nesting deliberately skipped — like core_apps/sessions, community state is
# shared across branches).
STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "community")
OPENED_JSON = os.path.join(STATE_DIR, "opened.json")
LOCK_PATH = os.path.join(STATE_DIR, ".lock")

# The one definition of the workspace, imported rather than mirrored — this
# used to be a hand-copied expanduser() that had to be kept in step by hand.
WORKSPACE = fused_dir()
# The full clone of the community repo, inside the user's workspace so the
# explorer lists it like any other folder and apps are editable in place.
SHOWCASE_DIR = os.path.join(WORKSPACE, "showcase")

GIT_TIMEOUT = 45  # bounded so a bad network surfaces as an error
CLONE_TIMEOUT = 180  # the full clone (every app + preview.png) is the long call
LOCK_TIMEOUT = CLONE_TIMEOUT + 20


class ActionError(Exception):
    """A user-facing failure: message is shown verbatim in the page."""


@contextlib.contextmanager
def _cache_lock():
    """Serialize every action that touches the showcase clone's git state
    (refresh, install, update) across concurrent requests — the browse page's
    background refresh and a clone can be in flight at once against the same
    repo otherwise. An OS advisory lock (not a Python-level one: each call may
    run in its own subprocess)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR)
    try:
        deadline = time.time() + LOCK_TIMEOUT
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    raise ActionError("the community catalog is busy with "
                                      "another request — try again in a moment")
                time.sleep(0.2)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _git(cwd, *args, timeout=GIT_TIMEOUT):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(
            [_git_bin(), "-C", cwd, *args],
            capture_output=True, text=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
            close_fds=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except FileNotFoundError:
        raise ActionError("git is not installed (or not on PATH) — the "
                          "community catalog needs it to sync")
    except subprocess.TimeoutExpired:
        raise ActionError("git timed out syncing the catalog — check your "
                          "network and revisit the apps page to retry")


def _git_ok(cwd, *args, what="", timeout=GIT_TIMEOUT):
    r = _git(cwd, *args, timeout=timeout)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        raise ActionError(f"{what or 'git ' + args[0]} failed: "
                          f"{detail[-1] if detail else 'unknown error'}")
    return r


def _read_opened():
    """{slug: epoch seconds of the last open} — best-effort, like installs."""
    try:
        with open(OPENED_JSON, encoding="utf-8") as f:
            data = json.load(f)
        opened = data.get("opened")
        return opened if isinstance(opened, dict) else {}
    except (OSError, ValueError):
        return {}


def _touch(slug):
    _require_slug(slug)
    opened = _read_opened()
    opened[slug] = time.time()
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".opened-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "opened": opened}, f, indent=2)
        os.replace(tmp, OPENED_JSON)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"status": "ok", "opened_at": opened[slug]}



def _cache_ready():
    """The showcase clone exists at SHOWCASE_DIR and is OURS (tracks
    REPO_URL). A foreign git repo the user placed at this path — tracking
    some other remote — is never treated as ready; refresh must refuse it,
    not silently adopt it."""
    if not os.path.isdir(os.path.join(SHOWCASE_DIR, ".git")):
        return False
    r = _git(SHOWCASE_DIR, "remote", "get-url", "origin")
    return r.returncode == 0 and r.stdout.strip() == REPO_URL


def _read_metadata(slug):
    """The app folder's own metadata.json, or None when absent/broken."""
    try:
        with open(os.path.join(SHOWCASE_DIR, slug, "metadata.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _entry(slug, meta):
    return {**meta, "slug": slug}


def _scan_catalog():
    """One catalog entry per app folder in the clone: any top-level directory
    carrying a metadata.json (skips .git, hidden dirs, README and friends)."""
    apps = []
    try:
        names = sorted(os.listdir(SHOWCASE_DIR))
    except OSError:
        return apps
    for name in names:
        if not _is_slug(name):
            # Also skips .git and hidden dirs (slugs validate with the same
            # pattern _require_slug enforces).
            continue
        meta = _read_metadata(name)
        if meta is not None:
            apps.append(_entry(name, meta))
    return apps


def _catalog_payload():
    if not _cache_ready():
        return {"status": "no-cache"}
    opened = _read_opened()
    entries = _scan_catalog()
    apps = [{**entry, "opened_at": opened.get(entry["slug"])} for entry in entries]
    head = _git(SHOWCASE_DIR, "rev-parse", "HEAD")
    return {
        "status": "ok",
        "commit": head.stdout.strip() if head.returncode == 0 else None,
        "cache_root": SHOWCASE_DIR,
        "apps": apps,
    }


def _clear_stale_staging():
    """Sweep leftover .showcase-clone-* staging dirs from interrupted first
    clones (app quit mid-clone: daemon thread, finally never ran). Runs under
    _cache_lock, so no clone can be legitimately staging right now — anything
    matching the prefix is dead weight in the user's workspace."""
    import glob
    for leftover in glob.glob(os.path.join(WORKSPACE, ".showcase-clone-*")):
        shutil.rmtree(leftover, ignore_errors=True)


def _refresh():
    """Clone the showcase repo if it isn't there yet; once it exists this is
    a no-op that just serves the catalog. The clone is the user's tree —
    apps are editable in place — so nothing here ever fetches, merges, or
    otherwise touches it again after the first clone."""
    _clear_stale_staging()
    if _cache_ready():
        return _catalog_payload()
    if os.path.exists(SHOWCASE_DIR):
        # A showcase folder that isn't our clone (user-made, a clone whose
        # .git was stripped, or a git repo tracking some other remote) is
        # the user's — never delete it, never fetch it, never touch its locks.
        raise ActionError(
            f"{SHOWCASE_DIR} exists but is not the showcase clone — "
            "move it aside to let the catalog sync")
    os.makedirs(WORKSPACE, exist_ok=True)
    # Clone into a hidden staging dir, then claim the final name with one
    # rename — no half-clone ever flashes up in the explorer listing.
    staging = tempfile.mkdtemp(dir=WORKSPACE, prefix=".showcase-clone-")
    try:
        r = _git(WORKSPACE, "clone", "--", REPO_URL,
                 os.path.join(staging, "showcase"), timeout=CLONE_TIMEOUT)
        if r.returncode != 0:
            detail = (r.stderr or "").strip().splitlines()
            raise ActionError("could not fetch the community catalog: "
                              f"{detail[-1] if detail else 'clone failed'}")
        os.rename(os.path.join(staging, "showcase"), SHOWCASE_DIR)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return _catalog_payload()


def _is_slug(slug):
    # Same shape CI enforces repo-side; also keeps a crafted slug from ever
    # forming a path outside the clone.
    import re
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug or ""))


def _require_slug(slug):
    if not _is_slug(slug):
        raise ActionError(f"invalid app slug: {slug!r}")


def ensure_showcase_in_background():
    """Fire-and-forget showcase clone, called by the server entry points
    (cli._run_serve, app._start_server_thread) right after ensure_fused_dir —
    NOT from create_app, so importing the server in tests never clones into a
    real workspace. Clones once, if the folder is missing; once the clone
    exists this is a no-op every subsequent start — the showcase is never
    fetched or synced again. Failures are logged, never raised — while the
    clone is missing, every visit to the apps page escalates to its own
    refresh (Apps.tsx), so a failed startup clone retries there without
    waiting for the next process start."""
    import logging
    import threading

    def _run():
        res = main(action="refresh")
        if res.get("status") != "ok":
            logging.getLogger(__name__).warning(
                "showcase clone failed: %s", res.get("message") or res.get("status"))

    threading.Thread(target=_run, daemon=True, name="showcase-clone").start()


def main(action: str = "catalog", slug: str = ""):
    try:
        if action == "catalog":
            return _catalog_payload()
        if action == "refresh":
            # Serialize so a background clone and a client-triggered refresh
            # can never race on the same on-disk repo.
            with _cache_lock():
                return _refresh()
        if action == "touch":
            return _touch(slug)
        raise ActionError(f"unknown action {action!r}")
    except ActionError as exc:
        return {"status": "error", "message": str(exc)}
