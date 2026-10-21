"""A global cap on how many Claude sessions run at once.

Each live session is a host process plus the CLI it holds, and a Mac that runs
ten of them at once is out of memory. Scheduled and queued TASKS wait for a
free slot; the chat composer and canvas spawns are counted but never held (a
person typing is never told to wait).

THE COUNT IS READ OFF DISK, not kept in a counter: a session host outlives the
server that spawned it, so a counter would start at zero after every restart
and over-admit. `host.json` with a live pid is the host's own "I am up" fact.
A freshly spawned run has no `host.json` yet, so a young run dir with a live
`pid` file counts too — without it two spawns in one tick both see a free slot.

The agent module comes from `project_queue.agent_module()` (None when it will
not import, which counts as nothing running) and is reached through `_agent()`
so tests can stand in for it.
"""
from __future__ import annotations

import os
import time
from datetime import datetime

from fused_render.shell import prefs

# A run dir this young with a live `pid` and no `host.json` is a spawn still
# starting up; older than this and it is something else (a one-shot, a dead
# host's leftovers) and does not hold a slot.
YOUNG_S = 20.0


def _agent():
    from fused_render import project_queue

    return project_queue.agent_module()


def cap() -> int:
    """The configured cap; 0 means unlimited."""
    return prefs.claude_max_concurrent_sessions()


def _young_spawn(agent, run_dir: str, now: float) -> bool:
    try:
        if now - os.stat(run_dir).st_mtime > YOUNG_S:
            return False
        with open(os.path.join(run_dir, "pid"), encoding="utf-8") as fh:
            return bool(agent._pid_alive(fh.read().strip()))
    except (OSError, ValueError):
        return False


def live_count() -> int:
    """How many session hosts are up (or just starting) right now."""
    agent = _agent()
    if agent is None:
        return 0
    try:
        names = sorted(os.listdir(agent.RUNS), reverse=True)[:agent._LIVE_SCAN_LIMIT]
    except OSError:
        return 0
    now = time.time()
    count = 0
    for name in names:
        run_dir = os.path.join(agent.RUNS, name)
        if os.path.isfile(os.path.join(run_dir, "host.json")):
            if agent._host_alive(run_dir):
                count += 1
        elif _young_spawn(agent, run_dir, now):
            count += 1
    return count


def at_cap() -> bool:
    limit = cap()
    return limit > 0 and live_count() >= limit


def has_live_host(entry: dict, session: str = "") -> bool:
    """Whether the entry's conversation already has a host to hand the message
    to. That send adds no process, so the cap never blocks it. `session` is the
    RESOLVED conversation (a follower's own `session_id` is empty; its leader's
    conversation is what it resumes); it falls back to the entry's field."""
    session = str(session or entry.get("session_id") or "")
    target = str(entry.get("target") or "")
    if not (session and target):
        return False
    agent = _agent()
    if agent is None:
        return False
    try:
        return bool((agent._live_host(target, session) or {}).get("run_id"))
    except Exception:  # noqa: BLE001 — unreadable means no host
        return False


def held(entry: dict, session: str = "") -> bool:
    """Whether the cap holds this entry back from spawning a new session."""
    return at_cap() and not has_live_host(entry, session)


def waiting(entries: list[dict], now: datetime) -> list[str]:
    """Task keys of due `pending` entries the cap is holding, oldest due first."""
    from fused_render import schedule

    if not at_cap():
        return []
    due: list[tuple[datetime, str]] = []
    by_id = {str(e.get("id") or ""): e for e in entries}
    for entry in entries:
        if entry.get("state") != schedule.PENDING:
            continue
        try:
            when = schedule.parse_due(entry.get("due"))
        except ValueError:
            continue
        session = ""
        if not entry.get("session_id"):
            session, _ready = schedule._follow_session(entry, by_id)
        if when > now or has_live_host(entry, session):
            continue
        due.append((when, schedule._task_key(entry, by_id)))
    due.sort(key=lambda pair: pair[0])
    keys: list[str] = []
    for _, key in due:
        if key not in keys:
            keys.append(key)
    return keys
