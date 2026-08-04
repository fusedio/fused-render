import asyncio
import email.utils
import itertools
import json
import mimetypes
import os
import subprocess
import sys
import time

from fastapi import APIRouter, Body, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from fused_render.server.common import _error, _require_fused, logger
from fused_render.server.gitignore import _git_ignored, _is_repo_root
# The tuning knobs (`_STAT_TTL_S`, `_CONDITIONS_TTL_S`, the `WALK_*`/`LIST_*`
# caps) are read through their DEFINING module below — `_server_mount._STAT_TTL_S`
# and friends — never re-bound here by `from … import`. Each of those modules says
# in a comment that the knob "is a module attribute so tests can override it", and
# a by-value copy in this module makes that promise a lie for every read on this
# side of the split: the override silently does nothing, which is how a
# cap/TTL test passes while exercising the production value. Importing the module
# also means the WRONG seam (`setattr(fs_read, "WALK_MAX_ENTRIES", …)`) raises
# AttributeError instead of quietly doing nothing. D178 is the same bug
# (`_STAT_CACHE_GEN`) caught during this split, so it is a known pattern.
#
# CACHES and SENTINELS stay by-value on purpose: a dict is the same object either
# way, and `_WALK_TRUNCATED` is compared with `is`, so a re-bound copy would break
# identity rather than merely go stale.
from fused_render.server import mount as _server_mount
from fused_render.server.mount import (
    _STAT_CACHE,
    _fs_stat,
    _mount_probe,
    _stat_or_none,
)
from fused_render.server.proxy import (
    _harden_raw,
    _proxy_raw,
    _proxy_raw_bearer,
    _proxy_raw_pooled,
)
from fused_render.server import templates as _server_templates
from fused_render.server.templates import (
    _CONDITIONS_CACHE,
    _conditions_payload,
    _prefs_mtime,
)
from fused_render.server import walk as _server_walk
from fused_render.server.walk import (
    _WALK_TRUNCATED,
    _list_direct,
    _list_response,
    _mount_list_error_response,
    _mount_list_item,
    _sort_entries,
    _walk_bfs,
    _win_protected,
)
from fused_render.server.watch import _WATCH_REGISTRY
from fused_render.shell import mounts as shell_mounts
from fused_render.shell import prefetch as shell_prefetch

router = APIRouter()



# GET /api/templates/registry moved to templates_api.py (extended §2.2
# shape) and registered via templates_router above.

@router.get("/api/fs/stat")
def api_fs_stat(path: str):
    # Short check-on-read TTL cache (mirrors api_fs_conditions) to avoid
    # re-paying the ~1.6s cold parent-prefix LIST that a mount stat costs
    # (see _STAT_CACHE). Only MOUNT-backed paths are cached: a local stat is
    # a cheap kernel call, and a local file can be mutated out-of-band (git,
    # another editor) with no hook for us to invalidate — caching those
    # would risk handing back a stale mtime the editor's optimistic lock
    # trusts, for no latency win. Mount paths are mutated only through this
    # server's fs/write|rename|copy|delete|mkdir handlers, which call
    # _invalidate_stat_cache, so their only staleness is the same bounded
    # external-change window the conditions cache already accepts. Only
    # success payloads (plain dicts) are stored; _fs_stat's 404/503 branches
    # return _error -> JSONResponse and are always recomputed.
    from fused_render.shell.mounts import is_mount_backed

    cached = _STAT_CACHE.get(path)
    if cached is not None and time.monotonic() - cached[0] < _server_mount._STAT_TTL_S:
        return cached[1]
    # Snapshot the invalidation generation BEFORE the (slow) stat. _fs_stat
    # releases the GIL on its cold mount LIST, so a concurrent mutation can
    # complete _invalidate_stat_cache — popping this key AND bumping the
    # generation — while we're in flight. Caching unconditionally here would
    # write our now-stale payload back, undoing that invalidation and
    # handing the editor's post-write optimistic-lock re-stat pre-mutation
    # metadata (a real clobber bug). So only cache if the generation is
    # UNCHANGED: the bump happens-before this check for any invalidation
    # that finished before our write, closing the TOCTOU window. If it
    # moved, return the fresh result WITHOUT caching it.
    gen = _server_mount._STAT_CACHE_GEN
    result = _fs_stat(path)
    if isinstance(result, dict) and is_mount_backed(path) and _server_mount._STAT_CACHE_GEN == gen:
        _STAT_CACHE[path] = (time.monotonic(), result)
    return result

