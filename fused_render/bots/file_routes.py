"""`/api/botfile/*` — the `.fusedbot` install page's HTTP API (docs/bots.md §17, D1459).

Not under `/api/bots/`: `/api/bots/{bid}/{op}` would read "file" as a bot id. So these routes are outside
routes.OWNED_PREFIXES and `forward_middleware` never sees them; the one route that needs the bots owner (the
install POST, which builds a Bot) checks ownership itself and forwards the same way the middleware does,
with the same client, headers and loop guard. The reads (probe, icon) and the upload touch no Bot and are
answered by whichever process the page talks to.

botfile (and through it the bot layer) is imported lazily, like routes.py does, so the server imports this
router without Chrome/CDP code.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from fused_render import _platform
from fused_render.bots import registry
from fused_render.server.common import _error, _require_fused

router = APIRouter()

UPLOAD_MAX_BYTES = 1 << 30  # package.PACKAGE_MAX_BYTES: an upload is a whole package
# The install POST's JSON body ({path, apps: [folder], memory, request_id, bot_id}): a few hundred bytes in
# practice, so a fixed cap keeps an unbounded body off the event loop (read and json.loads both run there).
INSTALL_BODY_MAX_BYTES = 64 << 10
EXT = ".fusedbot"


def _botfile():
    from fused_render.bots import botfile
    return botfile


GITHUB_INSTALL_WAIT_S = 600  # added to the install budget per ticked github row (api_install)


def _github_rows(path: str, apps: list[str]) -> int:
    """How many of the ticked `apps` are github rows in the package at `path`; 0 when it cannot be read (the
    install itself then answers why)."""
    try:
        bf = _botfile()
        return sum(1 for r in bf._app_rows(bf._index(path))
                   if r["folder"] in apps and r["source"].get("kind") == "github")
    except Exception:  # noqa: BLE001 - only sizes a timeout
        return 0


def _abs(path: str) -> str | None:
    return path if path and os.path.isabs(path) else None


@router.get("/api/botfile/install")
def botfile_probe(path: str = Query(""), light: str = Query("")):
    """What installing the package at `path` would do (botfile.probe). `light=1` is a card peek: index and
    bot.json only. Unguarded like /api/appfile/install's GET: it stages the bundled apps under the workspace's
    `app/.install-*` (swept after a day) and installs nothing."""
    if not _abs(path):
        return _error("path must be an absolute .fusedbot file path")
    bf = _botfile()
    try:
        return bf.probe(path, light=light == "1")
    except (bf.BotFileError, ValueError) as e:
        return _error(str(e))


@router.get("/api/botfile/icon")
def botfile_icon(path: str = Query("")):
    """The package's `icon.svg` member; 404 without one (the page falls back to the bot's face). Served
    with a no-script CSP: an SVG opened directly runs in this origin."""
    if not _abs(path):
        return _error("path must be an absolute .fusedbot file path")
    bf = _botfile()
    try:
        raw = bf.icon(path)
    except bf.BotFileError as e:
        return _error(str(e))
    if raw is None:
        return _error("this bot package has no icon", 404)
    return Response(raw, media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache",
                             "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


async def _forward_to_owner(request: Request, body: bytes, read_timeout_s: float):
    """Answer the request from the bots owner when this process is not it, else None (serve it here).
    Mirrors routes.forward_middleware's rungs: start the scheduler on a machine's first bots request, elect
    when allowed, refuse a forward that landed on a non-owner (stale record) instead of bouncing it, and on
    a refused connection retry the election so the page's retry lands on the new owner."""
    from fused_render.bots import routes
    if not _platform.IS_WINDOWS and registry.needs_start():
        try:
            await asyncio.to_thread(registry.ensure_started)
        except Exception:  # noqa: BLE001 - serve the request regardless (as the middleware does)
            import logging
            logging.getLogger(__name__).exception("could not start the bots scheduler")
    try:
        await asyncio.to_thread(registry.require_owner)
        return None
    except registry.NotOwner as e:
        info = e.info
    if request.headers.get("x-fused-forwarded"):
        return _error("this Fused app does not run the bots and was forwarded to by mistake; try again", 503)
    origin = (info or {}).get("origin") or ""
    if not origin:
        return _error(str(registry.NotOwner(info)) + ("; update it" if info and not info.get("known") else
                                                     ". Open Fused Render (or Fused Bot) to run them."), 503)
    import httpx
    headers = {k: v for k, v in request.headers.items() if k.lower() not in routes._SKIP_REQUEST_HEADERS}
    headers["X-Fused-Forwarded"] = "1"
    client = routes._forward_client()
    try:
        # The shared client's 120 s read timeout is shorter than an install that waits on Community
        # downloads (tools.MARKET_INSTALL_WAIT_S per row): this one request gets its own bound.
        req = client.build_request(request.method, origin + request.url.path, content=body, headers=headers,
                                   timeout=httpx.Timeout(read_timeout_s, connect=2.0))
        resp = await client.send(req)
    except httpx.HTTPError:
        registry.retry_election()
        return _error("the Fused app that ran the bots just quit; retrying", 503)
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in routes._SKIP_RESPONSE_HEADERS}
    return Response(resp.content, status_code=resp.status_code, headers=out_headers)


