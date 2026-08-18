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
import gzip
import json
import logging
import os
import re
import threading

from fastapi import APIRouter, Body, Header, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response

from fused_render.index import freshness, runner
from fused_render.index.freshness import enclosing_root
from fused_render.index.config import IndexConfig, load_config, save_config
from fused_render.index.guarded_query import MAX_LIMIT, run_guarded
from fused_render.index.ignore import MountGuard, default_ignore, norm
from fused_render.index.query import MAX_CORPUS, RANK_LIMIT
from fused_render.index.query import lookup as index_lookup
from fused_render.index.query import search_ranked as index_rank
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


# root -> the run id this process started for it at startup, for the warm
# below to wait on. Only the runs THIS boot spawned: a root that was
# debounce-skipped, refused, or failed leaves no entry, which is exactly the
# "there is nothing to wait for" signal the warm needs. Bounded by the number
# of scan roots (a handful), and rewritten from scratch on every call.
_startup_runs: dict = {}


def run_startup_scan(start_dir: str | None = None) -> None:
    """Reclaim old run dirs and kick off one incremental scan per root.

    Never raises and never blocks: the scan itself is a detached worker, so
    this is a `Popen` per root and nothing more. A scan that ran recently is
    skipped (SCAN_DEBOUNCE_S), and a root that no longer exists is ignored
    rather than reported — the config outlives the folders it names.

    Records each started run in `_startup_runs` for `run_startup_warm`, which
    warms only after the run covering its root has finished."""
    _startup_runs.clear()
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
            run_id = (started or {}).get("run_id")
            if run_id:
                _startup_runs[root] = run_id
            logger.info("index: started background scan of %s (run %s)",
                        root, run_id)
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


# How often the warm re-reads the event log of the one scan it is waiting for.
# Nothing signals this thread when a detached worker finishes, so the wait
# reads that log (`runner.has_ended`, cursored so only new lines are decoded).
# Half a second is the worker's own progress cadence, so a finer poll would
# mostly re-read a file with nothing new in it, and half a second of latency is
# nothing against the ~2.2 s the warm is saving.
WARM_WAIT_POLL_S = 0.5
# ...and the hard ceiling on that wait. The first-ever whole-home scan that
# motivated this took 9.2 s (570k files, 74k dirs); six minutes is ~40x that,
# so even a much larger home on a much slower disk still gets warmed. This is
# the LAST resort, not the usual exit: a worker killed mid-walk never writes
# `run_end`, and the wait spots that within ABANDONED_RUN_S (5 min) through the
# same mtime check `runner.status` uses. The ceiling sits just past that so the
# common death takes the specific path, and covers only the pathological rest —
# a worker alive but wedged — so the thread can never poll for the process
# lifetime.
WARM_WAIT_DEADLINE_S = 6 * 60.0


def _wait_for_scan(cfg: IndexConfig, run_id: str) -> bool:
    """Block until `run_id` stops running; False if the deadline beat it.

    One-shot and bounded, on a daemon thread nobody joins: it waits for one
    named run and then it is done, whichever way that run ended.

    The return value says how the WAIT ended, and nothing more — the caller
    warms either way, because how a scan ended does not tell you whether the
    index covers the root. It exists so the two ways of not-finishing can be
    told apart in a log and in a test.

    Deliberately NOT `runner.status`, which is the status panel's call: it
    folds the whole event log from line 0 every time, so polling it would
    re-parse a log the waited-on scan is appending to twice a second — work
    quadratic in that scan's length, spent competing with it for the disk.
    `runner.has_ended` carries a cursor and decodes only what is new, and the
    dead-worker case is the same `_looks_abandoned` mtime check `status`
    applies (a worker killed mid-walk never writes `run_end`, so without it
    this would wait out the whole deadline for the most common death)."""
    import time

    try:
        run_dir = runner._run_dir(cfg, run_id)
    except ValueError:
        # The run dir is gone (pruned, or a stubbed start that never made
        # one). Nothing left to wait for; whether the index covers the root is
        # a question for the search that follows, not for this.
        return True
    deadline = time.monotonic() + WARM_WAIT_DEADLINE_S
    cursor = 0
    while True:
        ended, cursor = runner.has_ended(run_dir, cursor)
        if ended:
            return True
        if runner._looks_abandoned(run_dir, time.time(), runner.ABANDONED_RUN_S):
            logger.info("index: scan %s stopped reporting; warming with "
                        "whatever the index holds", run_id)
            return False
        if time.monotonic() >= deadline:
            # Once, at info: this is a diagnosis of a stuck scan, not a
            # failure of the warm, which goes ahead regardless.
            logger.info("index: gave up waiting %.0fs for scan %s; warming "
                        "with whatever the index holds",
                        WARM_WAIT_DEADLINE_S, run_id)
            return False
        time.sleep(WARM_WAIT_POLL_S)


