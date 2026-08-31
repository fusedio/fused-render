"""The worker side of the contract, written once (SPEC AI-3, AI-9).

Every runner is a folder with a `pyproject.toml` and a `worker.py`, started by
`fused_render.ai.supervisor` on the interpreter built from that declaration.
What a worker DOES differs completely — mlx_text loads a language model and
streams tokens, diffusers_image loads a pipeline and writes a PNG — but what it
IS does not: four routes, a token in a header, a port the child publishes, a
state machine the supervisor polls, and progress posted to the download manager.

That invariant half lives here. A concrete worker supplies three functions and
calls `serve()`:

    download(model_id)          fetch what is missing; return where it landed
    load(model_id, fetched)     put it in memory; raise to fail
    generate(body[, write])     one request; NDJSON via `write` or a dict back

and gets `/health`, `/cancel`, `/quit`, `/generate`, `--download-only`, the port
handshake, the auth check, the error framing and the reporting for free.

**Why a shared module rather than two standalone files.** The obvious reading of
"a runner is a folder" is that each folder is self-contained, and the first cut
was exactly that: mlx_text/worker.py carried all of this inline. Copying it for
the image runner would have put the SUPERVISOR'S contract — the auth header's
name, the status file's shape, the state vocabulary it polls for, the way
download bytes are measured — in two places, and every bug in this feature so
far has been two places encoding one rule and drifting apart. The contract
belongs beside the thing that defines it, once.

This module is **stdlib only**, deliberately, for two reasons. It is imported by
every runner's interpreter, so anything imported here becomes a dependency of
every backend forever. And it means the contract is IMPORTABLE BY THE TESTS,
which is the only way any of it gets tested at all: neither concrete worker can
run on CI (one needs Metal, the other several GB of torch), but this can, with
stub callables standing in for the model.
"""

import argparse
import concurrent.futures
import contextlib
import datetime
import email.utils
import fnmatch
import hashlib
import http.client
import http.server
import importlib.util
import json
import os
import queue
import re
import shutil
import socket
import socketserver
import stat
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ------------------------------------------------------------------- the state
#
# The vocabulary the SUPERVISOR polls for. `state` is the load-time machine —
# `ready` is the only value meaning the model can answer, `error` the only
# terminal failure — so these strings are contract, not description.

STATE = {
    "state": "starting",   # starting | downloading | loading | ready | error
    "model": "",
    "detail": "",
    "error": "",
    "resident_bytes": None,
    "os_footprint_bytes": None,
    "loaded_at": None,
    #: "cuda" | "mps" | "cpu" — what the weights actually landed on, set by the
    #: runner's `load()`. Only the process holding them knows: the supervisor
    #: can see that this machine HAS a GPU and not that the runner's torch was
    #: built to use it, and since D381 that is the COMMON case rather than the
    #: exotic one on every platform — the default torch rows pin the `whl/cpu`
    #: build, so a machine with a card runs on the CPU until its user opts into
    #: the CUDA or ROCm row. Reported because a model answering
    #: at three tokens a second is working perfectly and looks broken, and the
    #: device is the whole of the explanation.
    #:
    #: None from a runner that does not set it — one device, nothing to say.
    "device": None,
}
_state_lock = threading.Lock()

#: Set by `/cancel`. Long-running work checks it; what "stop" means is the
#: runner's to decide (a token loop breaks, a denoiser raises).
#:
#: Cleared by whichever generation OWNS `GENERATE_LOCK`, never by the handler on
#: its way in: a second request arriving while the first is generating would
#: otherwise erase the ✕ just pressed for the first, which then runs to
#: completion under a Stop that appeared to work.
CANCEL = threading.Event()

#: One generation at a time. A laptop has one GPU, and neither mlx's model
#: object nor a diffusers pipeline is safe to call from two threads — so a
#: second request waits rather than interleaves.
GENERATE_LOCK = threading.Lock()


def _generate_thread_loop(tasks: "queue.SimpleQueue") -> None:
    """Body of the ONE thread every `generate()`/`_single`-style call runs on.

    See `run_on_generate_thread`'s own docstring for why this thread must never
    exit for the life of the process."""
    while True:
        tasks.get()()


def _start_generate_thread() -> "queue.SimpleQueue":
    tasks: "queue.SimpleQueue" = queue.SimpleQueue()
    threading.Thread(target=_generate_thread_loop, args=(tasks,),
                     name="generate", daemon=True).start()
    return tasks


#: The queue behind `run_on_generate_thread` — created once, at import time,
#: alongside the thread that drains it. See that function's docstring.
_GENERATE_TASKS = _start_generate_thread()


