"""What archiving a task tears down (Workspace scope, unit 3).

A task is more than its transcript: a live session, the dev servers that session
started, and the git worktrees it made under `.claude/worktrees/`. Archiving used
to file the row and leave all three running or on disk, so a week of tasks left
99 stale worktrees and a pile of orphaned listeners. Teardown is the other half
of the archive gesture:

1. `stop_session` — end the live run (the whole process tree, as delete does)
   and every descendant that escaped its process group: SIGTERM, a short grace,
   then SIGKILL.
2. `teardown_worktrees` — remove each RECORDED worktree that is clean and pushed;
   KEEP and REPORT any that is dirty, has unpushed commits, or cannot be read.
   `force=True` is the explicit "remove anyway".

NOTHING HERE RAISES. A kill that failed or a git that would not answer is a kept
worktree or a survivor in the report, never a failed archive: the filing has
already happened by the time this runs, and the reader's gesture must not 500.

Worktrees come only from the task's own transcript (`tasks._note_worktrees`:
`EnterWorktree` results and `git worktree add .claude/worktrees/...`). A task
that predates the recording has none, so teardown never guesses at a folder.

Every git spawn goes through `claude_agent.worktrees._git`: absolute exe path,
`close_fds=False`, no `cwd=` (the posix_spawn rule — libproj is resident here).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import time

from fused_render.claude_agent import worktrees as wt

logger = logging.getLogger(__name__)

#: Seconds a SIGTERMed process gets before SIGKILL.
KILL_GRACE_S = 2.0
_POLL_S = 0.05
#: git is asked fast: a status that takes longer than this is "unknown".
_GIT_S = 5.0

_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


# ------------------------------------------------------------------ processes


def _alive(pid: int) -> bool:
    """Running and not a zombie (a killed child nobody has reaped yet answers
    `os.kill(pid, 0)` for ever, and waiting on it would burn the whole grace)."""
    try:
        from fused_render import canvas_sync_state
        if not canvas_sync_state.pid_alive(pid):
            return False
    except Exception:  # noqa: BLE001 — no probe: treat as alive, SIGKILL is harmless
        return True
    try:
        from fused_render import sysmon
        if sysmon.backend is not None and sysmon.backend.is_zombie(pid):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def kill_pids(pids, grace_s: float = KILL_GRACE_S) -> list[int]:
    """SIGTERM every live pid in `pids`, wait up to `grace_s` for them to go,
    SIGKILL what is left. Returns the pids that were alive when asked."""
    asked = [p for p in dict.fromkeys(int(p) for p in pids) if p > 1 and _alive(p)]
    for pid in asked:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + max(0.0, grace_s)
    left = list(asked)
    while left and time.monotonic() < deadline:
        left = [p for p in left if _alive(p)]
        if left:
            time.sleep(_POLL_S)
    for pid in [p for p in left if _alive(p)]:
        try:
            os.kill(pid, _SIGKILL)
        except OSError:
            pass
    return asked


def _descendants(pid: int) -> list[int]:
    try:
        from fused_render import sysmon
        if sysmon.backend is None:
            return []
        return [int(p) for p in sysmon.backend.descendants(pid)]
    except Exception:  # noqa: BLE001 — a tree we cannot walk is a tree we do not kill
        return []


def _run_pid(run_dir: str) -> int:
    try:
        with open(os.path.join(run_dir, "pid"), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return 0


def stop_session(session_id: str, agent, runs, grace_s: float = KILL_GRACE_S) -> dict:
    """End every LIVE run of `session_id` in `runs` (`project_queue.scan_runs`
    records) and anything those runs left behind.

    The descendants are read BEFORE the cancel: once the CLI is gone its
    children are re-parented to init and no longer lead back to it. The cancel
    is `_cancel(..., interrupt_first=False)` — deny the card, kill the process
    group — because the next thing that happens to this session is its filing,
    not another send. What outlives the group (a dev server that called
    `setsid`) is SIGTERMed and, past the grace, SIGKILLed.
    Returns `{"stopped": <runs ended>, "killed": [<stray pids>]}`."""
    stopped = 0
    strays: list[int] = []
    for run in runs or []:
        if session_id not in (run.get("sessions") or []):
            continue
        run_dir = str(run.get("run_dir") or "")
        try:
            if not (run_dir and agent._alive(run_dir)):
                continue
            pid = _run_pid(run_dir)
            tree = _descendants(pid) if pid else []
            agent._cancel(str(run.get("run_id") or ""), interrupt_first=False)
            stopped += 1
            deadline = time.monotonic() + max(0.0, grace_s)
            while agent._alive(run_dir) and time.monotonic() < deadline:
                time.sleep(_POLL_S)
            if pid:
                strays += kill_pids([pid, *tree], grace_s=grace_s)
        except Exception:  # noqa: BLE001 — see the module docstring
            logger.warning("teardown: could not stop run %s of %s",
                           run.get("run_id"), session_id, exc_info=True)
    return {"stopped": stopped, "killed": strays}


# ------------------------------------------------------------------ worktrees


def _git(path: str, *args: str):
    """`(rc, stdout)` or `(None, "")` when git would not answer."""
    try:
        rc, out, _ = wt._git(path, *args, timeout=_GIT_S)
    except (wt._Refusal, OSError, subprocess.SubprocessError):
        return None, ""
    return rc, out


def _count(path: str, *args: str) -> int | None:
    rc, out = _git(path, "rev-list", "--count", *args)
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def inspect_worktree(path: str) -> dict:
    """What a worktree would lose if it were removed.

    `dirty_files`: changed plus untracked paths (the `git status --porcelain`
    lines), None when git did not answer. `unpushed`: commits on HEAD that the
    upstream does not have — `@{upstream}..HEAD`, and where there is no upstream
    `HEAD --not --remotes` (what no remote branch holds) — None when git errors
    both ways. `exists` False means the folder is already gone: nothing held."""
    if not os.path.isdir(path):
        return {"path": path, "exists": False, "dirty_files": 0, "unpushed": 0}
    rc, out = _git(path, "status", "--porcelain")
    dirty = len([ln for ln in out.splitlines() if ln.strip()]) if rc == 0 else None
    unpushed = _count(path, "@{upstream}..HEAD")
    if unpushed is None:
        unpushed = _count(path, "HEAD", "--not", "--remotes")
    return {"path": path, "exists": True, "dirty_files": dirty, "unpushed": unpushed}


def holds_open(info: dict) -> list[str]:
    """The reasons a worktree must stay, in a fixed order; [] frees it."""
    if not info.get("exists"):
        return []
    why = []
    if info.get("dirty_files"):
        why.append("dirty")
    if info.get("unpushed"):
        why.append("unpushed")
    if info.get("dirty_files") is None or info.get("unpushed") is None:
        why.append("unknown")
    return why


def _managed(path: str) -> bool:
    """Is `path` directly under some checkout's `.claude/worktrees/`? Only what
    this app (or the CLI's EnterWorktree) made is ever removed."""
    real = os.path.realpath(path)
    parent = os.path.dirname(real)
    return os.path.basename(parent) == "worktrees" and \
        os.path.basename(os.path.dirname(parent)) == ".claude"


def teardown_worktrees(paths, force: bool = False) -> dict:
    """Remove each worktree in `paths` that holds nothing open (or every managed
    one when `force`). `{"removed": [...], "kept": [{path, reasons, dirty_files,
    unpushed}], "gone": [...]}`. `reasons` adds `not-managed` for a path outside
    `.claude/worktrees/` and `remove-failed` for a git refusal."""
    removed: list[str] = []
    kept: list[dict] = []
    gone: list[str] = []
    for path in dict.fromkeys(str(p) for p in paths or []):
        info = inspect_worktree(path)
        if not info["exists"]:
            gone.append(path)
            continue
        entry = {"path": path, "dirty_files": info["dirty_files"],
                 "unpushed": info["unpushed"]}
        if not _managed(path):
            kept.append({**entry, "reasons": ["not-managed"]})
            continue
        reasons = holds_open(info)
        if reasons and not force:
            kept.append({**entry, "reasons": reasons})
            continue
        res = wt.remove_worktree(path, path, force=force)
        if isinstance(res, dict) and res.get("removed"):
            removed.append(path)
        else:
            kept.append({**entry, "reasons": [*reasons, "remove-failed"],
                         "error": str((res or {}).get("error") or "")})
    return {"removed": removed, "kept": kept, "gone": gone}
