"""The launch spec of a specialist session: its main chat, a breakout, or the update.

`agent._start` calls `spec(key, file)` for a start carrying `specialist="<id>:<kind>"` (target: the home), writes
the answer to the run dir, and `_claude_argv` folds it into the CLI's argv:

    deny        Edit rules for the app's own files (`.git`, `.specialist`, the project's Claude Code settings)
    ask         `mcp__specialist_sends`: a send to a connected service raises the approval card in every mode
    mcp_servers the session MCP servers (`sessmcp.py`) for the connected services' reads and sends
    append      one sentence saying which kind of session this is
    policy_env  for the update, FUSED_SPECIALIST_UNATTENDED: the permission server denies at once anything that
                would raise a card, since nobody is there to answer it

The update has no service servers (with `isolate_mcp`, no MCP server at all), so it can never message a person.
Anything wrong answers `{"error": sentence, "reason": code}` and the start is refused.
"""
from __future__ import annotations

import os
import secrets
import threading
import time

from fused_render.specialists import flag, history, paths, services, store
from fused_render.specialists import home as hm

KINDS = ("main", "breakout", "update")
TOKEN_TTL_S = 7 * 24 * 3600
DENY = (".git/**", paths.APP_DIR + "/**", ".claude/settings.json", ".claude/settings.local.json")
APPEND = {
    "main": "This is your main chat with the user.",
    "breakout": ("This is a breakout beside one of the user's tasks. Your next update reads this conversation, so do "
                 "not edit memory.md or your skills here."),
    "update": "Nobody is watching this run: work only with the files in this folder.",
}
_TOKENS_LOCK = threading.Lock()


def root() -> str:
    """The specialists root (agent.py's string test for "inside a home")."""
    return paths.root()


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
def session_servers(spec: store.Specialist, kind: str) -> dict:
    """`{name: mcp entry}` for `sessmcp.py <origin> <id> <kind> - <server>`, one per services server."""
    from fused_render.bots import paths as bpaths
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessmcp.py")
    script = agent._stable_script(src) or src
    # The token travels in the environment, not argv (a process list shows argv to anyone).
    env = {"PYTHONUTF8": "1", "FUSED_SPECIALIST_TOKEN": new_token(spec.id)}
    # A send waits behind its approval card, as long as the permission server waits.
    timeout = (int(agent._permission_wait()) + 60) * 1000
    return {server: {"command": agent._stable_python(),
                     "args": [script, bpaths.server_origin_quiet(), spec.id, kind, "-", server],
                     "env": env, "timeout": timeout, **agent._ALWAYS_LOAD_KEYS["claude"]}
            for server in services.SERVERS}


def _edit_rules(home: str) -> list[str]:
    """Edit rules for `DENY` under both spellings of the home (the CLI matches a rule's text)."""
    spellings = dict.fromkeys((os.path.abspath(home), os.path.realpath(home)))
    return [f"Edit(//{os.path.join(h, g).replace(os.sep, '/').lstrip('/')})" for h in spellings for g in DENY]


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
    hm.ensure_settings(s.home)
    history.commit_edits(s.home)
    update = kind == "update"
    return {"id": s.id, "kind": kind, "home": s.home, "deny": _edit_rules(s.home),
            "ask": [] if update else ["mcp__specialist_sends"],
            "mcp_servers": {} if update else session_servers(s, kind),
            "append": APPEND[kind],
            "policy_env": {"FUSED_SPECIALIST_UNATTENDED": "1"} if update else {}}
