"""Starting a detached Claude Code session FROM THE SERVER PROCESS.

Two features now do this — the apps API's scaffolding turn
(`server/routers/apps.py`) and scheduled messages (`schedule.py`) — and both
have to know the same three awkward things: that `agent._start` cannot be
called in this process at all, where the agent backend lives, and that a run
nobody polls never reaches its sidecar. That is why this module exists rather
than a second copy of the comment block below: the fork-safety reasoning is the
kind that gets paraphrased into something false on the second telling.

No import of anything under `fused_render.server` — `schedule.py` imports this
and the routers import both; keep it acyclic. The one thing that would tempt
such an import is the agent's path, and `core_templates` is where that actually
comes from (`server.templates.TEMPLATES_DIR` *is* `ensure_core_templates()`),
so asking it directly costs nothing and keeps the layering straight.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time

# How long the recording poll follows a run before giving up. A turn can run
# long — a scaffolding pass builds a whole app, and a scheduled message may hand
# the model a substantial job — so this is deliberately generous. At 2s a tick,
# 1800 ticks is ~1h.
_RECORD_POLL_TICKS = 1800
_RECORD_POLL_INTERVAL = 2


def agent_path() -> str:
    """The claude template backend (agent.py) — the STAGED core copy, the same
    file the split app view executes, so the runs dir, sidecar shape
    (`.claude-split.json`) and permission_server path stay in step with what the
    page will poll when the user opens the chat.

    Staging is idempotent (a marker compare, memoized per process), so calling
    it per spawn is a path lookup, not a tree copy."""
    from fused_render.core_templates import ensure_core_templates

    return os.path.join(ensure_core_templates(), "claude", "agent.py")


def load_agent():
    """Load agent.py as a module, for in-process READ paths only (`_poll`).

    The SPAWN goes through `spawn_helper` in a subprocess — see `SESSION_HELPER`
    for why calling `agent._start` in this process crashes it."""
    spec = importlib.util.spec_from_file_location(
        "fused_render_claude_agent", agent_path())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def record_session_when_ready(agent, run_id: str, on_tick=None) -> None:
    """Poll the detached run until it finishes.

    `agent._poll` is what writes the sidecar (the first poll that sees the
    session id records it, one-shot via the run's `recorded` marker) AND what
    commits the finished turn into the folder's repo (one-shot via
    `committed`) — but nobody is polling until the user opens that folder's
    claude chat, which may be never. This background loop polls all the way to
    `done` so both happen regardless: the session is listed when the user does
    look, and the turn's work is committed.

    Bookkeeping only. Every failure here is swallowed: a run whose sidecar entry
    never lands still did its work, and this thread must never be the reason a
    request or a scheduler tick fails.

    `on_tick(data)` is an OPTIONAL observer, called with each poll's result —
    including the final one, which is why it runs BEFORE the `done` check
    (scheduled messages learn the turn's outcome from exactly that tick). Return
    False from it to stop polling early; a caller with nothing to observe passes
    nothing and gets the loop this always was. Its exceptions are swallowed for
    the same reason the poll's are: an observer is not allowed to abandon a run
    whose sidecar has not been written yet."""
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


# The helper the spawn runs in. agent._start cannot be called in THIS process:
# its Popen sets cwd + start_new_session, which forces CPython off posix_spawn
# onto fork()+exec, and the server has libproj resident with a live proj.db
# SQLite handle — fork() runs PROJ's pthread_atfork child handler, which
# sqlite3_close()es that now-invalid handle and SIGSEGVs the child before exec
# (the exact crash test_worker_forksafe.py locks out of the executor; verified
# live: empty out.jsonl, dead pid, a Python .ips crash report with the server
# as parent). So the _start happens one hop away, in a bare python that has no
# libproj loaded and can fork freely. Args ride over stdin as JSON (never
# argv — the prompt is user text); the result comes back as one JSON line.
#
# `session_id` rides through as a real parameter because a scheduled message may
# target an EXISTING conversation ("" is a fresh one, which is all the apps API
# ever wants). model/effort stay empty: neither caller has a picker, so the
# session takes the same defaults a chat opened by hand would.
SESSION_HELPER = """\
import importlib.util, json, sys
req = json.load(sys.stdin)
spec = importlib.util.spec_from_file_location("claude_agent", req["agent"])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(json.dumps(mod._start(req["file"], req["message"], req["session_id"], "", "",
                            permission_mode=req["permission_mode"],
                            message_via_stdin=True)))
"""


def spawn_helper(target: str, prompt: str, permission_mode: str,
                 session_id: str = "") -> dict:
    """Run `agent._start` in the fork-safe helper; return its result dict.

    close_fds=False + no cwd + no start_new_session keeps THIS Popen on the
    posix_spawn path (no atfork handlers — same discipline as executor.py's
    worker spawn). The helper itself detaches claude with setsid; it is a bare
    python where fork() is safe.

    The prompt never enters argv (`input=`, and `message_via_stdin` on the far
    side): this runs inside the server process, whose argv every local user can
    read with `ps`."""
    proc = subprocess.run(
        [sys.executable, "-c", SESSION_HELPER],
        input=json.dumps(
            {"agent": agent_path(), "file": target, "message": prompt,
             "session_id": session_id, "permission_mode": permission_mode}),
        capture_output=True, text=True, timeout=60, close_fds=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        # _claude_bin's FileNotFoundError arrives here as a traceback whose
        # last line is the "Also looked in: ..." tail of a multi-line message
        # — useless on its own. Recognize it and say the one thing the user
        # can act on instead.
        if "claude CLI not found" in stderr:
            return {"error":
                    "Claude Code isn't installed (or couldn't be found). "
                    "Install it, check that `claude` runs in a terminal, then "
                    "try again. Help: "
                    "https://render.fused.io/#troubleshooting-notfound"}
        tail = stderr.splitlines()
        return {"error": "session helper failed: " + (tail[-1] if tail else "unknown")}
    return json.loads(proc.stdout)
