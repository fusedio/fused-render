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

# The state machine. "running" and "waiting" are the two NON-terminal
# states; the three terminal ones differ in what the user has to do about
# them, which is what decides how long each is kept (see `_sweep`).
#
# `WAITING`: work has stopped and is not coming back on its own — it is
# sitting on a QUESTION only the user can answer (today, the sole producer is
# `envinstall._mirror_into_jobs`'s `needs_build` branch: uv's "Install
# anyway" compile prompt). Not `RUNNING` — nothing is actually in flight, so
# a bar or a spinner would be a lie. Not `TERMINAL` either, and deliberately
# so: none of the three terminal states means "stopped, waiting on you" —
# `done` and `cancelled` both read as the row having reached an end the user
# does not need to act on, and `error` reads as "broken", which is worse than
# wrong here, since the compile prompt this row is sitting in front of is
# still an open, answerable question, not a failure. Kept OUT of
# `TERMINAL_STATES` on purpose: `upsert`'s finished-transition bookkeeping
# (`finished_at`, clearing `cancel_requested`) does not apply to a row that
# has not actually finished, and `_sweep`'s age-out clock must not start
# either — see `_sweep`'s own comment for how it is kept instead.
RUNNING = "running"
WAITING = "waiting"
TERMINAL_STATES = ("done", "error", "cancelled")
STATES = (RUNNING, WAITING) + TERMINAL_STATES

# What the record means for a progress bar: a download reads its numbers as
# bytes and has a bar; a task may have no numbers at all and reads as a
# spinner. Kept a small closed set so the UI never has to guess.
KINDS = ("download", "task")

# Whether `total` is the WHOLE download or one phase of it (SPEC AI-5n, D498).
# "phase" is the default a bare `download_snapshot` reporter has always sent
# without knowing it — a single repo's own total, which for a single-repo
# runner already is the whole download and needed no migration. "download" is
# an explicit claim only `worker_base.download_plan` (or an equivalent
# reporter) is entitled to make.
TOTAL_SCOPES = ("download", "phase")

# How long a finished job stays on screen ONCE SOMEONE HAS READ IT — the
# retention clock starts at first READ (`Job.first_read_at`), not at
# completion. The corner is meant to answer "is my work done", not to be read
# as a log — once a `done`/`cancelled` row has had a moment to register, it
# should clear itself so the manager stays a picture of what is happening NOW,
# not an accumulating list of what already happened. The client
# (`pollInterval` in jobs.ts) holds a matching grace window on ACTIVE-cadence
# polling after the last running job disappears, so the row is swept close to
# on-schedule instead of lagging behind a slow poll.
#
# **Why read-gated rather than a flat clock from `finished_at`.** A job that
# starts AND finishes entirely server-side — a scheduled run, or a Python
# reporter POSTing straight to `/api/jobs` with no `fused.trackJob()` handle —
# runs no JS, so it never calls `pingJobs()`, the only thing that nudges the
# shell's poll out of its idle cadence (jobs.ts `POLL_IDLE_MS`, 5s). A flat
# `finished_at + FINISHED_TTL_S` clock could then expire the row entirely
# between two idle polls: anything under roughly `POLL_IDLE_MS` of work could
# be born and swept with NOBODY ever having seen it, which is precisely the
# guarantee this constant exists to give ("noticed by someone who was not
# watching the corner", SPEC BG-6). Gating the clock on the first successful
# `list_jobs()` read — the same call the dock's poll and `fused.watchJob` both
# make — guarantees every reader gets a real `FINISHED_TTL_S` window from the
# moment THEY could first have seen the row, at the cost of an unread row
# living arbitrarily long (bounded by `FINISHED_UNREAD_DROP_S` below, not by
# this constant).
#
# **Known limitation, accepted rather than solved:** the clock starts on the
# FIRST read by ANY client, not per-client — so a second, slower reader (a
# background tab, a client that only polls occasionally) can still open the
# dock after the fast reader's `FINISHED_TTL_S` has elapsed and find the row
# already gone. Per-client retention would need a per-client read log, which
# is a much larger feature for a case this rare; "seen by the first reader
# to look" is the guarantee actually being made.
#
# Two rejected alternatives (see DECISIONS.md D469): a shorter fixed TTL from
# `finished_at` (does not fix the "never read at all" case, only shrinks its
# window) and dropping `POLL_IDLE_MS` to ~2s (a permanent background request
# every 2s for the life of every session, to cover a rare case a read gate
# covers for free).
#
# An `error` is exempt: it is the one outcome the user may need to act on, so
# it stays until dismissed — the same rule the persistent-error toast follows
# (lib/toast's ttlMs=0). MAX_JOBS is what bounds it.
FINISHED_TTL_S = 3.0

