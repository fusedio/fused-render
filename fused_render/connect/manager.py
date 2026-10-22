"""Who is connected to what, and the sign-in flows that change it (SPEC-connect-oauth §3, §11).

    list()                     every provider with its auth kinds and status
    status(key)                disconnected | pending | connected | reconnect | error
    login(key, kind?, client_id?, value?)   start (or join) a sign-in
    callback(key, url)         the paste fallback: the address the browser ended up on
    cancel(key) / logout(key)
    token(key, force_refresh=False)         a valid access token, refreshed when due

**The store is the source of truth for "connected".** `store.get(key)` is read
on every status; nothing here caches it. What lives in memory is only what has
no business on disk: a flow in flight (`_flows[key]`, its URL and user code),
and the `reconnect` mark a refused refresh leaves until the next login or
logout.

**Flows run on daemon threads**, the `hf_auth.py` shape: one flow per key; a
second login joins the live one rather than starting another (two authorize
URLs is two consents, only one of which is being listened for); `_start_lock`
makes check-and-create atomic while `_lock` guards only the pointer, so a
status poll never waits behind a vendor round-trip. Cancel sets a flag and
cancels the driver, which unblocks `result()`; a token that arrives after a
cancel is dropped, never stored.

The five kinds:

- `mcp-oauth`: `discovery.discover(resource)` -> `choose_client` (pre-registered,
  CIMD, DCR) -> `oauth.LoopbackFlow` with `resource` on both legs -> account =
  the server's `serverInfo.name` (best effort). The record keeps `resource`,
  `client_id`, `client_registration`, `token_url` and `revoke_url` so refresh
  and revoke need no rediscovery.
- `oauth-pkce`: `LoopbackFlow` from the auth entry; account from `identity_url`.
- `oauth-device`: `DeviceFlow`; status carries `verification_uri` and `user_code`.
- `cli`: run the vendor CLI's `command`; only the kind and the account are
  stored, and `token()` asks the CLI again every time (the CLI owns the token, D402).
- `api-key`: synchronous; the pasted value is probed against `identity_url`
  and stored only if the vendor accepts it.

**Refresh is lazy and serialized per key**: a token within 60 s of expiry is
refreshed by the call that finds it so, under a per-key lock, because refresh
tokens rotate and two refreshes racing with the same one would burn it. A
refresh that fails marks the provider `reconnect` and raises `NotConnected`.

Nothing here logs a token, a record or a vendor response body; debug lines
name the key and the state.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from fused_render.connect import oauth, registry, store

logger = logging.getLogger(__name__)

#: Refresh a token this close to its expiry rather than send it and get a 401.
REFRESH_MARGIN = 60.0
CLI_TIMEOUT = 10.0
HTTP_TIMEOUT = 30.0
#: How long `login` waits for the authorize URL so its reply can carry it.
URL_WAIT = 20.0
#: How long `callback` waits for the token exchange after a pasted URL.
CALLBACK_WAIT = 20.0
FLOW_TIMEOUT = 600.0

KINDS = ("mcp-oauth", "oauth-pkce", "oauth-device", "cli", "api-key")
_TOKEN_KINDS = ("mcp-oauth", "oauth-pkce", "oauth-device")
_LABELS = {
    "mcp-oauth": "Sign in with {name}",
    "oauth-pkce": "Sign in with {name}",
    "oauth-device": "Sign in with a code",
    "cli": "Use the {name} command-line sign-in",
    "api-key": "Paste a token",
}


class NotConnected(Exception):
    """No usable credential for this provider (never connected, or needs reconnecting)."""


class UnknownProvider(KeyError):
    pass


class NoFlow(ValueError):
    """`callback` with no browser sign-in waiting (routes answer 409)."""


@dataclass
class _Flow:
    kind: str
    started_at: float = field(default_factory=time.time)
    url: str | None = None
    user_code: str | None = None
    verification_uri: str | None = None
    opened: bool = False                  # the server opened the browser itself; the page must not open a second tab
    account: str | None = None
    error: str | None = None
    done: bool = False
    cancelled: bool = False
    handle: object = None                 # the LoopbackFlow / DeviceFlow
    ready: threading.Event = field(default_factory=threading.Event)     # url known, or done
    finished: threading.Event = field(default_factory=threading.Event)


_lock = threading.Lock()
_start_lock = threading.Lock()
_flows: dict[str, _Flow] = {}
_reconnect: set[str] = set()
_refresh_locks: dict[str, threading.Lock] = {}


# -- helpers --------------------------------------------------------------------

def _provider(key: str):
    p = registry.get(key)
    if p is None:
        raise UnknownProvider(key)
    return p


def _env_client_id(key: str) -> str:
    return (os.environ.get(f"FUSED_RENDER_CONNECT_{key.upper().replace('-', '_')}_CLIENT_ID") or "").strip()


def _resolved(p, entry: dict) -> dict:
    """The auth entry with `client_id` resolved and its availability decided."""
    e = dict(entry)
    kind = e.get("kind")
    out = {"kind": kind, "label": e.get("label") or _LABELS.get(kind, kind).format(name=p.name),
           "available": True}
    if e.get("byo"):
        out["byo"] = e["byo"]
    if e.get("docs"):
        out["docs"] = e["docs"]
    if kind in ("oauth-pkce", "oauth-device", "mcp-oauth"):
        # registry merges the environment's client id at load; repeated here
        # so a variable set after the pack was read still counts.
        e["client_id"] = (e.get("client_id") or "").strip() or _env_client_id(p.key)
    if kind not in KINDS:
        ok, reason = False, f"unknown sign-in kind {kind!r}"
    else:
        ok, reason = registry.availability(p, e)
    out["available"] = bool(ok)
    if not ok:
        out["reason"] = reason
    return {"entry": e, "public": out}


def _entries(p) -> list[dict]:
    return [_resolved(p, a) for a in (p.auth or []) if isinstance(a, dict)]


def _entry_for(p, kind: str | None) -> dict:
    """The resolved auth entry for `kind`, or the first available one."""
    entries = _entries(p)
    if kind:
        for r in entries:
            if r["entry"].get("kind") == kind:
                return r
        raise ValueError(f"{p.name} has no {kind!r} sign-in")
    for r in entries:
        if r["public"]["available"]:
            return r
    if entries:
        return entries[0]
    raise ValueError(f"{p.name} declares no sign-in")


def _open_browser(url: str) -> bool:
    if os.environ.get("FUSED_RENDER_CONNECT_OPEN_BROWSER") == "0":
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - no browser is the paste fallback's case
        return False


def _http() -> httpx.Client:
    """One short-lived client for identity / revoke / CLI-probe calls (patched by tests)."""
    return httpx.Client(timeout=HTTP_TIMEOUT)


class _Refused(Exception):
    """The vendor said this credential is not valid (identity probe)."""


def _account_from(data) -> str | None:
    if not isinstance(data, dict):
        return None
    if "ok" in data and ("team" in data or "user" in data):   # Slack auth.test
        user, team = data.get("user"), data.get("team")
        if user and team:
            return f"{user}@{team}"
        return user or team or None
    for k in ("login", "email", "name", "username", "user"):
        v = data.get(k)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            inner = _account_from(v)
            if inner:
                return inner
    return None


def _identity(url: str | None, token: str, entry: dict | None = None) -> str | None:
    """Ask the vendor who this token belongs to. Raises `_Refused` on 401/403/`ok: false`.

    Slack's `auth.test` is a POST answering `{ok, user, team}` (account
    `user@team`); everything else is a GET whose JSON has a login, email or name.
    """
    if not url:
        return None
    method = (entry or {}).get("identity_method") or \
        ("POST" if urlparse(url).path.rstrip("/").endswith("auth.test") else "GET")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    tmpl = (entry or {}).get("header")
    if isinstance(tmpl, str) and ":" in tmpl:   # api-key: "Authorization: Bearer {value}"
        name, _, v = tmpl.partition(":")
        headers.pop("Authorization", None)
        headers[name.strip()] = v.strip().replace("{value}", token)
    with _http() as http:
        r = http.request(method, url, headers=headers)
    if r.status_code in (401, 403):
        raise _Refused(f"the token was refused (HTTP {r.status_code})")
    if r.status_code >= 400:
        logger.debug("identity probe answered %s", r.status_code)
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("ok") is False:
        raise _Refused(str(data.get("error") or "refused"))
    return _account_from(data)


CLI_CACHE_S = 30.0
_cli_cache: dict[tuple, tuple[float, str]] = {}


def _run_cli(cmd: list[str], fresh: bool = False) -> str:
    """The token a vendor CLI prints (e.g. `gh auth token`). Raises NotConnected.
    Cached for CLI_CACHE_S: one MCP call is two or three requests, and a
    subprocess per request is what made the first GitHub call slow. `fresh`
    (a 401's retry, `token(force_refresh=True)`) re-runs the command, so a
    token the CLI rotated is picked up on the very next call."""
    k = tuple(cmd)
    hit = None if fresh else _cli_cache.get(k)
    if hit and time.monotonic() - hit[0] < CLI_CACHE_S:
        return hit[1]
    try:
        tok = _run_cli_uncached(cmd)
    except NotConnected:
        _cli_cache.pop(k, None)
        raise
    _cli_cache[k] = (time.monotonic(), tok)
    return tok


def _run_cli_uncached(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT, check=False,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        raise NotConnected(f"`{cmd[0]}` could not run: {e.__class__.__name__}") from None
    tok = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not tok or not tok[0].strip():
        raise NotConnected(f"`{' '.join(cmd)}` printed no token; sign in with `{cmd[0]}` first")
    return tok[0].strip()


def _token_record(kind: str, ts, **more) -> dict:
    rec = {"kind": kind, "access": ts.access}
    if ts.refresh:
        rec["refresh"] = ts.refresh
    if ts.expires_at:
        rec["expires_at"] = float(ts.expires_at)
    if ts.token_type:
        rec["token_type"] = ts.token_type
    if ts.scope:
        rec["scope"] = ts.scope
    rec.update({k: v for k, v in more.items() if v is not None})
    return rec


def _invalidate_tools(key: str) -> None:
    from fused_render.connect import tools  # tools imports this module
    tools.invalidate(key)


def _connected_now(key: str) -> None:
    with _lock:
        _reconnect.discard(key)
    _invalidate_tools(key)


# -- reads --------------------------------------------------------------------

def record(key: str) -> dict | None:
    """The stored record (with the token in it): for tools.py's native ctx only."""
    return store.get(key)


def is_connected(key: str) -> bool:
    return store.get(key) is not None


def mark_reconnect(key: str) -> None:
    logger.info("connect %s: needs reconnecting", key)
    with _lock:
        _reconnect.add(key)


def status(key: str) -> dict:
    _provider(key)
    with _lock:
        flow = _flows.get(key)
        needs = key in _reconnect
    rec = store.get(key)
    out: dict = {"state": "disconnected", "store": store.backend(), "tools": 0}
    if rec is not None:
        out.update(state="reconnect" if needs else "connected", kind=rec.get("kind"),
                   account=rec.get("account"))
        if not needs:
            from fused_render.connect import tools
            n = tools.cached_count(key)
            if n is None:
                tools.warm(key)
            out["tools"] = n or 0
    if flow is not None and not flow.done and not flow.cancelled:
        out.update(state="pending", kind=flow.kind)
        for k in ("url", "user_code", "verification_uri"):
            if getattr(flow, k):
                out[k] = getattr(flow, k)
        out["opened"] = flow.opened
    elif flow is not None and flow.done and flow.error:
        if rec is None:
            out["state"] = "error"
        out["error"] = flow.error
        out.setdefault("kind", flow.kind)
    return out


def list() -> list[dict]:
    out = []
    for p in registry.providers():
        st = status(p.key)
        count = st.pop("tools", 0)
        out.append({
            "key": p.key, "name": p.name,
            "icon": f"/api/connect/{p.key}/icon" if p.icon else None,
            "privacy": p.privacy,
            "auth": [r["public"] for r in _entries(p)],
            **st,
            # `tools` is the source dict here, with the status's count folded in.
            "tools": {**(p.tools or {}), "count": count},
        })
    return out


# -- flows --------------------------------------------------------------------

def _finish(key: str, flow: _Flow, rec_fn) -> None:
    """Run a flow body on its thread; store what it returns unless cancelled."""
    try:
        rec = rec_fn()
        if flow.cancelled:
            return
        store.put(key, rec)
        flow.account = rec.get("account")
        _connected_now(key)
        logger.info("connect %s: connected (%s)", key, flow.kind)
    except Exception as e:  # noqa: BLE001 - every failure is a sentence on the card
        if not flow.cancelled and not isinstance(e, oauth.FlowCancelled):
            flow.error = _sentence(e)
            logger.info("connect %s: sign-in failed: %s", key, e.__class__.__name__)
    finally:
        flow.done = True
        flow.ready.set()
        flow.finished.set()


def _sentence(e: Exception) -> str:
    if e.__class__.__name__ == "NeedsClientId":   # discovery's; imported lazily
        return ("This server does not let apps register themselves. Create an app at the "
                "vendor and paste its client id under Advanced.")
    if isinstance(e, oauth.FlowTimeout):
        return "The sign-in timed out. Try again."
    return str(e) or e.__class__.__name__


def _check_cancel(flow: _Flow) -> None:
    if flow.cancelled:
        raise oauth.FlowCancelled()


def _mcp_oauth(key: str, p, entry: dict, flow: _Flow) -> dict:
    from fused_render.connect import discovery  # only the mcp-oauth path needs it
    resource = entry["resource"]
    d = discovery.discover(resource)
    _check_cancel(flow)
    redirect_uris = [f"http://localhost:{port}{oauth.REDIRECT_PATH}" for port in oauth.REDIRECT_PORTS]
    cid, registration = discovery.choose_client(d, preregistered=entry.get("client_id") or None,
                                                redirect_uris=redirect_uris)
    _check_cancel(flow)
    lf = oauth.LoopbackFlow(
        authorize_url=d.authorization_endpoint, token_url=d.token_endpoint, client_id=cid,
        redirect={"host": oauth.REDIRECT_HOST, "ports": oauth.REDIRECT_PORTS, "path": oauth.REDIRECT_PATH},
        scopes={"scope": " ".join(d.scopes)} if d.scopes else None,
        extra_authorize={"resource": resource}, extra_token={"resource": resource})
    ts = _run_loopback(flow, lf)
    account = None
    try:
        from fused_render.connect.mcp_client import McpClient
        c = McpClient(resource, ts.access)
        try:
            info = c.initialize().get("serverInfo") or {}
            account = info.get("name") or None
        finally:
            c.close()
    except Exception as e:  # noqa: BLE001 - best effort: a label, not a gate
        logger.debug("connect %s: initialize for identity failed: %s", key, e.__class__.__name__)
    return _token_record("mcp-oauth", ts, account=account, resource=resource, client_id=cid,
                         client_registration=registration, token_url=d.token_endpoint,
                         revoke_url=d.revocation_endpoint)


def _run_loopback(flow: _Flow, lf) -> object:
    flow.handle = lf
    _check_cancel(flow)
    url = lf.start()
    flow.url = url
    flow.opened = _open_browser(url)
    flow.ready.set()
    return lf.result(timeout=FLOW_TIMEOUT)


def _oauth_pkce(key: str, p, entry: dict, flow: _Flow) -> dict:
    lf = oauth.LoopbackFlow(
        authorize_url=entry["authorize_url"], token_url=entry["token_url"],
        client_id=entry["client_id"], redirect=entry.get("redirect"),
        scopes=entry.get("scopes"), extra_authorize=entry.get("extra_authorize"),
        client_secret=entry.get("client_secret") or None)
    ts = _run_loopback(flow, lf)
    _check_cancel(flow)
    account = _identity(entry.get("identity_url"), ts.access, entry)
    return _token_record("oauth-pkce", ts, account=account, client_id=entry["client_id"],
                         token_url=entry["token_url"], revoke_url=entry.get("revoke_url"))


def _oauth_device(key: str, p, entry: dict, flow: _Flow) -> dict:
    df = oauth.DeviceFlow(device_url=entry["device_url"], token_url=entry["token_url"],
                          client_id=entry["client_id"], scope=entry.get("scope"))
    flow.handle = df
    _check_cancel(flow)
    info = df.start()
    flow.user_code = info.get("user_code")
    flow.verification_uri = info.get("verification_uri")
    flow.url = info.get("verification_uri_complete") or info.get("verification_uri")
    if flow.url:
        flow.opened = _open_browser(flow.url)
    flow.ready.set()
    ts = df.result(timeout=float(info.get("expires_in") or 900))
    _check_cancel(flow)
    account = _identity(entry.get("identity_url"), ts.access, entry)
    return _token_record("oauth-device", ts, account=account, client_id=entry["client_id"],
                         token_url=entry["token_url"], revoke_url=entry.get("revoke_url"))


_RUNNERS = {"mcp-oauth": _mcp_oauth, "oauth-pkce": _oauth_pkce, "oauth-device": _oauth_device}


def login(key: str, kind: str | None = None, client_id: str | None = None,
          value: str | None = None) -> dict:
    """Start a sign-in (or join the one in flight). Raises ValueError for a bad request."""
    p = _provider(key)
    r = _entry_for(p, kind)
    entry, public = r["entry"], r["public"]
    kind = entry.get("kind")
    if client_id and client_id.strip():
        entry["client_id"] = client_id.strip()   # bring-your-own app
    elif not public["available"]:
        raise ValueError(public.get("reason") or f"{public['label']} is not available")

    if kind in ("api-key", "cli"):
        cancel(key)   # a browser flow still in flight must not land on top of this
        if kind == "api-key":
            return _login_api_key(key, p, entry, value)
        return _login_cli(key, p, entry)
    if kind not in _RUNNERS:
        raise ValueError(f"unknown sign-in kind {kind!r}")

    with _start_lock:
        with _lock:
            live = _flows.get(key)
            live = live if live is not None and not live.done and not live.cancelled else None
        if live is not None:
            flow, joined = live, True
        else:
            flow, joined = _Flow(kind=kind), False
            with _lock:
                _flows[key] = flow
            runner = _RUNNERS[kind]
            threading.Thread(target=_finish, args=(key, flow, lambda: runner(key, p, entry, flow)),
                             name=f"connect-login-{key}", daemon=True).start()
            logger.debug("connect %s: %s sign-in started", key, kind)
    # Outside both locks: the URL arrives after discovery and a bind, and the
    # reply is more useful carrying it (the page opens nothing it has to poll for).
    flow.ready.wait(URL_WAIT)
    return {"joined": joined, **status(key)}


def _login_api_key(key: str, p, entry: dict, value: str | None) -> dict:
    value = (value or "").strip()
    if not value:
        raise ValueError("paste a token first")
    try:
        account = _identity(entry.get("identity_url"), value, entry)
    except _Refused as e:
        raise ValueError(f"{p.name} did not accept that token: {e}") from None
    except httpx.HTTPError as e:
        raise ValueError(f"{p.name} could not be reached to check the token: "
                         f"{e.__class__.__name__}") from None
    store.put(key, {"kind": "api-key", "access": value, "account": account})
    with _lock:
        _flows.pop(key, None)
    _connected_now(key)
    return {"joined": False, **status(key)}


def _login_cli(key: str, p, entry: dict) -> dict:
    try:
        tok = _run_cli(entry["command"])
        account = _identity(entry.get("identity_url"), tok, entry)
    except NotConnected as e:
        raise ValueError(str(e)) from None
    except _Refused as e:
        raise ValueError(f"{p.name} refused the token `{entry['command'][0]}` holds: {e}") from None
    except httpx.HTTPError as e:
        raise ValueError(f"{p.name} could not be reached: {e.__class__.__name__}") from None
    store.put(key, {"kind": "cli", "account": account})
    with _lock:
        _flows.pop(key, None)
    _connected_now(key)
    return {"joined": False, **status(key)}


def callback(key: str, url: str) -> dict:
    """The paste fallback: finish a loopback flow from the address the browser landed on."""
    _provider(key)
    with _lock:
        flow = _flows.get(key)
    if flow is None or flow.done or flow.cancelled or flow.handle is None \
            or not hasattr(flow.handle, "complete_from_url"):
        raise NoFlow("no browser sign-in is waiting for this provider")
    try:
        flow.handle.complete_from_url(url)
    except oauth.FlowError as e:   # no code, another flow's state, already finished
        raise ValueError(str(e)) from None
    flow.finished.wait(CALLBACK_WAIT)
    return status(key)


def cancel(key: str) -> dict:
    _provider(key)
    with _lock:
        flow = _flows.get(key)
    if flow is not None and not flow.done:
        flow.cancelled = True
        if flow.handle is not None:
            try:
                flow.handle.cancel()
            except Exception:  # noqa: BLE001
                pass
    return status(key)


def _revoke(rec: dict) -> bool | None:
    """POST the token(s) to the revoke endpoint. True / False, or None when there is none."""
    url = rec.get("revoke_url")
    if not url or rec.get("kind") not in _TOKEN_KINDS or not rec.get("access"):
        return None
    tokens = [rec["access"]]
    if rec.get("kind") == "mcp-oauth" and rec.get("refresh"):
        tokens.append(rec["refresh"])   # RFC 7009: the refresh token outlives the access token
    ok = True
    try:
        with _http() as http:
            for t in tokens:
                data = {"token": t}
                if rec.get("client_id"):
                    data["client_id"] = rec["client_id"]
                r = http.post(url, data=data, headers={"Authorization": f"Bearer {rec['access']}"})
                body = None
                try:
                    body = r.json()
                except ValueError:
                    pass
                if r.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
                    ok = False
    except Exception as e:  # noqa: BLE001 - a vendor that is down cannot block a disconnect
        logger.info("connect: revoke failed: %s", e.__class__.__name__)
        ok = False
    return ok


def logout(key: str) -> dict:
    """Revoke at the vendor when possible, then forget locally, whatever the vendor said."""
    _provider(key)
    cancel(key)
    # Close the MCP session first, while its token is still good to say goodbye with.
    _invalidate_tools(key)
    rec = store.get(key)
    revoked = _revoke(rec) if rec else None
    store.delete(key)
    with _lock:
        _flows.pop(key, None)
        _reconnect.discard(key)
    _invalidate_tools(key)
    out = {**status(key), "revoked": revoked}
    if revoked is False:
        out["warning"] = "Disconnected here, but the vendor did not confirm revoking the token."
    return out


# -- tokens -------------------------------------------------------------------

def token(key: str, force_refresh: bool = False) -> str:
    """A valid access token for `key`. Raises NotConnected.

    `force_refresh` is for a token the vendor just refused (tools.py's
    one-retry rule): refresh even though the clock says it is still good.
    """
    rec = store.get(key)
    if rec is None:
        raise NotConnected(f"{key} is not connected")
    kind = rec.get("kind")
    if kind == "cli":
        p = _provider(key)
        entry = next((a for a in p.auth if isinstance(a, dict) and a.get("kind") == "cli"), None)
        if entry is None:
            raise NotConnected(f"{key} no longer offers a command-line sign-in")
        try:
            return _run_cli(entry["command"], fresh=force_refresh)
        except NotConnected:
            mark_reconnect(key)
            raise
    if kind == "api-key":
        if force_refresh:
            mark_reconnect(key)
            raise NotConnected(f"{key}'s token was refused")
        return rec["access"]
    exp = rec.get("expires_at")
    due = force_refresh or (exp is not None and float(exp) - time.time() < REFRESH_MARGIN)
    if not due:
        return rec["access"]
    with _lock:
        rlock = _refresh_locks.setdefault(key, threading.Lock())
    seen = rec.get("access")
    with rlock:
        rec = store.get(key)
        if rec is None:
            raise NotConnected(f"{key} is not connected")
        if rec.get("access") != seen:   # another call refreshed while this one waited
            return rec["access"]
        if not rec.get("refresh") or not rec.get("token_url") or not rec.get("client_id"):
            mark_reconnect(key)
            raise NotConnected(f"{key}'s sign-in expired and cannot be refreshed")
        extra = {"resource": rec["resource"]} if rec.get("resource") else None
        secret = None
        p = registry.get(key)
        if p is not None:
            secret = next((a.get("client_secret") for a in p.auth
                           if isinstance(a, dict) and a.get("kind") == kind and a.get("client_secret")), None)
        try:
            ts = oauth.refresh(token_url=rec["token_url"], client_id=rec["client_id"],
                               refresh_token=rec["refresh"], client_secret=secret, extra=extra)
        except Exception as e:  # noqa: BLE001 - expired, revoked, offline: all "reconnect"
            logger.info("connect %s: refresh failed: %s", key, e.__class__.__name__)
            mark_reconnect(key)
            raise NotConnected(f"{key}'s sign-in could not be refreshed") from None
        new = dict(rec)
        new["access"] = ts.access
        if ts.refresh:
            new["refresh"] = ts.refresh       # rotated
        if ts.expires_at:
            new["expires_at"] = float(ts.expires_at)
        else:
            new.pop("expires_at", None)
        if ts.scope:
            new["scope"] = ts.scope
        store.put(key, new)
        with _lock:
            _reconnect.discard(key)
        logger.debug("connect %s: refreshed", key)
        return ts.access
