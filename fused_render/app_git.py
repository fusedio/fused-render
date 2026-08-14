"""Local version control for app folders (<workspace>/<tag>/<name>).

Every app scaffolded by POST /api/apps/new ships with a git repo and a single
boilerplate commit; after that, each completed Claude turn lands as its own
small commit (templates/claude/agent.py mirrors the commit helper here, since
templates must not import fused_render, D166). Manual edits made through the
editor's /api/fs endpoints are NOT committed (D245) — the user's own commits
and Claude's turns are the whole history.

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

Subprocess discipline: `git -C <dir>` (never `cwd=`) and `close_fds=False`,
matching every other subprocess spawn in this codebase (agent.py, history.py,
executor.py, server/ai.py). The server process gets libproj resident
(the fused engine's import tree reaches pyproj whenever a geo stack is
installed beside it — it did so via geopandas until D234 took that out of
`[bundled]`, and still does wherever one is present — and prefs' availability
probe imports it in-process), and from then on a plain fork() runs PROJ's
pthread_atfork child handler into a SIGSEGV before exec — the default
close_fds=True forks here on macOS, so every `git add` died rc=-11 with empty
stderr the moment the shell first polled /api/prefs (see apps.py's
_SESSION_HELPER comment for the same crash; verified live in the field
2026-08-03). close_fds=False makes CPython take the posix_spawn path instead,
which runs no atfork handlers — and unlike a hand-rolled os.posix_spawnp call,
subprocess.run degrades to CreateProcess on Windows rather than raising
AttributeError (there is no fork() to work around there in the first place).
"""
import logging
import os
import subprocess
import sys
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

# Session sidecars are chat bookkeeping, not app content — keep them out of
# history: the folder-level .claude-split.json (claude/agent.py — a historical
# filename that predates the template's rename and stays as it is, because it
# exists on users' disks), plus the legacy per-file <file>.json the pre-D235
# file-scoped chat template used to write beside the entry html — still
# ignored, because an existing repo may already have one.
#
# `.venv/` is here for a different reason: the app NEVER creates one (project
# venvs live under ~/.fused-render/venvs — SPEC PY-16, MD-7), but a user who runs
# `uv run` or `uv sync` in their own terminal will, and `commit()`'s `git add -A`
# would sweep tens of thousands of files into the app's history. `pyproject.toml`
# and `uv.lock` are deliberately NOT ignored: those are source and belong in the
# repo — they are what makes the folder reproduce on another machine.
_GITIGNORE = "*.html.json\n.claude-split.json\n.venv/\n"


def _git(app_dir: str, *args: str) -> subprocess.CompletedProcess:
    """One git invocation against the app repo — see the module docstring for
    the close_fds=False discipline. May raise subprocess.TimeoutExpired past
    _GIT_TIMEOUT; every caller in this file is already wrapped in a broad
    except Exception, so a hung git never escapes as an unhandled error."""
    return subprocess.run(
        ["git", "-C", app_dir, *_IDENTITY, *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        close_fds=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
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


def _ensure_excludes(app_dir: str) -> None:
    """Make sure every _GITIGNORE pattern is excluded in this repo, via the
    repo-local `.git/info/exclude` — NOT the app's `.gitignore`.

    A repo initialized before a pattern existed keeps its old `.gitignore`
    (init_repo only writes it when missing), so `git add -A` in commit()
    would sweep new bookkeeping files (e.g. `.claude-split.json`) into app
    history. info/exclude is git's file for exactly this: repo-scoped ignore
    rules that are not project content, so old apps get the new patterns
    without their (possibly user-edited) `.gitignore` being touched.
    Idempotent, append-only; best-effort like everything else here."""
    try:
        path = os.path.join(app_dir, ".git", "info", "exclude")
        if not os.path.isdir(os.path.dirname(path)):
            return
        try:
            with open(path, encoding="utf-8") as f:
                have = {ln.strip() for ln in f}
        except OSError:
            have = set()
        missing = [p for p in _GITIGNORE.splitlines() if p and p not in have]
        if missing:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(missing) + "\n")
    except Exception:
        logger.warning("ensure_excludes failed for %s", app_dir, exc_info=True)


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
        # Repos initialized before a _GITIGNORE pattern existed must not
        # sweep new bookkeeping files into history via the add -A below.
        _ensure_excludes(app_dir)
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
