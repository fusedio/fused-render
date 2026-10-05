"""Starting a Claude Code session FROM THE SERVER PROCESS, and following it.

The chat backend is `fused_render.claude_agent` (one in-process module). This
module is the small seam the scheduler, the apps API's scaffolding turn and
canvases use to start a session and to follow a run nobody is polling until
its finished turn is recorded and committed.

History worth keeping: until 0.6.5 `agent._start` could NOT run in this
process. Its Popen used start_new_session, which forces CPython off
posix_spawn onto fork()+exec, and the server has libproj resident with a live
proj.db SQLite handle — fork() runs PROJ's pthread_atfork child handler, which
closes that handle and SIGSEGVs the child before exec. So `_start` ran one hop
away in a bare `python -c` helper (`SESSION_HELPER`). The spawn is now
posix_spawn-safe (`agent._HOST_SPAWN`; the host does its own setsid), so
`spawn_helper` is a plain in-process call. The NAME stays because eighteen
test files and three callers know it, and because the error mapping it did
on the helper's stderr is still the right thing to tell the user.

No import of anything under `fused_render.server` — `schedule.py` imports this
and the routers import both; keep it acyclic.
"""
from __future__ import annotations

import os
import time

# How long the recording poll follows a run before giving up. A turn can run
# long — a scaffolding pass builds a whole app, and a scheduled message may hand
# the model a substantial job — so this is deliberately generous. At 2s a tick,
# 1800 ticks is ~1h.
_RECORD_POLL_TICKS = 1800
_RECORD_POLL_INTERVAL = 2


def agent_path() -> str:
    """Path of agent.py — the package copy. Kept for the callers that still
    hand a path to a by-path loader (session_host's request dict, tests)."""
    from fused_render.claude_agent import AGENT_PATH

    return AGENT_PATH


def load_agent():
    """THE agent module. Delegates to `fused_render.claude_agent.agent_module`;
    kept under its old name for the callers and tests that monkeypatch it."""
    from fused_render.claude_agent import agent_module

    return agent_module()


def record_session_when_ready(agent, run_id: str, on_tick=None) -> None:
    """Poll the detached run until it finishes.

    `agent._poll` is what records the run's session id (one-shot via the run's
    `recorded` marker) AND what commits the finished turn into the folder's
    repo (one-shot via `committed`) — but nobody is polling until the user
    opens that folder's claude chat, which may be never. This background loop
    polls all the way to `done` so both happen regardless.

    Bookkeeping only. Every failure here is swallowed: a run whose bookkeeping
    never lands still did its work, and this thread must never be the reason a
    request or a scheduler tick fails.

    `on_tick(data)` is an OPTIONAL observer, called with each poll's result —
    including the final one, which is why it runs BEFORE the `done` check
    (scheduled messages learn the turn's outcome from exactly that tick). Return
    False from it to stop polling early; a caller with nothing to observe passes
    nothing and gets the loop this always was. Its exceptions are swallowed for
    the same reason the poll's are: an observer is not allowed to abandon a run
    whose turn has not been committed yet."""
    for _ in range(_RECORD_POLL_TICKS):
        try:
            data = agent._poll(run_id)
        except Exception:
            return  # bookkeeping only; never let it matter
        if on_tick is not None:
            try:
                if on_tick(data) is False:
                    return
            except Exception:
                pass
        if data.get("done"):
            return
        time.sleep(_RECORD_POLL_INTERVAL)


def spawn_helper(target: str, prompt: str, permission_mode: str,
                 session_id: str = "", model: str = "", effort: str = "",
                 extra_read_dirs: list[str] | None = None) -> dict:
    """Start a session in-process; return `agent._start`'s result dict.

    `session_id` rides through because a scheduled message may target an
    EXISTING conversation ("" is a fresh one, which is all the apps API ever
    wants). model/effort likewise: empty means "no --model/--effort flag at
    all", which leaves the session on the defaults a chat opened by hand would
    detect for itself. `extra_read_dirs` are folders whose Read the run
    pre-allows — the scheduler passes its task-shots dir so an attached image
    never raises a permission card in a headless run nobody is watching.

    The one error this still rewrites: `_claude_bin`'s FileNotFoundError, whose
    multi-line "Also looked in: ..." message is useless to a user. Say the one
    thing they can act on instead. Everything else propagates as it did from
    the helper's stderr tail — as an `{"error": ...}` dict, never a raise, so
    a scheduler tick or an apps-API request keeps its own error path."""
    agent = load_agent()
    try:
        return agent._start(target, prompt, session_id, model, effort,
                            permission_mode=permission_mode,
                            extra_read_dirs=list(extra_read_dirs or []) or None)
    except FileNotFoundError as exc:
        if "claude CLI not found" in str(exc):
            return {"error":
                    "Claude Code isn't installed (or couldn't be found). "
                    "Install it, check that `claude` runs in a terminal, then "
                    "try again. Help: "
                    "https://render.fused.io/#troubleshooting-notfound"}
        return {"error": "session start failed: " + (str(exc).splitlines() or ["unknown"])[-1]}
    except Exception as exc:  # noqa: BLE001 — same contract as the old helper's stderr tail
        tail = str(exc).splitlines()
        return {"error": "session start failed: " + (tail[-1] if tail else type(exc).__name__)}
