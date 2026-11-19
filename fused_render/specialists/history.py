"""The home's history: a git repository the app commits to (docs/specialists.md §2 "History").

Commits are app-made: after each main-chat or breakout turn (the Stop hook calls `after_turn`), after each nightly
run, after each Settings save. `.gitignore` keeps app state, the inbox and said.md out, so the log holds only what
the specialist knows (charter, memory, knowledge, skills, people, triggers).

A breakout must never rewrite those files. It runs in its own workroom (`<home>/.specialist/breakouts/<bid>/work`,
docs/specialists.md §3), so no relative-path write of any tool can reach them; Edit rules refuse the absolute paths. What is
left is an approved interpreter or shell command that writes an absolute path into the home. The Stop-time `guard`
is the second layer for that: at a breakout turn's end it puts every protected tracked path back the way HEAD has
it, but ONLY when no main-chat turn and no nightly run was live during that turn (then it cannot tell whose change
is whose, so it does nothing and commits nothing; a `guard` row says so). A breakout turn never commits.

Who commits what: a main-chat turn commits everything in the home (its Stop); a Settings save commits only the
paths it wrote (`commit(paths=...)`); the nightly job commits its run. Before any commit the app rewrites the home's
`.claude/settings.json` to the canonical text, removes `settings.local.json` and drops any repository config
a session could have planted (`_sanitize_repo`), so history never records anything else there and no git command
of the app runs a command a session configured. Without git every function degrades to a no-op (`available()`
False), never an error.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Sequence

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
    # An absolute git, `-C` instead of cwd= and close_fds=False: CPython then posix_spawns instead of forking
    # (a fork with libproj resident dies; tests/test_git_posix_spawn.py).
    return subprocess.run([_git_bin(), "-C", home, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                           "-c", "core.pager=cat", "-c", "core.quotepath=false", "-c", "core.fsmonitor=false",
                           *args],
                          env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=check, close_fds=False)


def _git_bin() -> str:
    return shutil.which("git") or "git"


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


#: `core.*` keys `git init` writes (any platform); any other key in the home's `.git/config` was not the app's.
_CORE_KEYS = ("repositoryformatversion", "filemode", "bare", "logallrefupdates", "ignorecase", "precomposeunicode",
              "symlinks")


def _sanitize_repo(home: str) -> None:
    """Drop repository config a session could have planted (a clean filter, an fsmonitor, an external diff all run
    commands inside the app's own git calls): `.git/config` keeps only the `core` keys git init writes, and
    `.git/info/attributes` goes. Reading a config file with `git config --file` runs nothing. Never raises."""
    cfg = os.path.join(home, ".git", "config")
    try:
        res = _git(home, "config", "--file", cfg, "--list")
        keep, other = [], False
        for ln in res.stdout.splitlines():
            key, _, val = ln.partition("=")
            sect, _, name = key.partition(".")
            if sect == "core" and name in _CORE_KEYS and "\n" not in val:
                keep.append((name, val))
            else:
                other = True
        if other or res.returncode != 0:
            text = "[core]\n" + "".join(f"\t{k} = {v}\n" for k, v in keep) if keep else \
                "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
            with open(cfg, "w", encoding="utf-8") as fh:
                fh.write(text)
        attrs = os.path.join(home, ".git", "info", "attributes")
        if os.path.lexists(attrs):
            os.remove(attrs)
    except (OSError, subprocess.SubprocessError):
        logger.warning("specialists: could not check the history config in %s", home, exc_info=True)


def _commit_home(home: str, message: str, only: Sequence[str] | None = None) -> str | None:
    """Commit the home's changes (only under `only`, paths relative to the home, when given)."""
    with _lock(home):
        if not os.path.isdir(os.path.join(home, ".git")):
            return None
        from fused_render.specialists import home as hm

        hm.ensure_settings(home)
        _sanitize_repo(home)
        _git(home, "add", "-A", "--", *(only or ["."]), check=True)
        if _git(home, "diff", "--cached", "--quiet").returncode == 0:
            return None
        _git(home, "commit", "-q", "-m", message, check=True)
        return _git(home, "rev-parse", "--short", "HEAD", check=True).stdout.strip() or None


def _tracked(home: str, rel: str) -> bool:
    try:
        return bool(_git(home, "ls-files", "--", rel).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def commit(spec_id: str, message: str, paths_: Sequence[str] | None = None) -> str | None:
    """Commit the changes in the home (only under `paths_`, relative to the home, when given: a Settings save
    commits what it wrote and never a stray change beside it); the new short sha, or None when nothing changed
    (or no git)."""
    home = paths.home(spec_id)
    if not available() or not os.path.isdir(home):
        return None
    try:
        if paths_ is not None:
            safe = [p for p in paths_ if p and not os.path.isabs(p)
                    and ".." not in p.replace("\\", "/").split("/")]
            only = [p for p in safe if os.path.lexists(os.path.join(home, p)) or _tracked(home, p)]
            if not only:
                return None
            return _commit_home(home, message, only)
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
                _prune_empty(home, os.path.dirname(rel))
            return changed + [p for p in untracked if p not in changed]
    except (OSError, subprocess.SubprocessError):
        logger.warning("specialists: guard failed in %s", home, exc_info=True)
        return []


#: Folders of the layout (`home.ensure_layout`) that stay even when empty.
_LAYOUT_DIRS = ("knowledge", ".claude", os.path.join(".claude", "skills"))


def _prune_empty(home: str, rel_dir: str) -> None:
    """Remove `rel_dir` and its parents while they are empty, never the home or a layout folder (a removed new
    skill leaves no empty `.claude/skills/<name>/` behind)."""
    rel_dir = os.path.normpath(rel_dir) if rel_dir else ""
    while rel_dir and rel_dir not in (".", *_LAYOUT_DIRS) and not rel_dir.startswith(".."):
        try:
            os.rmdir(os.path.join(home, rel_dir))
        except OSError:
            return
        rel_dir = os.path.dirname(rel_dir)


def _guard_row(spec_id: str, session_id: str, restored: list[str], skipped: str = "") -> None:
    """A `guard` row in the runs log, so the user can see a breakout tried to rewrite what it may not (or that the
    guard stood down, and why)."""
    try:
        from fused_render.specialists import harness

        answer = f"Guard skipped: {skipped}" if skipped else "Put back: " + ", ".join(restored[:20])
        harness.save_row(spec_id, {"kind": "guard", "status": "skipped" if skipped else "done",
                                   "started": time.time(), "ended": time.time(),
                                   "trigger": {"session_id": session_id}, "answer": answer})
    except Exception:  # noqa: BLE001
        logger.warning("specialists: could not write the guard row", exc_info=True)


# Turn bookkeeping, in this process only (the hooks of every session in a home post to this server): a session
# cannot forge it the way it could forge a file. Lost on restart, when the live-run check still stands.
_TURNS: dict[str, dict] = {}
_TURNS_LOCK = threading.Lock()
#: A main turn "started" this long ago with no end recorded is taken as over (a lost Stop: Claude Code fires no
#: Stop for an interrupted turn, and the Stop button's `cancel` ends it here too, `main_turn_ended`).
MAIN_TURN_MAX_S = 3600.0


def _turns(spec_id: str) -> dict:
    return _TURNS.setdefault(spec_id, {"main_started": 0.0, "main_ended": 0.0, "breakouts": {}})


def main_turn_open(spec_id: str, within_s: float = MAIN_TURN_MAX_S) -> bool:
    """A main-chat turn's prompt hook fired (less than `within_s` ago) and its Stop has not (this process's
    record)."""
    with _TURNS_LOCK:
        t = _turns(spec_id)
        return t["main_started"] > t["main_ended"] and time.time() - t["main_started"] < within_s


def main_turn_ended(spec_id: str) -> None:
    """The main chat's turn is over without a Stop (the user pressed Stop)."""
    with _TURNS_LOCK:
        _turns(spec_id)["main_ended"] = time.time()


def _main_turn_live(spec_id: str, breakout_session: str) -> bool:
    """A main-chat run in the home is live right now (its turn is open)."""
    if main_turn_open(spec_id):
        return True
    try:
        from fused_render.specialists import chat

        cur = chat.state(spec_id).get("current")
        return bool(cur) and cur != breakout_session and chat.session_live(spec_id, cur)
    except Exception:  # noqa: BLE001 — cannot tell: say live (the guard then stands down; nothing is undone)
        return True


def _main_overlapped(spec_id: str, breakout_session: str) -> bool:
    """A main-chat turn was live at any moment of breakout `breakout_session`'s turn: live now, or started or
    ended since that turn started (when this process saw it start)."""
    if _main_turn_live(spec_id, breakout_session):
        return True
    with _TURNS_LOCK:
        t = _turns(spec_id)
        since = t["breakouts"].get(breakout_session)
        return since is not None and max(t["main_started"], t["main_ended"]) >= since


def _nightly_running(spec_id: str) -> bool:
    """The nightly job holds the home now (stream 3's `nightly.running`: this process, or the home's flock held by
    another). Its writes are uncommitted until it ends, so a guard now would undo them."""
    try:
        from fused_render.specialists import nightly

        return bool(nightly.running(spec_id))
    except Exception:  # noqa: BLE001 — cannot tell: do not guard (the nightly's work is worth more)
        return True


def _sync_workroom(spec_id: str, session_id: str) -> None:
    try:
        from fused_render.specialists import breakouts, store

        spec = store.get(spec_id)
        rec = next((r for r in breakouts.for_spec(spec_id) if r.get("session_id") == session_id), None)
        if spec is not None and rec is not None:
            breakouts.sync(spec, rec["id"])
    except Exception:  # noqa: BLE001
        logger.debug("specialists: workroom sync failed", exc_info=True)


def before_turn(spec_id: str, kind: str, session_id: str) -> None:
    """Called when a turn starts in the home (the prompt hook). Records the turn's start (the guard's attribution).
    A breakout turn: its workroom is refreshed (memory may have changed since its last turn), and whatever tracked
    change is pending is committed so the guard at that turn's Stop only ever undoes what changed DURING the turn;
    not while the nightly job runs or a main-chat turn is live (their work is theirs to commit). Never raises."""
    try:
        now = time.time()
        with _TURNS_LOCK:
            t = _turns(spec_id)
            if kind == "main":
                t["main_started"] = now
            elif kind == "breakout" and session_id:
                t["breakouts"][session_id] = now
                if len(t["breakouts"]) > 200:
                    for k in sorted(t["breakouts"], key=t["breakouts"].get)[:100]:
                        t["breakouts"].pop(k, None)
        if kind != "breakout":
            return
        _sync_workroom(spec_id, session_id)
        if _nightly_running(spec_id) or _main_turn_live(spec_id, session_id):
            return
        commit(spec_id, "pending changes before a breakout turn")
    except Exception:  # noqa: BLE001
        logger.warning("specialists: before_turn failed for %s", spec_id, exc_info=True)


def after_turn(spec_id: str, kind: str, session_id: str) -> None:
    """Called at the end of every turn of a session in the home (Stop hook).

    main:     everything that changed is committed (`main chat turn <sid>`).
    breakout: commits nothing. The guarded paths are put back as HEAD has them (`guard`) unless a main-chat turn or
              the nightly job was live during the turn: then nothing is touched and a `guard` row says why.
    While the nightly job runs nothing is committed at all: the job commits its own run. Never raises."""
    try:
        sid8 = (session_id or "")[:8]
        if kind == "main":
            with _TURNS_LOCK:
                _turns(spec_id)["main_ended"] = time.time()
        if _nightly_running(spec_id):
            if kind == "breakout":
                _guard_row(spec_id, session_id, [], skipped="the nightly update was running; its changes were "
                                                            "left alone and are committed by that run")
            return
        if kind == "breakout":
            if _main_overlapped(spec_id, session_id):
                if _dirty(spec_id):
                    _guard_row(spec_id, session_id, [], skipped="the main chat was working at the same time; "
                                                                "nothing was put back or committed")
                return
            restored = guard(spec_id)
            if restored:
                _guard_row(spec_id, session_id, restored)
            return
        commit(spec_id, f"main chat turn {sid8}".strip())
    except Exception:  # noqa: BLE001
        logger.warning("specialists: after_turn failed for %s", spec_id, exc_info=True)


def _dirty(spec_id: str) -> bool:
    """Any guarded path differs from HEAD (or is new)."""
    home = paths.home(spec_id)
    if not available() or not os.path.isdir(os.path.join(home, ".git")):
        return False
    try:
        st = _git(home, "status", "--porcelain", "--untracked-files=all", "--", *GUARDED).stdout
        return bool(st.strip())
    except (OSError, subprocess.SubprocessError):
        return False
