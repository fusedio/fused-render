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
must be cheap, non-blocking, and never raise. Everything slow (the HEAD
for the size gate, the download) happens on a daemon worker thread; a
process-wide semaphore keeps at most one file prefetching at a time.

The serve surfaces transient store errors as HTTP 500s mid-stream
(observed on S3), so the worker fetches in ranged chunks and resumes with
backoff — ranges already cached replay from disk, so a retried chunk
costs nothing. A completed file is remembered for this server run only;
re-prefetching after a restart re-streams from the local cache in
seconds without touching the store.
"""
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
ENABLED = os.environ.get("FUSED_RENDER_PREFETCH", "1") != "0"

CHUNK_BYTES = 32 * 1024 * 1024
# Let the interactive load that triggered us win the first cold seeks.
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
_worker_slot = threading.Semaphore(1)


def status() -> dict:
    """Snapshot of all jobs (introspection/tests)."""
    with _lock:
        return {p: dict(j) for p, j in _jobs.items()}


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
        threading.Thread(target=_run, args=(path, url), daemon=True).start()
    except Exception:                                  # pragma: no cover
        logger.warning("prefetch schedule failed for %r", path, exc_info=True)


def _run(path: str, url: str) -> None:
    try:
        _prefetch(path, url)
    except Exception:
        logger.warning("prefetch of %r died", path, exc_info=True)
        _finish(path, "failed")


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


def _wait_for_lull(path: str) -> None:
    start = time.monotonic()
    while time.monotonic() - start < MAX_IDLE_HOLD_S:
        with _lock:
            last = _touched.get(path, 0.0)
        if time.monotonic() - last >= IDLE_WAIT_S:
            return
        time.sleep(0.5)


def _prefetch(path: str, url: str) -> None:
    time.sleep(START_DELAY_S)
    with _worker_slot:
        try:
            size = _head_size(url)
        except Exception as exc:
            _release(exc)
            _finish(path, "failed")
            return
        if size is None or size > MAX_BYTES:
            _finish(path, "skipped")
            return
        with _lock:
            job = _jobs.get(path)
            if job is not None:
                job.update(status="running", size=size)

        off, errors = 0, 0
        while off < size:
            _wait_for_lull(path)
            end = min(off + CHUNK_BYTES, size) - 1
            try:
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
                errors = 0
            except Exception as exc:
                _release(exc)
                errors += 1
                if errors > MAX_CONSECUTIVE_ERRORS:
                    logger.warning("prefetch of %r gave up at %d/%d bytes",
                                   path, off, size)
                    _finish(path, "failed")
                    return
                # Resume at `off`: everything fetched so far is already in
                # the serve's cache, so a re-request of it replays locally.
                time.sleep(min(2.0 * errors, 30.0))
        _finish(path, "done")
        logger.info("prefetched %r (%d bytes) into the serve cache",
                    path, size)
