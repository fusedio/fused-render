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
              own metadata.json) joined with installs.json
              ({status:"no-cache"} before the first refresh; never touches
              the network)
  refresh   — clone (first run) or fetch+ff the community repo into
              <workspace>/showcase, then return the same payload as `catalog`
  install   — copy the showcase app folder into <workspace>/local/<slug>,
              git-init it with a pristine first commit, record the install
  update    — clean copy: replace + commit on top; edited copy: refuse with
              {status:"dirty"} unless force=true (which commits local edits
              first so nothing is lost from history)
  uninstall — move the installed folder to the Trash, drop the record
  touch     — record that an app was opened (preview or installed copy);
              feeds the "last opened" ordering of community cards

The repo is a FULL clone living inside the user's workspace
(~/Documents/Fused/showcase). It is the user's tree: apps opened from it are
editable in place, and nothing here ever resets or deletes it — a refresh
that can't fast-forward (local edits conflict, upstream rewrote history)
keeps the local tree as-is and still serves the catalog. Cloning an app
copies its CURRENT state, edits included; that's the point of "edit in the
showcase, clone to keep".

Bookkeeping (installs.json, opened.json) stays under ~/.fused-render/community/.
Every git call runs with GIT_TERMINAL_PROMPT=0 and a bounded timeout — a
first clone that can't finish in time surfaces as a friendly retry error
rather than a hang.
"""
import contextlib
import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

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
INSTALLS_JSON = os.path.join(STATE_DIR, "installs.json")
OPENED_JSON = os.path.join(STATE_DIR, "opened.json")
LOCK_PATH = os.path.join(STATE_DIR, ".lock")

# Mirrors shell/seed.fused_dir().
WORKSPACE = os.path.abspath(
    os.path.expanduser(os.environ.get("FUSED_RENDER_DIR") or "~/Documents/Fused"))
COMMUNITY_TAG_DIR = os.path.join(WORKSPACE, "local")
# The full clone of the community repo, inside the user's workspace so the
# explorer lists it like any other folder and apps are editable in place.
SHOWCASE_DIR = os.path.join(WORKSPACE, "showcase")

GIT_TIMEOUT = 45  # bounded so a bad network surfaces as an error
CLONE_TIMEOUT = 180  # the full clone (every app + preview.png) is the long call
# Longer than CLONE_TIMEOUT on purpose: an install (Clone) racing the first
# background clone should wait it out and then succeed, not die "busy" at 50s
# while the clone is still legitimately holding the lock.
LOCK_TIMEOUT = CLONE_TIMEOUT + 20
IDENTITY = ["-c", "user.name=Fused", "-c", "user.email=apps@fused.io"]
GITIGNORE = "*.html.json\n.claude-split.json\n.venv/\n"


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
            ["git", "-C", cwd, *IDENTITY, *args],
            capture_output=True, text=True, timeout=timeout, env=env,
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


def _read_installs():
    try:
        with open(INSTALLS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data.get("installs"), dict) else {"schema": 1, "installs": {}}
    except (OSError, ValueError):
        return {"schema": 1, "installs": {}}


def _write_installs(data):
    os.makedirs(STATE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix=".installs-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, INSTALLS_JSON)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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



# A git lockfile this old cannot belong to a live operation — real git calls
# hold their locks for seconds, ours are bounded by CLONE_TIMEOUT. Anything
# younger is left alone: the showcase clone is the USER's tree, and a fresh
# index.lock may be their own git command (IDE, terminal) in flight.
STALE_LOCK_AGE = 3600


def _remove_stale_locks():
    """Drop leftover git lockfiles in the showcase clone. A git process killed
    mid-operation (a hard kill, app quit) leaves its .lock behind, and every
    later command dies with "Unable to create '….lock': File exists … remove
    the file manually to continue". Because the clone is a user-editable
    workspace tree, a lock here is NOT stale by definition — only ones old
    enough (STALE_LOCK_AGE) that no live git process can be holding them are
    removed; a fresh lock just makes this refresh's fetch/merge fail, and the
    next one retries."""
    import glob
    git_dir = os.path.join(SHOWCASE_DIR, ".git")
    cutoff = time.time() - STALE_LOCK_AGE
    for lock in (glob.glob(os.path.join(git_dir, "*.lock"))
                 + glob.glob(os.path.join(git_dir, "info", "*.lock"))
                 + glob.glob(os.path.join(git_dir, "refs", "**", "*.lock"),
                             recursive=True)):
        try:
            if os.path.getmtime(lock) <= cutoff:
                os.unlink(lock)
        except OSError:
            pass


def _cache_ready():
    return os.path.isdir(os.path.join(SHOWCASE_DIR, ".git"))


def _read_metadata(slug):
    """The app folder's own metadata.json, or None when absent/broken."""
    try:
        with open(os.path.join(SHOWCASE_DIR, slug, "metadata.json"),
                  encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _app_commit(slug):
    """Last commit that touched the app's folder — the per-app freshness
    marker installs.json records and update detection compares against."""
    r = _git(SHOWCASE_DIR, "log", "-1", "--format=%H", "--", slug)
    return r.stdout.strip() or None if r.returncode == 0 else None


def _entry(slug, meta):
    return {**meta, "slug": slug, "commit": _app_commit(slug)}


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
            # Also skips .git, hidden dirs, and anything install/update could
            # never target (they validate slugs with the same pattern).
            continue
        meta = _read_metadata(name)
        if meta is not None:
            apps.append(_entry(name, meta))
    return apps


def _install_state(slug, entry, installs):
    rec = installs["installs"].get(slug)
    if not rec:
        return {"installed": False}
    path = rec.get("path", "")
    if not os.path.isdir(path):
        # Folder gone (user deleted it by hand) — treat as not installed.
        return {"installed": False, "missing_record": True}
    return {
        "installed": True,
        "path": path,
        "installed_commit": rec.get("commit"),
        "installed_at": rec.get("installed_at"),
        "update_available": bool(entry and entry.get("commit")
                                 and entry.get("commit") != rec.get("commit")),
    }


def _catalog_payload():
    if not _cache_ready():
        return {"status": "no-cache"}
    installs = _read_installs()
    opened = _read_opened()
    entries = _scan_catalog()
    by_slug = {a["slug"]: a for a in entries}
    apps = []
    for entry in entries:
        slug = entry["slug"]
        apps.append({**entry, **_install_state(slug, entry, installs),
                     "opened_at": opened.get(slug)})
    # Installed apps whose slug vanished from the catalog (yanked upstream):
    # still list them, marked, so Uninstall/Open keep working.
    for slug, rec in installs["installs"].items():
        if slug in by_slug or not os.path.isdir(rec.get("path", "")):
            continue
        apps.append({"slug": slug, "name": slug, "description": "",
                     "yanked": True, "opened_at": opened.get(slug),
                     **_install_state(slug, None, installs)})
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
    _clear_stale_staging()
    if not _cache_ready():
        if os.path.exists(SHOWCASE_DIR):
            # A showcase folder that isn't our clone (user-made, or a clone
            # whose .git was stripped) is the user's — never delete it.
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
    else:
        # OUR clone only: a git repo the user put at this path themselves
        # (tracking some other remote) must not have its locks yanked, its
        # remote fetched, or its files fast-forwarded.
        r = _git(SHOWCASE_DIR, "remote", "get-url", "origin")
        if r.returncode != 0 or r.stdout.strip() != REPO_URL:
            raise ActionError(
                f"{SHOWCASE_DIR} is a git repo but not the showcase clone — "
                "move it aside to let the catalog sync")
        _remove_stale_locks()
        _git_ok(SHOWCASE_DIR, "fetch", "--", "origin", what="fetch",
                timeout=CLONE_TIMEOUT)
        # ff-only, best-effort: the tree is the USER's (apps are editable in
        # place), so a merge that can't fast-forward — local edits conflict,
        # upstream rewrote history — keeps the local tree untouched and still
        # serves the catalog. Never reset, never re-clone over user files.
        _git(SHOWCASE_DIR, "merge", "--ff-only", "FETCH_HEAD")
    return _catalog_payload()


def _app_folder(slug):
    """The app's folder in the showcase clone (full clone: always on disk)."""
    _require_slug(slug)
    folder = os.path.join(SHOWCASE_DIR, slug)
    if not _cache_ready():
        raise ActionError("the showcase clone is missing — open the apps "
                          "page to clone it, then retry")
    if not os.path.isdir(folder):
        raise ActionError(f"app {slug!r} is not in the catalog — refresh and retry")
    return folder


def _is_slug(slug):
    # Same shape CI enforces repo-side; also keeps a crafted slug from ever
    # forming a path outside the clone/workspace.
    import re
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug or ""))