# The query the startup warm ranks with, and it is deliberately one that MATCHES
# NOTHING. A query with hits stops at the cheap substring pass (the ladder in
# `search_ranked`), leaving the subsequence-regex plan — the expensive half, and
# the one a mistyped query lands on — cold for the first user who needs it. A
# no-match query runs both passes, plus the ignore-root discovery behind the
# gitignore filter, and returns an empty body.
WARM_RANK_QUERY = "zqxjv"


def _ranked(cfg: IndexConfig, root: str, q: str, limit: int = RANK_LIMIT) -> dict:
    """`search_ranked` with the server's gitignore filter wired in.

    The index package cannot import the server, so `search_ranked` takes the
    filter as a callable — this is the only place that knows both. It is
    called BEFORE the cut to `limit` (search_ranked does that), and keyed on
    the enclosing INDEX ROOT rather than the requested folder, for the reason
    `api_index_search` gives: a pool keyed per browsed folder re-paid a whole
    check-ignore sweep every time browsing evicted one.

    `oracle_rels` comes from the caller, not from the payload: a ranked answer
    is ~200 rows with no dot-leading rels among them, so the filter's own
    discovery would find no `.gitignore`, decide nothing, and drop nothing
    (index_gitignore.filter_corpus says this at length)."""
    def drop_ignored(canonical_root: str, hits: list, oracle_rels: list) -> list:
        index_root = enclosing_root(scan_roots(cfg), canonical_root)
        return filter_corpus({"covered": True, "root": canonical_root,
                              "entries": hits}, index_root=index_root,
                             oracle_rels=oracle_rels)["entries"]

    return index_rank(cfg, root, q=q, limit=limit, gitignore_filter=drop_ignored)


def _covers(a: str, b: str) -> bool:
    """Whether the trees at `a` and `b` overlap (either contains the other)."""
    a = norm(os.path.abspath(a)).rstrip("/") or "/"
    b = norm(os.path.abspath(b)).rstrip("/") or "/"
    return (a == b or a.startswith((b if b == "/" else b + "/"))
            or b.startswith((a if a == "/" else a + "/")))


def _scan_in_flight(cfg: IndexConfig, root: str) -> bool:
    """Whether a live run is writing rows that belong to `root`.

    Both directions count. A scan of an ANCESTOR root will rewrite this
    folder's rows when it compacts; a scan of a DESCENDANT is adding rows
    underneath it. Either way the answer the search box has is provisional,
    which is the whole of what the `scanning` reason claims."""
    try:
        runs = runner.list_runs(cfg, limit=KEEP_RUNS)["runs"]
    except Exception:  # noqa: BLE001 - a search must not fail over housekeeping
        logger.exception("could not list index runs")
        return False
    return any(r.get("running") and r.get("root")
               and _covers(root, str(r["root"])) for r in runs)


def _rank_reason(cfg: IndexConfig, root: str, out: dict) -> str:
    """Why the ranked answer is what it is, in the client's vocabulary.

    The client switches the SOURCE on this — `mount` (and `package`) send it to
    the live walk, `uncovered` makes it ask for a scan, `scanning` makes it
    poll — so the ordering below is a set of claims about what is fixable:

      * `mount` first, and unconditionally. Indexing a remote mount is refused
        structurally (MountGuard, index/specs/scan.md), so no scan will ever
        cover it and no poll will ever end. This is the check the client must
        NOT carry a copy of: the rule is MountGuard's, it is the same object
        `runner.start` refuses with, and a second copy in TypeScript would
        drift from it silently.
      * `package` next, for the same reason in weaker form: the scan records a
        `.app` (or a `.photoslibrary`) as one opaque row and never lists it, so
        a folder inside one is permanently uncoverable however long you wait.
      * `scanning` over `uncovered`, because it says "ask again", and it is
        reported even when the answer IS covered: the rows are real, and more
        of them are on the way.

    The mount check is paid only on a miss — it realpaths — and a covered root
    cannot be mount-backed anyway, since nothing ever indexed one."""
    if not out.get("covered"):
        # BEFORE any kernel syscall of ours on the caller's path: blocks_root
        # is string work against the mount records plus one realpath, where a
        # stat under a wedged rclone mount blocks this thread indefinitely.
        if MountGuard(mounts_dir=runner._mounts_dir()).blocks_root(root):
            return "mount"
        if out.get("reason") == "package":
            return "package"
    if _scan_in_flight(cfg, root):
        return "scanning"
    return str(out.get("reason") or "")


