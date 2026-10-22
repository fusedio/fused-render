"""The connect sign-in driver: loopback + PKCE and RFC 8628 device code, every
provider a config entry (SPEC-connect-oauth §3, §11).

**Loopback + PKCE (`LoopbackFlow`).** `_fused_login.py`'s flow, parameterised:
a `state`, a `code_verifier` and its S256 challenge; a one-shot HTTP listener
on the registered redirect (`localhost` on the first free port of the pack's
list, or any free port on `127.0.0.1` when the list is null, Google's rule);
the authorize URL carries `client_id`, `redirect_uri`, `state`, the challenge,
the pack's `scopes` dict spread as query parameters and `extra_authorize`
(MCP's `resource`, Google's `access_type`). The listener checks `state` (a
mismatch is logged and ignored: it is someone else's tab, or a stale one),
exchanges the code in the request it arrived on, so the page it answers says
whether sign-in actually worked, then stops. `result()` blocks on an Event.

**The paste fallback is the same code path.** When the browser is not on this
machine (SSH, a container) it lands on a loopback URL it cannot reach and
shows that URL; `complete_from_url()` parses `code` and `state` out of it and
delivers them exactly as the listener would. It also covers a redirect list
whose ports are all busy: `start()` still returns a URL (for the first port,
with nothing listening) and `listening` says so, rather than failing a sign-in
the paste would have finished. The code is useless without the verifier, so
whatever else holds that port learns nothing it can use.

**Device code (`DeviceFlow`).** `hf_auth.py`'s shape with URLs from the pack:
`start()` returns what the page shows, `result()` polls on the caller's thread
(the manager runs it on a daemon thread), honouring `interval` and
`slow_down`; `cancel()` wakes the wait at once.

**Token responses** are read leniently because the first wave is not uniform:
standard JSON; a form-encoded body (GitHub without `Accept: application/json`,
which we always send anyway); Slack's `{ok, error}` envelope, where a user
token lives under `authed_user`; and GitHub's habit of answering errors with
200. `refresh()` returns `refresh=None` when the server did not rotate, and the
caller keeps the refresh token it had.

All HTTP is `httpx`. Every entry point takes `http=` (an `httpx.Client`) so
tests can hand in a `MockTransport`; without one a short-lived client is made
per call. Nothing here logs a token, a code or a verifier.

Rejected: `authlib`/`oauthlib` (a dependency for ~300 lines, and neither knows
Slack's envelope); a `ThreadingHTTPServer` for the listener (one callback is
the whole job, and a serial loop that re-checks a stop flag every half second
is what lets `cancel()` work without `shutdown()` from inside a handler).
"""
from __future__ import annotations

import base64
import hashlib
import html
import logging
import secrets
import socketserver
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

logger = logging.getLogger(__name__)

REDIRECT_HOST, REDIRECT_PORTS, REDIRECT_PATH = "localhost", [47823, 47824, 47825, 47826, 47827], "/callback"
HTTP_TIMEOUT = 30.0
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
#: RFC 8628 §3.5: `slow_down` adds 5 s to the interval, unless the server
#: names the new interval itself (GitHub does).
SLOW_DOWN_STEP = 5.0


class FlowError(Exception):
    """A sign-in that cannot finish: the vendor said no, or answered nonsense.

    `code` is the OAuth error code when the vendor gave one."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class FlowCancelled(FlowError):
    """`cancel()` was called while the flow was live."""


class FlowTimeout(FlowError):
    """Nobody finished the sign-in within `result()`'s timeout."""


