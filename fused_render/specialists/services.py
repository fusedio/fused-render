"""Connected services for specialists (PRODUCT §7, PLAN A.2 "Connected services", C.1 notes).

The roster is the bots' one: `apptools.registry()` = local app tools (apps with an MCP manifest) + every connected
service's tools (`apptools.connect_records()`), the token kept in the server. Per specialist the manifest's
`connections` lists the allowed services, keyed as the bots key them: a connect key (`notion`) or
`app:<normalised app folder>` for a local app.

Who sees what (`roster`): main chat and breakout get every service's tools (allowed or not: one not allowed asks
through the card, and "Approve, don't ask again" ticks it); the nightly job gets only the read-only tools of allowed
services; check and reviewer get none. Every call runs here (`call`), behind the session MCP server
(`sessmcp.py`), and the permission prompt tool asks `policy` first whether to card it.
"""
from __future__ import annotations

import hashlib
import logging
import re

logger = logging.getLogger(__name__)

APP_PREFIX = "app:"
NAME_MAX = 64
SERVER = "specialist_services"
TOOL_PREFIX = f"mcp__{SERVER}__"


def _apptools():
    from fused_render.bots import apptools

    return apptools


def norm(key: str) -> str:
    k = str(key or "").strip()
    if k.lower().startswith(APP_PREFIX):
        return APP_PREFIX + _apptools().norm_app(k[len(APP_PREFIX):])
    return _apptools().norm_app(k)


def key_of(rec) -> str:
    at = _apptools()
    return norm(at.connect_key(rec)) if at.is_connect(rec) else APP_PREFIX + at.norm_app(rec.app)


def title_of(rec) -> str:
    at = _apptools()
    return at.connect_title(at.connect_key(rec)) if at.is_connect(rec) else str(rec.app)


def tool_name(key: str, tool: str) -> str:
    """`<key>_<tool>` in MCP's charset, at most 64 chars (truncated + a 6-hex hash of the full name, so two long
    names never collide)."""
    raw = f"{key}_{tool}"
    name = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "tool"
    if len(name) > NAME_MAX:
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
        name = name[:NAME_MAX - 7].rstrip("_") + "_" + h
    return name


def _all_records() -> list:
    try:
        return list(_apptools().registry())
    except Exception:  # noqa: BLE001 — a broken roster must not break a session
        logger.debug("specialists: roster failed", exc_info=True)
        return []


def is_write(rec) -> bool:
    try:
        return bool(_apptools().is_write(rec))
    except Exception:  # noqa: BLE001
        return True


def allowed_set(spec) -> set:
    return {norm(k) for k in (getattr(spec, "connections", None) or [])}


def _schema(rec) -> dict:
    sp = getattr(rec, "spec", None)
    sch = getattr(sp, "input_schema", None)
    if isinstance(sch, dict):
        return sch
    props, req = {}, []
    for p in getattr(rec, "params", None) or []:
        t = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
        props[p.name] = {"type": t.get(p.annotation, "string")} if p.annotation in t else {}
        if not p.has_default:
            req.append(p.name)
    return {"type": "object", "properties": props, **({"required": req} if req else {})}


def roster(spec, kind: str) -> list[dict]:
    """`[{"name","key","title","write","rec"}]` the session of `kind` may see (module docstring)."""
    if kind not in ("main", "breakout", "nightly"):
        return []
    allowed = allowed_set(spec)
    out, seen = [], set()
    for rec in _all_records():
        key = key_of(rec)
        write = is_write(rec)
        if kind == "nightly" and (key not in allowed or write):
            continue
        name = tool_name(key, rec.name)
        if name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "key": key, "title": title_of(rec), "write": write, "rec": rec})
    return out


def mcp_tools(spec, kind: str) -> list[dict]:
    """The roster as MCP tool definitions; each description says which service and whether it writes."""
    tools = []
    for e in roster(spec, kind):
        desc = str(getattr(e["rec"], "description", "") or "").strip()
        tag = f"[{e['title']}, {'writes' if e['write'] else 'read-only'}]"
        tools.append({"name": e["name"], "description": f"{tag} {desc}".strip()[:1024],
                      "inputSchema": _schema(e["rec"])})
    return tools


def _strip(tool: str) -> str:
    return tool[len(TOOL_PREFIX):] if tool.startswith(TOOL_PREFIX) else tool


