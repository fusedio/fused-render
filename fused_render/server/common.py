import asyncio
import logging
import os
import time
import traceback
import uuid
from urllib.parse import unquote, urlsplit
from fastapi import Request
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from fastapi.responses import (
    JSONResponse,
)

from fused_render import calls as shell_calls
from fused_render import jobs as shell_jobs
from fused_render import netlog
from fused_render.server.asset_version import asset_version



logger = logging.getLogger(__name__)


def _forced_engine() -> str | None:
    """The process-level engine override, or None when unset (D69/D70 + §20).

    FUSED_RENDER_ENGINE forces the /api/run engine for the whole process:
    `builtin` never touches the `fused` package even if importable; `auto`
    opts in to it iff importable; `fused` demands it (a missing package is a
    startup error, not a silent fallback). **Unset returns None** — the
    engine then follows the persisted preference (shell/prefs.py, default
    **fused-when-importable** since D204, which reversed D70's builtin
    default here), re-read per request so the Preferences page's switch
    applies without a restart. Logged either way — engine choice changes the
    code contract, so it must never be silent, and a log line describing the
    wrong default is worse than none: it is the only place most people ever
    read this contract.
    """
    from fused_render import engine as _engine

    requested = _engine.forced_override()
    if requested is None:
        logger.info(
            "execution engine: following the preference (~/.fused-render/prefs.json, "
            "default fused when its package is importable, else builtin); "
            "FUSED_RENDER_ENGINE overrides it for this process"
        )
        return None
    if requested not in ("auto", "fused", "builtin"):
        raise RuntimeError(
            f"FUSED_RENDER_ENGINE={requested!r} is not one of: auto, fused, builtin"
        )
    if requested == "builtin":
        logger.info("execution engine: builtin (forced by FUSED_RENDER_ENGINE)")
        return "builtin"
    if _engine.available():
        logger.info("execution engine: fused (forced by FUSED_RENDER_ENGINE)")
        # Probe the app's interpreter now, while logging is configured and no
        # request is waiting (PY-17): it decides whether every header-less script
        # runs on this app's python or falls back to a script venv, and the
        # fallback's warning is the only signal a user gets that it happened. On
        # the preference-driven path (`raw is None`) the first /api/run probes
        # instead — still after logging setup, just not in the startup log.
        _engine.app_interpreter()
        return "fused"
    if requested == "fused":
        raise RuntimeError(
            "FUSED_RENDER_ENGINE=fused but the `fused` package is not importable; "
            "install it (pip install 'fused-render[fused]') or unset the override"
        )
    logger.info("execution engine: builtin (FUSED_RENDER_ENGINE=auto, `fused` not installed)")
    return "builtin"

import fused_render

# The fused_render package dir (NOT this file's own dir — this module lives
# a level deeper, at fused_render/server/common.py).
HERE = os.path.dirname(os.path.abspath(fused_render.__file__))
STATIC_DIR = os.path.join(HERE, "static")


#: The tiers an AI call can name, in FIXED order (SPEC RH-11, D631, D700) —
#: shared by `/api/ai` (server/ai.py) and the four capability routes
#: (routers/ai_runtime.py) so the two can never disagree about the vocabulary.
#: A tier is an ENGINE FAMILY, not a privacy class: `local` (this app's own
#: runners) and `apple` (the OS's on-device models, macOS 26+) both keep the
#: prompt on the machine; `claude` is the one that leaves it. The order is
#: fixed in code, never a user preference. A hosted gateway is appended here
#: when it arrives.
AI_PROVIDERS = ("local", "apple", "claude")

#: The apple tier's PINNED model ids -> the capability each serves (D700). The
#: OS owns the weights and picks the variant (AFM 3 Core vs Core Advanced by
#: hardware on macOS 27), so there is exactly one id per capability and no
#: version in it. Because none of these carries a `/` or ends in `.gguf`, the
#: shape rule (`server/ai.py::_is_local_model`) would route them to Claude —
#: this table is consulted BEFORE it, and is the one place the id -> provider
#: inference for this tier is spelled out: `fused.ai.text({model: "afm-text"})`
#: and `fused.ai.text({provider: "apple"})` mean the same call.
APPLE_MODELS = {
    "afm-text": "text-generation",
    "afm-speech": "automatic-speech-recognition",
    "afm-embedding": "embeddings",
}