@dataclass
class TokenSet:
    access: str
    refresh: str | None
    expires_at: float | None
    token_type: str
    scope: str | None
    raw: dict = field(repr=False)

    def __repr__(self) -> str:  # never print a token, even in a traceback
        return (f"TokenSet(access=<{len(self.access)} chars>, refresh={'<set>' if self.refresh else None}, "
                f"expires_at={self.expires_at}, token_type={self.token_type!r}, scope={self.scope!r})")


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge) per RFC 7636: 64 url-safe chars, base64url
    SHA-256 without padding."""
    verifier = secrets.token_urlsafe(48)
    return verifier, _s256(verifier)


def _s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def redirect_uris(redirect: dict | None = None) -> list[str]:
    """Every redirect URI a redirect spec can produce — what a DCR
    registration or a CIMD document lists. Empty when `ports` is null (any
    port: the vendor must accept loopback port wildcards, RFC 8252 §7.3)."""
    r = redirect or {"host": REDIRECT_HOST, "ports": REDIRECT_PORTS, "path": REDIRECT_PATH}
    return [f"http://{r['host']}:{p}{r['path']}" for p in (r.get("ports") or [])]


# ------------------------------------------------------------------ HTTP ---

class _Http:
    """`with _Http(http) as c:` — the caller's client, or a fresh one closed after."""

    def __init__(self, http):
        self._given = http
        self._own = None

    def __enter__(self) -> httpx.Client:
        if self._given is not None:
            return self._given
        self._own = httpx.Client(timeout=HTTP_TIMEOUT)
        return self._own

    def __exit__(self, *exc):
        if self._own is not None:
            self._own.close()


def _body(resp: httpx.Response) -> dict:
    """JSON body, else form-encoded, else {}."""
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        pass
    try:
        qs = parse_qs(resp.text, keep_blank_values=True)
        return {k: v[0] for k, v in qs.items()} if qs else {}
    except Exception:  # noqa: BLE001
        return {}


def _oauth_error(body: dict) -> FlowError | None:
    """The error a token-endpoint body carries, if any (standard or Slack)."""
    if body.get("ok") is False:
        code = str(body.get("error") or "slack_error")
        return FlowError(code, code=code)
    if body.get("error"):
        code = str(body["error"])
        desc = body.get("error_description")
        return FlowError(f"{code}: {desc}" if desc else code, code=code)
    return None


def _token_set(resp: httpx.Response) -> TokenSet:
    body = _body(resp)
    err = _oauth_error(body)
    if err is not None:
        raise err
    if resp.status_code >= 400:
        raise FlowError(f"token endpoint answered HTTP {resp.status_code}")
    src = body
    user = body.get("authed_user")
    if isinstance(user, dict) and user.get("access_token"):
        src = user  # Slack: the user token is the one a user-scope app asked for
    access = src.get("access_token")
    if not access or not isinstance(access, str):
        raise FlowError("token endpoint returned no access_token")
    expires_at = None
    try:
        if src.get("expires_in") not in (None, ""):
            expires_at = time.time() + float(src["expires_in"])
    except (TypeError, ValueError):
        expires_at = None
    scope = src.get("scope", body.get("scope"))
    return TokenSet(access=access, refresh=src.get("refresh_token") or None,
                    expires_at=expires_at, token_type=str(src.get("token_type") or "Bearer"),
                    scope=scope if isinstance(scope, str) else None, raw=body)