@router.get("/api/fs/conditions")
def api_fs_conditions(path: str):
    # Deferred condition.py evaluation (SPEC CT-12): stat marks gated
    # templates `conditional`; this resolves them while the client already
    # renders the first unconditional template.
    #
    # This does NOT gate first paint, on either side. Server-side it's a
    # sync `def`, so FastAPI runs it in the threadpool — its cold os.stat
    # over the mount (~1.6s) never blocks the event loop or other requests.
    # Client-side the frontend fetches it from a background useEffect
    # (Preview.tsx `useConditions`) and renders every unconditional
    # template while the verdict is still `null` — the gated ones just show
    # a spinner until it lands. So no change on the render path is needed.
    #
    # Re-navigating to the same directory would otherwise re-pay the full
    # gate-evaluation cost, so a short check-on-read TTL cache serves a
    # recent verdict. Only success payloads (plain dicts) are cached; error
    # responses (_error -> JSONResponse) are always recomputed.
    pm = _prefs_mtime()
    cached = _CONDITIONS_CACHE.get(path)
    if (cached is not None
            and time.monotonic() - cached[0] < _server_templates._CONDITIONS_TTL_S
            and cached[1] == pm):
        return cached[2]
    result = _conditions_payload(path)
    if isinstance(result, dict):
        _CONDITIONS_CACHE[path] = (time.monotonic(), pm, result)
    return result

