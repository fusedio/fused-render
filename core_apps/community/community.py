"""Backend for the Community marketplace sub-app (index.html).

One bare `main(action=...)` dispatcher, called via fused.runPython. Runs in
the executor's user-code subprocess, which deliberately cannot import
fused_render — so the few pieces of shell logic it needs (home dir, workspace
dir, atomic dir-claim, git-init-with-first-commit) are vendored here, matching
shell/storage.home_dir, shell/seed.fused_dir, zip_import.move_into_new_dir and
app_git.init_repo behavior (see docs/COMMUNITY_MARKETPLACE_SPEC.md §3).

Actions:
  catalog   — cached index.json joined with installs.json ({status:"no-cache"}
              before the first refresh; never touches the network)
  refresh   — clone (first run) or fetch+ff the community repo cache, sparse-
              checkout the browse set (index.json, */preview.png, */metadata.json),
              then return the same payload as `catalog`
  detail    — materialize one app folder in the cache; return its readme text
              + install state (the page renders the markdown client-side)
  install   — copy the cached app folder into <workspace>/local/<slug>,
              git-init it with a pristine first commit, record the install
  update    — clean copy: replace + commit on top; edited copy: refuse with
              {status:"dirty"} unless force=true (which commits local edits
              first so nothing is lost from history)
  uninstall — move the installed folder to the Trash, drop the record
  touch     — record that an app was opened (preview or installed copy);
              feeds the "last opened" ordering of community cards

State lives under ~/.fused-render/community/ (repo/ cache + installs.json);
the mount this file is served from is read-only. Every git call runs with
GIT_TERMINAL_PROMPT=0 and a timeout well under the executor's 60 s cap — a
first clone of a large catalog that can't finish in time surfaces as a
friendly retry error rather than a hang. If the catalog outgrows that budget,
the upgrade path is a detached worker reporting through fused.trackJob
(SPEC "Long-running work"); not needed at current sizes.
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
CACHE_REPO = os.path.join(STATE_DIR, "repo")
INSTALLS_JSON = os.path.join(STATE_DIR, "installs.json")
OPENED_JSON = os.path.join(STATE_DIR, "opened.json")
LOCK_PATH = os.path.join(STATE_DIR, ".lock")

# Mirrors shell/seed.fused_dir().
WORKSPACE = os.path.abspath(
    os.path.expanduser(os.environ.get("FUSED_RENDER_DIR") or "~/Documents/Fused"))
COMMUNITY_TAG_DIR = os.path.join(WORKSPACE, "local")

# The always-materialized browse set: catalog + every app's card assets.
SPARSE_BROWSE = ["/index.json", "/*/preview.png", "/*/metadata.json"]

GIT_TIMEOUT = 45  # < the executor's 60 s kill; clone is the longest call
LOCK_TIMEOUT = 50  # generous under the executor's 60 s hard kill
IDENTITY = ["-c", "user.name=Fused", "-c", "user.email=apps@fused.io"]
GITIGNORE = "*.html.json\n.claude-split.json\n.venv/\n"


class ActionError(Exception):
    """A user-facing failure: message is shown verbatim in the page."""


@contextlib.contextmanager
def _cache_lock():
    """Serialize every action that touches the on-disk cache repo (refresh,
    detail, install, update) across concurrent runPython calls — the browse
    page's background refresh, a detail fetch on card-open, and a write can
    all be in flight at once against the same repo otherwise. An OS advisory
    lock (not a Python-level one: each call may run in its own subprocess)
    also makes `_remove_stale_locks` safe: once this is held, no other
    action can be concurrently touching the repo, so any git *.lock file
    found on entry is from a process that's already gone, not a live one."""
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
                          "network and hit Refresh to retry")


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


def _remove_stale_locks():
    """Drop leftover git lockfiles in the cache repo. A git process killed
    mid-operation (the executor's hard kill, app quit) leaves its .lock
    behind, and every later command dies with "Unable to create '….lock':
    File exists … remove the file manually to continue". The cache is
    managed exclusively by this module and its git calls are short-lived,
    so any lock seen on a retry is stale by definition."""
    import glob
    git_dir = os.path.join(CACHE_REPO, ".git")
    for lock in (glob.glob(os.path.join(git_dir, "*.lock"))
                 + glob.glob(os.path.join(git_dir, "info", "*.lock"))
                 + glob.glob(os.path.join(git_dir, "refs", "**", "*.lock"),
                             recursive=True)):
        try:
            os.unlink(lock)
        except OSError:
            pass


