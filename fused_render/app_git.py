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

Subprocess discipline: `git -C <dir>` instead of cwd=, close_fds=False, no
start_new_session — keeps Popen on the posix_spawn path (no fork), because the
server process has libproj resident and fork() runs PROJ's atfork handler into
a SIGSEGV (see apps.py's _SESSION_HELPER comment).
"""
import logging
import os
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


def _git(app_dir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", app_dir, *_IDENTITY, *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT, close_fds=False,
    )


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
        logger.debug("init_repo failed for %s", app_dir, exc_info=True)
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
            logger.debug("app commit skipped (%s): add failed: %s",
                         app_dir, (r.stderr or "").strip())
            return False
        # Nothing staged (e.g. the change was to an ignored sidecar): no
        # commit, and no error either.
        if _git(app_dir, "diff", "--cached", "--quiet").returncode == 0:
            return False
        r = _git_retry_lock(app_dir, "commit", "-q", "-m", message or "Update")
        if r.returncode != 0:
            logger.debug("app commit skipped (%s): commit failed: %s",
                         app_dir, (r.stderr or "").strip())
            return False
        return True
    except Exception:
        logger.debug("app commit skipped (%s): unexpected", path, exc_info=True)
        return False
