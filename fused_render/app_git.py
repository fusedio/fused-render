"""Local version control for app folders (<workspace>/<tag>/<name>).

Every app scaffolded by POST /api/apps/new ships with a git repo and a single
boilerplate commit; after that, every change lands as its own small commit —
each completed Claude turn (templates/claude/agent.py mirrors the commit
helper here, since templates must not import fused_render, D166) and every
manual mutation made through the editor's /api/fs endpoints (fs_mutate.py).

Everything here is BEST-EFFORT: git may be missing, the folder may not be a
repo (pre-feature apps, hand-made folders), a concurrent commit may hold
index.lock. None of that may ever fail the operation that triggered the
commit — a save that landed on disk is a success whether or not it was
recorded. Helpers return False/None instead of raising.

Commits are scoped HARD to app dirs: exactly two levels under fused_dir(),
with a `.git` of their own. A path anywhere else — including a user's real
repository opened in the editor — is never committed to.

Subprocess discipline: `git -C <dir>` instead of cwd=, close_fds=False, no
start_new_session — keeps Popen on the posix_spawn path (no fork), because the
server process has libproj resident and fork() runs PROJ's atfork handler into
a SIGSEGV (see apps.py's _SESSION_HELPER comment).
"""
import os
import subprocess

from fused_render.shell.seed import fused_dir

_GIT_TIMEOUT = 30

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
        if _git(app_dir, "add", "-A").returncode != 0:
            return False
        # Nothing staged (e.g. the change was to an ignored sidecar): no
        # commit, and no error either.
        if _git(app_dir, "diff", "--cached", "--quiet").returncode == 0:
            return False
        return _git(app_dir, "commit", "-q", "-m",
                    message or "Update").returncode == 0
    except Exception:
        return False