def run_startup_warm() -> None:
    """Pay the first search's cold cost at idle instead of on a keystroke.

    Everything the corpus path caches is PER PROCESS and starts empty: the
    gitignore verdict pool (server/index_gitignore.py) knows nothing until
    some request sweeps `git check-ignore` over the whole corpus, and duckdb
    is not even imported until the first query. Measured on a 164k-entry home:
    ~2.2 s for the first search of a fresh process against ~0.8 s for the next
    one — and all of it was billed to the user's first keystroke.

    So it runs exactly the pair of calls `api_index_search` makes, with the
    same root and the same pool key. Usually that is all: an index is already
    on disk, the search answers `covered: true`, and the sweep lands in the
    pool immediately.

    On a first-ever boot it is not. The index has not covered home yet, the
    search answers `covered: false` cheaply, and there is nothing to sweep —
    which is exactly the boot the warm exists for. So when that happens it
    waits for the ONE scan `run_startup_scan` just spawned for this root
    (`_startup_runs`) and then warms — warms unconditionally, however that wait
    ended. That is not the general "poll for scans" scheduler this deliberately
    is not: it is one bounded wait on one named run, with a definite end
    (WARM_WAIT_DEADLINE_S) on a thread nobody joins. A root whose scan was
    debounce-skipped has no entry and is not waited on — there is no new run
    coming, and its index is already there.

    Never raises: it runs on a thread nobody joins."""
    try:
        root = warm_root()
        # A mount-backed home is refused by the index anyway, so the warm
        # could only answer `covered: false` — after aiming kernel I/O at a
        # mount path, which is the one thing this codebase never does
        # speculatively. Exactly the check `runner.start` makes, and what it
        # guarantees is what matters here: a path INSIDE the mounts dir matches
        # on `abspath` alone and is refused before any syscall touches it. It
        # is not free for everyone else — a local home falls through to
        # `is_mount_backed`, which realpaths the mounts dir and the path — but
        # those two realpaths are off the mount by construction.
        if MountGuard(mounts_dir=runner._mounts_dir()).blocks_root(root):
            return
        cfg = load_config()
        out = index_search(cfg, root)
        if not out.get("covered"):
            run_id = _startup_runs.get(root)
            if run_id is not None:
                # Searching again is unconditional — how the wait ENDED does
                # not tell us whether the index covers the root. A run dir
                # pruned mid-wait reads as a dead scan here, but `prune_runs`
                # only removes run dirs and never touches the store, so the
                # index may well be complete. A still-uncovered index costs
                # one cheap `covered: false`, which is exactly what the
                # original single-shot warm already paid.
                _wait_for_scan(cfg, run_id)
                out = index_search(cfg, root)
        # Unconditional, including the uncovered cases: filter_corpus is a
        # no-op on a response that is not covered, and the point is to run
        # precisely what the route runs.
        filter_corpus(out, index_root=enclosing_root(scan_roots(cfg),
                                                     out.get("root") or root))
        # And the path the HOME search now takes, which is a different query
        # over the same index: /api/index/rank, verbatim, with a query that
        # matches something on essentially every machine. Same duckdb
        # connection cost, same gitignore pool, but its own two-stage SQL —
        # warming only the corpus would leave the home page's first keystroke
        # paying for the ranked plan.
        _ranked(cfg, out.get("root") or root, WARM_RANK_QUERY)
    except Exception:  # noqa: BLE001 - a warm must never take the server down
        logger.exception("could not warm the index search path")


def startup_warm() -> None:
    """The create_app hook. A detached daemon thread, not `to_thread`: the
    startup hook must COMPLETE before the app serves, and this is seconds of
    duckdb and `git check-ignore` — the very cost the warm exists to move off
    the request path — and, on a first boot, a bounded wait for the startup
    scan on top of that. Nobody joins it and it cannot raise (above); `daemon`
    is what guarantees a warm still waiting cannot hold the process open."""
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


