"""The launch spec of a specialist session: its main chat, a breakout, or a learning run (kind `update`).

`agent._start` calls `spec(key, file)` for a start carrying `specialist="<id>:<kind>"` (target: the home), writes
the answer to the run dir, and `_claude_argv` folds it into the CLI's argv:

    kind        main, breakout or update (`_start` records no pill settings for an update)
    mcp_servers the session MCP server (`bots/botmcp.py`) for the connected services' tools
    append      one sentence saying which kind of session this is
    policy_env  for the update, FUSED_SPECIALIST_UNATTENDED: the permission server denies at once anything that
                would raise a card, since nobody is there to answer it
    workbench_skills  a main chat with the canvas tools only: hand the session the `workbench` plugin's format
                skills (canvas.toml, UDFs, JSON-UI widgets), as a canvas task gets them
    ask         a main chat with the canvas tools only: the tools that raise the approval card in every mode
                (`canvas_create`, show.py)

Apart from `ask`, nothing in it limits the session: the specialist runs free under its permission mode (the
owner's decision). While a
learning run goes, a main chat or breakout cannot start in the home. Anything wrong answers
`{"error": sentence, "reason": code}` and the start is refused.
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from fused_render.specialists import flag, history, paths, services, show, store, update
from fused_render.specialists import home as hm

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
    """A token for the session MCP servers of one session: it names the specialist and nothing else."""
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
def session_servers(spec: store.Specialist, canvas: bool = False) -> dict:
    """`{name: mcp entry}`, one per services server: the bots' stdio pipe (`bots/botmcp.py`) pointed at
    `/api/specialists/<id>/<server>/` (routes.py). `canvas` adds the main chat's `specialist_canvas` server
    (show.py)."""
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
                              f"api/specialists/{spec.id}/{server}", server],
                     "env": {"PYTHONUTF8": "1"}, "timeout": timeout, **agent._ALWAYS_LOAD_KEYS["claude"]}
            for server in services.SERVERS + ((show.SERVER,) if canvas else ())}


def spec(key: str, file: str) -> dict:
    """The launch spec for start key `key` on target `file`, or `{"error", "reason"}`."""
    if not flag.on():
        return {"error": "Specialists are off", "reason": "specialists_disabled"}
    spec_id, _, kind = str(key or "").partition(":")
    if kind not in KINDS:
        return {"error": "Bad specialist start key", "reason": "bad_key"}
    s = store.get(spec_id)
    if s is None:
        return {"error": "That specialist no longer exists", "reason": "no_specialist"}
    if not file or os.path.realpath(file) != os.path.realpath(s.home):
        return {"error": "A specialist session starts in the specialist's own folder", "reason": "wrong_folder"}
    run = kind == "update"
    if not run and update.learning(s.home):
        return {"error": update.LEARNING, "reason": "learning"}
    hm.ensure_settings(s.home)
    if hm.rewrite_charter(s.home, s.name, s.about):  # a home made under an older charter
        history.commit(s.home, "Settings", ["CLAUDE.md"])
    history.commit_edits(s.home)
    canvas = kind == "main" and show.on()  # the main chat only: a breakout and a learning run show nothing
    skills = canvas and show.skills()
    return {"kind": kind, "mcp_servers": session_servers(s, canvas),
            "append": APPEND[kind] + (" " + show.note(skills) if canvas else ""),
            "policy_env": {"FUSED_SPECIALIST_UNATTENDED": "1"} if run else {},
            **({"ask": show.ASK} if canvas else {}), **({"workbench_skills": True} if skills else {})}