def _post_token(http, url: str, data: dict) -> httpx.Response:
    with _Http(http) as c:
        try:
            return c.post(url, data=data, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise FlowError(f"could not reach the token endpoint: {exc.__class__.__name__}") from None


# --------------------------------------------------------------- loopback ---

_PAGE = """<!doctype html><meta charset="utf-8"><title>{title}</title>
<style>body{{font:15px -apple-system,system-ui,sans-serif;margin:15vh auto;max-width:28em;text-align:center;color:#222}}
@media (prefers-color-scheme:dark){{body{{background:#1e1e1e;color:#ddd}}}}</style>
<h2>{title}</h2><p>{body}</p>"""


def _page(title: str, body: str) -> bytes:
    return _PAGE.format(title=html.escape(title), body=html.escape(body)).encode("utf-8")


class _Server(HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), which can stall for
        # seconds on a Mac with slow reverse DNS. The name is never used.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


class LoopbackFlow:
    """One authorization-code + PKCE sign-in through a loopback redirect."""

    def __init__(self, *, authorize_url, token_url, client_id, redirect: dict | None = None,
                 scopes: dict | None = None, extra_authorize: dict | None = None,
                 extra_token: dict | None = None, client_secret: str | None = None, http=None):
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.client_id = client_id
        self.redirect = dict(redirect) if redirect else {
            "host": REDIRECT_HOST, "ports": list(REDIRECT_PORTS), "path": REDIRECT_PATH}
        self.scopes = dict(scopes or {})
        self.extra_authorize = dict(extra_authorize or {})
        self.extra_token = dict(extra_token or {})
        self.client_secret = client_secret
        self.http = http
        self.state = secrets.token_urlsafe(24)
        self._verifier, self._challenge = pkce_pair()
        self.redirect_uri: str = ""
        self.url: str = ""
        #: False when every registered port was busy: only the paste can finish.
        self.listening = False
        self._server: _Server | None = None
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._claimed = False
        self._cancelled = False
        self._token: TokenSet | None = None
        self._error: FlowError | None = None

    # -- start ---------------------------------------------------------------

    def _bind(self) -> None:
        host = self.redirect.get("host") or REDIRECT_HOST
        path = self.redirect.get("path") or REDIRECT_PATH
        ports = self.redirect.get("ports")
        handler = self._handler_class()
        if ports is None:
            self._server = _Server(("127.0.0.1", 0), handler)
            port = self._server.server_address[1]
            uri_host = self.redirect.get("host") or "127.0.0.1"
        else:
            port = None
            for p in ports:
                try:
                    self._server = _Server((host, p), handler)
                    port = p
                    break
                except OSError:
                    continue
            uri_host = host
            if port is None:
                port = ports[0]
                logger.warning("connect: every redirect port is busy (%s); "
                               "the sign-in can finish only by pasting the address", ports)
        self.listening = self._server is not None
        self.redirect_uri = f"http://{uri_host}:{port}{path}"

    def start(self) -> str:
        """Bind the listener and return the URL to open in the browser."""
        if self.url:
            return self.url
        self._bind()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "code_challenge": self._challenge,
            "code_challenge_method": "S256",
            **self.scopes,
            **self.extra_authorize,
        }
        sep = "&" if urlparse(self.authorize_url).query else "?"
        self.url = self.authorize_url + sep + urlencode(params)
        if self._server is not None:
            self._server.timeout = 0.5
            threading.Thread(target=self._serve, name="connect-loopback", daemon=True).start()
        return self.url

    def _serve(self) -> None:
        srv = self._server
        try:
            while not self._done.is_set():
                srv.handle_request()
        except Exception:
            logger.warning("connect: loopback listener stopped unexpectedly", exc_info=True)
        finally:
            srv.server_close()

    def _handler_class(self):
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                u = urlparse(self.path)
                if u.path != (flow.redirect.get("path") or REDIRECT_PATH):
                    return self._answer(404, "Not found", "This address is only for signing in.")
                status, title, body = flow._deliver(parse_qs(u.query))
                self._answer(status, title, body)

            def _answer(self, status, title, body):
                data = _page(title, body)
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                return

        return Handler

    # -- delivery (listener and paste) ----------------------------------------

    def _deliver(self, qs: dict) -> tuple[int, str, str]:
        """Handle one redirect's query. Returns (status, title, body) for the page."""
        def one(k):
            v = qs.get(k)
            return v[0] if v else None

        state, code, error = one("state"), one("code"), one("error")
        if not code and not error:
            return 400, "Nothing to do", "This address has no sign-in code in it."
        if state != self.state:
            logger.info("connect: ignored a redirect whose state does not match this flow")
            return 400, "Not this sign-in", ("This address belongs to a different or older sign-in. "
                                             "Start again from fused-render if this one is stuck.")
        with self._lock:
            if self._claimed or self._done.is_set():
                return 200, "Already done", "This sign-in has already finished. You can close this tab."
            self._claimed = True
        if error:
            desc = one("error_description")
            self._finish(error=FlowError(f"{error}: {desc}" if desc else error, code=error))
            return 400, "Sign-in was not completed", f"The service said: {error}. You can close this tab."
        try:
            tokens = self._exchange(code)
        except FlowError as exc:
            self._finish(error=exc)
            return 502, "Sign-in failed", f"{exc}. You can close this tab and try again."
        self._finish(token=tokens)
        return 200, "Signed in", "You can close this tab and return to fused-render."

    def _exchange(self, code: str) -> TokenSet:
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": self._verifier,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            **self.extra_token,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        return _token_set(_post_token(self.http, self.token_url, data))

    def _finish(self, *, token: TokenSet | None = None, error: FlowError | None = None) -> None:
        self._token, self._error = token, error
        self._done.set()

    def complete_from_url(self, url: str) -> None:
        """The paste fallback: finish with the address the browser ended up on.

        Raises FlowError when the address carries no code, belongs to another
        flow, or the flow is already over; the flow itself keeps waiting then.
        A vendor-side failure (bad code, `error=access_denied`) ends the flow
        and is what `result()` raises, the same as through the listener."""
        if not self.url:
            raise FlowError("this sign-in has not started")
        qs = parse_qs(urlparse((url or "").strip()).query)
        if not qs.get("code") and not qs.get("error"):
            raise FlowError("that address has no sign-in code in it")
        if (qs.get("state") or [None])[0] != self.state:
            raise FlowError("that address belongs to a different sign-in")
        if self._done.is_set() or self._claimed:
            raise FlowError("this sign-in has already finished")
        self._deliver(qs)

    # -- waiting ---------------------------------------------------------------

    def result(self, timeout: float = 600) -> TokenSet:
        """Block until the redirect arrives (or the paste). Raises
        FlowCancelled / FlowError / FlowTimeout; a timeout stops the listener."""
        if not self.url:
            raise FlowError("start() was not called")
        if not self._done.wait(timeout):
            self._cancelled = True
            self._done.set()
            raise FlowTimeout("the sign-in was not completed in time")
        if self._token is not None:
            return self._token
        if self._error is not None:
            raise self._error
        if self._cancelled:
            raise FlowCancelled("sign-in cancelled")
        raise FlowError("sign-in ended without a token")

    def cancel(self) -> None:
        """Stop the listener; a waiting `result()` raises FlowCancelled."""
        with self._lock:
            if self._done.is_set():
                return
            self._claimed = True
            self._cancelled = True
        self._done.set()