@router.post("/api/index/scan-folder")
def api_index_scan_folder(body: dict = Body(default={}),
                          x_fused: str | None = Header(default=None)):
    """Cover a folder the index has never visited, because someone searched it.

    The in-folder search box used to answer an uncovered folder with a live
    streamed walk. That walk survives only for the folders no scan can ever
    reach, so this is what replaces it: the box asks, the scan runs, and the
    box polls `/api/index/rank` (reason `scanning`) until rows appear.

    THE FOLDER ITSELF is the scan root, not some enclosing configured one: a
    folder is uncovered precisely because no configured root covers it — or
    because one does and pruned it, in which case rescanning that root would
    prune it again. Compaction keeps every row outside the root it is given
    (index/store.py), so a folder-sized scan merges into the store instead of
    replacing it.

    Never an error, and every "no" is a durable one — this route is called
    from a search box, so a refusal it could read as transient becomes a
    keystroke-rate retry loop:

      * `refused` — `runner.start` said no. Mount-backed (structurally, and
        BEFORE any kernel syscall on the path) or simply not a directory.
      * `debounced` — scanned inside `SCAN_DEBOUNCE_S`. The startup
        scheduler's own floor, deliberately not a second one: a folder that is
        still uncovered after a scan (the ignore rules exclude it) must not be
        rescanned on the next keystroke, and the next one after that.
      * `joined` — a run over this root was already in flight; polling it is
        the whole of what a second one would have achieved.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = str(body.get("path") or "").strip()
    if not path:
        return _error("'path' is required")
    import time

    cfg = load_config()
    root = runner.canonical_root(path)
    last = runner.last_scan(cfg, root)
    if last is not None and (time.time() - last) < SCAN_DEBOUNCE_S:
        return {"ok": True, "started": False, "why": "debounced",
                "run_id": None, "root": root}
    try:
        started = runner.start(cfg, root)
    except ValueError as e:
        logger.info("index: not scanning %s on demand (%s)", root, e)
        return {"ok": True, "started": False, "why": "refused",
                "error": str(e), "run_id": None, "root": root}
    logger.info("index: scanning %s on demand (run %s)",
                root, started.get("run_id"))
    return {"ok": True, "started": True,
            "why": "joined" if started.get("already_running") else "started",
            "run_id": started.get("run_id"), "root": root}


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


# The one value of `fmt` that means anything. Anything else — including the
# empty default every existing caller sends — is the classic `entries` shape:
# the JS bridge (`fused.fileIndex.search`, static/runtime.js) and any page a
# user has written against it must not change under them, and a 400 on an
# unknown format would break exactly the callers that never asked.
COLUMNS_FMT = "columns"


@router.get("/api/index/search")
def api_index_search(root: str = Query(default=""), q: str = Query(default=""),
                     limit: int = Query(default=MAX_CORPUS),
                     fmt: str = Query(default=""),
                     accept_encoding: str | None = Header(default=None)):
    """The explorer's in-folder corpus, index-backed.

    Same entry shape as /api/fs/walk, plus `covered`/`fresh` so the client can
    decide whether to use it. A miss is `{covered: false, entries: []}` with a
    200 — never an error: the explorer falls back to the live walk, and a red
    search box during the first-boot scan would be a lie about a system that
    is working exactly as designed.

    Entries pass through the gitignore filter before leaving: the walk this
    corpus replaces prunes gitignored entries, and the swap must not change
    what search shows (server/index_gitignore.py).

    `fmt=columns` asks for the same corpus as parallel arrays instead of one
    object per entry (§6 of index/specs/server-api.md) — the home page's
    corpus is the whole ranking set, 25.7 MB on a 164k-entry home, and it is
    fetched in one shot on the user's first keystroke."""
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
    if fmt != COLUMNS_FMT:
        return {"ok": True, **out}
    return _corpus_response(_columnar({"ok": True, **out}), accept_encoding)


@router.get("/api/index/rank")
def api_index_rank(root: str = Query(default=""), q: str = Query(default=""),
                   limit: int = Query(default=RANK_LIMIT)):
    """The home search: filtered AND ranked here, top `limit` hits returned.

    The corpus route next door hands the client every entry under `root`
    (19.8 MB on a 164k-entry home, and silently capped so most of a big home
    could not be found at all) and lets the browser rank it. This answers a
    few KB — no columnar format and no gzip special-casing, because that
    machinery exists for the 20 MB corpus and this is not that.

    A miss is `{covered: false, hits: []}` with a 200, exactly as for the
    corpus: "no index yet", "not covered" and "a scan is running" are one
    condition to a search box.

    ...one condition, but not one CAUSE, and `reason` names it — `mount`,
    `package`, `uncovered`, `scanning`, or `""` when the index answered
    outright. The in-folder search picks its source from that field (see
    `_rank_reason`): the live streamed walk survives only for the folders no
    scan will ever cover, an uncovered one is scanned on demand, and a folder
    with a scan in flight is polled. Deciding it here is the point — the mount
    policy is `MountGuard`'s, and a second copy of it in the client would
    drift.

    `positions` are deliberately NOT returned. The client re-runs `fuzzyMatch`
    over the ~200 rows it gets back to build its highlights, so
    platform/lib/fuzzy.ts stays the single source of truth for what highlights
    — and the ranker here stays free to carry positions internally without
    them becoming a wire contract.
    """
    if not root.strip():
        return _error("'root' is required")
    cfg = load_config()
    out = _ranked(cfg, root, q, limit)
    out["hits"] = [{k: v for k, v in h.items() if k != "positions"}
                   for h in out["hits"]]
    out["reason"] = _rank_reason(cfg, root, out)
    return {"ok": True, **out}


