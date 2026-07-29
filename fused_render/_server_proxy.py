import asyncio
import urllib.request
import httpx
from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)




# Response headers forwarded from the rclone serve on a proxied /api/fs/raw.
# Content-Length/-Range/Accept-Ranges make ranged readers (duckdb httpfs)
# work; Last-Modified/ETag let their caches revalidate.
_PROXY_HEADERS = ("content-length", "content-range", "content-type",
                  "accept-ranges", "last-modified", "etag")


# Media types a browser will EXECUTE as a document rather than display as data.
_SCRIPTABLE_MEDIA = frozenset((
    "text/html", "application/xhtml+xml", "image/svg+xml"))

# Fetch destinations that make the response a document on THIS origin: a
# top-level navigation, or a frame of one.
_DOCUMENT_DESTS = frozenset(("document", "iframe", "frame", "embed", "object"))


def _harden_raw(resp, request: Request):
    """Stop /api/fs/raw from handing a page the app's own origin.

    /api/fs/raw serves any absolute path with a content-type guessed from its
    name, and it is a plain GET with no X-Fused, so it is top-level navigable
    — and navigation is not subject to CORS. A foreign site can therefore point
    the browser at a .html file it arranged to be on disk (a drive-by download
    into ~/Downloads names the file for it) and get that file running as a
    FIRST-PARTY document on http://127.0.0.1:<port>. From there it is inside
    the trust boundary: it can send X-Fused: 1 and POST /api/run.

    D4 concedes that an .html file *you open* runs same-origin. That is about
    the user choosing the file; here the attacker chooses it, so the concession
    does not stretch to cover it.

    Two measures, both applied to every response this route produces:

      * `nosniff` unconditionally — every in-tree consumer reads this endpoint
        as data (.text()/.arrayBuffer()), so none of them can be hurt by it;

      * scriptable types are downgraded to text/plain ONLY when the request is
        a document load. The threat is navigating or framing; an <img src> at
        an SVG cannot execute script, and coercing it would break a working
        case to fix one that does not exist. Sec-Fetch-Dest is the right signal
        and this file already relies on browsers always sending Sec-Fetch-*
        (see the 307 branch in api_fs_raw). Sec-Fetch-Mode is checked too, for
        a browser that sends the mode but not the dest.

    Redirects are left alone: the 307 points at the object store, a foreign
    origin, and is already gated to non-browser clients."""
    if 300 <= resp.status_code < 400:
        return resp
    resp.headers["x-content-type-options"] = "nosniff"
    dest = request.headers.get("sec-fetch-dest", "").lower()
    mode = request.headers.get("sec-fetch-mode", "").lower()
    if dest in _DOCUMENT_DESTS or mode == "navigate":
        media = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if media in _SCRIPTABLE_MEDIA:
            # Keep serving the bytes — a download still saves the real file
            # under its own name — just never as a document on this origin.
            resp.headers["content-type"] = "text/plain; charset=utf-8"
    return resp


def _proxy_raw(upstream: str, request: Request):
    """Forward one GET/HEAD (with its Range header) to a mount's localhost
    rclone serve and stream the answer back. None when the serve can't be
    reached at all — the caller then reads the file the ordinary way; an HTTP
    error from a live serve passes through as-is (a 416/404 is an answer,
    not a reason to fall back to a different read path mid-protocol)."""
    headers = {}
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    req = urllib.request.Request(upstream, headers=headers, method=request.method)
    try:
        r = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        # Error responses carry protocol-level headers too — a 416's
        # `Content-Range: bytes */<size>` is how a range client learns the
        # file's length — so forward the same header set as on success.
        out = {k: v for k, v in e.headers.items() if k.lower() in _PROXY_HEADERS}
        try:
            payload = b"" if request.method == "HEAD" else e.read()
        finally:
            e.close()
        return Response(content=payload, status_code=e.code, headers=out)
    except OSError:
        return None
    out = {k: v for k, v in r.headers.items() if k.lower() in _PROXY_HEADERS}
    if request.method == "HEAD":
        r.close()
        return Response(status_code=r.status, headers=out)

    def body():
        try:
            while chunk := r.read(256 * 1024):
                yield chunk
        finally:
            r.close()

    return StreamingResponse(body(), status_code=r.status, headers=out)


