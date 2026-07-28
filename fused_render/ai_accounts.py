"""AI account management: /api/ai/accounts — list/connect/disconnect Claude
and ChatGPT (Codex) logins against the bundled AI proxy (shell/ai_proxy.py).

Follow-on to fused.ai() (SPEC RH-11) and its bundled-proxy design
(docs/AI_PROXY_BUNDLING.md) — that design turns "connect an account" into a
button in Preferences instead of a terminal `cli-proxy-api` invocation the
user has to run themselves. The wire contract this drives is
docs/AI_PROXY_MANAGEMENT_API.md, verified against a real CLIProxyAPI
v7.2.90 binary; every design choice below cites the section of that doc it
answers.

Shaped after account.py (the existing "external OAuth thing" router): a
single in-flight operation guarded by one module lock, a background thread
doing the actual waiting, and a poll-only status endpoint — because, like
that CLI login, this OAuth exchange has no push channel either. No import of
server.py (server includes this router — keep it acyclic); the X-Fused guard
is duplicated locally like account.py's is.

The one piece with no account.py precedent is the callback listener: the
provider's redirect_uri is a FIXED localhost port the proxy itself never
listens on (54545 for Claude, 1455 for Codex — see the doc's "Callback ports
are fixed" section), so grabbing the `code` out of the browser's redirect
requires binding that port ourselves for the duration of one login. That is
what makes this a one-click flow instead of a copy-the-code-out-of-a-dead-
page-URL flow.
"""
from __future__ import annotations

import dataclasses
import html
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import ai_proxy

router = APIRouter()

# UI-facing provider id -> the proxy's own naming + the fixed OAuth redirect
# it uses. "auth_provider" is what goes into "/{auth_provider}-auth-url" and
# into oauth-callback's "provider" field (ai_proxy.start_login/
# submit_login_code) — it does NOT match the credential listing's "provider"
# field (which says "claude", never "anthropic"); the doc's route table and
# credential-listing shape use the two different names for the same product,
# so the mapping below is the one place that seam is bridged.
_CALLBACK = {
    "claude": {"port": 54545, "path": "/callback", "auth_provider": "anthropic"},
    "codex": {"port": 1455, "path": "/auth/callback", "auth_provider": "codex"},
}

# How long a bound callback port waits for the browser before the login is
# reported failed and the port released. account.py's URL_CAPTURE_TIMEOUT is
# the same idea (bound the pathological hang) but sized for a subprocess
# printing a line in well under a second; this waits on an actual human
# reading a consent screen in a browser, hence minutes not seconds.
_CALLBACK_TIMEOUT_S = 300.0


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused / account._require_fused.
    # Duplicated deliberately: this module must not import server (no cycle
    # — server includes this router).
    if x_fused != "1":
        return JSONResponse({"error": "missing or invalid X-Fused header"}, status_code=403)
    return None


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _valid_credential_name(name: str) -> bool:
    """`name` is handed straight to ai_proxy.delete_credential(), which puts
    it in a `?name=` query param that names a file on disk server-side
    (auth-dir/<name>) — reject anything that could walk out of that
    directory. Legitimate names come from our own GET /api/ai/accounts
    listing and never contain a separator, so this is purely a defense-in-
    depth check against a hand-crafted request, not a normal-path concern."""
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


# -- the callback listener -----------------------------------------------------
#
# One HTTPServer per login attempt, bound to 127.0.0.1 only (never 0.0.0.0 —
# the doc's ?is_webui=1 section is explicit that the proxy's own equivalent
# shortcut was rejected for exactly this reason). It answers exactly one
# request (the provider's redirect carrying ?code=&state=), then closes.


_CALLBACK_OK_HTML = """<!doctype html>
<html><head><title>FusedRender</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif;
             text-align: center; margin-top: 4rem; color: #222;">
<h2>You're all set</h2>
<p>Return to FusedRender to finish connecting your account.</p>
<p style="color: #888;">You can close this tab.</p>
</body></html>
"""

_CALLBACK_ERROR_HTML = """<!doctype html>
<html><head><title>FusedRender</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, sans-serif;
             text-align: center; margin-top: 4rem; color: #222;">
<h2>Sign-in was not completed</h2>
<p>{error}</p>
<p>Return to FusedRender and try again.</p>
</body></html>
"""


def _make_handler(expected_path: str) -> type:
    """A BaseHTTPRequestHandler subclass bound to one expected callback path.

    Stashes the captured code/state/error on `self.server` (an attribute of
    the one HTTPServer instance this login attempt owns), not a module
    global — so there is no cross-attempt leakage even in the pathological
    case of two listener threads somehow existing at once."""

    class _CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib-mandated name
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != expected_path:
                # Something other than the OAuth redirect hit this port
                # (browser prefetch, favicon, a stray manual request) —
                # a 404 both is honest and, unlike capturing garbage,
                # doesn't fail the login over noise.
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            error = (qs.get("error") or [None])[0]
            self.server.captured = {
                "code": (qs.get("code") or [None])[0],
                "state": (qs.get("state") or [None])[0],
                "error": error,
            }
            page = _CALLBACK_OK_HTML if error is None else _CALLBACK_ERROR_HTML.format(
                error=html.escape(error))
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            # BaseHTTPRequestHandler logs every request to stderr by default
            # — silence it; a one-shot localhost OAuth callback isn't
            # something the app's console needs to see.
            pass

    return _CallbackHandler