def apple_model_for(capability: str) -> str | None:
    """The apple tier's one id for `capability`, or None when it has none
    (image and video: Apple ships no programmatic model for either)."""
    for model, served in APPLE_MODELS.items():
        if served == capability:
            return model
    return None


def provider_of_model(model: str | None) -> str | None:
    """The tier a PINNED id names, or None when the id is not pinned and the
    shape rule (or the default) decides. Today only the apple ids are pinned."""
    if model in APPLE_MODELS:
        return "apple"
    return None


def ai_result(payload: dict, *, provider: str, model: str, warnings=None,
              usage: dict | None = None, finish_reason: str = "stop",
              request_id: str | None = None, metadata: dict | None = None) -> dict:
    """The ONE result frame every `fused.ai` verb resolves with (RH-11, D632).

    Learn it once: `payload` is the verb's own output key(s) — `text`,
    `images`, `videos`, `text`+`segments`, `embeddings`, `answers` — and
    everything else is the same on all six. The frame is the AI SDK's `generateText`
    return contract, because that is the shape page authors already know:

      provider          which tier answered ("local" | "apple" | "claude")
      finishReason      "stop" | "length" | "cancelled"
      warnings          [{type: "unsupported-setting", setting, message}]
      usage             per-verb token/unit counts, camelCase, or null
      response          {id, modelId, timestamp} — what actually ran, when
      providerMetadata  {<provider>: {...}} — everything tier-specific that
                        used to sit at top level (seed, snapped size, file
                        paths, seconds). Read it when you need it; the
                        frame does not change shape because of it.

    No top-level `model` and no echoed inputs: `response.modelId` is the
    resolved id, and the SDK's rule that inputs are not echoed is kept —
    what the server snapped, clamped or invented lives in providerMetadata.
    """
    import datetime as _dt
    frame = {
        "provider": provider,
        "finishReason": finish_reason,
        "warnings": list(warnings or []),
        "usage": usage,
        "response": {
            "id": request_id,
            "modelId": model,
            # One format on every verb and both sides of the wire: seconds, Z
            # (`runtime.js`'s `resultFrame` trims toISOString() to the same).
            "timestamp": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "providerMetadata": {provider: dict(metadata or {})},
    }
    frame.update(payload)
    return frame


def ai_usage_tokens(usage: dict | None) -> dict | None:
    """Internal snake-case token counts -> the frame's camelCase `usage`.

    The counter (`ai_metrics`) keeps `input_tokens`/`output_tokens`; the
    wire speaks the SDK's `inputTokens`/`outputTokens`/`totalTokens`. One
    conversion, here, at the boundary — never two vocabularies in one object.
    """
    if not usage:
        return None
    i, o = usage.get("input_tokens"), usage.get("output_tokens")
    if i is None and o is None:
        return None
    return {"inputTokens": i, "outputTokens": o,
            "totalTokens": (i or 0) + (o or 0)}


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def resolve_py(py, html):
    """Resolve a request's `py` (absolute, or relative to its `html`) into a path.
    Shared by /api/run and /api/engine so both resolve identically. Returns
    (resolved, None) or (None, error-Response)."""
    if not py:
        return None, _error("request body must include 'py': a path to a Python file")
    if os.path.isabs(py):
        return py, None
    if not html:
        return None, _error(
            "'py' is a relative path but 'html' was not provided; either send an "
            "absolute 'py' path or include 'html' so it can be resolved")
    return os.path.normpath(os.path.join(os.path.dirname(html), py)), None


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Guard for the mutating/executing POSTs. Read endpoints are already safe
    # cross-origin because the browser blocks a foreign page from reading our
    # response; but a POST can be fired blind (no-cors fetch) by any website,
    # with no way to read the reply. Requiring a custom request header forces a
    # CORS preflight, which fails cross-origin since we return no CORS headers —
    # so only our own same-origin pages get through. Not authentication (D3
    # stands): it only blocks blind cross-origin POSTs, nothing more.
    if x_fused != "1":
        return _error("missing or invalid X-Fused header", status=403)
    return None


def ws_origin_ok(ws) -> bool:
    # `_require_fused`'s guard for a WebSocket. A socket has NO CORS preflight:
    # any website the user visits can `new WebSocket("ws://127.0.0.1:<port>/…")`
    # and the browser opens it — cross-site WebSocket hijacking. What the
    # browser DOES do is stamp the opening page's Origin on the handshake, and
    # a page cannot forge it. So: the Origin must be exactly this server —
    # scheme + host + port of the Host header the socket arrived on. Another
    # localhost port is a different site (a dev server, another app) and is
    # refused; a missing Origin (non-browser client) or the opaque "null"
    # (sandboxed iframe, file://) never equals it, so both fail closed. Not
    # authentication (D3 stands), same as X-Fused.
    # The Host must also be a loopback name: Origin == Host alone passes a
    # DNS-rebinding page (evil.example re-resolved to 127.0.0.1 sends a
    # matching Origin and Host). The LAN listener refuses sockets outright
    # (lan.py), so loopback is every legitimate caller.
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if not origin or not host:
        return False
    hostname = urlsplit("//" + host).hostname or ""
    if hostname not in ("127.0.0.1", "localhost", "::1") and not hostname.endswith(".localhost"):
        return False
    scheme = "https" if ws.url.scheme == "wss" else "http"
    return origin.lower() == f"{scheme}://{host}".lower()


async def unhandled_exception(request, exc):
    # A bare "Internal Server Error" with an empty body is undebuggable on
    # a DMG install: Finder-launched apps have no visible stderr, so the
    # traceback used to vanish (e.g. a right-click "Open with FusedRender"
    # that 500s on /render or /api/run leaves nothing to report). Put the
    # traceback in the response body (local single-user tool, D3 — the
    # only reader owns the machine) AND in the log file so a later
    # `Open app logs` gives the full story. Log with the request line so a
    # noisy log still pins the failure to a URL.
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # The id the middleware already stamped onto this request's call record
    # (it runs first, then re-raises past us). Echoing it here is what makes
    # `err_id` in the call log an actual join key rather than a dead field:
    # a screenshot of this 500 and the record in the log name each other.
    err_id = getattr(request.state, "fused_err_id", None)
    logger.error(
        "unhandled error on %s %s%s\n%s", request.method, request.url.path,
        f" [err_id {err_id}]" if err_id else "", tb
    )
    return _error(
        f"fused-render internal error on {request.method} "
        f"{request.url.path}"
        + (f" (err_id {err_id})" if err_id else "")
        + f":\n\n{tb}",
        status=500,
    )
# NOT IMPLEMENTED: detecting that the client hung up mid-request, which would
# let an abandoned run be recorded as `disconnected` instead of a served
# request (SPEC CL-5a's named gap). Both obvious approaches are dead ends
# under this app's shape, verified rather than assumed:
#
#   * From a ROUTE: a route still cannot see a hang-up from inside the
#     handler to record it as `disconnected` in the call log. (The old
#     `BaseHTTPMiddleware` wrapped `receive` so `is_disconnected()` never
#     fired; `NoCacheAndLog` below is pure ASGI and passes `receive` through,
#     and it DOES notice a failed `send` / cancelled app call.)
#   * From HERE: the middleware's own request CAN see the disconnect, but
#     `is_disconnected()` peeks by CONSUMING a message off the receive
#     channel (starlette.requests, an immediately-cancelled CancelScope
#     around `_receive()`). Polling it steals the `http.request` body message
#     the downstream route is waiting for, so every request with a body —
#     /api/run included — hangs. A body-less spike hides this completely.
#
# Doing it properly means converting this middleware to pure ASGI so it can
# tee the receive channel instead of racing the route for it. That is a
# change to the hottest path in the server and belongs in its own commit,
# not riding along with the call log. Meanwhile a supersession IS reported by
# the page (CL-5a), which covers the common slider case; a closed tab or
# reload still records as `ok`.

_LOG_SKIP_PREFIXES = ("/static/", "/template-assets/", "/template-shared/")

# The one exemption from no-cache below: Vite's build output under
# shell-dist/assets/ is content-hashed (`[name]-[hash].js|css|png`, the
# rollup default vite.config.js keeps; base `/static/shell-dist/`), so a
# changed file is a NEW URL and the old one can never go stale. Serving them
# immutable stops every native window from revalidating each chunk on open —
# those all share WebKit's 6-connection HTTP/1.1 pool per host:port (measured
# 2026-10-08), so a burst of 304 round-trips queues real API calls behind it.
# shell-dist/index.html, runtime.js and /template-assets are NOT hashed, so
# unversioned requests for them keep no-cache (the stale-half-old-UI reason
# above still holds). A request carrying `?v=` whose token equals the file's
# current stat token (asset_version.py) is content-versioned and served
# immutable; a mismatching token keeps no-cache.
_IMMUTABLE_PREFIX = "/static/shell-dist/assets/"
_VERSIONED_PREFIXES = ("/static/runtime.js", "/template-assets/", "/template-shared/")
# Marketplace asset bytes (routers/marketplace.py) are cached per catalog
# commit server-side and answered with their own `max-age=3600`, which this
# middleware keeps instead of overwriting with no-cache — the browse grid
# re-requests every preview on each visit otherwise.
_OWN_CACHE_PREFIX = "/api/marketplace/asset/"

def _net_begin(net, scope, path, headers):
    # Bookkeeping must never fail a request.
    try:
        client = scope.get("client")
        qs = scope.get("query_string", b"").decode("latin-1")
        return net.begin(scope["method"], path, qs, headers, client[0] if client else "")
    except Exception:  # noqa: BLE001
        logger.debug("netlog.begin failed", exc_info=True)
        return None


class NoCacheAndLog:
    """Pure-ASGI middleware: the app call log, the request ring, no-cache.

    Replaces an `@app.middleware("http")` function (BaseHTTPMiddleware), which
    cost an extra task + memory-stream pair per request and wrapped every body
    iterator. Behaviour is the same, but the response is observed by wrapping
    `send` instead of by holding a Response object.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # The app call log's single write point (calls.py, design §4.5).
        # begin() returns a record only for a request carrying runtime.js's
        # X-Fused-Page header — so the shell's own /api/fs/list, the
        # conditions probe, and every non-page caller are excluded by
        # construction rather than by an endpoint blocklist that would drift.
        # Route handlers enrich the same dict through
        # request.state.fused_call; only finish() writes.
        request = Request(scope)
        call = shell_calls.begin(request)
        scope.setdefault("state", {})["fused_call"] = call
        # App code changes between restarts and user files change on disk;
        # stale browser caches of shell/runtime JS cause confusing half-old
        # UIs. Also the browser request log (SPEC SV-3): one INFO line per
        # request with status + duration, so the log reconstructs the sequence
        # of calls a page made.
        path = scope["path"]
        method = scope["method"]
        logged = not path.startswith(_LOG_SKIP_PREFIXES)
        start = time.monotonic()
        # SPEC-quiet-notifications.md bug 2: every request gets a chance to
        # name who raised it, regardless of route. Read from the header only,
        # never the body (same spoof-proofing `X-Fused-Page` gets in
        # `server/routers/jobs.py`).
        raw_source = request.headers.get("x-fused-source")
        ambient_source = unquote(raw_source) if raw_source else ""
        # The request ring behind the Monitor's Network tab (netlog.py). A
        # request is "in flight" for as long as it holds one of the browser's
        # 6 connections, which is until the body ends.
        net = netlog.get()
        nrec = _net_begin(net, scope, path, request.headers)
        st = {"status": None, "server_ms": None, "sent": 0, "done": False}

        def _close_net(**kw):
            if st["done"]:
                return
            st["done"] = True
            if nrec is not None:
                net.finish(nrec, **kw)

        async def send_wrapper(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                status = message["status"]
                st["status"] = status
                dur = (time.monotonic() - start) * 1000
                st["server_ms"] = dur
                if logged:
                    logger.info("%s %s -> %s (%.0f ms)", method, path, status, dur)
                headers = MutableHeaders(scope=message)
                if call is not None:
                    shell_calls.finish(
                        call, status=status, elapsed_ms=dur,
                        content_length=headers.get("content-length"),
                    )
                self._cache_headers(headers, request, method, path, status)
                net.headers_sent(nrec, status, dur)
                # While a dashboard is open, tell the browser how long the
                # server took, so the page's own timing can split "queued in
                # the browser" from "slow server" (platform/lib/netTiming.ts,
                # static/runtime.js). `fr-watch` is the signal to flush;
                # nothing is sent when nobody watches.
                if path.startswith("/api/") and net.watched():
                    headers["Server-Timing"] = f"fr;dur={dur:.1f}, fr-watch"
            elif mtype == "http.response.body":
                st["sent"] += len(message.get("body", b""))
                await send(message)
                if not message.get("more_body", False):
                    _close_net(server_ms=st["server_ms"], nbytes=st["sent"], outcome="ok")
                return
            await send(message)

        try:
            with shell_jobs.ambient_source(ambient_source):
                await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            # The client went away mid-request — overwhelmingly a runPython
            # superseded by a newer call for the same .py (D114/RH-9) or a
            # closed tab. Recorded as its own outcome and kept out of every
            # latency statistic.
            if call is not None and st["status"] is None:
                shell_calls.finish(
                    call, status=None, elapsed_ms=(time.monotonic() - start) * 1000,
                    outcome="disconnected",
                )
            _close_net(outcome="disconnected")
            raise
        except Exception as exc:
            if isinstance(exc, ClientDisconnect):
                _close_net(
                    server_ms=st["server_ms"], nbytes=st["sent"], outcome="disconnected"
                )
                raise
            if st["status"] is None:
                if logged:
                    dur = (time.monotonic() - start) * 1000
                    logger.info("%s %s -> 500 (%.0f ms)", method, path, dur)
                # An unhandled exception escapes the app: @app.exception_handler
                # runs in ServerErrorMiddleware, OUTSIDE user middleware, so
                # this is the only place the record can be closed out. Mint the
                # correlation id here (we run before the handler) and stash it
                # so the handler can echo the same id into the 500 body.
                err_id = uuid.uuid4().hex[:12]
                scope["state"]["fused_err_id"] = err_id
                if call is not None:
                    shell_calls.finish(
                        call, status=500, elapsed_ms=(time.monotonic() - start) * 1000,
                        outcome="error", err_id=err_id,
                    )
                _close_net(status=500, outcome="error")
            else:
                _close_net(
                    server_ms=st["server_ms"], nbytes=st["sent"], outcome="error"
                )
            raise
        finally:
            # A streamed response whose client left can end the app call
            # quietly (starlette's disconnect listener cancels the stream and
            # returns) with the last body chunk never sent.
            if not st["done"]:
                _close_net(
                    server_ms=st["server_ms"], nbytes=st["sent"],
                    outcome="disconnected" if st["status"] is not None else "ok",
                )

    @staticmethod
    def _cache_headers(headers, request, method, path, status):
        # 304 too: a browser refreshes a cached entry's headers from a 304, so
        # an asset cached back when it was no-cache gets upgraded on its next
        # look.
        if path.startswith(_IMMUTABLE_PREFIX) and status in (200, 304):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif (status in (200, 304) and method in ("GET", "HEAD")
              and path.startswith(_VERSIONED_PREFIXES)
              and request.query_params.get("v") is not None
              and request.query_params.get("v") == asset_version(path)):
            headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif (path.startswith(_OWN_CACHE_PREFIX) and status == 200
              and "cache-control" in headers):
            pass
        else:
            headers["Cache-Control"] = "no-cache"


def get_start_dir(request: Request) -> str:
    return request.app.state.start_dir


def get_shell_path(request: Request) -> str:
    return request.app.state.shell_path
