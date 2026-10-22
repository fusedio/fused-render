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
this listener in ``Host`` (closes DNS rebinding) and must not come from
another site: ``Sec-Fetch-Site`` ``same-origin`` (the frame's own fetches),
``same-site`` (the workspace page framing it; a port does not change the
site) or ``none`` are served; ``cross-site`` is refused, and so is a
request whose ``Origin``/``Referer`` names a non-loopback host. A cross-site
page can therefore neither read through this proxy nor drive a
state-changing request with the user's token. WebSockets are not forwarded
(the workbench's embed needs none).

Upstream answers pass through with hop-by-hop headers removed, ``Location``
rewritten from the upstream origin to this one, and ``Set-Cookie`` dropped:
embed mode authenticates with the bearer, and a cookie scoped to the
upstream domain would not be accepted here anyway. A 401 from upstream
forgets the cached token so the next request fetches a refreshed one; the
embed's own refresh ping to the parent (``fused-embed-auth-refresh``) still
works as before.
"""
from __future__ import annotations

import asyncio
import logging
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


def _strip_headers(headers, drop: set[bytes]) -> list[tuple[bytes, bytes]]:
    """`headers` without `drop` and without any header a `Connection` header
    names: those are hop-by-hop for this one message (RFC 9110 §7.6.1)."""
    nominated = {name.strip().lower() for k, v in headers if k.lower() == b"connection"
                 for name in v.split(b",")}
    return [(k, v) for k, v in headers if k.lower() not in drop and k.lower() not in nominated]


def _is_loopback_host(hostport: str) -> bool:
    host = hostport.rsplit(":", 1)[0] if not hostport.startswith("[") else hostport.split("]")[0] + "]"
    return host.lower() in _LOOPBACK_HOSTS


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
    CLI child), shared by concurrent requests, forgotten on a 401."""

    def __init__(self, source: TokenSource):
        self._source = source
        self._token: str | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> str:
        async with self._lock:
            if self._token is not None and time.monotonic() - self._fetched_at < TOKEN_TTL_S:
                return self._token
            self._token = await asyncio.to_thread(self._source)
            self._fetched_at = time.monotonic()
            return self._token

    def forget(self) -> None:
        self._token = None


class ProxyApp:
    """ASGI app: forward to `upstream` (an origin, no path) with the bearer."""

    def __init__(self, upstream: str, token: TokenSource, port: int):
        self.upstream = upstream.rstrip("/")
        self._tokens = _TokenCache(token)
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
        if site == "cross-site":
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

    def apply(self, upstream: str | None, token: TokenSource | None) -> None:
        with self._lock:
            if upstream is None or token is None:
                self._stop()
                return
            upstream = upstream.rstrip("/")
            if self.running and self.upstream == upstream:
                return
            self._stop()
            self._start(upstream, token)

    def _start(self, upstream: str, token: TokenSource) -> None:
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
        app = ProxyApp(upstream, token, port)
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
