"""Slack's tools, native: the Slack Web API called from this process (SPEC-connect-oauth §4, §11).

Slack's hosted MCP server takes only a Slack app of one's own, so the Slack
pack ships its tools here instead: six Web API methods over `ctx.http` with the
user token from the PKCE sign-in as Bearer. `tools.py` (the core one) loads
this file by path and calls `call(ctx, name, args)`; the token never reaches
the caller, only the compact result.

Results are trimmed on purpose: Slack's messages carry `blocks`, `attachments`
and full user profiles that cost a model thousands of tokens and say nothing
`text` does not. What is kept: ts, user, text, channel, name, counts, the
thread fields and a permalink where Slack gives one.

Slack answers HTTP 200 with `{ok: false, error}` for every failure; that
becomes `ToolError(error)` (`{ok: False, text: "slack: <error>"}`), and the
token errors (`invalid_auth`, `token_expired`, ...) become `ToolAuthError`,
which the core refreshes once and retries.
"""
from __future__ import annotations

import re

from fused_render.connect.tools import ToolAuthError, ToolError

API = "https://slack.com/api/"

_AUTH_ERRORS = {"invalid_auth", "not_authed", "token_expired", "token_revoked",
                "account_inactive", "no_permission_for_token"}
_CHANNEL_ID = re.compile(r"^[CGD][A-Z0-9]{6,}$")


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


TOOLS = [
    {"name": "slack_search_messages",
     "description": "Search messages across the workspace with Slack's search syntax "
                    "(e.g. 'from:@ana in:#design budget after:2026-01-01').",
     "input_schema": _obj({"query": {"type": "string", "description": "Slack search query"},
                           "count": {"type": "integer", "description": "max results (1-100)", "default": 20}},
                          ["query"]),
     "read_only": True, "destructive": False},
    {"name": "slack_list_channels",
     "description": "List the channels the signed-in user can see.",
     "input_schema": _obj({"types": {"type": "string", "default": "public_channel,private_channel",
                                     "description": "comma-separated: public_channel, private_channel, mpim, im"},
                           "limit": {"type": "integer", "default": 200}}, []),
     "read_only": True, "destructive": False},
    {"name": "slack_channel_history",
     "description": "Recent messages in a channel (by name like 'general' or '#general', or by id).",
     "input_schema": _obj({"channel": {"type": "string"},
                           "limit": {"type": "integer", "default": 50},
                           "oldest": {"type": "string", "description": "only messages after this ts", "default": ""},
                           "latest": {"type": "string", "description": "only messages before this ts", "default": ""}},
                          ["channel"]),
     "read_only": True, "destructive": False},
    {"name": "slack_list_users",
     "description": "List the workspace's members (id, handle, real name).",
     "input_schema": _obj({"limit": {"type": "integer", "default": 200}}, []),
     "read_only": True, "destructive": False},
    {"name": "slack_send_message",
     "description": "Post a message as the signed-in user to a channel (name or id), "
                    "optionally as a reply in a thread.",
     "input_schema": _obj({"channel": {"type": "string"}, "text": {"type": "string"},
                           "thread_ts": {"type": "string", "default": ""}}, ["channel", "text"]),
     "read_only": False, "destructive": False},
    {"name": "slack_add_reaction",
     "description": "Add an emoji reaction (name without colons, e.g. 'thumbsup') to a message.",
     "input_schema": _obj({"channel": {"type": "string"}, "timestamp": {"type": "string"},
                           "name": {"type": "string"}}, ["channel", "timestamp", "name"]),
     "read_only": False, "destructive": False},
]