@router.get("/api/fs/list")
def api_fs_list(path: str, cursor: str | None = None):
    # A mount-backed listing must never issue kernel filesystem I/O: both
    # os.path.isdir and os.scandir below are kernel READDIR/GETATTR calls,
    # and on a flat remote prefix with millions of keys rclone's VFS must
    # enumerate the WHOLE directory before the kernel gets its first entry
    # — minutes of blocking that trips the macOS NFS deadman and kills the
    # mount (the mur-sst incident). Route off the kernel instead, so a
    # too-huge directory is a failed/partial request, never a wedged mount.
    #
    # Every response carries `truncated` (the listing is a partial page) and
    # `cursor` (an opaque resume token, non-None only on the direct S3/GCS
    # route — rclone and a local scandir can't resume). Fallback ladder for a
    # mount path: direct -> rc -> 503.
    if shell_mounts.is_mounts_root(path):
        # The mounts container is a LOCAL directory whose children are the
        # mountpoints. is_mount_backed is true for it (so no kernel readdir
        # touches it), yet it sits under no single mount record, so the
        # rc/S3 routes below have nothing to list and 503 ("cannot list
        # directory"). Enumerate the mount records directly instead — the
        # authoritative mount list, with zero kernel or remote I/O and no
        # sidecar files (mounts.json, per-mount *.json) leaking in.
        entries = _sort_entries([
            {"name": m["name"], "is_dir": True, "size": None,
             "mtime": None, "ignored": False}
            for m in shell_mounts.list_mounts() if m.get("name")
        ])
        return _list_response(path, entries, False, None)
    if shell_mounts.is_mount_backed(path):
        # Direct fast path: for anonymous plain AWS S3 / anonymous GCS — the
        # backends that dominate our mounts — page the store's own listing
        # API (rclone can't paginate its listing at any layer, so a
        # million-key prefix times out on the rc route). On any page failure,
        # log and fall through to the rc route below.
        if shell_mounts.direct_list_capable(path):
            try:
                entries, next_token = _list_direct(path, cursor)
            except shell_mounts.DirectListError:
                # A cursored request can't fall through to rc: rc re-serves
                # page 1 with cursor=None (the frontend dedupes it to zero
                # rows and pagination dies) or 503s on a huge dir. Return a
                # retryable error so the client resumes the SAME cursor.
                if cursor is not None:
                    return _error(
                        "listing continuation failed — retry", status=503)
                logger.warning("direct listing of %s failed; falling "
                               "back to rc", path, exc_info=True)
            else:
                return _list_response(path, entries,
                                      next_token is not None, next_token)
        try:
            listed = shell_mounts.rc_list_dir(path)
        except shell_mounts.RcListTimeout:
            return _error(
                f"directory listing timed out — too many entries to list "
                f"({path})", status=503)
        except shell_mounts.RcListUnavailable:
            # rcd down or path under no known mount: the mount can't be
            # trusted. Prefer the specific broken-mount wording when we have
            # it (it tells the user to reconnect from the Mounts page).
            broken = shell_mounts.broken_mount_error(path)
            return _error(broken or f"cannot list directory {path}",
                          status=503)
        except shell_mounts.RcListError:
            # rcd answered but rejected the listing. Two causes look alike
            # here: a genuinely broken mount (dead/stale/disconnected — the
            # empty-mountpoint bug this endpoint already guards), and a path
            # that is simply a file. broken_mount_error distinguishes them:
            # a message means the mount is unhealthy (503, reconnect); no
            # message means the mount is fine and the path just isn't a
            # directory (400, the mount-safe stand-in for os.path.isdir).
            broken = shell_mounts.broken_mount_error(path)
            if broken:
                return _error(broken, status=503)
            return _error(f"not a directory: {path}", status=400)
        # rcd answered but with nothing: a stale/dead mount (rcd alive, the
        # kernel mount gone) lists empty, and pre-Phase-1 an empty listing
        # consulted broken_mount_error before it was trusted. Restore that
        # so a dead mount 503s ("reconnect") instead of rendering as an
        # ordinary empty folder.
        if not listed:
            broken = shell_mounts.broken_mount_error(path)
            if broken:
                return _error(broken, status=503)
        # rclone can't resume a listing, so cap and flag rather than page:
        # the client sees the first LIST_MAX_ENTRIES entries, `truncated`
        # tells it there are more, and `cursor` stays None (no Load more).
        # Sort the WHOLE listing THEN cap, so the capped page is the true
        # sorted-first N rather than rclone's arbitrary order sliced. Skip
        # any entry missing a Name (a malformed rc entry must not 500).
        entries = _sort_entries(
            [_mount_list_item(de) for de in listed if de.get("Name")])
        truncated = len(entries) > _server_walk.LIST_MAX_ENTRIES
        return _list_response(path, entries[:_server_walk.LIST_MAX_ENTRIES], truncated, None)
    if not os.path.isdir(path):
        return _error(f"not a directory: {path}", status=400)
    entries = []
    # scandir over listdir+per-entry stat/isdir: the readdir already carries
    # each entry's type, so is_dir() is free and stat() is a single call —
    # the old loop did two stats per entry (os.stat + os.path.isdir's own
    # stat), i.e. 2N remote round-trips under a mount. Both follow symlinks,
    # matching the previous os.stat/os.path.isdir behavior.
    #
    # islice caps consumption at LIST_MAX_ENTRIES: a directory with a
    # million entries would otherwise build a million-entry JSON response.
    # Read one past the cap to detect overflow, then trim.
    try:
        with os.scandir(path) as it:
            dents = list(itertools.islice(it, _server_walk.LIST_MAX_ENTRIES + 1))
    except OSError as e:
        broken = shell_mounts.broken_mount_error(path)
        if broken:
            return _error(broken, status=503)
        return _error(f"cannot read directory {path}: {e}", status=400)
    truncated = len(dents) > _server_walk.LIST_MAX_ENTRIES
    if truncated:
        dents = dents[:_server_walk.LIST_MAX_ENTRIES]
    if not dents:
        # A dead mount leaves a plain empty dir (or a wedged NFS mount
        # serving nothing) at the mountpoint — an empty listing under
        # mounts/ is only trustworthy while the mount is healthy.
        broken = shell_mounts.broken_mount_error(path)
        if broken:
            return _error(broken, status=503)
    for de in dents:
        try:
            if _win_protected(de):
                # Windows hidden+system entries (e.g. the deny-ACL
                # Documents\My Videos compat junction) — Explorer hides
                # these, and scandir'ing into them raises WinError 5.
                continue
            st = de.stat()
            is_dir = de.is_dir()
        except OSError:
            continue  # unreadable entries skipped silently
        entries.append(
            {
                "name": de.name,
                "is_dir": is_dir,
                "size": None if is_dir else st.st_size,
                "mtime": st.st_mtime,
            }
        )
    ignored = _git_ignored(path, [e["name"] for e in entries])
    for e in entries:
        e["ignored"] = e["name"] in ignored
    # _sort_entries: dirs first, case-insensitive by name, exact name as a
    # deterministic tiebreak (same order for all three list routes).
    return _list_response(path, _sort_entries(entries), truncated, None)