def _require_slug(slug):
    if not _is_slug(slug):
        raise ActionError(f"invalid app slug: {slug!r}")


def _catalog_entry(slug):
    meta = _read_metadata(slug)
    return _entry(slug, meta) if meta is not None else None


def _claim_dir(staging, dest_base):
    """Atomic-claim `staging` as `dest_base` by rename, retrying -2..-9 on
    collision (the move_into_new_dir idiom — os.rename, never shutil.move)."""
    candidates = [dest_base] + [f"{dest_base}-{i}" for i in range(2, 10)]
    for dest in candidates:
        try:
            os.rename(staging, dest)
            return dest
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
                continue
            raise
    raise ActionError("could not claim an install folder (too many name collisions)")


def _stamp_fused_meta(app_dir):
    """Make sure the installed copy's entry page carries the
    `<meta name="fused-app">` marker — the only thing the listing trusts
    (D301). The showcase clone itself is synced from the community repo and is
    never edited here; the tag is expected upstream, and this covers the window
    (and any straggler app) where it isn't there yet. Best-effort like the rest
    of install/update — a page that can't be stamped is skipped, not fatal."""
    from fused_render import meta_migration

    entry = meta_migration._legacy_entry(app_dir)
    if entry is not None:
        meta_migration.stamp_entry(entry)