def run_on_generate_thread(fn, *args, **kwargs):
    """Call `fn(*args, **kwargs)` on this process's ONE persistent MLX thread,
    and return what it returns (or raise what it raised).

    **Never on the caller's own thread**, because the caller here is always an
    HTTP connection thread — `_Server` is a `ThreadingTCPServer`, which hands
    every request a BRAND NEW thread that exits the moment the response
    finishes. MLX keeps compiled-graph state in a C++ `thread_local` (an
    `mlx::core::detail::CompileCache`); tearing one down runs its destructor
    from `pthread`'s own thread-exit cleanup, well outside anything Python's
    `try`/`except` can reach, and a checkpoint whose generation step is
    `mx.compile`d (mlx-vlm's is) populates that cache on its very first call.
    Reproduced directly, no HTTP involved: two back-to-back `stream_generate`
    calls on the SAME thread never fail, but the same two calls issued from
    TWO DIFFERENT threads in a row crash the process on the second one, deep in
    `CompileCache::CacheEntry::~CacheEntry()` — a `SIGSEGV` with no Python
    traceback at all, which on this worker's actual TCP transport surfaces to
    the caller as nothing more specific than a bare "connection reset by
    peer". A `ThreadingTCPServer` request thread is exactly that "two
    different threads" shape: request 1 built the cache on thread A, thread A
    exits when the response ends, and request 2's thread B is the crash.

    So every `generate()`/`_single`/`_stream` call is routed through the SAME
    thread for the whole process, started once at import time and never
    joined — the identical fix `worker.py::_pin_stream` already applies to
    MLX's per-thread default-stream state, applied here to its per-thread
    compiled-graph state instead. `GENERATE_LOCK` already serializes every
    caller to one at a time, so there is never more than one task in flight on
    this thread regardless.

    A plain function call, not a generator: streaming (`_stream`) already
    passes its own `write` callback in as an argument, so the callable handed
    here does its own line-by-line writing from ON the generate thread — the
    connection thread only blocks on the result, it never touches MLX itself.
    """
    result: "queue.SimpleQueue" = queue.SimpleQueue()

    def task():
        try:
            result.put((True, fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller's thread
            result.put((False, e))

    _GENERATE_TASKS.put(task)
    ok, payload = result.get()
    if ok:
        return payload
    raise payload


#: How long a worker sits with NO new execution before it hands its allocator
#: pool back to the OS (`_release`, `_arm_release_timer`, `_fire_release`
#: below). A deliberate, named constant rather than a literal 30 scattered
#: across the file — see those functions' docstrings for the mechanism.
#:
#: Chosen to match `supervisor._REAPER_TICK_S` (also 30.0) in spirit, not by
#: coincidence: both exist because 30s is short enough that "idle" means
#: something a user would recognise, and this timer is that reaper's much
#: cheaper, purely in-process cousin. The reaper polls every 30s whether a
#: worker has been unused for `prefs.effective_ai_idle_unload_minutes()` (5
#: minutes by default) and, past that, KILLS THE PROCESS — losing the loaded
#: weights entirely, the next request pays a full reload. This timer fires
#: once, 30 SECONDS after the worker's own last execution, and only hands
#: back the allocator's idle pool — the model stays resident and the next
#: request is exactly as fast as this one, it just re-faults the working set
#: from the pool it now has to grow again. Two different costs at two very
#: different timescales, not one mechanism with two names.
_RELEASE_IDLE_S = 30.0

#: Guards `_release_timer`/`_release_generation` together, so arming a new
#: timer and a just-fired old one checking whether it is still current can
#: never interleave into a wrong answer.
_release_lock = threading.Lock()

#: The pending idle-release timer, or `None` between executions. Always
#: `threading.Timer` in production; tests substitute a manually-fired stand-in
#: (see `tests/test_ai_worker_base.py`) rather than sleeping 30 real seconds.
_release_timer = None

#: Bumped every time a timer is (re)armed. A fired timer compares the token it
#: was armed with against this before calling `_release` — belt-and-braces
#: alongside `Timer.cancel()` itself, for the window between a timer's wait
#: elapsing and its callback actually running, during which a new execution
#: could already have rearmed (see `_fire_release`).
_release_generation = 0

#: The constructor `_arm_release_timer` calls to make its timer — a LOCAL
#: seam, not `threading.Timer` used directly. A test that wants a manually-
#: fired stand-in only has to monkeypatch THIS name, rather than
#: `threading.Timer` itself — patching the real stdlib class would affect
#: every `threading.Timer` created anywhere in the process for the duration
#: of the test, including by daemon threads left running from an earlier
#: test in the same worker, which is exactly the kind of cross-test flake
#: this module's own generate-thread singleton (`_GENERATE_TASKS`) already
#: has to be careful never to become an instance of.
_new_timer = threading.Timer


def _arm_release_timer():
    """(Re)start the `_RELEASE_IDLE_S` idle timer — called once after every
    completed execution, success, `Cancelled`, or any other exception alike,
    since a render that already allocated its peak can still be the one the
    user pressed Stop on.

    Cancels whatever timer a PRIOR execution left pending first: a burst of
    renders must not pay the re-fault cost of clearing and re-growing the
    allocator pool between each one — only the LAST execution in a burst gets
    to start the clock, which is the whole reason this is a timer and not an
    unconditional `finally`-call (the first cut of this change, before an
    idle timer replaced it). Correctness in the race between "the old
    timer's wait already elapsed" and "cancel just ran" is `_fire_release`'s
    job, via the generation token.

    A no-op when no `release` hook was supplied (`serve(release=...)` never
    called) — see `_release`'s own docstring for exactly which runners that
    is and why (six wired, several deliberately not); this docstring used to
    keep its own, shorter list and it drifted out of date the moment
    `mlx_text` was wired in, which is why it no longer tries to repeat it.
    """
    global _release_timer, _release_generation
    if _release is None:
        return
    with _release_lock:
        if _release_timer is not None:
            # A `Timer` whose wait already elapsed and is mid-callback cannot
            # be stopped by `cancel()` — that race is exactly why
            # `_fire_release` re-checks its token under this same lock rather
            # than trusting cancellation alone.
            _release_timer.cancel()
        _release_generation += 1
        token = _release_generation
        timer = _new_timer(_RELEASE_IDLE_S, _fire_release, args=(token,))
        # Daemon: a pending release must never be what keeps the worker
        # process alive. `serve_forever()`'s own thread and `os._exit(0)` in
        # `/quit` are both how this process actually ends, and neither should
        # have to know this timer exists in order to exit cleanly.
        timer.daemon = True
        _release_timer = timer
    timer.start()


def _fire_release(token):
    """The idle timer's callback: reclaim the allocator pool, but only if
    nothing has run since `token` was handed out.

    `GENERATE_LOCK` FIRST, generation check second: acquiring the same lock
    `_single`/`_stream` hold for the whole request guarantees this can never
    run WHILE a generation is in flight (mid-render is the one moment this
    must never fire — see `_arm_release_timer`), and once acquired, a
    generation that started and finished entirely inside this timer's 30s
    wait has already rearmed with a NEWER token by the time that lock frees
    up — so the check below catches it even though `cancel()` came too late
    to stop this callback from being scheduled at all.

    Routed through `run_on_generate_thread`, same as `generate` itself: the
    hook this exists for is `mx.clear_cache()`, an MLX allocator call, and
    that function's docstring is the whole reason no MLX call is ever made
    from a thread other than the one dedicated to them — a `Timer` callback
    runs on yet another thread of its own, no different in kind from a
    `ThreadingTCPServer` connection thread in that respect.

    Never raises past this function. A `release` that throws must be a
    no-op — there is no request waiting on this timer to fail loudly at, and
    a broken reclaim must not take the timer thread down with it. Still
    LOGGED, though (`traceback.print_exc`, `_single`'s own pattern for an
    error nobody is waiting on): a release that fails on every idle window
    forever is otherwise invisible except as the footprint number this whole
    feature exists to move never actually moving, and that is exactly the
    silent-regression shape code review caught once already (a torch build
    where the MPS branch always raised and quietly took the CUDA branch down
    with it — see `torch_image.release`). The worker's own stderr lands in
    `$TMPDIR/fused-render-<pid>.log`, where a broken reclaim is now visible.
    """
    global _release_timer
    with GENERATE_LOCK:
        with _release_lock:
            if token != _release_generation:
                # Superseded: a later execution already rearmed a fresher
                # timer, which owns clearing the cache from here.
                return
            _release_timer = None
        try:
            run_on_generate_thread(_release)
        except Exception:  # noqa: BLE001 - logged below, then swallowed: see docstring
            traceback.print_exc(file=sys.stderr)

TOKEN = os.environ.get("FUSED_AI_WORKER_TOKEN", "")

#: Bounds on the PRE-AUTH body drain (Handler._drain). 64 KiB is far above any
#: real request this worker takes — they are small JSON objects — and the point
#: is only to stop an unauthenticated caller from naming a size that makes us
#: wait on it. 2s is generous for a body already in flight on loopback, and it
#: is the only read timeout on this connection at all (`_Server` sets none).
DRAIN_MAX_BYTES = 64 * 1024
DRAIN_TIMEOUT_S = 2.0

JOB_ID = ""
JOB_URL = (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/") + "/api/jobs"

JOB_TIMEOUT_S = 3.0


def set_state(**fields):
    with _state_lock:
        STATE.update(fields)


def snapshot():
    with _state_lock:
        return dict(STATE)


def describe_failure(exc):
    """What a user should be told about `exc` — the CHAIN, never the top frame.

    `str(exc)` is the wrong answer whenever a library re-raises, and the
    libraries a runner loads all do. transformers wraps every import failure
    from its lazy-module machinery, so a missing stdlib module three layers
    down arrived on the AI Models page as:

        Could not import module 'AutoTokenizer'

    while the actual exception it was raised `from` said:

        ModuleNotFoundError: No module named 'filecmp'

    One of those names a thing the user can act on; the other sends them
    looking at the model, the repo and the download — all of which were fine.
    So the chain is walked to its root and reported with the top message.

    `__cause__` first, then `__context__`: an explicit `raise … from e` is the
    library telling us what it wrapped, and an implicit context is the next-best
    evidence when it did not bother.

    `__suppress_context__` is honoured, which is the same rule `traceback`
    itself follows and not a technicality here. `raise … from None` is a library
    saying the exception it caught is NOT the explanation — the shape an
    optional-dependency probe takes (`except ImportError: raise … from None`) —
    and walking past it would let a deliberately hidden ImportError become our
    "root cause", up to and including firing the stdlib hint about an
    interpreter that is perfectly complete.
    """
    chain, seen = [], set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__)
    text = f"{exc.__class__.__name__}: {exc}"
    if len(chain) > 1:
        root = chain[-1]
        text += f" — caused by {root.__class__.__name__}: {root}"
    hint = _stdlib_hint(chain)
    return text + hint if hint else text


def _stdlib_hint(chain):
    """The sentence for a missing STDLIB module, or "".

    This is a fact about the INTERPRETER, not about the environment built on
    it, and the difference is the whole point: a missing third-party package is
    fixed by rebuilding the runner's venv, while a missing stdlib module is
    baked into the interpreter that venv was created from, so rebuilding
    reproduces it exactly. Told the first story, a user retries forever.

    **`ModuleNotFoundError`, not `ImportError`**, and that is not pedantry:
    `from email import nope` raises a plain ImportError whose `.name` is
    `email` — a package that is present and fine — so keying on ImportError
    would accuse a complete interpreter of missing part of its stdlib and tell
    the user a rebuild cannot help. That is the exact class of confidently
    wrong cause this function exists to stop, which makes it worth being
    strict: only "the module was not found" earns the accusation.

    The TOP-LEVEL name decides, because `sys.stdlib_module_names` holds only
    top-level names while a partial stdlib fails as `No module named
    'email.mime'`. The full name is what gets reported — it is the thing that
    is actually missing.
    """
    for exc in chain:
        name = getattr(exc, "name", None) or ""
        if isinstance(exc, ModuleNotFoundError) and name.partition(".")[0] in sys.stdlib_module_names:
            return (
                f"\n\n`{name}` is part of the PYTHON STANDARD LIBRARY, so this is "
                f"the interpreter this environment was built on ({sys.base_prefix}) "
                "shipping without it — not a problem with this model and not "
                "something rebuilding the environment can fix. Please report it "
                "with this message."
            )
    return ""


class Cancelled(Exception):
    """Raised out of a progress callback when the app asked us to stop.

    A worker's heavy phases are opaque C calls with no interruption point, so
    the only place a stop can be honoured is the callback the library hands us —
    which is where this comes from.
    """


class InsufficientDiskSpace(Exception):
    """Raised BEFORE a download starts, by `_ensure_disk_space`, when the
    target volume's free space is already known to be less than the total
    this fetch is about to write.

    Distinct from a bare `OSError` so `describe_failure`'s chain-walk prints
    THIS message at the top rather than whatever library call happened to be
    running when a mid-download `ENOSPC` finally surfaced — the whole point
    of checking early is a sentence that names the actual shortfall, not a
    syscall a user cannot act on.
    """


# ---------------------------------------------------------- SPEC AI-26 (D530)
def _ensure_disk_space(total_bytes, folder):
    """Raise `InsufficientDiskSpace` when `folder`'s volume is already known
    to have less free space than the bytes THIS DOWNLOAD STILL HAS TO WRITE
    — the fix for "no disk-space precheck anywhere" (SPEC AI-26): a download
    that will not fit used to fail as a mid-transfer `OSError: [Errno 28] No
    space left on device`, after however many gigabytes it managed to write
    and with an error that names a syscall rather than the gap.

    **`total_bytes` is the repo/file's FULL size in scope — a RESUME must be
    judged against what remains, not the whole thing again** (code review
    finding 2). Without this, a 30GB model interrupted at 28GB and retried
    with 5GB free was refused ("needs 30.0 GB, only 5.0 GB is free") even
    though only 2GB remained to fetch — defeating the entire `.part`/sidecar
    resume machinery this module otherwise goes to considerable lengths to
    provide. `bytes_on_disk(folder)` is the SAME figure the progress bar
    already trusts for "how much of this repo is durably on disk right
    now" — complete blobs plus a `.fusedpart`'s ALLOCATED-BLOCKS progress,
    not its sparse `ftruncate`d length — so subtracting it here reuses one
    notion of "already have" rather than inventing a second that could
    drift from what the bar reports. `folder` may legitimately hold MORE
    than what is in this fetch's own `allow_patterns` scope (an older
    revision's blobs, a differently-scoped prior fetch); that makes the
    subtraction a slight OVERESTIMATE of what has been durably written for
    THIS scope, which is the safe direction for a precheck to be
    imprecise in — the same "the bar can proceed on a guess" tolerance this
    module's listing-failure fallback already accepts, and refusing a
    resume that would actually have fit is a real regression while letting
    one through that comes up a little short during the fetch is not: the
    fetch itself still fails loudly if it genuinely runs out.

    `total_bytes=None` (a listing failure already degraded the progress bar
    to a guess, or a caller that never learned a total at all) is silently
    skipped — checking against an unknown total would mean either refusing
    every such download outright or checking against a size that IS a guess,
    and the existing `_fallback` degradation for a failed listing already
    treats "no total" as "proceed anyway, the bar just cannot be precise".
    This function makes the identical call for the SAME reason: an unknown
    figure is not evidence of a shortfall. A `remaining` of zero or less (a
    complete or over-complete repo, the fast path above should normally have
    already returned before this is ever called) is likewise skipped —
    nothing left to check space for.

    Checked against the NEAREST EXISTING ancestor of `folder`, because the
    folder itself is usually the thing this download is ABOUT to create —
    `os.statvfs` (what `shutil.disk_usage` calls) needs a path that already
    exists, and a repo's cache folder is created lazily by the first byte
    written into it.

    `shutil.disk_usage` itself failing (an unmounted volume, a path
    `statvfs` cannot reach) degrades to "proceed" rather than blocking a
    download over a filesystem question this function cannot actually
    answer — the same "cannot verify, so do not refuse" rule `_fallback`'s
    own callers already apply to a failed Hub listing.
    """
    if not total_bytes:
        return
    already = bytes_on_disk(folder) or 0
    remaining = total_bytes - already
    if remaining <= 0:
        return
    path = folder
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return
        path = parent
    if not path:
        return
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return
    if usage.free >= remaining:
        return
    need_gb = remaining / GB_BYTES
    free_gb = usage.free / GB_BYTES
    short_gb = need_gb - free_gb
    raise InsufficientDiskSpace(
        f"Not enough disk space: this download needs {need_gb:.1f} GB, "
        f"only {free_gb:.1f} GB is free — {short_gb:.1f} GB short.")


#: `shutil.disk_usage`/`_ensure_disk_space`'s own unit — a decimal gigabyte,
#: matching every other byte->GB reading in this app (`fit.py`'s own
#: `GB_BYTES`, restated here rather than imported: this module is
#: stdlib-only and `fit.py` is not, see the module docstring).
GB_BYTES = 1e9


# ------------------------------------------------------- reporting to the app


#: The last thing `report` sent, so `heartbeat` can send it again. One slot:
#: a worker reports about one piece of work at a time.
_last_report = {}
_last_report_lock = threading.Lock()

#: How often a heartbeat re-sends it. Well under `jobs.STALE_AFTER_S` (30s) —
#: the number that matters is the GAP between real ticks, and for a denoiser
#: that gap is one step, which on a laptop is routinely longer than the whole
#: stale window.
HEARTBEAT_S = 5.0


def report(job=None, **fields):
    """One progress tick to the download manager. Never raises, never blocks long.

    Returns the stored record, or None. **The return value is load-bearing**: the
    manager's ✕ sets `cancel_requested` on the row, and the reply to the tick we
    were sending anyway is how that reaches a process sitting inside a
    multi-minute call. Reporting is otherwise decoration — if it fails the model
    still loads — so the socket timeout is short and every error is swallowed.

    `job` overrides the id: a load reports to the row the supervisor opened for
    it, while one image generation reports to its own per-request row.
    """
    job = job or JOB_ID
    if not job or not JOB_URL.startswith("http"):
        return None
    # Remembered before the send, so a heartbeat repeats what we MEANT to say
    # even if this particular tick never landed. Only a REAL tick writes this
    # slot — see `_send`, which is what the heartbeat calls.
    with _last_report_lock:
        _last_report.clear()
        _last_report.update(job=job, fields=dict(fields))
    return _send(job, fields)


def _send(job, fields):
    """POST one tick. The half of `report` that does NOT remember it.

    Split out for the heartbeat, and this is not tidiness. A heartbeat that
    called `report` would re-write `_last_report` with the payload it had just
    read — so a real tick landing between the read and that write was clobbered
    back to the older one, and every later beat repeated the stale numbers. The
    bar went BACKWARDS while the model was making progress, which is a worse lie
    than the stall this exists to prevent.
    """
    body = json.dumps({"id": job, **fields}).encode()
    request = urllib.request.Request(
        JOB_URL, data=body,
        headers={"Content-Type": "application/json", "X-Fused": "1",
                 "X-Fused-Worker": TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=JOB_TIMEOUT_S) as response:
            record = json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


@contextlib.contextmanager
def heartbeat():
    """Keep the job row alive for as long as the body runs.

    A row with no update in `jobs.STALE_AFTER_S` (30s) is reported as "no longer
    reporting", which is true of a page that was closed and a LIE about a worker
    that is simply slow. The image runner reports once per denoising step, and a
    FLUX step on a laptop routinely takes longer than the whole stale window — so
    a render that was progressing perfectly announced, at step 1 of 3, that
    nobody was reporting it.

    This is AI-5b's rule ("the poll doubles as the heartbeat") applied where it
    was missing. It lives in the base rather than in the denoiser because the
    property that causes it — progress whose natural granularity is coarser than
    30 seconds — belongs to the CONTRACT, and the next runner to have it should
    not have to rediscover this.

    Deliberately re-sends the LAST payload rather than inventing a new one: the
    bar must not move on a tick that learned nothing, and repeating `done`/
    `total` is what "still here, still on this step" looks like. Plain `report`,
    never `report_or_cancel` — a `Cancelled` raised on a timer thread is raised
    at nobody. The ✕ is still honoured where it always was, in the generating
    thread's own tick.
    """
    stop = threading.Event()

    def beat():
        while not stop.wait(HEARTBEAT_S):
            with _last_report_lock:
                if not _last_report:
                    continue
                job = _last_report["job"]
                fields = dict(_last_report["fields"])
            # A terminal state is never repeated: the work is over and the row
            # is not ours to keep touching.
            if fields.get("state") in ("done", "error", "cancelled"):
                continue
            # Re-checked as late as possible. The work can finish during the
            # wait above, and the FIRST payload of a generation carries
            # `state: "running"` — so a beat that slipped through here after the
            # supervisor marked the row done would flip it back to running and
            # clear its `finished_at`.
            if stop.is_set():
                return
            _send(job, fields)

    thread = threading.Thread(target=beat, name="heartbeat", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # JOINED, not just signalled. `stop.set()` cannot reach a beat already
        # inside its POST, and that tick would land after the work finished —
        # the same revival by a slower route. Bounded by the socket timeout it
        # is waiting on, and free in the common case: a generation shorter than
        # one interval leaves the thread parked in `wait`, which returns at once.
        thread.join(timeout=JOB_TIMEOUT_S + 1.0)


def report_or_cancel(job=None, **fields):
    """`report`, raising `Cancelled` if the reply says the ✕ was pressed."""
    record = report(job=job, **fields)
    if record and record.get("cancel_requested"):
        raise Cancelled()
    return record


#: A runner's own memory measurement, when it has one better than RSS. Set by
#: `serve()`. MLX is the reason it exists: its weights are memory-mapped and its
#: arrays are lazy, so RSS right after a load reports the interpreter and not
#: the model — 379 MB for a 6GB model, which is what sent us looking.
_measure = None

#: A runner's own PEAK-memory probe, when it has one — SPEC AI-8c, D497. A
#: second, OPTIONAL hook beside `_measure`, for a different question:
#: `_measure`/`resident_bytes()` answer "what is this costing RIGHT NOW",
#: which `supervisor.refresh_memory` only samples when `/health` happens to be
#: read. `fit` (AI-16) needs the HIGH-WATER MARK of a whole load-and-generate
#: pass instead — a staged pipeline like `ltx-video` frees stages between
#: renders (`low_memory=True`), so its peak is whichever stage happened to be
#: resident at the moment someone looked, which bounds nothing. Set by
#: `serve(peak_memory=...)`.
_measure_peak = None

#: The RSS high-water mark this worker has observed, kept HERE rather than by
#: the supervisor — SPEC AI-8c. `resident_bytes()` already runs on every
#: `/health` and at load; remembering the running `max()` of what it measured
#: turns a sparse sample into a monotone bound at the cost of one module-level
#: integer. Still weaker than a runner's own allocator peak (it only sees the
#: moments `/health` was actually polled), which is exactly why a runner that
#: HAS a true peak probe is asked to supply one instead — see `peak_resident_
#: bytes` for the precedence between the two, and AI-16's `basis` for how that
#: difference is carried outward to a reader.
_rss_peak = None

#: A runner's own "give it back to the OS" hook, when it has one. Set by
#: `serve(release=...)` — the third optional hook beside `_measure`/`_measure_
#: peak` above. Never called directly from a request: `_arm_release_timer`
#: starts a `_RELEASE_IDLE_S` clock after every execution, and `_fire_release`
#: is what actually calls this, once that clock runs out with nothing new
#: having started.
#:
#: Why this exists: a live FLUX.2-Klein-4B-4bit render through `mflux-image`
#: needed ~24.12 GB at its peak (`mx.get_peak_memory()`, the `_measure_peak`
#: probe) but settles at 1.7 GB RSS once it is done — and yet the status bar
#: kept showing "1.7 GB now (21 GB held)" (`os_footprint_bytes()`, D597) long
#: after the render finished. MLX frees those buffers back to its OWN pool,
#: never to the OS, and only reclaims the pool once it exceeds `mx.set_cache_
#: limit`'s default — 1.5x the recommended working set, ~38.7 GB on the 34.4
#: GB machine this was measured on, i.e. ABOVE physical RAM, so that condition
#: can never fire on its own. `mx.clear_cache()` is the other side of that:
#: hand the idle pool back once nothing has asked for it in a while.
#:
#: Why an IDLE TIMER rather than an unconditional call in `_single`/`_stream`'s
#: `finally` (the first cut of this feature): a 24 GB working set is not free
#: to re-fault, and clearing right after every execution makes a burst of five
#: renders pay that cost five times over. Waiting `_RELEASE_IDLE_S` after the
#: LAST execution keeps a burst at full allocator speed and still releases the
#: moment the user actually walks away — see `_arm_release_timer` and
#: `_fire_release` for the mechanism, which lives entirely in this process
#: (no supervisor RPC, no new route: the worker already knows when its own
#: last execution ended).
#:
#: Wired into all five MLX runners (`mflux_image`, `ltx_video`, `mlx_text`,
#: `mlx_embed`, `mlx_whisper`) and the shared `torch_image` runner (MPS/CUDA).
#: An idle timer changes the earlier per-call exclusion's arithmetic: it does
#: not fire mid-loop, so a bulk run of embeds or a token stream is never
#: interrupted by it, and the machine this was measured on can hold a speech
#: model (whisper-large-v3, 3.66 GB measured) and a text model resident in
#: SEPARATE PROCESSES at once — the supervisor keeps one worker per
#: capability — each with its own idle pool, so the small-model case stacks
#: rather than disappearing into rounding.
#:
#: Still nothing to wire for `llamacpp_text`/`llamacpp_text*` (a fixed KV
#: context allocated up front, weights mmap'd — there is no reclaimable
#: cache), `faster_whisper` (its CTranslate2 backend exposes no cache-release
#: API), or `onnx_embed`/its cuda/directml/rocm shells (the arena is
#: session-scoped `SessionOptions` config decided at session creation, not
#: something a per-idle clear can touch).
#:
#: This only RECLAIMS memory after a run finishes — it must never be reached
#: for by anything trying to lower the PEAK a render needs (tiled decode,
#: `mx.eval()` boundaries, `set_cache_limit`/`set_memory_limit`): those are
#: separate, unapproved changes to what an execution costs, not to what it
#: leaves behind.
_release = None

#: A runner's own LIVE footprint probe, when it has one — the FOURTH optional
#: hook beside `_measure`/`_measure_peak`/`_release` above, set by
#: `serve(footprint=...)`. Answers a question none of the platform-level
#: readings inside `os_footprint_bytes()` can: how much of a DISCRETE GPU's
#: own memory this worker is holding.
#:
#: Why the gap exists: on Linux `resident_bytes()`'s RSS and `os_footprint_
#: bytes()`'s `psutil` fallback both walk the process's own address space,
#: and a CUDA/ROCm allocation lives in the driver, not there — so a
#: `diffusers_image_rocm` worker holding a FLUX.2-klein-4B pipeline pinned to
#: VRAM (`torch_image._place`'s "all-gpu" case) reported 0.59 GiB
#: of actual VRAM use as if it were memory the reaper had already reclaimed,
#: while `RssAnon` separately showed 11.7 GiB of weights parked in system RAM
#: by `enable_model_cpu_offload()` — both real, neither visible to a probe
#: that only reads `/proc/<pid>/status`. macOS never had this hole: `phys_
#: footprint` already counts the Metal pool a torch-on-MPS or MLX worker uses,
#: which is why this hook is wired ONLY for CUDA in `torch_image.main()` — an
#: MPS figure added on top of `phys_footprint` would double-count the same
#: bytes the platform reading already found.
#:
#: `torch_image.main()` supplies `torch.cuda.memory_reserved()`, not `_
#: allocated()` — see that call site for why reserved is the number that
#: actually moves when `release()`'s `empty_cache()` fires.
_footprint = None


def resident_bytes():
    """What this model is costing in memory, or None.

    RSS by default. **THE OLD JUSTIFICATION HERE WAS WRONG AND IS WORTH STATING
    PLAINLY** (D597): it said "on Apple Silicon the GPU pool IS system memory,
    so it is the honest single number and there is no separate VRAM figure to
    reconcile it with". The premise is true — unified memory — but the
    conclusion does not follow, because the pool is not in RSS. Measured on a
    live MLX FLUX worker: 172 MB of RSS against 23 GB of dirty
    `IOAccelerator (graphics)` regions, which are charged to the task's
    `phys_footprint` and never appear in `resident_size`. So RSS is a FLOOR
    here, not an honest total, and `os_footprint_bytes()` below is what answers
    "what is this process actually holding" — as a LOWER BOUND, since neither
    counter it can read is a superset of the other (see its own docstring).
    This function's RETURN VALUE is deliberately unchanged all the same: it
    feeds `peak_resident_bytes` -> `footprints.py` -> `fit.py`'s "measured"
    rung, so redefining it would silently re-verdict every model the user has
    ever run (easy -> tight, tight -> no). Whether a "measured" footprint
    should include the allocator pool is a real question and not this change's
    to answer.
    A runner that can do better supplies `memory=` to `serve()`, and the
    LARGER of the two wins — both are real measurements and neither is a
    superset (RSS includes the interpreter and framework; a framework allocator
    includes buffers that may not be faulted into RSS yet), so the cost is at
    least the larger.

    psutil comes with every runner's environment; if it is somehow absent the
    answer is whatever the runner could measure, or None rather than a guess.

    **Also updates `_rss_peak`**, as a side effect of the one RSS reading this
    function already takes — see `peak_resident_bytes`. Every call site here
    already runs on every `/health` and at load, so no new sampling point is
    needed to turn this into a high-water mark; it would just be a second read
    of the same number.
    """
    global _rss_peak
    own = None
    if _measure is not None:
        try:
            own = _measure()
        except Exception:  # noqa: BLE001 - a runner's own probe must never break /health
            own = None
    rss = None
    try:
        import psutil

        rss = int(psutil.Process(os.getpid()).memory_info().rss)
    except Exception:  # noqa: BLE001 - psutil raises its own family; none is fatal here
        rss = None
    if isinstance(rss, int) and rss > 0:
        _rss_peak = rss if _rss_peak is None else max(_rss_peak, rss)
    candidates = [n for n in (own, rss) if isinstance(n, int) and n > 0]
    return max(candidates) if candidates else None


#: macOS `TASK_VM_INFO`, and the byte offset of `phys_footprint` inside
#: `task_vm_info_data_t`. VERIFIED EMPIRICALLY rather than counted off a header:
#: `resident_size` at offset 16 matches `ps -o rss` exactly, and dirtying 500 MiB
#: of anonymous memory moved offset 144 by 524.6 MB and offset 152 by nothing.
#: The kernel reports how many `natural_t`s it filled, so a generous buffer is
#: safe across OS revisions (this field arrived in "rev1" and every supported
#: macOS fills well past it).
_TASK_VM_INFO = 22
_RESIDENT_SIZE_OFFSET = 16
_PHYS_FOOTPRINT_OFFSET = 144


def os_footprint_bytes():
    """A LOWER BOUND on what this process is holding RIGHT NOW, or None (D597).

    **`max(phys_footprint, resident_size)`, NOT `phys_footprint` alone** — code
    review 2026-08-28, finding 3, which corrected a claim this docstring and
    `ModelsDock.tsx`'s `MemoryCell` both used to make: that RSS is a strict
    subset of the footprint. It is not. `phys_footprint` deliberately EXCLUDES
    clean file-backed pages, and those are counted in `resident_size`. Measured
    here, in a plain interpreter with no framework loaded: `resident_size`
    19.2 MB against `phys_footprint` 9.3 MB. So for any runner that maps its
    weights read-only — GGUF/llama.cpp, torch with `mmap=True` — the footprint
    is the SMALLER of the two by roughly the size of the model file, and
    reporting it alone rendered a row as `8.2 GB now (1.1 GB held)`: a visible
    contradiction, with the status bar's colour band painted off the smaller
    number while the machine held the larger.

    NEITHER COUNTER IS THE TOTAL, which is why `max` rather than a choice
    between them: RSS misses the Metal pool (measured on a live MLX FLUX
    worker: 172 MB of RSS against 24 GB of `phys_footprint`, 23 GB of it dirty
    `IOAccelerator` regions), and the footprint misses clean file pages. Their
    max is a strictly better lower bound than either, and it restores the one
    invariant the UI pair depends on — "held" can never read as less than
    "now". A true total would need to add the disjoint parts, which needs a
    region-by-region walk this probe deliberately does not do; hence "lower
    bound" in the first line rather than "what it holds".

    STILL THE NUMBER ACTIVITY MONITOR SHOWS in the case that motivated it: on
    an MLX worker `phys_footprint` dominates by three orders of magnitude, so
    the max IS the footprint and a user watching their system monitor sees the
    figure this reports. The correction only ever raises the answer, and only
    where RSS is the bigger of the two.

    NOT `resident_bytes()`, and not a redefinition of it — see that function's
    own docstring for why its value is frozen. This is additive, and it does
    not feed `peak_resident_bytes` -> `footprints.py` -> `fit.py`, so no
    model's "measured" verdict moves because of it.

    NOT `get_active_memory()` either, which is what the framework has handed
    out of its pool. After a render finishes MLX returns buffers to its own
    pool but NOT to the OS, so active collapses while the process still holds
    the memory. That exclusion is deliberate and correct for a COST figure
    (`mflux_image/worker.py`'s own comment, and D310 measured a ~23.6 GB pool
    against ~14.1 GB active) because it keeps runners comparable — torch
    reports what it allocated, not the driver's reservation. It simply does not
    apply to a live reading.

    STDLIB ONLY on darwin, via `ctypes` — no psutil, whose `memory_full_info()`
    does not expose `phys_footprint`. Both figures come out of the SAME
    `task_vm_info` read (offsets 16 and 144), so they describe the same instant
    and the max cannot be taken across two moments. Falls back to psutil's RSS
    wherever no such counter exists (every non-macOS platform, and any darwin
    failure above), and to None if even that is unavailable: never a guess, the
    same rule the other two probes follow.

    **`_footprint`'s figure is ADDED to the platform figure above, not `max`ed
    into it** — a deliberate DIFFERENT rule from the `max` this function
    already takes between `phys_footprint` and `resident_size`. Those two
    OVERLAP (same `task_vm_info` read, so whichever is bigger already includes
    everything the smaller one counted); a Linux worker's RSS and a discrete
    GPU's VRAM do not — they are disjoint address spaces, and `psutil` cannot
    see the driver's allocation at all. Taking `max` of two disjoint figures
    would silently discard the smaller one from the total; summing is the
    better lower bound, which is all this function has ever promised to be.
    See `_footprint`'s own docstring for the measured numbers this closes the
    gap for (a ROCm/FLUX.2-klein worker: 11.7 GiB of weights in RSS, 0.59 GiB
    of driver context invisible to it) and why the hook itself never fires on
    darwin/MPS, where `phys_footprint` already counts that pool.
    """
    footprint = None
    resident = None
    if sys.platform == "darwin":
        try:
            import ctypes
            import ctypes.util
            import struct

            libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
            libc.mach_task_self.restype = ctypes.c_uint32
            size = 1024
            buf = (ctypes.c_uint8 * size)()
            count = ctypes.c_uint32(size // 4)
            rc = libc.task_info(
                ctypes.c_uint32(libc.mach_task_self()),
                ctypes.c_int(_TASK_VM_INFO),
                ctypes.byref(buf),
                ctypes.byref(count),
            )
            filled = count.value * 4
            raw = bytes(buf)
            if rc == 0 and filled >= _PHYS_FOOTPRINT_OFFSET + 8:
                footprint = struct.unpack_from("<Q", raw, _PHYS_FOOTPRINT_OFFSET)[0]
            if rc == 0 and filled >= _RESIDENT_SIZE_OFFSET + 8:
                resident = struct.unpack_from("<Q", raw, _RESIDENT_SIZE_OFFSET)[0]
        except Exception:  # noqa: BLE001 - a memory probe must never break /health
            footprint = resident = None
    if resident is None:
        # Every other platform, and any darwin failure above. On a machine with
        # no separate GPU pool hiding outside RSS this is exactly what this
        # function would have reported anyway.
        try:
            import psutil

            resident = int(psutil.Process(os.getpid()).memory_info().rss)
        except Exception:  # noqa: BLE001 - psutil raises its own family
            resident = None
    candidates = [n for n in (footprint, resident) if isinstance(n, int) and n > 0]
    platform_figure = max(candidates) if candidates else None

    gpu = None
    if _footprint is not None:
        try:
            probed = _footprint()
        except Exception:  # noqa: BLE001 - a runner's own probe must never break /health
            probed = None
        if isinstance(probed, int) and probed > 0:
            gpu = probed

    if platform_figure is None:
        return gpu
    return platform_figure + gpu if gpu is not None else platform_figure


def peak_resident_bytes():
    """The high-water mark of what this model has cost, or None — SPEC AI-8c.

    **`max(probe, _rss_peak)`, not the probe alone** — the same correction
    `resident_bytes()` makes between its own two readings, for the identical
    reason. MLX's `mx.get_peak_memory()` is a true peak of the ALLOCATOR only:
    it does not count the interpreter and framework baseline `resident_bytes`'s
    own docstring measures at 379 MB for a 6GB model. Returning the probe
    outright would let a `measured` footprint (AI-16a) UNDERSTATE what the
    process actually occupied — the exact dishonesty AI-16c exists to remove,
    now on the write side instead of the read side: a badge reading "Ran
    comfortably here (20 GB)" for a load whose real high-water was larger is
    the same wrong claim a `_fit_verdict` computed over the wrong footprint
    used to make. Neither reading is a superset of the other (a probe can
    catch a stage that came and went between two `/health` polls that
    `_rss_peak`'s sparser sampling missed, and `_rss_peak` catches whatever a
    probe's own accounting does not cover), so the larger of the two is what
    is least wrong, exactly as `resident_bytes` already argues for `own`/`rss`.

    A probe that raises or answers nothing (a wheel shipping neither
    `get_peak_memory` name) is simply absent from the `max` — `_rss_peak`
    alone answers, which is `resident_bytes`'s pre-AI-8c behaviour restated as
    a high-water mark rather than a sample.

    `None` when NEITHER has an answer yet — a worker `/health`ed before its
    first `resident_bytes()` call, or one whose environment has no psutil and
    no probe. Never a guess, the same rule `resident_bytes` follows.
    """
    peak = None
    if _measure_peak is not None:
        try:
            probed = _measure_peak()
        except Exception:  # noqa: BLE001 - a runner's own probe must never break /health
            probed = None
        if isinstance(probed, int) and probed > 0:
            peak = probed
    candidates = [n for n in (peak, _rss_peak) if isinstance(n, int) and n > 0]
    return max(candidates) if candidates else None


# --------------------------------------------------------- downloading weights
#
# Progress is measured from the DISK (SPEC AI-5b). `snapshot_download` exposes
# only its outer "Fetching N files" counter through `tqdm_class`; the per-file
# byte bars are internal. Reporting that counter as bytes is how a 4.6GB pull
# came to read "10 / 11 B", and during a single large shard it does not move at
# all — so the row also went stale mid-download and the manager declared nobody
# was reporting. Walking the repo folder answers both: real bytes, and a tick
# every second whatever huggingface_hub is doing inside.
#
# The FETCH is ours too (SPEC AI-5i). `snapshot_download` opens one connection
# per file and one file at a time, so a model whose bytes are a single 4.6GB
# shard downloads on exactly one connection — and an interruption throws the
# whole thing away, which matters because the supervisor kills the fetch on quit
# (AI-5e). What is below fetches with several connections at once, split across
# files AND inside one file with `Range`, recording per-segment offsets as the
# bytes land. Every failure and every incapability falls back to
# `snapshot_download` under the same progress wrapper: a download that got
# faster and sometimes broken would be a bad trade.

#: Below this a file is fetched whole: splitting a 200KB config across four
#: sockets costs four round trips to save nothing.
SEGMENT_MIN_BYTES = 32 * 1024 * 1024
#: The size of one unit of work once a file IS being split. A separate
#: constant from `SEGMENT_MIN_BYTES` on purpose, even though the two start out
#: equal: one decides whether to split a file at all, the other decides how
#: big each piece is once splitting happens, and nothing says a future tuning
#: pass changes them together.
#:
#: **A FLOOR, not the size — see `MAX_CHUNKS_PER_FILE`.** Every piece is exactly
#: this big up to a file of `500 × CHUNK_BYTES` (16,777,216,000 bytes, ~16.8GB);
#: above that the piece grows so that the COUNT stops. Everything below is about
#: why a fixed size rather than `size / N`, and it holds unchanged: the point was
#: never 32MB specifically, it was many more units of work than connections.
#:
#: **Fixed size, not `size / N` — this is the fix for the download's tail.**
#: A big shard used to become a handful of EQUAL shares (see the retired
#: `MAX_SEGMENTS_PER_FILE` below): four connections at four different real
#: speeds finish at four different times, and once the fast three are done
#: there is nothing left to hand them — the slowest share runs out the clock
#: alone, which measured as a 4.6GB model crawling from ~90% to 100% for over
#: a minute. Fixed-size chunks make a big file into MANY units of work in one
#: shared queue: a worker that finishes early pulls the next chunk rather than
#: finding nothing assigned to it, so a slow connection only ever delays its
#: own current 32MB, never the tail of the whole download.
#:
#: **Two costs this accepts, deliberately, both raised in review and both
#: judged worth it rather than left unexamined.**
#:
#: (1) A 4.6GB shard is now ~144 requests instead of 4 — 144 TCP/TLS
#: handshakes rather than 4, since `urllib` opens a fresh connection per
#: `_open` call. Against a 32MB transfer per chunk that overhead is a small
#: percentage, and it buys the one property size/N could not have at any
#: chunk count: MORE units of work than `MAX_CONNECTIONS`, which is what
#: makes stealing possible at all. A pool with only as many items as workers
#: — four shares on eight connections — can never exhibit the failure this
#: fixes, no matter how the four are sized.
#:
#: (2) `work` is built file-by-file (`_segmented_fetch`), so with one file's
#: chunk count at or above `MAX_CONNECTIONS`, all 8 connections work that ONE
#: file before the next file's chunks start — files finish roughly in
#: submission order rather than interleaved. That is a scheduling preference,
#: not a correctness gap: the cap still holds, nothing waits on a connection
#: sitting idle, and a multi-file repo still finishes strictly faster than
#: before this change. Round-robining chunks ACROSS files instead would
#: trade "finishes files one at a time" for "every file creeps up together",
#: which is not obviously better and was not what the tail bug asked for —
#: left as a real option for whoever next has a reason to prefer it, not
#: implemented speculatively here.
CHUNK_BYTES = 32 * 1024 * 1024
#: The most pieces ONE file may be split into, whatever its size. Together with
#: the floor above:
#:
#:     chunk = max(CHUNK_BYTES, ceil(size / MAX_CHUNKS_PER_FILE))
#:
#: so a file grows its piece SIZE once it would otherwise grow its piece COUNT
#: past this. The floor and the ceiling meet at exactly `500 × CHUNK_BYTES` —
#: 16,777,216,000 bytes, ~16.8GB — and every file below that is chunked exactly
#: as it was before this cap existed, which is every model in today's catalog
#: and every file of the 280-file `MiniMaxAI/MiniMax-H3` (its largest plans 311
#: pieces, so the cap never engages there at all).
#:
#: **The cost this removes is SIDECAR BOOKKEEPING, and nothing else.** Every
#: segment's cursor lives in the sidecar, which is rewritten whole every
#: `FLUSH_EVERY_S` (one second) for the life of the download. `Comfy-Org/MiniMax-H3`
#: — 30 files, 471GB, with single files up to 66.3GB — planned 1,976 segments in
#: one file and 14,057 across the repo: serialising ~2,000 dicts on a 1Hz timer
#: for the several hours such a download runs, per file in flight. Capped, that
#: same 66.3GB file is 500 × 133MB.
#:
#: **It is NOT a rate-limit measure and must not be read as one.** The Hub meters
#: URLs carrying a `/resolve/` segment; our ranged GETs go to the presigned CDN
#: location, which carries none, so chunk count consumes no quota at all. The
#: metered cost of a download is about one metadata resolve per FILE (280 for the
#: largest MiniMax repo, against 3,000 per five minutes anonymously), which no
#: chunking decision changes. What protects against rate limits is the Hub token,
#: the `RateLimit` parse in `_throttle_wait_s`, and `_resolved_meta`.
#:
#: **Why this does not reintroduce `_RETIRED_MAX_SEGMENTS_PER_FILE`'s tail
#: problem.** That cap was 4 — at or below `MAX_CONNECTIONS = 8`, so a big file
#: became a handful of static shares and a worker that finished early had nothing
#: to steal. 500 units against 8 connections is still sixty times more work than
#: workers, which is the property the tail fix actually needed; a queue that deep
#: hands off exactly as well as an uncapped one.
#:
#: **The accepted cost:** a failed chunk re-fetches a whole chunk, so at the
#: 66.3GB extreme that is up to 133MB rather than 32MB. It only applies above
#: ~16.8GB, where 133MB is two tenths of a percent of the file, and the retry
#: loop's own budget (`SEGMENT_ATTEMPTS`) already makes exactly this trade one
#: size down.
MAX_CHUNKS_PER_FILE = 500
#: Across everything — the ONE number that bounds how many sockets a download
#: opens. A pool per file would multiply the caps together.
MAX_CONNECTIONS = 8
#: RETIRED, deliberately left named rather than silently deleted: this used to
#: cap a single file at 4 equal shares, back when segments were `size / N`.
#: With fixed-size `CHUNK_BYTES` chunks pulled from one GLOBAL queue, a big
#: file simply produces more chunks — that is the whole fix above — and
#: `MAX_CONNECTIONS` is what already bounds how many run at once, so a second,
#: per-file cap would only recreate the tail this redesign removes: capping a
#: 4.6GB shard at 4 chunks again puts it back on 4 static shares. Nothing
#: reads this constant; it stays as a marker for why the number is gone.
_RETIRED_MAX_SEGMENTS_PER_FILE = 4
#: Deliberately NOT hf's `.incomplete`. hf resumes one of those by seeking to
#: its current length; our segments write out of order, so a partial file of
#: length N does not mean the first N bytes are there, and handing hf one of
#: ours would produce a silently corrupt blob. A suffix of our own also keeps
#: the fallback clean — hf never sees our state at all. On the append-only path
#: (`_appends_only`) length N *does* mean the first N bytes, but the suffix
#: stays ours on both: which of the two wrote a given part file is not something
#: hf could tell, and `_clear_parts` deletes them before the fallback runs
#: rather than offering hf a file whose meaning depends on the platform.
PART_SUFFIX = ".fusedpart"
#: `os.O_BINARY` where the platform has one, and 0 where the question does not
#: arise. **Windows only, and load-bearing exactly there.** A bare `os.open`
#: there gets the CRT's default translation mode, which is TEXT, so every `\n`
#: in a write becomes `\r\n` on the way to the disk. A weights blob is not text
#: — 0x0a is about one byte in 256 of one — so a part file written without this
#: flag is both LONGER than the file it describes and wrong in content, while
#: the cursors go on saying the download is complete: they count what was handed
#: to `os.write`, and the translation happens below that. The mirror path's
#: sha256 then declines a repo the Hub could serve, and the Hub path, which has
#: no digest, would publish a corrupt blob under a real etag — permanent, since
#: hf serves it from cache forever (`finish`). The stdlib does exactly this for
#: exactly this reason: `tempfile` ORs it into every one of its own flag sets.
#:
#: **Latent until the append-only route existed, and exposed rather than
#: introduced by it.** `os.pwrite` does not exist on the one platform where the
#: flag matters, so before `_appends_only` no `os.open` in this file had ever
#: written a byte there. Windows CI found it the first time one did.
_BINARY = getattr(os, "O_BINARY", 0)
#: SPEC AI-29 (D533) — refuses to open a `.part` path that is a SYMLINK,
#: rather than a regular file, without rejecting a legitimate RESUME (the
#: whole reason `O_EXCL`/`create_new` cannot be used here, unlike a one-shot
#: temp file: a `.part` file is deliberately reopened across process
#: restarts, sidecar-tracked, so "already exists" cannot mean "reject" the
#: way it would for a throwaway file). `O_NOFOLLOW` blocks exactly the attack
#: this item's checklist names — a symlink pre-planted at the `.part` path
#: to redirect writes elsewhere — while a real, previously-created `.part`
#: file (never a symlink; nothing in this module ever creates one there)
#: keeps opening normally. Absent on Windows (`getattr` default 0, the same
#: pattern `_BINARY` above already uses), where `os.open` has no symlink to
#: follow in the same sense and the flag does not exist.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
READ_BYTES = 1024 * 1024
#: Big enough that a filesystem which really allocates cannot hide it in a
#: block, small enough that paying for it on one is nothing.
SPARSE_PROBE_BYTES = 4 * 1024 * 1024
#: How much of a blob is hashed at a time on the MIRROR path (see
#: `_FileFetch.finish`). The same size as `READ_BYTES`, and for the same reason:
#: nothing in this file may hold a multi-gigabyte shard in memory.
HASH_BLOCK_BYTES = 1024 * 1024
HTTP_TIMEOUT_S = 30.0
SEGMENT_ATTEMPTS = 5
RETRY_BACKOFF_S = 0.5
#: A rate limit is not a fault, and `SEGMENT_ATTEMPTS` is a claim about faults:
#: it exists to decide "this file is unreachable, hand the repo to hf". A 429
#: says the opposite — the server is reachable and is asking us to wait — so it
#: gets an allowance of its own, counted separately. With the shared budget, a
#: throttled download gave up after five attempts and about seven seconds of
#: backoff and fell into `snapshot_download`, which is SLOWER: the user saw a
#: download crawl for no stated reason, having been throttled and never told.
#:
#: **THE REAL GUARANTEE IS THE TIME, NOT THE COUNT.** `THROTTLE_TOTAL_MAX_S` is
#: the bound worth reasoning about — the most wall clock one stretch of being
#: rate-limited may cost before this gives up and lets the ordinary failure path
#: hand the repo to hf. The attempt count bounds the REQUESTS instead, for the
#: pathological case the time budget cannot see: a server naming a wait of a
#: millisecond, over and over. The pair of them without the total was the review
#: finding — 60 attempts × a 60s ceiling is an hour, which is exactly what the
#: ceiling below says it exists to prevent.
#:
#: Ten minutes spans two of the Hub's five-minute fixed windows, so a genuinely
#: exhausted quota is waited out twice over before this concludes the wait is
#: not the answer.
THROTTLE_ATTEMPTS = 60
THROTTLE_TOTAL_MAX_S = 600.0
#: The longest SINGLE wait a throttle may impose, however long the server asked
#: for. A `Retry-After` of an hour is a legal answer, and the whole download
#: sitting still for it is not: coming back early costs a handful of extra
#: requests (a fixed window resets when it resets, whatever we do in the
#: meantime) and buys a row that keeps saying something true and a total budget
#: that stays accountable in minute-sized pieces. The wait is slept in
#: `THROTTLE_SLICE_S` slices on top of that, because `time.sleep(60)` cannot be
#: interrupted and `self.stop` is how a ✕ reaches a parked segment.
THROTTLE_WAIT_MAX_S = 60.0
THROTTLE_SLICE_S = 0.5
FLUSH_EVERY_S = 1.0
#: The revision both paths use, named rather than implied. It is hf's own
#: `snapshot_download` default, which is what keeps the fast path and the
#: fallback on one revision of a model.
DEFAULT_REVISION = "main"

#: The sidecar's own format number. Bumped whenever what a sidecar MEANS
#: changes shape — twice so far, and both times for the same reason.
#:
#: **3: `MAX_CHUNKS_PER_FILE`.** A file above `500 × CHUNK_BYTES` (~16.8GB) is
#: now split into 500 larger pieces rather than into `CHUNK_BYTES` ones, so a
#: version-2 sidecar for such a file lists boundaries this build would never
#: derive — and lists MORE of them, at every 32MB rather than every
#: `ceil(size/500)`. Every SMALLER file is planned identically, so nothing but
#: the version number distinguishes a stale sidecar from a current one, which is
#: exactly the dangerous shape described below.
#:
#: **2: the chunk queue.** A segment used to be one of `size / N` equal shares,
#: and became one of many fixed-size `CHUNK_BYTES` pieces, so a sidecar an older
#: build left behind describes boundaries this build would derive differently for
#: the same file. Identity
#: (etag, size) still matches such a sidecar, and the layout even often looks
#: internally consistent — which is exactly the shape of input that turns a
#: resume into a silently wrong blob rather than an obviously failed one, so
#: it cannot be left to the layout check to notice. Anything read back with a
#: different number, MISSING included — every sidecar written before this
#: field existed reads as missing — is treated exactly like no sidecar at all:
#: the safe reading, since a fresh download from a clean chunk plan is always
#: correct, merely slower than a resume would have been.
SIDECAR_VERSION = 3

_CONTENT_RANGE = re.compile(r"/(\d+)\s*$")
_RANGE_START = re.compile(r"^bytes\s+(\d+)-")
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Everything a segment retries on. `HTTPException` earns its place: an
#: `IncompleteRead` or an `InvalidChunkLength` from a body that broke mid-stream
#: is not an `OSError`, and outside this tuple one such hiccup — among the
#: commonest ways a transport misbehaves — aborted the whole multi-file download.
_TRANSIENT = (OSError, urllib.error.URLError, http.client.HTTPException, ValueError)


class _Unsegmentable(Exception):
    """This repo cannot be fetched our way, so hf's downloader gets it back.

    Not an error in itself — no range support, a Hub that reported no size, a
    cache filesystem that cannot hold a sparse file — which is why it reads as a
    fallback rather than as a failed download.

    A platform without `os.pwrite` was one of these and is NOT one any more: it
    fetches on a single append-only stream instead (`_appends_only`), which is
    the same guarantee by a different route rather than a weakened one.
    """


# ---------------------------------------------------------- SPEC AI-29 (D533)
#: `mirror.py`'s manifest reader already refuses `..`, an absolute path and a
#: Windows separator in a repo-relative NAME (`_safe_name`, that module's own
#: docstring gives the identical reasoning) — because a CDN manifest is
#: untrusted-origin by construction. The Hub metadata path
#: (`_hub_file_meta`/`HfApi.model_info`) had NO equivalent check before this:
#: `_FileFetch.link` joins `name` straight into `os.path.join(self.snapshot,
#: name)` and `os.symlink`s (or copies) a blob there, so a repo publishing a
#: sibling `rfilename` of `../../../../some/path` — the Hub is not proven to
#: reject that server-side, and this code should not rely on it doing so even
#: if it currently does — would write outside the snapshot directory
#: entirely. Restated here, verbatim in spirit, rather than imported: `mirror.
#: py` is loaded as a bare module by a runner's OWN interpreter with no
#: `fused_render` package on `sys.path` (see its own top-of-file note), so a
#: cross-import in either direction is not available.
def _safe_repo_relative_name(name) -> bool:
    """Whether `name` is a repo-relative path safe to join under a snapshot
    directory and write to — the identical rule `mirror._safe_name` states
    for the identical reason, applied to Hub-reported filenames too."""
    if not isinstance(name, str) or not name or len(name) > 512:
        return False
    if name.startswith("/") or "\\" in name or ":" in name:
        return False
    parts = name.split("/")
    return all(part and part not in (".", "..") for part in parts)


#: A blob is named by its etag and joined as ONE path segment
#: (`os.path.join(folder, "blobs", etag)`) — never repo-relative, so this is
#: stricter than `_safe_repo_relative_name` the same way `mirror._safe_
#: filename` is stricter than `mirror._safe_name` (a `/` here addresses a
#: different location inside the cache dir entirely, not a deeper file within
#: one). Not restricted to hex (a caller-supplied `meta` — the model mirror —
#: already validates its own etag as hex via `mirror._safe_etag` before this
#: function ever sees it; requiring hex again here would refuse a legitimate
#: git blob sha1/sha256 the Hub itself reports in some non-lowercase or
#: differently-shaped form this code has not audited every corner of), only
#: that it cannot be a path.
def _safe_blob_name(etag) -> bool:
    if not isinstance(etag, str) or not etag or len(etag) > 256:
        return False
    if "/" in etag or "\\" in etag or etag in (".", ".."):
        return False
    return True


def repo_folder(model_id, repo_type="model"):
    """This repo's folder in the hub cache, or None.

    `repo_folder_name` is hf's OWN encoder for `org/name` -> `models--org--name`,
    used here rather than a `.replace("/", "--")` for the usual reason: the
    layout is theirs to change, and a second copy of it here would keep
    reporting numbers for a directory that no longer exists. If hf ever moves
    the helper, progress degrades to a pulse — never to a wrong figure.
    """
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        from huggingface_hub.file_download import repo_folder_name
    except ImportError:
        return None
    return os.path.join(HF_HUB_CACHE, repo_folder_name(repo_id=model_id, repo_type=repo_type))


def bytes_on_disk(folder):
    """How much of `folder` is on disk right now, in bytes — None if unknown.

    Counts the partial files a download in flight is writing — hf's
    `.incomplete` and our own `.fusedpart` — which is the whole point: they ARE
    the progress. Symlinks are skipped from the `lstat` result itself, so the
    snapshot entries are not counted a second time on top of the blobs they
    point at.

    A `.fusedpart` is measured by ALLOCATED BLOCKS rather than by length. Our
    segments write out of order, so the file is created at its final size with
    `ftruncate` and filled as a sparse file: `st_size` is the full 4.6GB from
    the first second, and reporting that would put the bar at 100% before a
    byte had arrived. `st_blocks` is what the download has actually put on the
    disk. Where the platform has no such notion (Windows), `st_blocks` is
    absent and the length is the honest answer anyway — nothing is sparse
    there, and doubly so since `_appends_only`: a part file written by a single
    append-only stream is never pre-sized, so its length is exactly what has
    landed. That is also why the POSIX branch takes the MIN of the two rather
    than the blocks alone — an appended part file on a platform that HAS
    `st_blocks` can report more allocated blocks than it holds bytes.
    """
    if not folder:
        return None
    total = 0
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            blocks = getattr(info, "st_blocks", None)
            if name.endswith(PART_SUFFIX) and blocks is not None:
                total += min(info.st_size, blocks * 512)
            else:
                total += info.st_size
    return total


def selects(name, include=None, allow=None, ignore=None) -> bool:
    """Whether a repo file is in scope, with `huggingface_hub`'s OWN semantics.

    One function, because three readers ask it and they must not drift: the
    total on the bar, the list the segmented fetch works through, and
    `snapshot_download` itself on the fallback path. Hub's `filter_repo_objects`
    is `(no allow_patterns or any match) and (no ignore_patterns or no match)`,
    matched with `fnmatch` against the path RELATIVE to the repo root — where
    `*` crosses `/` like every other character, which is what makes
    `transformer/*.safetensors` a subtree rule rather than a one-level one.

    **`ignore` wins over `allow`**, as it does there. `include` is ours: a single
    exact filename, for a fetch of one GGUF out of a repo that publishes twenty.
    """
    if include is not None and name != include:
        return False
    if allow and not any(fnmatch.fnmatch(name, pattern) for pattern in allow):
        return False
    if ignore and any(fnmatch.fnmatch(name, pattern) for pattern in ignore):
        return False
    return True


def _repo_files(model_id, include=None, allow=None, ignore=None,
                revision=DEFAULT_REVISION):
    """`(sha, files)` — the commit this listing resolved to, and what to fetch.

    The sha comes back WITH the list because the two must not be decided
    separately. A listing at the repo's default branch paired with a fetch at a
    hardcoded "main" is two sources of truth that agree by coincidence: where
    they differ, we would fetch a genuinely different revision than the list
    implied, record a ref for it, and stay internally consistent while doing it
    — etag matches content, so nothing downstream could ever notice. The
    revision is therefore asked for explicitly (the same `main` hf's own
    `snapshot_download` defaults to, so the fast path and the fallback cannot
    land on different revisions of one model), and the fetch is pinned to the
    SHA that answer resolved to, which also settles the repo moving between the
    listing and the last byte.

    `files` is `(name, size)` for every file this download will ACTUALLY fetch.

    One metadata call, no weights, and ONE place that decides what is in scope —
    the total on the bar and the list the fetch works through come from the same
    filter, or the two disagree and a bar measures itself against files nobody
    is downloading.

    **Scoped, because a repo is rarely fetched whole.** `include` is a single
    filename (one GGUF out of a repo that publishes a dozen quantizations of the
    same model); `allow`/`ignore` are the same fnmatch patterns
    `snapshot_download` takes, applied by `selects` with the same precedence, so
    a download that fetches part of a repo does not measure itself against the
    rest of it.

    Raises, unlike its callers: the fetch cannot proceed on a guess, while the
    bar can.
    """
    from huggingface_hub import HfApi

    info = HfApi().model_info(model_id, revision=revision, files_metadata=True)
    files = []
    for sibling in getattr(info, "siblings", None) or []:
        name = getattr(sibling, "rfilename", None) or ""
        if not name:
            continue
        if not selects(name, include=include, allow=allow, ignore=ignore):
            continue
        files.append((name, getattr(sibling, "size", None)))
    return getattr(info, "sha", None), files


def repo_total_bytes(model_id, include=None, allow=None, ignore=None):
    """The size of what will ACTUALLY be fetched, from the Hub, or None.

    Without it the bar has no total and shows as indeterminate — which is
    honest, and much better than a wrong total. Summing the whole repo when only
    part of it is being fetched is how a 2.6GB pull came to read as a fraction
    of 30GB and then jump to "complete" against a figure it never downloaded.

    `allow` gained a name here (it was `include`/`ignore` only) for
    `download_plan` (SPEC AI-5n): a phase scoped with `allow_patterns` has to
    be priced against the same scope its `download_snapshot` call fetches, the
    same reason `download_snapshot` itself threads `allow_patterns` into
    `_repo_files` rather than pricing the whole repo.
    """
    try:
        return _total_bytes(
            _repo_files(model_id, include=include, allow=allow, ignore=ignore)[1])
    except Exception:  # noqa: BLE001 - a missing total is a cosmetic loss, never fatal
        return None


def _total_bytes(files):
    """What `_repo_files` adds up to, or None for an indeterminate bar."""
    total = sum(size for _name, size in files if isinstance(size, int) and size > 0)
    return total or None


def _capped(done, total):
    """Never report more done than there is to do.

    `bytes_on_disk` measures the whole repo folder, and a SCOPED total covers
    only part of it — so a machine that already holds another quantization of
    the same model would otherwise report 8GB of a 2.6GB download.
    """
    if done is None or total is None:
        return done
    return min(done, total)


def _remove(path):
    with contextlib.suppress(OSError):
        os.remove(path)


def _hf_token():
    """The user's Hub token, or None.

    Sent on OUR requests as well as hf's: a gated repo answers the metadata call
    for an anonymous caller and then 401s on the blob, which reads as a broken
    download rather than as a missing login.
    """
    try:
        from huggingface_hub.utils import get_token

        return get_token()
    except Exception:  # noqa: BLE001 - an unreadable token is an anonymous fetch, not a failure
        return None


def _hub_file_meta(repo_id, filename, revision):
    """Everything one file needs to be fetched and filed: where, and what.

    `location` is the post-redirect CDN/Xet URL — the one worth range-fetching,
    and the one that expires mid-download. `etag` names the blob in the cache
    and `commit` names the snapshot folder its entry lives in; both are hf's own
    layout, so both come from hf rather than from a second derivation here.
    """
    from huggingface_hub import get_hf_file_metadata, hf_hub_url

    url = hf_hub_url(repo_id, filename, revision=revision)
    meta = get_hf_file_metadata(url, token=_hf_token())
    return {"url": url, "location": getattr(meta, "location", None) or url,
            "etag": getattr(meta, "etag", None),
            "commit": getattr(meta, "commit_hash", None),
            "size": getattr(meta, "size", None)}


def _open(url, token, start=None, end=None):
    """One GET, ranged when `start` is given.

    `identity` is not politeness: a gzipped body's bytes are not the file's
    bytes, and every offset here is an offset into the file.
    """
    headers = {"Accept-Encoding": "identity"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if start is not None:
        headers["Range"] = "bytes=%d-%s" % (start, "" if end is None else end)
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT_S)


def _supports_ranges(location, token):
    """Does this URL really serve ranges? One byte answers it.

    A 206 with a parseable `Content-Range`, or no. A server that answers a Range
    request with 200 is saying it will send the whole body every time, and four
    segments of that is four times the download — so a doubtful answer means one
    segment, never an optimistic four.

    Skipped entirely for a file too small to split, so a repo of small configs
    costs no extra request at all. None means the question could not be ASKED,
    which is a different thing from a no — see `_probe_host`.
    """
    try:
        with _open(location, token, 0, 0) as response:
            if getattr(response, "status", 200) != 206:
                return False
            return bool(_CONTENT_RANGE.search(response.headers.get("Content-Range") or ""))
    except _TRANSIENT:
        return None


def _probe_host(location, token, probes):
    """`_supports_ranges`, asked once per HOST for the length of one download.

    Range support belongs to the CDN answering, not to the path: every shard of
    a repo comes off the same host with the same presigning scheme. Asked per
    file it is a serial TLS handshake per file before a single byte moves — the
    same startup cost `_resolve` was parallelised to remove, reintroduced on a
    thirty-shard repo.

    True, False, or None for "could not ask" — and the three are distinct
    because two rules turn on the difference. Only an ANSWER is remembered:
    caching a probe that FAILED lets one transient 503 put every remaining shard
    of the repo on a single connection for the rest of the download, with
    nothing on screen to say the fast path switched itself off. And only an
    answer of NO is grounds for throwing away a recorded layout (see `plan`);
    silence is not.
    """
    host = urllib.parse.urlsplit(location).netloc
    if probes.get(host) is None:
        answer = _supports_ranges(location, token)
        if answer is None:
            return None  # this file goes on one connection; the next re-asks
        probes[host] = answer
    return probes[host]


def _sparse_ok(folder):
    """Can this filesystem hold a pre-sized file without allocating it?

    The whole design writes segments OUT OF ORDER, which means creating each
    part file at its final size up front. Where `ftruncate` allocates instead of
    punching a hole that costs the repo's full size — 25GB reserved before a
    byte downloads, on a filesystem that may not have it — and `bytes_on_disk`,
    which counts allocated blocks, would report 100% from the first second.
    Both are hf's job on such a filesystem, so this is a fallback condition and
    not a bug to work around.

    Asked once per download with a throwaway file, because asking it of the
    first real part file means the zero-fill has already happened.
    """
    probe = os.path.join(folder, ".fusedpart-probe")
    # Bound before the try, not inside it. Today the only escape from that block
    # is an OSError that returns early, so this cannot be read unbound — but that
    # is an argument about which exceptions three syscalls raise, and `_drain`
    # just showed what happens when such an argument stops holding.
    blocks = None
    try:
        os.makedirs(folder, exist_ok=True)
        fd = os.open(probe, os.O_RDWR | os.O_CREAT | os.O_TRUNC | _BINARY,
                     0o644)
        try:
            os.ftruncate(fd, SPARSE_PROBE_BYTES)
            blocks = getattr(os.fstat(fd), "st_blocks", None)
        finally:
            os.close(fd)
    except OSError:
        return False
    finally:
        _remove(probe)
    return blocks is not None and blocks * 512 < SPARSE_PROBE_BYTES // 2


def _appends_only():
    """Whether this platform must fetch each file on ONE sequential stream.

    True where there is no `os.pwrite`, which means Windows and nothing else.

    Segments write OUT OF ORDER into a file that was pre-sized before a byte
    arrived, and only an unbuffered positional write makes AI-5i's guarantee
    hold there — that a counted byte is a written byte. `seek` + `write` on a
    buffered handle does not: the count runs ahead of the disk, and a resume
    then skips bytes that were never durable.

    So this used to be a flat refusal, and the refusal cost the model mirror its
    entire purpose on that platform: the mirror's only transport is
    `_segmented_fetch`, so a Windows client declined every time, every
    acquisition went to the Hub, and none of them appeared in the access logs
    the feature exists to produce (AI-5l).

    **A single append-only stream keeps the same guarantee by a different route
    rather than giving it up.** With one segment and an `O_APPEND` fd there is
    no out-of-order write left to make: every `os.write` is a syscall landing at
    the END of the file, so the file's LENGTH is the progress and a resume is a
    `Range` from that length. Nothing is buffered and nothing is seeked — the
    two things the pre-sized layout needed `pwrite` to avoid. What that also
    removes is the pre-sized file itself, and with it the sparse-filesystem
    requirement (`_segmented_fetch`) and the reason the bar counts blocks rather
    than length (`bytes_on_disk`).

    What is given up is parallelism WITHIN one file, and only that: chunks of
    DIFFERENT files still run on `MAX_CONNECTIONS` streams, because two files
    are two fds and nothing between them is out of order. A repo of thirty
    shards is as parallel here as anywhere; a single 4.6GB shard is not.

    Asked at call time rather than cached at import: it is one `hasattr`, and
    the tests answer it by taking the attribute away from a module they have
    already imported (`test_ai_hub_fetch_no_pwrite.py`), which is what lets the
    win32 path be exercised on POSIX at all.
    """
    return not hasattr(os, "pwrite")


def _file_size(path):
    """The file's length, or 0 where there is no file. Never raises."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _chunks(size):
    """Split [0, size) into `CHUNK_BYTES`-or-larger pieces. `done` is the cursor.

    Below `SEGMENT_MIN_BYTES` the file is one piece covering the whole thing —
    unchanged from before the chunk queue, and still the right answer: there
    is nothing to gain from splitting a file too small to matter.

    At or above it, every piece but the last is exactly one chunk — fixed
    size, not `size / N`. A fixed size is what turns a big file into MANY
    units of work rather than a HANDFUL: the whole point, since a queue with
    only as many items as connections gives a slow one nothing to hand off
    once its faster siblings finish (see `CHUNK_BYTES`'s own comment). A
    30-shard repo and a single 4.6GB shard both resolve to plans a worker can
    keep pulling from until the file is actually done.

    The chunk is `CHUNK_BYTES` up to the point where that would mean more than
    `MAX_CHUNKS_PER_FILE` pieces, and grows from there — a floor on the size and
    a ceiling on the count, which is what keeps a 66GB file's sidecar from
    carrying two thousand cursors rewritten every second.

    **PER FILE, and not per repo, deliberately.** A repo-wide budget would make
    one file's chunk size depend on the rest of the FILE SET, and this function
    is deterministic in `size` alone — no `count` argument, unlike the
    equal-share split it replaced. That is what lets a resume regenerate the
    exact boundaries a previous run planned without persisting the piece count
    anywhere but the sidecar's own `segments` list; under a per-repo cap, a
    resume after any change to the file list (a scoped download, an
    `allow_patterns` fetch, a repo that gained a file) would re-plan a file whose
    own bytes never moved, and throw away recorded progress to do it. The tighter
    bound is not worth that.
    """
    if size < SEGMENT_MIN_BYTES:
        return [{"start": 0, "end": size - 1, "done": 0}]
    chunk = max(CHUNK_BYTES,
                (size + MAX_CHUNKS_PER_FILE - 1) // MAX_CHUNKS_PER_FILE)
    pieces = []
    start = 0
    while start < size:
        end = min(start + chunk, size) - 1
        pieces.append({"start": start, "end": end, "done": 0})
        start = end + 1
    return pieces


def _seg_complete(seg):
    return seg["start"] + seg["done"] > seg["end"]


def _blob_sha256(path):
    """The file's sha256, read in fixed blocks.

    In BLOCKS rather than `read()`: this runs on multi-gigabyte shards, and the
    whole reason a part file is pre-sized and written through `pwrite` is that
    nothing here holds a file in memory.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(HASH_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


# -------------------------------------------------------------- being throttled
#
# A 429 used to be indistinguishable from a broken link: it landed in the
# generic `HTTP <code>` branch of the retry loop, spent the segment's whole
# budget on backoff in about seven seconds, and took the repo into hf's own
# `snapshot_download` — a slower download, with nothing anywhere saying why it
# had become slow. Two things are wrong with that and both are fixed here: a
# throttle is WAITED OUT rather than counted as a fault (see
# `THROTTLE_ATTEMPTS`), and it is SAID on the job row.
#
# The notice is a process global, which is right rather than merely convenient:
# a download-only worker process serves exactly one download, so there is no
# second job the notice could be attributed to, and the segment threads have no
# other way to reach the row. `fetch_with_progress`'s tick is the only channel
# to it, it runs on a different thread from every segment, and several segments
# can be throttled at once — hence the lock rather than a bare assignment.

_THROTTLE_LOCK = threading.Lock()
_THROTTLE_DETAIL = None


def _note_throttle(seconds, hub):
    """Publish "we are being rate-limited" for the next tick to say.

    `hub` is whether the throttling host is Hugging Face — `_FileFetch`'s
    `re_resolvable`, which is True on the Hub path only. A 429 from whatever
    `FUSED_MODEL_MIRROR` names is a real throttle and worth saying, but naming
    the Hub for it, or offering a Hub sign-in as the cure, would be advice about
    a host that is not involved.

    The sign-in half is added only when there is no token: it is the one action
    that raises the limit, and telling a signed-in user to sign in reads as the
    app not knowing what it is doing. `_hf_token()` answers that question for
    the whole file — nothing here reads the environment itself, and no part of
    the token goes anywhere near the message.
    """
    global _THROTTLE_DETAIL
    waiting = f"waiting {max(1, int(round(seconds)))}s"
    if not hub:
        detail = f"This download is being rate-limited — {waiting}"
    elif _hf_token():
        detail = f"Hugging Face is limiting this download — {waiting}"
    else:
        detail = ("Hugging Face is limiting this download — sign in to Hugging "
                  "Face in Preferences → AI for a higher limit")
    with _THROTTLE_LOCK:
        _THROTTLE_DETAIL = detail


def _clear_throttle():
    """Retire the notice, because bytes are moving again."""
    global _THROTTLE_DETAIL
    with _THROTTLE_LOCK:
        _THROTTLE_DETAIL = None


def _throttle_detail():
    """The throttle notice a tick should show instead of its own detail, or None."""
    with _THROTTLE_LOCK:
        return _THROTTLE_DETAIL


def _http_status(error):
    """The HTTP status an exception carries, or None if it carries none.

    Two client libraries reach this file and they raise different shapes. Our own
    requests go through `urllib`, whose `HTTPError` IS a response (`.code`,
    `.headers`); the Hub calls go through `huggingface_hub`, which raises
    `requests`-shaped errors carrying the response beside them (`.response`).
    Both are throttled by the same server for the same reason, so the throttle
    logic reads them through one pair of accessors rather than existing twice.

    Duck-typed rather than imported: this module is stdlib-only by contract (see
    the module docstring), so it cannot name `requests.HTTPError` to check it.
    """
    code = getattr(error, "code", None)
    if isinstance(code, int):
        return code
    code = getattr(getattr(error, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def _http_headers(error):
    """The response headers an exception carries, or None. See `_http_status`."""
    headers = getattr(error, "headers", None)
    if headers is None:
        headers = getattr(getattr(error, "response", None), "headers", None)
    return headers


#: One `r=`/`t=` parameter of a `RateLimit` entry. Deliberately loose: this is a
#: structured-field list whose parameter ORDER is not guaranteed, whose names are
#: quoted, and which may carry several buckets in one header — so the parse looks
#: for the two parameters it understands and ignores everything else, rather than
#: implementing the grammar and failing on the parts it does not need.
_RATELIMIT_PARAM = re.compile(r'\b([rt])\s*=\s*"?(-?\d+)"?')


def _ratelimit_reset_s(headers):
    """Seconds until the rate limit resets, from the IETF `RateLimit` header.

    **This, not `Retry-After`, is what the Hub actually sends.** It rate-limits
    by REQUEST COUNT over five-minute fixed windows in three buckets (api, pages,
    resolvers) and answers a 429 with
    `RateLimit: "resolvers";r=0;t=42` — `t` being the seconds left in the window
    (`draft-ietf-httpapi-ratelimit-headers`). There is no `Retry-After` on it, so
    parsing only that one meant our own fetch fell back to a guessed backoff
    while the exact answer sat unread in the response — and `snapshot_download`,
    the FALLBACK, has parsed this header since hf 1.2.0, so hf's own client was
    better informed about the wait than our fast path was.

    Several buckets can arrive in one header, and the interesting one is the
    bucket that is actually exhausted: `r=0`. With none of them at zero (or no
    `r` at all) the longest named reset wins — coming back too late costs a
    little throughput, coming back too early costs another 429.

    Anything unparseable is None, never a bogus zero: the caller's next source is
    strictly better than a wait this function invented.
    """
    if headers is None:
        return None
    raw = headers.get("RateLimit")
    if not raw:
        return None
    exhausted, named = [], []
    for entry in str(raw).split(","):
        params = {key.lower(): int(value)
                  for key, value in _RATELIMIT_PARAM.findall(entry)}
        reset = params.get("t")
        if reset is None or reset < 0:
            continue
        named.append(reset)
        if params.get("r") == 0:
            exhausted.append(reset)
    pool = exhausted or named
    return float(max(pool)) if pool else None


def _retry_after_s(headers):
    """`Retry-After` off a response, in seconds, or None if there is none to read.

    Kept beside `_ratelimit_reset_s` although the Hub does not send it: it costs
    nothing, our own mirror or whatever CDN fronts it may well send it, and the
    503 case in `_is_throttled` is defined in terms of it.

    Both forms the RFC permits, because both are served in the wild: delta
    seconds, and an HTTP-date. A date in the past (a clock skewed either way, a
    response that sat in a queue) is a wait of zero rather than a negative one.
    """
    header = ((headers.get("Retry-After") if headers is not None else None) or "").strip()
    if not header:
        return None
    try:
        return max(0.0, float(int(header)))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        # A `Retry-After` date is GMT by definition; a naive one is that,
        # not local time.
        when = when.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return max(0.0, (when - now).total_seconds())


def _is_throttled(error):
    """Is this exception the server asking us to wait?

    429 always — it is the ONLY thing the Hub answers a rate limit with, and it
    shapes no bandwidth, so there is nothing else to detect. 503 only WITH a
    `Retry-After`: a bare 503 is an overloaded or broken host, which is exactly
    what the ordinary retry budget is for, and treating every one of them as a
    throttle would turn a genuinely dead endpoint into a download that waited
    minutes before falling back.

    Anything with no status at all — a socket error, a bad manifest — is not a
    throttle, which is what lets this be asked of an arbitrary exception.
    """
    status = _http_status(error)
    if status == 429:
        return True
    return status == 503 and _retry_after_s(_http_headers(error)) is not None


def _throttle_wait_s(error, attempt):
    """How long to wait for this throttle: what the server asked, or a backoff.

    Precedence: the `RateLimit` reset (the Hub's own exact answer), then
    `Retry-After` (anyone else's), then a backoff of our own that doubles per
    attempt. Capped by `THROTTLE_WAIT_MAX_S` whichever it is.

    A named wait of ZERO falls through to the backoff rather than being honoured
    — `t=0` means "the window is resetting about now", and taken literally it
    turned the retry budget into an immediate re-request loop against a host that
    had just said it was over its limit.
    """
    headers = _http_headers(error)
    for named in (_ratelimit_reset_s(headers), _retry_after_s(headers)):
        if named:
            return min(named, THROTTLE_WAIT_MAX_S)
    return min(THROTTLE_WAIT_MAX_S, RETRY_BACKOFF_S * 2 ** (attempt - 1))


def _throttle_sleep(stop, seconds):
    """Wait `seconds`, in slices, giving up early once `stop` is set.

    In slices because a single `time.sleep` of a minute cannot be interrupted,
    and the ✕ a user presses reaches a segment only through `stop` (see
    `THROTTLE_WAIT_MAX_S`). Counted DOWN rather than measured against a
    deadline, so the loop terminates on the number of naps taken: a test that
    replaces the clock does not turn this into a spin.
    """
    while seconds > 0 and not stop.is_set():
        nap = min(THROTTLE_SLICE_S, seconds)
        time.sleep(nap)
        seconds -= nap


class _Throttle:
    """One stretch of being rate-limited: its waits, its bounds, its notice.

    A stretch, not a request. Both places that can be throttled — the segment
    loop and the Hub metadata call — need the same three things (honour the
    named wait, stay inside a budget, say so on the row), and the budget has to
    be shared across the CONSECUTIVE 429s that make up one stretch rather than
    reset per request. Two copies of that bookkeeping is how the count and the
    clock come to disagree.

    **`progressed()` is half the point.** A long download over a busy link is
    throttled in bursts, and the allowance is a claim about ONE burst: without
    the reset, the 61st 429 of a healthy multi-hour download — reached an hour
    apart, with gigabytes moved in between — was treated as an ordinary fault and
    spent the segment's retry budget on falling back to hf. Exactly the mistake
    `tries` avoids by resetting on the cursor moving, made one level up.
    """

    def __init__(self, hub, stop=None):
        self.hub = hub
        #: Never set for a caller that has nothing to cancel (the metadata call
        #: is a single request, not a multi-hour park), so the slicing loop reads
        #: it uniformly rather than branching on None.
        self.stop = stop if stop is not None else threading.Event()
        self.attempts = 0
        self.waited = 0.0

    def wait(self, error):
        """Wait out one throttle; False once the budget is spent.

        False is what turns a rate limit back into an ordinary failure, which is
        the right end state: a host that has been asking us to wait for ten
        minutes is not going to be waited out, and the fallback — hf's own
        downloader, which parses the same header — is a better answer than a
        parked segment holding a connection nobody else can use.
        """
        if self.attempts >= THROTTLE_ATTEMPTS or self.waited >= THROTTLE_TOTAL_MAX_S:
            return False
        self.attempts += 1
        seconds = min(_throttle_wait_s(error, self.attempts),
                      THROTTLE_TOTAL_MAX_S - self.waited)
        self.waited += seconds
        # Announced BEFORE the sleep: the announcement is the point — a download
        # that has gone quiet for a minute has to say why, and only the thread
        # being throttled knows.
        _note_throttle(seconds, hub=self.hub)
        _throttle_sleep(self.stop, seconds)
        return True

    def progressed(self):
        """The stretch is over — bytes moved, or the call went through."""
        self.attempts = 0
        self.waited = 0.0
        _clear_throttle()


def _throttled_retry(call, hub, stop=None):
    """`call()`, waiting out a rate limit instead of failing on one.

    For the requests that are NOT the chunk loop, and on the Hub they are the
    ones that get throttled. The Hub meters URLs with a `/resolve/` segment in
    them; our ranged GETs go to the presigned CDN location, which has none, so
    the metadata call is where a 429 realistically lands — and there it used to
    escape the segmented fetch entirely and take the whole repo into the
    fallback, with none of the waiting or disclosure below it.

    Anything that is not a throttle re-raises untouched, which is what lets this
    wrap a call whose other failures (`_Unsegmentable`, a socket error, a repo
    that moved) must reach their own handlers unchanged.
    """
    throttle = _Throttle(hub, stop)
    while True:
        try:
            value = call()
        except Exception as error:  # noqa: BLE001 - re-raised below unless it is a throttle
            if not _is_throttled(error) or not throttle.wait(error):
                raise
            continue
        throttle.progressed()
        return value


def _resolved_meta(repo_id, filename, revision, stop=None):
    """`_hub_file_meta`, waiting out a rate limit rather than failing on one.

    The single funnel for every Hub metadata call in this file — the pre-flight
    resolve and the mid-download re-resolve — so both get the same treatment from
    one place. `hub=True` unconditionally: this function IS the Hub path.
    """
    return _throttled_retry(
        lambda: _hub_file_meta(repo_id, filename, revision), hub=True, stop=stop)


class _FileFetch:
    """One file's download: its part file, its segments, its sidecar.

    Owns everything between "we know the etag" and "the snapshot entry exists",
    because those two are the only points at which the state on disk is state hf
    would recognise. Everything in between is ours and carries our own suffix.
    """

    def __init__(self, folder, repo_id, filename, revision, meta, token, stop,
                 probes=None, re_resolvable=True):
        self.folder = folder
        self.repo_id = repo_id
        self.filename = filename
        #: Every name in the repo that resolves to this blob. A repo really does
        #: publish the same bytes twice, and one etag is one blob — so the extra
        #: names are LINKS to make, never a second download to run.
        self.filenames = [filename]
        self.revision = revision
        self.meta = meta
        self.token = token
        self.stop = stop
        self.probes = {} if probes is None else probes
        #: Whether a fresh `location` can be obtained for this file at all. True
        #: on the Hub path, where `location` is a presigned CDN URL that expires;
        #: False for caller-supplied metadata, where there is nothing to refresh
        #: — see `_re_resolve`.
        self.re_resolvable = re_resolvable
        #: The digest to verify the finished blob against, or None. **Captured
        #: here, from the metadata this fetch was PLANNED with, and never re-read
        #: out of `self.meta` at publish time.** `self.meta` is reassignable
        #: (`_re_resolve` replaces it wholesale), and reading the digest late is
        #: exactly how the mirror path's hash check came to switch itself off
        #: silently in the one situation it exists for.
        self.verify = meta.get("sha256")
        self.size = meta["size"]
        self.blob = os.path.join(folder, "blobs", meta["etag"])
        self.part = self.blob + PART_SUFFIX
        self.sidecar = self.part + ".json"
        self.snapshot = os.path.join(folder, "snapshots", meta["commit"])
        #: One append-only stream instead of segments — see `_appends_only`.
        #: Snapshotted per fetch rather than asked again at every write: the
        #: answer cannot change under a running download, and a plan made one
        #: way must not be written the other.
        self.append = _appends_only()
        self.lock = threading.Lock()      # guards the segment cursors
        self.flush_lock = threading.Lock()  # one writer of the sidecar at a time
        self.fd = None
        self.segments = []
        self.pending = 0
        self.flushed = 0.0

    # -- planning ---------------------------------------------------------

    def plan(self):
        """The segments still to fetch. Empty means the bytes are already here.

        Segments share ONE fd, opened read-write and pre-sized, and write
        through `os.pwrite` — no userspace buffering, so bytes a segment has
        counted are bytes the kernel already has. That is precisely what makes a
        `SIGKILL` mid-download resumable rather than merely restartable.
        """
        if os.path.exists(self.blob) and os.path.getsize(self.blob) == self.size:
            # Whatever a previous attempt left is dead the moment the blob
            # exists: nothing will ever resume into it, and unremoved it is a
            # multi-gigabyte leak inside the hub cache that also goes on
            # counting towards the bar.
            _remove(self.part)
            _remove(self.sidecar)
            return []
        os.makedirs(os.path.dirname(self.blob), exist_ok=True)
        if self.append:
            return self._plan_append()
        saved = self._saved()
        if saved is not None:
            # The layout to resume with is the layout the bytes were fetched
            # INTO. A probe that fails for a moment (a 503 on the one-byte
            # request) must not cost us that: re-deriving on silence yields one
            # segment, a segment-count mismatch, and the deletion of gigabytes
            # of durable, correctly recorded progress — for a network condition
            # that says nothing about the bytes on disk.
            #
            # A probe that ANSWERS NO is different, and asking is what makes the
            # difference visible. Without it, a server that has stopped honouring
            # ranges hands byte 0 to every segment past the first, `_whole_body`
            # refuses, and the refusal takes down the whole repo — the fallback
            # then deleting this file's sidecar along with every OTHER file's
            # progress. Restarting this one file whole is strictly cheaper.
            #
            # `_chunks` is deterministic in `size` alone, so the layout it
            # derives here is the SAME plan a fresh download would make —
            # `_restore` below is what checks that the saved offsets actually
            # fit onto it.
            self.segments = _chunks(self.size)
            if not self._restore(saved):
                saved = None
            elif len(saved) > 1 and _probe_host(self.meta["location"],
                                                self._cdn_token(),
                                                self.probes) is False:
                saved = None
        if saved is None:
            # …and once the sidecar is out, its layout goes with it. Kept, it
            # would split a download that starts from zero by a plan that
            # described a file we just deleted: one connection for a 4.6GB
            # shard, or dozens for a small one.
            self.segments = _chunks(self.size)
            if len(self.segments) > 1 and _probe_host(self.meta["location"],
                                                       self._cdn_token(),
                                                       self.probes) is not True:
                # No confirmed range support: one connection for the whole
                # file rather than the chunk plan `_chunks` would otherwise
                # hand out, for the same reason `_whole_body` refuses a 200 at
                # a non-zero offset — every chunk past the first would be
                # handed byte 0.
                self.segments = [{"start": 0, "end": self.size - 1, "done": 0}]
            _remove(self.part)
            _remove(self.sidecar)
        self.fd = os.open(self.part, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _BINARY, 0o644)
        os.ftruncate(self.fd, self.size)
        self.flush(force=True)
        pending = [seg for seg in self.segments if not _seg_complete(seg)]
        self.pending = len(pending)
        return pending

    def _plan_append(self):
        """The same plan on a platform with no `os.pwrite`: one stream, appended.

        ONE segment covering the whole file, an fd opened `O_APPEND`, and no
        `ftruncate` to the final size. Those are the only three differences from
        `plan()` above and they are all one difference: **here the part file's
        LENGTH is the progress**, where on the segmented path the length is
        final from the first second and the cursors are the progress.

        That is how AI-5i's invariant is kept rather than traded away. A counted
        byte must be a byte the kernel already has, which is why out-of-order
        segments need an unbuffered positional write; a single sequential stream
        gets the same promise from `O_APPEND` itself, since every `os.write`
        lands at the end of the file and the end of the file is the only cursor
        there is. A `SIGKILL` therefore leaves a PREFIX — never a file with a
        hole in the middle that a length would misdescribe.

        The sidecar still licenses the resume, exactly as above: without one this
        part file is bytes of unknown provenance. And the one segment derived
        here is also what refuses a sidecar the SEGMENTED path wrote — four
        recorded segments against one derived, so `_restore` says no and the file
        restarts whole. It has to: that part file is pre-sized and full of holes,
        and appending onto it would publish a blob of exactly the right length
        and partly wrong content. The refusal holds in the other direction too,
        in `_saved`.

        Then the recorded cursor and the file are made to AGREE before a byte
        moves, by truncating the file back to the cursor. `flush` fsyncs the data
        before it writes the sidecar, so a recorded offset is always durable
        while the last second of writes may not be — a distinction the segmented
        path keeps by resuming from the recorded offset and overwriting anything
        past it positionally. Appending cannot overwrite, so the un-vouched-for
        tail goes instead: at most one flush interval of bytes re-fetched,
        against a resume that would otherwise append at a length no sidecar ever
        recorded.
        """
        self.segments = [{"start": 0, "end": self.size - 1, "done": 0}]
        saved = self._saved()
        if saved is not None and self._restore(saved):
            self.segments[0]["done"] = min(self.segments[0]["done"],
                                           _file_size(self.part))
        else:
            self.segments[0]["done"] = 0
            _remove(self.part)
            _remove(self.sidecar)
        self.fd = os.open(self.part,
                          os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW | _BINARY,
                          0o644)
        os.ftruncate(self.fd, self.segments[0]["done"])
        self.flush(force=True)
        pending = [seg for seg in self.segments if not _seg_complete(seg)]
        self.pending = len(pending)
        return pending

    def _cdn_token(self):
        """The credential to send with the BLOB request — usually none.

        huggingface_hub drops `Authorization` the moment the download URL
        differs from the Hub URL, and S3 is the reason: a presigned URL already
        carries its credentials in the query string, and a request bearing two
        authentication mechanisms is refused with a 400. Sent anyway, the probe
        fails and every segment burns its whole retry budget for any user with a
        token set — which is everyone pulling a gated model — and the download
        falls back to something SLOWER than what this replaced, silently,
        because the fallback is invisible by design.

        Computed per request rather than stored: `_re_resolve` can hand us a
        location on a different host than the one we started on.
        """
        return None if self.meta["location"] != self.meta["url"] else self.token

    def _saved(self):
        """The segments a previous run recorded for THIS file, or None.

        **Version first, before identity even gets a look-in.** The chunk
        queue changed what a segment list MEANS — fixed `CHUNK_BYTES` pieces
        rather than `size / N` equal shares — so a sidecar an older build
        wrote can have the right etag, the right size, and a layout that still
        passes the shape check in `_restore`, while every offset in it means a
        different byte than this build would derive for the same file. Etag
        and size agreeing says nothing about that; only the version does. A
        missing `version` — every sidecar written before this field existed —
        reads as a mismatch by construction, since `state.get` returns `None`
        and `None != SIDECAR_VERSION`.

        Identity next — etag, size, and a part file still as long as it was —
        because a sidecar belonging to a different revision of the file would
        have us skip bytes that were never fetched, and the result is a blob of
        exactly the right length that is silently wrong. The layout itself is
        checked in `_restore`, against the segments derived from this answer.

        **The part-file LENGTH check belongs to the pre-sized layout, so it is
        skipped on the append-only path** (`_appends_only`), where a part file
        shorter than the file it describes is the ordinary case — its length is
        the progress. Skipping it is not a hole: the two layouts still refuse to
        resume each other's part files, in both directions. A segmented run
        handed an APPENDED part file sees a short file where a pre-sized one is
        required and starts clean; an appending run handed a SEGMENTED one gets
        past this check and is refused by `_restore`, which derives one segment
        against the sidecar's many. Either way the answer is "no sidecar", which
        is only ever slower.

        **`isinstance(state, dict)` is checked explicitly, not left to fall out
        of a `KeyError`.** A sidecar whose JSON parses but is not an object — a
        truncated write that still happens to be valid JSON on its own, like a
        bare `2` or a list — has no `.get`, and `state["etag"]` on such a value
        raises `TypeError`, not `KeyError`; both were already caught here, so
        this was harmless before the version check was added. `state.get(...)`
        on a non-dict raises `AttributeError`, which was NOT in the tuple below
        — so a malformed sidecar stopped reading as "no sidecar" for this one
        file and instead escaped `plan()` entirely, taking the whole repo into
        the fallback and `_clear_parts` deleting every OTHER file's progress
        along with it. Checking the shape up front says directly what every
        line below it assumes, rather than relying on whichever accessor
        happens to be first to notice.
        """
        try:
            with open(self.sidecar) as handle:
                state = json.load(handle)
            if not isinstance(state, dict):
                return None
            if state.get("version") != SIDECAR_VERSION:
                return None
            if state["etag"] != self.meta["etag"] or state["size"] != self.size:
                return None
            saved = state["segments"]
            if not saved:
                return None
            landed = os.path.getsize(self.part)  # raises: no part file, no resume
            if not self.append and landed < self.size:
                return None
            return saved
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _restore(self, saved):
        """Put those offsets back, or say no.

        Validated in full BEFORE a single cursor is moved, so a half-accepted
        sidecar cannot land either.
        """
        try:
            if len(saved) != len(self.segments):
                return False
            for seg, old in zip(self.segments, saved):
                if old["start"] != seg["start"] or old["end"] != seg["end"]:
                    return False
                if not 0 <= old["done"] <= seg["end"] - seg["start"] + 1:
                    return False
        except (KeyError, TypeError):
            return False
        for seg, old in zip(self.segments, saved):
            seg["done"] = old["done"]
        return True

    def flush(self, force=False):
        """Record the offsets, durably, at most once a second.

        The ORDER here is the correctness argument for the whole feature:
        snapshot the cursors, fsync the DATA, then write the snapshot down.
        Recorded offsets are therefore always bytes the disk already has, never
        bytes still in flight — which a kill would lose while the sidecar went
        on claiming them, and a resume would then skip.

        Driven by the writing threads rather than by a timer of its own: a
        segment that is not moving has nothing new to record, and a thread would
        be one more thing to shut down. Written atomically, because a torn
        sidecar loses the whole download.
        """
        with self.lock:
            now = time.monotonic()
            if not force and now - self.flushed < FLUSH_EVERY_S:
                return
            self.flushed = now
            state = {"version": SIDECAR_VERSION, "etag": self.meta["etag"],
                     "size": self.size,
                     "segments": [dict(seg) for seg in self.segments]}
        with self.flush_lock:
            if self.fd is not None:
                os.fsync(self.fd)
            tmp = self.sidecar + ".tmp"
            # SPEC AI-29 (D533): `os.open` with `_NOFOLLOW` rather than a
            # plain `open(tmp, "w")` — the same symlink-planting defence the
            # `.part` file opens above already apply, extended to the
            # sidecar's own write-then-`os.replace` (an atomic rename does
            # not follow a symlink AT the destination, but writing the `.tmp`
            # source through a pre-planted symlink first would still hand an
            # attacker the CONTENT, and a subsequent `os.replace` would then
            # make `self.sidecar` itself become that symlink).
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _BINARY, 0o644)
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle)
            os.replace(tmp, self.sidecar)

    # -- moving the bytes -------------------------------------------------

    def run(self, seg):
        """Fill one segment, reconnecting until it is done or the budget is out.

        Reconnects on both kinds of interruption: an exception, and a body that
        simply ends early — a server closing mid-stream raises nothing.

        The budget resets on the CURSOR MOVING across a whole attempt, which is
        not the same as bytes arriving, and the difference is both a hang and an
        abort. Bytes arriving is too generous: a server that ignores `Range` and
        truncates hands back the same prefix every time, `_whole_body` rewinds
        the cursor to zero to take it safely, and a budget keyed on bytes never
        expires — the job hangs with the bar oscillating between 0% and 50%
        until someone kills the process. And it is too mean, because it was read
        from a drain that a raising `read()` never returns from: half a gigabyte
        on disk followed by a connection reset counted as a failed attempt, so a
        link that resets reliably exhausted the budget and took the whole
        multi-file download into a fallback that then deleted every recorded
        byte. The cursor before and after answers both.
        """
        ranged = len(self.segments) > 1
        refreshed = False
        tries = 0
        # Throttles are bounded apart from `tries`, in time rather than in
        # attempts, and reset by progress exactly as `tries` is — see
        # `_Throttle`.
        throttle = _Throttle(self.re_resolvable, self.stop)
        reason = "nothing was attempted"
        while tries < SEGMENT_ATTEMPTS and not self.stop.is_set():
            if _seg_complete(seg):
                return
            start = seg["start"] + seg["done"]
            want_range = ranged or seg["done"] > 0
            before = seg["done"]
            try:
                with _open(self.meta["location"], self._cdn_token(),
                           start if want_range else None,
                           seg["end"] if want_range else None) as response:
                    if want_range:
                        if getattr(response, "status", 200) != 206:
                            start = self._whole_body(seg)
                        else:
                            self._check_range(response, start)
                    self._drain(response, seg, start)
                if seg["done"] > before:
                    # Bytes are moving, so this stretch of being throttled is
                    # over: the allowance comes back and the row stops saying
                    # "waiting". Here rather than in the cursor-moved branch
                    # below, which a completed segment returns past.
                    throttle.progressed()
                if _seg_complete(seg):
                    return
                reason = f"the stream ended at byte {seg['start'] + seg['done']}"
            except urllib.error.HTTPError as error:
                if _is_throttled(error) and throttle.wait(error):
                    # A wait the server ASKED for, so it costs no attempt: the
                    # `continue` skips the retry budget below entirely. Once the
                    # throttle budget itself is spent, `wait` says False and this
                    # becomes the ordinary failure it now really is.
                    continue
                if error.code in (401, 403) and not refreshed and self.re_resolvable:
                    # `location` is a presigned CDN URL and a multi-hour
                    # download outlives it. Re-resolving does NOT count against
                    # the budget: an expired signature is not evidence that the
                    # file is unreachable. Its own failure is an ordinary
                    # network fault and must be COUNTED rather than escape —
                    # otherwise one unlucky moment aborts the whole download.
                    #
                    # **`self.re_resolvable` is what keeps this off the mirror
                    # path**, where the URL is commit-pinned and immutable: there
                    # is no signature to refresh, so a 401 or 403 there means the
                    # object is missing or misconfigured, and the ORDINARY retry
                    # below is the whole answer. Asking anyway made a request to
                    # huggingface.co in the middle of a download whose entire
                    # point is that huggingface.co is never contacted.
                    refreshed = True
                    try:
                        self._re_resolve()
                        continue
                    except _Unsegmentable:
                        raise
                    except Exception as again:  # noqa: BLE001 - hf raises its own family
                        reason = f"re-resolving after HTTP {error.code}: {again}"
                else:
                    reason = f"HTTP {error.code}"
            except _TRANSIENT as error:
                reason = f"{error.__class__.__name__}: {error}"
            if seg["done"] > before:
                # The cursor moved, so the connection worked, so BOTH allowances
                # come back. The re-resolve is one per stall and not one per
                # segment: a presigned URL is good for minutes and a
                # multi-gigabyte download is not, so a second expiry is ordinary
                # — and unhandled it spends the whole retry budget on 401s and
                # aborts into a fallback that then deletes the resumable state.
                #
                # The throttle allowance comes back with them, and for the same
                # reason: bytes arrived, so whatever the server was limiting a
                # moment ago it is serving now. (Reached when `_drain` raised on
                # top of real progress; the ordinary path resets inside the
                # `try` above.)
                tries, refreshed = 0, False
                throttle.progressed()
            else:
                tries += 1
                time.sleep(min(5.0, RETRY_BACKOFF_S * tries))
        if self.stop.is_set():
            return
        raise RuntimeError(f"{self.filename}: gave up at byte "
                           f"{seg['start'] + seg['done']} — {reason}")

    def _re_resolve(self):
        """A fresh presigned URL for this file. Only the LOCATION may change.

        Refuses outright when this fetch's metadata did not come from the Hub.
        The caller above already checks that, so this is the invariant stated
        where it is enforced rather than a second condition to keep in step: the
        one thing that must never happen is a Hub call, or a `self.meta`
        replacement, on a path that has neither a URL to refresh nor a Hub to
        ask.

        `etag`, `size` and `commit` are what the blob path, every segment offset
        and the snapshot folder were derived from before any thread started. A
        repo updated mid-download therefore has to abort, never continue: the
        new revision's bytes written at the old revision's offsets and published
        as `blobs/<old-etag>` are a mix of two revisions at exactly the right
        length, under a name hf will then serve from cache forever.
        """
        if not self.re_resolvable:
            raise _Unsegmentable(
                f"{self.filename}: this download has no re-resolvable location")
        fresh = _resolved_meta(self.repo_id, self.filename, self.revision,
                               stop=self.stop)
        for field in ("etag", "size", "commit"):
            if fresh.get(field) != self.meta[field]:
                raise _Unsegmentable(
                    f"{self.filename}: the repo changed mid-download "
                    f"({field} {self.meta[field]!r} -> {fresh.get(field)!r})")
        self.meta = fresh

    def _check_range(self, response, start):
        """A 206 is not a promise that it is the range we ASKED for.

        A proxy that clamps ranges answers `bytes=1150000-` with `Content-Range:
        bytes 0-…/size` — the scattering `_whole_body` refuses, wearing a legal
        status code. Written where it was asked for, one body's bytes land at
        four different offsets and the file is exactly the right LENGTH and
        entirely wrong content.
        """
        header = (response.headers.get("Content-Range") or "").strip()
        match = _RANGE_START.match(header)
        if not match or int(match.group(1)) != start:
            raise _Unsegmentable(
                f"{self.filename}: asked for byte {start}, got "
                f"{header or 'a 206 with no Content-Range'}")

    def _whole_body(self, seg):
        """Handle a 200 answering a request we ranged, or refuse to.

        A server that answered the probe with a 206 and then ignores `Range` is
        sending byte 0 to every segment. Writing that at a segment's own offset
        produces a file of exactly the right LENGTH and entirely wrong content —
        the one failure mode of this whole design that no size check would
        catch. Only the segment that starts at zero can use such a body, and it
        has to rewrite from the top rather than resume into it.
        """
        if seg["start"]:
            raise _Unsegmentable(
                f"{self.filename}: the server ignored Range on a segment "
                f"starting at byte {seg['start']}")
        with self.lock:
            seg["done"] = 0
            if self.append and self.fd is not None:
                # Rewinding the CURSOR is not enough where the cursor is the
                # file's length. An `O_APPEND` fd would write this body after
                # the bytes already there; the cursor would then reach `size`
                # over a file half again too long, `finish` would believe it,
                # and the blob published under a real etag would be exactly the
                # permanent failure this function exists to prevent. The
                # segmented path needs nothing here — its writes are
                # positional, so byte 0 of this body goes to offset 0 whatever
                # the part file already holds.
                os.ftruncate(self.fd, 0)
        return 0

    def _drain(self, response, seg, start):
        """Copy this response into the part file, advancing the cursor as it goes.

        Deliberately reports nothing: what an attempt achieved is the cursor's
        movement, which the caller can still read after this raises — and a
        `read()` raising mid-body, on top of bytes already written, is the case
        a returned flag got wrong.

        Three paths leave without the loop body ever running — an empty first
        read, no room left in the segment, and `stop` already set on entry —
        and the last of those is the ORDINARY one: `stop` is set exactly when a
        sibling segment has failed, so every other segment arrives here to wind
        down. A vestigial `return moved` survived the rewrite of this function
        and raised `UnboundLocalError` on all three, which is a `NameError` and
        therefore not in `_TRANSIENT`: it escaped the retry loop entirely and
        turned a tidy wind-down into the fallback deleting every recorded byte.
        """
        offset = start
        while not self.stop.is_set():
            chunk = response.read(READ_BYTES)
            if not chunk:
                break
            # A server ignoring the END of the range must not overrun into the
            # next segment's bytes.
            room = seg["end"] - (seg["start"] + seg["done"]) + 1
            if room <= 0:
                break
            chunk = chunk[:room]
            written = 0
            while written < len(chunk):
                if self.append:
                    # `O_APPEND`, so the write lands at the end of the file —
                    # which is this segment's cursor by construction, there
                    # being exactly one segment and nothing that seeks.
                    # `offset` is deliberately not consulted: see
                    # `_plan_append`. Still one syscall per write and no
                    # userspace buffer, which is the property that matters.
                    moved = os.write(self.fd, chunk[written:])
                    if not moved:
                        # A loop that trusts a syscall to make progress is a
                        # HANG rather than an error, and this one would spin on
                        # a non-empty buffer forever, burning a core with the
                        # download frozen and nothing in any log. Raising hands
                        # it to `_TRANSIENT` like any other write failure, which
                        # retries and then falls back.
                        raise OSError(f"{self.filename}: a write of "
                                      f"{len(chunk) - written} bytes moved none")
                    written += moved
                else:
                    written += os.pwrite(self.fd, chunk[written:],
                                         offset + written)
            offset += len(chunk)
            with self.lock:
                seg["done"] += len(chunk)
            self.flush()

    # -- publishing -------------------------------------------------------

    def finish(self):
        """Publish the blob and link it. The LAST segment's thread runs this.

        Checked against the CURSORS, never against the part file's length. The
        file is `ftruncate`d to its final size before a byte arrives, so its
        length is right from the first second and a sparse file of pure holes
        passes a size check — which would put a zero-filled blob under a real
        etag into the hub cache, where hf serves it from cache forever. The
        cursors are the same durable-byte accounting the sidecar records, and
        they are the only evidence there is that the file is whole.

        On the append-only path (`_appends_only`) the length happens to agree
        with the cursors, and the gate is still the cursors: ONE rule rather
        than a per-platform one, and the cursors are the stricter of the two
        anyway — `_whole_body` can rewind them, and a length that had not been
        rewound with them is exactly the state this must not publish.

        No hash on the HUB path, like huggingface_hub itself, which relies on TLS
        and `Content-Length`: re-reading every gigabyte off the disk would give
        back a good part of what this feature is for.

        **The MIRROR path is hashed, and the trade is genuinely different there.**
        A `sha256` in the metadata is what marks it (the Hub cannot give us one;
        see `mirror.file_meta`). On that path we are the origin, so nobody else
        would ever notice a bad byte we shipped — a corrupt upload, a truncated
        object, a cache poisoned somewhere between us and the user — and a wrong
        blob filed under a real etag is not a failed download but a PERMANENT
        one: hf's own loaders then serve those bytes out of the cache forever,
        and no later download would refetch them. One re-read of the file we just
        wrote, once, against a digest we generated from the same blob hf produced,
        is what makes that impossible.

        Verified BEFORE `os.replace`, not after. A blob is published the instant
        it is renamed, and a concurrent load can pick it up between the rename
        and any check that follows — so the check that has to hold is the one on
        the part file, where a failure leaves nothing in the cache to clean up.
        """
        if self.fd is not None:
            os.fsync(self.fd)
            landed = sum(seg["done"] for seg in self.segments)
            missing = [seg for seg in self.segments if not _seg_complete(seg)]
            if missing or landed != self.size:
                raise RuntimeError(
                    f"{self.filename}: {landed} of {self.size} bytes landed, "
                    f"{len(missing)} segment(s) short")
            if self.append and _file_size(self.part) != self.size:
                # **On this route the length is an INDEPENDENT witness, so it is
                # worth one syscall.** The cursors count what was handed to
                # `os.write`; the length is what the kernel actually kept, and
                # the two can only disagree if something between them rewrote
                # the bytes — which is precisely what a text-mode fd does
                # (`_BINARY`), and what no cursor and no `Content-Length` can
                # notice. It is not a duplicate of the check above: there the
                # question is whether every segment finished, here whether the
                # file those segments claim to have written is the size they
                # claim. On the SEGMENTED route the same check would be
                # theatre, since the file is `ftruncate`d to its final size
                # before a byte arrives — which is exactly why AI-5i gates
                # publishing on the cursors and says a length proves nothing
                # there. The Hub path carries no digest, so without this a
                # translated blob would be published under a real etag and hf
                # would serve it forever; with it, that download falls back.
                raise RuntimeError(
                    f"{self.filename}: {self.size} bytes were counted into a "
                    f"part file of {_file_size(self.part)} — something between "
                    f"this process and the disk rewrote them")
            os.close(self.fd)
            self.fd = None
            digest = self.verify
            if digest:
                # ONE read of the whole file, here — not per segment and not per
                # chunk. The segments write out of order, so there is no
                # streaming hash to keep: `_drain` sees the file's bytes in
                # whatever order the connections deliver them.
                actual = _blob_sha256(self.part)
                if actual != digest:
                    # The part file and its sidecar go with it. Kept, the next
                    # run would resume INTO bytes already known to be wrong and
                    # arrive at the same mismatch, forever.
                    _remove(self.part)
                    _remove(self.sidecar)
                    raise RuntimeError(
                        f"{self.filename}: the mirror served {actual[:12]} where "
                        f"the manifest says {digest[:12]}")
            os.replace(self.part, self.blob)
            _remove(self.sidecar)
        return self.link()

    def link(self):
        """The snapshot entry hf's own loaders read.

        A RELATIVE symlink into `blobs/`, matching hf's `_create_symlink`: an
        absolute one breaks the moment the cache is moved or read through
        another mount. Windows without developer mode cannot make one at all,
        and hf's own answer there is a copy, so ours is too.
        """
        targets = [os.path.join(self.snapshot, name) for name in self.filenames]
        for target in targets:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            relative = os.path.relpath(self.blob, os.path.dirname(target))
            _remove(target)
            try:
                os.symlink(relative, target)
            except OSError:
                shutil.copyfile(self.blob, target)
        return targets[0]

    def close(self):
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None


def _resolve(repo_id, filenames, revision, meta=None):
    """One metadata call per file, concurrently.

    Serially this is a round trip per file before a single byte moves, which on
    a repo of thirty shards is several seconds of nothing happening — the exact
    thing this feature exists to remove.

    `meta` is where that metadata comes FROM, defaulting to the Hub. A caller
    that already knows every file's url, etag, commit and size — the model
    mirror reads them out of one manifest — supplies its own and the Hub is not
    consulted at all. Same signature either way, so the pool below cannot tell
    the difference and neither can anything downstream of it.
    """
    # The Hub path goes through `_resolved_meta`, which waits out a 429 rather
    # than letting it abort the whole fetch: these are `/resolve/` URLs, which is
    # the bucket the Hub actually meters (the ranged GETs below go to a presigned
    # CDN location it does not). A supplied provider is somebody else's host and
    # is left exactly as it was handed to us.
    provider = meta or _resolved_meta
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_CONNECTIONS, len(filenames)),
            thread_name_prefix="meta") as pool:
        return list(pool.map(
            lambda name: provider(repo_id, name, revision), filenames))


def _run_segment(fetch, seg):
    """One unit of work in the pool: fill a segment, and finalise if it was the
    last one its file was waiting on.

    **`force=True` on every completion, so a 4.6GB shard now forces ~144
    sidecar fsyncs instead of 4 — raised in review and judged not worth
    changing yet.** The data most of that fsync flushes is already clean:
    `_drain`'s own periodic `flush()` (no `force`) runs throughout the
    segment on the same 1-second cadence regardless of chunk size, so a
    completed 32MB chunk typically has little UNFLUSHED data behind it and
    this call's real job is durably recording the LAST partial tick — the
    bytes written since that periodic flush last fired, which on a fast link
    can be most of the chunk. If this ever shows up in profiling on real
    storage, the change to make is dropping `force` here entirely and
    letting the periodic flush alone catch a finished segment: correctness
    would not regress (a segment whose completion lands between two
    periodic ticks just resumes as its last-recorded, slightly earlier
    offset — a bigger but still bounded re-fetch, not a corrupt one), only
    resume-efficiency would give up a little in exchange for far fewer
    forced fsyncs. Not made speculatively because nothing here has measured
    it as a real cost.
    """
    try:
        try:
            fetch.run(seg)
        finally:
            fetch.flush(force=True)
        if not _seg_complete(seg):
            return  # abandoned because a sibling segment failed; nothing to publish
        with fetch.lock:
            fetch.pending -= 1
            last = fetch.pending == 0
        if last:
            fetch.finish()
    except BaseException:
        # The other segments stop pulling bytes nobody is going to use, and
        # what they already wrote is recorded before they go. That state is for
        # a LATER RUN of the app: this attempt is about to hand the repo to
        # huggingface_hub, which fetches those files itself (see `_clear_parts`).
        #
        # `finish()` is INSIDE this guard, not after it. It fails for reasons
        # that have nothing to do with the download — a full disk, an
        # `os.replace` across devices, another instance publishing the same blob
        # first — and outside the guard that exception reached the caller with
        # `stop` still clear, so the pool's own shutdown waited for every
        # remaining segment of every remaining file to finish first: minutes and
        # gigabytes spent on a download that had already failed.
        fetch.stop.set()
        raise


#: "no `token` argument was passed", as distinct from "the token is None".
#: `None` is a real and meaningful value here — it is what the mirror path
#: passes to mean ANONYMOUS — so it cannot also stand for "ask hf's own store".
_ASK_HF = object()


def _segmented_fetch(model_id, filenames, revision, ref=DEFAULT_REVISION,
                     meta=None, token=_ASK_HF):
    """Fetch `filenames` into the hub cache ourselves. Returns the snapshot dir.

    `meta` and `token` are the two seams a non-Hub source needs, and both
    default to today's behaviour exactly. `meta` supplies the per-file metadata
    (see `_resolve`); `token` is the credential the requests carry, and the
    mirror path passes `None` for it — `_cdn_token` sends the Hub token whenever
    the blob URL IS the metadata URL, which is true of our own mirror by
    construction, and a user's Hub token has no business being offered to
    whatever host `FUSED_MODEL_MIRROR` names.

    Everything else is deliberately NOT a seam. The one-commit check, the
    asked-for-commit pin, the sparse-file requirement and the ref write are what
    make a fetch a SNAPSHOT rather than a pile of files, and they hold whoever
    supplied the metadata — all the more so for a manifest we read off a CDN.

    `revision` is REQUIRED and has no default on purpose: it must be the commit
    the caller's file list resolved to, and a default here is exactly how a list
    taken from one revision came to be fetched at another. `ref` is the branch
    NAME that resolved to it, recorded so a later offline load can resolve the
    same name — hf writes that ref too, and a cache without it needs the network
    to answer a question it already knows.

    The units of work in the pool are SEGMENTS ACROSS ALL FILES under one cap,
    which is what makes `MAX_CONNECTIONS` mean what it says: a pool per file
    would multiply the two caps together and open thirty sockets on a repo of
    thirty shards.

    **A platform with no `os.pwrite` fetches each file on one append-only
    stream instead of splitting it** (`_appends_only`), and that is the only
    thing it changes: the queue, the cap and this pool are untouched, so the
    serialization is per FILE and a repo of shards still moves on
    `MAX_CONNECTIONS` connections. It used to be an `_Unsegmentable` refusal
    here, which quietly meant the model mirror never fetched on Windows and no
    Windows acquisition ever reached our access logs (AI-5l).

    **No cache lock, unlike `snapshot_download`, and deliberately.** Two app
    instances fetching one repo would write the SAME bytes at the SAME offsets:
    the etag names the content, so there is no version of this race that puts
    wrong bytes in a blob. What can happen is wasted work — one instance's
    `os.replace` pulls the part file out from under the other, whose next
    syscall fails and whose download falls back to hf — and that costs a slower
    download, never a corrupt cache. Inside one app it cannot happen at all:
    the supervisor's deterministic job id joins a second Download of a model
    onto the first (AI-5a).
    """
    folder = repo_folder(model_id)
    if not folder:
        raise _Unsegmentable("the hub cache layout is unavailable")
    if not filenames:
        raise _Unsegmentable("the Hub listed no files for this repo")
    if not _appends_only() and not _sparse_ok(folder):
        # The sparse requirement belongs to the PRE-SIZED part file, which the
        # append-only path never creates. Refusing here anyway would take this
        # whole fetch — and with it the model mirror — off the one platform
        # `_appends_only` exists to keep it on.
        raise _Unsegmentable(f"{folder} cannot hold a sparse file")

    if token is _ASK_HF:
        token = _hf_token()
    stop = threading.Event()
    probes = {}  # host -> range support, so a repo of shards probes once
    fetches, by_etag = [], {}
    resolved = _resolve(model_id, filenames, revision, meta)
    for name, info in zip(filenames, resolved):
        if not (isinstance(info.get("size"), int) and info.get("etag")
                and info.get("commit")):
            # "reported", not "the Hub reported": the metadata may have come
            # from a caller's own manifest (see `meta` above), and a message
            # naming the Hub for a mirror manifest sends a reader to the wrong
            # place.
            raise _Unsegmentable(f"{name}: no size, etag or commit was reported")
        # SPEC AI-29 (D533): a `name`/`etag` that would escape the snapshot
        # or blobs directory is refused here, before a `_FileFetch` is ever
        # constructed — falling back to hf's own downloader (which does its
        # own, independently-maintained path handling) rather than joining
        # either into a cache path this module controls.
        if not _safe_repo_relative_name(name):
            raise _Unsegmentable(f"{name}: not a safe repo-relative path")
        if not _safe_blob_name(info["etag"]):
            raise _Unsegmentable(f"{name}: etag is not a safe blob name")
        already = by_etag.get(info["etag"])
        if already is not None:
            # One etag is one blob, and a repo really does publish the same
            # bytes under two names. A second fetch of it would share the part
            # file, the sidecar and the blob path with the first: the bytes
            # pulled twice, and whichever `os.replace` lost the race finding
            # nothing there and taking the whole download into the fallback.
            already.filenames.append(name)
            continue
        fetch = _FileFetch(folder, model_id, name, revision, info, token, stop,
                           probes, re_resolvable=meta is None)
        by_etag[info["etag"]] = fetch
        fetches.append(fetch)

    commits = {fetch.meta["commit"] for fetch in fetches}
    if len(commits) != 1:
        # One revision is one commit. Two would mean the repo moved under us
        # mid-listing, and half a snapshot of each is not a snapshot.
        raise _Unsegmentable(f"one revision reported {len(commits)} commits")
    if _COMMIT_SHA.match(revision or "") and revision not in commits:
        # Asked for a commit and given another: the listing this file set came
        # from no longer describes what the Hub is serving.
        raise _Unsegmentable(f"asked for commit {revision}, the Hub resolved "
                             f"{commits.copy().pop()}")

    try:
        work = []
        for fetch in fetches:
            pending = fetch.plan()
            if pending:
                work.extend((fetch, seg) for seg in pending)
            else:
                fetch.finish()  # already on disk, or restored complete: just file it
        if work:
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(MAX_CONNECTIONS, len(work)),
                    thread_name_prefix="fetch") as pool:
                futures = [pool.submit(_run_segment, f, seg) for f, seg in work]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
    finally:
        for fetch in fetches:
            fetch.close()

    _write_ref(folder, ref, commits.pop())
    return fetches[0].snapshot


def _write_ref(folder, ref, commit):
    """`refs/<branch>` -> the commit, so a later load resolves it offline.

    Only for a branch NAME: a revision that is itself a sha needs no ref, and
    writing one named after a sha is not something hf would ever read.
    """
    if not ref or _COMMIT_SHA.match(ref):
        return
    path = os.path.join(folder, "refs", ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(commit)


def _clear_parts(folder):
    """Drop our partial files before handing the repo back to huggingface_hub.

    Not because hf would misread them — the suffix exists precisely so it never
    sees them — but because hf is about to fetch those same files ITSELF. Kept,
    they would count towards the progress bar for a download nothing is writing,
    and then sit in the hub cache forever beside the blob hf finished: nothing
    ever resumes into a part file whose blob already exists.

    So the resume story is honest about its scope. It covers the app being
    killed, quit or crashed — the case that motivated it (AI-5e) — and not a
    fetch that failed its way into the fallback, which re-downloads.
    """
    for dirpath, _dirs, files in os.walk(folder or ""):
        for name in files:
            if PART_SUFFIX in name:
                _remove(os.path.join(dirpath, name))


def _fallback(model_id, error, source="segmented fetch"):
    """Say why we are back on hf's downloader. The supervisor captures stderr,
    so a fallback that happens in the field is diagnosable rather than merely
    slow.

    `source` names WHICH path gave up — the segmented Hub fetch or the model
    mirror. One line saying "segmented fetch" for a mirror that 404s sends a
    reader to the wrong half of the feature, and the two fail for entirely
    different reasons.
    """
    sys.stderr.write(
        f"[fused] {source} of {model_id} unavailable, falling back to "
        f"huggingface_hub: {error.__class__.__name__}: {error}\n")
    _clear_parts(repo_folder(model_id))


# --------------------------------------------------------------- the mirror path
#
# A suggested model can come off a distribution WE run instead of off
# huggingface.co — see `mirror.py` for the protocol, the two environment
# variables and why the permission is per-model. Everything below is the hook:
# one branch inside `download_snapshot` for a repo and one inside `download_file`
# for a single file (AI-5m — a different object with a weaker claim, not the same
# manifest read loosely), so every runner call site is untouched and any failure
# lands on the Hub path unchanged.

#: The loaded `mirror` module, or False for "there is no usable one here".
#: Cached because the answer cannot change within a process, and False rather
#: than None so a failed load is not retried on every call.
_MIRROR = None


def _mirror_module():
    """`mirror.py` from beside this file, or None.

    Loaded LAZILY and BY PATH, both deliberately. `worker_base` is imported two
    ways — as a bare module by a worker, which puts `runners/` on `sys.path`, and
    by absolute path from the tests, which does not — so a plain `import mirror`
    resolves in one and not the other. And loading it here rather than at module
    scope keeps this file's module-scope imports stdlib-only, the rule
    `test_ai_worker_base` enforces by reading this source.

    None for a missing or broken file, because a runner venv that somehow lacks
    it should download from the Hub, not fail.
    """
    global _MIRROR
    if _MIRROR is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mirror.py")
        try:
            spec = importlib.util.spec_from_file_location("fused_model_mirror", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _MIRROR = module
        except Exception as error:  # noqa: BLE001 - no mirror is a slower download
            sys.stderr.write(f"[fused] the model mirror client is unavailable: "
                             f"{error.__class__.__name__}: {error}\n")
            _MIRROR = False
    return _MIRROR or None


def _mirror_snapshot(model_id, allow_patterns=None, ignore_patterns=None):
    """The repo off our own mirror, or None to say "take the Hub path".

    None for every way of not having a mirror — not configured, not permitted
    for this model, no manifest, a manifest that does not hold up, a 404, a 5xx,
    a mid-download drop, a hash that does not match. A mirror that is down must
    cost a slower download and never a failed one, so the ONLY thing that
    escapes here is `Cancelled`: a ✕ must not be answered by starting the
    download again somewhere else.

    **The whole branch is inside one guard, the manifest call included.** It was
    outside it, on the reasoning that `mirror.manifest` returns None rather than
    raising — which is what that function intends and not what it guaranteed: a
    truncated chunked body raises `http.client.IncompleteRead`, which is neither
    an `OSError` nor a `ValueError`, so it escaped the client AND this function
    and FAILED a download the Hub could have served. Both halves are fixed, and
    the promise this feature is allowed to exist on should not rest on having
    enumerated every exception a URL library can raise.

    The manifest is filtered by the SAME `selects` the Hub listing goes through,
    so a scoped download (`torch_image` passes an allow-list) measures and
    fetches the same subset on both paths — and the fetch record is written with
    that scope, which is what makes the second download of a mirrored model come
    back off the fast path.
    """
    mirror = _mirror_module()
    if mirror is None:
        return None
    try:
        if not mirror.allowed(model_id):
            return None
        manifest = mirror.manifest(model_id)
        if manifest is None:
            return None
        files = [(entry["name"], entry["size"]) for entry in manifest["files"]
                 if selects(entry["name"], allow=allow_patterns,
                            ignore=ignore_patterns)]
        if not files:
            # A manifest that selects nothing at this scope is not a mirror hit;
            # the Hub listing is the authority on what the scope was supposed to
            # match.
            return None
        names = [name for name, _size in files]
        fetched = fetch_with_progress(
            model_id,
            lambda: _segmented_fetch(
                model_id, names, manifest["commit"],
                meta=mirror.file_meta(model_id, manifest),
                # Anonymous, always. The blob URL is the metadata URL here, so
                # `_cdn_token` would otherwise hand a user's Hub token to
                # whatever host `FUSED_MODEL_MIRROR` names.
                token=None),
            total=_total_bytes(files))
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - every failure degrades to the Hub
        _fallback(model_id, error, source="the model mirror")
        return None
    # Recorded exactly as the Hub path records it, and for the same reason: what
    # landed is a normal hf cache entry, so the cached-model fast path, the Local
    # tab's inventory and deletion all read it without knowing where it came
    # from. `_record_fetch` verifies the names are really there and writes
    # nothing if they are not.
    #
    # **`names` comes from the manifest here, where the Hub path takes it from
    # the Hub listing — and `_record_fetch`'s shortfall check is only as good as
    # that list's independence.** Against the manifest alone the check would be
    # self-certifying: a manifest missing `config.json` would download a subset,
    # record the subset as complete at this scope, and every later bring-up would
    # then be served a snapshot that cannot load, with nothing left to refetch
    # it. What makes the list trustworthy is that `mirror.manifest` refuses any
    # manifest that does not ASSERT it lists the whole repo at this commit, and
    # `scripts/build_model_mirror.py` sets that assertion only after checking the
    # snapshot against the Hub's own listing — on a build machine, where asking
    # the Hub costs nothing. The independence is real; it just lives at build
    # time, because asking the Hub here is the one thing this feature exists to
    # avoid.
    _record_fetch(repo_folder(model_id), _commit_of(fetched), names, fetched,
                  allow=allow_patterns, ignore=ignore_patterns)
    return fetched


def _mirror_file(repo_id, filename, detail=None, job=None, row=None):
    """ONE file off our own mirror, or None to say "take the Hub path" (AI-5m).

    The `_mirror_snapshot` above cannot serve this. A GGUF repo publishes dozens
    of quantizations of the same model — `unsloth/Qwen3.5-9B-GGUF` is 147.81GB
    whole — and `llama_text.download` wants one 2.6GB file out of it, while a
    per-repo manifest is only accepted when it ASSERTS it lists the whole repo at
    that commit. Earning that assertion would mean mirroring all 147.81GB to
    serve the one file, and weakening it would break AI-5k. So this reads a
    different object: `mirror.file_manifest`, one named file, no completeness
    claim (see that function for why the claim is unnecessary here rather than
    merely inconvenient).

    **No `_record_fetch`, and that is load-bearing rather than an omission.**
    `download_file` has never written one — one file is not a scope a later
    bring-up can be told is complete — so there is no record for a wrong manifest
    to make self-certifying, which is precisely what lets the manifest be
    claim-free. Adding a record here would take that argument away.

    Same degradation rules as the snapshot branch, for the same reason: the whole
    body including the manifest call sits inside one guard, every failure returns
    None after saying which path gave up, and only `Cancelled` escapes (AI-5e).
    """
    mirror = _mirror_module()
    if mirror is None:
        return None
    try:
        if not mirror.allowed(repo_id):
            return None
        manifest = mirror.file_manifest(repo_id, filename)
        if manifest is None:
            return None
        entry = manifest["files"][0]
        return fetch_with_progress(
            repo_id,
            lambda: os.path.join(
                _segmented_fetch(repo_id, [filename], manifest["commit"],
                                 meta=mirror.file_meta(repo_id, manifest),
                                 # Anonymous, always, for `_mirror_snapshot`'s
                                 # reason: the blob URL is the metadata URL
                                 # here, so `_cdn_token` would otherwise hand a
                                 # user's Hub token to whatever host
                                 # `FUSED_MODEL_MIRROR` names.
                                 token=None),
                filename),
            total=entry["size"], detail=detail, job=job, row=row)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - every failure degrades to the Hub
        _fallback(repo_id, error, source="the model mirror")
        return None


class _HubByteTicker:
    """A `tqdm_class` for hf's OWN downloaders, and the counter it writes into.

    The fallback path's bar used to be driven only by the disk walk, and
    `hf_xet` (installed in every runner venv; every mlx-community repo is
    Xet-backed) delivers bytes in BURSTS rather than a steady trickle:
    measured on a 481MB repo, `bytes_on_disk` sat on one number for 6 seconds,
    then jumped ~90MB, then landed the final ~45% all at once on completion.
    Scaled to a 4.6GB model that is a bar parked on one number for a MINUTE
    while the download is perfectly healthy — the "stuck at 98%" report. hf
    knows the true count the whole time; this reads it through the one seam
    hf exposes for exactly that, and the disk walk stays as the heartbeat and
    the fallback for whatever this cannot read.

    **The outer bar is a file counter, and reporting it as bytes is the
    original AI-5b trap one level further in.** `snapshot_download` hands its
    `tqdm_class` to THREE distinct bars: one wrapping `hf_thread_map` over the
    file list — `desc="Fetching N files"`, no `unit` at all, one `.update(1)`
    per file — and two created directly for BYTES, `unit="B"`, one per
    conceptual stream (`_create_progress_bar` in hf's own `_snapshot_download`
    and `_xet_progress_reporting`). `unit == "B"` is what tells them apart:
    when `unit` is absent it is the file counter, which is exactly how "10 /
    11 B" happened before — the same bug this feature exists to fix,
    reappearing one seam further in.

    **Two byte streams, not one, and they must not be SUMMED.** hf's Xet path
    reports network TRANSFER (`desc` containing "downloading bytes") and disk
    RECONSTRUCTION (`desc` containing "reconstruct") separately — both cover
    close to the same total under dedup/compression, so adding them can read
    past 100% of a file that is not yet done. `bytes_on_disk` means "landed on
    disk", which is what the reconstruction stream already means, so ticks
    into the "reconstruct" bucket and the "transfer" bucket are kept apart and
    the counter reports whichever is FURTHER ALONG — never their sum. A plain
    `http_get` download (no Xet) hands back exactly one bar, whose `desc` is a
    filename and matches neither keyword; it lands in the transfer bucket,
    which is exactly the bytes it wrote, so the two-bucket split costs nothing
    on that path.

    `seen` is False until some byte bar reports ANYTHING, which is what lets
    the caller tell "no usable counter" (an hf version that reports
    differently, or ignores `tqdm_class` altogether, or a fetch that never
    goes near hf's downloaders at all) from "the counter says zero because
    nothing has landed yet" — the two must not be confused, or a slow start
    would look like this feature having failed rather than a download that is
    merely early.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._transfer = 0
        self._reconstruct = 0
        self.seen = False

    @property
    def value(self):
        with self._lock:
            return max(self._transfer, self._reconstruct)

    def bar(self):
        """A tqdm-COMPATIBLE class bound to this ticker — not a `tqdm`
        subclass, deliberately: hf's own `_create_progress_bar` hands a
        non-tqdm `cls` straight through as `cls(**kwargs)`, with none of
        tqdm's `disable`/`name` machinery grafted on, which this needs
        exactly as little of. Read back off an instance: `.n`, `.total` (hf's
        own aggregation helpers check both with `getattr`/`hasattr`) and
        `.format_dict` — verified against the real huggingface_hub installed
        in the runner venvs (1.27.0, 1.28.0): `_AggregatedTqdm.set_postfix_str`
        forwards to `_set_aggregate_rate_postfix`, which does
        `bar.format_dict.get("rate")` on OUR bar, called by
        `XetDownloadProgressReporter.update_progress` on the first non-zero
        byte increment. Missing it is not a cosmetic gap: it is an
        `AttributeError` raised from inside the download, on every
        Xet-backed repo — which is every mlx-community one — the moment the
        first byte lands. Called: `.update`, `.set_postfix_str`.
        """
        ticker = self

        class _Bar:
            def __init__(self, *_args, **kwargs):
                self.n = kwargs.get("initial") or 0
                self.total = kwargs.get("total")
                self._is_bytes = kwargs.get("unit") == "B"
                self._reconstruct = "reconstruct" in (kwargs.get("desc") or "").lower()

            @property
            def format_dict(self):
                # hf reads only `rate` off this (see the docstring above); `n`
                # and `total` are included too since real tqdm's own
                # `format_dict` carries them and a future caller reading
                # either would otherwise hit the same missing-attribute crash
                # this property exists to stop. `rate` is genuinely unknown —
                # this class does no timing of its own — and `None` is what
                # `_format_speed_postfix` already renders as "???B/s".
                return {"rate": None, "n": self.n, "total": self.total}

            def update(self, n=1):
                if not self._is_bytes or not n:
                    return
                with ticker._lock:
                    # `self.n` too, not just the ticker's own counters: it is
                    # what `format_dict` above reports back to hf, and a
                    # bar's own count has to agree with what it told the
                    # ticker it saw.
                    self.n += n
                    if self._reconstruct:
                        ticker._reconstruct += int(n)
                    else:
                        ticker._transfer += int(n)
                    ticker.seen = True

            def __enter__(self):
                return self

            def __exit__(self, *_exc_info):
                return False

            def close(self):
                pass

            def refresh(self):
                pass

            def set_postfix_str(self, *_args, **_kwargs):
                pass

            def set_description(self, *_args, **_kwargs):
                pass

        return _Bar


def fetch_with_progress(model_id, call, total=None, detail="Fetching weights…",
                        job=None, row=None, counter=None):
    """Run `call()` on a thread, reporting progress once a second.

    `call` is whatever huggingface_hub function actually fetches — a whole
    snapshot for one runner, a single GGUF file for another — and this is the
    part neither of them should write twice: the poll is the progress AND the
    heartbeat, without which a long single-file download reports nothing for
    minutes and the manager calls the row abandoned. The ONE-SECOND TICK is
    unconditional regardless of what drives `done` below it — the tick itself
    is the heartbeat (AI-5h), and a fetch that happens to have a live byte
    counter must keep beating exactly as often as one that does not.

    `counter`, when given, is a `_HubByteTicker` whose `tqdm_class` `call` was
    built to pass to hf's own downloader. Once it has `seen` anything, `done`
    is `max(counter.value, bytes_on_disk(folder))` rather than the disk walk
    alone — never less than either, so a counter that stalls while the disk
    still advances (or the reverse) cannot make the bar go backwards. Before
    it has seen anything — no counter at all, an hf version that reports
    differently, a `tqdm_class` silently ignored — this degrades to exactly
    the disk-walk-only behaviour from before it existed. See `_HubByteTicker`
    for why a byte counter existing at all used to be exactly the AI-5b trap
    ("10 / 11 B") one level further in.

    **`job`/`row` exist because not every fetch belongs to a download job.** A
    runner that pulls a component model DURING a request — the speech detector,
    the two diarization models — is reporting into a row the supervisor opened
    for a transcription, not into this process's `JOB_ID` (which is the model's
    own load row, long since finished). `job` sends the tick to the right row;
    `row` is that row's IDENTITY (`supervisor.transcribe_row_fields`), restated
    on every tick because the manager can evict and rebuild any row at any tick
    and a report with no `title` is refused outright.

    `kind`/`unit` are this function's own and override the row's, deliberately:
    for the length of the fetch the row IS a download and 6MB of 26MB is what a
    person wants to see. The next tick from the work itself restates the row's
    own pair, so the flip is for the duration and not a rename.

    **The tick carries the ✕ back**, and that became load-bearing the moment a
    fetch could land on a transcription row. It ticked with a plain `report`
    while these fetches owned a model-load row, whose ✕ the supervisor answers
    by killing the process — so nothing here had to. A component fetch reports
    into a row whose `cancellable` is True and whose ✕ must stop THIS work, and
    with a plain `report` the user pressed it, the manager set
    `cancel_requested`, and 33MB carried on downloading behind a row that went
    on saying "running". The reply to the tick we were sending anyway is the
    only channel that reaches a thread parked inside huggingface_hub.

    `CANCEL` is consulted too, but ONLY when `job` was passed. That flag is the
    `/cancel` route's, it belongs to the generation holding `GENERATE_LOCK`, and
    it is cleared by `_single`/`_stream` on the way in — so it means this fetch
    exactly when this fetch is inside a request. A model download runs on
    `_bring_up`'s own thread with no such lock, where a flag left set by an
    earlier cancelled generation would abort a download nobody asked to stop.

    **A ✕ that lands as the fetch FINISHES is not honoured**, which is the same
    rule `_call_with_ticks` states: the bytes are on the disk, and throwing them
    away would make the next attempt re-download what this one already has. So
    the final report is a plain `report`. The abandoned fetch thread is a
    daemon nobody waits for — it finishes into a result that is discarded, and
    huggingface_hub resumes partial files, so the bytes are not lost either.
    """
    folder = repo_folder(model_id)
    if total is None:
        total = repo_total_bytes(model_id)
    # `total_scope` explicit on EVERY tick, not left for the row's default or
    # a caller a level up to have set (code review, SPEC AI-5n/D498):
    # `total_scope` is STICKY on the job row (`jobs.py` only overwrites it
    # when a tick's body names it), and `download_plan` wraps a multi-phase
    # download with its own ticker beating `total_scope="download"` on a
    # tighter cadence than this function's own one-second ticks. Without this,
    # a tick from THIS phase landing right after one of `download_plan`'s
    # beats would leave the row saying `total_scope="download"` under a total
    # that is only this phase's own (smaller) figure — `modelSize.ts` would
    # then let a partial phase total win outright over the catalog's constant,
    # showing e.g. 19.1GB for a 28.5GB download at the phase boundary. `total`
    # here is always THIS call's own phase total (`download_plan` prices the
    # WHOLE download separately, through its own `_tick`), so `"phase"` is
    # unconditionally correct for every tick this function sends.
    identity = {**(row or {}), "kind": "download", "unit": "bytes", "total_scope": "phase"}
    # A notice left over from a PREVIOUS fetch is not about this one. It cannot
    # happen in a download-only worker (one process, one download), but a
    # resident worker fetches component models during requests, and a fetch that
    # ended while a segment was still parked on a 429 would otherwise open the
    # next row already claiming to be rate-limited.
    _clear_throttle()

    def measured():
        """Bytes done right now, from whichever source is actually moving.

        `bytes_on_disk` always runs — it is the heartbeat's OWN measurement
        too, and the fallback the moment `counter` cannot answer. `counter`,
        once it has `seen` anything, can only raise the reported number: a
        burst that lands on disk between two of hf's own ticks is still real
        progress, and `max` is what keeps either source's silence from
        hiding the other's advance.
        """
        disk = bytes_on_disk(folder)
        if counter is not None and counter.seen:
            return counter.value if disk is None else max(disk, counter.value)
        return disk

    def tick(**fields):
        """One progress report that can carry a ✕ back. See the docstring.

        A throttle notice WINS over the caller's `detail`. It is the one thing
        the row can say that the caller does not know: "Fetching weights…" over
        a download the Hub has parked is a true sentence that reads as a lie,
        and the segment threads have no other way to reach this row.
        """
        notice = _throttle_detail()
        if notice:
            fields["detail"] = notice
        report_or_cancel(job=job, **identity, state="running", **fields)
        if job is not None and CANCEL.is_set():
            raise Cancelled()

    tick(detail=detail, done=_capped(measured(), total), total=total)

    result = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as e:  # noqa: BLE001 - carried out and re-raised on the caller's thread
            result["error"] = e

    thread = threading.Thread(target=run, name="fetch", daemon=True)
    thread.start()
    while thread.is_alive():
        thread.join(timeout=1.0)
        if not thread.is_alive():
            # Finished during the join. Ticking now would be the late-cancel
            # the docstring refuses — the bytes are already on the disk.
            break
        tick(done=_capped(measured(), total), total=total, detail=detail)
    if "error" in result:
        raise result["error"]
    # Land on the total rather than on the last measurement: the snapshot
    # symlinks are not counted, so a finished repo measures slightly under its
    # own size and a bar that stopped at 98% reads as a download that gave up.
    report(job=job, **identity, state="running",
           done=total or measured(), total=total)
    return result["value"]


# ------------------------------------------------------- the already-cached path
#
# **A model already complete on disk is resolved WITHOUT touching the network**,
# and the reason is that "Fetching weights…" for a cached model was costing about
# a second of wall clock before any weight was read. Measured on this machine for
# `mlx-community/whisper-tiny.en-8bit`, fully cached: `download_snapshot` 483ms
# and `download_file` 456ms, against ~14ms for the actual `load()` inside
# mlx-whisper. All of it is Hub round-trips — `HfApi().model_info(files_metadata=
# True)` is 228ms on its own and hf's own `snapshot_download` spends another
# ~220ms revalidating etags — and it is also the source of the "You are sending
# unauthenticated requests to the HF Hub" line in every worker log. The same two
# answers off the cache alone are 0.13ms and 0.14ms.
#
# **The trade, stated so the next reader does not have to rediscover it: a model
# already complete on disk will NOT pick up a newer Hub revision.** Nothing here
# re-checks `main` once the cache can answer, so a repo that was re-uploaded
# under the same branch keeps serving the bytes this machine already has until
# something else forces a re-check (a cache clear, a fetch of a file this
# snapshot does not have, or a caller that scopes the download differently).
# That is deliberate (D367): bring-up latency and working offline are worth more
# here than revision freshness, because these are pinned model snapshots a user
# downloaded on purpose rather than a moving dependency — and a silently changing
# set of weights under a name the user chose would be the worse surprise anyway.
#
# What must NOT change is a first download, and that is the whole shape of this:
# the local attempt either answers completely or it is discarded, and everything
# below it — the metadata call, the total, the segmented fetch, the progress
# reporting — runs exactly as it did before.
#
# **What "complete" can and cannot mean without a listing** — this is the part a
# code review corrected, and the correction is the reason for the two rules
# below. hf's own completeness check (`_raise_if_incomplete_snapshot`) verifies
# the snapshot against `trees/<commit>.json`, and hf's own comment says that with
# no tree listing cached "we cannot tell, so we do nothing". `_segmented_fetch`
# never writes one: it publishes blobs and hand-writes `refs/<branch>`
# (`_write_ref`), and it is the NORMAL path — hf's downloader runs only on
# fallback. So for essentially every repo this app fetched itself, hf's check is a
# no-op, and it must not be counted as evidence. It was, in the first cut of this,
# and the claim that the gap was unreachable was simply wrong.
#
# **So this app records its own fetches, and the fast path answers only from that
# record.** `_record_fetch` writes `<repo folder>/.fused-fetch-<commit>.json` when a
# download completes, holding the SCOPE it was asked for and the file names the
# LISTING asked for — verified present at write time, and not written at all if they
# are not (see `_record_fetch`: a list built from what happened to land would be
# self-certifying). `_cached_path` serves a request only when the record is for the
# commit hf resolved, at the scope being asked for, and every recorded name is
# present and settled. That makes "nothing would be downloaded" a claim about the
# disk rather than an inference from it, and it is what the two earlier attempts
# were reaching for:
#
# * **Scoping is a property of the on-disk STATE, not of the call.** Refusing
#   scoped CALLS — the previous rule — left the reachable half open, because the
#   same repo id reaches BOTH kinds of call: `diffusers_image` fetches
#   `black-forest-labs/FLUX.2-klein-4B` scoped to `recipe["keep"]`, and
#   `mflux_image.download` fetches whatever id it is given unscoped, with
#   `/api/ai/runtime/download` choosing between them from the user's image-engine
#   preference and `weights_only=True` stopping before the `load()` that would
#   refuse the format. A download on the MLX engine after one on the Diffusers
#   engine would have been answered from a cache holding a tenth of the repo,
#   reporting success having fetched nothing. The same flip needs no user at all if
#   a recipe is ever REMOVED: `download()` takes its unscoped branch against a
#   still-scoped cache, and `from_pretrained` fails on a component nobody fetched.
#   A recorded scope answers both — and lets a scoped call USE the fast path when
#   the scope on disk is its own.
# * **A file that went away has to be DETECTABLE.** A blob pruned together with its
#   snapshot entry leaves nothing for a walk of the snapshot to trip over; only a
#   list of what should be there catches it. Before this path existed, pressing
#   Download again re-listed and re-fetched exactly that, so short-circuiting
#   without the list quietly removed the app's only repair route. Checking recorded
#   NAMES also makes the verification exact instead of structural: nothing depends
#   on `os.walk`, which does not follow directory symlinks by default and so used
#   to refuse a snapshot whose files sit under a linked subdirectory — a fast path
#   wrongly declined rather than wrongly taken, but wrong.
# * **`_settled` per name**, which is where an interrupted download's part files
#   are ruled out — per BLOB rather than per repo, for the reason `_settled` gives.
#
# A cache with no record — every machine that already holds models when this ships
# — takes the networked path exactly as before and gains one on the way out, so the
# next bring-up is the fast one. No migration step, and no cache is ever served on
# the strength of a record this app did not write.
#
# **Two further rules the first cut got wrong, both about not doing MORE than
# looking.** (1) A repo the cache has never held must not reach a hub download
# function at all, not even with `local_files_only=True`: `tests/test_ai_hub_
# fetch.py` states the invariant that pressing Stop must not start a download,
# and it enforces it by counting `snapshot_download`/`hf_hub_download` calls —
# correctly, because "we only passed local_files_only" is precisely the kind of
# claim that stops being true when an argument gets dropped in a refactor. So the
# gate is a plain filesystem look FIRST (`_has_cached_snapshot`,
# `try_to_load_from_cache`), and hf is consulted only once the cache is known to
# hold something for this repo. (2) `Cancelled` must never be read as "not
# cached". It is an ordinary `Exception`, so a bare `except Exception` here turned
# a ✕ into "fall back to the network" — which is the one degradation this file
# forbids everywhere else, because it starts a fresh multi-gigabyte download out
# of pressing Stop. `_NOT_CACHED` is therefore an explicit tuple, and every hub
# answer that means "the cache cannot serve this" is inside it.

#: What "the cache cannot answer" arrives as, and nothing else.
#:
#: Both of hf's own verdicts are `OSError` subclasses, checked against
#: huggingface_hub 1.28: `LocalEntryNotFoundError` is
#: `(EntryNotFoundError, FileNotFoundError)` and `IncompleteSnapshotError` — a
#: snapshot its tree listing says is missing files — derives from that. A cache
#: directory this process cannot read is an `OSError` too, and an `ImportError` is
#: what a venv without the library looks like.
#:
#: **Named as a tuple rather than caught broadly on purpose**: `Cancelled` is a
#: plain `Exception` and is exactly what must NOT be swallowed here (see above),
#: and neither is a `KeyboardInterrupt` or a bug in this file. A hub version that
#: invents a new not-cached error outside this tuple costs a slow bring-up; a
#: `Cancelled` inside it costs the user a download they pressed Stop on.
_NOT_CACHED = (OSError, ImportError)

#: hf's own marker for a blob it is still writing. Ours is `PART_SUFFIX`.
_HF_PART_SUFFIX = ".incomplete"


def _settled(path):
    """Whether the file at `path` is finished rather than mid-download.

    **Per BLOB, deliberately, and this replaced a repo-wide scan.** A leftover
    part file is not a fact about a repo, it is a fact about one blob — and it
    persists BY DESIGN, because it is the resume state (AI-5i). Scanning the whole
    repo folder therefore let one cancelled download disable the fast path for
    every unrelated, fully-cached file in the same repo, forever: a multi-GGUF repo
    where the user stopped one quantization kept paying the ~450ms this exists to
    remove for the dozen it already had.

    The three names are spelled out rather than matched by suffix, which also
    settles an old inconsistency (`endswith(PART_SUFFIX)` here against
    `PART_SUFFIX in name` in `_clear_parts`): our part file, its offsets sidecar
    — `<part>.json`, which a suffix test misses — and hf's own marker. hf 1.x also
    writes uuid-named `.incomplete` blobs that no per-blob name can predict, and
    that is covered from the other side: an unfinished blob is not a file
    `try_to_load_from_cache` or a snapshot symlink resolves to at all.

    `realpath` first, because the caller holds a snapshot entry and the markers sit
    beside the BLOB it links into.
    """
    blob = os.path.realpath(path)
    return not any(os.path.lexists(blob + suffix) for suffix in
                   (PART_SUFFIX, PART_SUFFIX + ".json", _HF_PART_SUFFIX))


def _has_cached_snapshot(folder):
    """Whether the cache holds ANY snapshot directory for this repo.

    The purely-local gate in front of every hub call on the fast path, and the
    reason it exists is the invariant above: a repo this machine has never
    downloaded must not reach `snapshot_download` at all. `snapshots/` is hf's own
    layout — the same one `bytes_on_disk` and `_clear_parts` already walk — and if
    hf ever moves it this answers False, which costs the fast path and takes the
    networked one. Slower, never wrong.
    """
    if not folder:
        return False
    try:
        with os.scandir(os.path.join(folder, "snapshots")) as entries:
            return any(entry.is_dir() for entry in entries)
    except OSError:
        return False


#: One completed fetch, per commit, in the repo's own cache folder.
#:
#: In hf's cache directory rather than beside the app's other state, because it
#: describes THAT cache and has to die with it: `hf cache delete` and a manual
#: `rm -rf` of the repo folder both take it, which is the only correct lifetime for
#: a record of "what is on this disk". Dot-prefixed so it cannot be mistaken for
#: repo content, and per-commit so a second revision writes its own rather than
#: overwriting the answer for the first.
_FETCH_RECORD = ".fused-fetch-%s.json"

#: The suffix of the file a record is written to before it is `os.replace`d into
#: place. `_has_fetch_record` has to EXCLUDE it: a half-written record is not a
#: record, and reading the prefix alone made a record-less repo pay a hub resolve
#: on every download.
#:
#: **Excluded, never deleted, and made unique per writer.** Two model loads sharing
#: one HF cache are separate processes with no lock between them, and this name is
#: what a fetch in flight is writing RIGHT NOW — so sweeping it to save a round trip
#: made the other process's `os.replace` fail, its record never get written, and its
#: repo stay permanently cold, which is the failure the record exists to prevent.
#: A pid AND a random token keep two writers off each other's file; each cleans up
#: only its own.
#:
#: **A temp left by a HARD KILL is therefore permanent, and that is the accepted
#: trade rather than an oversight.** With the token, no other writer can tell a
#: leftover from a live stage, so the only safe automatic reclamation would be by
#: AGE — and the thing being reclaimed is a few hundred bytes written inside a
#: window of about a millisecond (one `json.dump` of a name list, then `os.replace`),
#: invisible to `_all_present`, `_recorded_files`, `_has_fetch_record` and
#: `_clear_parts`, and counted by `bytes_on_disk` only as its own length. So the
#: exposure is one file per crash that lands inside that window per repo, against a
#: delete that can strike a live writer and cost exactly the permanently cold repo
#: this record exists to prevent. The user's own reaper takes them either way:
#: `hf cache delete` and an `rm -rf` of the repo folder remove the temp with the
#: record and the weights.
_RECORD_TEMP = ".writing"


def _temp_record(name):
    """The name THIS writer stages a record in before publishing it.

    **The pid is not enough, and the failure it leaves is worse than the one this
    design replaced.** Two containers sharing a mounted HF cache have their own pid
    namespaces, and a pid is reused after a crash in any case — so a pid-only name
    can be one file that two writers interleave into, and `os.replace` then publishes
    mixed JSON as TRUTH. The sweep this replaced only ever cost a cold repo. The
    random token is what makes "a temp is distinguishable from another writer's"
    actually true; the pid stays because it makes a leftover attributable when
    somebody is looking at the directory wondering where it came from.
    """
    return "%s.%d-%s%s" % (name, os.getpid(), uuid.uuid4().hex[:8], _RECORD_TEMP)


def _scope_key(allow, ignore):
    """A fetch's scope, as one comparable value.

    Sorted, because `allow_patterns` is a list whose ORDER means nothing to
    `fnmatch` — two callers naming the same patterns in a different order must not
    read as two different scopes. `None` and `[]` both come out as "unscoped",
    which is what `selects` already treats them as.
    """
    return {"allow": sorted(allow or ()), "ignore": sorted(ignore or ())}


def _commit_of(snapshot):
    """The commit a resolved snapshot directory IS, or None — hf's cache lays it out
    as `snapshots/<commit>`.

    One function for the reader and both writers, so a record can never be filed
    under a different name than the one the fast path looks up: that is exactly how
    the fallback used to file records under the listing's sha while hf had landed
    somewhere else, leaving the repo permanently cold.

    **None for anything that is not a plausible commit, and that is not
    defensiveness — the empty string is a key that READS BACK.** A path with no
    basename made the writer file `.fused-fetch-.json` and the reader look up the
    very same name, so a record written under nothing at all came back as a hit,
    which is the opposite of the miss this promises. `_COMMIT_SHA` is the same
    40-hex test `_segmented_fetch` already applies to a revision, so a `local_dir`
    download or a layout change answers None here, nothing is recorded, and the
    download takes the networked path. Slower, never wrong.
    """
    name = os.path.basename(os.path.normpath(snapshot or ""))
    return name if _COMMIT_SHA.match(name) else None


def _record_fetch(folder, commit, names, snapshot, allow=None, ignore=None):
    """Write down that a fetch of `names` at this scope COMPLETED for `commit`.

    Called after a download returns, which is the only moment this is knowable:
    `_write_ref` runs after the last file lands, so reaching there SHOULD mean the
    whole requested set is on disk — and the request's own scope is what makes "the
    whole set" mean anything at all. Should, not does: that is checked below rather
    than trusted, because the two fetch paths filter the listing with different
    matchers and a partial fallback is a real thing.

    **A SHORTFALL writes nothing at all**, and that is the whole reason `names`
    comes from the listing rather than from the disk. A record is verified by
    looking its own names up, so building it out of "whatever landed" would make it
    self-certifying: a fetch that delivered 1 of 50 files would record one name,
    every later check would pass, and the fast path would serve an incomplete
    snapshot forever. Checked against the set the LISTING asked for — the one thing
    here the fetch did not choose — a fetch that fell short leaves no record, and a
    repo with no record is merely cold, which is where it was before this existed.
    The shortfall is named on stderr because silence is what would make it
    invisible: a repo that never warms up is otherwise indistinguishable from one
    nobody loaded twice. **That applies to the two ANOMALIES — a shortfall, and a
    fetch that returned no snapshot path — and deliberately not to the ordinary
    nothings** (no cache folder, a revision that is not a commit, a listing that
    selected nothing), which are shapes the world produces rather than signs
    something went wrong, and which would be noise on every `local_dir` download.

    **Best-effort, and never in the way.** A finished download must not fail
    because a record could not be written: the weights are there either way, and
    the only cost of a missing record is a slower next bring-up. Written to a
    per-writer temporary name and `os.replace`d, so a crash mid-write leaves either
    the old record or none — never half of one that a later fast path would read as
    truth, and never another process's file (see `_RECORD_TEMP`).
    """
    if not folder or not commit or not names:
        # Three ORDINARY nothings, and silent for that reason: no folder is a venv
        # with no huggingface_hub, no commit is `_commit_of` refusing a path that is
        # not a sha (a `local_dir` download, say), and no names is a listing that
        # selected nothing. None of them is a fetch that went wrong.
        return
    if not snapshot:
        # An ANOMALY, unlike the three above: both callers reach here only after a
        # fetch returned, so a fetch that returned no path at all is a bug in this
        # file rather than a shape the world produces — and it is named for the same
        # reason the shortfall below is, that a repo which never warms up is
        # otherwise indistinguishable from one nobody loaded twice.
        #
        # It cannot be defaulted to `""` and joined: every presence check would
        # become CWD-relative, and a process whose working directory happens to hold
        # a matching name — `config.json` is not far-fetched — would pass the
        # shortfall check and record a fetch whose snapshot nobody had located.
        sys.stderr.write(
            f"[fused] the download of {commit[:12]} succeeded but reported no "
            f"snapshot path, so it is not recorded for the cached-model fast path. "
            f"This is not a failure: the next load re-resolves over the network.\n")
        return
    # Presence only, deliberately: `_settled` is a READ-time question — a part file
    # can appear beside a blob after this runs, and `_all_present` asks both at the
    # moment the answer is used, which is the moment that matters.
    missing = [name for name in names
               if not os.path.exists(os.path.join(snapshot, name))]
    if missing:
        # **Worded as the diagnostic it is, because it fires on a download that
        # WORKED.** `selects` and hf's `filter_repo_objects` disagreeing by one name
        # is the real possibility this whole check exists for, and when it happens
        # the weights are on disk and the load is about to succeed — so a line
        # shaped like an error had a user reading a perfect download as a broken
        # one. Silence was the original problem, so the line stays and says what is
        # true: nothing failed, only the shortcut was declined.
        sys.stderr.write(
            f"[fused] the download of {commit[:12]} succeeded; not recording it "
            f"for the cached-model fast path, because {len(missing)} of "
            f"{len(names)} listed files are not in the snapshot "
            f"({', '.join(missing[:3])}{', …' if len(missing) > 3 else ''}). "
            f"This is not a failure: the next load re-resolves over the network, "
            f"exactly as it did before that shortcut existed.\n")
        return
    path = os.path.join(folder, _FETCH_RECORD % commit)
    payload = {"commit": commit, "scope": _scope_key(allow, ignore),
               "files": sorted(names)}
    temporary = os.path.join(folder, _temp_record(_FETCH_RECORD % commit))
    try:
        os.makedirs(folder, exist_ok=True)
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(temporary, path)
    except OSError:
        # Ours and ours alone — the random TOKEN in the name is what makes that
        # true, not the pid, which two containers over one mounted cache can share
        # (see `_temp_record`) — so cleaning it up here cannot take a record
        # another writer is in the middle of staging.
        _remove(temporary)


def _has_fetch_record(folder):
    """Whether this repo folder holds ANY fetch record.

    Asked BEFORE hf is consulted, so a cache filled before this existed — or by
    somebody else's tooling — costs one `scandir` and no hub call at all on its way
    to the networked path. The per-commit lookup still has to happen afterwards;
    this only avoids asking hf to resolve a snapshot whose completeness nothing
    here could vouch for anyway.
    """
    prefix = _FETCH_RECORD.split("%s")[0]
    try:
        with os.scandir(folder) as entries:
            return any(entry.name.startswith(prefix)
                       and not entry.name.endswith(_RECORD_TEMP)
                       for entry in entries)
    except OSError:
        return False


def _recorded_files(folder, commit, allow, ignore):
    """The file list a completed fetch of THIS scope recorded, or None.

    None for every way of not knowing — no record, a record for another commit or
    another scope, a record this process cannot read or cannot parse. They all mean
    the same thing to the caller (take the networked path), and none of them is
    worth a warning: no record is the normal state of every cache filled before
    this existed.
    """
    if not folder or not commit:
        return None
    try:
        with open(os.path.join(folder, _FETCH_RECORD % commit),
                  encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return None
    if record.get("commit") != commit:
        return None
    if record.get("scope") != _scope_key(allow, ignore):
        return None
    files = record.get("files")
    return files if isinstance(files, list) and files else None


def _all_present(snapshot, names):
    """Whether every recorded name is in the snapshot, resolved and settled.

    `os.path.exists` FOLLOWS symlinks, which is the point: a snapshot entry is a
    link into `blobs/`, and a blob pruned or never copied leaves the link behind.
    Checked by NAME rather than by walking the directory, so a file that vanished
    WITH its entry is caught — the case a walk cannot see at all — and nothing
    depends on how `os.walk` treats a linked subdirectory.
    """
    for name in names:
        entry = os.path.join(snapshot, name)
        if not os.path.exists(entry) or not _settled(entry):
            return False
    return True


def _cached_path(model_id, resolve, allow=None, ignore=None):
    """`resolve()`'s answer if the cache can serve it with NO network, else None.

    `resolve` is the hf call with `local_files_only=True` — the same function and
    the same arguments the networked path uses, so the local answer cannot be a
    differently-scoped one. It is reached only once the cache is known to hold a
    snapshot for this repo and to carry no interrupted download's leftovers,
    because on the fast path hf is asked to CONFIRM something the filesystem
    already suggested rather than asked to go and look.

    Failures come back as None so the caller takes the networked path — but only
    the ones in `_NOT_CACHED`. Anything else propagates, `Cancelled` above all:
    the whole point of this file's degradation rules is that a ✕ is the one
    failure that must not be answered by starting a download.

    The answer is verified against this app's OWN record of what it fetched (see
    the note above this section): the record has to be for the commit hf resolved,
    at the scope being asked for, and every name in it has to be present and
    settled. A repo with no record is never served, which is what makes "nothing
    would be downloaded" a fact rather than an inference.

    The commit comes from the resolved path's own basename — hf's cache puts a
    snapshot at `snapshots/<commit>` — and if that ever stops being true the lookup
    finds no record and the download takes the networked path. Slower, never wrong.
    """
    folder = repo_folder(model_id)
    if not _has_cached_snapshot(folder) or not _has_fetch_record(folder):
        return None
    try:
        path = resolve()
    except _NOT_CACHED:
        return None
    if not path or not os.path.isdir(path):
        return None
    names = _recorded_files(folder, _commit_of(path), allow, ignore)
    return path if names and _all_present(path, names) else None


def _cached_file(repo_id, filename):
    """One file's path if the cache already holds it, else None.

    `try_to_load_from_cache` rather than `hf_hub_download(local_files_only=True)`,
    because it is hf's own read-only cache lookup: it resolves the ref and the
    blob off the disk and CANNOT download, so the "pressing Stop starts a
    download" invariant holds by construction rather than by an argument that a
    later edit might drop. Measured at 0.6ms for a hit and 0.1ms for a repo the
    cache has never seen.

    It has three answers and only one of them is a path: a `str`, `None` for "not
    cached", and a `_CACHED_NO_EXIST` sentinel for "the Hub said this file does
    not exist and that was cached too". The `isinstance` check covers the
    sentinel without importing a private name, and the `isfile` covers a blob
    deleted under a ref that still points at it.
    """
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return None
    try:
        path = try_to_load_from_cache(repo_id, filename)
    except _NOT_CACHED:
        return None
    if not isinstance(path, str) or not os.path.isfile(path):
        return None
    return path if _settled(path) else None


def download_snapshot(model_id, allow_patterns=None, ignore_patterns=None, **kwargs):
    """The repo, with progress. What most runners mean by "download".

    The total is measured against the SAME patterns the download uses, or a pull
    that deliberately fetches part of a repo measures itself against weights it
    was never going to fetch — a bar that stalls partway and then jumps. The
    segmented fetch takes its file list from the same filter, for the same
    reason.

    Both scopes are first-class arguments rather than `**kwargs` precisely so
    that they reach `_repo_files` too: an `allow_patterns` that only reached
    `snapshot_download` would fetch a tenth of a repo behind a bar priced at all
    of it.
    """
    def local():
        from huggingface_hub import snapshot_download

        return snapshot_download(model_id, local_files_only=True)

    # Already on disk, at this scope, and complete? Then there is nothing to
    # download and nothing to report: no metadata call, no etag revalidation, no
    # bar that fills in one tick. See the note above this section for what
    # "complete" is allowed to mean here and what it costs in revision freshness.
    # `kwargs` is excluded outright — an argument this function does not know about
    # changes what a download IS, and no record describes it.
    if not kwargs:
        cached = _cached_path(model_id, local, allow=allow_patterns,
                              ignore=ignore_patterns)
        if cached:
            return cached

        # Our own mirror, for a suggested model, BEFORE the Hub is consulted at
        # all — the point of the feature is a download that involves
        # huggingface.co nowhere, so a listing call here would defeat it. Excluded
        # under `kwargs` for the same reason the fast path above is: an argument
        # this function does not know about changes what a download IS, and a
        # manifest describes the whole repo at one commit and nothing else.
        mirrored = _mirror_snapshot(model_id, allow_patterns, ignore_patterns)
        if mirrored:
            return mirrored

    # ONE listing, serving the bar's total, the list to fetch AND the revision
    # to fetch it at. Asking twice is a second round trip before any byte moves;
    # deciding the revision separately is how a list from one revision comes to
    # be fetched at another.
    sha, files, total = None, None, None
    try:
        sha, files = _repo_files(model_id, allow=allow_patterns,
                                 ignore=ignore_patterns)
        total = _total_bytes(files)
    except Exception as error:  # noqa: BLE001 - the bar can proceed on a guess; the fetch cannot
        _fallback(model_id, error)

    # Fail BEFORE a byte moves, when the listing gave us a real total (SPEC
    # AI-26) — deliberately after the listing (so this never runs for a
    # cached/mirrored fast-path return above) and before either fetch path
    # below opens a connection.
    _ensure_disk_space(total, repo_folder(model_id))

    def hub(revision=None):
        from huggingface_hub import snapshot_download

        # PINNED when we have a commit, for `_segmented_fetch`'s reason (AI-5i): a
        # branch name resolved twice is two answers, and a repo that moved between
        # the listing and the fallback would land a different commit than the one
        # the total, the file list and the fetch record all describe.
        #
        # ONE dict that `kwargs` updates, not two splatted into one call: the
        # caller's own `revision` has to win, and splatting both raised
        # `TypeError: got multiple values for keyword argument 'revision'` instead
        # — masked only because a call carrying `kwargs` returns before the pin is
        # applied, which makes it a crash waiting on a reorder rather than a
        # non-issue.
        options = {"allow_patterns": allow_patterns,
                   "ignore_patterns": ignore_patterns}
        if revision:
            options["revision"] = revision
        # Ours unless `kwargs` already names one — a caller that passed its
        # own `tqdm_class` gets it back unmolested, and `counter` simply never
        # sees anything (`measured` in `fetch_with_progress` degrades to the
        # disk walk on its own; see `_HubByteTicker`).
        ticker = _HubByteTicker()
        options.setdefault("tqdm_class", ticker.bar())
        options.update(kwargs)
        return fetch_with_progress(
            model_id, lambda: snapshot_download(model_id, **options), total=total,
            counter=ticker)

    if kwargs or files is None or not sha:
        # An extra argument changes WHAT is fetched — `allow_patterns`, a
        # revision, a local dir — and a fetch that quietly ignored one would
        # download the wrong thing. Ours honours exactly the two it knows about.
        # A listing with no sha is the same problem: nothing to pin to.
        return hub()
    names = [name for name, _size in files]
    try:
        fetched = fetch_with_progress(
            model_id, lambda: _segmented_fetch(model_id, names, sha), total=total)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - every failure degrades to hf's downloader
        _fallback(model_id, error)
    else:
        # The one moment completeness is knowable: the fetch returned, so the
        # listing's names should all be on disk — `_record_fetch` checks that rather
        # than taking it on trust, and writes nothing if they are not. Recorded WITH
        # the scope that produced it (what a later fast path compares itself
        # against) and keyed by the commit the returned snapshot IS rather than by
        # the sha we asked for, so a record cannot be filed under a name the reader
        # will not look up.
        _record_fetch(repo_folder(model_id), _commit_of(fetched), names, fetched,
                      allow=allow_patterns, ignore=ignore_patterns)
        return fetched
    fell_back = hub(revision=sha)
    # Same rule as above, and the same reason it is checked rather than assumed:
    # hf filters with `filter_repo_objects` where `_repo_files` filtered with
    # `selects`, and the two are written to agree rather than guaranteed to. Where
    # they disagree the listing's set is not all there, `_record_fetch` writes
    # nothing, and this repo stays on the networked path — which is the safe
    # direction, unlike recording whatever happened to land. Recording at all is
    # what keeps a repo that ever fell back from being cold forever.
    _record_fetch(repo_folder(model_id), _commit_of(fell_back), names, fell_back,
                  allow=allow_patterns, ignore=ignore_patterns)
    return fell_back


#: How often `download_plan`'s own ticker corrects the row back to the GRAND
#: total. Deliberately shorter than `fetch_with_progress`'s own one-second
#: cadence (see `download_plan`'s docstring for why two tickers exist at all):
#: a tighter interval here means the window where a viewer can catch a phase's
#: OWN, smaller total on screen is well under a second rather than up to one.
_PLAN_TICK_S = 0.3


def _phase_total(model_id, allow, ignore, folder):
    """One phase's own total, in bytes, or None — WITHOUT a Hub metadata call
    when the phase is already complete on disk per this app's own
    completeness record (SPEC AI-5l's fast path in `download_snapshot`,
    reused rather than reimplemented).

    This is `download_plan`'s fix for the bug review caught: pricing every
    phase up front via a bare `repo_total_bytes()` call means an LTX bring-up
    with `mlx-community/gemma-3-12b-it-4bit` already cached contacted the Hub
    anyway, purely to price a phase that needed no network to answer at all
    — the exact "no metadata call, no etag revalidation" fast path
    `download_snapshot` documents for itself, silently defeated one level up
    by a pricing step that never checked. `_cached_path` is the SAME check
    (`local_files_only=True`, verified against our own fetch record) that
    function runs first, so a fully-offline bring-up with every phase already
    on disk now prices the whole plan without touching the network once.

    **Still not perfectly mirror-transparent.** A phase NOT in our own
    completeness record but servable entirely by `_mirror_snapshot` (AI-5l)
    still pays one Hub metadata call here before `download_snapshot`'s own
    mirror check gets a chance to run — `_mirror_snapshot` does not offer a
    cheap, network-free "would you serve this" probe separate from actually
    fetching, so there is no way to skip pricing for that case without either
    duplicating its logic or triggering the fetch itself twice. Narrower than
    the reported defect, which is specifically about an ALREADY-CACHED phase,
    and accepted rather than solved here.
    """
    def local():
        from huggingface_hub import snapshot_download

        return snapshot_download(model_id, local_files_only=True)

    if _cached_path(model_id, local, allow=allow, ignore=ignore):
        return bytes_on_disk(folder)
    return repo_total_bytes(model_id, allow=allow, ignore=ignore)


def download_plan(phases):
    """Fetch several repos as ONE download, priced at their SUM (SPEC AI-5n,
    D498). Returns each phase's own `download_snapshot` result, in order.

    `phases` is an ordered list of `(model_id, allow_patterns, ignore_patterns)`
    — every repo one logical "Download" button touches. `ltx-video` fetches
    the LTX weights and then `mlx-community/gemma-3-12b-it-4bit` as two
    sequential `download_snapshot` calls; reporting only the first repo's
    total is AI-5b's original defect rebuilt one level up — true of a phase,
    silent about the download the button actually started.

    **The grand total is summed ONCE, before a byte moves**, from
    `_phase_total` per phase — the SAME call `download_snapshot` makes for
    its own scoped total when a phase is not already on disk, so a phase that
    only fetches part of a repo is priced against exactly what it fetches, in
    the sum as much as on its own. See `_phase_total`'s own docstring for how
    an already-complete phase is priced with no network at all.
    **A phase whose size cannot be answered costs the WHOLE total**, not
    just its own: `total` is `None` the moment any phase's is, rather than
    summing the KNOWN phases and pretending the rest cost nothing — that is
    AI-5b's defect rebuilt a second way, a bar priced at a fraction that jumps
    the instant the indeterminate phase's bytes start landing. Indeterminate is
    honest; partial is not.

    **Each phase still runs through `download_snapshot`, unmodified.** AI-5l's
    mirror branch, AI-5i's segmented fetch and the already-complete fast path
    all still apply per phase exactly as they do for a bare call — this
    function COMPOSES them, it does not reimplement any of them.

    **A second reporting layer rides alongside each phase's own, and the two
    are not coordinated.** `download_snapshot` has no way to know it is one of
    several phases, so it goes on reporting its OWN phase-scoped total on its
    OWN cadence (`fetch_with_progress`'s one-second tick) — reused code, not
    duplicated here. A background ticker corrects the row back to the GRAND
    total on a TIGHTER cadence (`_PLAN_TICK_S`) for the life of the whole
    plan, naming the phase in `detail` ("Fetching weights… (2 of 2)") and
    marking `total_scope="download"` so `modelSize.ts` knows this total may
    win outright rather than only ever raise the catalog's constant. Because
    the two tickers are independent, a viewer CAN catch a tick that briefly
    shows one phase's own smaller total before the next correction lands —
    reporting has always been best-effort here (see `report`'s own
    docstring), and the alternative is threading a "stay quiet" flag through
    `download_snapshot`'s three fetch paths (mirror, segmented, hub fallback)
    to suppress its own ticks, which is exactly the duplication this function
    exists to avoid. `_PLAN_TICK_S` keeps that window well under a second.

    **The closing tick only lands on SUCCESS.** A phase that raises — a
    network failure, or `Cancelled` from the ✕ — must not be followed by a
    tick claiming `done=<the grand total>`: that is a finished-download shape
    for a download that did not finish, the same lie a `state="running"`
    revival elsewhere in this file is written not to tell. `fetch_with_progress`
    itself never sends a closing tick on failure either — the exception is the
    report — and this function now matches it.
    """
    resolved = list(phases)
    if not resolved:
        return []

    folders = [repo_folder(model_id) for model_id, _allow, _ignore in resolved]
    per_phase_totals = [
        _phase_total(model_id, allow, ignore, folder)
        for (model_id, allow, ignore), folder in zip(resolved, folders)
    ]
    total = None if any(t is None for t in per_phase_totals) else sum(per_phase_totals)
    count = len(resolved)

    def grand_done():
        """`bytes_on_disk` summed across every phase's folder, or None the
        moment any one of them cannot answer — the same all-or-nothing rule
        `total` follows above, so `done` and `total` never disagree about
        which phases they cover."""
        parts = [bytes_on_disk(folder) for folder in folders]
        return None if any(part is None for part in parts) else sum(parts)

    current_phase = [1]

    def _tick(**fields):
        report(state="running", kind="download", unit="bytes",
              total_scope="download", total=total, **fields)

    _plan_stop = threading.Event()

    def beat():
        while not _plan_stop.wait(_PLAN_TICK_S):
            n = current_phase[0]
            detail = (f"Fetching weights… ({n} of {count})" if count > 1
                      else "Fetching weights…")
            _tick(detail=detail, done=_capped(grand_done(), total))

    ticker = threading.Thread(target=beat, name="download-plan", daemon=True)
    ticker.start()
    try:
        results = []
        for index, (model_id, allow, ignore) in enumerate(resolved, start=1):
            current_phase[0] = index
            results.append(download_snapshot(model_id, allow_patterns=allow,
                                             ignore_patterns=ignore))
    except BaseException:
        _plan_stop.set()
        # JOINED on the SAME bound `heartbeat()` uses (`JOB_TIMEOUT_S + 1.0`),
        # not a shorter one: `_tick` is a plain `report`, which POSTs with a
        # `JOB_TIMEOUT_S` socket timeout, and a `ticker.join` shorter than
        # that can return while a beat is still inside its POST. That beat
        # then lands AFTER this function returns — reviving a row a moment
        # later flipped `state` back to "running" and cleared `finished_at`,
        # exactly the "tick in flight outlives the work" bug `heartbeat`
        # already had to fix once.
        ticker.join(timeout=JOB_TIMEOUT_S + 1.0)
        raise
    _plan_stop.set()
    ticker.join(timeout=JOB_TIMEOUT_S + 1.0)
    # Land on the grand total, `fetch_with_progress`'s own closing tick's
    # reasoning applied one level up: every phase folder's snapshot
    # symlinks are not counted, so a finished plan measures slightly under
    # its own total and a bar stuck short of 100% reads as a download that
    # gave up. Only reached on success — see the docstring's closing note.
    _tick(detail="Fetching weights…", done=total if total is not None else grand_done())
    return results


def download_file(repo_id, filename, detail=None, job=None, row=None):
    """One file out of a repo — a GGUF checkpoint, say — with progress.

    The total is THAT FILE's size, not the repo's. A repo that publishes a dozen
    quantizations of the same model sums to tens of gigabytes, and measuring a
    2.6GB pull against that is how a download reads as barely started for its
    whole life and then jumps to complete.

    `job`/`row` for a fetch that happens inside a REQUEST rather than inside a
    download — the diarization models on the first `diarize: true`, the speech
    detector on a machine whose Download predates it. Without them the tick goes
    to this process's `JOB_ID`, which is the model's own load row: finished,
    and reopened as a running download of something the user never asked for
    while the row they ARE watching says nothing. See `fetch_with_progress`.
    """
    # The same fast path `download_snapshot` takes, and it matters most for the
    # SMALL components: the 2MB speech detector and the two diarization models
    # are fetched inside a transcription, so on a warm cache their 456ms each was
    # latency a user waits through on the way to a transcript they already had
    # the bytes for. One file needs no snapshot completeness question, so this is
    # hf's read-only cache lookup rather than a download function told not to
    # download — see `_cached_file`.
    cached = _cached_file(repo_id, filename)
    if cached:
        return cached

    detail = detail or f"Fetching {filename}…"

    # Our own mirror, for a suggested model, BEFORE the Hub is consulted at all —
    # the point of the feature is a download that involves huggingface.co
    # nowhere, so the listing below would defeat it. This is the branch that
    # keeps llama.cpp's GGUFs on the mirror: since D416 it is the only local text
    # engine on Windows and Linux, and it fetches one file rather than a snapshot,
    # so `_mirror_snapshot` never sees a suggested text model on those platforms.
    mirrored = _mirror_file(repo_id, filename, detail=detail, job=job, row=row)
    if mirrored:
        return mirrored

    # One listing here too, for the revision as much as for the total: a GGUF
    # fetched at a revision its listing never described is the same bug as a
    # whole snapshot fetched that way, one file wide.
    sha, total = None, None
    try:
        sha, files = _repo_files(repo_id, include=filename)
        total = _total_bytes(files)
    except Exception as error:  # noqa: BLE001 - the bar can proceed on a guess; the fetch cannot
        _fallback(repo_id, error)

    # See `download_snapshot`'s identical call (SPEC AI-26) — after the
    # listing, before either fetch path below.
    _ensure_disk_space(total, repo_folder(repo_id))

    def hub():
        from huggingface_hub import hf_hub_download

        ticker = _HubByteTicker()
        return fetch_with_progress(
            repo_id,
            lambda: hf_hub_download(repo_id=repo_id, filename=filename,
                                    tqdm_class=ticker.bar()),
            total=total, detail=detail, job=job, row=row, counter=ticker)

    if not sha:
        return hub()
    try:
        return fetch_with_progress(
            repo_id,
            lambda: os.path.join(_segmented_fetch(repo_id, [filename], sha),
                                 filename),
            total=total, detail=detail, job=job, row=row)
    except Cancelled:
        raise
    except Exception as error:  # noqa: BLE001 - every failure degrades to hf's downloader
        _fallback(repo_id, error)
    return hub()


# -------------------------------------------------------------------- bring-up


def _bring_up(model_id, download, load):
    """download -> load -> ready, on its own thread, reporting every step.

    `load` is handed what `download` returned — a snapshot path, a dict of
    paths — rather than resolving the files a second time. The first cut had
    `load` call the downloader again for its path, which re-ran the Hub metadata
    call and re-reported a finished download on every load of a cached model.
    """
    try:
        set_state(state="downloading", detail="Fetching weights…")
        fetched = download(model_id)

        set_state(state="loading", detail="Loading weights into memory…")
        # No total: this is one long opaque step, and an invented percentage is
        # what makes live work read as frozen.
        report(kind="task", unit="", done=None, total=None,
               detail="Loading weights into memory…")
        load(model_id, fetched)

        set_state(state="ready", detail="", error="",
                  resident_bytes=resident_bytes(), loaded_at=time.time())
        # That figure is already stale — with lazy, memory-mapped weights most
        # of the model has not been touched yet. `/health` re-measures on every
        # poll, which is what the number on screen actually comes from.
        report(state="done", detail="Model loaded")
    except Cancelled:
        # **A ✕ is not a failure, and saying it is costs more than a wrong word.**
        # A terminal `state="error"` on the row CLEARS `cancel_requested`
        # (`jobs.upsert`: a finished job cannot be cancelled) — so the
        # supervisor's own poll, which is the thing that would have written the
        # right verdict half a second later, can no longer see the ✕ that caused
        # this at all. It then reads /health, finds "error", and reports the
        # download the user stopped as a load that crashed.
        #
        # The health state stays "error" because that is the only non-ready
        # terminal this contract has, and the supervisor's post-spawn loop is
        # watching for exactly it; `error="cancelled"` is the literal string
        # `_failure_text`/`_bring_up` switch on, so the supervisor's independent
        # verdict AGREES with the row instead of overwriting it.
        set_state(state="error", error="cancelled")
        report(state="cancelled")
    except BaseException as e:  # noqa: BLE001 - this thread's only job is to explain a failure
        # Deliberately broad and deliberately last: this thread is the only
        # thing that can say why a load failed, and an unhandled exception here
        # would leave /health saying "loading" forever.
        message = describe_failure(e)
        set_state(state="error", error=message)
        report(state="error", message=message)
        traceback.print_exc(file=sys.stderr)


# ----------------------------------------------------------------- HTTP server


def _handler(generate, streaming):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # the supervisor captures stderr; per-request noise is not useful

        def _drain(self):
            """Read and discard the request body. True if it was fully drained.

            Draining at all is mandatory before answering a request WITHOUT
            reading its body. This handler is HTTP/1.1, so the connection is
            kept alive and the next request is parsed off the same socket — an
            undrained body is still queued there and its bytes get read as that
            request's request-line. And closing a socket that still holds
            unread data makes Windows send an RST instead of a FIN, which
            reaches the client as [WinError 10053] ConnectionAbortedError
            rather than the clean 403 this path exists to deliver. (Both halves
            are real: the desync bites on every platform, the RST is
            Windows-specific.)

            BOUNDED on both axes, because the only caller runs BEFORE
            authentication and Content-Length is the caller's to claim.
            Unbounded, a client with a wrong token could announce a huge body
            and then send nothing, holding one of this ThreadingTCPServer's
            threads for as long as it pleased — and `_Server` sets no socket
            timeout, so "as long as it pleased" is forever. Enough of those and
            the worker stops answering /health, which is how the supervisor
            decides it is alive. So: a length over DRAIN_MAX_BYTES is not read
            at all, and what is read gets DRAIN_TIMEOUT_S to arrive.

            Returning False means the connection is NOT safe to keep alive —
            an over-long, half-sent, chunked or unparseable body is still in
            the socket, and the next request read off it would consume that as
            its request-line. The caller closes instead.
            """
            # Content-Length is the only framing this can drain. A chunked body
            # is length-prefixed per chunk, and BaseHTTPRequestHandler does not
            # decode it — self.rfile is the raw socket, so following it means
            # parsing the chunk framing here. Transfer-Encoding also OVERRIDES
            # Content-Length when both are sent (RFC 9112 s6.1), so a
            # Content-Length read alongside one would stop in the wrong place.
            # Nothing legitimate POSTs chunked to this worker (small JSON, sent
            # with a length), so treat any Transfer-Encoding as undrainable and
            # end the connection rather than guess where the body stops.
            # Presence, not truth: an empty `Transfer-Encoding:` is malformed
            # framing either way, and `.get()` would read it as absent.
            if "Transfer-Encoding" in self.headers:
                return False
            raw = self.headers.get("Content-Length")
            try:
                remaining = int(raw) if raw else 0
            except ValueError:
                return False          # unparseable: cannot know where it ends
            if remaining < 0 or remaining > DRAIN_MAX_BYTES:
                return False
            if remaining == 0:
                return True
            previous = self.connection.gettimeout()
            self.connection.settimeout(DRAIN_TIMEOUT_S)
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 16 * 1024))
                    if not chunk:
                        return False  # client stopped short of what it claimed
                    remaining -= len(chunk)
                return True
            except OSError:           # includes the socket timeout
                return False
            finally:
                self.connection.settimeout(previous)

        def _authorized(self):
            # The token is a header the supervisor generated and passed in this
            # process's environment. A foreign page that guessed the ephemeral
            # port still cannot drive the model, and the value never lands in a
            # log line or a Referer.
            if TOKEN and self.headers.get("X-Fused-Worker") == TOKEN:
                return True
            # Drain BEFORE the refusal, not after: see _drain. A rejected POST
            # still arrived with a body, and leaving it unread turns a 403 into
            # a dropped connection. When it cannot be drained safely, the
            # refusal still goes out — but this connection ends with it, rather
            # than being reused with someone else's bytes queued on it.
            drained = self._drain()
            self.send_response(403)
            self.send_header("Content-Length", "0")
            if not drained:
                # Announced, not just done: the client is owed the reason its
                # connection is about to end. send_header's own side effect
                # sets close_connection, which is what actually stops this
                # socket being reused with those undrained bytes still on it.
                self.send_header("Connection", "close")
            self.end_headers()
            return False

        def _json(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorized():
                return
            if self.path.startswith("/health"):
                # Measured HERE, not read back from the state set at load time.
                # It used to be stored once, right after `load()` returned, and
                # served unchanged forever — so the supervisor's `refresh_memory`
                # re-read the same frozen number every poll, and a model whose
                # weights fault in during its first generation was reported at
                # whatever it happened to cost before it had done anything.
                health = snapshot()
                if health.get("state") == "ready":
                    # `resident_bytes()` FIRST: it is what feeds `_rss_peak`
                    # (SPEC AI-8c), and `peak_resident_bytes()`'s RSS fallback
                    # must see THIS poll's reading before it answers.
                    health["resident_bytes"] = resident_bytes()
                    health["peak_resident_bytes"] = peak_resident_bytes()
                    # The OS's own "right now" figure (D597), additive beside
                    # the two above — see `os_footprint_bytes`.
                    health["os_footprint_bytes"] = os_footprint_bytes()
                self._json(health)
                return
            self._json({"error": "not found"}, status=404)

        def do_POST(self):
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                self._json({"error": "body must be JSON"}, status=400)
                return

            if self.path.startswith("/quit"):
                self._json({"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.1), os._exit(0)),
                                 daemon=True).start()
                return

            if self.path.startswith("/cancel"):
                CANCEL.set()
                self._json({"ok": True})
                return

            if self.path.startswith("/generate"):
                if snapshot()["state"] != "ready":
                    self._json({"error": "the model is not loaded"}, status=409)
                    return
                # NOT cleared here. `CANCEL` belongs to the generation that is
                # RUNNING, and this handler may be a second request waiting for
                # `GENERATE_LOCK`: clearing before the lock erases the ✕ the
                # user just pressed for the first one, which then runs to
                # completion under a Stop that appeared to work. Each generation
                # clears the flag once it owns the lock — see `_generation`.
                if streaming:
                    self._stream(body)
                else:
                    self._single(body)
                return

            self._json({"error": "not found"}, status=404)

        def _single(self, body):
            """One JSON reply, for work that produces an ARTEFACT rather than a
            stream. An image is not a sequence of tokens, and pretending it is
            would buy nothing — its progress is steps, and those go to the job
            row where the download manager can already draw them."""
            with GENERATE_LOCK, heartbeat():
                CANCEL.clear()
                try:
                    # `run_on_generate_thread`, never a bare `generate(body)`:
                    # this method runs on a `ThreadingTCPServer` connection
                    # thread, which is exactly the thread MLX's compiled-graph
                    # cache must never touch — see that function's docstring.
                    result = run_on_generate_thread(generate, body)
                    self._json({"ok": True, "result": result})
                except Cancelled:
                    self._json({"ok": True, "cancelled": True})
                except BaseException as e:  # noqa: BLE001 - must reach the client
                    traceback.print_exc(file=sys.stderr)
                    self._json({"ok": False, "error": describe_failure(e)})
                finally:
                    # Arms the idle-release timer on every outcome, not just
                    # success: a cancelled or failed generation can still have
                    # allocated its peak before it stopped, and this only
                    # STARTS the clock — it does not clear anything itself.
                    # See `_arm_release_timer`.
                    _arm_release_timer()

        def _stream(self, body):
            """NDJSON, chunked. `{"type":"chunk"}` lines closed by
            `{"type":"done"}` — the shape `fused.ai`'s reader already speaks."""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def write(payload):
                line = (json.dumps(payload) + "\n").encode()
                self.wfile.write(f"{len(line):X}\r\n".encode() + line + b"\r\n")
                self.wfile.flush()

            with GENERATE_LOCK, heartbeat():
                CANCEL.clear()
                try:
                    # `run_on_generate_thread`, never a bare `generate(body,
                    # write)`: this method runs on a `ThreadingTCPServer`
                    # connection thread — see that function's docstring for
                    # why MLX's generation must never run there. `write`
                    # itself still runs on the generate thread it is called
                    # from (it is passed in, not called back across threads),
                    # so every chunk is written to `self.wfile` from ONE
                    # thread at a time, exactly as before.
                    run_on_generate_thread(generate, body, write)
                except BaseException as e:  # noqa: BLE001 - must reach the client
                    write({"type": "done", "ok": False,
                           "error": describe_failure(e)})
                    traceback.print_exc(file=sys.stderr)
                finally:
                    # Same reasoning as `_single`'s finally — and note WHERE
                    # this sits: after `run_on_generate_thread` has RETURNED,
                    # i.e. after the whole token stream or transcription has
                    # finished, not after its first chunk. The idle clock only
                    # starts once the worker is actually idle again.
                    _arm_release_timer()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

    return Handler


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = socket.AF_INET


def build_server(generate, streaming=False, host="127.0.0.1"):
    """The HTTP half on an ephemeral port, unstarted. Split out of `serve` so a
    test can drive the real routes without spawning a process."""
    return _Server((host, 0), _handler(generate, streaming))


#: Where the supervisor wants this worker to run from, when it could not pass
#: `cwd=` to Popen. Mirrors `supervisor.WORKER_CWD_ENV` — spelled here too because
#: this module is imported from the runner's own interpreter, where
#: `fused_render` is not importable.
WORKER_CWD_ENV = "FUSED_AI_WORKER_CWD"


def _adopt_spawn_shape():
    """Do for ourselves the two things a `fork()`-based spawn used to do.

    On macOS the supervisor starts workers with `posix_spawn` (see
    `supervisor._spawn_kwargs` for the crash that forced it), and CPython only
    takes that path for a Popen with no `cwd` and no `start_new_session`. So
    the working directory arrives in an environment variable, and the worker
    makes itself a session leader — which is what `_kill_tree`'s `killpg`
    keys on, so an unload still takes the whole tree down.

    Both are best-effort. A missing directory is the supervisor's fact to
    report (the runner folder it named does not exist), not this worker's to
    die on before it can say anything; and `setsid` fails with EPERM when the
    process already leads a session, which is exactly the case on the
    platforms that still spawn with `start_new_session=True`.
    """
    cwd = os.environ.get(WORKER_CWD_ENV)
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass
    setsid = getattr(os, "setsid", None)
    if setsid is not None:
        try:
            setsid()
        except OSError:
            pass


def serve(download, load, generate, streaming=False, memory=None, peak_memory=None,
         release=None, footprint=None, argv=None):
    """Parse the supervisor's argv and run this worker. Does not return.

    `--download-only` fills the cache and exits; the exit CODE is the answer
    there, because the supervisor waits on the process rather than on a health
    route, so a failure must not be swallowed into a status nobody reads.

    `peak_memory` is `memory`'s sibling (SPEC AI-8c, D497) — a runner that can
    answer "what did this cost at its WORST", not just "right now". See
    `peak_resident_bytes` for what a runner without one gets instead.

    `release` is a third, independent optional hook: not a measurement but an
    action, armed to run `_RELEASE_IDLE_S` after the worker's last execution
    (win, loss, or cancel alike) if nothing new has started by then — see
    `_release`, `_arm_release_timer` and `_fire_release` for the mechanism and
    why only some runners pass one.

    `footprint` is a fourth, independent optional hook: a runner's own reading
    of memory that lives OUTSIDE its own address space — a discrete GPU's
    VRAM, which neither RSS nor macOS's `phys_footprint` can see. `os_
    footprint_bytes()` adds it to the platform figure it already computes; see
    that function's and `_footprint`'s own docstrings for why addition, not
    `max`, and why `torch_image.main()` is the only caller that supplies one.
    """
    global JOB_ID, _measure, _measure_peak, _release, _footprint

    _adopt_spawn_shape()
    _measure = memory
    _measure_peak = peak_memory
    _release = release
    _footprint = footprint
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    # Not required: a download-only run serves nothing, so it has no port to
    # publish and no status file to publish it in.
    parser.add_argument("--status", default="")
    parser.add_argument("--job", default="")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args(argv)
    JOB_ID = args.job
    set_state(model=args.model)

    if args.download_only:
        try:
            download(args.model)
        except Cancelled:
            # Still non-zero — the weights are not on the disk and a zero would
            # report the download DONE — but not a traceback: `_fetch_only`
            # tails this log for the message it puts on a failed row, and a
            # stack trace for something the user deliberately pressed is the
            # noise that made a cancel look like a crash. The supervisor tells
            # the two apart by the row's own ✕, not by what is written here.
            sys.stderr.write("cancelled\n")
            sys.exit(1)
        except BaseException as e:  # noqa: BLE001 - stderr is the supervisor's report
            traceback.print_exc(file=sys.stderr)
            sys.stderr.write(f"\n{e.__class__.__name__}: {e}\n")
            sys.exit(1)
        sys.exit(0)

    if not args.status:
        sys.stderr.write("--status is required unless --download-only\n")
        sys.exit(2)

    # Bind :0 and publish what we got. Anything the parent reserved could be
    # taken between its bind and our exec, so the child is the one that picks.
    server = build_server(generate, streaming)
    port = server.server_address[1]
    tmp = args.status + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({"port": port, "pid": os.getpid(), "model": args.model}, handle)
    os.replace(tmp, args.status)

    threading.Thread(target=_bring_up, args=(args.model, download, load),
                     name="load", daemon=True).start()
    server.serve_forever()
