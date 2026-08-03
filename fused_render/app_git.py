"""Local version control for app folders (<workspace>/<tag>/<name>).

Every app scaffolded by POST /api/apps/new ships with a git repo and a single
boilerplate commit; after that, every change lands as its own small commit —
each completed Claude turn (templates/claude/agent.py mirrors the commit
helper here, since templates must not import fused_render, D166) and every
manual mutation made through the editor's /api/fs endpoints (fs_mutate.py,
debounced/serialized through app_commit_queue's single worker).

Everything here is BEST-EFFORT: git may be missing, the folder may not be a
repo (pre-feature apps, hand-made folders), a concurrent commit may hold
index.lock. None of that may ever fail the operation that triggered the
commit — a save that landed on disk is a success whether or not it was
recorded. Helpers return False/None instead of raising, and every skipped
commit says why at DEBUG level so a "why didn't this commit?" has an answer
in the server log. A missed commit is also not a lost change: the next
successful commit's `add -A` sweeps everything pending.

Commits are scoped HARD to app dirs: exactly two levels under fused_dir(),
with a `.git` of their own. A path anywhere else — including a user's real
repository opened in the editor — is never committed to.

Subprocess discipline: git runs through os.posix_spawnp directly, NEVER
subprocess. The server process gets libproj resident (importing the fused
engine pulls geopandas→pyproj, and prefs' availability probe does that
in-process), and from then on fork() runs PROJ's pthread_atfork child
handler into a SIGSEGV before exec — subprocess.Popen forks here on macOS,
so every `git add` died rc=-11 with empty stderr the moment the shell first
polled /api/prefs (see apps.py's _SESSION_HELPER comment for the same crash;
verified live in the field 2026-08-03). posix_spawn does not run atfork
handlers, so this path stays alive no matter what the process has loaded.
"""
import logging
import os
import selectors
import signal
import subprocess
import time

from fused_render.shell.seed import fused_dir

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30

# One short retry when another writer (a Claude turn's fallback sweep, a
# second editor save) holds index.lock. Best-effort still — after the retry
# the change just waits for the next commit's `add -A`.
_LOCK_RETRY_DELAY_S = 0.3

# Commit identity, passed per-invocation: an app folder is not the user's
# repo, and the machine may have no global git identity at all — a fresh
# machine must still get its boilerplate commit.
_IDENTITY = ["-c", "user.name=Fused", "-c", "user.email=apps@fused.io"]

# Session sidecars (<file>.json next to the entry html, agent.py) are chat
# bookkeeping, not app content — keep them out of history.
_GITIGNORE = "*.html.json\n"


def _spawn(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True) built on os.posix_spawnp — see the
    module docstring for why fork() (and therefore subprocess) is off-limits
    here. Kills the child and returns rc=-SIGKILL on timeout."""
    r_out, w_out = os.pipe()
    r_err, w_err = os.pipe()
    try:
        pid = os.posix_spawnp(
            argv[0], argv, dict(os.environ),
            file_actions=[
                (os.POSIX_SPAWN_DUP2, w_out, 1),
                (os.POSIX_SPAWN_DUP2, w_err, 2),
            ])
    except Exception:
        os.close(r_out)
        os.close(r_err)
        raise
    finally:
        os.close(w_out)
        os.close(w_err)
    buf = {r_out: bytearray(), r_err: bytearray()}
    sel = selectors.DefaultSelector()
    sel.register(r_out, selectors.EVENT_READ)
    sel.register(r_err, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0:
                os.kill(pid, signal.SIGKILL)
                break
            for key, _ in sel.select(left):
                chunk = os.read(key.fd, 65536)
                if chunk:
                    buf[key.fd] += chunk
                else:
                    sel.unregister(key.fd)
    finally:
        sel.close()
        os.close(r_out)
        os.close(r_err)
    rc = os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
    return subprocess.CompletedProcess(
        argv, rc,
        buf[r_out].decode("utf-8", "replace"),
        buf[r_err].decode("utf-8", "replace"))


def _git(app_dir: str, *args: str) -> subprocess.CompletedProcess:
    return _spawn(["git", "-C", app_dir, *_IDENTITY, *args], _GIT_TIMEOUT)


def _git_retry_lock(app_dir: str, *args: str) -> subprocess.CompletedProcess:
    """Like _git, with one retry when the failure is index.lock contention —
    the only failure that is expected to be gone milliseconds later."""
    r = _git(app_dir, *args)
    if r.returncode != 0 and "index.lock" in (r.stderr or ""):
        time.sleep(_LOCK_RETRY_DELAY_S)
        r = _git(app_dir, *args)
    return r


def app_dir_for(path: str) -> str | None:
    """The app folder containing `path`, or None when the path is not inside
    one. An app dir is exactly <fused_dir()>/<tag>/<name> — never the
    workspace root, a tag dir, or anything outside the workspace."""
    root = fused_dir()
    ap = os.path.abspath(path)
    if not ap.startswith(root + os.sep):
        return None
    rel = os.path.relpath(ap, root)
    parts = rel.split(os.sep)
    if len(parts) < 2 or parts[0].startswith(".") or parts[1].startswith("."):
        return None
    return os.path.join(root, parts[0], parts[1])


def init_repo(app_dir: str) -> bool:
    """git-init `app_dir` and land everything in it as one boilerplate commit.
    True on success; False (never an exception) when git is missing or any
    step fails — creation already succeeded and must stay that way."""
    try:
        if _git(app_dir, "init", "-q").returncode != 0:
            return False
        gi = os.path.join(app_dir, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write(_GITIGNORE)
        if _git(app_dir, "add", "-A").returncode != 0:
            return False
        return _git(app_dir, "commit", "-q", "-m",
                    "New app from starter").returncode == 0
    except Exception:
        logger.warning("init_repo failed for %s", app_dir, exc_info=True)
        return False


def commit(path: str, message: str) -> bool:
    """Commit everything pending in the app repo containing `path`.

    No-op (False) when the path is not inside an app dir, the app dir has no
    repo, there is nothing to commit, or git fails. True only when a commit
    was actually created."""
    try:
        app_dir = app_dir_for(path)
        if app_dir is None or not os.path.isdir(os.path.join(app_dir, ".git")):
            return False
        r = _git_retry_lock(app_dir, "add", "-A")
        if r.returncode != 0:
            logger.warning("app commit skipped (%s): add failed rc=%s "
                           "stdout=%r stderr=%r", app_dir, r.returncode,
                           (r.stdout or "").strip(), (r.stderr or "").strip())
            return False
        # Nothing staged (e.g. the change was to an ignored sidecar): no
        # commit, and no error either.
        if _git(app_dir, "diff", "--cached", "--quiet").returncode == 0:
            return False
        r = _git_retry_lock(app_dir, "commit", "-q", "-m", message or "Update")
        if r.returncode != 0:
            logger.warning("app commit skipped (%s): commit failed rc=%s "
                           "stdout=%r stderr=%r", app_dir, r.returncode,
                           (r.stdout or "").strip(), (r.stderr or "").strip())
            return False
        return True
    except Exception:
        logger.warning("app commit skipped (%s): unexpected", path, exc_info=True)
        return False
