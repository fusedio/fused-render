"""The app call log: one structured record per API call a page makes.

A page's calls through the injected runtime (`fused.runPython`, `stat`,
`readFile`, `writeFile`) are invisible today — a failure flashes the D17
overlay, `print()` lands in the browser console, and nothing accumulates. This
module is the durable half: an append-only JSONL store under
``~/.fused-render/calls/`` holding one bounded record per call, so "why is this
page slow", "what did my app just do", and "did it error when the user opened
it" have answers that survive a reload. Read back by the `calls` view template
(templates/calls/) and the ``fused-render calls`` CLI; see
docs/CALL_LOG_DESIGN.md for the design and its rationale.

**Where the write happens (design §4.5).** Six levels can see a call; the
record is written at the ASGI middleware, enriched in place by route handlers,
and annotated by headers from runtime.js:

  * ``begin(request)`` — the middleware, on the way in. Returns a mutable
    record dict, or None when this is not an app call (no ``X-Fused-Page``
    header — so the shell's own /api/fs/list, the conditions probe, and every
    non-runtime caller are excluded by construction) or when logging is off.
    Stashed on ``request.state.fused_call`` for handlers to enrich.
  * ``enrich_run`` / ``enrich_write`` — route handlers, mutating that dict.
    They never write. A handler that enriches nothing still yields a valid thin
    record, so a new endpoint is logged by default rather than by remembering
    to instrument it.
  * ``finish(...)`` — the middleware, on the way out: status, ``server_ms``,
    outcome, then the single ``record()`` call.

``record()`` is the ONLY writer of HTTP-call records, and it only does a
``put_nowait`` onto a bounded queue — the background writer thread does the
append, so nothing on the request path touches the filesystem. The one
deliberate carve-out is ``record_page_error()``: a page-level JS error is not
an HTTP call (that is the whole point — it is what happened INSTEAD of one), so
it comes from the page via POST /api/calls/event and is recorded directly.

**Fail-open is normative.** Logging must never fail, or meaningfully delay, the
thing it observes. An unwritable directory, a full queue, a value that won't
serialize — each drops the record and counts the drop; none may touch the
response. Bounds are structural and independent: per-record caps, a per-page
rate cap, and retention by both age and directory size.

No import of server.py (server imports this module — keep it acyclic); the
X-Fused guard is duplicated locally like shell/bookmarks.py's is.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Header, Request
from fastapi.responses import JSONResponse

from fused_render.shell import storage

logger = logging.getLogger(__name__)

router = APIRouter()

RECORD_VERSION = 1

# Attribution + correlation headers set by static/runtime.js. X-Fused-Page is
# what makes a request an "app call" at all; the other two are additive context.
PAGE_HEADER = "x-fused-page"
CALL_HEADER = "x-fused-call"
TARGET_HEADER = "x-fused-target"

# Caps, verbatim from the serve plane's error record (the fused repo's
# spec/serve/error-reporting.md §1.2) so a local record and a deployed one carry
# the same bounds and render in one viewer. Truncation is MARKED in the record,
# never silently grown.
PARAMS_CAP = 2_048
OUTPUT_CAP = 4_096
ERROR_CAP = 16_384
RECORD_CAP = 32_768

# Bounded queue: a burst longer than this drops records rather than growing
# memory or blocking a run. Sized for a slider scrub (which can issue hundreds
# of calls a second) plus headroom.
QUEUE_MAX = 2_048

# Per-page token bucket: a runaway render loop must not fill the disk. Refills
# continuously at RATE_PER_MIN/60 per second up to RATE_BURST.
RATE_PER_MIN = 600
RATE_BURST = 200
# Above this many tracked pages, evict the ones whose bucket has fully refilled
# (see _rate_ok) — the dict is keyed by page path and would otherwise grow for
# the whole life of a long browsing session.
BUCKETS_MAX = 512

# Retention: age first, then a directory-size backstop. D68 chose the system
# temp dir for the app log precisely because "nothing prunes the directory";
# this store is durable instead, so the pruning is code.
DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_BYTES = 200 * 1024 * 1024
RETENTION_DAYS_ENV = "FUSED_RENDER_CALLS_RETENTION_DAYS"
DISABLE_ENV = "FUSED_RENDER_CALLS"

# Per-FILE cap, rolled to a new part when exceeded. Without it a single day's
# file grows without bound: the directory cap (above) can only delete whole
# files, and it must never delete a live one — so a burst inside one day had
# nothing standing in its way at all. logs.py rotates the app log for the same
# reason; this is the call log's equivalent.
MAX_FILE_BYTES = 32 * 1024 * 1024

_SUFFIX = ".calls.jsonl"

# The calls view's own reader. Reading the log must not append to the log:
# beyond being noise, the view POLLS while following, so each poll's four reader
# calls would appear in the next poll's results — a feedback loop that inflates
# the counts it reports forever, and makes the viewer the busiest "target" in
# any page's rollup. Same instinct as the middleware skipping /api/calls and
# D68's access log skipping the static mounts.
#
# Matched by SHAPE (`<...>/calls/reader.py`) rather than by one absolute path,
# because the same reader legitimately runs from three places: the package dir,
# the staged core copy the executor actually resolves
# (~/.fused-render/.core-templates/, core_templates.py), and a user's fork under
# ~/.fused-render/templates/. Pinning one path would silently miss the other two
# — including the staged copy, which is the one that normally runs.
_SELF_READER_NAME = "reader.py"
_SELF_READER_DIR = "calls"

# Never log the log's own endpoints: the page-error POST and the reader's own
# reads would otherwise appear as app calls and (worse) a burst of them would
# spend the rate budget that the calls they describe need.
SKIP_PREFIXES = ("/api/calls",)


# --------------------------------------------------------------- store layout

def store_dir() -> str:
    """Directory holding the JSONL files, under the branch-aware shell home."""
    return os.path.join(storage.home_dir(), "calls")


def day_stamp(when: float | None = None) -> str:
    """The UTC date segment that names a file: ``YYYY-MM-DD``."""
    stamp = datetime.fromtimestamp(when if when is not None else time.time(), timezone.utc)
    return stamp.strftime("%Y-%m-%d")


def day_file(when: float | None = None, part: int = 1) -> str:
    """This process's file: ``<date>-<pid>-<part>.calls.jsonl``.

    Per-pid like logs.py's log_path(), and for the same reason: two live
    servers (two ports, or the desktop app beside a CLI) would otherwise
    interleave lines into one file. The reader globs the day, so a split file
    set costs nothing on the read side.

    ``part`` is the within-day roll (MAX_FILE_BYTES). Zero-padded so a plain
    name sort stays chronological — date, then pid, then part — which the
    oldest-first size trim depends on. The ``.calls.jsonl`` compound suffix is
    what the template registry binds (CT-3 specificity beats a bare ``.jsonl``).
    """
    return os.path.join(
        store_dir(), f"{day_stamp(when)}-{os.getpid()}-{part:03d}{_SUFFIX}")


def current_file() -> str:
    """The file to append to now: the highest existing part for today+pid, rolled
    to the next part once it passes MAX_FILE_BYTES."""
    part = 1
    while part < 1000:
        path = day_file(part=part)
        try:
            if os.path.getsize(path) < MAX_FILE_BYTES:
                return path
        except OSError:
            return path  # absent -> this is the one to create
        part += 1
    return day_file(part=999)  # pathological; keep writing rather than lose records


def store_files() -> list[str]:
    """Every JSONL file in the store, oldest name first (so date order)."""
    try:
        names = sorted(n for n in os.listdir(store_dir()) if n.endswith(_SUFFIX))
    except OSError:
        return []
    return [os.path.join(store_dir(), n) for n in names]


def retention_days() -> int:
    raw = os.environ.get(RETENTION_DAYS_ENV)
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return _prefs_snapshot()[2]


# The prefs store is read per call — three times, between `enabled()` and the
# param-redaction mode — and each read opens and parses prefs.json. On the hot
# path that measured ~2.8 ms per run, most of the logging overhead and all of it
# avoidable. A 1 s TTL keeps the repo's no-restart posture (CT-5: a preference
# change applies to the next run, not the next launch) while making the common
# case a dict lookup. Deliberately not a permanent cache: the switch must still
# take effect on its own.
_PREFS_TTL_S = 1.0
_prefs_cache: tuple[float, bool, str, int] | None = None
_prefs_cache_lock = threading.Lock()


def _prefs_snapshot() -> tuple[bool, str, int]:
    """(enabled, params_mode, retention_days), re-read at most once a second."""
    global _prefs_cache
    now = time.monotonic()
    with _prefs_cache_lock:
        if _prefs_cache is not None and now - _prefs_cache[0] < _PREFS_TTL_S:
            return _prefs_cache[1], _prefs_cache[2], _prefs_cache[3]
    from fused_render.shell import prefs as shell_prefs

    snapshot = (shell_prefs.calls_enabled(), shell_prefs.calls_params_mode(),
                shell_prefs.calls_retention_days())
    with _prefs_cache_lock:
        _prefs_cache = (now, *snapshot)
    return snapshot


def invalidate_prefs_cache() -> None:
    """Drop the cached snapshot — called when the prefs endpoint writes."""
    global _prefs_cache
    with _prefs_cache_lock:
        _prefs_cache = None


def enabled() -> bool:
    """Whether capture is on. ``FUSED_RENDER_CALLS=0`` is the process-level
    off switch (tests, a user who wants nothing recorded); otherwise the pref
    decides, defaulting to on."""
    raw = os.environ.get(DISABLE_ENV)
    if raw is not None:
        return raw.strip().lower() not in ("0", "false", "no", "off")
    return _prefs_snapshot()[0]


# ---------------------------------------------------------------------- caps

def _cap_text(value, limit: int) -> tuple[str | None, bool]:
    """Cap a string to ``limit`` bytes, keeping the TAIL.

    Tail, not head: the last lines of stdout and the bottom of a traceback are
    where the failure is. Returns (value, truncated).
    """
    if not value:
        return (None, False) if value is None or value == "" else (str(value), False)
    text = value if isinstance(value, str) else str(value)
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text, False
    # Decode with errors="replace" so slicing mid-codepoint can't raise.
    return raw[-limit:].decode("utf-8", "replace"), True


def _cap_params(params) -> tuple[object, bool]:
    """Cap params to PARAMS_CAP of serialized JSON, honouring the redaction pref.

    ``keys`` records the key names but no values — enough to tell one call
    shape from another without persisting what may be a secret. ``off`` records
    nothing. Default is full: params are the inputs the author's own code
    already received and are usually the whole repro (the same named trade-off
    the serve spec makes), and locally they are already sitting in the URL bar.
    """
    if not isinstance(params, dict) or not params:
        return None, False
    mode = _prefs_snapshot()[1]
    if mode == "off":
        return None, False
    if mode == "keys":
        return sorted(params.keys()), False
    try:
        encoded = json.dumps(params, default=str)
    except (TypeError, ValueError):
        return sorted(params.keys()), True
    if len(encoded.encode("utf-8", "replace")) <= PARAMS_CAP:
        return params, False
    # Over budget: keep the key names (the shape) rather than an arbitrary
    # subset of values, which would read as a complete param set and mislead.
    return sorted(params.keys()), True


def _result_shape(result) -> dict:
    """`result_kind` / `result_rows` for a run's return value.

    ``result_rows`` prefers a real row count: len() for a list, else the common
    reader envelope's ``rows``/``total_rows`` (templates/table, csv, xlsx,
    duckdb all return one of these), so the size chart can be read in rows and
    not just bytes.
    """
    if result is None:
        return {"result_kind": "null", "result_rows": None}
    if isinstance(result, bool):
        return {"result_kind": "bool", "result_rows": None}
    if isinstance(result, (int, float)):
        return {"result_kind": "number", "result_rows": None}
    if isinstance(result, str):
        return {"result_kind": "str", "result_rows": None}
    if isinstance(result, list):
        return {"result_kind": "list", "result_rows": len(result)}
    if isinstance(result, dict):
        rows = None
        for key in ("total_rows", "rows"):
            value = result.get(key)
            if isinstance(value, int):
                rows = value
                break
            if isinstance(value, list):
                rows = len(value)
                break
        return {"result_kind": "dict", "result_rows": rows}
    return {"result_kind": type(result).__name__, "result_rows": None}


# --------------------------------------------------------------- attribution

_templates_dirs_cache: tuple[str, tuple[str, ...]] | None = None


def _templates_dirs() -> tuple[str, ...]:
    """Every directory a TEMPLATE page can be served from.

    Three, not one: the packaged set, the staged core copy the server actually
    reads (~/.fused-render/.core-templates — core_templates.py), and the user
    override channel. Missing the staged dir would flag every built-in
    template's calls as the user's own work, which is exactly backwards for the
    "My pages" filter.
    """
    # Cached, keyed by the shell home (which a test or FUSED_RENDER_HOME can
    # change): resolving these per call cost three realpath() syscalls on the
    # hot path for values that never move within a process.
    global _templates_dirs_cache
    home = storage.home_dir()
    if _templates_dirs_cache is not None and _templates_dirs_cache[0] == home:
        return _templates_dirs_cache[1]
    package = os.path.realpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"))
    user = os.path.realpath(os.path.join(home, "templates"))
    dirs: tuple[str, ...]
    try:
        from fused_render.core_templates import core_templates_dir

        dirs = (package, user, os.path.realpath(core_templates_dir()))
    except (ImportError, OSError):
        dirs = (package, user)
    _templates_dirs_cache = (home, dirs)
    return dirs


def _is_self_read(resolved: str | None) -> bool:
    """True when this run IS the calls view reading the log (see _SELF_READER_*).

    Shape match, so the packaged reader, the staged core copy that actually
    runs, and a user's fork are all covered.
    """
    if not resolved:
        return False
    try:
        real = os.path.realpath(resolved)
    except OSError:
        return False
    parts = real.replace("\\", "/").split("/")
    return (len(parts) >= 2
            and parts[-1] == _SELF_READER_NAME
            and parts[-2] == _SELF_READER_DIR)


def is_first_party(page: str | None) -> bool:
    """True when ``page`` is a shipped or user TEMPLATE, not the user's own app.

    Template readers ride the same runtime and the same /api/run — previewing a
    parquet really does make the duckdb template call Python — so those calls
    are real records attributed to the template's own template.html. Correct,
    but "my app's calls" then needs a deliberate filter rather than an
    accident: this flag is it (design §4.6).
    """
    if not page:
        return False
    try:
        real = os.path.realpath(page)
    except OSError:
        return False
    for base in _templates_dirs():
        try:
            if os.path.commonpath([real, base]) == base:
                return True
        except (OSError, ValueError):
            continue
    return False


# ------------------------------------------------------ superseded reporting

# Call ids the PAGE has reported as abandoned (D114 latest-wins cancellation).
# The server cannot infer this: a client abort does not raise into the handler —
# uvicorn runs it to completion and the middleware would record an ordinary
# success — so the page is the only source of truth, and this is where its
# report waits for the still-running call to finish. That ordering is what makes
# the whole approach work: the client knows at abort time, typically ~1s before
# the handler returns, so the mark is nearly always present when finish() writes
# the record, and no patching of an append-only file is needed.
#
# Bounded two ways: a TTL, because a page that reports and then navigates away
# would otherwise leave ids resident forever, and a hard count, because a
# runaway page must not grow this without limit.
_SUPERSEDED: dict[str, float] = {}
_SUPERSEDED_TTL_S = 300.0
_SUPERSEDED_MAX = 4096
_superseded_lock = threading.Lock()


def mark_superseded(call_ids: list) -> int:
    """Remember that the page abandoned these calls; returns how many were taken."""
    now = time.monotonic()
    with _superseded_lock:
        for known, seen in list(_SUPERSEDED.items()):
            if now - seen > _SUPERSEDED_TTL_S:
                del _SUPERSEDED[known]
        added = 0
        for call_id in call_ids:
            if isinstance(call_id, str) and call_id:
                _SUPERSEDED[call_id] = now
                added += 1
        while len(_SUPERSEDED) > _SUPERSEDED_MAX:
            _SUPERSEDED.pop(next(iter(_SUPERSEDED)))
    return added


def _take_superseded(call_id: str | None) -> bool:
    """Consume a pending mark — once, so an id can never stamp two records."""
    if not call_id:
        return False
    with _superseded_lock:
        return _SUPERSEDED.pop(call_id, None) is not None


# ------------------------------------------------------------- the write path

_queue: queue.Queue | None = None
_writer: threading.Thread | None = None
_lock = threading.Lock()
_dropped = 0
_buckets: dict[str, tuple[float, float]] = {}  # page -> (tokens, last refill)
_buckets_lock = threading.Lock()


def _rate_ok(page: str) -> bool:
    """Token bucket per page. Overflow drops the record, never the response."""
    now = time.monotonic()
    with _buckets_lock:
        tokens, last = _buckets.get(page, (float(RATE_BURST), now))
        tokens = min(float(RATE_BURST), tokens + (now - last) * (RATE_PER_MIN / 60.0))
        # Bounded: one entry per page visited, so a long session browsing many
        # files would otherwise grow it for the life of the process.
        #
        # Preference order matters. A bucket idle long enough to have refilled
        # completely carries no state worth keeping — dropping it is equivalent
        # to never having seen that page — so those go first and the cap costs
        # nothing. But a BURST of many distinct pages has no idle buckets at all
        # (the first version of this evicted nothing and grew unbounded anyway),
        # so past the cap the least-recently-seen entries go too. That can only
        # ever hand a page MORE budget than it had, never less, so a page cannot
        # be silenced by another page's churn.
        if len(_buckets) > BUCKETS_MAX:
            full_after = RATE_BURST / (RATE_PER_MIN / 60.0)
            for known, (_, seen) in list(_buckets.items()):
                if now - seen > full_after and known != page:
                    del _buckets[known]
            if len(_buckets) > BUCKETS_MAX:
                stale_first = sorted(_buckets.items(), key=lambda kv: kv[1][1])
                for known, _ in stale_first[: len(_buckets) - BUCKETS_MAX]:
                    if known != page:
                        del _buckets[known]
        if tokens < 1.0:
            _buckets[page] = (tokens, now)
            return False
        _buckets[page] = (tokens - 1.0, now)
        return True


def _ensure_writer() -> queue.Queue | None:
    """Lazily start the writer thread; None when it cannot be started.

    Lazy so importing this module (or running a server nobody uses) costs
    nothing, and so tests that never record never spawn a thread. Daemon so a
    pending flush can never hold the process open — a lost tail of diagnostic
    records at exit is strictly better than a server that will not quit.
    """
    global _queue, _writer
    with _lock:
        if _writer is not None and _writer.is_alive():
            return _queue
        _queue = queue.Queue(maxsize=QUEUE_MAX)
        _writer = threading.Thread(target=_writer_loop, args=(_queue,),
                                   name="fused-render-calls", daemon=True)
        _writer.start()
        return _queue


def _writer_loop(q: queue.Queue) -> None:
    """Drain the queue onto disk, one JSON line per record.

    Coalesces whatever is already queued into a single open/append/close, so a
    burst costs one write rather than one per record. Every failure mode here
    is swallowed to a WARNING: this thread must never die, because a dead
    writer would silently stop logging while the caller kept queueing.
    """
    _sweep_safely()
    last_sweep = time.monotonic()
    last_day = day_stamp()
    while True:
        try:
            first = q.get()
        except (OSError, ValueError):  # pragma: no cover - queue is process-local
            return
        batch = [first]
        while len(batch) < 256:
            try:
                batch.append(q.get_nowait())
            except queue.Empty:
                break
        try:
            _append(batch)
        except OSError as e:
            logger.warning("call log: could not write %d record(s): %s", len(batch), e)
        # Retention runs on the writer thread (never a request). On a day roll as
        # well as the 24h timer: a server running across midnight should prune the
        # newly-expired day promptly, and the roll is the natural hook — the
        # writer has just moved to a new file.
        today = day_stamp()
        if today != last_day or time.monotonic() - last_sweep > 86_400:
            last_day = today
            _sweep_safely()
            last_sweep = time.monotonic()


def _prune(rec: dict) -> dict:
    """Drop null-valued keys before serializing.

    A record carries every field the widest call could need, so a narrow one (a
    `stat`, a raw read) was mostly `null`s — wasteful, and unreadable when the
    raw JSONL is what you are looking at.

    It also stops a *successful* record from containing the literal text
    `error`. Generic log viewers infer a level by sniffing for level words
    anywhere in the line, so the field NAME `"error": null` made every healthy
    call render as ERROR in `log_studio` (which the registry offers for this
    file). With nulls dropped, only a record that really failed carries the
    word, and the inference is right by accident rather than wrong by accident.

    Absent-means-null is safe for every consumer here: the reader, the CLI and
    the template all read through `.get()`/`?? undefined`, and the sidecar
    precedent (HV-6) already says writers may grow records additively.
    """
    return {key: value for key, value in rec.items() if value is not None}


def _append(records: list[dict]) -> None:
    path = current_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    for rec in records:
        try:
            line = json.dumps(_prune(rec), default=str)
        except (TypeError, ValueError) as e:  # pragma: no cover - default=str is total
            logger.warning("call log: unserializable record dropped: %s", e)
            continue
        if len(line.encode("utf-8", "replace")) > RECORD_CAP:
            line = json.dumps(_shrink(rec), default=str)
        lines.append(line)
    if not lines:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _shrink(rec: dict) -> dict:
    """Last-resort trim for a record that is still over RECORD_CAP after the
    per-field caps — drop the big optional text, keep the skeleton, and say so.
    A record that names its own truncation is worth far more than none."""
    out = dict(rec)
    for key in ("stdout_tail", "stderr_tail", "params"):
        out[key] = None
    error = out.get("error")
    if isinstance(error, dict):
        capped, _ = _cap_text(error.get("traceback"), 2_048)
        out["error"] = {**error, "traceback": capped}
    out["truncated"] = True
    return out


def record(rec: dict) -> None:
    """Queue one record. Never raises, never blocks, never touches the disk."""
    global _dropped
    page = rec.get("page") or ""
    if page and not _rate_ok(page):
        _dropped += 1
        logger.warning("call log: rate cap hit for %s; record dropped", page)
        return
    q = _ensure_writer()
    if q is None:  # pragma: no cover - thread start failure
        return
    try:
        q.put_nowait(rec)
    except queue.Full:
        _dropped += 1
        logger.warning("call log: queue full; record dropped (%d total)", _dropped)


def dropped_count() -> int:
    return _dropped


# ------------------------------------------------------------------ retention

def _sweep_safely() -> None:
    try:
        sweep()
    except OSError as e:
        logger.warning("call log: retention sweep failed: %s", e)


def sweep(now: float | None = None) -> int:
    """Delete files older than the retention window, then trim oldest-first
    while the directory exceeds the size cap. Returns the number removed.

    Two independent bounds on purpose: age answers "don't keep my activity
    forever", size answers "don't fill my disk today" — one busy afternoon can
    breach the second without coming near the first.
    """
    files = store_files()
    if not files:
        return 0
    removed = 0
    days = retention_days()
    if days > 0:
        cutoff = (now if now is not None else time.time()) - days * 86_400
        for path in list(files):
            try:
                if os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    files.remove(path)
                    removed += 1
            except OSError:
                continue
    total = 0
    sizes = []
    for path in files:
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        sizes.append((path, size))
        total += size

    # A file dated TODAY may be open for append — by this process or by another
    # server on another port — and deleting it would silently discard the whole
    # day's records (the writer just recreates it on its next batch). Trimming is
    # whole-file, so today's files are simply not candidates; MAX_FILE_BYTES is
    # what bounds growth inside a day.
    today = day_stamp(now if now is not None else time.time())
    for path, size in sizes:  # oldest first (store_files is name-sorted)
        if total <= DEFAULT_MAX_BYTES:
            break
        if os.path.basename(path).startswith(today):
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        total -= size
        removed += 1
    if total > DEFAULT_MAX_BYTES:
        # Everything left is today's. Say so rather than pretending the cap held.
        logger.warning(
            "call log: %.1f MB exceeds the %.0f MB cap but only today's files "
            "remain (live; not deleted) — per-file rolling caps each at %.0f MB",
            total / 1e6, DEFAULT_MAX_BYTES / 1e6, MAX_FILE_BYTES / 1e6)
    return removed


# ------------------------------------------- middleware / handler integration

def begin(request: Request) -> dict | None:
    """Start a record for an app call, or return None when this isn't one.

    Called by the middleware on the way in. The ``X-Fused-Page`` header is the
    whole test: only static/runtime.js sends it, so shell-issued requests and
    anything else hitting the API are excluded by construction rather than by
    an endpoint blocklist that would drift.
    """
    path = request.url.path
    if path.startswith(SKIP_PREFIXES):
        return None
    page = request.headers.get(PAGE_HEADER)
    if not page:
        return None
    if not enabled():
        return None
    # A read route names the file it touched in its `path` query param
    # (/api/fs/stat, /api/fs/raw). Seeding `entrypoint` from it means the
    # per-target rollup can say "this page stat'd that file 400 times" instead
    # of collapsing every read into one "/api/fs/stat" row — which is the shape
    # the stat-in-a-loop bug actually shows up in. /api/run and /api/fs/write
    # overwrite it with their resolved target when they enrich.
    touched = request.query_params.get("path") if request.method == "GET" else None
    return {
        "version": RECORD_VERSION,
        "call_id": request.headers.get(CALL_HEADER) or uuid.uuid4().hex,
        "kind": "call",
        "occurred_at": _now_iso(),
        "page": page,
        "target_file": request.headers.get(TARGET_HEADER) or None,
        "first_party": is_first_party(page),
        "route": path,
        "http_method": request.method,
        "status": None,
        "entrypoint": touched,
        "entrypoint_name": os.path.basename(touched) if touched else None,
        "engine": None,
        "outcome": None,
        "server_ms": None,
        "params": None,
        "result_bytes": None,
        "result_kind": None,
        "result_rows": None,
        "stdout_tail": None,
        "stderr_tail": None,
        "error": None,
        "err_id": None,
        "truncated": False,
    }


def enrich_run(call: dict | None, *, resolved: str, params: dict, engine: str,
               result: dict) -> None:
    """Add /api/run's detail to the in-flight record (the handler never writes).

    This is where the log earns its keep: the resolved .py, the params that
    produced this run, the engine that ran it, and — on failure — the traceback
    and the output tails a user has since clicked away from.
    """
    if call is None:
        return
    if _is_self_read(resolved):
        # Reading the log is not an app call worth logging (see _SELF_READER).
        # Flagged rather than dropped here so finish() stays the single writer.
        call["_drop"] = True
        return
    call["entrypoint"] = resolved
    call["entrypoint_name"] = os.path.basename(resolved) if resolved else None
    call["engine"] = engine
    capped_params, params_truncated = _cap_params(params)
    call["params"] = capped_params
    stdout, out_truncated = _cap_text(result.get("stdout"), OUTPUT_CAP)
    stderr, err_truncated = _cap_text(result.get("stderr"), OUTPUT_CAP)
    call["stdout_tail"] = stdout
    call["stderr_tail"] = stderr
    truncated = params_truncated or out_truncated or err_truncated
    if params_truncated:
        call["params_truncated"] = True

    duration = result.get("duration_ms")
    if isinstance(duration, (int, float)):
        # The engine's own measurement of the run itself, which excludes the
        # request plumbing server_ms includes. Both are kept: the gap between
        # them is queueing and serialization.
        call["run_ms"] = round(float(duration))

    if result.get("ok"):
        call["outcome"] = "ok"
        call.update(_result_shape(result.get("result")))
    else:
        call["outcome"] = "error"
        error = result.get("error") or {}
        traceback_text, tb_truncated = _cap_text(error.get("traceback"), ERROR_CAP)
        message, msg_truncated = _cap_text(error.get("message"), OUTPUT_CAP)
        call["error"] = {
            "type": error.get("type") or "Error",
            "message": message,
            "traceback": traceback_text,
        }
        truncated = truncated or tb_truncated or msg_truncated
    call["truncated"] = bool(call.get("truncated")) or truncated


def enrich_write(call: dict | None, *, path: str, content: str | None,
                 status: int) -> None:
    """Add /api/fs/write's detail: what was written and how big.

    The path and the byte count only — never the content. "What did my app just
    overwrite" is a real question; keeping a copy of every save is not an
    answer to it.
    """
    if call is None:
        return
    call["entrypoint"] = path
    call["entrypoint_name"] = os.path.basename(path) if path else None
    call["bytes_written"] = len(content.encode("utf-8", "replace")) if isinstance(content, str) else None
    if status == 409:
        call["outcome"] = "conflict"
    elif status == 403:
        call["outcome"] = "readonly"


def finish(call: dict | None, *, status: int | None, elapsed_ms: float,
           outcome: str | None = None, content_length: str | None = None,
           err_id: str | None = None) -> None:
    """Close the record out and hand it to the writer — the single write point
    for HTTP-call records.

    ``server_ms`` is time-to-response-OBJECT, not time-to-last-byte: the
    middleware resumes when call_next returns, and a FileResponse or the mount
    proxy has not streamed its body yet. For /api/run (a fully materialised
    JSONResponse) the two are the same; for /api/fs/raw they are not, which is
    why result_bytes there comes from Content-Length. The existing SV-3 access
    line has exactly this property, so this inherits a known limitation rather
    than inventing one.
    """
    if call is None or call.get("_drop"):
        return
    call["status"] = status
    call["server_ms"] = round(elapsed_ms)
    if err_id:
        call["err_id"] = err_id
    if _take_superseded(call.get("call_id")):
        # The page threw this result away before it arrived, so the call must not
        # enter any latency statistic (CL-5). This wins over the handler's own
        # ok/error label because "nobody ever saw this" is the truer fact about
        # the call; any error detail already enriched onto the record is kept —
        # only the outcome changes.
        outcome = "superseded"
    if outcome:
        # Beats whatever the handler enriched: "nobody saw this result" is the
        # truer fact about the call than ok/error, and the error detail is kept.
        call["outcome"] = outcome
    elif call.get("outcome") is None:
        call["outcome"] = "error" if (status is None or status >= 400) else "ok"
    if call.get("result_bytes") is None and content_length:
        try:
            call["result_bytes"] = int(content_length)
        except (TypeError, ValueError):
            pass
    record(call)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------ page-level errors

def record_page_error(body: dict) -> dict:
    """Record a JS error the page hit — the record for when NO call happened.

    The deliberate carve-out from "only the middleware writes": a page-level
    error is not an HTTP call, it is what happened instead of one. Zero call
    records for a page that should have run Python is the single most
    informative signal in this store, and without this it is a dead end — with
    it, "the page never called Python" becomes "TypeError at sine.html:42".
    """
    page = body.get("page")
    if not isinstance(page, str) or not page:
        return {"recorded": False, "reason": "missing 'page'"}
    if not enabled():
        return {"recorded": False, "reason": "capture disabled"}
    message, msg_truncated = _cap_text(body.get("message"), OUTPUT_CAP)
    stack, stack_truncated = _cap_text(body.get("stack"), ERROR_CAP)
    source, _ = _cap_text(body.get("source"), 1_024)
    rec = {
        "version": RECORD_VERSION,
        "call_id": uuid.uuid4().hex,
        "kind": "page-error",
        "occurred_at": _now_iso(),
        "page": page,
        "target_file": body.get("target_file") if isinstance(body.get("target_file"), str) else None,
        "first_party": is_first_party(page),
        "route": None,
        "http_method": None,
        "status": None,
        "outcome": "error",
        "server_ms": None,
        "error": {
            "type": body.get("type") if isinstance(body.get("type"), str) else "Error",
            "message": message,
            "traceback": stack,
        },
        "source": source,
        "line": body.get("line") if isinstance(body.get("line"), int) else None,
        "col": body.get("col") if isinstance(body.get("col"), int) else None,
        "truncated": msg_truncated or stack_truncated,
    }
    record(rec)
    return {"recorded": True, "call_id": rec["call_id"]}


# ------------------------------------------------------------------ read side

READ_CHUNK = 256 * 1024


def _iter_lines_reverse(path: str, chunk: int = READ_CHUNK):
    """Yield a file's lines from the END, reading backwards in blocks.

    Records are appended, so newest-last — and every caller wants newest-first.
    Reading backwards means a `since` window or a `limit` touches only the tail
    it needs; the previous `readlines()` pulled an entire day's file into memory
    before yielding its first line, which on a large store was the whole cost.
    """
    with open(path, "rb") as fh:
        try:
            pos = fh.seek(0, os.SEEK_END)
        except OSError:  # pragma: no cover - unseekable store file
            return
        tail = b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step) + tail
            lines = block.split(b"\n")
            # The first element may be a partial line continuing into the
            # previous block; hold it back until that block is read.
            tail = lines.pop(0)
            for line in reversed(lines):
                if line.strip():
                    yield line.decode("utf-8", "replace")
        if tail.strip():
            yield tail.decode("utf-8", "replace")


def _iter_records(paths: list[str], since: float | None = None):
    """Yield parsed records from newest file to oldest, newest line first.

    ``since`` is an optimization, not a filter (callers still match on it): it
    lets this stop reading instead of parsing the whole store to answer a
    one-hour question.

      * a file whose mtime predates the window is skipped whole — its last
        append is older than anything asked for, and for an append-only file
        mtime IS its newest record;
      * within a file, the first record older than the window ends that file —
        everything further back is older still.

    Files are only skipped, never stopped at, because same-day files from
    different processes interleave in time; the per-file bound is exact.

    A corrupt line (a torn tail of a file being appended right now) is skipped,
    not fatal.
    """
    for path in reversed(paths):
        if since is not None:
            try:
                if os.path.getmtime(path) < since:
                    continue
            except OSError:
                continue
        try:
            for line in _iter_lines_reverse(path):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if since is not None:
                    stamp = _epoch(rec.get("occurred_at"))
                    if stamp is not None and stamp < since:
                        break  # rest of this file is older
                yield rec
        except OSError:
            continue


def _matches(rec: dict, *, page=None, entrypoint=None, route=None, outcome=None,
             kind=None, since=None, until=None, failed=False, q=None,
             first_party=None) -> bool:
    # "This file" matches any of the three roles a path can play in a record:
    # the page that made the call, the file a template page was previewing, or
    # the call's own target. The last one is not optional — a `.py` is NEVER a
    # `page` (the .html is), so without it the Calls view on a data file, which
    # the registry offers and the gate confirms has history, showed nothing.
    if page and page not in (rec.get("page"), rec.get("target_file"), rec.get("entrypoint")):
        return False
    if entrypoint and entrypoint not in (rec.get("entrypoint") or ""):
        return False
    if route and rec.get("route") != route:
        return False
    if outcome and rec.get("outcome") != outcome:
        return False
    if kind and rec.get("kind") != kind:
        return False
    if first_party is not None and bool(rec.get("first_party")) != first_party:
        return False
    if failed and rec.get("outcome") not in ("error", "conflict"):
        return False
    stamp = _epoch(rec.get("occurred_at"))
    if since is not None and (stamp is None or stamp < since):
        return False
    if until is not None and (stamp is None or stamp > until):
        return False
    if q:
        needle = q.lower()
        haystack = " ".join(
            str(rec.get(k) or "") for k in
            ("page", "entrypoint", "route", "outcome", "stdout_tail", "stderr_tail")
        )
        error = rec.get("error") or {}
        if isinstance(error, dict):
            haystack += " " + " ".join(str(v or "") for v in error.values())
        if needle not in haystack.lower():
            return False
    return True


def _epoch(iso: str | None) -> float | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def query(limit: int = 100, cursor: str | None = None, **filters) -> dict:
    """A page of records, newest first, plus the cursor to resume from.

    ``cursor`` is a ``call_id``: records are returned only after (i.e. older
    than) it is seen, and the response's ``cursor`` is the newest record in
    this batch. That is what makes an agent's read "everything since I last
    looked" rather than a wall-clock guess about how long the human took
    (design §9.2b).
    """
    limit = max(1, min(int(limit), 1_000))
    out: list[dict] = []
    newest: str | None = None
    seeking = cursor is not None
    # No `since` hint while seeking a cursor: the walk has to reach the cursor
    # record itself, which may sit outside the caller's time window.
    hint = None if seeking else filters.get("since")
    for rec in _iter_records(store_files(), since=hint):
        if newest is None:
            newest = rec.get("call_id")
        if seeking:
            # Everything newer than the cursor is what the caller has not seen;
            # stop as soon as we reach the cursor itself.
            if rec.get("call_id") == cursor:
                break
        if not _matches(rec, **filters):
            continue
        out.append(rec)
        if len(out) >= limit and not seeking:
            break
    return {"records": out, "cursor": newest, "count": len(out)}


def overview(**filters) -> dict:
    """Counts and span across the whole (filtered) store — the digest an agent
    should read before it reads any records."""
    outcomes: dict[str, int] = {}
    kinds: dict[str, int] = {}
    pages: dict[str, int] = {}
    first = last = None
    total = 0
    for rec in _iter_records(store_files(), since=filters.get("since")):
        if not _matches(rec, **filters):
            continue
        total += 1
        outcomes[rec.get("outcome") or "unknown"] = outcomes.get(rec.get("outcome") or "unknown", 0) + 1
        kinds[rec.get("kind") or "call"] = kinds.get(rec.get("kind") or "call", 0) + 1
        page = rec.get("page")
        if page:
            pages[page] = pages.get(page, 0) + 1
        stamp = _epoch(rec.get("occurred_at"))
        if stamp is not None:
            if last is None or stamp > last:
                last = stamp
            if first is None or stamp < first:
                first = stamp
    return {
        "total": total,
        "outcomes": outcomes,
        "kinds": kinds,
        "pages": sorted(pages.items(), key=lambda kv: -kv[1])[:50],
        "first_at": first,
        "last_at": last,
        "dropped": _dropped,
    }


def _percentile(values: list[float], pct: float) -> float | None:
    """Nearest-rank percentile. No interpolation: with a handful of samples an
    interpolated p95 invents a number no call actually took."""
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return round(ordered[idx], 1)


def _durations(rec: dict) -> float | None:
    """The duration a chart should use, or None when the record must not count.

    Superseded/aborted calls are EXCLUDED from every latency statistic:
    runPython's latest-wins cancellation (D114/RH-9) means one slider drag
    issues dozens of calls of which one completes, so counting them would
    report "40 calls, p95 3.2s" for what the user experienced as one request.
    They are still counted separately — thrown-away work is exactly the
    "my page is hammering Python" signal — just never averaged in.
    """
    if rec.get("outcome") in ("superseded", "aborted", "disconnected"):
        return None
    value = rec.get("server_ms")
    return float(value) if isinstance(value, (int, float)) else None


def targets(**filters) -> dict:
    """Per-entrypoint rollup: count, percentiles, error rate, bytes.

    The highest-value view in the feature and the one a human reads first —
    "which of my .py files carries this page, and which one is slow".
    """
    acc: dict[str, dict] = {}
    for rec in _iter_records(store_files(), since=filters.get("since")):
        if not _matches(rec, **filters):
            continue
        # A page-error has no target — it is what happened INSTEAD of a call —
        # so it would only add an "(unknown)" row here. The overview's `kinds`
        # counts it, and the record list shows it.
        if rec.get("kind") == "page-error":
            continue
        name = rec.get("entrypoint") or rec.get("route") or "(unknown)"
        row = acc.setdefault(name, {
            "entrypoint": name,
            "name": rec.get("entrypoint_name") or name,
            "count": 0, "errors": 0, "superseded": 0,
            "bytes": 0, "rows": None, "last_at": None,
            "_durations": [],
        })
        row["count"] += 1
        if rec.get("outcome") in ("error", "conflict"):
            row["errors"] += 1
        if rec.get("outcome") in ("superseded", "aborted", "disconnected"):
            row["superseded"] += 1
        size = rec.get("result_bytes")
        if isinstance(size, (int, float)):
            row["bytes"] += int(size)
        rows = rec.get("result_rows")
        if isinstance(rows, int):
            row["rows"] = rows
        duration = _durations(rec)
        if duration is not None:
            row["_durations"].append(duration)
        stamp = _epoch(rec.get("occurred_at"))
        if stamp is not None and (row["last_at"] is None or stamp > row["last_at"]):
            row["last_at"] = stamp
    rows_out = []
    for row in acc.values():
        durations = row.pop("_durations")
        row["p50"] = _percentile(durations, 50)
        row["p95"] = _percentile(durations, 95)
        row["max"] = round(max(durations), 1) if durations else None
        row["error_rate"] = round(row["errors"] / row["count"], 4) if row["count"] else 0.0
        rows_out.append(row)
    rows_out.sort(key=lambda r: -r["count"])
    return {"targets": rows_out}


def series(bucket_ms: int = 60_000, **filters) -> dict:
    """Pre-bucketed time series for the charts.

    Bucketing server-side is the whole reason the charts stay fast: the
    template never sees 100k records, it sees one point per bucket. Superseded
    calls get their own count and are kept out of the percentiles (_durations).
    """
    bucket_ms = max(1_000, min(int(bucket_ms), 86_400_000))
    width = bucket_ms / 1000.0
    buckets: dict[int, dict] = {}
    for rec in _iter_records(store_files(), since=filters.get("since")):
        if not _matches(rec, **filters):
            continue
        stamp = _epoch(rec.get("occurred_at"))
        if stamp is None:
            continue
        key = int(stamp // width)
        point = buckets.setdefault(key, {
            "t": key * width, "count_ok": 0, "count_err": 0,
            "count_superseded": 0, "bytes_sum": 0, "_durations": [],
        })
        outcome = rec.get("outcome")
        if outcome in ("error", "conflict"):
            point["count_err"] += 1
        elif outcome in ("superseded", "aborted", "disconnected"):
            point["count_superseded"] += 1
        else:
            point["count_ok"] += 1
        size = rec.get("result_bytes")
        if isinstance(size, (int, float)):
            point["bytes_sum"] += int(size)
        duration = _durations(rec)
        if duration is not None:
            point["_durations"].append(duration)
    points = []
    for key in sorted(buckets):
        point = buckets[key]
        durations = point.pop("_durations")
        point["p50"] = _percentile(durations, 50)
        point["p95"] = _percentile(durations, 95)
        point["max"] = round(max(durations), 1) if durations else None
        points.append(point)
    return {"bucket_ms": bucket_ms, "points": points}


def detail(call_id: str) -> dict | None:
    for rec in _iter_records(store_files()):
        if rec.get("call_id") == call_id:
            return rec
    return None


# --------------------------------------------------------------------- routes

def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep this module
    # from importing server (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing or invalid X-Fused header"}, status_code=403)
    return None


@router.get("/api/calls/config")
def api_calls_config():
    """Where the store is and whether capture is on — so a page, the CLI, or an
    agent can find it without hardcoding the path."""
    from fused_render.shell import prefs as shell_prefs

    return {
        "dir": store_dir(),
        "today": day_file(),
        "suffix": _SUFFIX,
        "enabled": enabled(),
        "params_mode": shell_prefs.calls_params_mode(),
        "retention_days": retention_days(),
        "dropped": _dropped,
    }


@router.post("/api/calls/event")
def api_calls_event(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Page-originated events: a JS error, or calls the page abandoned.

    Both are things only the page can know — a JS error is what happened
    *instead* of a call, and a superseded call looks like a plain success from
    the server's side (CL-5/CL-6).
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    kind = body.get("kind")
    if kind == "superseded":
        ids = body.get("call_ids")
        if not isinstance(ids, list):
            return JSONResponse(
                {"error": "'call_ids' must be a list of call ids"}, status_code=400)
        return {"marked": mark_superseded(ids)}
    if kind != "page-error":
        return JSONResponse(
            {"error": "'kind' must be 'page-error' or 'superseded'"}, status_code=400)
    return record_page_error(body)