async def _proxy_raw_pooled(client: httpx.AsyncClient, upstream: str,
                            request: Request, extra_headers: dict | None = None):
    """Opt-in pooled proxy of a store's *signed* URL (TASK F). Same forwarded
    header set (_PROXY_HEADERS) and status pass-through (206/416/404) as
    _proxy_raw, but streams through a shared keep-alive httpx pool so a burst of
    range reads reuses sockets to the store instead of paying urllib's fresh
    TLS handshake + redirect round trip per block. Returns None only when the
    store is unreachable at the connection level — the caller then falls back to
    the 307 so the read still completes. Async (awaited on the event loop): the
    pool is the point, and to_thread'ing a sync client would defeat it.

    `extra_headers` (e.g. an Authorization: Bearer for the private-GCS bearer
    tier) is merged onto the OUTBOUND request after the Range header — it never
    appears on the response, so a token there is never echoed to the client."""
    r = await _pooled_send(client, upstream, request, extra_headers)
    if r is None:
        return None
    return await _pooled_response(r, request)


async def _pooled_send(client: httpx.AsyncClient, upstream: str,
                       request: Request, extra_headers: dict | None = None):
    """Send one pooled, streamed request to `upstream` (mirroring the request's
    method + Range, plus `extra_headers`) and return the OPEN httpx response, or
    None on a connection-level error. The caller must either build a response
    from it (_pooled_response, which closes it) or aclose it — so a peek-then-
    retry path (bearer 401/403) can inspect the status and drop the response."""
    headers = {}
    rng = request.headers.get("range")
    if rng:
        headers["Range"] = rng
    if extra_headers:
        headers.update(extra_headers)
    req = client.build_request(request.method, upstream, headers=headers)
    try:
        return await client.send(req, stream=True)
    except httpx.HTTPError:
        return None


async def _pooled_response(r, request: Request):
    """Build the client-facing response from an open httpx response `r`,
    forwarding only _PROXY_HEADERS and passing the status through. Closes `r`
    (immediately for HEAD, after streaming for GET)."""
    out = {k: v for k, v in r.headers.items() if k.lower() in _PROXY_HEADERS}
    if request.method == "HEAD":
        await r.aclose()
        return Response(status_code=r.status_code, headers=out)

    async def body():
        try:
            async for chunk in r.aiter_bytes(256 * 1024):
                yield chunk
        finally:
            await r.aclose()

    return StreamingResponse(body(), status_code=r.status_code, headers=out)


def _bearer_status_passes(status: int) -> bool:
    """Whether a bearer-proxy upstream status is a client-facing answer. 2xx/3xx
    (success/redirect) and the meaningful store answers 404 (absent) and 416
    (unsatisfiable range) pass through; everything else (401, 403, 429, 5xx) is
    NOT a client-facing answer in bearer mode — there is no 307 URL for the
    client to retry against, and on main these reads went via the rclone serve
    whose pacer retries transient errors — so the caller falls through to the
    serve instead of leaking the error status (finding 9)."""
    return 200 <= status < 400 or status in (404, 416)


async def _proxy_raw_bearer(request: Request, path: str):
    """Proxy a cold private-GCS read through the pooled client with the bearer
    Authorization header attached out-of-band (the token must never reach the
    client in a URL/redirect).

    On a 401 (stale/rotated token) invalidate the cached credential, re-resolve,
    and retry ONCE. A 403 is an IAM denial WITH a valid token, so it does NOT
    invalidate — churning the credential per denied read would evict the live
    token out from under concurrent legitimate reads (mirrors _gcs_get_direct's
    401-only policy) — it simply falls through to the serve. Any non-pass-through
    status (a second 401, 403, 429, 5xx) also falls through by returning None
    (never an error status to the client), so the read still completes via the
    serve. The open upstream response is always closed before a retry or a
    fall-through."""
    from fused_render.shell import mounts as shell_mounts

    for attempt in (1, 2):
        bearer = await asyncio.to_thread(shell_mounts.bearer_upstream_for, path)
        if bearer is None:
            return None
        # Only now (a real bearer remote) is the pooled client needed — reaching
        # for it before the bearer check would break the non-bearer fall-through.
        client = request.app.state.pooled_client
        url, extra_headers = bearer
        r = await _pooled_send(client, url, request, extra_headers)
        if r is None:
            return None
        if _bearer_status_passes(r.status_code):
            return await _pooled_response(r, request)
        await r.aclose()
        # 401 on the first attempt: token went stale/rotated -> re-resolve and
        # retry once. 403 (IAM denial), transient 429/5xx, and a second 401 all
        # fall through to the serve WITHOUT invalidating.
        if attempt == 1 and r.status_code == 401:
            await asyncio.to_thread(shell_mounts.invalidate_gcs_token, path)
            continue
        return None
    return None