def find(spec, kind: str, tool: str) -> dict | None:
    name = _strip(str(tool or ""))
    return next((e for e in roster(spec, "main" if kind == "nightly" else kind) if e["name"] == name), None)


def call(spec, kind: str, name: str, args) -> dict:
    """Run one service tool for a session of `kind`: `{"content":[{"type":"text","text"}], "isError"}`. Refuses
    a tool not in that kind's roster (so a nightly can never write) and never raises."""
    entry = next((e for e in roster(spec, kind) if e["name"] == _strip(str(name or ""))), None)
    if entry is None:
        why = ("the nightly update may only use read-only tools of allowed services" if kind == "nightly"
               else "no such service tool for this specialist")
        return {"content": [{"type": "text", "text": f"error: {why} ({name})"}], "isError": True}
    try:
        res = _apptools().run_tool(entry["rec"], args if isinstance(args, dict) else {})
    except Exception as e:  # noqa: BLE001
        return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    text = str(getattr(res, "text", "") or "")
    if getattr(res, "dropped", None):
        text += "\n(ignored unknown arguments: " + ", ".join(res.dropped) + ")"
    return {"content": [{"type": "text", "text": text}], "isError": not bool(getattr(res, "ok", False))}


def _send_pending(spec_id: str, session_id: str) -> bool:
    try:
        from fused_render.specialists import drafts

        return bool(drafts.send_pending(spec_id, session_id))
    except Exception:  # noqa: BLE001 — stream 3 not there yet, or nothing pending
        return False


def policy(spec_id: str, kind: str, session_id: str, tool: str) -> str:
    """`allow` (no card), `ask` (the normal card, "Always approve" honoured) or `ask_always` (a card every time).

    main:     allowed -> allow, else ask; a write while a draft send is pending in this session -> ask.
    breakout: allowed and read-only -> allow; a write -> ask_always; else ask.
    Anything unknown -> ask (fail closed to a card, never to silence)."""
    from fused_render.specialists import store

    spec = store.get(spec_id)
    if spec is None or kind not in ("main", "breakout"):
        return "ask"
    e = find(spec, kind, tool)
    if e is None:
        return "ask_always" if kind == "breakout" else "ask"
    allowed = e["key"] in allowed_set(spec)
    if kind == "breakout":
        if e["write"]:
            return "ask_always"
        return "allow" if allowed else "ask"
    if e["write"] and _send_pending(spec_id, session_id):
        return "ask"
    return "allow" if allowed else "ask"


def set_allowed(spec_id: str, add=(), remove=()) -> list:
    """Edit the allowed list against the live one (a card's "don't ask again" may land while Settings is open)."""
    from fused_render.specialists import store

    spec = store.get(spec_id)
    if spec is None:
        raise KeyError(spec_id)
    cur = [norm(k) for k in spec.connections if norm(k)]
    drop = {norm(k) for k in remove or ()}
    cur = [k for k in dict.fromkeys(cur) if k not in drop]
    for k in add or ():
        k = norm(k)
        if k and k not in cur:
            cur.append(k)
    return store.update(spec_id, connections=cur).connections


def connections(spec) -> dict:
    """`{"services":[{"key","title","kind":"connect|app","connected","allowed"}]}` for Settings › Connections:
    every connect provider (signed in or not) and every local app with tools."""
    at = _apptools()
    allowed = allowed_set(spec)
    out, seen = [], set()
    try:
        from fused_render.connect import manager
        from fused_render.connect import registry as creg

        provs = list(creg.providers())
    except Exception:  # noqa: BLE001
        provs, manager = [], None
    for p in provs:
        key = norm(getattr(p, "key", "") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            connected = bool(manager.is_connected(p.key)) if manager else False
        except Exception:  # noqa: BLE001
            connected = False
        out.append({"key": key, "title": str(getattr(p, "name", "") or key), "kind": "connect",
                    "connected": connected, "allowed": key in allowed})
    for rec in _all_records():
        if at.is_connect(rec):
            continue
        key = key_of(rec)
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "title": str(rec.app), "kind": "app", "connected": True, "allowed": key in allowed})
    for key in sorted(allowed - seen):  # allowed but gone: still shown so it can be removed
        out.append({"key": key, "title": key, "kind": "app" if key.startswith(APP_PREFIX) else "connect",
                    "connected": False, "allowed": True})
    return {"services": out}
