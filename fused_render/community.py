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

The repo is a full WORKING TREE (every app's files on disk, no sparse
checkout, no materialize step) living inside the user's workspace
(~/Fused/showcase), cloned SHALLOW — `--depth 1 --single-branch --no-tags`,
one commit of history. It is the user's tree: apps are edited in place, and
nothing here ever resets, deletes, fetches, or syncs it once cloned — that
is the whole point of "edit in the showcase" (there is no separate installed
copy to keep in sync). Opening an app there IS opening your copy.

The lock file (`.lock`, `_cache_lock`) stays under ~/.fused-render/community/.
Every git call runs with GIT_TERMINAL_PROMPT=0 and a bounded timeout — a
first clone that can't finish in time surfaces as a friendly retry error
rather than a hang. A machine with no usable git is refused BEFORE the clone
(`_require_git`), naming git as the requirement, since every surface that
would otherwise sit on "still downloading" forever renders that message.
"""
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

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
# nesting deliberately skipped — like routers/claude_sessions.py's STATE_DIR,
# community state is shared across branches).
STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "community")
LOCK_PATH = os.path.join(STATE_DIR, ".lock")

# The one definition of the workspace, imported rather than mirrored — this
# used to be a hand-copied expanduser() that had to be kept in step by hand.
WORKSPACE = fused_dir()
# The clone of the community repo, inside the user's workspace so the
# explorer lists it like any other folder and apps are editable in place.
SHOWCASE_DIR = os.path.join(WORKSPACE, "showcase")

GIT_TIMEOUT = 45  # bounded so a bad network surfaces as an error
# The clone (every app, with its preview still and assets) is the long call —
# tens of megabytes even shallow, on a first-visit request path.
CLONE_TIMEOUT = 180
LOCK_TIMEOUT = CLONE_TIMEOUT + 20


class ActionError(Exception):
    """A user-facing failure: message is shown verbatim in the page."""


@contextlib.contextmanager
def _cache_lock():
    """Serialize every action that touches the showcase clone's git state
    across concurrent requests — the browse page's background refresh and a
    clone can be in flight at once against the same repo otherwise. An OS
    advisory lock (not a Python-level one: each call may run in its own
    subprocess).

    This used to enumerate "(refresh, install, update)"; `install` and
    `update` were deleted from this module, so the list named two callers
    that no longer exist (finding 8, code review 2026-08-27). The rule is
    unchanged and is stated by kind rather than by caller now, so it cannot
    go stale the same way again."""
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



def _normalize_git_url(url):
    """A remote URL reduced to `(host, path)`, tolerant of the spellings
    that name the SAME remote without being the same string: a trailing
    `.git`, a trailing slash, and the ssh scp-like form
    (`git@host:org/repo`) versus an explicit scheme (`https://host/org/
    repo`, `ssh://git@host/org/repo`). Without this, `_cache_ready`'s exact
    string match reported a genuine clone as `no-cache` forever the moment
    its origin was authored differently than `REPO_URL` happens to be
    spelled today (an ssh remote against an https REPO_URL, a
    FUSED_RENDER_COMMUNITY_REPO override that changed form) — and
    `_refresh` then read that as "exists but is not the showcase clone",
    refusing on every visit. Host is case-folded (DNS is
    case-insensitive); the path is left as-is, since it can be
    case-sensitive depending on the host."""
    u = (url or "").strip()
    if not u:
        return ("", "")
    if "://" not in u and "@" in u and ":" in u.split("@", 1)[1]:
        # scp-like syntax: user@host:org/repo — the colon is a path
        # separator here, not a port (that form requires an explicit
        # scheme, handled by the branch below).
        userhost, _, path = u.partition(":")
        host = userhost.rsplit("@", 1)[-1]
    else:
        parsed = urlsplit(u)
        host = parsed.netloc.rsplit("@", 1)[-1]  # drop any userinfo
        path = parsed.path
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-len(".git")]
    return (host.lower(), path)


def _cache_ready():
    """The showcase clone exists at SHOWCASE_DIR and is OURS (tracks
    REPO_URL, allowing for the URL spellings `_normalize_git_url`
    tolerates). A foreign git repo the user placed at this path — tracking
    some other remote entirely — is never treated as ready; refresh must
    refuse it, not silently adopt it."""
    if not os.path.isdir(os.path.join(SHOWCASE_DIR, ".git")):
        return False
    r = _git(SHOWCASE_DIR, "remote", "get-url", "origin")
    return (r.returncode == 0
            and _normalize_git_url(r.stdout.strip()) == _normalize_git_url(REPO_URL))


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
            # pattern below).
            continue
        meta = _read_metadata(name)
        if meta is not None:
            apps.append(_entry(name, meta))
    return apps


def _catalog_payload():
    if not _cache_ready():
        return {"status": "no-cache"}
    apps = _scan_catalog()
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


def _git_install_hint():
    """The one-line "get git" instruction for this platform. macOS is called
    out on its own because /usr/bin/git is a shim that EXISTS on every Mac
    without the developer tools behind it — `which` finds it, so the missing
    piece is the toolchain, not a download."""
    if sys.platform == "darwin":
        return "run `xcode-select --install` to get it, then revisit this page"
    if sys.platform == "win32":
        return "install it from https://git-scm.com/download/win, then revisit this page"
    return ("install it with your package manager (for example `apt install "
            "git`), then revisit this page")


def _require_git():
    """Refuse the clone up front when git can't run, with a message that says
    so — the showcase is a git clone and there is no fallback.

    `git --version` is the probe, not `shutil.which`: on macOS /usr/bin/git is
    an xcode-select shim present on every machine whether or not the developer
    tools are installed, so `which` succeeds and only actually RUNNING git
    reveals `xcrun: error: invalid active developer path`. That failure used to
    reach the page (when it reached it at all) as a raw xcrun line under
    "could not fetch the community catalog"; a git that cannot report its own
    version is unambiguously unusable, so it is named as such here instead of
    pattern-matching stderr.

    Cheap enough to run on every clone attempt (milliseconds, once — the clone
    behind it is tens of seconds), and it never runs again after the first
    successful clone, since _refresh returns early then."""
    try:
        r = _git(WORKSPACE, "--version", timeout=15)
        ok = r.returncode == 0
    except ActionError:
        # _git already maps FileNotFoundError/timeout to a message, but both
        # mean the same thing here and this one names the consequence.
        ok = False
    if not ok:
        raise ActionError(
            "could not download the showcase apps: git is required to clone "
            f"them and it isn't available on this machine — {_git_install_hint()}")


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
    # Ahead of mkdtemp, so a machine without git leaves no staging dir behind
    # — and AFTER makedirs, since `_git` runs with `-C WORKSPACE` and a missing
    # cwd fails even `--version`.
    _require_git()
    # Clone into a hidden staging dir, then claim the final name with one
    # rename — no half-clone ever flashes up in the explorer listing.
    staging = tempfile.mkdtemp(dir=WORKSPACE, prefix=".showcase-clone-")
    try:
        # Shallow, one branch, no tags. Nobody here reads history: this
        # module clones once and never fetches again (D550), and the two
        # reads it does perform work fine at depth 1 (`rev-parse HEAD` for
        # the catalog's commit, `remote get-url origin` for _cache_ready).
        # Depth is worth more than it looks, and grows more so over time: the
        # repo's heaviest content is preview stills and app assets, and every
        # recompression pass upstream adds a slim blob without removing the
        # fat original from history — a full clone pays for both copies, a
        # shallow one only for HEAD. What is left after depth is the working
        # tree itself, which only the community repo can shrink.
        #
        # It stays an ordinary git work tree, which is what SPEC §33/GT-20
        # needs: `fetch` + `rev-list --left-right --count HEAD...origin/x` +
        # `pull --ff-only` all behave on a shallow clone, so the opt-in
        # Update row still reports and applies upstream commits. A user who
        # wants the full log runs `git fetch --unshallow` themselves.
        #
        # Not partial (`--filter=blob:none`): the tree is fully checked out,
        # so every blob gets fetched anyway — it measured barely better than
        # plain shallow, and the price is a promisor remote (a standing
        # network dependency) inside the user's editable tree.
        r = _git(WORKSPACE, "clone", "--depth", "1", "--single-branch",
                 "--no-tags", "--", REPO_URL,
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


def main(action: str = "catalog"):
    try:
        if action == "catalog":
            return _catalog_payload()
        if action == "refresh":
            # Serialize so a background clone and a client-triggered refresh
            # can never race on the same on-disk repo.
            with _cache_lock():
                return _refresh()
        raise ActionError(f"unknown action {action!r}")
    except ActionError as exc:
        return {"status": "error", "message": str(exc)}
