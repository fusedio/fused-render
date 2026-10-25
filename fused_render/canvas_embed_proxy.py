"""A loopback proxy that frames a custom deployment's workbench with a bearer.

The canvas workspace embeds the hosted workbench in an iframe
(``<base>/workbench/<handle>/<name>?fused_embed_auth=1``). The hosted
environments serve that page without a login cookie and let the frame ask
its parent for a bearer over postMessage. A custom deployment (D1411) may
sit behind a gateway that requires a browser login for the SPA itself: a
cross-site iframe never carries that cookie, so the frame is redirected to
the identity provider, which refuses to render in a frame, and the embed
never loads.

Such a gateway can instead admit requests carrying ``Authorization: Bearer``
with a token its own tenant issued. Browsers never send that header on an
iframe navigation or an asset load, so this module does: when the
``canvases_embed_proxy`` preference is on, the custom environment's embed
loads from a SECOND loopback listener, ``http://127.0.0.1:<port>``, which
forwards every request to the deployment's workbench origin with the custom
env's own ``fused login`` token (``canvases.access_token``, D1412) attached.
The workspace frames that origin and posts its messages to it; the
workbench inside runs in embed mode exactly as it does against the hosted
environments, and its relative ``/server``, ``/_next`` and ``/api`` paths
all land here and go upstream with the bearer.

A listener of its own, not a path on the main server: the workbench uses
absolute paths, which on the main origin would collide with fused-render's
own routes, and a separate origin keeps the bearer-injecting surface apart
from the app's pages. Loopback only, like the main server (D3).

Who gets in. The proxy attaches a real credential to whatever reaches it, so
it admits only what the workspace's frame produces. Every request must name
this listener in ``Host`` (closes DNS rebinding) and carry
``Sec-Fetch-Site``: ``same-origin`` (the framed workbench's own requests) and
``none`` (a navigation the user made) are served. ``same-site`` is served
only for the frame's own load: ``Sec-Fetch-Dest: iframe``,
``Sec-Fetch-Mode: navigate`` and a ``Referer`` on the app's own origin
(``FUSED_RENDER_ORIGIN``'s port). A browser counts every loopback port as
the same site, so without that a page any other local server serves could
drive requests with the user's token. ``cross-site`` is refused, and so is
a request with no ``Sec-Fetch-Site`` (a browser too old to send it, or a
non-browser client) and one whose ``Origin``/``Referer`` names a
non-loopback host. WebSockets are not forwarded (the workbench's embed
needs none).

Upstream answers pass through with hop-by-hop headers removed, ``Location``
rewritten from the upstream origin to this one, ``Access-Control-*`` dropped
(the frame is same-origin with this listener and never needs a CORS grant;
relayed, a grant such as ``Allow-Origin: *`` would let another origin read
answers made with the user's token), and ``Set-Cookie`` dropped:
embed mode authenticates with the bearer, and a cookie scoped to the
upstream domain would not be accepted here anyway. A 401 from upstream
forgets the cached token so the next request fetches a refreshed one; the
embed's own refresh ping to the parent (``fused-embed-auth-refresh``) still
works as before.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

logger = logging.getLogger("fused_render.canvas_embed_proxy")

#: Seconds a fetched token is reused before asking the credential store
#: again. The store refreshes an expired token itself (_fused_token.py), so
#: this only bounds how long the proxy keeps using one after a re-login.
TOKEN_TTL_S = 300.0
#: Per-request upstream timeouts: the workbench's own calls (UDF runs) can
#: be long; connects should not.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 600.0

_HOP_BY_HOP = {
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailer", b"transfer-encoding", b"upgrade",
}
#: Request headers never forwarded: the proxy sets its own Host and
#: Authorization, and an inbound Authorization must not override ours.
#: Cookie: a browser scopes cookies to the host, not the port, so this
#: origin is sent every cookie set on 127.0.0.1 (the app's own, any other
#: local server's), none of which is the deployment's to see.
_DROP_REQUEST = _HOP_BY_HOP | {b"host", b"authorization", b"content-length", b"cookie"}
#: Response headers never relayed: Set-Cookie (see module doc), HSTS (this
#: origin is plain http on loopback) and the framing policies a deployment
#: may set for its own origin, which would stop the workspace framing us.
_DROP_RESPONSE = _HOP_BY_HOP | {
    b"set-cookie", b"strict-transport-security", b"content-length",
    b"x-frame-options", b"content-security-policy",
}
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

TokenSource = Callable[[], str]
#: A cheap fingerprint of the credentials the token comes from (the store's
#: stat): a change means a sign-out, a sign-in or another account.
CredentialsStamp = Callable[[], object]


def _strip_headers(headers, drop: set[bytes]) -> list[tuple[bytes, bytes]]:
    """`headers` without `drop` and without any header a `Connection` header
    names: those are hop-by-hop for this one message (RFC 9110 §7.6.1)."""
    nominated = {name.strip().lower() for k, v in headers if k.lower() == b"connection"
                 for name in v.split(b",")}
    return [(k, v) for k, v in headers if k.lower() not in drop and k.lower() not in nominated]


def _is_loopback_host(hostport: str) -> bool:
    host = hostport.rsplit(":", 1)[0] if not hostport.startswith("[") else hostport.split("]")[0] + "]"
    return host.lower() in _LOOPBACK_HOSTS


def _app_port() -> int:
    """The app's own port, from the FUSED_RENDER_ORIGIN server/app.py exports
    before serving; 0 when unknown, which admits no framing page."""
    try:
        return urlsplit(os.environ.get("FUSED_RENDER_ORIGIN") or "").port or 0
    except ValueError:
        return 0


def _is_app_page(referer: str) -> bool:
    """Whether a frame load's `Referer` is a page on the app's own origin
    (any loopback host name for it: the app answers on each)."""
    try:
        parts = urlsplit(referer)
        port = parts.port
    except ValueError:
        return False
    app_port = _app_port()
    return (parts.scheme == "http" and bool(parts.netloc) and app_port != 0
            and port == app_port and _is_loopback_host(parts.netloc))


def _origin_is_loopback(value: str) -> bool:
    """`Origin` / `Referer` values: absent or `null` pass (an opaque origin
    cannot be another site's page reaching in with credentials we add — it
    would still be cross-site by Sec-Fetch-Site), a URL must be loopback."""
    if not value or value == "null":
        return True
    try:
        netloc = urlsplit(value).netloc
    except ValueError:
        return False
    return bool(netloc) and _is_loopback_host(netloc)


class _TokenCache:
    """One token at a time, fetched off the event loop (the source runs a
    CLI child), shared by concurrent requests, forgotten on a 401 and
    whenever `stamp` changes: a signed-out or switched account must not keep
    riding on the old token for the rest of the TTL."""

    def __init__(self, source: TokenSource, stamp: CredentialsStamp | None = None):
        self._source = source
        self._stamp = stamp
        self._token: str | None = None
        self._token_stamp: object = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            # Read before the fetch: a change landing during it re-fetches
            # next time rather than pinning the old token to the new stamp.
            stamp = self._stamp() if self._stamp is not None else None
            if (self._token is not None and stamp == self._token_stamp
                    and time.monotonic() - self._fetched_at < TOKEN_TTL_S):
                return self._token
            self._token = await asyncio.to_thread(self._source)
            self._token_stamp = stamp
            self._fetched_at = time.monotonic()
            return self._token

    def forget(self) -> None:
        self._token = None


class ProxyApp:
    """ASGI app: forward to `upstream` (an origin, no path) with the bearer."""

    def __init__(self, upstream: str, token: TokenSource, port: int,
                 stamp: CredentialsStamp | None = None):
        self.upstream = upstream.rstrip("/")
        self._tokens = _TokenCache(token, stamp)
        self.port = port
        self._client = None

    def _own_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _get_client(self):
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                follow_redirects=False,
            )
        return self._client

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            await self._lifespan(receive, send)
            return
        if kind != "http":
            # websocket: refuse the handshake; nothing in embed mode needs one.
            await send({"type": "websocket.close", "code": 1003})
            return
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        refusal = self._refuse(headers)
        if refusal is not None:
            await _plain(send, 403, refusal)
            return
        await self._forward(scope, receive, send, headers)

    async def _lifespan(self, receive, send):
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._client is not None:
                    await self._client.aclose()
                await send({"type": "lifespan.shutdown.complete"})
                return

    def _refuse(self, headers: dict[bytes, bytes]) -> str | None:
        host = headers.get(b"host", b"").decode("latin-1")
        if not _is_loopback_host(host) or not host.endswith(f":{self.port}"):
            return "this proxy serves its own loopback origin only"
        site = headers.get(b"sec-fetch-site", b"").decode("latin-1").lower()
        if site == "same-site":
            dest = headers.get(b"sec-fetch-dest", b"").decode("latin-1").lower()
            mode = headers.get(b"sec-fetch-mode", b"").decode("latin-1").lower()
            if dest != "iframe" or mode != "navigate" \
                    or not _is_app_page(headers.get(b"referer", b"").decode("latin-1")):
                return "only the app's canvas frame may load this proxy from another loopback port"
        elif not site:
            return "requests without Sec-Fetch-Site are not forwarded"
        elif site not in ("same-origin", "none"):
            return "cross-site requests are not forwarded"
        for name in (b"origin", b"referer"):
            if not _origin_is_loopback(headers.get(name, b"").decode("latin-1")):
                return "requests from another origin are not forwarded"
        return None

    async def _forward(self, scope, receive, send, headers: dict[bytes, bytes]) -> None:
        import httpx

        path = scope.get("raw_path") or scope["path"].encode()
        query = scope.get("query_string", b"")
        url = self.upstream + path.decode("latin-1") + (("?" + query.decode("latin-1")) if query else "")
        out_headers = _strip_headers(scope.get("headers", []), _DROP_REQUEST)
        body = await _read_body(receive)
        client = await self._get_client()
        try:
            token = await self._tokens.get()
        except Exception as exc:  # noqa: BLE001 — the credential store's failure is the answer
            logger.warning("canvas embed proxy: no token for %s: %s", self.upstream, exc)
            await _plain(send, 502, f"could not read the fused access token: {exc}")
            return
        out_headers.append((b"authorization", f"Bearer {token}".encode()))
        try:
            request = client.build_request(scope["method"], url, headers=out_headers, content=body)
            response = await client.send(request, stream=True)
        except httpx.HTTPError as exc:
            await _plain(send, 502, f"upstream request failed: {exc}")
            return
        try:
            if response.status_code == 401:
                # The gateway or the app rejected the token: forget it so the
                # next request fetches a refreshed one, and relay the 401 (the
                # embed's own refresh handling fires on it).
                self._tokens.forget()
            relay = []
            for k, v in _strip_headers(response.headers.raw, _DROP_RESPONSE):
                lk = k.lower()
                if lk.startswith(b"access-control-"):
                    continue    # see the module doc: no CORS grant leaves here
                if lk == b"location":
                    v = self._rewrite_location(v)
                relay.append((lk, v))
            await send({"type": "http.response.start", "status": response.status_code,
                        "headers": relay})
            async for chunk in response.aiter_raw():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await response.aclose()

    def _rewrite_location(self, value: bytes) -> bytes:
        text = value.decode("latin-1")
        if text.startswith(self.upstream + "/") or text == self.upstream:
            return (self._own_origin() + text[len(self.upstream):]).encode("latin-1")
        return value


async def _read_body(receive) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body"):
            break
    return b"".join(chunks)


async def _plain(send, status: int, text: str) -> None:
    body = text.encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


class Controller:
    """The listener's lifecycle. `apply(upstream, token)` starts it, or
    restarts it when the upstream origin changed (settings.toml edited);
    `apply(None, …)` stops it. Idempotent; safe from any thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._server = None
        self._thread: threading.Thread | None = None
        self.upstream: str | None = None
        self.port: int | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self.port is not None

    def url(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.running else None

    def apply(self, upstream: str | None, token: TokenSource | None,
              stamp: CredentialsStamp | None = None) -> None:
        with self._lock:
            if upstream is None or token is None:
                self._stop()
                return
            upstream = upstream.rstrip("/")
            if self.running and self.upstream == upstream:
                return
            self._stop()
            self._start(upstream, token, stamp)

    def _start(self, upstream: str, token: TokenSource,
               stamp: CredentialsStamp | None = None) -> None:
        import uvicorn

        from fused_render.logs import uvicorn_log_config

        self.error = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(128)
        except OSError as exc:
            self.error = f"could not bind a loopback port: {exc}"
            return
        port = sock.getsockname()[1]
        app = ProxyApp(upstream, token, port, stamp)
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                lifespan="on", log_config=uvicorn_log_config())
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                                  daemon=True, name="fused-canvas-embed-proxy")
        thread.start()
        self._server, self._thread = server, thread
        self.upstream, self.port = upstream, port
        logger.info("canvas embed proxy: %s -> %s", self.url(), upstream)

    def _stop(self) -> None:
        server, thread = self._server, self._thread
        self._server = self._thread = None
        self.upstream = self.port = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5.0)

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """Block until the listener accepts connections (tests, first open)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            server = self._server
            if server is not None and getattr(server, "started", False):
                return True
            if self._thread is not None and not self._thread.is_alive():
                return False
            time.sleep(0.02)
        return False


controller = Controller()
