"""Background whole-file prefetch for mount-backed files.

Cold analytical reads on a mounted file are latency-bound, not
bandwidth-bound: DuckDB's sorted/filtered scans issue hundreds of tiny
scattered range reads, and rclone's VFS cache serializes concurrent
uncached reads of one file at roughly one seek per half-second (measured:
a cold sort on a 644MB parquet moves ~3MB but takes ~30s, and a filtered
count pays the same again). The same serve streams sequential reads an
order of magnitude faster per byte (4.7-12MB/s measured). So: the first
time a mount-backed file is read at all, stream the whole thing through
the HTTP serve in the background and throw the bytes away. The side
effect is the point — rclone's vfs cache (see mounts.SERVE_VFS_OPT)
keeps a sparse on-disk copy of every range that passes through it, so
once the stream completes every scattered read the file will ever see is
a local disk hit. There is no second copy and no storage owned here: the
serve's LRU-capped cache is the store, it survives server restarts, and
rclone's fingerprint check invalidates it if the object changes upstream.

Triggered from the /api/fs/raw proxy (the one path every remote byte
already flows through — templates stay mount-agnostic), so scheduling
must be cheap, non-blocking, and never raise. `schedule()` just records
the job and submits a coroutine to a single dedicated prefetch event loop
(one background thread for the whole process, not one thread per file —
a zarr store's thousands of chunk reads would otherwise mint thousands of
threads). The blocking urllib calls run via asyncio.to_thread on that
loop's bounded shared executor.

Two phases with very different cost profiles, kept separate:
  1. Decide — a HEAD for the size gate. Cheap; runs immediately and
     concurrently for every scheduled file, so a sub-MIN_BYTES chunk is
     dismissed in one round trip without waiting behind anything.
  2. Stream — the whole-file download. Expensive; serialized by an
     asyncio semaphore (one file at a time: two big streams would evict
     each other from the LRU serve cache) and delayed by START_DELAY_S so
     the interactive read that triggered us wins its first cold seeks.

The serve surfaces transient store errors as HTTP 500s mid-stream
(observed on S3), so the worker fetches in ranged chunks and resumes with
backoff — ranges already cached replay from disk, so a retried chunk
costs nothing. A completed file is remembered for this server run only;
re-prefetching after a restart re-streams from the local cache in
seconds without touching the store.
"""
import asyncio
import logging
import os
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# Skip files larger than this: the serve cache is LRU-capped at 20Gi
# (SERVE_VFS_OPT) and one giant file would evict everything else. Beyond
# the cap the on-demand path still works exactly as before.
MAX_BYTES = int(os.environ.get("FUSED_RENDER_PREFETCH_MAX_BYTES",
                               1024 * 1024 * 1024))
# Skip files smaller than this. Prefetch only pays off for large,
# latency-bound files whose scattered reads dominate (see the module
# docstring): a zarr chunk or a tiny metadata object is read once, whole,
# and fast on-demand — streaming it through the serve to warm a cache the
# redirected client never reads back is pure waste, and one tracked job
# per chunk is how the maps below blow up on a store with thousands of
# them. Below the floor the on-demand path is already quick.
MIN_BYTES = int(os.environ.get("FUSED_RENDER_PREFETCH_MIN_BYTES",
                               8 * 1024 * 1024))
# Bound the in-memory maps: a store with thousands of tiny objects would
# otherwise mint a permanent entry per object for the life of the process.
# Only terminal jobs are evicted (oldest first); the MIN_BYTES floor keeps
# most chunk churn out of the maps entirely, so this is a backstop.
MAX_TRACKED = int(os.environ.get("FUSED_RENDER_PREFETCH_MAX_TRACKED", 2048))
ENABLED = os.environ.get("FUSED_RENDER_PREFETCH", "1") != "0"

CHUNK_BYTES = 32 * 1024 * 1024
# Applies to the DOWNLOAD phase only, not the size-gate HEAD: once a file
# clears the gate, let the interactive load that triggered us win its
# first cold seeks before the whole-file stream starts competing for the
# store's bandwidth.
START_DELAY_S = 5.0
# Between chunks, hold off while the file is being read interactively —
# but never indefinitely: prefetching IS the fix for those slow reads.
IDLE_WAIT_S = 2.0
MAX_IDLE_HOLD_S = 30.0
MAX_CONSECUTIVE_ERRORS = 30
FAILED_RETRY_COOLDOWN_S = 60.0

_lock = threading.Lock()
_jobs: dict = {}     # path -> {"status", "size", "done", "at"}
_touched: dict = {}  # path -> monotonic time of last interactive access

# Single dedicated event loop for all prefetch work, spun up lazily on the
# first schedule() and run on one daemon thread. Keeps schedule() a plain
# sync call usable from anywhere (request handlers, tests) while the actual
# I/O runs as coroutines off the caller's thread.
_loop_lock = threading.Lock()
_loop: "asyncio.AbstractEventLoop | None" = None
# Serializes the download phase (created on the loop; see _acquire_slot).
_download_slot: "asyncio.Semaphore | None" = None


def status() -> dict:
    """Snapshot of all jobs (introspection/tests)."""
    with _lock:
        return {p: dict(j) for p, j in _jobs.items()}


def _evict_locked() -> None:
    """Drop oldest terminal jobs (and their touch times) once the maps
    exceed MAX_TRACKED. Caller must hold `_lock`. Queued/running jobs are
    never evicted — losing one would strand an in-flight download's
    status; a completed job only means a slightly slower next read (it
    re-prefetches from the local cache in seconds, see docstring)."""
    if len(_jobs) <= MAX_TRACKED:
        return
    terminal = sorted(
        (j["at"], p) for p, j in _jobs.items()
        if j["status"] in ("done", "skipped", "failed"))
    for _, p in terminal:
        if len(_jobs) <= MAX_TRACKED:
            break
        _jobs.pop(p, None)
        _touched.pop(p, None)