def _bind_callback_server(port: int, path: str) -> HTTPServer:
    """Bind the provider's fixed callback port, loopback-only.

    An OSError here almost always means the port is already held — by a
    concurrent login (shouldn't happen past the single-flight check below,
    but TOCTOU is possible), or by the user's own separately-installed
    CLIProxyAPI mid-login (docs/AI_PROXY_MANAGEMENT_API.md's "Callback ports
    are fixed" section calls this out by name). Either way we fail fast with
    a message naming the likely cause rather than hanging."""
    try:
        return HTTPServer(("127.0.0.1", port), _make_handler(path))
    except OSError as e:
        raise RuntimeError(
            f"could not bind 127.0.0.1:{port} for the OAuth callback ({e}); this "
            "usually means a login is already in progress, or you have your own "
            "CLIProxyAPI mid-login on this machine"
        ) from e


# The phases in which an attempt still owns its callback port and so blocks a
# new login. Anything else (done/failed) is a finished attempt kept only so
# /connect/status can report its outcome — it must not block the next login,
# nor be reported as in-flight by the accounts listing.
_IN_FLIGHT_PHASES = ("waiting_browser", "exchanging")


@dataclasses.dataclass
class _ActiveConnect:
    """The one in-flight (or just-finished) account-connect attempt.

    `phase` moves waiting_browser -> exchanging -> done|failed, written by
    the listener thread (_listen) and by the status route's live poll; plain
    attribute writes are fine under the GIL, same discipline as account.py's
    _ActiveLogin/_SetupJob. `http_server` and `cancel_event` exist only to
    let /connect/cancel unblock the listener thread and free the port —
    once phase reaches "exchanging" the server is already closed and both
    become inert.
    """

    provider: str
    auth_provider: str
    state: str
    http_server: HTTPServer
    cancel_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    phase: str = "waiting_browser"
    detail: str | None = None


_LOCK = threading.Lock()
_active: _ActiveConnect | None = None


def _listen(entry: _ActiveConnect) -> None:
    """Serve exactly one request on entry's callback port, then hand any
    captured code to the proxy. Runs on a daemon thread for the life of one
    login attempt.

    Uses handle_request() in a loop rather than serve_forever(): the stdlib
    only allows stopping serve_forever() via shutdown() called from ANOTHER
    thread (calling it from the same thread deadlocks, per the socketserver
    docs) — handle_request() with server.timeout set just returns on a
    socket timeout, so this loop can poll the cancel event and the overall
    deadline with no cross-thread signaling at all.
    """
    server = entry.http_server
    server.timeout = 1.0
    deadline = time.monotonic() + _CALLBACK_TIMEOUT_S
    captured = None
    while time.monotonic() < deadline:
        if entry.cancel_event.is_set():
            break
        server.handle_request()
        captured = getattr(server, "captured", None)
        if captured is not None:
            break
    # Release the port the instant we're done needing it: everything past
    # this point is talking to the proxy's management API, not the browser.
    server.server_close()

    if entry.cancel_event.is_set():
        return  # /connect/cancel already decided the outcome; nothing to report
    if captured is None:
        entry.phase = "failed"
        entry.detail = f"timed out waiting for the browser after {int(_CALLBACK_TIMEOUT_S)}s"
        return
    if captured["error"]:
        entry.phase = "failed"
        entry.detail = f"authorization was not granted: {captured['error']}"
        return
    if captured["state"] != entry.state:
        # The doc calls this the CSRF token: a callback carrying a state we
        # didn't hand out for THIS login must never be submitted.
        entry.phase = "failed"
        entry.detail = "callback state did not match the login we started"
        return
    if not captured["code"]:
        entry.phase = "failed"
        entry.detail = "callback did not include an authorization code"
        return

    entry.phase = "exchanging"
    try:
        ai_proxy.submit_login_code(entry.auth_provider, entry.state, captured["code"])
    except RuntimeError as e:
        entry.phase = "failed"
        entry.detail = str(e)
    # On success entry.phase stays "exchanging" — oauth-callback's 200 proves
    # nothing (see ai_proxy.submit_login_code's docstring); the status route
    # below polls get-auth-status for the real outcome.


def _poll_status(entry: _ActiveConnect | None) -> dict:
    if entry is None:
        return {"state": "idle", "detail": None}
    if entry.phase != "exchanging":
        # waiting_browser/done/failed are all settled without touching the
        # network — only "exchanging" needs a live call.
        return {"state": entry.phase, "detail": entry.detail}
    try:
        result = ai_proxy.poll_login_status(entry.state)
    except RuntimeError as e:
        entry.phase, entry.detail = "failed", str(e)
        return {"state": entry.phase, "detail": entry.detail}
    status = result.get("status")
    if status == "ok":
        entry.phase, entry.detail = "done", None
    elif status == "error":
        entry.phase = "failed"
        entry.detail = result.get("error") or "login failed"
    # status == "wait" (or anything unrecognized): stay "exchanging" and let
    # the client poll again.
    return {"state": entry.phase, "detail": entry.detail}


