"""Local version control for app folders (<workspace>/<tag>/<name>).

The `local` tag is ONE git repository — `<fused_dir()>/local/.git` — holding
every app folder under it (D626; local_monorepo.py migrates the old
one-repo-per-app layout into it). Every app scaffolded by POST /api/apps/new
lands in that shared repo as one scoped boilerplate commit; after that, each
completed Claude turn lands as its own small commit
(templates/claude/agent.py mirrors the commit helper here, since templates
must not import fused_render, D166). Manual edits made through the editor's
/api/fs endpoints are NOT committed (D245) — the user's own commits and
Claude's turns are the whole history.

Because sibling apps share the repo, every write is PATHSPEC-SCOPED to the
one app folder it is about: `git add -A -- <name>` and
`git commit -m … -- <name>`. A bare `add -A` from anywhere in a work tree is
whole-tree since git 2.0, and would sweep a concurrent session's work on a
sibling app into this app's commit. The same applies to `commit`: the
pathspec keeps another session's STAGED entries out of this commit.

An app that still heads its own repository (an unmigrated folder, or a
folder the migration deliberately skipped because it has a remote) keeps the
legacy behaviour: commits land in ITS `.git`, and a nested `.git` shadows the
shared repo for everything below it (git itself guarantees that).

Everything here is BEST-EFFORT: git may be missing, the repo may not exist
yet, a concurrent commit may hold index.lock. None of that may ever fail the
operation that triggered the commit — a save that landed on disk is a
success whether or not it was recorded. Helpers return False/None instead of
raising, and every skipped commit says why at DEBUG level so a "why didn't
this commit?" has an answer in the server log. A missed commit is also not a
lost change: the next successful scoped commit sweeps everything pending
under that app.

Commits are scoped HARD to app dirs: exactly two levels under fused_dir(),
and only when the folder resolves to a repo WE own — its own remote-less
`.git`, or the shared `local` repo. A path anywhere else — including a
user's real repository opened in the editor — is never committed to.

Subprocess discipline: `git -C <dir>` (never `cwd=`) and `close_fds=False`,
matching every other subprocess spawn in this codebase (agent.py, history.py,
executor.py, server/ai.py). The server process gets libproj resident
(the fused engine's import tree reaches pyproj whenever a geo stack is
installed beside it — it did so via geopandas until D276 took that out of
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


logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30

# One short retry when another writer (a Claude turn's fallback sweep, a
# second editor save — with one shared repo, a session working on a SIBLING
# app too) holds index.lock. Best-effort still — after the retry the change
# just waits for the next commit's scoped `add -A`.
_LOCK_RETRY_DELAY_S = 0.3

# Commit identity, passed per-invocation: an app folder is not the user's
# repo, and the machine may have no global git identity at all — a fresh
# machine must still get its boilerplate commit.
_IDENTITY = ["-c", "user.name=Fused", "-c", "user.email=apps@fused.io"]

# The tag whose apps share one repository. Other tags are the user's to
# arrange (sandbox/showcase are externally synced clones already).
LOCAL_TAG = "local"

# Session sidecars are chat bookkeeping, not app content — keep them out of
# history: the folder-level .claude-split.json (claude/agent.py — a historical
# filename that predates the template's rename and stays as it is, because it
# exists on users' disks), plus the legacy per-file <file>.json the pre-D235
# file-scoped chat template used to write beside the entry html — still
# ignored, because an existing repo may already have one.
#
# `.venv/` is here for a different reason: the app NEVER creates one (project
# venvs live under ~/.fused-render/venvs — SPEC PY-16, MD-7), but a user who runs
# `uv run` or `uv sync` in their own terminal will, and a scoped `git add -A`
# would sweep tens of thousands of files into the app's history. `pyproject.toml`
# and `uv.lock` are deliberately NOT ignored: those are source and belong in the
# repo — they are what makes the folder reproduce on another machine.
# `.fused/` is the app's own state folder (D548): machine-local by
# definition — a cache that can be deleted at any time, and data keyed to
# THIS machine's absolute paths — so it is never app history. The trailing
# slash matters: it matches the directory only, leaving an exported
# `<name>.fused` app file (SPEC §43) tracked like any other artifact.
_GITIGNORE = "*.html.json\n.claude-split.json\n.venv/\n.fused/\n"

# The shared repo's root .gitignore: the per-app set plus the one file macOS
# drops into any folder the Finder has looked at. Written once at repo
# creation and never touched again — an app's own `.gitignore` beside its
# files still cascades on top of it (git's normal nesting rules).
_ROOT_GITIGNORE = ".DS_Store\n" + _GITIGNORE


def _git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    """One git invocation against a repo we own — see the module docstring for
    the close_fds=False discipline. May raise subprocess.TimeoutExpired past
    _GIT_TIMEOUT; every caller in this file is already wrapped in a broad
    except Exception, so a hung git never escapes as an unhandled error."""
    return subprocess.run(
        [_git_bin(), "-C", repo_dir, *_IDENTITY, *args],
        capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        encoding="utf-8", errors="replace",
        close_fds=False,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def _git_retry_lock(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    """Like _git, with one retry when the failure is index.lock contention —
    the only failure that is expected to be gone milliseconds later."""
    r = _git(repo_dir, *args)
    if r.returncode != 0 and "index.lock" in (r.stderr or ""):
        time.sleep(_LOCK_RETRY_DELAY_S)
        r = _git(repo_dir, *args)
    return r


def local_repo_root() -> str:
    """The shared apps repository: `<fused_dir()>/local`. Path only, no I/O."""
    return os.path.join(fused_dir(), LOCAL_TAG)


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


def _pathspec(spec: str) -> str:
    """`spec` armored for use as a git pathspec. Git treats `*`, `?`, `[…]`
    and a leading `:` as pathspec magic, and app folder names may carry any
    of them — `:(literal)` turns all of it off, so an app named `[draft]`
    scopes to its own folder instead of pattern-matching siblings. The legacy
    `.` scope has no magic and stays bare."""
    return spec if spec == "." else ":(literal)" + spec


def _repo_scope(app_dir: str) -> tuple[str, str] | None:
    """Where a commit about `app_dir` goes: `(repo_dir, pathspec)`, or None
    when the folder resolves to no repo we own.

    Precedence is the same rule git itself applies to a nested `.git`: an
    app heading its OWN repository (unmigrated, or migration-skipped because
    it grew a remote) commits there, scoped `.`; otherwise an app directly
    under a `local` tag that IS a repository commits into the shared repo,
    scoped to its folder name. Anything else — an app in another tag, a
    workspace with no repo anywhere — is nobody's to commit."""
    if os.path.isdir(os.path.join(app_dir, ".git")):
        return app_dir, "."
    local = local_repo_root()
    if (os.path.dirname(app_dir) == local
            and os.path.isdir(os.path.join(local, ".git"))):
        return local, os.path.basename(app_dir)
    return None