def _api(ctx, method: str, params: dict) -> dict:
    data = {k: v for k, v in params.items() if v not in (None, "")}
    r = ctx.http.post(API + method, data=data, headers={"Authorization": f"Bearer {ctx.token}"})
    if r.status_code == 429:
        raise ToolError(f"ratelimited (retry after {r.headers.get('retry-after', '?')} s)")
    if r.status_code >= 400:
        raise ToolError(f"HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise ToolError("invalid response") from None
    if not body.get("ok"):
        err = str(body.get("error") or "unknown_error")
        if err in _AUTH_ERRORS:
            raise ToolAuthError(err)
        if err == "missing_scope" and body.get("needed"):
            err = f"missing_scope ({body['needed']})"
        raise ToolError(err)
    return body


def _clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _msg(m: dict, channel=None) -> dict:
    out = {"ts": m.get("ts"), "user": m.get("user") or m.get("username") or m.get("bot_id"),
           "text": m.get("text") or ""}
    if channel is not None:
        out["channel"] = channel
    for k in ("thread_ts", "reply_count", "permalink"):
        if m.get(k):
            out[k] = m[k]
    if m.get("reactions"):
        out["reactions"] = {r.get("name"): r.get("count") for r in m["reactions"]}
    return out


def _channels(ctx, types: str, limit: int) -> list[dict]:
    out, cursor = [], None
    while len(out) < limit:
        body = _api(ctx, "conversations.list", {"types": types, "limit": min(200, limit - len(out)),
                                                "exclude_archived": "true", "cursor": cursor})
        for c in body.get("channels") or []:
            out.append({"id": c.get("id"), "name": c.get("name"),
                        "is_private": bool(c.get("is_private")), "is_member": bool(c.get("is_member")),
                        "num_members": c.get("num_members"),
                        "topic": (c.get("topic") or {}).get("value") or None})
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return out[:limit]


def _resolve_channel(ctx, channel: str) -> str:
    """A channel id from an id, '#name' or 'name' (one conversations.list walk)."""
    channel = (channel or "").strip()
    if not channel:
        raise ToolError("channel is required")
    if _CHANNEL_ID.match(channel):
        return channel
    name = channel.lstrip("#")
    for c in _channels(ctx, "public_channel,private_channel", 2000):
        if c["name"] == name:
            return c["id"]
    raise ToolError(f"channel_not_found ({channel})")


def search_messages(ctx, query: str, count=20) -> dict:
    body = _api(ctx, "search.messages", {"query": query, "count": _clamp(count, 1, 100, 20)})
    m = body.get("messages") or {}
    return {"total": m.get("total", 0),
            "messages": [_msg(x, (x.get("channel") or {}).get("name") or (x.get("channel") or {}).get("id"))
                         for x in m.get("matches") or []]}


def list_channels(ctx, types="public_channel,private_channel", limit=200) -> dict:
    chans = _channels(ctx, types or "public_channel,private_channel", _clamp(limit, 1, 2000, 200))
    return {"count": len(chans), "channels": chans}


def channel_history(ctx, channel: str, limit=50, oldest="", latest="") -> dict:
    cid = _resolve_channel(ctx, channel)
    body = _api(ctx, "conversations.history", {"channel": cid, "limit": _clamp(limit, 1, 1000, 50),
                                               "oldest": oldest, "latest": latest})
    msgs = [_msg(m) for m in body.get("messages") or []]
    return {"channel": cid, "count": len(msgs), "has_more": bool(body.get("has_more")), "messages": msgs}


def list_users(ctx, limit=200) -> dict:
    limit = _clamp(limit, 1, 2000, 200)
    out, cursor = [], None
    while len(out) < limit:
        body = _api(ctx, "users.list", {"limit": min(200, limit - len(out)), "cursor": cursor})
        for u in body.get("members") or []:
            prof = u.get("profile") or {}
            out.append({"id": u.get("id"), "name": u.get("name"),
                        "real_name": u.get("real_name") or prof.get("real_name") or None,
                        "is_bot": bool(u.get("is_bot")), "deleted": bool(u.get("deleted"))})
        cursor = (body.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
    return {"count": len(out[:limit]), "users": out[:limit]}


def send_message(ctx, channel: str, text: str, thread_ts="") -> dict:
    if not text:
        raise ToolError("text is required")
    cid = _resolve_channel(ctx, channel)
    body = _api(ctx, "chat.postMessage", {"channel": cid, "text": text, "thread_ts": thread_ts})
    return {"ok": True, "channel": body.get("channel") or cid, "ts": body.get("ts")}


def add_reaction(ctx, channel: str, timestamp: str, name: str) -> dict:
    cid = _resolve_channel(ctx, channel)
    _api(ctx, "reactions.add", {"channel": cid, "timestamp": timestamp, "name": (name or "").strip(":")})
    return {"ok": True, "channel": cid, "ts": timestamp, "name": (name or "").strip(":")}


_HANDLERS = {
    "slack_search_messages": search_messages,
    "slack_list_channels": list_channels,
    "slack_channel_history": channel_history,
    "slack_list_users": list_users,
    "slack_send_message": send_message,
    "slack_add_reaction": add_reaction,
}
_PARAMS = {t["name"]: set(t["input_schema"]["properties"]) for t in TOOLS}


def call(ctx, name: str, args: dict):
    fn = _HANDLERS.get(name)
    if fn is None:
        raise ToolError(f"unknown tool {name}")
    kwargs = {k: v for k, v in (args or {}).items() if k in _PARAMS[name]}
    missing = [k for k in next(t for t in TOOLS if t["name"] == name)["input_schema"]["required"]
               if not kwargs.get(k)]
    if missing:
        raise ToolError(f"missing argument: {missing[0]}")
    return fn(ctx, **kwargs)
