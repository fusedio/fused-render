"""The launch spec of an expert session: its main chat, a breakout, or a learning run (kind `update`).

`agent._start` calls `spec(key, file)` for a start carrying `expert="<id>:<kind>"` (target: the home), writes
the answer to the run dir, and `_claude_argv` folds it into the CLI's argv:

    kind        main, breakout or update (`_start` records no pill settings for an update)
    mcp_servers the session MCP server (`bots/botmcp.py`) for the connected services' tools
    append      one sentence saying which kind of session this is
    policy_env  for the update, FUSED_EXPERT_UNATTENDED: the permission server denies at once anything that
                would raise a card, since nobody is there to answer it
    workbench_skills  a main chat with the canvas tools only: hand the session the `workbench` plugin's format
                skills (canvas.toml, UDFs, JSON-UI widgets), as a canvas task gets them
    ask         the tools that raise the approval card in every mode: `canvas_create` (show.py, a main chat with
                the canvas tools) and `task_start` (tasks.py, a main chat or a breakout)

Apart from `ask`, nothing in it limits the session: the expert runs free under its permission mode (the
owner's decision). While a
learning run goes, a main chat or breakout cannot start in the home. Anything wrong answers
`{"error": sentence, "reason": code}` and the start is refused.
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from fused_render.experts import flag, history, paths, services, show, store, tasks, update
from fused_render.experts import home as hm

KINDS = ("main", "breakout", "update")
TOKEN_TTL_S = 7 * 24 * 3600
APPEND = {
    "main": "This is your main chat with the user.",
    "breakout": "This is a breakout beside one of the user's tasks; your next learning run reads it.",
    "update": "This is your learning run; nobody is watching it.",
}
_TOKENS_LOCK = threading.Lock()


# ------------------------------------------------------------------- tokens ---
def _tokens_path() -> str:
    return os.path.join(paths.state_root(), "tokens.json")


def new_token(spec_id: str) -> str:
    """A token for the session MCP servers of one session: it names the expert and nothing else."""
    tok = secrets.token_urlsafe(24)
    now = time.time()
    with _TOKENS_LOCK:
        d = {k: v for k, v in hm.read_json(_tokens_path(), {}).items() if v["exp"] > now}
        d[tok] = {"spec": spec_id, "exp": now + TOKEN_TTL_S}
        hm.write_json(_tokens_path(), d)
    return tok


def check_token(spec_id: str, token: str) -> bool:
    rec = hm.read_json(_tokens_path(), {}).get(token or "")
    return bool(rec) and rec["spec"] == spec_id and rec["exp"] > time.time()


# ------------------------------------------------------------------ servers ---
def session_servers(spec: store.Expert, canvas: bool = False, task_tools: bool = False) -> dict:
    """`{name: mcp entry}`, one per services server: the bots' stdio pipe (`bots/botmcp.py`) pointed at
    `/api/experts/<id>/<server>/` (routes.py). `canvas` adds the main chat's `expert_canvas` server
    (show.py); `task_tools` the `expert_tasks` server of a main chat or breakout (tasks.py)."""
    from fused_render.bots import paths as bpaths
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bots", "botmcp.py")
    script = agent._stable_script(src) or src
    token = new_token(spec.id)
    # A call may wait behind an approval card, as long as the permission server waits.
    timeout = (int(agent._permission_wait()) + 60) * 1000
    return {server: {"command": agent._stable_python(),
                     "args": [script, bpaths.server_origin_quiet(), spec.id, token,
                              f"api/experts/{spec.id}/{server}", server],
                     "env": {"PYTHONUTF8": "1"}, "timeout": timeout, **agent._ALWAYS_LOAD_KEYS["claude"]}
            for server in services.SERVERS + ((show.SERVER,) if canvas else ())
            + ((tasks.SERVER,) if task_tools else ())}


def spec(key: str, file: str) -> dict:
    """The launch spec for start key `key` on target `file`, or `{"error", "reason"}`."""
    if not flag.on():
        return {"error": "Experts are off", "reason": "experts_disabled"}
    spec_id, _, kind = str(key or "").partition(":")
    if kind not in KINDS:
        return {"error": "Bad expert start key", "reason": "bad_key"}
    s = store.get(spec_id)
    if s is None:
        return {"error": "That expert no longer exists", "reason": "no_expert"}
    if not file or os.path.realpath(file) != os.path.realpath(s.home):
        return {"error": "An expert session starts in the expert's own folder", "reason": "wrong_folder"}
    run = kind == "update"
    if not run and update.learning(s.home):
        return {"error": update.LEARNING, "reason": "learning"}
    hm.ensure_settings(s.home)
    if hm.rewrite_charter(s.home, s.name, s.about):  # a home made under an older charter
        history.commit(s.home, "Settings", ["CLAUDE.md"])
    history.commit_edits(s.home)
    canvas = kind == "main" and show.on()  # the main chat only: a breakout and a learning run show nothing
    skills = canvas and show.skills()
    # A main chat and a breakout start and manage Claude tasks (tasks.py); a learning run does not: nobody is
    # there to approve a start.
    ask = (show.ASK if canvas else []) + ([] if run else tasks.ASK)
    return {"kind": kind, "mcp_servers": session_servers(s, canvas, task_tools=not run),
            "append": APPEND[kind] + (" " + show.note(skills) if canvas else "") + ("" if run else " " + tasks.NOTE),
            "policy_env": {"FUSED_EXPERT_UNATTENDED": "1"} if run else {},
            **({"ask": ask} if ask else {}), **({"workbench_skills": True} if skills else {})}
