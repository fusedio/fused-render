"""The app's connected services in a specialist's main chat and breakouts.

The roster is the bots' one (`apptools.registry()`: local app tools and connected services' tools; the tokens stay
in the server), split in two session MCP servers: reads in `specialist_services`, sends in `specialist_sends`. The
launch spec gives sends an ask rule, so the CLI raises the app's card for each one in every permission mode, and
`call` runs a send only by spending the one-time grant `agent._decide` recorded (`record_grant`) when the user
allowed that card, so a session calling the route itself gets nowhere.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time

SERVERS = ("specialist_services", "specialist_sends")
SENDS_PREFIX = "mcp__specialist_sends__"
GRANT_TTL_S = 120.0
NAME_MAX = 64
NEEDS_CARD = "This send needs the user's approval card."
_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
_GRANTS: dict[str, tuple[str, str, float]] = {}  # "<run>/<request>" -> (specialist id, tool, expiry)
_LOCK = threading.Lock()


def _apptools():
    from fused_render.bots import apptools

    return apptools


def tool_name(key: str, tool: str) -> str:
    """`<service>_<tool>` in MCP's charset, at most 64 characters (a long one ends in a hash of the full name)."""
    raw = f"{key}_{tool}"
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "tool"
    if len(name) > NAME_MAX:
        name = name[:NAME_MAX - 7].rstrip("_") + "_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    return name


def _roster(server: str) -> dict:
    """`{name: (record, service title)}` for one server: reads or sends."""
    at = _apptools()
    out = {}
    for rec in at.registry():
        if at.is_write(rec) != (server == "specialist_sends"):
            continue
        key = at.connect_key(rec) if at.is_connect(rec) else at.norm_app(rec.app)
        title = at.connect_title(key) if at.is_connect(rec) else str(rec.app)
        out.setdefault(tool_name(key, rec.name), (rec, title))
    return out


def _schema(rec) -> dict:
    sch = getattr(getattr(rec, "spec", None), "input_schema", None)
    if isinstance(sch, dict):
        return sch
    params = getattr(rec, "params", None) or []
    props = {p.name: ({"type": _TYPES[p.annotation]} if p.annotation in _TYPES else {}) for p in params}
    req = [p.name for p in params if not p.has_default]
    return {"type": "object", "properties": props, **({"required": req} if req else {})}


def mcp_tools(server: str) -> list[dict]:
    """One server's tools as MCP definitions; each description names its service."""
    verb = "sends" if server == "specialist_sends" else "reads"
    return [{"name": name, "description": f"[{title}, {verb}] {rec.description or ''}".strip()[:1024],
             "inputSchema": _schema(rec)} for name, (rec, title) in _roster(server).items()]


def _take_grant(spec_id: str, tool: str) -> bool:
    now = time.time()
    with _LOCK:
        for key, (sid, t, exp) in list(_GRANTS.items()):
            if exp <= now:
                _GRANTS.pop(key)
            elif sid == spec_id and t == tool:
                _GRANTS[key] = (sid, "", exp)  # spent, but kept till expiry: the same card never grants twice
                return True
    return False


def call(spec, server: str, name: str, args) -> dict:
    """Run one tool of `server`: `{content, isError}`. A send spends a grant first. Never raises."""
    entry = _roster(server).get(name)
    if entry is None:
        return {"content": [{"type": "text", "text": f"error: no such tool ({name})"}], "isError": True}
    if server == "specialist_sends" and not _take_grant(spec.id, name):
        return {"content": [{"type": "text", "text": NEEDS_CARD}], "isError": True}
    res = _apptools().run_tool(entry[0], args if isinstance(args, dict) else {})
    return {"content": [{"type": "text", "text": str(res.text or "")}], "isError": not res.ok}


def record_grant(run_id: str, request_id: str) -> None:
    """The user allowed card `request_id` of run `run_id`, a send in a specialist's session: grant that one send
    for GRANT_TTL_S. Recording the same card twice grants it once."""
    from fused_render.claude_agent import agent_module
    from fused_render.specialists import paths

    run_dir = os.path.join(agent_module().RUNS, run_id)
    try:
        with open(os.path.join(run_dir, "perm", request_id + ".req.json"), encoding="utf-8") as fh:
            tool = str(json.load(fh).get("tool") or "")
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
            spec_id = paths.spec_of_path(str(json.load(fh).get("file") or ""))
    except (OSError, ValueError, AttributeError):
        return
    if spec_id and tool.startswith(SENDS_PREFIX):
        with _LOCK:
            _GRANTS.setdefault(f"{run_id}/{request_id}", (spec_id, tool[len(SENDS_PREFIX):], time.time() + GRANT_TTL_S))
