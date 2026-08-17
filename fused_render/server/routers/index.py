"""The file index's HTTP surface: scan control, status polling, stats,
lookup, and the small persisted config — plus the startup scan scheduler.

The engine lives in `fused_render/index/` and knows nothing about HTTP; this
module is the thin adapter. Every route resolves its `IndexConfig` from disk
per request (`load_config()`), which is the whole point of de-globalizing it:
an ignore-list edit applies to the next scan without a restart.

Mutating routes carry the usual X-Fused guard. Reads are unguarded like the
other read endpoints — a foreign page cannot read our responses anyway — and
none of them can write.

User SQL against the index goes through POST /api/index/query, which executes it
in a confined read-only DuckDB session (`index/guarded_query.py`,
`index/specs/query.md §5`), and POST /api/index/ask, which compiles a question
into one of those statements through the existing AI relay. Both are POST and
X-Fused-guarded despite being reads: they execute a caller-shaped statement, so
neither should be reachable from a crafted link.
"""
import asyncio
import json
import logging
import os
import re
import threading

from fastapi import APIRouter, Body, Header, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from fused_render.index import freshness, runner
from fused_render.index.freshness import enclosing_root
from fused_render.index.config import IndexConfig, load_config, save_config
from fused_render.index.guarded_query import MAX_LIMIT, run_guarded
from fused_render.index.ignore import MountGuard, default_ignore, norm
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
from fused_render.server import ai as _server_ai
from fused_render.server.common import _error, _require_fused
from fused_render.server.index_gitignore import filter_corpus

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
    opened on would give different answers per window.

    Roots come back in `runner.canonical_root` form — the same spelling
    `runner.start` files every store key under. This function's output is not only
    started but COMPARED against those keys (the stale-fingerprint scan below,
    freshness.note_folder_opened, routers/git_repos._usable), and the user's raw
    configured spelling is not the stored one: `~/proj` never matches
    `/Users/me/proj`, and on Windows `expanduser("~")` returns `C:\\Users\\me`
    against a stored `C:/Users/me`, so every lookup misses on that platform and the
    index reads as permanently unreconciled."""
    if cfg.roots:
        return [runner.canonical_root(r) for r in cfg.roots]
    return [runner.canonical_root("~")]


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


# ------------------------------------------------------------- startup warm

def warm_root() -> str:
    """The root the explorer's home page will search.

    FilesHome searches `config.home` — `expanduser("~")` from
    routers/config.py — and NOT the folder the app was opened on, so a warm
    aimed anywhere else fills a pool the first keystroke never reads. In
    `canonical_root` spelling because that is the spelling every store key and
    every scan-root comparison uses (see `scan_roots`)."""
    return runner.canonical_root("~")


def run_startup_warm() -> None:
    """Pay the first search's cold cost at idle instead of on a keystroke.

    Everything the corpus path caches is PER PROCESS and starts empty: the
    gitignore verdict pool (server/index_gitignore.py) knows nothing until
    some request sweeps `git check-ignore` over the whole corpus, and duckdb
    is not even imported until the first query. Measured on a 164k-entry home:
    ~2.2 s for the first search of a fresh process against ~0.8 s for the next
    one — and all of it was billed to the user's first keystroke.

    So it runs exactly the pair of calls `api_index_search` makes, with the
    same root and the same pool key. An index that has not covered the home
    root yet answers `covered: false` cheaply and pools nothing; a scan that
    completes later therefore still leaves the first search paying the sweep.
    Deliberately NOT polled or retried: a loop chasing the scan would be a
    second scheduler, and the persisted pool (index_gitignore) is what covers
    the restart case.

    Never raises: it runs on a thread nobody joins."""
    try:
        root = warm_root()
        # A mount-backed home is refused by the index anyway, so the warm
        # could only answer `covered: false` — after aiming kernel I/O at a
        # mount path, which is the one thing this codebase never does
        # speculatively. Pure string work, exactly as in `runner.start`.
        if MountGuard(mounts_dir=runner._mounts_dir()).blocks_root(root):
            return
        cfg = load_config()
        out = index_search(cfg, root)
        filter_corpus(out, index_root=enclosing_root(scan_roots(cfg),
                                                     out.get("root") or root))
    except Exception:  # noqa: BLE001 - a warm must never take the server down
        logger.exception("could not warm the index search path")


def startup_warm() -> None:
    """The create_app hook. A detached daemon thread, not `to_thread`: the
    startup hook must COMPLETE before the app serves, and this is seconds of
    duckdb and `git check-ignore` — the very cost the warm exists to move off
    the request path. Nobody joins it and it cannot raise (above)."""
    threading.Thread(target=run_startup_warm, name="index-warm",
                     daemon=True).start()


# ------------------------------------------------------ open-folder freshness

# At most one check in flight. /api/fs/list fires for every folder the explorer
# opens AND again on every watch tick of a folder being displayed, so the hook
# is called far more often than a check costs — and a check opens duckdb over
# dirs.parquet. A plain non-reentrant lock, acquired by the request thread and
# released by the worker, is the whole concurrency control.
_freshness_slot = threading.Lock()

# ...but a non-reentrant lock is not a debounce: it drops the checks that
# OVERLAP one, not the ones that follow it, so opening folders in sequence
# checked on every single open. Each check opens duckdb over dirs.parquet, and
# every check that does fire a scan ends by invalidating every corpus the
# client has fetched (platform/lib/index-status). So a root is checked at most
# this often, whatever the explorer is doing.
#
# freshness.MIN_INTERVAL_S is the other floor — on the SCANS themselves, read
# off scans.json so it also sees the ones the scheduler and the manual buttons
# start; this one is about the checks, and only about the ones this process
# makes. One cadence, so this must not be LONGER: a check is the only thing that
# can act on the scan floor, and checking less often than scans are allowed just
# leaves the difference on the table.
#
# Slightly SHORTER, and the epsilon is load-bearing. The two clocks start at
# different moments: this one is stamped when a check begins, while
# `runner._record_scan` stamps when the scan it starts is spawned — a duckdb
# lookup and a Popen later. So last_scan is always a little after last_check, and
# at exactly equal intervals the next due check lands inside that offset, finds
# the scan floor not yet clear, refuses — and re-stamps, pushing the next check
# out a further full interval. Every second cycle would be a no-op and the real
# cadence would be ~2x the number both comments name. Five seconds of slack
# covers the spawn comfortably.
FRESHNESS_CHECK_S = 55.0

# root -> when it was last checked. Bounded by the number of configured scan
# roots (a handful), so it needs no eviction.
_freshness_checked: dict = {}
_freshness_checked_lock = threading.Lock()


def _freshness_due(root: str, now: float) -> bool:
    """Whether `root` is due a check, stamping it when it is."""
    with _freshness_checked_lock:
        last = _freshness_checked.get(root)
        if last is not None and (now - last) < FRESHNESS_CHECK_S:
            return False
        _freshness_checked[root] = now
        return True


def _run_freshness_check(path: str) -> None:
    """The check itself, off the request thread. Never raises: a listing must
    not fail, or slow down, because index housekeeping did."""
    try:
        import time

        cfg = load_config()
        roots = scan_roots(cfg)
        # The debounce is keyed on the enclosing ROOT, not the folder: a scan
        # is per root, so two folders under one root are the same question. A
        # folder under no root has no question to ask at all, and
        # note_folder_opened would answer None anyway.
        root = enclosing_root(roots, path)
        if root is None or not _freshness_due(root, time.time()):
            return
        started = freshness.note_folder_opened(cfg, path, roots)
        if started:
            logger.info("index: %s changed since the last scan; rescanning %s",
                        path, started)
    except Exception:  # noqa: BLE001 - housekeeping must never surface
        logger.exception("could not check index freshness for %s", path)
    finally:
        if _freshness_slot.locked():
            _freshness_slot.release()


def note_folder_opened(path: str) -> bool:
    """The explorer opened `path`: check the index against it in the background.

    Returns whether a check was started. Called from /api/fs/list — the folder
    the user is looking at is the one whose search must not be stale, and the
    listing request is the only signal that says so on every platform."""
    if not _freshness_slot.acquire(blocking=False):
        return False
    try:
        threading.Thread(target=_run_freshness_check, args=(path,),
                         daemon=True, name="index-freshness").start()
    except RuntimeError:  # interpreter shutting down
        _freshness_slot.release()
        return False
    return True


# ------------------------------------------------------------------- scanning

@router.post("/api/index/scan")
def api_index_scan(body: dict = Body(default={}),
                   x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    cfg = load_config()
    full = bool(body.get("full"))
    root = body.get("root") or ""
    if root:
        try:
            started = runner.start(cfg, str(root), full=full)
        except ValueError as e:
            return _error(str(e))
        return {"ok": True, **started, "runs": [started]}
    # No root means "the whole index", which is every configured root — the
    # panel's Re-index and Full-scan buttons say exactly that. Scanning only
    # the first left the others stale with nothing in the UI to show it.
    # One dead root does not fail the rest, as in run_startup_scan: the config
    # outlives the folders it names.
    runs, last_error = [], None
    for r in scan_roots(cfg):
        try:
            runs.append(runner.start(cfg, r, full=full))
        except ValueError as e:
            last_error = str(e)
            logger.info("index: skipping %s (%s)", r, e)
    if not runs:
        return _error(last_error or "no scannable roots are configured")
    return {"ok": True, **runs[0], "runs": runs}


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
        # The most recent RUNNING run, not simply the most recent: with
        # several roots a quick scan can finish while a big one still walks,
        # and reporting the finished one froze the panel's counts for minutes
        # while `scanning` (rightly) stayed true. Falls back to the latest run
        # when nothing is running, which is the idle "last scan" readout.
        live = next((r for r in runs if r["running"]), None)
        return {**base, **(live or runs[0])}
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
    is working exactly as designed.

    Entries pass through the gitignore filter before leaving: the walk this
    corpus replaces prunes gitignored entries, and the swap must not change
    what search shows (server/index_gitignore.py)."""
    if not root.strip():
        return _error("'root' is required")
    cfg = load_config()
    out = index_search(cfg, root, q=q, limit=limit)
    # Filtered per INDEX ROOT, not per requested folder: the explorer's
    # in-folder search asks with whichever folder is open, and a cache keyed on
    # that re-paid a whole-subtree check-ignore sweep every time browsing
    # evicted a folder. `out["root"]` (not the raw query string) is the
    # canonical spelling the corpus rels are relative to.
    index_root = enclosing_root(scan_roots(cfg), out.get("root") or root)
    out = filter_corpus(out, index_root=index_root)
    return {"ok": True, **out}