async def _read_capped(request: Request, cap: int) -> bytes | JSONResponse | None:
    """The whole body when it is at most `cap` bytes, else None (the caller answers 413); a bad Content-Length
    is the error response itself. Same two checks as botfile_upload: the declared length before a byte is
    read, the running count while streaming (a chunked body declares none)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > cap:
                return None
        except ValueError:
            return _error("bad Content-Length")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > cap:
            return None
    return bytes(buf)


def _installed(row) -> bool:
    """An install answer's row put an app on disk: ok, or `settings` (installed, the bot not updated)."""
    return isinstance(row, dict) and (row.get("ok") is True or row.get("state") == "settings")


@router.post("/api/botfile/install")
async def botfile_install(request: Request, x_fused: str | None = Header(default=None)):
    """Install the package: body `{path, apps: [folder], memory: bool, bot_id?, request_id?}` -> `{ok, bot_id,
    apps: [{folder, ok, state, error?, path?}], warnings: [str]}` (botfile.install; state "settings" = the app
    installed but the bot was not updated, retried like a failed row). `bot_id` is re-install mode (the
    page's per-row Retry: no new bot); `request_id` makes the POST idempotent. Runs in the bots owner;
    forwarded there from any other process with the body unchanged, so the owner is the one that dedups."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    raw = await _read_capped(request, INSTALL_BODY_MAX_BYTES)
    if raw is None:
        return _error(f"the request body is larger than {INSTALL_BODY_MAX_BYTES >> 10} KB", 413)
    if isinstance(raw, JSONResponse):
        return raw
    try:
        import json
        body = json.loads(raw or b"{}")
    except ValueError:
        return _error("body must be JSON")
    if not isinstance(body, dict):
        return _error("body must be an object")
    path = _abs(str(body.get("path") or ""))
    if not path:
        return _error("path must be an absolute .fusedbot file path")
    apps = body.get("apps") if isinstance(body.get("apps"), list) else []
    bot_id = body.get("bot_id") or None
    request_id = body.get("request_id") or None
    if bot_id is not None and not (isinstance(bot_id, str) and re.fullmatch(r"[0-9a-f]{1,32}", bot_id)):
        return _error("bot_id must be a bot id")
    from fused_render.bots import tools
    from fused_render.bots.routes import FORWARD_TIMEOUT_S
    apps = [a for a in apps if isinstance(a, str)]
    # A github row clones: each appfile_git._git call may take its TIMEOUT_S (300 s) and a clone is retried over
    # ssh, so it gets GITHUB_INSTALL_WAIT_S on top of the per-row marketplace wait. One bounded index read.
    n_gh = await asyncio.to_thread(_github_rows, path, apps)
    budget = FORWARD_TIMEOUT_S + tools.MARKET_INSTALL_WAIT_S * max(1, len(apps)) + GITHUB_INSTALL_WAIT_S * n_gh
    fwd = await _forward_to_owner(request, raw, budget)
    if fwd is not None:
        if fwd.status_code == 200:
            # The owner installed; THIS process's paged hub snapshot must not lag the folders it added.
            try:
                import json as _json
                if any(_installed(r) for r in (_json.loads(fwd.body or b"{}").get("apps") or [])):
                    from fused_render.server.routers import apps as apps_router
                    apps_router._reset_snapshot()
            except Exception:  # noqa: BLE001 - the snapshot heals on its own TTL
                pass
        return fwd
    try:
        # Served here (this process owns; a forward was gated by the owner's own copy of this check): an install
        # writes a bot folder and its apps, and a home without the workspace claim is read-only for bot state.
        await asyncio.to_thread(registry.require_workspace)
    except registry.WorkspaceBusy as e:
        return _error(str(e), 409)
    except registry.NotOwner as e:  # lost the flock since the forward check
        return _error(str(e), 503)
    bf = _botfile()
    if request_id is not None and not (isinstance(request_id, str) and bf.REQUEST_ID_RE.match(request_id)):
        return _error("request_id must be 1-64 letters, digits, '-' or '_'")
    try:
        out = await asyncio.to_thread(bf.install, path, apps=apps,
                                      memory=body.get("memory") is True, bot_id=bot_id, request_id=request_id,
                                      wait_s=budget)
    except registry.NotOwner as e:  # lost the flock between the check and the build
        return _error(str(e), 503)
    except bf.NoSuchBot as e:
        return _error(str(e), 404)
    except (bf.BotFileError, ValueError, RuntimeError) as e:
        return _error(str(e))
    if any(_installed(r) for r in out.get("apps") or []):
        # The paged hub's snapshot must not lag the folders just installed (routers/appfile.py does the same).
        # Never a non-200 from here: the bot exists, and the page retries a failure by re-POSTing.
        try:
            from fused_render.server.routers import apps as apps_router
            apps_router._reset_snapshot()
        except Exception:  # noqa: BLE001 - the snapshot heals on its own TTL
            import logging
            logging.getLogger(__name__).debug("apps snapshot reset failed", exc_info=True)
    return out


def _safe_name(name: str) -> str:
    """`[A-Za-z0-9._ -]` only, one `.fusedbot` suffix, never empty or a dot-name."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    if base.lower().endswith(EXT):
        base = base[: -len(EXT)]
    base = re.sub(r"[^A-Za-z0-9._ -]", "", base).strip(" .")[:120] or "bot"
    return base + EXT


