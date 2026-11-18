"""The launch spec of a specialist main chat or breakout (PLAN A.2, A.7, C.1 notes).

Main chat and breakouts run on the task chat stack with the home as target. `agent._start` calls
`spec(key, file)` for a start carrying `specialist="<id>:main"` or `"<id>:breakout:<bid>"`, writes the answer to
the run dir as `specialist.json`, and `_claude_argv` folds it into the CLI's argv:

    deny        Edit rules (both spellings) for the paths this kind may not write
    ask         rules that force the permission prompt tool even in auto/bypass modes (service calls)
    allow       pre-allowed rules: the note tool, Read rules for a breakout's parent folder and transcript dir
    mcp_servers `specialist` (note) and `specialist_services` (the roster), both `sessmcp.py`
    hooks       Stop -> specialist_hook.py stop (history.after_turn: commit / guard)
    append      one paragraph saying which kind of session this is
    policy_env  FUSED_SPECIALIST_POLICY for the permission server's policy seam
    token       the session MCP servers' token (`.state/tokens.json`, 7-day expiry)

Any problem answers `{"error": sentence, "reason": code}` and the start is refused.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time

from fused_render.specialists import harness
from fused_render.specialists import home as hm
from fused_render.specialists import paths

TOKEN_TTL_S = 7 * 24 * 3600
SERVERS = ("specialist", "specialist_services")
NOTE_TOOL = "mcp__specialist__note"
SERVICES_RULE = "mcp__specialist_services"
#: Breakout: everything the specialist knows plus app state (home.PROTECTED).
BREAKOUT_DENY = hm.PROTECTED
MAIN_DENY = (".git/**", ".specialist/**", "inbox/**", "said.md")
#: How `ask` names service tools. "server": one rule for the whole server; "tools": one rule per roster tool
#: (the fallback PLAN D.1 names, used when a whole-server ask rule does not force the prompt in auto mode).
ASK_MODE = "server"
_TOKENS_LOCK = threading.Lock()


def root() -> str:
    """The specialists root (agent.py's string test for "inside a home": no folder is created)."""
    return paths.root()


def _err(msg: str, reason: str) -> dict:
    return {"error": msg, "reason": reason}


def parse_key(key: str) -> tuple[str, str, str] | None:
    parts = str(key or "").split(":")
    if len(parts) == 2 and parts[1] == "main" and parts[0]:
        return parts[0], "main", ""
    if len(parts) == 3 and parts[1] == "breakout" and parts[0] and parts[2]:
        return parts[0], "breakout", parts[2]
    return None


# ------------------------------------------------------------------- tokens ---
def _tokens_path() -> str:
    return os.path.join(paths.state_root(), "tokens.json")


def new_token(spec_id: str, kind: str, breakout_id: str = "") -> str:
    tok = secrets.token_urlsafe(24)
    now = time.time()
    with _TOKENS_LOCK:
        d = hm.read_json(_tokens_path(), {})
        d = {k: v for k, v in (d.items() if isinstance(d, dict) else [])
             if isinstance(v, dict) and float(v.get("exp") or 0) > now}
        d[tok] = {"spec": spec_id, "kind": kind, "breakout_id": breakout_id, "exp": now + TOKEN_TTL_S}
        hm.write_json(_tokens_path(), d)
    return tok


def check_token(spec_id: str, kind: str, token: str) -> dict | None:
    """The token's record when it is live and names this specialist and kind, else None."""
    if not token:
        return None
    d = hm.read_json(_tokens_path(), {})
    rec = d.get(token) if isinstance(d, dict) else None
    if not isinstance(rec, dict) or float(rec.get("exp") or 0) <= time.time():
        return None
    if rec.get("spec") != spec_id or rec.get("kind") != kind:
        return None
    return rec


# ------------------------------------------------------------------ servers ---
def _origin() -> str:
    try:
        from fused_render.bots import paths as bpaths

        return bpaths.server_origin_quiet()
    except Exception:  # noqa: BLE001
        return os.environ.get("FUSED_RENDER_ORIGIN", "").rstrip("/")


def _python() -> str:
    try:
        from fused_render.claude_agent import agent_module

        return agent_module()._stable_python()
    except Exception:  # noqa: BLE001
        return sys.executable


def _sessmcp_path() -> str:
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessmcp.py")
    try:
        from fused_render.claude_agent import agent_module

        return agent_module()._stable_script(src) or src
    except Exception:  # noqa: BLE001
        return src


def _timeout_ms() -> int:
    try:
        from fused_render.claude_agent import agent_module

        wait = int(agent_module()._permission_wait())
    except Exception:  # noqa: BLE001
        wait = 3600
    return (wait + 60) * 1000


def _always_load() -> dict:
    try:
        from fused_render.claude_agent import agent_module

        return dict(agent_module()._ALWAYS_LOAD_KEYS["claude"])
    except Exception:  # noqa: BLE001
        return {"alwaysLoad": True}


def session_servers(spec, kind: str, *, token: str = "", breakout_id: str = "", only=SERVERS) -> dict:
    """`{name: mcp entry}` for the session MCP servers of `spec` and `kind` (sessmcp.py argv
    `<origin> <id> <kind> <token> <server>`). A fresh token when none is given."""
    tok = token or new_token(spec.id, kind, breakout_id)
    script, py, origin = _sessmcp_path(), _python(), _origin()
    out = {}
    for server in only:
        out[server] = {"command": py, "args": [script, origin, spec.id, kind, tok, server],
                       "env": {"PYTHONUTF8": "1"}, "timeout": _timeout_ms(), **_always_load()}
    return out


# --------------------------------------------------------------------- spec ---
def deny_rules(home: str, kind: str) -> list[str]:
    return harness.edit_rules(home, BREAKOUT_DENY if kind == "breakout" else MAIN_DENY)


def ask_rules(spec, kind: str) -> list[str]:
    if ASK_MODE == "server":
        return [SERVICES_RULE]
    from fused_render.specialists import services

    return [services.TOOL_PREFIX + e["name"] for e in services.roster(spec, kind)] or [SERVICES_RULE]


def _breakout_reads(spec_id: str, bid: str) -> tuple[list[str], dict | None, str]:
    """Read dirs for a breakout (the parent's folder and transcript dir), its record, and an error sentence."""
    try:
        from fused_render.specialists import breakouts
    except ImportError:
        return [], None, ""
    try:
        rec = breakouts.get(spec_id, bid)
    except Exception:  # noqa: BLE001 — a stub
        return [], None, ""
    if not isinstance(rec, dict):
        return [], None, "That breakout no longer exists"
    if rec.get("status") == "deleted":
        return [], rec, "That specialist was deleted"
    parent = rec.get("parent") if isinstance(rec.get("parent"), dict) else {}
    dirs = []
    pfile = str(parent.get("file") or "")
    if pfile:
        try:
            from fused_render.claude_agent import agent_module

            ag = agent_module()
            wd = ag._workdir(pfile)
            dirs.append(wd)
            if parent.get("session_id"):
                dirs.append(os.path.dirname(ag._session_transcript_path(wd, parent["session_id"])))
        except Exception:  # noqa: BLE001
            dirs.append(pfile if os.path.isdir(pfile) else os.path.dirname(pfile))
    return [d for d in dirs if d], rec, ""


def append_text(spec, kind: str, rec: dict | None) -> str:
    if kind == "main":
        return (f"SPECIALIST: This is your main chat with the user (you are the {spec.name} specialist). "
                "When the user tells you to remember something or change how you work, change memory.md or the "
                "skill at once and say what you changed. Send other lessons to your inbox with "
                f"{NOTE_TOOL}.")
    title = ""
    if isinstance(rec, dict) and isinstance(rec.get("parent"), dict):
        title = str(rec["parent"].get("title") or "")
    return (f"SPECIALIST: This is a breakout from the task \"{title or 'a task'}\" (you are the {spec.name} "
            "specialist). Memory, knowledge, skills, people and triggers are locked in a breakout; to record "
            f"something you learned, use the {NOTE_TOOL} tool. A breakout cannot open another breakout.")


def spec(key: str, file: str) -> dict:
    """The launch spec for start key `key` on target `file` (the home), or `{"error","reason"}`."""
    from fused_render.specialists import flag, store

    if not flag.on():
        return _err("Specialists are off", "specialists_disabled")
    parsed = parse_key(key)
    if parsed is None:
        return _err("Bad specialist start key", "bad_key")
    spec_id, kind, bid = parsed
    s = store.get(spec_id)
    if s is None:
        return _err("That specialist no longer exists", "no_specialist")
    if not file or os.path.realpath(file) != os.path.realpath(s.home):
        return _err("A specialist session must start in the specialist's home folder", "wrong_folder")
    reads, rec = [], None
    if kind == "breakout":
        reads, rec, err = _breakout_reads(spec_id, bid)
        if err:
            return _err(err, "no_breakout")
    token = new_token(spec_id, kind, bid)
    origin = _origin()
    return {
        "id": spec_id, "kind": kind, "breakout_id": bid, "home": s.home,
        "deny": deny_rules(s.home, kind),
        "ask": ask_rules(s, kind),
        "allow": [NOTE_TOOL] + harness.read_rules(reads),
        "mcp_servers": session_servers(s, kind, token=token, breakout_id=bid),
        "hooks": {"Stop": [{"hooks": [{"specialist_hook": "stop"}]}]},
        "append": append_text(s, kind, rec),
        "policy_env": {"FUSED_SPECIALIST_POLICY": f"{origin}|{spec_id}|{kind}"},
        "token": token,
    }
