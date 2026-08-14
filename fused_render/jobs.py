"""The background-job registry — the model behind the shell's download manager.

A page can start work that outlives the call that started it: a model download,
a checkpoint pull, a venv build, a generation that runs for minutes. Today each
one of those invents its own progress UI inside its own page, so the work is
only visible while you are looking straight at it — navigate away and an 8GB
download is happening with nothing on screen to say so. This module is the
shared place to say it: one record per long-running operation, held by the
server, shown by ONE surface in the shell (the download manager at the foot of
the notification stack).

**Why the server holds it, not the page.** The reporter and the viewer are
different documents. A rendered page lives in a same-origin iframe that the
shell tears down on every navigation, and it may not even be the same browser
tab as the shell chrome. The record has to outlive the reporter's document, so
it lives in the one process both sides can reach — and the same registry then
answers a detached Python worker POSTing to `/api/jobs` directly, with no
second mechanism.

In memory, deliberately: the jobs describe work happening in THIS app session,
and a restart of the server is the end of that session. Nothing here is
history — the call log (`calls.py`) is the durable record; this is the live one.

**Reporting is best-effort and never authoritative.** A reporter can die
mid-download (its page was closed) and the work carries on with nobody to
describe it. A record whose `state` is still "running" but which has not been
updated in `STALE_AFTER_S` is reported as `stalled`, which the UI shows as
exactly what it is — "no longer reporting" — rather than as a frozen bar that
looks like a hang. It is dropped entirely after `STALE_DROP_S` so a dead
reporter cannot wedge the list for the rest of the session.

No import of anything under `fused_render.server` — the router imports this
module; keep it acyclic.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import asdict, dataclass

# The state machine. "running" is the only non-terminal state; the three
# terminal ones differ in what the user has to do about them, which is what
# decides how long each is kept (see `_sweep`).
RUNNING = "running"
TERMINAL_STATES = ("done", "error", "cancelled")
STATES = (RUNNING,) + TERMINAL_STATES

# What the record means for a progress bar: a download reads its numbers as
# bytes and has a bar; a task may have no numbers at all and reads as a
# spinner. Kept a small closed set so the UI never has to guess.
KINDS = ("download", "task")

# How long a finished job stays on screen. Long enough to be noticed by someone
# who was not staring at the corner when it landed, short enough that the
# manager is a picture of what is happening NOW and empties itself.
#
# An `error` is exempt: it is the one outcome the user may need to act on, so
# it stays until dismissed — the same rule the persistent-error toast follows
# (lib/toast's ttlMs=0). MAX_JOBS is what bounds it.
FINISHED_TTL_S = 30.0

# A running job with no update in this long is reported as `stalled`. Above
# every reporter's poll cadence by a wide margin (the examples report at
# 1.5-2s), so an ordinary slow poll or a busy machine never trips it.
STALE_AFTER_S = 30.0

# ...and dropped entirely after this long. A reporter that died is not coming
# back; the row would otherwise sit there for the life of the app.
STALE_DROP_S = 600.0

# Cap on records the sweep will evict down to. Written for a pathological
# reporter (a loop minting a fresh id per update), and the eviction order below
# is chosen so that when it IS reached, what survives is what a person would
# want to see.
#
# **Not a hard cap any more, and deliberately so.** A queue of transcriptions
# (SPEC AI-10a) makes "more than 64 rows of live work" the designed usage, and
# for that work the row is the only channel the ✕ and the page have — so live
# SERVER rows are exempt and the list can exceed this. What that costs is a
# dict entry per piece of work the user actually started; what the exemption
# buys is that asking for sixty transcriptions does not silently fail some of
# them. Page-owned rows are still capped, which is where the unbounded risk
# lives (a page can mint rows it never finishes).
MAX_JOBS = 64

# Ids are chosen by the reporter — the runtime mints one per `fused.trackJob()`, and
# a Python worker may use a stable one of its own so a re-launched worker
# re-attaches to its row rather than opening a second. Constrained to a plain
# token so it stays safe as a dict key, a URL path segment, and a React key.
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Ids under this prefix belong to the SERVER (SPEC §40): a model download, a
# generation — work this process runs and can therefore really stop. A page may
# READ and CANCEL them like any other row, but it may not WRITE one: the ids are
# deterministic (`sys:ai-model:<repo>`), so without this a page could post
# `state: "done"` for a download that is still running and the manager would
# believe it.
SERVER_ID_PREFIX = "sys:"

# Who is running the work, which decides what the manager's ✕ can do:
#   "page"   — only the page knows what stopping means, so cancel is a REQUEST
#              it reads back off its next tick and honours (or does not).
#   "server" — this process owns the subprocess, so cancel is an ACTION.
OWNER_PAGE = "page"
OWNER_SERVER = "server"

# Field caps. Titles and details are single-line labels in a 360px column;
# a message carries a traceback, so it is allowed to be long and multi-line.
TITLE_MAX = 120
DETAIL_MAX = 200
MESSAGE_MAX = 4000
PAGE_MAX = 1024

# Dismissed ids, bounded. A reporter that keeps posting after its job finished
# would otherwise resurrect the row the user just closed, and "it came back"
# reads as a bug in the app rather than as a late report.
#
# What is refused is precise, and the precision is the point: a LATE TICK, never
# a fresh start. A tick from a poll loop is a delta (`done`, `detail`) or a
# terminal state; the opening report a `fused.trackJob()` handle sends is the only
# thing that carries `state: "running"` explicitly. So an opening report CLEARS
# the dismissal and re-opens the row, and everything else stays refused.
#
# That distinction is what lets a page reuse a STABLE id (the documented pattern
# — a reload re-attaches to its own row) without the id dying the first time
# anyone dismissed it: the next real run announces itself and gets a row, while
# the previous run's trailing ticks stay silenced. A plain time window was tried
# first and is strictly worse at both ends — too short and a slow poll loop
# resurrects the row anyway, too long and a legitimate re-run is invisible.
_DISMISSED_MAX = 256


class JobError(ValueError):
    """A malformed report. Carries the message the endpoint returns verbatim."""


@dataclass
class Job:
    """One long-running operation, as last reported.

    `done`/`total` are floats-or-None rather than ints because a reporter
    aggregating several files (an HF snapshot pull) has no reason to round, and
    None is meaningfully different from 0: no total at all is an indeterminate
    bar, a total of 0 is a download of nothing.
    """

    id: str
    title: str
    detail: str = ""
    kind: str = "task"
    state: str = RUNNING
    done: float | None = None
    total: float | None = None
    unit: str = ""
    message: str = ""
    # The .html that raised it, from the X-Fused-Page header. Attribution only
    # — the manager shows which page a row belongs to, and clicking it goes
    # back there.
    page: str = ""
    # OWNER_PAGE or OWNER_SERVER — see SERVER_ID_PREFIX. Not settable from a
    # report body: it follows from the id, so a page cannot claim to be the
    # server by saying so.
    owner: str = OWNER_PAGE
    cancellable: bool = False
    cancel_requested: bool = False
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
# id -> when it was dismissed. Insertion-ordered, so the oldest entry is the
# first one to drop when the cap bites.
_dismissed: dict[str, float] = {}


# ------------------------------------------------------------------ validation


def _text(value: object, cap: int, *, one_line: bool = True) -> str:
    """A user-visible string, bounded and (for labels) collapsed to one line.

    Newlines are collapsed rather than escaped because these land in
    `textContent`: a label with a raw newline does not break anything, it just
    silently becomes a two-line row in a stack whose rows are one line tall.
    """
    if value is None:
        return ""
    text = str(value)
    if one_line:
        text = " ".join(text.split())
    return text[:cap]


def _number(value: object, name: str) -> float | None:
    """A non-negative finite number, or None.

    NaN and infinity are rejected rather than clamped: both come out of a
    division a reporter did not guard (`n / total` with total 0), and silently
    turning that into a number would paint a confident bar from a bug. The
    reporter hears about it instead.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JobError(f"'{name}' must be a number or null, got {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise JobError(f"'{name}' must be a finite number, got {value!r}")
    return max(0.0, number)


def _one_of(value: object, allowed: tuple[str, ...], name: str, default: str) -> str:
    if value is None:
        return default
    text = str(value)
    if text not in allowed:
        raise JobError(f"'{name}' must be one of {', '.join(allowed)}, got {text!r}")
    return text


def clean_id(value: object) -> str:
    text = "" if value is None else str(value)
    if not _ID_RE.match(text):
        raise JobError(
            "'id' must be 1-128 characters of letters, digits, '.', '_', ':' or '-'"
        )
    return text


# -------------------------------------------------------------------- mutation


def upsert(body: dict, *, page: str = "", now: float | None = None,
           server: bool = False) -> dict:
    """Create or update one record from a reporter's POST body.

    Upsert rather than create+update: a reporter's every progress tick is the
    same call with the same id, so there is one code path whether this is the
    first report or the four hundredth, and a reporter that lost its
    server-side record (an app restart mid-download) transparently re-creates
    it instead of failing every tick from then on.

    Only the keys PRESENT in the body are applied — a tick that carries just
    `done` must not blank the title the first tick set. That is also why this
    reads the body directly instead of taking a fully-populated Job.

    `server=True` is for THIS process reporting its own work (a model download,
    a generation), and it is the only way to write a `sys:` id. The HTTP
    endpoint never passes it, so a page cannot post progress for a job the
    server owns — those ids are deterministic, and a forged "done" on a download
    still running is exactly the lie the manager would have no way to catch.
    """
    if not isinstance(body, dict):
        raise JobError("request body must be a JSON object")
    now = time.time() if now is None else now
    job_id = clean_id(body.get("id"))
    if job_id.startswith(SERVER_ID_PREFIX) and not server:
        raise JobError(
            f"'id' may not start with {SERVER_ID_PREFIX!r} — that prefix is "
            "reserved for work the app itself is running"
        )

    with _lock:
        if job_id in _dismissed:
            if body.get("state") == RUNNING:
                # A fresh start reusing the id, not the dismissed job arguing:
                # only an opening report states `running` outright. Forget the
                # dismissal and let the record below be created.
                del _dismissed[job_id]
            else:
                # A late tick from the run the user already closed. Answered as
                # if it had been stored, so a reporter mid-loop does not start
                # erroring — it simply has no row any more.
                return _public(
                    Job(id=job_id, title="", state=RUNNING, started_at=now, updated_at=now),
                    now,
                )

        job = _jobs.get(job_id)
        if job is None:
            title = _text(body.get("title"), TITLE_MAX)
            if not title:
                raise JobError("the first report for a job must include a 'title'")
            # The owner follows from the id, and is fixed at creation: it says
            # what the manager's ✕ is able to do, which is a fact about who runs
            # the work, not a claim a later tick gets to revise.
            job = Job(
                id=job_id,
                title=title,
                started_at=now,
                updated_at=now,
                owner=OWNER_SERVER if job_id.startswith(SERVER_ID_PREFIX) else OWNER_PAGE,
            )
            _jobs[job_id] = job
        elif "title" in body:
            title = _text(body.get("title"), TITLE_MAX)
            if title:
                job.title = title

        if "detail" in body:
            job.detail = _text(body.get("detail"), DETAIL_MAX)
        if "message" in body:
            job.message = _text(body.get("message"), MESSAGE_MAX, one_line=False)
        if "kind" in body:
            job.kind = _one_of(body.get("kind"), KINDS, "kind", job.kind)
        if "unit" in body:
            job.unit = _text(body.get("unit"), 16)
        if "done" in body:
            job.done = _number(body.get("done"), "done")
        if "total" in body:
            job.total = _number(body.get("total"), "total")
        if "cancellable" in body:
            job.cancellable = bool(body.get("cancellable"))
        if page:
            job.page = _text(page, PAGE_MAX)

        if "state" in body:
            state = _one_of(body.get("state"), STATES, "state", job.state)
            if state != job.state:
                job.state = state
                job.finished_at = now if state in TERMINAL_STATES else None
                if state in TERMINAL_STATES:
                    # A finished job cannot be cancelled, so the request is
                    # spent whether or not it was honored. Leaving it set would
                    # keep the row's Cancel button lit on a row that is done.
                    job.cancel_requested = False

        job.updated_at = now
        _sweep(now)
        return _public(job, now)


def request_cancel(job_id: str, *, now: float | None = None) -> dict | None:
    """Ask the reporter to stop. Returns the record, or None if there isn't one.

    A REQUEST, not a kill: the server does not know what the work is or which
    process is doing it. The reporter learns about it in the response to its
    next progress tick and stops the way it knows how (the examples call their
    own `action: "cancel"`), then reports state "cancelled". The row therefore
    stays "running, cancelling…" until the work actually stops, which is the
    truth — a row that flips to "cancelled" while a 4-minute download carries
    on underneath it would be a lie the UI told to feel responsive.
    """
    now = time.time() if now is None else now
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        if job.state == RUNNING:
            job.cancel_requested = True
        return _public(job, now)


def _forget(job_id: str, now: float) -> None:
    """Drop a record and remember the dismissal. Caller holds the lock."""
    del _jobs[job_id]
    _dismissed.pop(job_id, None)  # re-insert at the end, so the cap drops oldest
    _dismissed[job_id] = now
    while len(_dismissed) > _DISMISSED_MAX:
        _dismissed.pop(next(iter(_dismissed)))


def dismiss(job_id: str, *, now: float | None = None) -> bool:
    """Drop a finished — or stalled — record. Returns whether there was one.

    A job someone is actively reporting on is refused: the only honest way to
    make that row go away is to stop the work (`request_cancel`), and a dismiss
    that hid a live download would put the app back in the state this whole
    feature exists to fix — multi-GB of traffic with nothing on screen saying so.

    A STALLED job is dismissible, though, and that is not a softening of the
    rule but the same rule applied: nobody is reporting on it, so the row is not
    hiding anything the app could otherwise tell you — it is the app admitting
    it has stopped knowing. The user closing that row usually knows exactly what
    it was (they closed the page). And it is not a one-way door: a reporter that
    comes back opens the row again with its next `fused.trackJob()` handle.
    """
    now = time.time() if now is None else now
    with _lock:
        job = _jobs.get(job_id)
        if job is None or (job.state == RUNNING and not is_stalled(job, now)):
            return False
        _forget(job_id, now)
        return True


def clear_finished(*, now: float | None = None) -> int:
    """Dismiss every finished (or stalled) record at once. Returns how many."""
    now = time.time() if now is None else now
    with _lock:
        gone = [j.id for j in _jobs.values()
                if j.state != RUNNING or is_stalled(j, now)]
        for job_id in gone:
            _forget(job_id, now)
        return len(gone)


def reset() -> None:
    """Empty the registry — for tests, and for nothing else."""
    with _lock:
        _jobs.clear()
        _dismissed.clear()


# --------------------------------------------------------------------- reading


def list_jobs(*, now: float | None = None) -> list[dict]:
    """Every live record, oldest first.

    Ascending by start time so rows never reorder under the pointer: a new job
    appends at the BOTTOM of the column, nearest the screen edge the eye is
    already on — the same ordering rule the toast stack above it follows.
    """
    now = time.time() if now is None else now
    with _lock:
        _sweep(now)
        jobs = sorted(_jobs.values(), key=lambda j: (j.started_at, j.id))
        return [_public(job, now) for job in jobs]


def is_stalled(job: Job, now: float) -> bool:
    return job.state == RUNNING and (now - job.updated_at) > STALE_AFTER_S


def _public(job: Job, now: float) -> dict:
    """The wire shape: the record plus what only the reader can know.

    `stalled` is COMPUTED here rather than stored, because it is a statement
    about the present ("nobody has reported in 30s"), not an event that
    happened — storing it would need a timer to un-set it the moment a late
    tick arrives.
    """
    record = asdict(job)
    record["stalled"] = is_stalled(job, now)
    return record


def _sweep(now: float) -> None:
    """Drop what has aged out, then enforce the cap. Caller holds the lock.

    Two different statements with two different rules. Ageing out says *this
    row is over*; the cap says *there are too many rows to show*. So the cap
    never drops live SERVER work — for that, the row is the only channel the
    work has, not a view of it. See the comment on the cap branch below.

    Ageing out goes through `_forget`, exactly like a user dismissing the row,
    because it is the same statement — *this row is over* — and it needs the
    same protection from the same late tick. A reporter that posts its FULL
    status every tick (the documented direct-HTTP path: a detached worker with
    no `fused.trackJob()` handle to remember it already finished) would otherwise
    re-create the record from scratch the moment it aged out, and keep doing so
    every FINISHED_TTL_S for as long as it kept posting — a finished download
    blinking back onto the screen every 30 seconds. Only a fresh opening report
    reopens a forgotten id, which is the one case that should.
    """
    for job_id, job in list(_jobs.items()):
        if job.state == RUNNING:
            if (now - job.updated_at) > STALE_DROP_S:
                _forget(job_id, now)
        elif job.state == "error":
            continue  # kept until dismissed — see FINISHED_TTL_S
        elif (now - (job.finished_at or job.updated_at)) > FINISHED_TTL_S:
            _forget(job_id, now)

    if len(_jobs) <= MAX_JOBS:
        return
    # Over the cap: finished rows go before running ones (the work they
    # describe is over), and within each group the least recently updated
    # first. A live download is the last thing evicted.
    #
    # NOT `_forget`, unlike the age-outs above: this is capacity pressure, not a
    # statement that the work is over. The row evicted here may well be a live
    # download whose reporter is mid-loop, and forgetting it would silence that
    # reporter for good — the row could never come back, because its ticks are
    # deltas and only an opening report reopens a forgotten id.
    #
    # **Live SERVER work is not a candidate at all.** The rows above are a
    # display; a row describing work the app itself is running is a CHANNEL —
    # it is simultaneously the queue's state, the ✕'s only route to the
    # process, the progress readout, and the completion signal `fused.watchJob`
    # polls. Dropping one does not show less, it takes the ✕ away and tells the
    # page the work stopped: `watch` resolves null after five consecutive
    # misses and a settled promise cannot be un-settled by a row that comes
    # back. This cap was sized for a handful of downloads, and then a queue of
    # sixty recordings (SPEC AI-10a) made "more than 64 live rows" the designed
    # usage rather than the pathological case — at which point the eviction was
    # rejecting transcriptions that went on to succeed.
    #
    # The asymmetry with page-owned rows is what makes this safe rather than
    # unbounded: a `sys:` row can only be minted by this process's own code and
    # is bounded by work actually in flight, while `fused.trackJob()` lets any
    # page open rows it never finishes. So those stay evictable, and the cap
    # goes on doing its job for the case it was written for.
    #
    # FINISHED server rows are evictable like anything else — the exemption is
    # for live work, not for a `sys:` prefix — and the age sweep above still
    # drops a running row whose reporter has gone silent for `STALE_DROP_S`,
    # so a crashed worker cannot pin a row for the session.
    evictable = [
        job for job in _jobs.values()
        if not (job.state == RUNNING and job.owner == OWNER_SERVER)
    ]
    order = sorted(evictable, key=lambda j: (j.state == RUNNING, j.updated_at))
    for job in order[: len(_jobs) - MAX_JOBS]:
        del _jobs[job.id]
