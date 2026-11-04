"""What the Google packs' native tools share: one way to call a Google REST API
with the connect token, one way to turn its answers into tool errors, and the
small argument plumbing every `tools.py` repeats (SPEC-connect-oauth §4, §11).

The Gmail, Google Calendar and Google Docs packs are three cards on purpose:
each asks for its own scopes at its own consent screen, so connecting the
calendar never hands a bot the inbox. They talk to the same vendor the same
way, which is what lives here rather than three times over.

Google's error envelope is `{error: {code, status, message}}`: a 401 (or
`UNAUTHENTICATED`) is `ToolAuthError`, which the core refreshes once and
retries; a 403 names the permission or the scope (`insufficient
authentication scopes`); a 404 is the id the model got wrong; a 429 and
`RESOURCE_EXHAUSTED` are the quota. Everything else is the vendor's own
`message`, which is what a model needs to read to try something else.
"""
from __future__ import annotations

from fused_render.connect.tools import ToolAuthError, ToolError


def obj(props: dict, required: list[str] | None = None) -> dict:
    """A tool's `input_schema`."""
    return {"type": "object", "properties": props, "required": list(required or [])}


def clamp(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def api(ctx, method: str, url: str, *, params: dict | None = None, json=None):
    """One call with the connect token as Bearer; the decoded JSON body (or {}).

    `params` with a None or "" value are dropped, so a tool can pass every
    optional argument straight through.
    """
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    kwargs = {"params": params, "headers": {"Authorization": f"Bearer {ctx.token}",
                                            "Accept": "application/json"}}
    if json is not None:
        kwargs["json"] = json
    r = ctx.http.request(method, url, **kwargs)
    if r.status_code == 204 or not r.content:
        body = {}
    else:
        try:
            body = r.json()
        except ValueError:
            if r.status_code < 400:
                raise ToolError("invalid response") from None
            body = {}
    if r.status_code < 400:
        return body if isinstance(body, dict) else {"value": body}
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    status = str(err.get("status") or "")
    message = str(err.get("message") or f"HTTP {r.status_code}")
    if r.status_code == 401 or status == "UNAUTHENTICATED":
        raise ToolAuthError(message)
    if r.status_code == 429 or status == "RESOURCE_EXHAUSTED":
        raise ToolError(f"ratelimited ({message})")
    if r.status_code == 403:
        raise ToolError(f"permission denied ({message})")
    if r.status_code == 404:
        raise ToolError(f"not found ({message})")
    raise ToolError(message)


def dispatch(tools: list[dict], handlers: dict, ctx, name: str, args: dict):
    """`call(ctx, name, args)` for a pack: unknown names, stray arguments and
    missing required ones handled one way for every Google pack."""
    fn = handlers.get(name)
    spec = next((t for t in tools if t["name"] == name), None)
    if fn is None or spec is None:
        raise ToolError(f"unknown tool {name}")
    allowed = set(spec["input_schema"]["properties"])
    kwargs = {k: v for k, v in (args or {}).items() if k in allowed}
    for k in spec["input_schema"].get("required") or []:
        if kwargs.get(k) in (None, "", [], {}):
            raise ToolError(f"missing argument: {k}")
    return fn(ctx, **kwargs)
