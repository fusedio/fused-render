"""The bounded executor the chat's router runs agent handlers on.

WHY ITS OWN POOL. Every chat action blocks somewhere real — `_poll` reads the
run's `out.jsonl` and may `git commit` at turn end, `_cancel` sleeps up to 5 s
waiting for the CLI's control response, `_snapshots` runs difflib over a file's
history, `_history`/`_sessions` read whole transcripts. Run on anyio's default
thread pool (what `run_in_threadpool` / a sync route uses) they would share
its ~40 tokens with every other sync route in the server, including
`/api/health` — and a backlog of chat polls from several open chats under host
pressure is exactly the moment the health probe must still answer (D1307,
SPEC §50: a slow probe reads as a dead server). Eight workers is generous for
one user's chats (each open chat polls about every 400 ms and a poll costs a
few ms in-process) and small enough that a stuck handler cannot take the
process's thread count with it.

Hardcoded, no pref: nothing about a user's setup makes a different number
right, and a knob nobody remembers setting is how a machine ends up with a
chat that queues for a reason its owner cannot find.

BUDGETS. The router wraps each call in `asyncio.wait_for(..., budget(action))`
and answers 504 when it expires. `/api/run` gave every call the executor's
60 s subprocess timeout; the pure reads get less here (an answer that late
only costs the caller a held connection). `poll` keeps the full 60 s although
it is the hot path: the poll that sees a turn end runs `_commit_turn` — up to
three git calls on a 30 s timeout each plus a 3 s self-HTTP — and a shorter
budget would 504 exactly the poll carrying the finished reply (D1310).

THE CLOCK STARTS AT SUBMIT. `wait_for` wraps the pool future, so time spent
QUEUED for a worker counts against the budget as much as time running. With
this pool saturated by several chats' polls, a Stop could spend its whole
budget waiting for a slot and 504 without ever running — which is why
`cancel`, `decide` and `app_state` ride `CONTROL_POOL` instead.

A TIMEOUT DOES NOT STOP THE WORK. Python cannot cancel a running thread:
`wait_for` cancels the asyncio future, the client gets its 504, and the worker
thread keeps running the handler until it returns on its own, holding its pool
slot until then. That is accepted, not overlooked — every handler here is
bounded by its own I/O (the longest deliberate wait is `_cancel`'s 5 s), so a
leaked worker is a slow one, not a lost one. If that ever stops being true the
fix is in the handler, not here.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="claude-agent")

#: THE CONTROL LANE (D1310). Stop, Allow/Deny and the app-state answer are a
#: user waiting on a button, or a CLI parked on a reply; they must never queue
#: behind polls. The router's budget counts QUEUE time as well as run time
#: (`wait_for` starts the clock at submit), so with the main pool saturated by
#: several chats' polls a Stop could spend its whole 15 s waiting for a slot
#: and come back a 504 having never run: a silent no-op Stop. Two workers:
#: these are rare and short, and two lets a Stop proceed while an Allow is
#: still in flight.
CONTROL_POOL = ThreadPoolExecutor(max_workers=2,
                                  thread_name_prefix="claude-agent-ctl")

#: The actions that ride `CONTROL_POOL`; every other action rides `POOL`.
CONTROL_ACTIONS = frozenset({"cancel", "decide", "app_state"})


def pool_for(action: str) -> ThreadPoolExecutor:
    """The executor `action` runs on: `CONTROL_POOL` for a control action."""
    return CONTROL_POOL if action in CONTROL_ACTIONS else POOL

#: Default budget in seconds — the old `/api/run` executor timeout, kept for
#: every action that can write (start/send/decide/snapshot_revert/...).
DEFAULT_BUDGET_S = 60.0

#: Per-action overrides. The 30 s group are pure reads, and `cancel` is 5 s of
#: control-response wait plus a process-tree kill. `poll` is deliberately NOT
#: here: it gets the default because its turn-end commit can take that long
#: (see the module docstring).
BUDGETS_S: dict[str, float] = {
    "live_run": 30.0,
    "live_host": 30.0,
    "defaults": 30.0,
    "terminal_command": 30.0,
    "sessions": 30.0,
    "history": 30.0,
    "cancel": 15.0,
}


def budget(action: str) -> float:
    """Seconds the router waits for `action` before answering 504."""
    return BUDGETS_S.get(action, DEFAULT_BUDGET_S)