# ------------------------------------------------------------------ user SQL

# Rows a query answers with when the caller names no limit. Small: this fills a
# table in a preferences panel, and MAX_LIMIT is there for a caller that means
# it.
DEFAULT_QUERY_LIMIT = 200


def _guarded(cfg: IndexConfig, sql: str, limit) -> dict | object:
    """`run_guarded` with both failure modes mapped to a 400.

    duckdb's own exceptions are 400s too, not 500s: "no such column" is the
    caller's mistake, and the panel shows the message verbatim so a typo is
    self-explanatory. Only the message travels — no traceback."""
    try:
        n = int(limit) if limit is not None else DEFAULT_QUERY_LIMIT
    except (TypeError, ValueError):
        return _error("'limit' must be a number")
    try:
        return run_guarded(cfg, sql, limit=min(max(n, 0), MAX_LIMIT))
    except ValueError as e:
        return _error(str(e))
    except Exception as e:  # noqa: BLE001 - duckdb's exception tree, flattened
        return _error(f"{type(e).__name__}: {e}")


@router.post("/api/index/query")
def api_index_query(body: dict = Body(default={}),
                    x_fused: str | None = Header(default=None)):
    """Run one read-only statement against `files` / `dirs`.

    Guarded like the mutating routes even though it writes nothing: it executes
    a statement the caller wrote, which is not something a foreign page should
    be able to fire blind."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    sql = body.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        return _error("'sql' must be a non-empty string")
    out = _guarded(load_config(), sql, body.get("limit"))
    if not isinstance(out, dict):
        return out
    return {"ok": True, **out}


# What the model needs to write a valid statement, and nothing more: the two
# schemas, the units, and the shape of an acceptable answer. Kept here rather
# than derived from store.schemas because it is prose for a reader, not a
# contract — a column list is only half of what makes `size` mean bytes.
_ASK_SYSTEM_PROMPT = """\
You translate a question about a filesystem index into ONE DuckDB SQL statement.