# Poll request.is_disconnected() once every this many walked entries. The
# explorer search fires a fresh /api/fs/walk on each keystroke and abandons
# the previous one; without this the superseded walk would keep enumerating
# (over a mount, keep issuing remote LISTs) until it hit a cap. Checked
# between entries only — a single blocking directory listing can't be
# interrupted mid-call (same best-effort caveat as WALK_FLUSH_INTERVAL_S).
WALK_DISCONNECT_CHECK_EVERY = 64

@router.get("/api/fs/walk")
async def api_fs_walk(request: Request, path: str, hidden: str = "0", stream: str = "0"):
    # Recursive listing of a directory subtree, for the explorer search
    # (flat, ranked client-side). Walks BREADTH-FIRST so shallow entries —
    # the ones a search almost always targets — are all emitted before any
    # deep subtree can exhaust the WALK_MAX_ENTRIES cap (the old
    # depth-first walk let one big sibling starve every later one). Prunes
    # WALK_IGNORE_DIRS entirely, prunes gitignored entries inside git
    # repositories (see _walk_bfs — which is why walk entries carry no
    # `ignored` dimming flag: nothing ignored survives to be dimmed),
    # emits WALK_LEAF_DIR_SUFFIXES packages without descending, never
    # follows symlinks, and skips unreadable entries silently (matches
    # /api/fs/list). `rel` is posix-relative to `path`.
    #
    # The walk is bounded on three axes so a search-as-you-type over a big
    # (esp. mount) root can't kick off an unbounded enumeration: entry count
    # and DEPTH (both enforced inside _walk_bfs — see its caps), and the HTTP
    # request lifetime (if the client abandons this keystroke we stop pulling
    # from the walk; see WALK_DISCONNECT_CHECK_EVERY). The blocking walk runs
    # in a threadpool (iterate_in_threadpool) so this async route can poll for
    # disconnect without stalling the event loop.
    #
    # `hidden=1` (explicit intent: the user typed a dot-leading query)
    # includes dot-files and descends into dot-dirs. WALK_IGNORE_DIRS and
    # gitignore pruning apply regardless — those trees are noise, not
    # "hidden data", and letting hidden=1 descend into .git/node_modules
    # would flood the results with machine-managed junk.
    #
    # `stream=1` returns NDJSON: zero or more `{"entries": [...]}` batch
    # lines (WALK_BATCH_SIZE each) followed by exactly one terminal
    # `{"done": true, "truncated": bool, "total": n}` line. The client
    # scores batches as they arrive, so first results paint while the walk
    # is still running. Without `stream=1` the response is the original
    # single-JSON shape, unchanged for old clients.
    include_hidden = hidden == "1"
    # Under a mount, os.path.isdir is itself a kernel GETATTR on the mount
    # we route around; _walk_bfs lists mount dirs via the rc API and simply
    # yields nothing for a non-directory root, so the guard is local-only.
    under_mount = shell_mounts.is_mount_backed(path)
    if not under_mount and not os.path.isdir(path):
        return _error(f"not a directory: {path}", status=400)
    # Remote-mount clamp: under a mount mountpoint every directory is a
    # remote LIST round-trip, so both caps drop to their _REMOTE values (see
    # the constants' comments). The caps are enforced INSIDE _walk_bfs so the
    # walk terminates early instead of the consumer draining a huge tree.
    max_entries = (_server_walk.WALK_MAX_ENTRIES_REMOTE if under_mount
                   else _server_walk.WALK_MAX_ENTRIES)
    max_depth = (_server_walk.WALK_MAX_DEPTH_REMOTE if under_mount
                 else _server_walk.WALK_MAX_DEPTH_LOCAL)
    walker = _walk_bfs(path, include_hidden, max_entries=max_entries, max_depth=max_depth)

    # Force the ROOT listing eagerly (the first next() runs it) so a dead
    # mount / down rcd / timed-out or not-a-directory root fails with
    # fs/list's status codes instead of streaming a 200-empty body. Only the
    # ROOT raises out of _walk_bfs; deeper per-dir failures skip-and-continue
    # (feeding the truncated flag via the _WALK_TRUNCATED sentinel). Run in a
    # threadpool: the root listing is blocking (a remote LIST under a mount).
    def _pull_first():
        try:
            return next(walker), True
        except StopIteration:
            return None, False

    try:
        first, have_first = await run_in_threadpool(_pull_first)
    except shell_mounts.RcListError as e:
        return _mount_list_error_response(path, e)

    def _items():
        if have_first:
            yield first
        yield from walker

    if stream != "1":
        entries = []
        truncated = False
        seen = 0
        async for entry in iterate_in_threadpool(_items()):
            seen += 1
            if seen % WALK_DISCONNECT_CHECK_EVERY == 0 and await request.is_disconnected():
                break  # client abandoned this keystroke — stop the walk
            if entry is _WALK_TRUNCATED:
                truncated = True  # a dir was cut / skipped (partial coverage)
                continue
            entries.append(entry)
            if len(entries) >= max_entries:
                truncated = True
                break
        return {"path": path, "entries": entries, "truncated": truncated}

    async def ndjson():
        batch = []
        total = 0
        truncated = False
        seen = 0
        last_flush = time.monotonic()
        async for entry in iterate_in_threadpool(_items()):
            seen += 1
            if seen % WALK_DISCONNECT_CHECK_EVERY == 0 and await request.is_disconnected():
                return  # client abandoned this keystroke — stop the walk
            if entry is _WALK_TRUNCATED:
                truncated = True
                continue
            batch.append(entry)
            total += 1
            now = time.monotonic()
            if (len(batch) >= _server_walk.WALK_BATCH_SIZE
                    or now - last_flush >= _server_walk.WALK_FLUSH_INTERVAL_S):
                yield json.dumps({"entries": batch}) + "\n"
                batch = []
                last_flush = now
            if total >= max_entries:
                truncated = True
                break
        if batch:
            yield json.dumps({"entries": batch}) + "\n"
        yield json.dumps({"done": True, "truncated": truncated, "total": total}) + "\n"

    return StreamingResponse(ndjson(), media_type="application/x-ndjson")

