"""The home's history: a git repository the app commits to (PLAN A.2 "History / undo").

Commits are app-made: after each main-chat or breakout turn (the Stop hook calls `after_turn`), after each nightly
run, after each Settings save. `.gitignore` keeps app state, the inbox and said.md out, so the log holds only what
the specialist knows (charter, memory, knowledge, skills, people, triggers).

A breakout must never rewrite those files. Permission rules refuse it, but an approved interpreter write gets past
Edit rules (verified), so `after_turn(kind="breakout")` also puts every protected tracked path back the way HEAD
has it (`guard`). Without git every function degrades to a no-op (`available()` False), never an error.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time

from fused_render.specialists import paths

logger = logging.getLogger(__name__)

#: Identity for app commits when the user has none configured (`-c` only fills it in, it never overrides theirs).
_IDENT = ("Fused specialists", "specialists@fused.invalid")
#: Tracked files a breakout may not change (home.PROTECTED minus the untracked ones).
GUARDED = ("CLAUDE.md", "memory.md", "knowledge", ".claude", "people.md", "triggers.json", "specialist.json",
           ".gitignore")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
STATUS_OFF = "History is off: git not found"


def available() -> bool:
    return shutil.which("git") is not None


def _lock(home: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(os.path.realpath(home), threading.Lock())


def _git(home: str, *args: str, check: bool = False, timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("GIT_AUTHOR_NAME", _IDENT[0])
    env.setdefault("GIT_AUTHOR_EMAIL", _IDENT[1])
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    # No hooks, no signing prompts, no pager: the user's global git config must not make an app commit hang.
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "-c", "core.pager=cat",
           "-c", "core.quotepath=false", *args]
    return subprocess.run(cmd, cwd=home, env=env, capture_output=True, text=True, timeout=timeout, check=check)


def init(home: str, message: str = "create") -> str | None:
    """`git init` the home (once) and commit what is there. Returns the commit sha, or None (no git, failure)."""
    if not available():
        return None
    try:
        with _lock(home):
            if not os.path.isdir(os.path.join(home, ".git")):
                _git(home, "init", "-q", check=True)
        return _commit_home(home, message)
    except (OSError, subprocess.SubprocessError):
        logger.warning("specialists: git init failed in %s", home, exc_info=True)
        return None


def _commit_home(home: str, message: str) -> str | None:
    with _lock(home):
        if not os.path.isdir(os.path.join(home, ".git")):
            return None
        _git(home, "add", "-A", check=True)
        if _git(home, "diff", "--cached", "--quiet").returncode == 0:
            return None
        _git(home, "commit", "-q", "-m", message, check=True)
        return _git(home, "rev-parse", "--short", "HEAD", check=True).stdout.strip() or None


def commit(spec_id: str, message: str) -> str | None:
    """Commit every change in the home; the new short sha, or None when nothing changed (or no git)."""
    home = paths.home(spec_id)
    if not available() or not os.path.isdir(home):
        return None
    try:
        return _commit_home(home, message)
    except (OSError, subprocess.SubprocessError):
        logger.warning("specialists: commit failed in %s", home, exc_info=True)
        return None


def log(spec_id: str, path: str | None = None, limit: int = 50) -> list[dict]:
    """`[{"sha","at","message","files"}]`, newest first; `path` (relative to the home) narrows it."""
    home = paths.home(spec_id)
    if not available() or not os.path.isdir(os.path.join(home, ".git")):
        return []
    args = ["log", f"-n{max(1, int(limit))}", "--format=%x1e%h%x1f%ct%x1f%s", "--name-only"]
    if path:
        args += ["--", path]
    try:
        out = _git(home, *args).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    commits = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) != 3:
            continue
        try:
            at = float(parts[1])
        except ValueError:
            at = 0.0
        commits.append({"sha": parts[0], "at": at, "message": parts[2],
                        "files": [f for f in rest.splitlines() if f.strip()]})
    return commits


def memory_version(spec_id: str) -> str:
    """A short id of memory.md's CURRENT content: git's blob id (computed here, so it needs no git and changes
    exactly when the text does, committed or not). "" when there is no memory.md."""
    try:
        with open(os.path.join(paths.home(spec_id), "memory.md"), "rb") as f:
            data = f.read()
    except OSError:
        return ""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:8]  # noqa: S324 — git's id, not security


def diff_files(spec_id: str, sha: str) -> list[str]:
    home = paths.home(spec_id)
    if not sha or not available() or not os.path.isdir(os.path.join(home, ".git")):
        return []
    try:
        res = _git(home, "show", "--name-only", "--format=", str(sha))
    except (OSError, subprocess.SubprocessError):
        return []
    return [f for f in res.stdout.splitlines() if f.strip()] if res.returncode == 0 else []


def guard(spec_id: str) -> list[str]:
    """Put every guarded tracked path back as HEAD has it, and remove new files under guarded folders. Returns the
    paths it restored or removed ([] when nothing had changed)."""
    home = paths.home(spec_id)
    if not available() or not os.path.isdir(os.path.join(home, ".git")):
        return []
    try:
        with _lock(home):
            st = _git(home, "status", "--porcelain", "-z", "--untracked-files=all", "--", *GUARDED).stdout
            changed, untracked = [], []
            entries = st.split("\0")
            i = 0
            while i < len(entries):
                e = entries[i]
                i += 1
                if len(e) < 4:
                    continue
                code, rel = e[:2], e[3:]
                if code[0] in "RC":  # a rename: the next entry is its source
                    if i < len(entries) and entries[i]:
                        changed.append(entries[i])
                    i += 1
                if code == "??":
                    untracked.append(rel)
                else:
                    changed.append(rel)
            if changed:
                tracked = set(_git(home, "ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
                back = [p for p in changed if p in tracked]
                gone = [p for p in changed if p not in tracked]
                if back:
                    _git(home, "checkout", "HEAD", "--", *back, check=True)
                    _git(home, "reset", "-q", "HEAD", "--", *back)
                for p in gone:  # added to the index and the tree by the breakout
                    _git(home, "rm", "-q", "-f", "--cached", "--", p)
                    untracked.append(p)
            for rel in untracked:
                try:
                    os.remove(os.path.join(home, rel))
                except OSError:
                    pass
            return changed + [p for p in untracked if p not in changed]
    except (OSError, subprocess.SubprocessError):
        logger.warning("specialists: guard failed in %s", home, exc_info=True)
        return []


def _guard_row(spec_id: str, session_id: str, restored: list[str]) -> None:
    """A `guard` row in the runs log, so the user can see a breakout tried to rewrite what it may not."""
    try:
        from fused_render.specialists import harness

        harness.save_row(spec_id, {"kind": "guard", "status": "done", "started": time.time(),
                                   "ended": time.time(), "trigger": {"session_id": session_id},
                                   "answer": "Put back: " + ", ".join(restored[:20])})
    except Exception:  # noqa: BLE001
        logger.warning("specialists: could not write the guard row", exc_info=True)


def _main_turn_live(spec_id: str, breakout_session: str) -> bool:
    """A main-chat run in the home is live right now (then the guard must not undo what main chat is writing,
    PLAN D.1: the changes are committed as main instead)."""
    try:
        from fused_render.specialists import chat

        cur = chat.state(spec_id).get("current")
        return bool(cur) and cur != breakout_session and chat.session_live(spec_id, cur)
    except Exception:  # noqa: BLE001
        return False


def after_turn(spec_id: str, kind: str, session_id: str) -> None:
    """Called at the end of every turn of a session in the home (Stop hook). A breakout's turn first restores the
    guarded paths; then whatever changed is committed. Never raises."""
    try:
        sid8 = (session_id or "")[:8]
        if kind == "breakout":
            if _main_turn_live(spec_id, session_id):
                commit(spec_id, f"main chat turn {sid8} (during breakout)".strip())
                return
            restored = guard(spec_id)
            if restored:
                _guard_row(spec_id, session_id, restored)
            commit(spec_id, f"breakout turn {sid8}".strip())
        else:
            commit(spec_id, f"main chat turn {sid8}".strip())
    except Exception:  # noqa: BLE001
        logger.warning("specialists: after_turn failed for %s", spec_id, exc_info=True)