# The backstop for a terminal row that is NEVER read at all — a headless
# server, a `fused-render` run with no browser ever attached, a closed tab
# whose dock never polls again. Without this, such a row would sit in
# `_jobs` for the life of the process: `FINISHED_TTL_S` only starts counting
# once something reads the row, so an unread row is otherwise unbounded, and
# `MAX_JOBS` is a DIFFERENT rule (capacity pressure, not "this work is over"
# — see `_sweep`'s docstring) that only bites once 64 rows have piled up.
# Same magnitude as `STALE_DROP_S` below and for the same reason: ten minutes
# is long enough that any UI actually watching the corner would have read the
# row by now, and a process that has run headless for that long is not about
# to grow a viewer.
FINISHED_UNREAD_DROP_S = 600.0

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

# `schedule.py`'s own row id prefix (mirrors `SCHEDULE_JOB_PREFIX` in
# frontend/src/platform/lib/jobs.ts — keep the two spellings in step). A row
# under this prefix is deliberately excluded from every frontend surface
# (`isScheduleJob`/`jobRows` drops it from Activity, and `terminalJobs` is
# applied AFTER `jobRows` in `ActivityDock.tsx`, so it never reaches
# Notifications either) — a scheduled run already gets its own toast
# (`schedule-toast.ts`) and its own row on the Scheduled page. `_sweep`'s
# keep-until-dismissed exemption (below) exists so a row a human still needs
# to SEE is not swept out from under them; a row no surface shows or lets
# them dismiss has no claim on that exemption; see `_sweep` for what it gets
# instead.
SCHEDULE_JOB_PREFIX = "sys:schedule:"

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
# The model name is a dimmed SUFFIX on the title row, never the detail line —
# detail is the one thing a running worker's progress ticks own, and a model
# name concatenated in there would get overwritten by the next "step 2/4" and
# vanish. Same cap as `title`: a full repo id ("black-forest-labs/FLUX.1-schnell")
# is exactly the kind of thing this field carries, so it gets the same room.
MODEL_MAX = TITLE_MAX

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
    # The model running this row, if any — a dimmed suffix the UI draws after
    # the title (JobRow's `.dl-model`), never folded into `title` or `detail`.
    # "" means "no model to show" (a download, a scheduled run, a page's own
    # `fused.trackJob()`), which the manager renders with no element at all
    # rather than an empty one — see DownloadManager.tsx's `job.model &&`.
    model: str = ""
    kind: str = "task"
    state: str = RUNNING
    done: float | None = None
    total: float | None = None
    # Whether `total` prices the WHOLE download or only the phase currently in
    # flight (SPEC AI-5n, D498). "download" — a reporter used `download_plan`
    # (or a runner with only ever one repo, where a phase total already is the
    # whole download) and the figure is complete; "phase" — a bare
    # `download_snapshot` call, which only ever knows its own repo. Read by
    # `modelSize.ts`: a "download" total may WIN outright over the catalog's
    # hand-written constant, where a "phase" total may only ever raise it
    # (never-understate), because a phase total being SMALLER than the
    # constant is exactly what a multi-repo download in progress looks like,
    # not evidence the constant is wrong.
    total_scope: str = "phase"
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
    # When this row was first READ (via `list_jobs`) while in a TERMINAL
    # state — not when it finished. `FINISHED_TTL_S` counts from here, not
    # from `finished_at`; see that constant's own comment for why. Set for
    # every terminal state uniformly, `error` included, even though `error`
    # is exempt from the sweep that reads it — a per-state exception here
    # would buy nothing and cost a second code path to reason about.
    first_read_at: float | None = None
    # The id of another row this row is currently blocked on, or "" for the
    # ordinary case. Set by `ai/supervisor._wait_ready` while an image/video
    # render's row is waiting on a shared model load: the manager hides
    # whichever row this field names for as long as THAT row is running, so a
    # single wait for one model shows as ONE row instead of two saying the
    # same thing (SPEC §36 — one row per unit of work; D586's sibling case in
    # jobs.ts `jobRows` is the scheduled-run precedent for the same rule).
    # SERVER-ONLY (see `upsert`'s `server` gate below): this field HIDES a
    # row, so a page allowed to set it could blank a live download's only row
    # by falsely claiming to be waiting on it. Cleared the instant the wait
    # ends (ready, error, evicted, cancelled, or timed out) so a load that
    # THEN fails is not left invisible — D266's guarantee that both rows can
    # show a real failure only holds if the merge does not outlive the wait.
    waiting_for: str = ""


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
        if "model" in body:
            job.model = _text(body.get("model"), MODEL_MAX)
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
        if "total_scope" in body:
            job.total_scope = _one_of(body.get("total_scope"), TOTAL_SCOPES,
                                     "total_scope", job.total_scope)
        if "cancellable" in body:
            job.cancellable = bool(body.get("cancellable"))
        if "waiting_for" in body and server:
            # Silently dropped for a page report (no `server=True`) rather
            # than rejected — see the field's own comment on `Job` for why a
            # page is not allowed to set it at all. A falsy value (the normal
            # tick, and the wait-ended report) clears it; a real value is run
            # through `clean_id` so it stays a legal id and not a string that
            # would break the manager's lookup silently.
            value = body.get("waiting_for")
            job.waiting_for = clean_id(value) if value else ""
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

    Guarded on `RUNNING` alone, unchanged by `WAITING`'s arrival: a
    `WAITING` row has nothing left running to signal a cancel TO (the worker
    that wrote it already exited), so the client's own `canCancel` never
    calls this route for one at all — the dock's ✕ on a `WAITING` row goes
    through `dismiss`, below, exactly like it already does for `done` and
    `cancelled`. Setting `cancel_requested` here for a state nothing is
    listening for would only leave a flag standing that nothing ever clears.
    """
    now = time.time() if now is None else now
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        if job.state == RUNNING:
            job.cancel_requested = True
        return _public(job, now)


def clear_cancel_requested(job_id: str, *, now: float | None = None) -> dict | None:
    """Disown a flag a NEW attempt did not ask for. Returns the record, or
    None if there isn't one.

    `upsert`'s own body has no key for this — a reporter's tick legitimately
    has nothing to say about a request it did not make, so there is no
    "clear it" shape to put in a POST. This exists for the one caller that
    genuinely needs to say it: a mirror thread opening a NEW attempt under an
    id `upsert` reused from a previous one. `upsert` clears the flag itself
    on a transition INTO a terminal state, but a mirror that dies mid-attempt
    (its own `jobs.upsert` calls are best-effort) can leave the row `running`
    with the flag still set — and because job ids are deterministic per venv
    key, the next attempt's opening `upsert` inherits that row, sees no state
    change (it was already `running`), and never runs the clearing branch.
    Called once, right after that opening `upsert`, so a ✕ pressed from then
    on (`request_cancel`, above) still sets the flag normally — this only
    disowns what an attempt did not itself request.
    """
    now = time.time() if now is None else now
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        job.cancel_requested = False
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
    """Dismiss every TERMINAL record at once (the bulk "Clear" button).
    Returns how many.

    Used to also take `is_stalled(...)` rows — the SAME set `dismiss` (above)
    accepts one at a time — which meant Clear silently orphaned live work.
    `is_stalled` only means "no report in `STALE_AFTER_S`" (30s); a job can be
    genuinely `RUNNING` and merely quiet for that long — a slow model load, a
    generation between report ticks, a throttled background tab — and the
    work itself does not stop when its row does: `ai/supervisor._cancel_state`
    returns None for a missing row, and `_is_cancelled` reads None as "not
    cancelled". The row is gone, though, and `fused.watchJob` gives up after
    a few consecutive misses and reports the work as stopped — so a user
    reads Clear as having cancelled their AI job, while the real process
    keeps running with nothing on screen reporting it, which is precisely
    the failure this whole feature exists to prevent, reached through its
    own Clear button.

    The per-row ✕ (`dismiss`) is deliberately NOT changed: closing one
    specific stalled row is a choice by a user who usually knows what it
    was (see that function's own docstring) — only the BULK sweep, which
    cannot know that about any of the rows it takes, is wrong to extend to
    a row that may still be doing real work.

    **`WAITING` rows are excluded too, for the identical reason `dismiss`
    (above) already refuses to take one automatically.** `j.state !=
    RUNNING` used to be this filter, which also swept up every `WAITING`
    row — a row sitting in front of an open, answerable question (uv's
    "Install anyway" prompt), not finished work. The Notifications "Clear"
    button (`RepoUpdatesDock.tsx`) is gated on and titled around TERMINAL
    rows only, so a `WAITING` row was being taken by a control that neither
    shows it nor counts it, permanently — `_forget` also blocks the id from
    being re-created by a later tick, so the prompt could never come back
    either. Scoped to `TERMINAL_STATES` so a bulk Clear only ever takes what
    it claims to.
    """
    now = time.time() if now is None else now
    with _lock:
        gone = [j.id for j in _jobs.values() if j.state in TERMINAL_STATES]
        for job_id in gone:
            _forget(job_id, now)
        return len(gone)


def reset() -> None:
    """Empty the registry — for tests, and for nothing else."""
    with _lock:
        _jobs.clear()
        _dismissed.clear()


# --------------------------------------------------------------------- reading


def list_jobs(*, now: float | None = None, mark_read: bool = False) -> list[dict]:
    """Every live record, oldest first.

    Ascending by start time so rows never reorder under the pointer: a new job
    appends at the BOTTOM of the column, nearest the screen edge the eye is
    already on — the same ordering rule the toast stack above it follows.

    **`mark_read` defaults to False, and that default is deliberate, not an
    oversight to fix later.** This function is not only the shell's `GET
    /api/jobs` — `grep -rn "list_jobs(" fused_render/` also finds
    `supervisor._cancel_state` (polled every `_CANCEL_CHECK_INTERVAL_S`,
    0.5s, for the whole duration of every model load) and
    `capture._cancel_requested` (an internal cancel poll of its own), neither
    of which is a person looking at the corner. If this function marked rows
    read by default, a scheduled run finishing while any model happened to be
    loading would get `first_read_at` stamped by the supervisor's own poll
    within half a second, with no browser anywhere — the row would sweep
    `FINISHED_TTL_S` later with nobody having seen it, which is exactly the
    failure the read gate exists to prevent, reached through a different
    door, and silently: the same scheduled run would be visible or invisible
    depending on whether a model happened to be loading. Only
    `routers/jobs.py`'s `GET /api/jobs` — the one client-facing read of this
    list — passes `mark_read=True`.

    **The asymmetry that decides the default:** a row that never gets marked
    lingers for up to `FINISHED_UNREAD_DROP_S` (10 minutes) before the
    backstop takes it — a cosmetic cost, an extra row on screen. A row marked
    by a caller that was never actually looking loses the outcome entirely
    within `FINISHED_TTL_S` (3s) — not cosmetic, the exact failure this whole
    feature exists to prevent. Between "row lingers too long" and "row
    vanishes before anyone saw it", the safe default is the one that can only
    ever err toward lingering.

    This is the ONE place `first_read_at` CAN be set — not `upsert`'s own
    `_public` call when a reporter's tick lands it on a terminal state,
    which is a WRITE the reporter sees, not a READ the corner made. Setting
    it there would restart the exact bug FINISHED_TTL_S's read-gating
    exists to fix: the retention clock ticking down from the moment of
    completion again, just relabelled.

    **Order matters**: `_sweep` runs FIRST, against whatever `first_read_at`
    values already existed from an EARLIER call, and only THEN does this
    function (when `mark_read`) mark newly-terminal rows as read for THIS
    call. A row can therefore never be swept in the same call that first
    reveals it — the sweep that could act on today's read already ran before
    today's read happened.
    """
    now = time.time() if now is None else now
    with _lock:
        _sweep(now)
        if mark_read:
            for job in _jobs.values():
                if job.state != RUNNING and job.first_read_at is None:
                    job.first_read_at = now
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
    blinking back onto the screen every few seconds. Only a fresh opening
    report reopens a forgotten id, which is the one case that should. This
    protection is unchanged by the read-gating below: `_forget` is still what
    every age-out path funnels through.

    **Every terminal state a surface can actually show and let the user
    dismiss is kept until dismissed, same as `WAITING` (D657, scoped by the
    schedule carve-out below).** This used to read `job.state in ("error",
    WAITING)` — a `done` or `cancelled` row instead aged out `FINISHED_TTL_S`
    after its first READ (`job.first_read_at`), on the theory that the
    download manager only needed to answer "is my work done right now", not
    hold a log. THE FINDING: once EVERY finished job (D586, broadened by
    D656) routes to the shell's Notifications list instead of just failures,
    that list *is* meant to hold a log — a `done` row expiring 3s after its
    first read was deleting the very entry Notifications exists to keep, out
    from under a user who had not yet looked. THE FIX: `done` and
    `cancelled` get the same unconditional exemption `error`/`WAITING`
    already had. `MAX_JOBS`'s cap (below) is what bounds all of them now.

    **A `sys:schedule:*` row does NOT get this exemption, even though it is
    terminal.** The exemption's whole premise is "a human still needs to SEE
    this row, so do not sweep it out from under them" — but `jobRows`
    (frontend) drops every `sys:schedule:*` id from what Activity draws, and
    `ActivityDock.tsx` applies `jobRows` before `terminalJobs`, so a schedule
    row never reaches Notifications either. No surface shows it and none can
    dismiss it, so it has no claim on a "kept until dismissed" rule — kept
    that way regardless, it is one permanent row per turn on a schedule (a
    5-minute schedule saturates `MAX_JOBS` within hours, with only eviction
    pressure to shed it). A schedule run already gets `schedule-toast.ts`'s
    own toast and its own row on the Scheduled page, so nothing is lost by
    letting the registry row age out on the ORIGINAL read-gated
    `FINISHED_TTL_S` clock every terminal row had before D657 — the readers
    that clock exists for (`fused.watchJob`, the Scheduled page's own poll)
    are exactly the ones still reading this row.

    `FINISHED_TTL_S`/`FINISHED_UNREAD_DROP_S`/`job.first_read_at` are left in
    place rather than deleted for this reason — `list_jobs`'s `mark_read`
    still has other callers (`routers/jobs.py`, `supervisor.py`,
    `capture/__init__.py`) whose own read-vs-poll distinction does not
    depend on this branch, and the schedule carve-out above is exactly the
    one reachable state that still exercises this read-gated clock.

    **`WAITING` is exempt from the cap below (`evictable`), not only from
    ageing out here.** Its reporter has already exited (the sole producer,
    `envinstall._mirror_into_jobs`'s "Install anyway" prompt, has nothing
    left polling), so `updated_at` never advances — under the cap's own sort
    key, `(state == RUNNING, updated_at)`, a long-open `WAITING` row becomes
    the single least-recently-updated evictable row and would be the FIRST
    thing dropped once the registry fills, exactly the outcome this
    exemption exists to prevent. See the cap's own comment for the rest of
    the reasoning it shares with live `SERVER` rows.
    """
    for job_id, job in list(_jobs.items()):
        if job.state == RUNNING:
            if (now - job.updated_at) > STALE_DROP_S:
                _forget(job_id, now)
        elif job.state == WAITING:
            # No heartbeat left to keep it "fresh" the way a `RUNNING` row's
            # own ticks do — aging it out on any clock would make the
            # question it is sitting in front of (uv's "Install anyway"
            # prompt, still open on the page) vanish from the dock while it
            # is still exactly as open as when it appeared.
            continue
        elif job.state in TERMINAL_STATES:
            if job_id.startswith(SCHEDULE_JOB_PREFIX):
                # No surface shows this row or lets it be dismissed — see
                # this function's own docstring — so it ages out on the
                # ORIGINAL read-gated clock every terminal row had before
                # D657, instead of the keep-until-dismissed rule below.
                if job.first_read_at is not None:
                    if (now - job.first_read_at) > FINISHED_TTL_S:
                        _forget(job_id, now)
                elif (now - (job.finished_at or job.updated_at)) > FINISHED_UNREAD_DROP_S:
                    _forget(job_id, now)
            else:
                # A row some surface can show and let the user dismiss —
                # kept until they do (see this function's own docstring for
                # why that is now every non-schedule terminal state).
                continue

    # **The cap counts only what it could actually shed.** Measuring it against
    # every row while refusing to evict most of them does not bound anything —
    # it just moves which row pays, and the row that paid was whichever had
    # JUST finished: over the cap with 64+ live server rows, the terminal row
    # was the only candidate left and went on the very next `list_jobs()`, which
    # is the same read `fused.watchJob` polls. So a watcher never saw the
    # outcome. A success survives that (the artefact is on disk) but a failure
    # or a cancel has no artefact, so the page reported "no longer being
    # reported" instead of the real reason — on exactly the large queue the
    # exemption exists to support.
    #
    # It also put the cap in contradiction with BG-6: a finished record is
    # supposed to stay `FINISHED_TTL_S` so someone not watching the corner can
    # notice it, and this was silently shortening that to zero under pressure.
    #
    # `WAITING` is exempt from this list, not only from the age-out above.
    # Its reporter has already exited, so `updated_at` never moves again —
    # under the sort key below, `(state == RUNNING, updated_at)`, that makes
    # a long-open `WAITING` row the single OLDEST-updated evictable row,
    # first in line the moment the registry fills. A queue of downloads (SPEC
    # AI-10a) plus a schedule ageing out per-turn (above) is enough to reach
    # `MAX_JOBS`, and the very first thing evicted would then be the one row
    # this whole exemption exists to protect: an open "Install anyway"
    # prompt, dropped while the question it is sitting in front of is still
    # exactly as open as when it appeared.
    evictable = [
        job for job in _jobs.values()
        if not (job.state == RUNNING and job.owner == OWNER_SERVER)
        and job.state != WAITING
    ]
    if len(evictable) <= MAX_JOBS:
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
    order = sorted(evictable, key=lambda j: (j.state == RUNNING, j.updated_at))
    for job in order[: len(evictable) - MAX_JOBS]:
        del _jobs[job.id]