@router.api_route("/api/fs/raw", methods=["GET", "HEAD"])
async def api_fs_raw(path: str, request: Request, base: str | None = None,
                     pooled: str | None = None):
    # Every response this route can produce goes through _harden_raw: the
    # read has four exits (HEAD, the 307, the proxied mount read, and the
    # local file) and three of them can put a scriptable content-type on
    # this origin, so the hardening lives at the single choke point rather
    # than being repeated — and cannot be missed by a new exit.
    return _harden_raw(
        await _api_fs_raw_read(path, request, base, pooled), request)

async def _api_fs_raw_read(path: str, request: Request, base: str | None,
                           pooled: str | None):
    # Page-relative resolution (SPEC RH-1): a *relative* `path` is resolved against
    # the directory of `base` — the page's own absolute path, sent by the runtime's
    # fused.rawUrl(), the same contract /api/run uses via `html` (see the resolve at
    # the top of api_run). An absolute `path` is used verbatim (base ignored). This is
    # what lets one `fused.rawUrl("data/x.json")` call resolve locally here AND, when
    # the page is hosted, against the bundle's _asset route by the same key.
    if base and not os.path.isabs(path):
        path = os.path.normpath(os.path.join(os.path.dirname(base), path))
    # Mount-backed file with a live HTTP serve: proxy the bytes from
    # rclone instead of reading through the kernel mount. Concurrent
    # ranged reads (duckdb's httpfs) through the NFS mount stall its 1s
    # RPC timeout and get the whole mount dropped; the same reads proxied
    # over HTTP are merely slow. Explicit HEAD support matters: httpfs
    # HEADs for the length first, and Starlette's implicit HEAD-on-GET
    # would run the full upstream GET just to drop the body.
    #
    # No stat() before a serve-backed GET: on a mount that's a VFS
    # getattr — a full remote round trip (~1s cold), paid serially
    # before the read even starts, per never-listed object. The serve
    # and the store both 404 a missing object themselves (_proxy_raw
    # passes error statuses through), so existence falls out of the
    # read. Only HEAD (answered from st_size) and the local-file
    # fallback below still stat.
    upstream = await asyncio.to_thread(shell_mounts.serve_url_for, path)
    if upstream is not None:
        # Every remote read flows through here, so this is where the
        # shell learns a mounted file is in use: kick off (or just
        # touch) its background whole-file prefetch. Cheap no-op after
        # the first call; templates stay mount-agnostic (prefetch.py).
        shell_prefetch.schedule(path, upstream)
        # HEAD answered from a VFS getattr rather than proxied: ranged
        # clients (duckdb httpfs, fsspec/zarr, geotiff) probe the length
        # before reading, and proxying that probe is a full remote round
        # trip for headers the getattr already knows. The serve reads
        # the same rclone remote, so the sizes agree. In a thread — a
        # cold getattr would otherwise stall the event loop.
        if request.method == "HEAD":
            if shell_mounts.is_mount_backed(path):
                # A missing-sidecar HEAD (.zmetadata, .ovr) is exactly the
                # cold-negative that a kernel os.stat would turn into a
                # full-prefix enumeration and a wedged mount — answer it
                # through the rclone rcd instead. Confirmed-missing/
                # non-regular -> 404; indeterminate (rcd down/timeout) ->
                # 503, never "missing".
                try:
                    pr = await asyncio.to_thread(_mount_probe, path)
                except (shell_mounts.RcListUnavailable,
                        shell_mounts.RcListTimeout) as e:
                    return _mount_list_error_response(os.path.dirname(path), e)
                if not pr.exists or pr.is_dir:
                    return _error(f"no such file: {path}", status=404)
                # rclone reports Size:-1 for an object of unknown length;
                # `-1 or 0` is -1, which is an invalid content-length. Clamp
                # a missing/negative size to 0 (keep the mtime fallback).
                size = pr.size if pr.size is not None and pr.size >= 0 else 0
                mtime = pr.mtime or 0.0
            else:
                st = await asyncio.to_thread(_stat_or_none, path)
                if st is None:
                    return _error(f"no such file: {path}", status=404)
                size, mtime = st.st_size, st.st_mtime
            media_type, _ = mimetypes.guess_type(path)
            return Response(status_code=200, headers={
                "content-length": str(size),
                "content-type": media_type or "application/octet-stream",
                "accept-ranges": "bytes",
                "last-modified": email.utils.formatdate(mtime, usegmt=True),
            })
        # Cold reads go straight to the store: the serve's VFS layer
        # serializes concurrent uncached range reads of one file (an
        # analytical scan pays ~0.25s per seek) and its per-file open
        # ceremony dwarfs a small metadata fetch (zarr.json), while the
        # store answers the same GETs in parallel. Once the prefetch
        # has landed the whole file in the serve cache, the serve
        # replays ranges from local disk and wins again. A 307 rather
        # than a proxied fetch: the client (duckdb httpfs, fsspec)
        # re-issues each GET against the store itself with its own
        # pooled parallel connections — proxying here paid a fresh TLS
        # handshake per range read (measured 2x on the point-read
        # phase) and streamed every byte through this process twice.
        # Whole-file GETs redirect too (zarr stores read many tiny
        # metadata files whole; schedule() above warms the serve cache
        # regardless). GET only: presigned links are minted for GET (a
        # HEAD fails their signature). Native clients only: a browser
        # fetch would follow the redirect cross-origin and die on CORS
        # — browsers always send Sec-Fetch-Mode, duckdb's httpfs never
        # does, so its absence is the gate.
        if ("sec-fetch-mode" not in request.headers
                and not shell_prefetch.is_done(path)):
            direct = await asyncio.to_thread(
                shell_mounts.upstream_url_for, path)
            if direct:
                # OPT-IN pooled proxy (TASK F): the pyramid/geotiff workers
                # read this file with a plain per-block urllib GET and would
                # otherwise re-follow the 307 to the store on EVERY ~64KB
                # block — a fresh TLS handshake + redirect round trip per
                # read, serial, multi-second cold. When they set &pooled=1
                # we stream the same signed URL back through a shared
                # keep-alive httpx pool (sockets reused across range reads).
                # duckdb/parquet never set the flag, so they still get the
                # 307 and their own pooled parallel connections — no
                # regression. A pooled fetch that can't reach the store
                # returns None and falls through to the 307.
                if pooled:
                    resp = await _proxy_raw_pooled(
                        request.app.state.pooled_client, direct, request)
                    if resp is not None:
                        return resp
                return RedirectResponse(direct, status_code=307)
            # No 307-able URL: a token-only private GCS remote can't hand
            # the client a signed link (the bearer token must never appear
            # in a URL), so proxy the bytes through the pooled client with
            # the Authorization header attached out-of-band — regardless of
            # the &pooled flag, there is nothing to redirect to. A stale
            # token (401/403) self-heals via one re-resolve + retry inside
            # _proxy_raw_bearer; a still-denied or unreachable read returns
            # None and falls through to the serve.
            resp = await _proxy_raw_bearer(request, path)
            if resp is not None:
                return resp
        # Not redirected (browser, warm read, or no direct URL): proxy the
        # bytes. Guard non-files here — a directory proxied through rclone
        # serve comes back as a 200 HTML listing, so resolve shape before
        # serving. But NOT with a kernel os.stat on a mount-backed path: this
        # branch is reached once the prefetch has landed (is_done) or for a
        # browser read, and a cold GETATTR on a never-listed object forces
        # rclone to enumerate the whole parent prefix (~28s on a 44k-entry
        # dir), past the NFS deadman -> the mount is dropped. Answer
        # existence/shape through the rcd (_mount_probe) like the HEAD branch
        # above; only a local path stats the kernel.
        if shell_mounts.is_mount_backed(path):
            try:
                pr = await asyncio.to_thread(_mount_probe, path)
            except (shell_mounts.RcListUnavailable,
                    shell_mounts.RcListTimeout) as e:
                return _mount_list_error_response(os.path.dirname(path), e)
            if not pr.exists or pr.is_dir:
                return _error(f"no such file: {path}", status=404)
        else:
            st = await asyncio.to_thread(_stat_or_none, path)
            if st is None:
                return _error(f"no such file: {path}", status=404)
        resp = await asyncio.to_thread(_proxy_raw, upstream, request)
        if resp is not None:
            return resp  # upstream unreachable -> plain file read below
    # Reached when serve_url_for returned None (no live serve) or the
    # proxied read failed. For a mount-backed path that means the rclone
    # serve died or is respawning: reading through the kernel mount here is
    # the wedge this whole module exists to avoid, so refuse with 503 rather
    # than fall back to a local file read.
    if shell_mounts.is_mount_backed(path):
        return _error("mount serve unavailable", status=503)
    st = await asyncio.to_thread(_stat_or_none, path)
    if st is None:
        return _error(f"no such file: {path}", status=404)
    media_type, _ = mimetypes.guess_type(path)
    return FileResponse(path, media_type=media_type or "application/octet-stream")

