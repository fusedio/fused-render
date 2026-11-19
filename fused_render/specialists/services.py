"""The app's connected services in a specialist's main chat and breakouts.

The roster is the bots' one (`apptools.registry()`: local app tools and connected services' tools; the tokens stay
in the server), served as one session MCP server, `specialist_services`. Reads and sends alike go through the
session's permission mode like any other tool.
"""
from __future__ import annotations

import hashlib
import re

SERVERS = ("specialist_services",)
NAME_MAX = 64
_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


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


def _roster() -> dict:
    """`{name: (record, service title)}`."""
    at = _apptools()
    out = {}
    for rec in at.registry():
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
    """The server's tools as MCP definitions; each description names its service."""
    return [{"name": name, "description": f"[{title}] {rec.description or ''}".strip()[:1024],
             "inputSchema": _schema(rec)} for name, (rec, title) in _roster().items()]


def call(spec, server: str, name: str, args) -> dict:
    """Run one tool of `server`: `{content, isError}`. Never raises."""
    entry = _roster().get(name)
    if entry is None:
        return {"content": [{"type": "text", "text": f"error: no such tool ({name})"}], "isError": True}
    res = _apptools().run_tool(entry[0], args if isinstance(args, dict) else {})
    return {"content": [{"type": "text", "text": str(res.text or "")}], "isError": not res.ok}