def _too_big() -> JSONResponse:
    return _error(f"the file is larger than {UPLOAD_MAX_BYTES >> 20} MB", 413)


@router.post("/api/botfile/upload")
async def botfile_upload(request: Request, name: str = Query(""), x_fused: str | None = Header(default=None)):
    """A `.fusedbot` dropped on the Bots page: the request body IS the file (raw bytes, `?name=` its file
    name), streamed to the managed downloads dir (appfetch.downloads_dir(), where deep-link fetches land)
    under a free name -> `{ok, path}`; the page then opens that path's install view. Refused unless the bytes
    are a bot package. Content-Length is checked against the cap before a byte is read, and the count is
    enforced while streaming too (a chunked body declares none)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > UPLOAD_MAX_BYTES:
                return _too_big()
        except ValueError:
            return _error("bad Content-Length")
    from fused_render import appfetch, appfile, appfile_container
    from fused_render.server.routers.appfile import _unique_export_path
    dest_dir = appfetch.downloads_dir()
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".upload-", suffix=".tmp")
    dest = None
    try:
        got = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                got += len(chunk)
                if got > UPLOAD_MAX_BYTES:
                    return _too_big()
                await asyncio.to_thread(f.write, chunk)  # a 1 GiB body must not block the event loop
        if not got:
            return _error("the request body must be the .fusedbot file")
        try:  # the reader's own sentence: "this is a fused app file…", the version line, or the generic refusal
            idx = await asyncio.to_thread(appfile_container.read_index, tmp, max_entries=appfile.MAX_OPEN_ENTRIES,
                                          max_entry_bytes=1 << 30, max_total_bytes=1 << 30, kind="bot")
        except appfile_container.ContainerError as e:
            return _error(str(e))
        # The install page's probe runs the same read-only checks (botfile._validate: text budget, bot.json, the
        # app list, every playbook and memory member's size + hash): refuse here with the probe's own sentence
        # rather than save a file the next page cannot open. Upload only (`bundled`): every apps/*.fused member is
        # stream-verified too, a cost the probe must not pay on every open. The temp file is unlinked in
        # `finally`; nothing is placed yet.
        bf = _botfile()
        try:
            await asyncio.to_thread(lambda: bf._validate(tmp, idx, bundled=True))
        except bf.BotFileError as e:
            return _error(str(e))
        # No-clobber placement: link() fails on an existing name, so a racing upload of the same name takes
        # the next free one instead of being overwritten (a check-then-replace would race).
        want = _safe_name(name)
        for _ in range(50):
            candidate = _unique_export_path(dest_dir, want)
            try:
                os.link(tmp, candidate)
            except FileExistsError:
                continue
            dest = candidate
            break
        if dest is None:
            return _error("could not find a free file name in the downloads folder")
    except OSError as e:
        return _error(f"could not save the file: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return JSONResponse({"ok": True, "path": dest})