# ----------------------------------------------------------------- device ---

class DeviceFlow:
    """One RFC 8628 device-authorization sign-in."""

    def __init__(self, *, device_url, token_url, client_id, scope: str | None = None, http=None):
        self.device_url = device_url
        self.token_url = token_url
        self.client_id = client_id
        self.scope = scope
        self.http = http
        self.interval = 5.0
        self.expires_at = 0.0
        self._device_code: str | None = None
        self._cancel = threading.Event()

    def start(self) -> dict:
        """Ask for a device code. Returns {verification_uri,
        verification_uri_complete?, user_code, expires_in, interval}."""
        data = {"client_id": self.client_id}
        if self.scope:
            data["scope"] = self.scope
        with _Http(self.http) as c:
            try:
                resp = c.post(self.device_url, data=data, headers={"Accept": "application/json"})
            except httpx.HTTPError as exc:
                raise FlowError(f"could not reach the device endpoint: {exc.__class__.__name__}") from None
        body = _body(resp)
        err = _oauth_error(body)
        if err is not None:
            raise err
        if resp.status_code >= 400 or not body.get("device_code") or not body.get("user_code"):
            raise FlowError(f"device endpoint answered HTTP {resp.status_code} without a code")
        self._device_code = body["device_code"]
        try:
            self.interval = max(0.0, float(body.get("interval", 5)))
        except (TypeError, ValueError):
            self.interval = 5.0
        try:
            expires_in = float(body.get("expires_in", 900))
        except (TypeError, ValueError):
            expires_in = 900.0
        self.expires_at = time.time() + expires_in
        out = {
            # Google spells it verification_url.
            "verification_uri": body.get("verification_uri") or body.get("verification_url"),
            "user_code": body["user_code"],
            "expires_in": expires_in,
            "interval": self.interval,
        }
        if body.get("verification_uri_complete"):
            out["verification_uri_complete"] = body["verification_uri_complete"]
        return out

    def result(self, timeout: float = 900) -> TokenSet:
        """Poll until approved. `authorization_pending` keeps polling,
        `slow_down` widens the interval, `expired_token` / `access_denied`
        (and any other error) raise FlowError."""
        if not self._device_code:
            raise FlowError("start() was not called")
        deadline = min(time.time() + timeout, self.expires_at or float("inf"))
        data = {"grant_type": DEVICE_GRANT, "device_code": self._device_code,
                "client_id": self.client_id}
        while True:
            wait = min(self.interval, max(0.0, deadline - time.time()))
            if self._cancel.wait(wait):
                raise FlowCancelled("sign-in cancelled")
            if time.time() >= deadline:
                raise FlowTimeout("the device code expired before it was approved")
            resp = _post_token(self.http, self.token_url, data)
            if self._cancel.is_set():
                raise FlowCancelled("sign-in cancelled")
            err = _oauth_error(_body(resp))
            if err is None:
                return _token_set(resp)
            if err.code == "authorization_pending":
                continue
            if err.code == "slow_down":
                try:
                    named = float(_body(resp).get("interval"))
                except (TypeError, ValueError):
                    named = None
                self.interval = named if named and named > self.interval else self.interval + SLOW_DOWN_STEP
                continue
            raise err

    def cancel(self) -> None:
        self._cancel.set()