@router.websocket("/api/fs/events")
async def api_fs_events(ws: WebSocket):
    # File-change feed (SPEC §13.2), WebSocket not SSE (D74): every rendered
    # pane holds one of these open for the lifetime of the page, and SSE
    # rides ordinary HTTP/1.1 — Chrome caps those at 6 per origin, so a
    # 6-pane panel pinned every socket and all later fetches (/api/run!)
    # queued browser-side forever. WebSockets live in a separate, much
    # larger connection pool. Messages are JSON: {path, mtime} on change,
    # {keepalive: true} every 15 s (WF-3).
    #
    # Stat mechanics live in the module-level _WATCH_REGISTRY, NOT here:
    # every socket watching a given path shares ONE ticker (so a panel of
    # panes previewing the same mounted file makes one stat per interval,
    # not one per pane), stats run off the event loop with a hard timeout
    # so a hung NFS stat can't freeze the server, mount-backed paths poll
    # at 5s via the rclone rc API instead of the kernel, and a stat already
    # in flight is never stacked on. This all exists because a stat storm
    # on a slow S3-backed NFS mount killed the mount — see the registry's
    # header comment. This handler just plumbs each path's queue to the
    # socket and emits keepalives.
    await ws.accept()
    paths = ws.query_params.getlist("path")

    queue: asyncio.Queue = asyncio.Queue()
    entries = [await _WATCH_REGISTRY.subscribe(p, queue) for p in paths]

    async def pump():
        # Forward change messages; a 15s idle gap emits a keepalive (WF-3).
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                await ws.send_text(json.dumps({"keepalive": True}))
                continue
            await ws.send_text(msg)

    pumper = asyncio.create_task(pump())
    try:
        # Drain the receive side purely to learn about disconnect; the
        # pump loop alone would only notice on its next send.
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        pumper.cancel()
        for entry in entries:
            _WATCH_REGISTRY.unsubscribe(entry, queue)

