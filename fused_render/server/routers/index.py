"""The file index's HTTP surface: scan control, status polling, stats,
lookup, and the small persisted config — plus the startup scan scheduler.

The engine lives in `fused_render/index/` and knows nothing about HTTP; this
module is the thin adapter. Every route resolves its `IndexConfig` from disk
per request (`load_config()`), which is the whole point of de-globalizing it:
an ignore-list edit applies to the next scan without a restart.

Mutating routes carry the usual X-Fused guard. Reads are unguarded like the
other read endpoints — a foreign page cannot read our responses anyway — and
none of them can write.

There is deliberately no route that runs SQL against the index (`index/specs/
query.md §5`).
"""
import asyncio
import logging
import os

from fastapi import APIRouter, Body, Header, Query

from fused_render.index import runner
from fused_render.index.config import IndexConfig, load_config, save_config
from fused_render.index.ignore import clean_patterns, default_ignore, norm
from fused_render.index.query import MAX_CORPUS
from fused_render.index.query import lookup as index_lookup
from fused_render.index.query import search_under as index_search
from fused_render.index.query import stats as index_stats
from fused_render.index.store import (
    applied_ignore_sig,
    delete_store,
    read_manifest,
    save_applied_ignore,
)
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)
router = APIRouter()

# How recently a root must have been scanned for the startup scheduler to skip
# it. Short enough that a machine left on for a day rescans when the app is
# reopened, long enough that a dev-server reload loop (or three windows opening
# at once) cannot queue scan after scan. An on-demand scan is always available
# regardless.
SCAN_DEBOUNCE_S = 15 * 60
# Run directories kept around for post-mortems; the rest are reclaimed at
# startup (index/specs/scan.md §2).
KEEP_RUNS = 20


def scan_roots(cfg: IndexConfig, start_dir: str | None = None) -> list:
    """What the scheduler indexes: the configured roots, else the user's home.

    Home is the default rather than the project root because a whole-home scan
    with the default ignore rules costs seconds, and an index that only covers
    one project answers almost no search the explorer wants to ask. `start_dir`
    is accepted so a caller can express a narrower intent, but it is not used
    as a fallback — an index that silently follows whichever folder the app was
    opened on would give different answers per window."""
    if cfg.roots:
        return list(cfg.roots)
    return [os.path.expanduser("~")]


def run_startup_scan(start_dir: str | None = None) -> None:
    """Reclaim old run dirs and kick off one incremental scan per root.

    Never raises and never blocks: the scan itself is a detached worker, so
    this is a `Popen` per root and nothing more. A scan that ran recently is
    skipped (SCAN_DEBOUNCE_S), and a root that no longer exists is ignored
    rather than reported — the config outlives the folders it names."""
    try:
        cfg = load_config()
        runner.prune_runs(cfg, keep=KEEP_RUNS)
    except Exception:  # noqa: BLE001 - housekeeping must never stop the server
        logger.exception("could not read the index config")
        return
    import time

    now = time.time()
    for root in scan_roots(cfg, start_dir):
        try:
            last = runner.last_scan(cfg, root)
            if last is not None and (now - last) < SCAN_DEBOUNCE_S:
                logger.info("index: %s was scanned %.0fs ago, skipping",
                            root, now - last)
                continue
            started = runner.start(cfg, root)
            logger.info("index: started background scan of %s (run %s)",
                        root, (started or {}).get("run_id"))
        except ValueError as e:
            # A root that no longer exists, or one that turned out to be
            # mount-backed — skip it quietly; the config outlives the folders
            # it names. runner.start makes this call, and it checks the mount
            # guard BEFORE any kernel syscall, so a wedged mount cannot hang
            # this loop (there is deliberately no os.path.isdir here).
            logger.info("index: skipping %s (%s)", root, e)
        except Exception:  # noqa: BLE001 - one bad root must not stop the rest
            logger.exception("could not start the index scan of %s", root)


async def startup_scan(start_dir: str | None = None) -> None:
    """The create_app hook. Off the event loop because it touches the disk."""
    await asyncio.to_thread(run_startup_scan, start_dir)


# ------------------------------------------------------------------- scanning