Two views are available and nothing else:

  files(path VARCHAR, dir VARCHAR, name VARCHAR, ext VARCHAR, size BIGINT,
        mtime DOUBLE, depth INTEGER)
  dirs(dir VARCHAR, sig VARCHAR, n_files INTEGER, total_size BIGINT,
       mtime_ns BIGINT, n_subdirs INTEGER, depth INTEGER)

- `path` and `dir` are absolute, POSIX-separated. `name` is the basename.
- `ext` is lowercase and has no leading dot; it is '' for a file with no
  extension.
- `size` is bytes. `mtime` is epoch SECONDS (a float); `dirs.mtime_ns` is epoch
  NANOSECONDS, and 0 there means unknown.
- `depth` is the absolute count of '/' in the path.

Rules: answer with ONE statement, a SELECT (a WITH is fine). No INSERT, UPDATE,
DELETE, CREATE, COPY, ATTACH, SET or PRAGMA — they are refused. Do not read any
file or table other than these two views. Give the columns readable aliases, and
add an ORDER BY and a LIMIT when the question implies a top-N.

Reply with the SQL and nothing else: no prose, no explanation, no code fence.\
"""

_FENCE = re.compile(r"```[a-zA-Z]*\s*(.*?)```", re.S)


def _sql_from_answer(text: str) -> str:
    """The SQL out of a model's reply.

    Asked for bare SQL, models still fence it and still add a sentence either
    side often enough that stripping is cheaper than a retry. The FIRST fenced
    block wins; with no fence the whole reply is the statement."""
    m = _FENCE.search(text or "")
    return (m.group(1) if m else (text or "")).strip()


@router.post("/api/index/ask")
async def api_index_ask(body: dict = Body(default={}),
                        x_fused: str | None = Header(default=None)):
    """A question in English, answered from the index.

    The compiled SQL is returned WHATEVER happens to it — including when the
    guard refuses it — because a wrong answer with the statement visible is
    debuggable and a bare error is not.

    Nothing here trusts the model: its statement goes through exactly the same
    guard a hand-typed one does (`index/guarded_query.py`). The prompt asking
    for a SELECT is a hint, not the boundary."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return _error("'prompt' must be a non-empty string")
    # The relay owns the claude hop, the model preference, the timeout and the
    # typed error envelope; a failure passes straight through it unchanged
    # rather than being re-described here.
    resp = await _server_ai._ai_relay({"prompt": prompt.strip(),
                                       "system_prompt": _ASK_SYSTEM_PROMPT,
                                       "stream": False})
    try:
        answered = json.loads(bytes(resp.body))
    except ValueError:
        return _error("the AI relay returned an unreadable response", status=502)
    if not answered.get("ok"):
        return resp
    sql = _sql_from_answer((answered.get("result") or {}).get("text") or "")
    if not sql:
        return _error("the model answered with no SQL", status=502)
    # In a threadpool, unlike `query` next door: that one is a plain `def`
    # handler, so FastAPI already threadpools it, while this handler must be
    # `async` for the relay `await` above — and a duckdb query bounded only by
    # guarded_query.TIMEOUT_S (10s) run inline would block the event loop, and
    # with it every other request the app is making meanwhile.
    out = await run_in_threadpool(
        lambda: _guarded(load_config(), sql, body.get("limit")))
    if not isinstance(out, dict):
        # Same 400, plus the statement that earned it.
        return JSONResponse({**json.loads(bytes(out.body)), "sql": sql},
                            status_code=out.status_code)
    return {"ok": True, "sql": sql, **out}


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
            # Verbatim — the textarea is a document the user authors, and the
            # panel documents `#` comments. Parsing happens in cfg.rules.
            cfg.ignore = [str(v) for v in value]
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
    # Same shape as the GET: the panel swaps its whole state for this
    # response, so a save that reported the raw configured list would blank
    # the coverage line whenever the roots are the unconfigured home default.
    return {"ok": True, "roots": scan_roots(saved),
            "configured_roots": saved.roots, "ignore": saved.ignore,
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