# -- routes --------------------------------------------------------------------


@router.get("/api/ai/accounts")
def api_ai_accounts():
    # Cheap and non-spawning by construction: status() never spawns the
    # proxy, and list_credentials() (which does hit the network) only runs
    # when status() says something is already there to ask.
    st = ai_proxy.status()
    accounts = []
    if st.get("running"):
        try:
            files = ai_proxy.list_credentials()
        except RuntimeError:
            # Best-effort: the proxy could have died in the gap between the
            # health probe above and this call, or be a version this app
            # wasn't built against (see the doc's version-skew note). Either
            # way a listing hiccup degrades to "no accounts shown", not a
            # 500 — the tab should never hard-fail over this.
            files = []
        for f in files:
            provider = f.get("provider")
            if provider not in ("claude", "codex"):
                continue
            # Map explicitly, field by field — never pass the raw entry
            # through. It carries an id_token sub-object and an on-disk path
            # (docs/AI_PROXY_MANAGEMENT_API.md's credential listing shape);
            # neither belongs in a response any page on this server can read.
            accounts.append({
                "provider": provider,
                "email": f.get("email"),
                "label": f.get("label"),
                "disabled": bool(f.get("disabled")),
                "name": f.get("name"),
            })
    with _LOCK:
        entry = _active
    # Only a GENUINELY in-flight attempt belongs here. _active is also kept
    # after an attempt settles (so /connect/status can still report the
    # outcome), but reporting a settled attempt as the listing's `login` reads
    # to the page as "a login is in progress" forever — which disabled every
    # Connect button after the FIRST login finished, making it impossible to
    # connect a second provider without restarting. The phases below are the
    # same two api_ai_accounts_connect treats as blocking, deliberately: this
    # field exists to mirror that gate, so it must not be broader than it.
    login = None
    if entry is not None and entry.phase in _IN_FLIGHT_PHASES:
        login = {"provider": entry.provider, "state": entry.phase, "detail": entry.detail}
    return {
        "supervised": bool(st.get("supervised")),
        "running": bool(st.get("running")),
        "accounts": accounts,
        "login": login,
    }


@router.post("/api/ai/accounts/connect")
def api_ai_accounts_connect(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    provider = body.get("provider")
    if provider not in _CALLBACK:
        return _error(f"'provider' must be one of: {', '.join(_CALLBACK)}")

    global _active
    with _LOCK:
        if _active is not None and _active.phase in _IN_FLIGHT_PHASES:
            # Structural, not a race we chose: the callback ports are fixed
            # per provider, and the doc is explicit that two logins can't run
            # concurrently — so this is rejected outright rather than queued.
            return _error(
                f"a login for {_active.provider} is already in progress; cancel it "
                "first or wait for it to finish",
                409,
            )
        spec = _CALLBACK[provider]
        try:
            server_ = _bind_callback_server(spec["port"], spec["path"])
        except RuntimeError as e:
            return _error(str(e), 409)
        try:
            result = ai_proxy.start_login(spec["auth_provider"])
        except RuntimeError as e:
            server_.server_close()
            return _error(str(e), 502)
        state, url = result.get("state"), result.get("url")
        if not isinstance(state, str) or not state or not isinstance(url, str) or not url:
            server_.server_close()
            return _error("the AI proxy did not return a login state/url", 502)
        entry = _ActiveConnect(
            provider=provider, auth_provider=spec["auth_provider"],
            state=state, http_server=server_,
        )
        _active = entry
        threading.Thread(target=_listen, args=(entry,), daemon=True).start()
    # The frontend opens this URL itself (window.open) — this route only ever
    # returns the string, never a browser action taken server-side (the same
    # house rule account.py's login flow follows).
    return {"authorize_url": url, "state": state}


@router.get("/api/ai/accounts/connect/status")
def api_ai_accounts_connect_status():
    with _LOCK:
        entry = _active
    return _poll_status(entry)


@router.post("/api/ai/accounts/connect/cancel")
def api_ai_accounts_connect_cancel(x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    global _active
    with _LOCK:
        entry, _active = _active, None
    if entry is None:
        return {"ok": True, "canceled": False}
    entry.cancel_event.set()  # wakes _listen within its <=1s poll tick
    # Best-effort: also drop the proxy's own pending OAuth session so it
    # doesn't sit around for its 30-minute TTL. Safe even if the state
    # already completed, failed, or was never reached by the listener —
    # ai_proxy.cancel_login's own contract is a no-op past TTL/completion.
    try:
        ai_proxy.cancel_login(entry.state)
    except RuntimeError:
        pass
    return {"ok": True, "canceled": True}


@router.delete("/api/ai/accounts/{name}")
def api_ai_accounts_delete(name: str, x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not _valid_credential_name(name):
        return _error("'name' is not a valid credential name")
    try:
        ai_proxy.delete_credential(name)
    except RuntimeError as e:
        return _error(str(e), 502)
    return {"ok": True}