def _sparse_add(*patterns):
    """sparse-checkout add, deduped and self-healing.

    Deduped: `git sparse-checkout add` appends to the pattern file blindly,
    so re-adding on every detail/refresh grows it without bound — skip
    patterns already present. Self-healing: the add fails on a stale
    lockfile (git killed mid-sync) or on preview droppings in the tree;
    both are throwaway by doctrine, so clear and retry once instead of
    surfacing "remove the file manually to continue" to the user."""
    r = _git(CACHE_REPO, "sparse-checkout", "list")
    have = r.stdout.splitlines() if r.returncode == 0 else []
    seen = set(have)
    missing = [p for p in patterns if p not in seen]
    if len(have) != len(seen):
        # Compact a pattern file bloated by pre-dedupe blind adds: `set`
        # rewrites it wholesale with the unique patterns (order preserved).
        cmd = ("sparse-checkout", "set", "--no-cone",
               *dict.fromkeys(have + missing))
    elif missing:
        cmd = ("sparse-checkout", "add", *missing)
    else:
        return
    if _git(CACHE_REPO, *cmd).returncode == 0:
        return
    _remove_stale_locks()
    _clean_cache()
    _git_ok(CACHE_REPO, *cmd, what=f"sparse-checkout {cmd[1]}")


def _cache_ready():
    return os.path.isdir(os.path.join(CACHE_REPO, ".git"))