def _columnar(out: dict) -> dict:
    """`entries` re-cut as parallel arrays; everything else untouched.

    Every entry carries the same four keys, so the classic shape spends ~40
    bytes per entry spelling `rel`/`is_dir`/`size`/`mtime` out again — a third
    of a 164k-entry corpus. The arrays are index-aligned and the same length;
    `size`/`mtime` stay nullable (a directory legitimately has neither) and
    `is_dir` travels as 0/1 because `false` costs three more bytes 164k times.

    Deliberately NOT a cleverer packing. Front-coding the rels (they arrive
    depth-then-path ordered, so neighbours share long prefixes) takes the body
    from 21 MB to 12 MB — but costs 0.45 s of Python per corpus against the
    0.07 s the whole serialization takes, so it spends more of the first
    search's budget than it saves. Compression buys more for a fraction of
    that (below)."""
    entries = out.get("entries") or []
    body = {k: v for k, v in out.items() if k != "entries"}
    body["fmt"] = COLUMNS_FMT
    body["rels"] = [e["rel"] for e in entries]
    body["dirs"] = [1 if e["is_dir"] else 0 for e in entries]
    body["sizes"] = [e["size"] for e in entries]
    body["mtimes"] = [e["mtime"] for e in entries]
    return body


def _corpus_response(body: dict, accept_encoding: str | None) -> Response:
    """The compact corpus, gzipped when the caller says it can take it.

    Level 1, not the default 9: measured on the 164k-entry corpus this route
    exists for, level 1 takes the compact body from 21 MB to 5.0 MB in 0.06 s
    — a fifth of the bytes for less CPU than the JSON encoding itself. Higher
    levels spend several seconds to shave a few percent off a body that is
    read once and thrown away.

    Per-route rather than a GZip middleware: this app also streams the live
    walk and serves file bytes raw, and compressing those on a LOCAL server
    would be CPU spent against loopback for nothing. This one response is the
    outlier — a single 25 MB read on a keystroke.

    `Accept-Encoding` is honoured rather than assumed: browsers and the JS
    bridge all send gzip, but a client that cannot decompress must still be
    able to read the corpus. `Vary` goes on both answers because both live at
    the same URL — an intermediary keyed on the URL alone would otherwise hand
    a gzipped body to the client that asked for identity, or the reverse."""
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = {"Vary": "Accept-Encoding"}
    if not _accepts_gzip(accept_encoding):
        return Response(content=payload, media_type="application/json",
                        headers=headers)
    return Response(content=gzip.compress(payload, 1),
                    media_type="application/json",
                    headers={**headers, "Content-Encoding": "gzip"})


def _accepts_gzip(accept_encoding: str | None) -> bool:
    """Whether the caller will take a gzipped body, per RFC 9110 §12.5.3.

    A substring test for "gzip" is not that test: `gzip;q=0` is the explicit
    spelling of "I cannot decode this", and reading it as consent hands such a
    client 5 MB it has no way to open. So the qvalue is parsed, an explicit
    `gzip` (or the legacy `x-gzip`) beats the `*` wildcard, and anything
    unparseable is treated as a refusal — being wrong the safe way costs a
    bigger body, not an unreadable one."""
    explicit = star = None
    for part in (accept_encoding or "").split(","):
        token, _, params = part.strip().partition(";")
        token = token.strip().lower()
        if token not in ("gzip", "x-gzip", "*"):
            continue
        q = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    q = float(value.strip())
                except ValueError:
                    q = 0.0
        if token == "*":
            star = q if star is None else max(star, q)
        else:
            explicit = q if explicit is None else max(explicit, q)
    q = explicit if explicit is not None else star
    return q is not None and q > 0


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