# ---------------------------------------------------------------- refresh ---

def refresh(*, token_url, client_id, refresh_token, client_secret=None, extra: dict | None = None,
            http=None) -> TokenSet:
    """Trade a refresh token for a fresh access token.

    The returned `refresh` is None when the server did not rotate it: the
    caller keeps the refresh token it already has (RFC 6749 §6 lets a server
    reuse it). A server that rotates returns the new one and the old one is
    dead from then on, so the caller must store it before the next refresh."""
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id, **(extra or {})}
    if client_secret:
        data["client_secret"] = client_secret
    return _token_set(_post_token(http, token_url, data))


# ---------------------------------------------- identity and revoke helpers ---
# Not in the §11 contract; added so every kind probes and revokes one way.

_LABEL_KEYS = ("login", "username", "user", "name", "email", "id")


def _is_slack_api(url: str) -> bool:
    u = urlparse(url)
    return u.hostname == "slack.com" and u.path.startswith("/api/")


def identity(identity_url: str, access: str, http=None) -> str | None:
    """The account label an identity endpoint gives for `access`, or None
    when the token is rejected.

    GET with Bearer, except Slack Web API methods (`slack.com/api/*`), which
    are POSTed and answer `{ok, user, team, ...}`. The label is the first of
    login / username / user / name / email / id that is present (GitHub:
    `login`; Slack `auth.test`: `user`). Raises FlowError when the endpoint
    cannot be reached, so "rejected" and "offline" are told apart."""
    headers = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
    with _Http(http) as c:
        try:
            if _is_slack_api(identity_url):
                resp = c.post(identity_url, headers=headers)
            else:
                resp = c.get(identity_url, headers=headers)
        except httpx.HTTPError as exc:
            raise FlowError(f"could not reach the identity endpoint: {exc.__class__.__name__}") from None
    if resp.status_code in (401, 403):
        return None
    body = _body(resp)
    if resp.status_code >= 400 or body.get("ok") is False:
        return None
    for k in _LABEL_KEYS:
        v = body.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def revoke(revoke_url: str, token: str, *, client_id: str | None = None,
           token_type_hint: str | None = None, http=None) -> bool:
    """Best-effort revocation (RFC 7009, and Slack's `auth.revoke`). True
    when the vendor confirmed; never raises, since disconnect must still clear
    the local record when the vendor is unreachable."""
    headers = {"Accept": "application/json"}
    data = {"token": token}
    if _is_slack_api(revoke_url):
        headers["Authorization"] = f"Bearer {token}"
    if client_id:
        data["client_id"] = client_id
    if token_type_hint:
        data["token_type_hint"] = token_type_hint
    with _Http(http) as c:
        try:
            resp = c.post(revoke_url, data=data, headers=headers)
        except httpx.HTTPError:
            return False
    if resp.status_code >= 400:
        return False
    return _body(resp).get("ok") is not False