def _read_index():
    try:
        with open(os.path.join(CACHE_REPO, "index.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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
    index = _read_index()
    if index is None:
        return {"status": "no-cache"}
    installs = _read_installs()
    opened = _read_opened()
    by_slug = {a.get("slug"): a for a in index.get("apps", [])}
    apps = []
    for entry in index.get("apps", []):
        slug = entry.get("slug")
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
    return {
        "status": "ok",
        "generated_at": index.get("generated_at"),
        "commit": index.get("commit"),
        "cache_root": CACHE_REPO,
        "apps": apps,
    }


def _refresh():
    os.makedirs(STATE_DIR, exist_ok=True)
    if not _cache_ready():
        # Fresh clone: metadata only, then materialize just the browse set.
        if os.path.isdir(CACHE_REPO):
            shutil.rmtree(CACHE_REPO, ignore_errors=True)
        r = _git(STATE_DIR, "clone", "--filter=blob:none", "--no-checkout",
                 "--", REPO_URL, CACHE_REPO)
        if r.returncode != 0:
            shutil.rmtree(CACHE_REPO, ignore_errors=True)  # no half-clones
            detail = (r.stderr or "").strip().splitlines()
            raise ActionError("could not fetch the community catalog: "
                              f"{detail[-1] if detail else 'clone failed'}")
        try:
            _git_ok(CACHE_REPO, "sparse-checkout", "set", "--no-cone", *SPARSE_BROWSE,
                    what="sparse-checkout")
            _git_ok(CACHE_REPO, "checkout", what="checkout")
        except ActionError:
            # Half-set-up clone: no index.json, and a later refresh would
            # take the fetch/merge branch and never re-run this setup —
            # drop it entirely so the next refresh re-clones from scratch.
            shutil.rmtree(CACHE_REPO, ignore_errors=True)
            raise
    else:
        _remove_stale_locks()
        _clean_cache()
        # Re-assert the browse patterns on every refresh: a cache cloned under
        # an older pattern set (e.g. when cards used icon.svg) would otherwise
        # keep it forever — fetch+ff never rewrites sparse-checkout config.
        # `add`, not `set`: set would drop the per-app /<slug>/ patterns that
        # _materialize appended, de-materializing every previewed app. Stale
        # old patterns linger harmlessly (they match nothing once the files
        # leave the repo). Idempotent and cheap when nothing changed.
        _sparse_add(*SPARSE_BROWSE)
        _git_ok(CACHE_REPO, "fetch", "--", "origin", what="fetch")
        # ff-only: the cache is managed, never edited, so a non-ff means the
        # upstream rewrote history — re-clone is the recovery, not a merge.
        r = _git(CACHE_REPO, "merge", "--ff-only", "FETCH_HEAD")
        if r.returncode != 0:
            shutil.rmtree(CACHE_REPO, ignore_errors=True)
            return _refresh()
    return _catalog_payload()


def _materialize(slug):
    """Ensure the app folder exists in the cache working tree (lazy blobs)."""
    _require_slug(slug)
    folder = os.path.join(CACHE_REPO, slug)
    if not _cache_ready():
        raise ActionError("catalog cache is missing — hit Refresh first")
    # No --no-cone here: `add` inherits the non-cone mode `set` established
    # (and rejects the flag on some git versions).
    _sparse_add(f"/{slug}/")
    if not os.path.isdir(folder):
        # Pattern present but folder absent (e.g. a past add wrote the
        # pattern, then failed applying it): dedupe above skipped the add,
        # so force a working-tree reapply before concluding the app is gone.
        _remove_stale_locks()
        _clean_cache()
        _git(CACHE_REPO, "sparse-checkout", "reapply")
    if not os.path.isdir(folder):
        raise ActionError(f"app {slug!r} is not in the catalog — refresh and retry")
    return folder


def _require_slug(slug):
    # Same shape CI enforces repo-side; also keeps a crafted slug from ever
    # forming a path outside the cache/workspace.
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug or ""):
        raise ActionError(f"invalid app slug: {slug!r}")


def _clean_cache():
    """Reset the cache working tree to HEAD: drop tracked-file edits and
    remove untracked files. Preview renders live from this tree, so a
    previewed app may have written next to itself (a JSON store, a sqlite db,
    ./.cache) — that state is deliberately throwaway, and this is where it
    dies: on refresh, and before anything is copied out (install/update).
    Want preview state to survive? Install the app; the copy in
    Fused/local/ is yours."""
    _git(CACHE_REPO, "checkout", "-q", "--", ".")
    _git(CACHE_REPO, "clean", "-qfdx")


def _catalog_entry(slug):
    index = _read_index() or {}
    for entry in index.get("apps", []):
        if entry.get("slug") == slug:
            return entry
    return None


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
    src = _materialize(slug)
    entry = _catalog_entry(slug) or {}
    os.makedirs(COMMUNITY_TAG_DIR, exist_ok=True)
    # Stage inside the destination tag dir so the final claim is a same-
    # filesystem rename (a home-dir staging dir could sit on another volume).
    staging = tempfile.mkdtemp(dir=COMMUNITY_TAG_DIR, prefix=f".install-{slug}-")
    try:
        stage_app = os.path.join(staging, slug)
        _clean_cache()   # never copy preview droppings out of the cache
        shutil.copytree(src, stage_app)
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
    src = _materialize(slug)
    _clean_cache()   # never copy preview droppings out of the cache
    _replace_contents(app_dir, src)
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


def _detail(slug):
    _require_slug(slug)
    entry = _catalog_entry(slug)
    installs = _read_installs()
    if entry is None and slug in installs["installs"]:
        # Yanked upstream: gone from the cache repo's tree, so `_materialize`
        # would always fail here. The install record is what still matters —
        # skip the cache entirely so Open/Uninstall keep working.
        return {
            "slug": slug, "entry": None, "readme": "", "folder": None,
            "preview_entry": None, "yanked": True,
            **_install_state(slug, None, installs),
        }
    folder = _materialize(slug)
    readme = ""
    try:
        with open(os.path.join(folder, "readme.md"), encoding="utf-8") as f:
            readme = f.read()
    except OSError:
        pass
    return {
        "slug": slug,
        "entry": entry,
        "readme": readme,
        "folder": folder,
        "preview_entry": os.path.join(folder, "index.html"),
        **_install_state(slug, entry, installs),
    }


def main(action: str = "catalog", slug: str = "", force: bool = False):
    try:
        if action == "catalog":
            return _catalog_payload()
        # Everything below touches the cache repo's git state — serialize so
        # a background refresh, a detail fetch, and a write can never race
        # on the same on-disk repo.
        if action == "refresh":
            with _cache_lock():
                return _refresh()
        if action == "detail":
            with _cache_lock():
                return _detail(slug)
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
