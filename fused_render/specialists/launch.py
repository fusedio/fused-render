"""The launch spec of a specialist main chat or breakout (PLAN A.2, A.7, C.1 notes).

Main chat and breakouts run on the task chat stack with the home as target. `agent._start` calls
`spec(key, file)` for a start carrying `specialist="<id>:main"` (target: the home) or `"<id>:breakout:<bid>"`
(target: the breakout's workroom, `breakouts.cwd`; CONTRACT C1), writes the answer to
the run dir as `specialist.json`, and `_claude_argv` folds it into the CLI's argv:

    deny        Edit rules (both spellings) for the paths this kind may not write
    ask         rules that force the permission prompt tool even in auto/bypass modes (service calls)
    allow       pre-allowed rules: the note tool; for a breakout, Read rules for the home, the parent's folder and
                its transcript dir
    mcp_servers `specialist` (note) and `specialist_services` (the roster), both `sessmcp.py`
    hooks       Stop -> specialist_hook.py stop (history.after_turn: commit / guard)
    append      one paragraph saying which kind of session this is
    policy_env  FUSED_SPECIALIST_POLICY for the permission server's policy seam (no secret: CONTRACT C2)
    token       the session MCP servers' token (`.state/tokens.json`, 7-day expiry)

Before answering it rewrites the home's `.claude/settings.json` to the canonical text (R4) and, for a breakout,
refreshes its workroom. Any problem answers `{"error": sentence, "reason": code}` and the start is refused.
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
#: Main chat: app state, plus the project's Claude Code settings (a hook added there would run in every
#: unattended side session of this home).
MAIN_DENY = (".git/**", ".specialist/**", "inbox/**", "said.md", ".claude/settings.json",
             ".claude/settings.local.json")
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
    """A session token for the session MCP servers of one run. It names the specialist, the kind and the breakout,
    nothing else: it can call tools, never grant one (CONTRACT C2)."""
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


# ------------------------------------------------------------------- grants ---
# A service call that is not `allow` runs only after the user answered its card (CONTRACT C2). The grant is
# recorded by the SERVER when the user's `decide allow` for that card passes through it (`agent._decide` ->
# `agent_hooks.grant_from_decision`, before the decision is written, so the call can never arrive first); nothing a
# session can read lets it record one. `services.call` consumes it: one call, within `GRANT_TTL_S`, of the same
# specialist, kind, breakout, session and tool.
GRANT_TTL_S = 120.0


def _grants_path() -> str:
    return os.path.join(paths.state_root(), "grants.json")


def _tool_key(tool: str) -> str:
    from fused_render.specialists import services

    return str(tool or "").removeprefix(services.TOOL_PREFIX)


def record_grant(spec_id: str, kind: str, breakout_id: str, session_id: str, tool: str, key: str) -> bool:
    """Record a one-time grant (valid `GRANT_TTL_S`), once per `key` (the card: `<run id>/<request id>`), so a
    second report of the same decision grants nothing more. Server-side callers only."""
    if not spec_id or kind not in ("main", "breakout") or not tool or not key:
        return False
    now = time.time()
    with _TOKENS_LOCK:
        d = hm.read_json(_grants_path(), {})
        d = {k: v for k, v in (d.items() if isinstance(d, dict) else []) if isinstance(v, dict)
             and float(v.get("exp") or 0) > now}
        if key in d or key in _SPENT:
            return True
        d[key] = {"spec": spec_id, "kind": kind, "breakout_id": breakout_id or "", "session_id": session_id or "",
                  "tool": _tool_key(tool), "exp": now + GRANT_TTL_S}
        hm.write_json(_grants_path(), d)
    return True


#: Card keys whose grant was spent (this process): a late second report of the decision grants nothing.
_SPENT: dict[str, float] = {}


def take_grant(spec_id: str, kind: str, breakout_id: str, session_id: str, tool: str) -> bool:
    """Consume one live grant for this specialist, kind, breakout and tool (and session, when both sides know
    it). True when there was one."""
    now, want = time.time(), _tool_key(tool)
    with _TOKENS_LOCK:
        d = hm.read_json(_grants_path(), {})
        d = d if isinstance(d, dict) else {}

        def fits(v) -> bool:
            if not isinstance(v, dict) or float(v.get("exp") or 0) <= now:
                return False
            if v.get("spec") != spec_id or v.get("kind") != kind or v.get("tool") != want:
                return False
            if str(v.get("breakout_id") or "") != str(breakout_id or ""):
                return False
            return not (v.get("session_id") and session_id) or v.get("session_id") == session_id

        hit = next((k for k, v in d.items() if fits(v)), None)
        if hit is None:
            return False
        d.pop(hit)
        _SPENT[hit] = now
        for k in [k for k, t in _SPENT.items() if now - t > GRANT_TTL_S * 2]:
            _SPENT.pop(k, None)
        hm.write_json(_grants_path(), {k: v for k, v in d.items()
                                       if isinstance(v, dict) and float(v.get("exp") or 0) > now})
    return True


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
        # The token travels in the server's environment, not its argv (a process list shows argv to anyone).
        out[server] = {"command": py, "args": [script, origin, spec.id, kind, "-", server],
                       "env": {"PYTHONUTF8": "1", "FUSED_SPECIALIST_TOKEN": tok},
                       "timeout": _timeout_ms(), **_always_load()}
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
    reads, rec = [], None
    if kind == "breakout":
        reads, rec, err = _breakout_reads(spec_id, bid)
        if err:
            return _err(err, "no_breakout")
        if not isinstance(rec, dict):
            return _err("That breakout no longer exists", "no_breakout")
        if rec.get("status") != "open":
            return _err("This breakout is closed; reopen it to continue.", "breakout_closed")
        from fused_render.specialists import breakouts

        folder = breakouts.sync(s, bid)
        if not file or os.path.realpath(file) != os.path.realpath(folder):
            return _err("A breakout must start in its own folder", "wrong_folder")
        reads = [s.home] + reads
    elif not file or os.path.realpath(file) != os.path.realpath(s.home):
        return _err("A specialist session must start in the specialist's home folder", "wrong_folder")
    hm.ensure_settings(s.home)
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