def _ensure_excludes(repo_dir: str) -> None:
    """Make sure every _GITIGNORE pattern is excluded in this repo, via the
    repo-local `.git/info/exclude` — NOT a tracked `.gitignore`.

    A repo initialized before a pattern existed keeps its old `.gitignore`
    (we only write one when missing), so a scoped `git add -A` in commit()
    would sweep new bookkeeping files (e.g. `.claude-split.json`) into app
    history. info/exclude is git's file for exactly this: repo-scoped ignore
    rules that are not project content, so old repos get the new patterns
    without their (possibly user-edited) `.gitignore` being touched.
    Idempotent, append-only; best-effort like everything else here."""
    try:
        path = os.path.join(repo_dir, ".git", "info", "exclude")
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
        logger.warning("ensure_excludes failed for %s", repo_dir, exc_info=True)


def ensure_local_repo() -> bool:
    """Make `<fused_dir()>/local` a git repository (idempotent). True when the
    repo exists on return; False (never an exception) when git is missing or
    init fails. First creation writes the root `.gitignore` and commits it,
    so HEAD exists before the first app's scoped commit."""
    local = local_repo_root()
    try:
        os.makedirs(local, exist_ok=True)
        if not os.path.isdir(os.path.join(local, ".git")):
            if _git(local, "init", "-q").returncode != 0:
                return False
        gi = os.path.join(local, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write(_ROOT_GITIGNORE)
            if _git(local, "add", "--", ".gitignore").returncode == 0 and \
               _git(local, "diff", "--cached", "--quiet",
                    "--", ".gitignore").returncode != 0:
                _git_retry_lock(local, "commit", "-q", "-m",
                                "Workspace apps repo", "--", ".gitignore")
        return True
    except Exception:
        logger.warning("ensure_local_repo failed", exc_info=True)
        return False


def init_repo(app_dir: str) -> bool:
    """Version-control a freshly scaffolded `app_dir`, landing its contents as
    one boilerplate commit. In the `local` tag that means a SCOPED commit into
    the shared repo (created here if this is the first app); anywhere else it
    falls back to the legacy one-repo-per-app init. True on success; False
    (never an exception) when git is missing or any step fails — creation
    already succeeded and must stay that way."""
    try:
        app_dir = os.path.abspath(app_dir)
        if os.path.dirname(app_dir) == local_repo_root():
            if not ensure_local_repo():
                return False
            name = os.path.basename(app_dir)
            local = local_repo_root()
            if _git_retry_lock(local, "add", "-A", "--",
                               _pathspec(name)).returncode != 0:
                return False
            if _git(local, "diff", "--cached", "--quiet",
                    "--", _pathspec(name)).returncode == 0:
                return False
            return _git_retry_lock(
                local, "commit", "-q", "-m", "New app from starter",
                "--", _pathspec(name)).returncode == 0
        # Legacy: an app outside the shared tag gets its own repository.
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
    """Commit everything pending under the app folder containing `path`,
    scoped to that folder alone.

    No-op (False) when the path is not inside an app dir, the app dir
    resolves to no repo we own, there is nothing to commit, or git fails.
    True only when a commit was actually created."""
    try:
        app_dir = app_dir_for(path)
        if app_dir is None:
            return False
        scope = _repo_scope(app_dir)
        if scope is None:
            return False
        repo_dir, spec = scope
        # Repos initialized before a _GITIGNORE pattern existed must not
        # sweep new bookkeeping files into history via the scoped add below.
        _ensure_excludes(repo_dir)
        r = _git_retry_lock(repo_dir, "add", "-A", "--", _pathspec(spec))
        if r.returncode != 0:
            logger.warning("app commit skipped (%s): add failed rc=%s "
                           "stdout=%r stderr=%r", app_dir, r.returncode,
                           (r.stdout or "").strip(), (r.stderr or "").strip())
            return False
        # Nothing staged under this app (e.g. the change was to an ignored
        # sidecar): no commit, and no error either. Scoped, so a sibling
        # app's staged work neither triggers nor rides this commit.
        if _git(repo_dir, "diff", "--cached", "--quiet",
                "--", _pathspec(spec)).returncode == 0:
            return False
        r = _git_retry_lock(repo_dir, "commit", "-q", "-m",
                            message or "Update", "--", _pathspec(spec))
        if r.returncode != 0:
            logger.warning("app commit skipped (%s): commit failed rc=%s "
                           "stdout=%r stderr=%r", app_dir, r.returncode,
                           (r.stdout or "").strip(), (r.stderr or "").strip())
            return False
        return True
    except Exception:
        logger.warning("app commit skipped (%s): unexpected", path, exc_info=True)
        return False