@router.post("/api/index/scan")
def api_index_scan(body: dict = Body(default={}),
                   x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    cfg = load_config()
    root = body.get("root") or ""
    if not root:
        roots = scan_roots(cfg)
        root = roots[0] if roots else os.path.expanduser("~")
    try:
        started = runner.start(cfg, str(root), full=bool(body.get("full")))
    except ValueError as e:
        return _error(str(e))
    return {"ok": True, **started}


@router.post("/api/index/cancel")
def api_index_cancel(body: dict = Body(default={}),
                     x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        return {"ok": True, **runner.cancel(load_config(), str(body.get("run_id") or ""))}
    except ValueError as e:
        return _error(str(e))


@router.get("/api/index/status")
def api_index_status(run_id: str = Query(default=""),
                     since: int = Query(default=0)):
    """The scan's state, flat enough to render directly.

    Without a `run_id` this answers for the MOST RECENT run, which is what a
    page that just loaded wants: it has no run id, but a scan may well be in
    flight (the startup one) and the UI should be able to say "building
    index… N files" instead of pretending nothing is happening.
    """
    cfg = load_config()
    manifest = read_manifest(cfg)
    runs = runner.list_runs(cfg, limit=KEEP_RUNS)["runs"]
    # `has_index` and `scanning` are the two bits the explorer's decision table
    # turns on, and they are independent: a rescan over an existing index keeps
    # serving the last completed generation (index-store.md §4), so "scanning"
    # means "say indexing…", not "stop using the index".
    base = {"ok": True,
            "has_index": manifest is not None,
            # any(), not runs[0]: with several roots, a quick second scan can
            # finish (and become runs[0]) while the first root's is still
            # walking — the indexing caveat must hold until they ALL settle
            "scanning": any(r["running"] for r in runs),
            "files_indexed": int((manifest or {}).get("rows") or 0),
            "last_completed_at": (manifest or {}).get("updated"),
            # kept as the pre-existing names for the same two facts
            "indexed": manifest is not None,
            "updated": (manifest or {}).get("updated")}
    if not run_id:
        if not runs:
            return {**base, "run_id": None, "root": None, "phase": "",
                    "dirs": 0, "files": 0, "reused": 0, "current": "",
                    "summary": None, "cancelled": False, "error": None,
                    "running": False}
        return {**base, **runs[0]}
    try:
        out = runner.status(cfg, run_id, since=since)
    except ValueError as e:
        return _error(str(e))
    root = None
    for run in runs:
        if run["run_id"] == run_id:
            root = run["root"]
            break
    return {**base, **out["state"], "run_id": run_id, "root": root,
            "events": out["events"], "cursor": out["cursor"]}


@router.get("/api/index/runs")
def api_index_runs():
    return {"ok": True, **runner.list_runs(load_config())}


# -------------------------------------------------------------------- reading

@router.get("/api/index/stats")
def api_index_stats(root: str = Query(default="")):
    return {"ok": True, **index_stats(load_config(), root=root)}


@router.get("/api/index/lookup")
def api_index_lookup(q: str = Query(default=""), limit: int = Query(default=100),
                     offset: int = Query(default=0),
                     sort: str = Query(default="mtime")):
    return {"ok": True,
            **index_lookup(load_config(), q, limit=limit, offset=offset,
                           sort=sort)}


@router.get("/api/index/search")
def api_index_search(root: str = Query(default=""), q: str = Query(default=""),
                     limit: int = Query(default=MAX_CORPUS)):
    """The explorer's in-folder corpus, index-backed.

    Same entry shape as /api/fs/walk, plus `covered`/`fresh` so the client can
    decide whether to use it. A miss is `{covered: false, entries: []}` with a
    200 — never an error: the explorer falls back to the live walk, and a red
    search box during the first-boot scan would be a lie about a system that
    is working exactly as designed."""
    if not root.strip():
        return _error("'root' is required")
    return {"ok": True, **index_search(load_config(), root, q=q, limit=limit)}


# --------------------------------------------------------------------- config

@router.get("/api/index/config")
def api_index_config():
    cfg = load_config()
    return {"ok": True, "roots": scan_roots(cfg), "configured_roots": cfg.roots,
            "ignore": cfg.ignore, "defaults": default_ignore(),
            "location": cfg.dir}


@router.post("/api/index/config")
def api_index_config_write(body: dict = Body(default={}),
                           x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    cfg = load_config()
    for key in ("roots", "ignore"):
        if key not in body:
            continue
        value = body[key]
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            return _error(f"'{key}' must be an array of strings")
        if key == "roots":
            # Roots are PATHS, not ignore patterns: clean_patterns rstrips
            # "/" into the empty string, silently destroying a root of "/".
            cfg.roots = [norm(os.path.abspath(os.path.expanduser(r.strip())))
                         for r in value if r.strip()]
        else:
            cfg.ignore = clean_patterns(value)
            cfg._rules = None
    saved = save_config(cfg)
    # Reconcile. The engine fingerprints the rules each root's slice of the
    # index was BUILT under (index/specs/scan-ignore.md §4): while they
    # differ, the store still holds rows for folders the user just excluded
    # and is still missing the ones they just re-included. The next scan is
    # what fixes that — it sees the mismatch, discards the reuse cache and
    # rebuilds — so a save starts one PER STALE ROOT rather than only the
    # first: each root reconciles on its own scan, and a root left out here
    # would look reconciled forever once its own sig was stamped. A root with
    # no fingerprint yet has no index slice to reconcile; the startup
    # scheduler scans it under the new rules anyway.
    sig = saved.rules.sig()
    stale = [r for r in scan_roots(saved)
             if (applied_ignore_sig(saved, r) or sig) != sig]
    rescan_run_ids = []
    for root in stale:
        try:
            started = runner.start(saved, root)
            rescan_run_ids.append((started or {}).get("run_id"))
        except (ValueError, OSError):
            logger.exception("could not start the post-edit rescan of %s", root)
    return {"ok": True, "roots": saved.roots, "ignore": saved.ignore,
            "defaults": default_ignore(), "location": saved.dir,
            "needs_rescan": bool(stale),
            "rescan_run_id": rescan_run_ids[0] if rescan_run_ids else None,
            "rescan_run_ids": rescan_run_ids}


@router.post("/api/index/delete")
def api_index_delete(x_fused: str | None = Header(default=None)):
    """Drop the whole index. Search silently falls back to the live walk until
    the next scan, so this is a reclaim-disk / start-over button, not a
    destructive one — the only thing lost is derived data.

    Any scan in flight is cancelled first: a worker that survived the delete
    would compact its shards into the store moments later and quietly undo
    it."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    cfg = load_config()
    cancelled = []
    for run in runner.list_runs(cfg, limit=KEEP_RUNS)["runs"]:
        if run["running"]:
            try:
                runner.cancel(cfg, run["run_id"])
                cancelled.append(run["run_id"])
            except ValueError:
                pass
    delete_store(cfg)
    return {"ok": True, "deleted": True, "cancelled": cancelled,
            "location": cfg.dir}