def is_done(path: str) -> bool:
    """True once `path` has been fully streamed into the serve's cache this
    server run. The raw proxy uses this to route cold ranged reads straight
    to the store (mounts.upstream_url_for) and warm ones to the caching
    serve, whose sparse cache replays them from disk."""
    with _lock:
        job = _jobs.get(path)
        return bool(job and job["status"] == "done")


def _ensure_loop() -> "asyncio.AbstractEventLoop":
    """Start (once) the background prefetch event loop and return it."""
    global _loop
    if _loop is not None:
        return _loop
    with _loop_lock:
        if _loop is None:
            loop = asyncio.new_event_loop()
            threading.Thread(target=loop.run_forever, daemon=True,
                             name="prefetch-loop").start()
            _loop = loop
    return _loop


def schedule(path: str, url: str) -> None:
    """Note that `path` (served at `url`) is being read; start a background
    prefetch unless one already ran. Called on the raw-proxy hot path:
    cheap, non-blocking, never raises."""
    try:
        if not ENABLED:
            return
        now = time.monotonic()
        with _lock:
            _touched[path] = now
            job = _jobs.get(path)
            if job is not None:
                retry = (job["status"] == "failed"
                         and now - job["at"] >= FAILED_RETRY_COOLDOWN_S)
                if not retry:
                    return
            _jobs[path] = {"status": "queued", "size": None, "done": 0,
                           "at": now}
            _evict_locked()
        asyncio.run_coroutine_threadsafe(_prefetch(path, url), _ensure_loop())
    except Exception:                                  # pragma: no cover
        logger.warning("prefetch schedule failed for %r", path, exc_info=True)


def _finish(path: str, status_: str) -> None:
    with _lock:
        job = _jobs.get(path)
        if job is not None:
            job["status"] = status_
            job["at"] = time.monotonic()


def _release(exc: BaseException) -> None:
    """Close the response an HTTPError carries. Raised before the `with`
    block takes ownership, it would otherwise hold its serve connection
    open for as long as the exception stays referenced — which in the
    retry loop below spans the whole backoff sleep."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            exc.close()
        except Exception:
            pass


def _head_size(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=30) as r:
        cl = r.headers.get("Content-Length")
        return int(cl) if cl is not None else None


def _fetch_chunk(url: str, off: int, end: int, path: str) -> int:
    """Fetch bytes [off, end] through the serve and discard them (the point
    is the serve's cache side effect); update the job's progress as bytes
    land. Returns the new offset. Blocking — run via asyncio.to_thread."""
    req = urllib.request.Request(url)
    req.add_header("Range", f"bytes={off}-{end}")
    with urllib.request.urlopen(req, timeout=120) as r:
        while True:
            b = r.read(1024 * 1024)
            if not b:
                break
            off += len(b)
            with _lock:
                if path in _jobs:
                    _jobs[path]["done"] = off
    return off


async def _acquire_slot() -> "asyncio.Semaphore":
    """The download semaphore, created lazily on the loop thread (so it
    binds to this loop). Runs single-threaded here, so no lock needed."""
    global _download_slot
    if _download_slot is None:
        _download_slot = asyncio.Semaphore(1)
    return _download_slot


async def _wait_for_lull(path: str) -> None:
    start = time.monotonic()
    while time.monotonic() - start < MAX_IDLE_HOLD_S:
        with _lock:
            last = _touched.get(path, 0.0)
        if time.monotonic() - last >= IDLE_WAIT_S:
            return
        await asyncio.sleep(0.5)


async def _prefetch(path: str, url: str) -> None:
    try:
        # Phase 1 — decide. No delay, no download slot: the HEAD is cheap
        # and gates whether we bother at all, so it runs immediately and
        # concurrently with every other scheduled file's HEAD.
        try:
            size = await asyncio.to_thread(_head_size, url)
        except Exception as exc:
            _release(exc)
            _finish(path, "failed")
            return
        if size is None or not (MIN_BYTES <= size <= MAX_BYTES):
            _finish(path, "skipped")
            return
        with _lock:
            job = _jobs.get(path)
            if job is not None:
                job.update(status="running", size=size)

        # Phase 2 — stream. Expensive, so one file at a time, and only
        # after START_DELAY_S so the triggering interactive read gets ahead.
        await asyncio.sleep(START_DELAY_S)
        async with await _acquire_slot():
            off, errors = 0, 0
            while off < size:
                await _wait_for_lull(path)
                end = min(off + CHUNK_BYTES, size) - 1
                try:
                    off = await asyncio.to_thread(
                        _fetch_chunk, url, off, end, path)
                    errors = 0
                except Exception as exc:
                    _release(exc)
                    errors += 1
                    if errors > MAX_CONSECUTIVE_ERRORS:
                        logger.warning("prefetch of %r gave up at %d/%d bytes",
                                       path, off, size)
                        _finish(path, "failed")
                        return
                    # Resume at `off`: everything fetched so far is already
                    # in the serve's cache, so re-requesting it replays
                    # locally.
                    await asyncio.sleep(min(2.0 * errors, 30.0))
            _finish(path, "done")
            logger.info("prefetched %r (%d bytes) into the serve cache",
                        path, size)
    except Exception:
        logger.warning("prefetch of %r died", path, exc_info=True)
        _finish(path, "failed")