def _init_repo(app_dir):
    _git_ok(app_dir, "init", "-q", what="git init")
    gi = os.path.join(app_dir, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w", encoding="utf-8") as f:
            f.write(GITIGNORE)
    _git_ok(app_dir, "add", "-A", what="git add")
    _git_ok(app_dir, "commit", "-q", "-m", "Install from community",
            what="git commit")


def _install(slug):
    _require_slug(slug)
    installs = _read_installs()
    state = _install_state(slug, None, installs)
    if state.get("installed"):
        return {"status": "already-installed", "path": state["path"]}
    src = _app_folder(slug)
    entry = _catalog_entry(slug) or {}
    os.makedirs(COMMUNITY_TAG_DIR, exist_ok=True)
    # Stage inside the destination tag dir so the final claim is a same-
    # filesystem rename (a home-dir staging dir could sit on another volume).
    # Copies the folder's CURRENT state — showcase edits ride along, which is
    # the point of "edit in the showcase, clone to keep".
    staging = tempfile.mkdtemp(dir=COMMUNITY_TAG_DIR, prefix=f".install-{slug}-")
    try:
        stage_app = os.path.join(staging, slug)
        shutil.copytree(src, stage_app)
        _stamp_fused_meta(stage_app)
        dest = _claim_dir(stage_app, os.path.join(COMMUNITY_TAG_DIR, slug))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    _init_repo(dest)
    installs["installs"][slug] = {
        "path": dest,
        "commit": entry.get("commit"),
        "local_commit": _head_sha(dest),
        "version": entry.get("version"),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_installs(installs)
    return {"status": "installed", "path": dest}


def _head_sha(app_dir):
    r = _git(app_dir, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def _worktree_clean(app_dir, rec=None):
    r = _git(app_dir, "status", "--porcelain")
    if r.returncode != 0:
        return False  # no repo / broken repo — treat as edited, never clobber
    if r.stdout.strip():
        return False
    head = _head_sha(app_dir)
    if head is None:
        return False
    pristine = (rec or {}).get("local_commit")
    if pristine:
        # Clean means HEAD is exactly the last commit WE made (install or
        # update) — any commit beyond it is a user edit, however it landed.
        return head == pristine
    # Installs from before local_commit was recorded: the only commit we ever
    # made is the install commit, so a single-commit history means untouched.
    count = _git(app_dir, "rev-list", "--count", "HEAD")
    return count.returncode == 0 and count.stdout.strip() == "1"


def _replace_contents(app_dir, src):
    """Swap everything except .git (and the local .gitignore) for `src`'s files."""
    keep = {".git", ".gitignore"}
    for name in os.listdir(app_dir):
        if name in keep:
            continue
        path = os.path.join(app_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(app_dir, name)
        if os.path.isdir(s):
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def _update(slug, force):
    _require_slug(slug)
    installs = _read_installs()
    rec = installs["installs"].get(slug)
    if not rec or not os.path.isdir(rec.get("path", "")):
        raise ActionError(f"{slug!r} is not installed")
    app_dir = rec["path"]
    entry = _catalog_entry(slug)
    if entry is None:
        raise ActionError(f"{slug!r} is no longer in the catalog — no update to apply")
    if entry.get("commit") == rec.get("commit"):
        return {"status": "up-to-date"}
    clean = _worktree_clean(app_dir, rec)
    if not clean and not force:
        return {"status": "dirty"}
    if not clean:
        # Nothing is ever lost from history: land the user's current state
        # first, then apply upstream on top. Abort instead of silently
        # dropping edits if the snapshot commit can't be made (lock
        # contention, hooks, global gpgsign, ...).
        _git_ok(app_dir, "add", "-A", what="git add")
        _git_ok(app_dir, "commit", "-q", "-m",
                "Local edits before community update", what="git commit")
    src = _app_folder(slug)
    _replace_contents(app_dir, src)
    # Re-stamp: the replace copies the showcase's (possibly untagged) files
    # over the install's tagged entry, and an update that strips the marker
    # makes the app vanish from the listing.
    _stamp_fused_meta(app_dir)
    _git_ok(app_dir, "add", "-A", what="git add")
    # An update that changes nothing (sha moved but files identical) leaves
    # nothing staged; that's fine — record the new commit either way.
    if _git(app_dir, "diff", "--cached", "--quiet").returncode != 0:
        _git_ok(app_dir, "commit", "-q", "-m",
                f"Update to community {str(entry.get('commit'))[:12]}",
                what="git commit")
    rec["commit"] = entry.get("commit")
    rec["local_commit"] = _head_sha(app_dir)
    rec["version"] = entry.get("version")
    _write_installs(installs)
    return {"status": "updated", "path": app_dir}


def _trash(path):
    """Move to the user's Trash (macOS) or a local holding dir elsewhere —
    same doctrine as the disk-usage app: never a hard rm."""
    trash_dir = os.path.expanduser("~/.Trash")
    if not os.path.isdir(trash_dir):
        trash_dir = os.path.join(STATE_DIR, "trash")
        os.makedirs(trash_dir, exist_ok=True)
    base = os.path.basename(path.rstrip(os.sep))
    dest = os.path.join(trash_dir, base)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(trash_dir, f"{base}-{n}")
        n += 1
    shutil.move(path, dest)
    return dest


def _uninstall(slug):
    _require_slug(slug)
    installs = _read_installs()
    rec = installs["installs"].pop(slug, None)
    if rec is None:
        return {"status": "not-installed"}
    trashed = None
    if os.path.isdir(rec.get("path", "")):
        trashed = _trash(rec["path"])
    _write_installs(installs)
    return {"status": "uninstalled", "trashed_to": trashed}


def refresh_in_background():
    """Fire-and-forget showcase clone/sync, called by the server entry points
    (cli._run_serve, app._start_server_thread) right after ensure_fused_dir —
    NOT from create_app, so importing the server in tests never clones into a
    real workspace. First run performs the full clone; later runs fetch+ff.
    Failures are logged, never raised — while the clone is missing, every
    visit to the apps page escalates to its own refresh (Apps.tsx), so a
    failed startup clone retries there without waiting for the next process
    start. Once the clone exists, this startup sync is the only fetch."""
    import logging
    import threading

    def _run():
        res = main(action="refresh")
        if res.get("status") != "ok":
            logging.getLogger(__name__).warning(
                "showcase refresh failed: %s", res.get("message") or res.get("status"))

    threading.Thread(target=_run, daemon=True, name="showcase-refresh").start()


def main(action: str = "catalog", slug: str = "", force: bool = False):
    try:
        if action == "catalog":
            return _catalog_payload()
        # Everything below touches the showcase clone's git state — serialize
        # so a background refresh and a write can never race on the same
        # on-disk repo.
        if action == "refresh":
            with _cache_lock():
                return _refresh()
        if action == "install":
            with _cache_lock():
                return _install(slug)
        if action == "update":
            with _cache_lock():
                return _update(slug, force)
        if action == "uninstall":
            return _uninstall(slug)
        if action == "touch":
            return _touch(slug)
        raise ActionError(f"unknown action {action!r}")
    except ActionError as exc:
        return {"status": "error", "message": str(exc)}