def _git_repo_payload(path: str):
    """Whether `path` is the work-tree root of a git repository.

    Backs the Compress submenu's two git formats, which only make sense at a
    repo root (see gitignore._is_repo_root). It is a `git` subprocess, so it
    is deliberately NOT part of the stat payload: it runs on submenu hover,
    once, instead of on every right-click of every row.

    A mount-backed path answers False without asking git at all — a `git -C`
    over a mount walks the remote prefix, the known mount-wedging pattern —
    and that is also the honest answer: an object-store prefix is not a
    checkout."""
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    if shell_mounts.is_mount_backed(path):
        return {"path": path, "is_repo_root": False}
    return {"path": path, "is_repo_root": _is_repo_root(path)}


@router.get("/api/fs/git-repo")
def api_fs_git_repo(path: str):
    return _git_repo_payload(path)

@router.post("/api/fs/reveal")
def api_fs_reveal(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    # Open the path in the OS file manager (Finder / Explorer / xdg).
    # Browsers block file:// navigation from http pages, so the breadcrumb's
    # reveal button goes through the server, which is local-only.
    # A file is revealed selected inside its folder; a directory is opened.
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    path = body.get("path")
    if not path or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    if not os.path.exists(path):
        return _error(f"no such path: {path}", status=404)

    is_dir = os.path.isdir(path)
    if sys.platform == "darwin":
        cmd = ["open", path] if is_dir else ["open", "-R", path]
    elif os.name == "nt":
        # Explorer needs native backslash paths — forward slashes make
        # /select, silently open the default folder instead.
        win_path = os.path.normpath(path)
        cmd = ["explorer", win_path] if is_dir else f'explorer /select,"{win_path}"'
    else:
        cmd = ["xdg-open", path if is_dir else os.path.dirname(path)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return JSONResponse({"ok": True})
