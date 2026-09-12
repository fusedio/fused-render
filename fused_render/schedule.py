"""Scheduled Claude messages: a durable list of "send this prompt to this
target at this time", plus the loop that sends them.

The app already knows how to start a detached Claude Code session from the
server process (`claude_spawn`). What it had no way to say was *later*. This
module is that word, and it is deliberately the whole of it: the schedule lives
here, the firing decision lives here, and the only thing the OS is asked for is
to have the app running (`schedule_wake`).

**Why the app owns the send rather than cron/launchd owning it.** An external
scheduler can run `claude -p` perfectly well — and would run it in a different
world. `supervisor/paths.py:child_environment` injects some twenty variables
into every child the app spawns (state dir, cache dirs, the bundled rclone and
uv, `TMPDIR`, and the `CLAUDE_CONFIG_DIR` passthrough that a relocation once
broke), and `_plugin_argv` hands the session fused-render's skills only when
that env contract is present. A crontab line reproduces none of it, so the
scheduled turn silently becomes a different install: other state dir, no
skills. On macOS it is worse than different — D72's TCC finding is that a
process which is not the app does not inherit the app's Documents/Desktop
grants, so a cron-launched turn touching ~/Documents raises a consent prompt
with nobody present to answer it, and the credentials it needs live in the login
Keychain of a GUI session it is not in. Firing from inside the server process
makes a scheduled turn environmentally identical to one the user typed.

**What that costs, stated plainly: nothing fires while the app is not running.**
That is the trade this design accepts, and the two mechanisms below are what
make it survivable rather than silent:

* **Wall-clock, not tick-counting.** Every tick asks "what is due *now*",
  comparing stored timestamps against the clock. Nothing counts elapsed ticks,
  so a laptop that slept through a due time fires on the tick after it wakes,
  and an app that was quit fires on the tick after it next starts. Catch-up
  is not a feature here; it is what the absence of tick-counting gets for free.
* **A QUEUE, rather than a bound on how late is still worth sending.** This is
  the part that changed, and the old reasoning is worth keeping visible because
  the new rule is an answer to it rather than a denial of it. The bound existed
  because unbounded catch-up is its own bug: a message meant for Tuesday's 9am
  standup, fired unattended on Friday afternoon against a repo that has moved
  on, is worse than one that never fired. The default was a day; past it an
  entry became `missed` — visible, never sent.

  What that got wrong is *who decides*. A day is a guess about the user's
  habits made by a constant, and the user was never asked. So the decision moved
  to them: missed work is **queued** and runs when the app next opens, and the
  queue is a surface with cancel-each and cancel-all on it (`queue`,
  `cancel_queued`, and the popover the shell raises from them). Silently
  discarding a message is no longer something this module does on its own.

  Three rules make that safe rather than reckless, and each is load-bearing:

  1. **One-offs are unbounded, because an unsent one-shot is GONE.** Ten
     one-offs missed over two weeks all fire on open. `max_late_seconds()`
     therefore answers `None` by default — no bound. An operator who sets
     `FUSED_RENDER_SCHEDULE_MAX_LATE` explicitly still gets one, and it still
     produces `missed` exactly as before; the env var is the escape hatch for
     an install that wants the old shape.
  2. **Recurring occurrences COALESCE — only the latest missed run is sent**
     (`_coalesce`). The surviving half of the old 120-second occurrence bound:
     replaying a week of "daily at 9am" into one thread is not what the words
     meant, and the next run is already coming. The dropped runs are counted
     and reported on the survivor (`skipped`, `skipped_note`) rather than
     vanishing.
  3. **Scheduling into the past is allowed** and is recorded with the due time
     the user picked, so history reads truthfully. Because the queue runs in
     due order, a due time in the past sorts ahead of everything later — it is
     at the head of the queue and goes on the next tick. **A REPEAT anchored in
     the past reads the same way**: the LATEST slot at or before now is
     materialized once and goes immediately, the slots before it never happen,
     and the series then continues from now (`_catch_up_base`). That is rule (2)
     arrived at from the other side, and it is literally the same walk.

  Nothing here counts ticks either: coalescing walks the recurrence with
  wall-clock arithmetic, asking "which occurrences lie between this entry's due
  time and now", and never "how many ticks did we miss".

**The claim-before-spawn order matters.** An entry is written `sending` BEFORE
the helper is spawned, not after. If the process dies mid-spawn the entry is
`sending`, not `pending`, so the next boot does not send it again — a stuck
entry that a sweep later reports as interrupted. That is the safe direction to
fail: an unsent message is a disappointment, a message sent five times over five
crash-restarts is an agent running unattended five times.

**Permission mode.** A scheduled turn has no page attached, so it inherits the
apps API's problem (see `_SCHEDULED_PERMISSION_MODE`) and its answer, with one
extra wrinkle: the apps API's session is one the user is about to look at, and
this one is by definition unattended. The mode is therefore per-entry and
recorded with the entry, so "auto" is a choice made per message rather than a
property of scheduling.

**Nobody is looking when any of this happens**, which is the premise of the
feature and therefore the premise of its reporting: a row on a page the user has
to think to visit is not how they should learn that last night's message failed.
Two surfaces close that, and the block above `_JOB_PREFIX` is where they are
explained — a live job row (what is it doing, including "parked on a permission
card") and an event log the shell toasts (what happened while I was away).

No import of anything under `fused_render.server` — the router imports this
module; keep it acyclic.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from fused_render import claude_spawn, cron, recur
from fused_render.shell import storage

logger = logging.getLogger(__name__)

# The store. Branch-aware via storage.home_dir(), so a dev checkout on a branch
# ref never fires the baseline install's messages (its own dir, its own list).
_STORE_NAME = "scheduled_messages.json"

# ---------------------------------------------------------------- the states
#
# `pending` is the only state the loop acts on, and `sending` is the only
# non-terminal one it can leave behind (see the claim-before-spawn note above).
PENDING = "pending"
SENDING = "sending"
SENT = "sent"
MISSED = "missed"
ERROR = "error"
CANCELLED = "cancelled"
# A recurring TEMPLATE, not a message: never claimed, never sent. Each tick
# materializes its next run as an ordinary `pending` occurrence (carrying
# `template_id`), so everything downstream — claiming, job rows, events, the
# watcher — only ever handles one-shots. See `_materialize`.
#
# TWO kinds of template share this state, and deliberately so: a cron one
# (`repeats`, a 5-field line) and a structured one (`rule` + `anchor` + `made`,
# see recur.py). Everything between the template and the send is identical for
# both — only "when is the next one" differs, which is `_next_template_due` and
# nothing else. A third state for the new kind would have made every consumer
# (the listing's live/handled split, cancel's cascade, restore's guard, the
# router's projection) grow a second branch to say the same thing twice.
RECURRING = "recurring"
STATES = (PENDING, SENDING, SENT, MISSED, ERROR, CANCELLED, RECURRING)

# How often the loop looks. A scheduled message is a minute-granularity promise
# at best (the user picks a wall-clock time, not a deadline), and a tick is one
# small JSON read, so this is chosen for "fires close enough to the stated
# minute" rather than for precision.
POLL_INTERVAL_S = 30

# How late an overdue message may still be sent. **None means no bound**, which
# is the default: a missed one-off is queued and runs when the app next opens,
# however old (see the module docstring). It was 24h, and the env var — which
# still works, and still produces `missed` past its value — is what an install
# that wants the old shape sets. "A day" was always a judgement about the user's
# habits rather than a fact, and the queue is where that judgement now lives.
_DEFAULT_MAX_LATE_S: int | None = None
_MAX_LATE_ENV = "FUSED_RENDER_SCHEDULE_MAX_LATE"

# How many occurrences `_coalesce` will walk past in one pass before giving up
# and firing what it has reached.
#
# A bound on WORK, not on lateness — nothing here decides whether a message is
# too old, only how long one sweep may spend catching a recurrence up. An app
# closed for a year with an every-minute rule is half a million steps of
# recurrence arithmetic on the tick that reopens it, and the tick thread is the
# one that also fires everything else due. Hitting the cap costs only precision
# in the REPORT: the survivor fires at the occurrence the walk reached rather
# than at the very latest one, and `_materialize` still puts the successor ahead
# of `now`, so no backlog is left behind either way.
_COALESCE_MAX_STEPS = 20000

# How long an entry may sit in `sending` before a sweep calls it interrupted.
# Generously past the helper's own 60s timeout: the window this covers is the
# process dying between the claim and the result, not a slow spawn.
_SENDING_STUCK_S = 300

# Mode the scheduled session runs in when the caller names none. Same reasoning
# as the apps API's `_APP_SESSION_PERMISSION_MODE`: nobody is polling `decide`,
# so under the strict default ("prompt") the first tool call parks a request in
# the run's perm/ dir and blocks until PERMISSION_WAIT expires and the server
# denies it — a message that "sent" and did nothing. "auto" lets the CLI's own
# classifier approve what it judges safe and park the rest, which is the most a
# turn nobody is watching can honestly be given.
_SCHEDULED_PERMISSION_MODE = "auto"

# The modes agent.py accepts — the same four, in the same spelling. Hardcoded
# rather than imported, because importing means loading the template backend (a
# module-level `exec_module`) on every validation.
#
# Copying it is only safe because a TEST holds the copies together
# (test_claude_schedule_pill.py, the technique agent.py's own SWITCHABLE_MODES
# comment names). The first version of this line omitted `acceptEdits` while its
# comment called the list four words long, and the failure mode is worth
# recording: `_start` re-validating downstream means drift can never buy a
# scheduled turn MORE auto-approval than the template offers — but it can do the
# opposite, and did. A composer sitting on `acceptEdits` had its schedule refused
# with "expected one of (...)", naming a mode the user had never chosen.
PERMISSION_MODES = ("prompt", "auto", "acceptEdits", "plan")

# ------------------------------------------------------------------ reporting
#
# A scheduled message is the one kind of work in this app that NOBODY is looking
# at when it happens — that is its whole premise — so the two surfaces below are
# not decoration, they are the only way a user finds out what it did.
#
#   the JOB REGISTRY (jobs.py, D244) answers "what is it doing right now": one
#     `task` row per send, live in the shell's download manager from anywhere in
#     the app, carrying the turn's phase and — the one worth having — whether it
#     is parked on a permission card nobody has answered.
#   the EVENT LOG below answers "what happened while I was away": an
#     append-only, monotonically-ided log the shell polls and turns into toasts,
#     exactly the shape the mount-health monitor established.
#
# Both are best-effort and neither is authoritative: the store is the record.

# `sys:` marks a job this process owns, which is what lets the manager's ✕ be a
# real cancel rather than a request (jobs.OWNER_SERVER). One id per entry, so a
# re-report after a server restart re-attaches to the same row.
_JOB_PREFIX = "sys:schedule:"

# Bounded like the mount-health log: this is a running narration for the UI to
# toast, not history. The store holds every entry's outcome durably.
_EVENTS_MAX = 100

# What the shell narrates. `done` is an info toast (your message ran), the other
# two are errors that need a person — which is the gap this log exists to close.
#
# THERE IS NO EVENT FOR A RUN PARKED ON A CARD (Akshil, 2026-09-03). One was
# added on this branch and taken straight back out: a toast for it interrupted
# the reader for something the Tasks page already says on its own — the row
# wears the Needs attention ring and sorts to the top of the list, which is
# where somebody goes to act on it anyway. This log is for what happened while
# nobody was looking, and a run that is still going has not happened yet.
EVENT_DONE = "done"
EVENT_FAILED = "failed"
EVENT_MISSED = "missed"
EVENT_KINDS = (EVENT_DONE, EVENT_FAILED, EVENT_MISSED)

_events: list[dict] = []
_event_seq = 0
# The highest event id a client has confirmed it narrated. **Server-side on
# purpose**, and the correction to the first shape of this feature, which copied
# the mount-health poller's "first successful poll is a silent baseline" rule.
#
# That rule is right for mounts and exactly wrong here. Mount health emits
# nothing at startup by design (a mount already broken at boot is left alone),
# so its baseline only ever swallows a previous session's log. THIS log's most
# important events — the `missed` verdicts from the catch-up pass — are emitted
# by the loop's first tick, which lands well before the shell has loaded. A
# client-side baseline therefore marked them seen and never said a word, which is
# the precise failure the log was added to prevent.
#
# So the client narrates everything it is given and confirms what it narrated;
# the server is what remembers, which also makes a reload silent for free. The
# mark is in memory next to the ring it indexes: both describe THIS run of the
# app, and the durable record of every outcome is the store.
_delivered = 0
_events_lock = threading.Lock()

# Serialises the read-modify-write of the store. `storage.write_json` is atomic
# per write (temp + os.replace) but the store is read-modify-written from the
# loop thread, the request thread, and the recording threads, and last-write-wins
# across THOSE would drop a cancel or resurrect a fired entry.
_lock = threading.RLock()

# Serialises the wake stub's launchctl pair. Separate from `_lock` because
# `_sync_wake` must not hold the store lock across two subprocesses; see there for
# why it also has to RE-READ rather than take a snapshot from its caller.
# Lock order is `_wake_lock` then `_lock`, never the reverse.
_wake_lock = threading.Lock()

# Entry ids whose turn THIS process is watching.
#
# The store cannot answer that question, and that is the whole reason this exists:
# `sent` with no `turn` is what a LIVE turn looks like and equally what one
# abandoned by a killed process looks like. Only the difference decides whether the
# sweep should close the entry, and only a live process knows it.
_watched: set[str] = set()
_watched_lock = threading.Lock()

_thread: threading.Thread | None = None
_thread_lock = threading.Lock()

# THE LOOP'S DOORBELL — how work that is due NOW gets sent now.
#
# The loop used to `time.sleep(POLL_INTERVAL_S)`, which is right for asking
# "what came due while I was asleep" and wrong for the one case the user
# actually watches: scheduling something for a time that has already passed, or
# restoring a skipped run whose time has gone. Those are due the instant they
# are stored, and the tick that would send them was up to 30 seconds away —
# then the Tasks page's own poll (10-30s) on top of that. A message asked for
# NOW sat reading "Upcoming" for the best part of a minute with nothing
# happening, which is indistinguishable from a scheduler that is not running.
#
# So a mutation that stores past-due work rings this, and the loop wakes. It is
# a HINT AND NEVER A MECHANISM: every rule about what fires stays in `tick`,
# which still runs on its own timer, so a missed ring costs latency and nothing
# else. That is what makes it safe to ring from a request thread (`Event.set`
# is atomic and idempotent), and why the loop clears the flag AFTER waking
# rather than before ticking — a ring that lands mid-tick then wakes the pass
# after it, instead of being swallowed by the pass that was already running
# when it arrived.
_wake = threading.Event()


def _ring(entries: list[dict] | None = None, now: datetime | None = None) -> bool:
    """Wake the loop if anything in `entries` is pending and already due.

    Reads the store when handed nothing. The test is deliberately the cheap
    half of `_claim_due`'s — pending, and due — because a spurious ring costs
    one early tick that finds nothing to do, while a missed one costs the user
    the whole poll interval."""
    now = now or _now()
    if entries is None:
        with _lock:
            entries = _read()
    for entry in entries:
        if entry.get("state") != PENDING:
            continue
        try:
            if parse_due(entry.get("due")) <= now:
                _wake.set()
                return True
        except ValueError:
            continue
    return False


def wake() -> None:
    """Ring the loop's doorbell unconditionally — "look again, now".

    A HINT, exactly like `_ring`, and never a mechanism: every rule about what
    fires stays in `tick`, so a ring that lands at the wrong moment costs one
    early pass that finds nothing. `_ring` asks the store whether anything is
    due before ringing; this is for callers that already know something changed
    OUTSIDE the store and cannot answer that question — the watcher seeing a
    holder's process exit, a turn ending in this process. Their news is "a
    folder just freed", which no entry's due time records.
    """
    _wake.set()


# ------------------------------------------------------------- the second ring
#
# `_turn_ended` rings the loop the moment a verdict lands, which is what makes
# "the next task starts in about a second" true for every hold that ends on an
# EVENT. Two of them do not: a transcript that reads live until its 45-second
# window runs out, and a folder holder that is `starting` or `reserved` — a
# grace period with a clock on it. Nothing rings when those lapse, so a tick
# that held something for one of them would wait out the whole 30-second poll.
#
# THE TIMER IS SET FOR WHEN THE CLOCK ACTUALLY RUNS OUT (round-3 review,
# 2026-09-12). It used to ring two seconds later and then — keyed on the set of
# entries held, which does not change while the hold stands — never again, so
# for the 20-second reservation, the two-minute starting grace and the
# 45-second transcript window it exists for it rang once, far too early, and
# left the 30-second poll to do the work anyway. The pass that holds knows how
# long each hold has left (`project_queue.holder_expires_in`, `_live_expires_in`),
# and the earliest of those is the moment worth waking for.
_REARM_FLOOR_S = 0.5     # never ring busier than this
_rearm_lock = threading.Lock()
_rearm_timer: threading.Timer | None = None


def _cancel_rearm() -> None:
    """Drop any pending re-ring. One timer at a time is the whole invariant —
    `_rearm` calls this before arming, and a shutdown (or a test) calls it to
    leave nothing behind. The timer is a daemon, so nothing here keeps the
    process alive either way."""
    global _rearm_timer
    with _rearm_lock:
        timer, _rearm_timer = _rearm_timer, None
    if timer is not None:
        timer.cancel()


def _rearm(delays: list[float]) -> None:
    """Ring the loop again when the earliest hold this pass made expires.

    `delays` is seconds-from-now, one per hold that ends on a clock rather than
    on an event; an empty list means every hold this pass made has a bell of its
    own and no timer is needed. Bounded on both ends: never sooner than
    `_REARM_FLOOR_S` (a hold whose clock has already run out re-ticks once, it
    does not spin) and never later than `POLL_INTERVAL_S`, which is the floor
    under all of this and the reason a missed ring costs latency and nothing
    else.

    **Idempotent per pass, not per episode.** Called on every tick, and every
    call replaces the one timer this module owns — a hold that stands for ten
    minutes therefore costs one timer at a time, re-armed to the remaining time
    as the clock runs down, rather than one timer per tick piling up or (the bug
    this replaces) one timer for the whole episode fired two seconds in.

    A hint like `wake` itself: the timer only sets an Event, every rule about
    what fires stays in `tick`, and a ring that lands on a state that has not
    moved costs one early pass that finds nothing. Best-effort — a timer that
    cannot start leaves the ordinary poll interval doing what it always has."""
    global _rearm_timer
    _cancel_rearm()
    if not delays:
        return
    delay = min(max(min(delays), _REARM_FLOOR_S), float(POLL_INTERVAL_S))
    try:
        timer = threading.Timer(delay, wake)
        timer.daemon = True
        timer.start()
    except Exception:  # noqa: BLE001 — the 30s poll is the floor under this
        logger.debug("could not re-arm the schedule loop", exc_info=True)
        return
    with _rearm_lock:
        _rearm_timer = timer


def _live_expires_in(session_id: str, now: datetime) -> float:
    """Seconds until the per-session transcript hold on `session_id` lapses by
    itself — 0.0 when nothing can be read, which asks for no timer at all.

    `_session_live` is true while the tail's last real activity is inside
    `RUNNING_WINDOW_SEC`, so that moment is when this hold ends if nothing else
    ends it first. The echo window (`VERDICT_ECHO_SEC`) is not a second
    candidate: it can only ever silence a hold that the 45-second window is
    already holding, so the window is always the later of the two and always
    the one that actually frees the entry."""
    try:
        from fused_render import session_liveness

        active = session_liveness.session_activity(session_id, now.timestamp())
    except Exception:  # noqa: BLE001 — an unreadable tail asks for no bell
        logger.debug("could not read the tail for %s", session_id, exc_info=True)
        return 0.0
    if not active:
        return 0.0
    return max(0.0, active + session_liveness.RUNNING_WINDOW_SEC - now.timestamp())


def _pq():
    """`project_queue`, imported on use.

    That module reads this one back (`_sending_entries` asks for the claimed
    entries), so a module-level import here would close the cycle — which is
    why it imports this one inside a function too. Everything below is gated on
    `project_queue.enabled()`, and with the flag off the only cost is this
    lookup."""
    from fused_render import project_queue

    return project_queue


# How far a `follow_of` chain is walked before the walk simply stops. The shape
# the client makes is two deep — a message typed into a chat whose first message
# is still queued, and a second typed behind that — and the bound is a little
# more so that a hand-edited store (a chain of twenty, or one that points at
# itself) cannot spin a tick. Stopping early costs a follower its leader's key,
# which is the same answer an erased leader already gives.
FOLLOW_HOPS = 4


def leader_of(entry: dict, by_id: dict | None) -> dict | None:
    """The entry at the head of `entry`'s follow chain, or None if it follows
    nothing.

    `follow_of` is the one-off twin of `template_id` + `_chain_session`: a
    message typed into a brand-new chat whose FIRST message is still queued has
    no session to name, so it names the entry it was typed behind instead, and
    everything that groups, orders or dispatches that work reads the leader's
    answer through here. `by_id` is the store the caller already holds, keyed by
    entry id — it is not read from disk here, because every caller is either
    mid-pass over the entries or inside the lock that owns them.

    **The LAST entry that exists, not the id that was written.** A leader the
    user erased does not orphan the chain behind it: the walk stops at whatever
    it reached, so a follower of an erased leader stands alone (exactly as it
    did before this field existed) and a follower of THAT follower groups under
    it. Bounded by `FOLLOW_HOPS`, and a chain that points back at something
    already seen ends there — this walks a store a human can edit, and a cycle
    must cost nothing.

    **A CHAIN LONGER THAN `FOLLOW_HOPS` HAS NO LEADER AT ALL — None, not the
    middle entry the walk happened to stop on** (round-2 review, 2026-09-12).
    Answering with a middle entry files the follower under a key that is itself
    a follower: `_task_key` would put it under `pending:<middle>` while the
    middle entry's own row is `pending:<head>`, so one message would appear
    under two keys and the ring would reach neither row reliably. Standing alone
    is the answer an erased leader already gives, it is a shape only a
    hand-edited store can make, and it costs that entry its place in its chat
    rather than the listing its consistency.
    """
    if not by_id:
        return None
    seen = {str(entry.get("id") or "")}
    leader: dict | None = None
    current = entry
    for _ in range(FOLLOW_HOPS):
        nxt = str(current.get("follow_of") or "")
        if not nxt or nxt in seen:
            break
        found = by_id.get(nxt)
        if found is None:
            break
        seen.add(nxt)
        leader = current = found
    else:
        # Every hop spent and the chain still goes somewhere real: too long to
        # file, so this entry stands alone.
        nxt = str(current.get("follow_of") or "")
        if nxt and nxt not in seen and by_id.get(nxt) is not None:
            return None
    return leader


def _by_id() -> dict:
    """The store keyed by entry id, for a caller that holds no copy of its own.
    Under the lock, like every other read here."""
    with _lock:
        return {str(e.get("id") or ""): e for e in _read()}


def _task_key(entry: dict, by_id: dict | None = None) -> str:
    """The Tasks page's key for one entry — the session it ran in, else the one
    it named, else its LEADER's key, else `pending:<id>`.

    The SAME rule as the tasks router's `_entry_session` plus its fallback, and
    it has to be: `tasks_watch.notify` keys are matched against the rows that
    listing built, so a key spelled differently here would ring a row nobody is
    watching. Which is also why the follower case belongs here: a message typed
    into a queued chat is filed under the LEADER's row, so ringing its own
    `pending:<id>` would ring nothing at all.

    `by_id` is the caller's own copy of the store; the disk is read only for an
    entry that actually follows one, which is the only case that needs it."""
    session = str(entry.get("claude_session_id") or entry.get("session_id") or "")
    if session:
        return session
    from fused_render import tasks_store

    if str(entry.get("follow_of") or ""):
        leader = leader_of(entry, _by_id() if by_id is None else by_id)
        if leader is not None:
            session = str(leader.get("claude_session_id")
                          or leader.get("session_id") or "")
            return session or tasks_store.pending_key(str(leader.get("id") or ""))
    return tasks_store.pending_key(str(entry.get("id") or ""))


def _notify(keys: set[str]) -> None:
    """Tell the Tasks long-poll which rows moved. Best-effort: a watcher that
    cannot be rung costs one poll interval, never the write that got here."""
    keys = {k for k in keys if k}
    if not keys:
        return
    try:
        from fused_render import tasks_watch

        tasks_watch.notify(keys)
    except Exception:  # noqa: BLE001 — a missed ring is latency, not an error
        logger.debug("could not notify the tasks watcher", exc_info=True)


def store_path() -> str:
    return os.path.join(storage.home_dir(), _STORE_NAME)


def shots_dir() -> str:
    """Where task-attached images live: ``~/.fused-render/task-shots``.

    NOT the claude template's own ``shots`` dir, which is tempdir-rooted and
    swept on a 12-hour TTL — an annotation is junk once its turn is over, but a
    scheduled task can fire days after its images were attached, and a repeat
    re-reads them on every run. Branch-aware via ``storage.home_dir()`` like
    the store itself.

    **Resolved, and forward-slashed.** The pre-allowed Read rule matches TEXT,
    not inodes, so every spelling of this directory in the system has to be the
    same spelling: `_images` stores `realpath`s on the entry, `_attachments_block`
    puts those in the prompt, and `_send` pre-allows THIS. Left unresolved, the
    two disagree wherever a symlink sits on the path — a symlinked home, or
    macOS' own `/tmp` -> `/private/tmp` — and the headless run is handed paths it
    is not allowed to open (Bugbot, PR #865)."""
    return os.path.realpath(
        os.path.join(storage.home_dir(), "task-shots")).replace("\\", "/")


#: The claude page's wire tag for a message's attachments — a SECOND COPY of
#: `PANE_SHOT_TAG` in fused_render/templates/claude/template.html, which is the
#: canonical one. It cannot be imported in either direction (a template may not
#: import fused_render, SPEC PY-15 / D166), and it is already spelled a third
#: time in `tasks_store._MACHINERY_STRIP` and a fourth in `agent.py`'s. The
#: parity test in tests/test_schedule_images.py reads the page's constant out of
#: template.html and compares this one to it.
_PANE_SHOT_TAG = "pane-shot"


def _images(value) -> list[str]:
    """Validate a request's ``images`` into stored task-shot paths.

    NO COUNT CAP and NO TYPE CHECK (D618). `IMAGES_MAX` (4) is gone, for the
    reason D615 deleted the chat's byte cap: it protected nothing the shots
    directory's own pruner did not already own, and it refused the gesture the
    feature exists for — dropping the five files a task is about. The key is
    still spelled ``images`` on the wire and in the store, because every entry
    written before today spells it that way and a rename would be a migration
    bought for a word; what it holds is any file the upload endpoint minted.

    Only paths under ``shots_dir()`` are accepted — the upload endpoint is the
    only thing that writes there, so this is what keeps the field from being a
    way to point a scheduled prompt at an arbitrary file on disk. Realpath
    membership, not string prefix on the raw value, so a symlink under the dir
    cannot smuggle a target out of it."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("images: expected a list of attachment paths")
    return [_shot_path(item, "images") for item in value]


def _shot_path(value, field: str) -> str:
    """One request-supplied attachment path, resolved into a shots_dir resident.

    The containment rule, in ONE function, because two fields now carry the
    same paths — ``images`` (the flat list every stored entry has always had)
    and ``attachments`` (the same paths with the user's own filename and kind
    beside them, see `_attachments`). Two copies of a containment check is two
    chances to relax one of them."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field}: expected a list of attachment paths")
    root = os.path.realpath(shots_dir())
    real = os.path.realpath(os.path.expanduser(value.strip()))
    if not real.startswith(root + os.sep):
        raise ValueError(f"{field}: not a task attachment path")
    if not os.path.isfile(real):
        raise ValueError(f"{field}: attachment no longer exists")
    return real.replace("\\", "/")


#: What an attachment's `kind` may be. The claude page's own vocabulary minus
#: the two kinds only a browser can produce: "pane" and "overview" are pictures
#: of a screen somebody was looking at, and a scheduled run has no screen (see
#: `_outgoing`). A task attachment is a picture the user brought in ("image") or
#: a file that is not a picture at all ("file") — the same two the upload
#: endpoint mints and the same two the New task card draws.
_ATTACH_KINDS = ("image", "file")

#: How long an attachment's display name may be. It is the filename the user
#: recognises, shown on a chip and named in the prompt — not a place to put a
#: paragraph, and the field arrives from a request body.
_ATTACH_NAME_MAX = 255


def _attachments(value, images: list[str]) -> list[dict]:
    """Validate a request's ``attachments`` into stored ``{path, name, kind}``.

    WHY THIS EXISTS ALONGSIDE ``images``: the fired run's message is the claude
    page's own `<pane-shot>` block now (see `_attachments_block`), and that
    block is read back by the chat to draw a RECEIPT ROW per attachment — a
    thumbnail, or a 📄 and the file's name. A bare path cannot fill that row
    honestly: the stored name is a minted timestamp (`a1b2c3d4.pdf`), and the
    kind of a `.tif` that the upload endpoint transcoded is not the kind its
    extension says. Both facts are known in the browser at attach time and
    nowhere else, so they travel.

    ``images`` DOES NOT GO AWAY, and neither field is required:

      * both sent — what the New task card does — and each is validated on
        its own, `create` storing both exactly as they arrived. They are not
        cross-checked: both fields are already confined to `shots_dir()`, so a
        client that disagreed with itself would only be describing its own
        files oddly, and refusing the schedule over it would turn a cosmetic
        mismatch into a lost task;
      * only ``images`` (every client written before today, and every entry
        already on disk) — the name is the path's basename and the kind is
        guessed from its extension. A worse answer than the browser's, and the
        only one available; it is exactly what the New task card's own Edit
        path does with a stored path (`attachmentKindOf`);
      * only ``attachments`` — ``images`` is derived from it, so `_send`, the
        occurrence copy and every existing reader keep working unchanged.

    Same containment as `_images` (`_shot_path`), because this field is the
    other way a request names a file to put in a prompt."""
    if value in (None, ""):
        return [_derived_attachment(p) for p in images]
    if not isinstance(value, list):
        raise ValueError("attachments: expected a list of "
                         "{path, name, kind} objects")
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("attachments: expected a list of "
                             "{path, name, kind} objects")
        path = _shot_path(item.get("path"), "attachments")
        kind = item.get("kind")
        if kind not in _ATTACH_KINDS:
            raise ValueError("attachments: kind must be one of "
                             + ", ".join(_ATTACH_KINDS))
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError("attachments: name must be a string")
        # BASENAME, and never the raw value: this name is only ever DISPLAYED,
        # so a client that sent a path here must not have it read as one — and
        # a newline in it would break the one-line-per-row reading of the block
        # it lands in. An empty one falls back to the stored file's own name
        # rather than refusing: a nameless chip is a small loss, a refused
        # schedule is not.
        name = os.path.basename((name or "").strip().replace("\\", "/"))
        name = name.replace("\r", " ").replace("\n", " ").strip()
        if len(name) > _ATTACH_NAME_MAX:
            raise ValueError(
                f"attachments: name is longer than {_ATTACH_NAME_MAX} characters")
        out.append({"path": path, "name": name or os.path.basename(path),
                    "kind": kind})
    return out


def _stored_attachments(entry: dict) -> list[dict]:
    """An ENTRY's attachments, read back — never re-validated.

    `_attachments` is the request path and it refuses a file that has gone; a
    stored entry has to survive one. A scheduled task can fire days after it
    was written and its files can be moved out from under it, and a
    materialization or a send that RAISED over that would take down the whole
    tick — where the run itself only needs to be handed the path and let the
    Read fail with something the user can read.

    Also the migration: an entry written before the field existed has only
    ``images``, and gets basename/extension answers (`_derived_attachment`)."""
    stored = entry.get("attachments")
    if isinstance(stored, list) and stored:
        out: list[dict] = []
        for item in stored:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            kind = item.get("kind")
            fallback = _derived_attachment(path)
            out.append({
                "path": path,
                "name": str(item.get("name") or "").strip() or fallback["name"],
                "kind": kind if kind in _ATTACH_KINDS else fallback["kind"],
            })
        if out:
            return out
    return [_derived_attachment(str(p)) for p in (entry.get("images") or [])
            if isinstance(p, str) and p.strip()]


def _derived_attachment(path: str) -> dict:
    """One `{path, name, kind}` for a path that arrived WITHOUT one — a legacy
    entry, or a client still sending only ``images``.

    The extension is all there is to go on, and it is the same list the New
    task card guesses from (`DRAWABLE_EXTS` in NewJobModal.tsx): only formats a
    browser actually draws count as "image", because the receipt row this feeds
    puts an `<img>` on a picture and a 📄 on everything else — and an `<img>`
    pointed at a `.csv` is a broken-image glyph, which reads as a bug."""
    ext = os.path.splitext(path)[1].lower()
    return {"path": path, "name": os.path.basename(path),
            "kind": "image" if ext in _DRAWABLE_EXTS else "file"}


#: Mirror of `DRAWABLE_EXTS` in frontend/src/shell/NewJobModal.tsx — the formats
#: a browser can draw. Duplicated rather than shared because one side is Python
#: and the other TypeScript; a test pins the pair (D146: the duplicated rule
#: gets a test, not a comment).
_DRAWABLE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif",
                            ".bmp", ".svg", ".ico"})


def max_late_seconds() -> int | None:
    """The catch-up bound in seconds, or **None for no bound** — which is the
    default, and what makes a missed one-off queue rather than expire.

    An operator who sets `FUSED_RENDER_SCHEDULE_MAX_LATE` to a positive number
    still gets the old behaviour: past that many seconds an entry becomes
    `missed`, visible and never sent.

    Anything else falls back to the default, unbounded. That covers a nonsense
    value (the env var is a string a human typed) and 0/negative, which used to
    be refused because a zero bound would have meant "expire everything the
    instant it is late" — under the new default it means what it always fell
    back to, which is now "no bound"."""
    raw = os.environ.get(_MAX_LATE_ENV)
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_MAX_LATE_S
    return seconds if seconds > 0 else _DEFAULT_MAX_LATE_S


def _entry_bound(entry: dict) -> int | None:
    """How late THIS entry may be and still be sent, or None for no bound.

    Per-entry, because the store is a JSON file a human may edit and because an
    occurrence written by an older version carries its own `max_late` (the
    120-second skip-not-catch-up bound that coalescing replaced). Read
    defensively for the same reason: a `max_late` that came back as a string, a
    bool, or a negative must not decide whether a message is sent — it falls
    back to the global answer."""
    bound = entry.get("max_late")
    if isinstance(bound, (int, float)) and not isinstance(bound, bool) and bound > 0:
        return int(bound)
    return max_late_seconds()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _emit(kind: str, entry: dict, detail: str = "") -> None:
    """Append one event for the shell to narrate.

    Ordering is by the monotonic `id`, not by `ts` — a poller tracks a
    high-water mark against it, and wall-clock is only there to be shown.
    Called OUTSIDE `_lock` by every caller: it takes its own (short, in-memory)
    lock, and keeping the two un-nested means neither can ever wait on the
    other."""
    global _event_seq
    with _events_lock:
        _event_seq += 1
        _events.append({
            "id": _event_seq,
            "kind": kind,
            "entry_id": str(entry.get("id") or ""),
            "target": str(entry.get("target") or ""),
            # The prompt, not a summary: a toast saying "a scheduled message
            # failed" sends the user hunting, and the first words of what they
            # asked for are what identifies it to them.
            "message": str(entry.get("message") or "")[:200],
            "detail": detail,
            # Whether this was a task somebody RAN (New task with the when-row
            # untouched, a new app's scaffolding task) rather than one they
            # scheduled. The shell's toast reads it: "Scheduled message ran"
            # is a lie about a task that ran because the user just clicked.
            "immediate": _flag(entry.get("immediate")),
            "ts": time.time(),
        })
        del _events[:-_EVENTS_MAX]


def event_log() -> list[dict]:
    """Every event still in the ring, oldest first — regardless of delivery.
    For tests and debugging; the shell reads `undelivered_events`."""
    with _events_lock:
        return list(_events)


def undelivered_events() -> list[dict]:
    """Events no client has confirmed narrating yet, oldest first.

    A plain read: draining is `ack_events`, so a page that merely LOOKS at this
    (a duplicate poll, a second window, a speculative fetch) cannot cost the user
    a notification."""
    with _events_lock:
        return [e for e in _events if e["id"] > _delivered]


def ack_events(event_id: int) -> int:
    """Confirm every event up to `event_id` has been narrated; returns the mark.

    Only ever moves FORWARD, so an out-of-order or replayed ack cannot re-arm
    events that were already shown. The client acks AFTER narrating, which means
    a client that dies in between sees them once more on its next poll — a
    duplicate toast rather than a silent miss, which is the right way round for a
    feature whose whole job is telling you what you did not see."""
    global _delivered
    with _events_lock:
        if isinstance(event_id, int) and event_id > _delivered:
            _delivered = event_id
        return _delivered


def _job_id(entry_id: str) -> str:
    return _JOB_PREFIX + entry_id


def _report(entry_id: str, **fields) -> dict | None:
    """One progress tick against this entry's job row; returns the record.

    Best-effort, like every reporter in this app: a registry that refuses a
    field must not cost a scheduled message its send. The RETURN is load-bearing
    though — it is how the watcher learns the manager's ✕ was pressed, so a
    plain `_report(id)` with no fields is a legitimate "read it back" call.

    `tier=jobs.TRANSIENT` on every call, not just the terminal one: nobody
    asked for a scheduled run's own row and a send that worked leaves nothing
    behind to open, so it is never kept once terminal (SPEC
    actionable-notifications). `**fields` comes after it so a future caller
    could still override per-call, but no caller here does.

    `origin="Scheduler"` on every call for the identical reason `tier` is: a
    scheduled entry fires with no page open at all, so "Scheduler" is this
    row's one true source regardless of which entry or which tick."""
    try:
        from fused_render import jobs

        return jobs.upsert(
            {"id": _job_id(entry_id), "tier": jobs.TRANSIENT,
             "origin": "Scheduler", **fields}, server=True
        )
    except Exception:  # noqa: BLE001 — reporting is never authoritative
        logger.debug("could not report scheduled-message job state", exc_info=True)
        return None


def _job_title(entry: dict) -> str:
    """The row's label: the prompt's first line, which is what the user typed
    and therefore what they will recognise in a column of unrelated work."""
    first = str(entry.get("message") or "").strip().splitlines()
    return (first[0] if first else "Scheduled message")[:100]


def parse_due(value) -> datetime:
    """An ISO 8601 instant as an aware UTC datetime.

    A naive string is read as LOCAL time, not UTC: it came from a human (or a
    date input) who wrote the time on their own clock, and reading "09:00" as
    UTC would fire it at the wrong hour for everyone not on UTC. Raises
    ValueError with a usable message — the router turns that into a 400."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("due: expected an ISO 8601 timestamp")
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"due: not an ISO 8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive -> this machine's zone
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------- the store


def _read() -> list[dict]:
    """The stored entries, in order. A missing or corrupt store reads as empty —
    same posture as every other registry here (bookmarks, recents): the
    schedule degrades to "nothing scheduled", it never raises into a listing."""
    data = storage.read_json(store_path())
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def _write(entries: list[dict]) -> None:
    storage.write_json(store_path(), {"entries": entries})


def _text(value) -> str:
    """One free-text field off an entry (or off a request body), as a string.

    Anything that is not a string reads as empty rather than raising. The store
    is a JSON file a human may edit and the router hands this module a raw dict,
    so a `title` that came back as a number, a list, or null must cost the entry
    nothing — an empty title falls through to the next branch of the title
    precedence (`ai-title`, then the message's first line), which is exactly the
    behaviour of not setting one."""
    return value.strip() if isinstance(value, str) else ""


def _flag(value) -> bool:
    """One boolean field, read the same way and for the same reason.

    `bool(value)` is deliberately NOT what this does: the strings a hand-edited
    store or a sloppy client can carry ("false", "no", "0") are all truthy, and
    silently reading "false" as True would flip a schedule's threading model
    without anybody asking. Only a real `true` means true; everything else is
    the default."""
    return value is True


def _pending_due(entries: list[dict]) -> list[str]:
    return [str(e.get("due") or "") for e in entries if e.get("state") == PENDING]


def _watching(entry_id: str, on: bool) -> None:
    """Mark (or unmark) an entry as having a live watcher in THIS process."""
    with _watched_lock:
        if on:
            _watched.add(entry_id)
        else:
            _watched.discard(entry_id)


def _is_watched(entry_id: str) -> bool:
    with _watched_lock:
        return entry_id in _watched


def _sync_wake() -> None:
    """Tell the OS-side wake stub which times still matter.

    **Called OUTSIDE `_lock`, always.** On macOS this shells out to `launchctl`
    twice, and holding the store lock across two subprocesses would make every
    tick able to stall a `GET /api/schedule` for as long as launchd takes to
    answer.

    **Reads the pending times itself**, under `_wake_lock`, rather than taking a
    snapshot from the caller. Snapshotting was the first shape and it lost writes:
    each caller sampled the times inside its own `_lock` block and synced after
    releasing, so two mutations racing could reach `launchctl` in the opposite
    order and the OLDER snapshot would overwrite the plist — dropping the newer
    message's time, with nothing to resync until the next store mutation happened
    to come along. A message scheduled in that window simply missed its wake. One
    lock serialises the launchctl pair, and re-reading inside it means whoever
    writes the plist last also read the store last.

    Lock order is `_wake_lock` then `_lock`, and nothing may take `_wake_lock`
    while holding `_lock` — the reverse pairing deadlocks, which is what the
    "outside `_lock`, always" rule above is really protecting.

    Best-effort by construction: the wake stub only makes the app more likely to
    be RUNNING at a due time, and everything about firing works without it. A
    platform that has no stub, or a dev checkout with no app bundle to relaunch,
    is a no-op — never an error that fails the write that got here."""
    try:
        from fused_render import schedule_wake

        with _wake_lock:
            with _lock:
                due = _pending_due(_read())
            schedule_wake.sync(due)
    except Exception:  # noqa: BLE001 — a wake stub must never break the schedule
        logger.debug("could not sync the schedule wake stub", exc_info=True)


def list_entries() -> list[dict]:
    """Every entry, live ones first, each group ordered by what the reader wants
    from it. What the UI lists; no side effects.

    The two groups run in OPPOSITE directions, because "most relevant first" means
    opposite things about the future and the past:

    * **live** (`pending`/`sending`) ascending — soonest first, so the next thing
      that will happen is at the top;
    * **handled** (everything terminal) DESCENDING — most recent first, so the
      latest news is at the top. Ascending here was a straight bug: it buried
      what just ran under every message ever scheduled, and grew worse the longer
      the feature was used.

    A handled entry sorts on when it ACTED (`fired`), falling back to its due time
    for one that never did — `missed` and `cancelled` have no fired stamp. That is
    also the stamp the row shows, so the order matches what the reader is reading.
    """
    live, handled = [], []
    for entry in _read():
        bucket = (live if entry.get("state") in (PENDING, SENDING, RECURRING)
                  else handled)
        bucket.append(entry)
    live.sort(key=lambda e: str(e.get("due") or ""))
    handled.sort(key=lambda e: str(e.get("fired") or e.get("due") or ""),
                 reverse=True)
    return live + handled


def _local_naive(when: datetime) -> datetime:
    """An aware instant as the naive local wall-clock time cron math wants."""
    return when.astimezone().replace(tzinfo=None)


def _from_local(when: datetime) -> datetime:
    """A naive local wall-clock time (cron output) back to an aware UTC instant."""
    return when.astimezone().astimezone(timezone.utc)


def create(target: str, message: str, due=None, session_id: str = "",
           permission_mode: str = "", repeats: str = "",
           rule: dict | None = None, title=None, description=None,
           new_task_each_run=None, session_learned=None,
           immediate=None, images=None, attachments=None,
           create_target: bool = False,
           model: str = "", effort: str = "", priority=None,
           follow_of: str = "", origin: str = "") -> dict:
    """Validate and store one scheduled message; return the stored entry.

    `title` and `description` are the user's own words about the work, both
    optional and both stored as given. An empty `title` is not a missing value
    to be filled in here — it is the first branch of a precedence the tasks
    endpoint owns (the user's title, else Claude Code's `ai-title` off the
    transcript, else the message's first line), and guessing one at creation
    would pin the row to whatever the message happened to open with.

    The three of them are UNTYPED on purpose: the router hands this module the
    request body's values as they arrived, and the form omits a field rather
    than sending a blank one, so "absent", "null" and "" all have to mean the
    same thing. `_text` and `_flag` are where that happens.

    `new_task_each_run` only means anything on a repeating message, and it names
    the threading model: a task IS a Claude session, so by default every run of
    a repeating message appends to the same thread (the occurrence inherits the
    template's `session_id`). Ticked, each run starts a fresh session instead.
    See `_materialize`, which is where the one-line difference lives.

    A repeating message created WITHOUT a `session_id` — which is every one the
    Tasks page makes, and every one handed off from a chat, that form dropping
    the open conversation rather than letting a repeat compound it for ever —
    still chains: its first run opens a thread and `_chain_session` records
    which, so runs 2..N continue it.

    `session_learned` is the PROVENANCE of `session_id`, and this function
    never invents it. Two very different things put an id on an entry — a chat
    handoff (the conversation the user was in) and a thread the template LEARNED
    on its first run — and an edit, which is cancel + re-create, has to treat
    them oppositely: a repeat must refuse the former and keep the latter. Only
    `_chain_session` mints the marker; `create` accepts it so that an edit
    re-stating a learned id can say which kind it is, because the alternative
    was the form INFERRING it from whether the entry repeated, and that could
    not survive a task being demoted to a one-off and promoted back.

    `follow_of` is the same link one step down: the entry this message was
    typed BEHIND, for the one case where a chat has no session to name because
    its own first message is still queued. It must name an entry that exists
    (400 otherwise), and everything downstream — the Tasks row it is filed
    under, its place in the line, the session it resumes when it finally goes —
    reads the leader's answer through `leader_of`.

    `origin` is WHO ASKED FOR THIS MESSAGE, and it is "" for everything the
    calendar and the Tasks page create — which is what makes it readable the
    other way round: an entry with no origin is a message somebody SCHEDULED,
    and a chat with one of those aimed at it still shuts its composer until it
    goes, exactly as it did before the queue existed (Akshil, 2026-09-12).
    `"chat"` is written by the admission endpoint and by nothing else: a
    message the chat itself queued is that conversation's own next line, so the
    composer stays open and the waiting bubble says so under itself instead.

    Stored ONLY when non-empty, so an ordinary spawn's entry is main's field for
    field, and never invented downstream: `_materialize` mints an occurrence
    without one (a repeat is a schedule, whoever first typed it) and `restore`
    leaves alone what is there. `resend` is the exception that proves the rule —
    it copies the original's, because asking a chat's queued message again is
    still the chat's message.

    With `repeats` (a 5-field cron expression) the stored entry is a RECURRING
    template instead: `due` is ignored — the cron line already says every time
    it means — and the first occurrence is materialized immediately, so the
    wake stub knows about it before this returns.

    With `rule` (a structured repeat, see recur.py) the entry is also a
    RECURRING template, and the one difference from the cron case is that `due`
    is REQUIRED rather than ignored: a rule counts from an anchor, and the
    anchor is that first run. It is kept in its own field because `due` is
    rewritten on every materialization to mirror the next occurrence, and a
    series numbered from a moving anchor would renumber itself every tick.

    `create_target` opts the caller into ONE new folder: a target whose last
    segment does not exist yet is made here, provided its parent already does.
    The New task form offers this (it shows the path as a new folder while you
    type it), so the endpoint behind that form passes it; every other caller
    leaves it off and keeps the plain "no such file or directory" refusal. It is
    a flag rather than the default precisely because of the re-send path
    (`resend` below), where a target that has since been deleted is a fact the
    user needs told — silently re-making a deleted FILE's name as a directory
    would be the worst possible answer.

    Exactly one level, never `-p`: two missing segments means the user is not
    naming a new folder in a place they know, they are typing into a tree that
    is not there, and inventing both is how a typo becomes a real directory.

    The folder is CHECKED where the target is resolved and MADE at the very
    bottom, immediately before the entry is stored — so a request refused by a
    later validation (a cron line, a due date, a permission mode) leaves nothing
    on disk. Ordering rather than a rollback: an unlink after the fact could
    remove a directory something else had already raced into.

    Raises ValueError for everything a caller can get wrong (the router maps it
    to a 400). The one validation deliberately NOT here is "is this path
    mount-backed" — that needs the mounts registry, which lives above this
    module; the router refuses those before calling, exactly as the claude
    template's own gate does."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message: cannot be empty")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target: required")
    # BOTH FIELDS, either one sufficient. `images` is the flat path list every
    # entry on disk carries; `attachments` is the same paths with the filename
    # and the kind the browser knew, which is what the fired run's `<pane-shot>`
    # block needs to draw a receipt row rather than a raw path. Each is derived
    # from the other when only one arrived — see `_attachments`.
    images = _images(images)
    attachments = _attachments(attachments, images)
    if not images:
        images = [a["path"] for a in attachments]
    target = os.path.abspath(os.path.expanduser(target))
    # The new folder is only CHECKED here; it is made at the very bottom, right
    # before the entry is stored. Everything between this point and there can
    # still refuse the request (a cron line that will not parse, an unreadable
    # due date, an unknown permission mode), and a directory made up here would
    # outlive that refusal — the user gets a 400 and an empty folder they never
    # asked for. Deciding now and acting last keeps the create path all-or-
    # nothing without a rollback that could delete someone else's work.
    make_target = False
    if not os.path.exists(target):
        if not create_target:
            raise ValueError(f"target: no such file or directory: {target}")
        parent = os.path.dirname(target)
        # abspath has already collapsed "." and "..", so a basename of either is
        # only reachable at the filesystem root — where there is nothing to make.
        if not os.path.basename(target) or os.path.basename(target) in (".", ".."):
            raise ValueError(f"target: no such file or directory: {target}")
        if not os.path.isdir(parent):
            raise ValueError(
                f"target: only one new folder can be created, and {parent} "
                "does not exist either")
        make_target = True

    repeats = (repeats or "").strip()
    if rule is not None and repeats:
        raise ValueError("rule: cannot be combined with `repeats` — a message "
                         "repeats one way or the other, not both")
    spec = None
    if rule is not None:
        # Validated here as well as in the router, because the router is not the
        # only caller and a rule that reaches the store unreadable becomes a
        # template that stops firing with nobody to tell.
        spec = recur.validate_rule(rule)
        if due is None:
            raise ValueError("rule: needs `due` — the date and time of the "
                             "first run, which is what the repeat counts from")
        when = due if isinstance(due, datetime) else parse_due(due)
        if when.tzinfo is None:
            when = when.astimezone()
        when = when.astimezone(timezone.utc)
        # NO catch-up-bound refusal here, unlike a one-shot below. An anchor in
        # the past is a perfectly ordinary way to say "every other Monday, on
        # the phase that started last Monday" — the anchor sets the pattern, the
        # LATEST slot at or before now is materialized once as a catch-up (see
        # `_catch_up_base`), and the series then continues from now. A past
        # anchor sets the phase AND runs, exactly the way a past one-shot does.
    elif repeats:
        # Parse errors surface here, at creation, with the field named —
        # never later in the loop against a stored line nobody can see.
        line = cron.parse(repeats)
        when = _from_local(line.next_after(_local_naive(_now())))
    else:
        when = due if isinstance(due, datetime) else parse_due(due)
        if when.tzinfo is None:
            when = when.astimezone()
        when = when.astimezone(timezone.utc)
        # A DUE TIME IN THE PAST IS ACCEPTED, and stored as the time the user
        # picked. This used to be refused past the catch-up bound, and the
        # refusal was right for as long as the bound was: an entry that would be
        # swept to `missed` on the very next tick is better refused than accepted
        # and silently dropped.
        #
        # With catch-up unbounded there is nothing to refuse it FOR. Picking a
        # date days back on the calendar now means "run this, and file it under
        # then" — the due time is recorded as given so history reads truthfully,
        # and because the queue runs in due order (`_claim_due`) a past due time
        # sorts ahead of everything later, which puts it at the head of the queue
        # and sends it on the next tick.
        #
        # An operator who has set FUSED_RENDER_SCHEDULE_MAX_LATE is the one case
        # where this can still be stored only to expire. It is still accepted:
        # the bound is theirs, the sweep applies it, and `missed` says plainly
        # what happened — where a refusal here would report a policy the caller
        # did not set as if the date were malformed.

    mode = permission_mode or _SCHEDULED_PERMISSION_MODE
    if mode not in PERMISSION_MODES:
        raise ValueError(f"permission_mode: expected one of {PERMISSION_MODES}")

    # THE MESSAGE THIS ONE WAS TYPED BEHIND, and it has to name a real one. A
    # follower borrows its leader's task key and its leader's session, so an id
    # that names nothing would be a message filed under a row that does not
    # exist — refused here, with the field named, exactly like every other thing
    # a caller can get wrong. The check is a moment old by the time the entry is
    # stored (the leader can be cancelled in between) and that is fine: an
    # erased leader is a case every reader already handles by leaving the
    # follower to stand alone.
    follow_of = str(follow_of or "").strip()
    if follow_of:
        with _lock:
            known = any(str(e.get("id") or "") == follow_of for e in _read())
        if not known:
            raise ValueError(
                f"follow_of: no scheduled message with id {follow_of!r}")

    # No vocabulary check: the one word anything writes is "chat" (the chat
    # admission), and a store that refused an unknown one would be this layer
    # holding a list of its callers. Normalised like every other string here so
    # "absent", None and "  " are the one thing they mean.
    origin = str(origin or "").strip()

    entry = {
        # Due-time-ordered id: the store is a list a human may well read, and an
        # id that sorts the way the schedule does is worth more here than an
        # opaque uuid. Same shape agent.py uses for a run id — a timestamp plus
        # three random bytes, so two messages due the same second still differ.
        "id": when.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex(),
        "target": target,
        "message": message,
        "due": when.isoformat(),
        "session_id": session_id or "",
        # WHERE that id came from, and False unless the caller says otherwise —
        # a chat handoff is the ordinary case, and only `_chain_session` ever
        # mints this. `and` the id itself so the marker can never outlive it: a
        # provenance for nothing is a claim the form would have to second-guess.
        "session_learned": _flag(session_learned) and bool(session_id or ""),
        "permission_mode": mode,
        # WHICH Claude runs the turn, and how hard: handed to `spawn_helper` by
        # `_send` exactly as the direct spawn used to hand them. "" is "no flag"
        # — the session detects its own defaults — and stays the answer for
        # every creator with no opinion.
        #
        # TWO creators now supply them. The new-app composer's pickers
        # (routers/apps.py) always did; the Tasks form's More options row does
        # as of 2026-09-03, which also closes the hole the old note here
        # described: editing a task is cancel + re-create, so a form that could
        # not re-state these silently reset them to the defaults on every edit.
        # The form prefills both off the entry and sends them back, so an edit
        # now carries the choice across instead of quietly dropping it.
        "model": str(model or ""),
        "effort": str(effort or ""),
        # The user's own words, both optional and both "" by default. Read
        # through `_text` because the router hands this module the request body
        # unvalidated, exactly as it does for every other field here.
        "title": _text(title),
        "description": _text(description),
        # Task-shot paths, already validated into shots_dir() residents by
        # `_images` above. Stored on the entry (and copied onto every
        # occurrence) so the send path and an edit both read them off the row.
        "images": images,
        # The same attachments, with the two facts a path does not carry: the
        # NAME the user recognises (a stored path is a minted timestamp) and the
        # KIND the browser settled (a `.tif` the upload endpoint transcoded is a
        # picture whose extension says otherwise). Read by
        # `_attachments_block` to write the claude page's own `<pane-shot>`
        # block, which is what makes the fired turn render receipt rows in the
        # chat instead of a list of paths (D619).
        "attachments": attachments,
        # Threading, not scheduling: whether each run of a REPEAT opens its own
        # session. Stored on one-shots too (as False) so every entry has the
        # same shape and the form reads it back the same way — a one-shot has
        # one run, so there is nothing for it to mean there.
        "new_task_each_run": _flag(new_task_each_run),
        # "RUN THIS NOW", not "run this at a time I chose". The New task form
        # sets it when the card was opened from the List or the Board and the
        # user never touched the when-row: the message is due now because now is
        # what the form defaults to, not because anybody planned it for now.
        #
        # It changes nothing about WHEN this runs — the scheduler still reads
        # `due` and only `due`. It exists so the CALENDAR can tell the two apart:
        # a grid of everything anyone ever typed into the Tasks page is not a
        # plan, and a task nobody scheduled has no business drawing a chip on it
        # (Akshil, 2026-08-23). See `_scheduled_message` in the tasks router,
        # which carries it onto the message the calendar reads.
        #
        # Only ever true on a one-off: touching the repeat tick IS choosing a
        # time, so the form never sends it alongside a rule.
        "immediate": _flag(immediate) and not (repeats or spec is not None),
        # SKIP THE QUEUE — the only field the project queue adds to an entry,
        # and the only part of that feature that cannot be derived from the
        # disk (project_queue.py's docstring says why everything else is).
        # False for every entry anybody creates: priority is a thing the user
        # asks for on work that is already waiting, never a property a new
        # message is born with. `set_priority` is what turns it on.
        #
        # Stored flag-agnostically, like every other field here. The flag
        # decides whether the ORDER reads it (`_queue_order`, `_claim_due`);
        # storing it either way means flipping the pref does not have to
        # migrate a store, and an entry skipped with the flag on keeps its
        # place in line if it is flipped off and back.
        "priority": _flag(priority),
        # WHEN RUN NOW WAS PRESSED ON THIS MESSAGE — "" until it is, and never
        # set by anybody creating one.
        #
        # `due` is a fact about the ASK and never moves (`run_now`'s docstring
        # says why: a message that ran early is a message that ran early, and
        # the calendar draws the chip on the day the user picked). But Run now
        # on a message due TOMORROW, in a folder somebody else is holding, has
        # to leave something behind that says the user asked for it now —
        # otherwise the entry is skipped to the head of a line it is not even
        # in, every reader still sees `upcoming`, and the scheduler waits for
        # tomorrow (browser QA, 2026-09-12).
        #
        # So: a second stamp beside `due`, never instead of it. Everything that
        # asks "is this waiting to go RIGHT NOW" reads the earlier of the two
        # (`_queue_due`, `project_queue.order_key`, `tasks._queue_at`);
        # everything that asks "when was this scheduled for" — the calendar,
        # `_next_run`, the row's own time — reads `due` and is untouched.
        #
        # Only ever WRITTEN under the flag (run-now's queued arm is the only
        # thing that produces one), stored flag-agnostically like `priority`
        # above, and NEVER inherited: a restore clears it and a materialized
        # occurrence is born without one.
        "run_now_at": "",
        # THE ENTRY THIS MESSAGE WAS TYPED BEHIND — "" for all but one shape.
        # A chat with no session yet whose first message was queued IS a task
        # (`pending:<entry-id>`), and the second message typed into it has
        # nothing else to name: no session exists to continue, and a fresh entry
        # of its own would fork a second row beside the very chat it was typed
        # into (browser QA, 2026-09-12). Naming the leader is what keeps the two
        # messages one task — the one-off twin of `template_id` +
        # `_chain_session`, which is how a repeat's runs 2..N find their thread.
        #
        # Stored flag-agnostically like `priority` above, and only ever WRITTEN
        # under the flag: the client passes it from the admission answer, and
        # admission is the only thing that produces a queued first message. So
        # with the flag off the field is absent from everything in the store and
        # every reader of it is a no-op.
        "follow_of": follow_of,
        "state": RECURRING if (repeats or spec is not None) else PENDING,
        # "" on a one-shot; the cron line on a template. An OCCURRENCE never
        # carries it — the link runs the other way, through `template_id`.
        "repeats": repeats,
        # None on a one-shot and on a cron template; the structured repeat on a
        # rule template. Carried beside `repeats` rather than instead of it so
        # an existing store keeps reading exactly as it did.
        "rule": spec,
        "created": _now().isoformat(),
        "fired": "",
        "run_id": "",
        "error": "",
        # `state` says whether the message was SENT; `turn` says how the session
        # it started then went — "" until the turn ends, else ok/failed/cancelled.
        # Two fields because they fail independently and the difference matters:
        # a message can send perfectly and its turn still die on the first tool
        # call, and reporting that as a send failure would send the user looking
        # in the wrong place.
        "turn": "",
        # WHEN the turn's verdict landed — stamped by the same write that fills
        # `turn` in, "" until then. The Tasks page compares it against the
        # transcript's tail: activity meaningfully after this moment is new
        # work, not the verdict's own closing echo.
        "turn_at": "",
        # The Claude Code session this message's turn actually ran in, filled in by
        # the watcher from the run's first reporting tick. Distinct from
        # `session_id` above (the input) precisely so a fresh send does not end up
        # looking like a continuation, and it is what the page links to the Inbox
        # with — that app addresses a session by this id and nothing else.
        "claude_session_id": "",
    }
    # WRITTEN ONLY WHEN THERE IS ONE, unlike every field above it. The absence
    # is the meaning — "nobody's chat queued this, somebody scheduled it" — and
    # a stored `"origin": ""` would be the same sentence said in a way that
    # changes the bytes of every entry main writes. Same rule as `host_sent`.
    if origin:
        entry["origin"] = origin
    if spec is not None:
        # The first run of the series, kept where materialization cannot move
        # it — see the docstring. Every occurrence is "the anchor plus k steps",
        # so this is the field the whole schedule hangs off.
        entry["anchor"] = when.isoformat()
        # How many occurrences this template has MATERIALIZED, which is what
        # `count` is measured against. Skipped ones count: "ends after 13
        # occurrences" is a promise about the runs the schedule puts on the
        # calendar, and deciding to skip one is a decision about a run that was
        # scheduled. Counting only the ones that fired would quietly extend the
        # series every time the app was closed at the wrong moment.
        entry["made"] = 0
    if make_target:
        # LAST, after every validation above has had its chance to refuse: from
        # here on the only thing left is writing the entry, so the folder and
        # the task appear together or neither does.
        try:
            # mkdir, not makedirs: the one-level rule is enforced by the call
            # itself, so a parent that vanished since the check above raises
            # rather than being invented.
            os.mkdir(target)
        except FileExistsError:
            # Someone else made it in the meantime, which is the outcome asked
            # for. Only a non-directory is a problem, and os.path.exists above
            # would not have missed one that was already there.
            if not os.path.isdir(target):
                raise ValueError(
                    f"target: {target} exists and is not a folder") from None
        except OSError as exc:
            raise ValueError(f"target: could not create {target}: {exc}") from exc
    with _lock:
        entries = _read()
        entries.append(entry)
        _write(entries)
    _sync_wake()
    if repeats or spec is not None:
        # First occurrence, immediately — so the schedule the user just wrote
        # is visible (and wake-synced) without waiting for the next tick.
        _materialize(_now())
    # AFTER materialization, so a repeat anchored in the past rings for the
    # catch-up occurrence that pass just created rather than for the template
    # (which never fires). Reads the store rather than testing `entry`, for the
    # same reason. A future-dated message rings nothing and waits for its time.
    _ring()
    return entry


def cancel(entry_id: str) -> dict | None:
    """Cancel a pending entry or a recurring template; return it, or None if
    there is nothing cancellable under that id. A `sending` entry is
    deliberately NOT cancellable — the helper is already away and the turn may
    have started, so "cancelled" would be a claim this module cannot make good
    on.

    Cancelling a TEMPLATE also cancels its pending occurrence: "stop this
    recurring job" means no further runs, and the materialized next run is a
    further run. Cancelling just the OCCURRENCE is also allowed and means the
    opposite — skip this one, keep the schedule (the next materialization pass
    picks up from the skipped time)."""
    cancelled = None
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") != entry_id:
                continue
            if entry.get("state") == PENDING:
                entry["state"] = CANCELLED
            elif entry.get("state") == RECURRING:
                entry["state"] = CANCELLED
                for occurrence in entries:
                    if (str(occurrence.get("template_id") or "") == entry_id
                            and occurrence.get("state") == PENDING):
                        occurrence["state"] = CANCELLED
            else:
                return None
            _write(entries)
            cancelled = entry
            break
    if cancelled is None:
        return None
    _sync_wake()
    return cancelled


def restore(entry_id: str) -> dict | None:
    """Un-skip a skipped occurrence: `cancelled` -> `pending`, if its time has
    not passed. Returns the restored entry, or None when there is nothing
    restorable under that id.

    Only OCCURRENCES restore, and only under a template that is still
    recurring — restoring a one-shot the user cancelled outright would be an
    undo feature, which this deliberately is not; a skip is the one cancel
    that names an exception to a rule that is still standing, so it is the one
    worth walking back. The materializer may have already created the NEXT
    occurrence, so a restore can briefly leave two pending under one template;
    the firing loop handles each at its own time, which is exactly what
    "unskip" means."""
    restored = None
    with _lock:
        entries = _read()
        templates = {str(e.get("id")): e for e in entries
                     if e.get("state") == RECURRING}
        for entry in entries:
            if entry.get("id") != entry_id:
                continue
            if entry.get("state") != CANCELLED:
                return None
            if str(entry.get("template_id") or "") not in templates:
                return None
            try:
                when = parse_due(entry.get("due"))
            except ValueError:
                return None
            if when <= _now():
                return None
            entry["state"] = PENDING
            entry["error"] = ""
            # NEVER INHERITED, same rule as `priority` on a materialized
            # occurrence: "run it now" was said about the run that was then
            # skipped, and a restored occurrence that came back already at the
            # head of its folder's line is not what anybody asked for.
            entry["run_now_at"] = ""
            _write(entries)
            restored = entry
            break
    if restored is None:
        return None
    _sync_wake()
    return restored


# ---------------------------------------------------------------- the queue
#
# With catch-up unbounded, opening the app after a week away can find real work
# waiting — which is only safe if the user can SEE it and stop it. These two
# functions are that surface; the shell raises one popover from them on open
# (never one per message) with cancel-each and cancel-all.


def _queue_order(entries: list[dict], now: datetime) -> list[tuple[datetime, str, dict]]:
    """Past-due pending entries, in the order `_claim_due` will send them.

    Deliberately the same key — due time, id breaking ties — because a queue
    listed in one order and run in another is worse than no queue at all. That
    is also why the project-queue flag is read HERE and not only in the sweep:
    with it on both sort by `project_queue.order_key`, so a skipped entry shows
    at the head of the list it will actually be sent from.

    Entries past an explicit bound are left out: they are not waiting to run,
    they are waiting to be swept to `missed`."""
    queued: list[tuple[datetime, str, dict]] = []
    for entry in entries:
        if entry.get("state") != PENDING:
            continue
        try:
            when = parse_due(entry.get("due"))
        except ValueError:
            continue
        when = _queue_due(entry, when)
        if when > now:
            continue
        bound = _entry_bound(entry)
        if bound is not None and when < now - timedelta(seconds=bound):
            continue
        queued.append((when, str(entry.get("id") or ""), entry))
    if _pq().enabled():
        # The whole store, keyed by id, so `order_key` can put a follower behind
        # the message it was typed behind. Built once per call rather than
        # looked up per entry: a follower's leader need not be past due (it can
        # be the one already claimed), so the map has to be wider than the list
        # being sorted.
        by_id = {str(e.get("id") or ""): e for e in entries}
        queued.sort(key=lambda item: _pq().order_key(item[2], by_id))
    else:
        queued.sort(key=lambda item: (item[0], item[1]))
    return queued


def queue(now: datetime | None = None) -> dict:
    """What is waiting and what is in flight: `{"queued": [...], "running": [...]}`.

    * **queued** — past-due `pending` entries, in run order. Not "everything
      scheduled": a message due tomorrow is not queued, it is scheduled, and
      showing it here would make the cancel-all button mean something the user
      did not ask for.
    * **running** — entries in `sending`, i.e. claimed but not yet spawned.
      Narrow on purpose. A `sent` entry with a live turn is running too, but it
      has its own cancel (the job registry's ✕, which really does stop the run)
      and it is past the point this surface can withdraw it.

    A plain read, like `list_entries`: no materialize, no coalesce, no claim.
    The tick owns every state change, and a GET that mutated the store would
    make merely LOOKING at the queue change what runs."""
    now = now or _now()
    with _lock:
        entries = _read()
    running = [e for e in entries if e.get("state") == SENDING]
    running.sort(key=lambda e: str(e.get("fired") or e.get("due") or ""))
    return {"queued": [entry for _, _, entry in _queue_order(entries, now)],
            "running": running}


def cancel_queued(entry_ids=None, all_queued: bool = False,
                  now: datetime | None = None) -> dict:
    """Drop queued messages: `{"cancelled": [id...], "refused": [id...],
    "reasons": {id: why}}`.

    **The claim race is the whole design problem here**, and the answer is to
    refuse rather than to force. The tick claims an entry (`pending` ->
    `sending`) immediately before spawning its helper, so between the moment the
    user reads the queue and the moment they press Cancel, an entry can be away.
    Cancelling it then would be a claim this module cannot make good on — the
    process is launched, the turn may have started — and writing `cancelled`
    over `sending` would additionally destroy the record the stuck sweep needs
    to report an interrupted send.

    So the transition allowed is exactly `pending` -> `cancelled`, decided on a
    fresh read under `_lock` — the same lock and the same re-read `_claim` uses.
    One of the two wins and the other sees the loser's state: cancel first and
    the claim returns None (the tick skips it), claim first and cancel refuses
    it as already running. Neither can leave a half-cancelled entry.

    `all_queued` means the entries `queue()` would list right now, recomputed
    under the lock rather than trusted from the client — "cancel all" must mean
    the queue as it is, not the queue as the page last drew it, or a message
    that came due in between would be cancelled without ever being shown.

    Cancelling a recurring OCCURRENCE means what it means everywhere else in
    this module: skip this one, keep the schedule."""
    now = now or _now()
    cancelled: list[str] = []
    refused: list[str] = []
    reasons: dict[str, str] = {}
    with _lock:
        entries = _read()
        by_id = {str(e.get("id") or ""): e for e in entries}
        if all_queued:
            targets = [entry_id for _, entry_id, _ in _queue_order(entries, now)]
        else:
            targets = [str(i) for i in (entry_ids or []) if isinstance(i, str)]
        changed = False
        for entry_id in targets:
            entry = by_id.get(entry_id)
            if entry is None:
                refused.append(entry_id)
                reasons[entry_id] = "no scheduled message with that id"
                continue
            state = entry.get("state")
            if state == PENDING:
                entry["state"] = CANCELLED
                cancelled.append(entry_id)
                changed = True
            elif state == SENDING or (state == SENT and not entry.get("turn")):
                refused.append(entry_id)
                reasons[entry_id] = ("already running — it was claimed for "
                                     "sending before this cancel arrived")
            else:
                refused.append(entry_id)
                reasons[entry_id] = f"already {state}"
        if changed:
            _write(entries)
    if changed:
        _sync_wake()
    return {"cancelled": cancelled, "refused": refused, "reasons": reasons}


def set_priority(entry_ids: list[str], value: bool) -> dict:
    """Skip the queue (or un-skip it): `{"updated": [id...], "refused": [id...]}`.

    **Only a PENDING entry can be skipped**, and every other state is refused
    rather than silently ignored. That is the same line `cancel_queued` draws
    and for the same reason: an entry the tick has already claimed is away, and
    moving it up the line it has left would be a promise about a message that
    is not in the line any more. A `sent`, `cancelled` or `missed` entry has no
    line to be at the head of at all.

    The write is flag-agnostic — the field is stored either way (see `create`)
    — and so is the ring, which is why this is safe to call with the project
    queue off: an entry marked priority that nothing reads is an entry in the
    order it was already in.

    **Rings both bells.** `wake()` because the head of a folder's line may have
    just changed and the loop is otherwise up to 30 seconds away from noticing;
    `tasks_watch.notify` because the Tasks page draws the position and a skip
    that takes a poll interval to appear reads as a button that did nothing.
    Both after the lock, never inside it.
    """
    wanted = [str(i) for i in (entry_ids or []) if isinstance(i, str) and i]
    value = value is True
    updated: list[str] = []
    refused: list[str] = []
    keys: set[str] = set()
    with _lock:
        entries = _read()
        by_id = {str(e.get("id") or ""): e for e in entries}
        changed = False
        for entry_id in wanted:
            entry = by_id.get(entry_id)
            if entry is None or entry.get("state") != PENDING:
                refused.append(entry_id)
                continue
            updated.append(entry_id)
            # `by_id` rather than a second read: a follower is filed under the
            # row its leader owns, and this is the map that answers that.
            keys.add(_task_key(entry, by_id))
            if _flag(entry.get("priority")) != value:
                entry["priority"] = value
                changed = True
        if changed:
            _write(entries)
    if updated:
        wake()
        _notify(keys)
    return {"updated": updated, "refused": refused}


def _update(entry_id: str, **fields) -> None:
    """Merge `fields` into one entry, re-reading under the lock so a concurrent
    cancel or create is not clobbered by a stale copy."""
    written = False
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") == entry_id:
                entry.update(fields)
                _write(entries)
                written = True
                break
    if written:
        _sync_wake()


# --------------------------------------------------------------- the firing


def _queue_due(entry: dict, when: datetime) -> datetime:
    """When this entry joined the LINE — `when` (its due time), or the moment
    Run now was pressed on it if that came first.

    THE ONE PLACE the two stamps are reconciled, so "due for queue purposes"
    cannot drift from "due" by accident. `due` itself is never rewritten (see
    `run_now`), so a message the user asked for now while its folder was busy
    would otherwise be invisible to every reader of the line — `_claim_due`
    would not consider it, `_queue_order` would not list it, and the row would
    read `upcoming` while the answer to the gesture said `queued`.

    An unreadable stamp is no stamp: the entry keeps its own due time, which is
    the same direction every other defensive read here takes."""
    stamp = str(entry.get("run_now_at") or "")
    if not stamp:
        return when
    try:
        asked = parse_due(stamp)
    except ValueError:
        return when
    return min(when, asked)


def _claim_due(now: datetime) -> list[dict]:
    """Move every entry that should act now out of `pending`, and return the
    ones to actually send.

    ONE locked read-modify-write for the sweep, so a tick that spawns nothing
    still persists its `missed` verdicts.

    **The due entries are returned STILL PENDING, and each is claimed
    individually right before its own spawn** (`_claim`). Claiming the whole
    batch here was the first shape and it was wrong in a way that inverts the
    point of claiming at all: `tick` spawns sequentially, so a process that died
    inside the first helper left every SIBLING persisted as `sending` with no
    spawn behind it — and the stuck sweep then reported them interrupted, so
    messages that had never been attempted were never sent. Claiming protects
    the ONE message actually in flight; anything not yet attempted must stay
    `pending` so the next tick (or the next launch) still sends it.

    The events this pass decides on are collected and emitted AFTER the lock
    (`_emit` takes its own), so the two locks are never nested."""
    due: list[tuple[datetime, str, dict]] = []
    announce: list[tuple[str, dict, str]] = []
    with _lock:
        entries = _read()
        changed = False
        for entry in entries:
            state = entry.get("state")
            if state == SENDING:
                # Left behind by a process that died between claim and spawn.
                # Reported, never retried — see the module docstring.
                fired = entry.get("fired") or ""
                try:
                    stuck_since = parse_due(fired)
                except ValueError:
                    stuck_since = None
                if stuck_since and (now - stuck_since).total_seconds() > _SENDING_STUCK_S:
                    entry["state"] = ERROR
                    entry["error"] = ("interrupted: the app stopped between "
                                      "claiming this message and sending it")
                    announce.append((EVENT_FAILED, dict(entry), entry["error"]))
                    changed = True
                continue
            if state == SENT and not entry.get("turn"):
                # Sent, with the turn still open. In a live process that is the
                # NORMAL shape of a running turn and `_watch_turn` owns it; with
                # nothing watching, the process that was watching died mid-turn and
                # nobody is ever coming back for it.
                #
                # `_close_unwatched` is the in-process floor under an ending watch
                # and cannot cover this — the whole thread went with the process —
                # so the sweep is the only place a restart can notice. Left alone
                # the entry costs the user three separate things: the page reads
                # `Running…` for ever, no toast ever says what happened, and its
                # session stays in `_busy_sessions`, so the NEXT scheduled message
                # to that conversation is held back tick after tick until the
                # catch-up bound gives up and calls it missed.
                #
                # `state` stays SENT because that is true — the message did go —
                # and `turn` becomes `unknown`, the same verdict and the same word
                # `_close_unwatched` uses for a watch that ended without one.
                if not _is_watched(str(entry.get("id") or "")):
                    entry["turn"] = "unknown"
                    entry["turn_at"] = now.isoformat()
                    entry["error"] = ("interrupted: the app stopped while this "
                                      "message's turn was running")
                    announce.append((EVENT_FAILED, dict(entry), entry["error"]))
                    changed = True
                continue
            if state != PENDING:
                continue
            try:
                when = parse_due(entry.get("due"))
            except ValueError:
                entry["state"] = ERROR
                entry["error"] = f"unreadable due time: {entry.get('due')!r}"
                announce.append((EVENT_FAILED, dict(entry), entry["error"]))
                changed = True
                continue
            when = _queue_due(entry, when)
            if when > now:
                continue
            # The bound is per-entry and USUALLY None — nothing expires, missed
            # work queues (see the module docstring). A number gets here two
            # ways: an operator's FUSED_RENDER_SCHEDULE_MAX_LATE, or an
            # occurrence written by an older version whose `max_late` field
            # survives in the store. Both mean the same thing here and are read
            # the same way, defensively, in `_entry_bound`.
            bound = _entry_bound(entry)
            if bound is not None and when < now - timedelta(seconds=bound):
                changed = True
                entry["state"] = MISSED
                entry["error"] = (
                    "skipped: the app was not running at this time "
                    "(recurring runs are never sent late)"
                    if entry.get("template_id") else
                    "not sent: the app was not running between "
                    "this time and the catch-up bound")
                announce.append((EVENT_MISSED, dict(entry), entry["error"]))
                continue
            # Due and sendable. Left PENDING — `_claim` takes it, one at a time.
            # The entry travels with its sort keys because the project queue's
            # order reads a field off it; `entries` is this call's own copy and
            # nothing writes to it after the lock, so carrying it out is safe.
            due.append((when, str(entry["id"]), entry))
        if changed:
            _write(entries)
    for kind, entry, detail in announce:
        _emit(kind, entry, detail)
    if changed:
        _sync_wake()
    # BY DUE TIME, not by store order. The store is in creation order, and the two
    # disagree the moment a catch-up pass finds several messages overdue at once:
    # something scheduled this morning for tonight would go before something
    # scheduled at lunch for 2pm. It matters most for same-session sends, where the
    # hold in `tick` turns "which goes first" into "which conversation turn happens
    # first", but a batch firing in the order the user asked for is the right
    # behaviour for all of them. Ties break on the id, itself due-time-derived.
    #
    # With the project queue on, `order_key` puts a skipped entry in front of
    # that — the one thing a user can say about the order, and the only reason
    # this sort is not simply the tuple above.
    if _pq().enabled():
        # …and `order_key` reads a follower's LEADER off this map, so a message
        # typed into a queued chat can never be sent before the message it was
        # typed behind. `entries` is this call's own copy, as above.
        by_id = {str(e.get("id") or ""): e for e in entries}
        due.sort(key=lambda item: _pq().order_key(item[2], by_id))
    else:
        due.sort(key=lambda item: (item[0], item[1]))
    return [entry_id for _, entry_id, _ in due]


def _claim(entry_id: str, now: datetime, session_id: str = "") -> dict | None:
    """Take ONE entry for sending: `pending` -> `sending`, written before the
    caller spawns anything. Returns the claimed copy, or None if it is no longer
    pending (cancelled between the sweep and here, or already taken).

    The re-read under the lock is what makes that None real rather than
    theoretical: the sweep's verdict is a moment old by the time we get here, and
    a cancel landing in that window must win.

    `session_id` is the conversation a FOLLOWER resolved from its leader at
    dispatch (`_follow_session`), written onto the entry in the same breath as
    the claim and only where the entry has none of its own. The same move
    `_chain_session` makes for a template's next run, and written rather than
    passed straight to the spawn for the same reason: `session_id` is the INPUT
    ("resume this one"), and a row that resumed a conversation without recording
    which one would read afterwards as a fresh send. `session_learned` goes with
    it, because the system worked this id out from a run — nobody chose it."""
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") != entry_id:
                continue
            if entry.get("state") != PENDING:
                return None
            entry["state"] = SENDING
            entry["fired"] = now.isoformat()
            if session_id and not str(entry.get("session_id") or ""):
                entry["session_id"] = session_id
                entry["session_learned"] = True
            _write(entries)
            claimed = dict(entry)
            break
        else:
            return None
    _sync_wake()
    return claimed


def _fail(entry: dict, reason: str) -> None:
    """One send that did not happen: on the entry, on its job row, in the log."""
    _update(entry["id"], state=ERROR, error=reason)
    _report(entry["id"], title=_job_title(entry), kind="task", detail=entry["target"],
            state="error", message=reason)
    _emit(EVENT_FAILED, entry, reason)


def _outgoing(entry: dict) -> str:
    """The entry's message with the file it was scheduled against named in
    front of it, or the message unchanged.

    The Claude page prepends a `<live-app-state>` block to every send a human
    makes, and that block is the ONLY durable record of which FILE a chat is
    about: a transcript's own `cwd` is always the folder, because Claude Code
    keys its session store by cwd and a file has no cwd. Both readers of that
    record — `tasks_store.pane_file`, for which file "open this task" lands on,
    and the template's `_cli_sessions`, for which chats a file is offered —
    find nothing on a scheduled run, because the block is built in browser JS
    at send time and a scheduled run has no browser. The session came from a
    file and then read as if it had come from the folder.

    So the one fact the scheduler actually holds is written in the same shape:
    `entry["target"]`, when it is a file. Nothing else. There is no screen to
    snapshot, no pane shot to take and no annotation to carry, and inventing
    any of them would put a description of a screen nobody was looking at into
    the transcript — the block says plainly that this is a scheduled run.

    A folder target is left alone: the folder is already what the transcript's
    cwd says, so a block naming it would add nothing and claim a pane that
    never existed."""
    message = entry.get("message") or ""
    # A message with no words is left exactly as it is. The block is a PREFIX
    # to something a human said, and prepending it to nothing would send a
    # send that is pure machinery — which every reader here strips back to ""
    # anyway, leaving a turn whose only content the model is asked to answer
    # is a description of a file.
    if not message.strip():
        return message
    state = _outgoing_state(entry)
    return (state + "\n\n" + message) if state else message


def _outgoing_state(entry: dict) -> str:
    """The `<live-app-state>` block alone, or "" for a target that has none.

    Split out of `_outgoing` for `_composed`, which has to put the attachments
    block BETWEEN this one and the user's words — the claude page's own block
    order (`composeOutgoing`), and the order every leading-block strip in the
    system expects."""
    target = entry.get("target") or ""
    if not target or os.path.isdir(target) or not os.path.isfile(target):
        return ""
    state = json.dumps({"entry": target, "scheduled": True})
    return ("<live-app-state>\n"
            "The file this task was scheduled against. No one is at the "
            "screen — this is a scheduled run, so there is no pane snapshot, "
            "no screenshot and no annotation, only the target itself.\n"
            f"{state}\n"
            "</live-app-state>")


def _attachments_block(entry: dict) -> str:
    """The task's attachments as the claude page's own `<pane-shot>` block, or "".

    THE CHAT'S WIRE, NOT A SENTENCE OF OUR OWN (D619). This used to append a
    plain-text tail — "Attached files (read them with the Read tool):" and the
    paths, one per line — and it worked for the model and failed for the human:
    opening the fired task's chat showed the user's turn ending in a list of
    `/Users/…/task-shots/…pdf` temp paths, where the SAME chat renders its own
    attachments as receipt rows (a thumbnail, or 📄 and the file's name, both
    opening the viewer). Same files, two presentations, and the one the user
    could not read was the one they never chose.

    The block the chat writes is the block that renders. `template.html` reads
    it back on restore (`paneShotIn` → `shotRestoreReceipt`) and every reader of
    a transcript already strips it from a row title (`tasks_store`,
    `agent.py::_strip_machinery`, `sessionTitle`), because `pane-shot` has been
    in `_MACHINERY_STRIP` all along. Nothing new had to learn anything; the
    scheduler just had to stop inventing a shape.

    Paths, not pixels, exactly as before: the spawned run pre-allows Read of
    ``shots_dir()`` (`_send` passes it as an extra read dir), so the model opens
    the files itself and a repeat re-reads them on every run without the store
    carrying megabytes of base64.

    THE TAG AND THE PAYLOAD SHAPE ARE DUPLICATED, not imported: a template may
    not import fused_render and fused_render may not import a template (SPEC
    PY-15 / D166), so `_PANE_SHOT_TAG` is a second copy of the page's
    `PANE_SHOT_TAG` and the entries are hand-written to the shape `paneShotIn`
    parses. A parity test reads the page's constant out of template.html and
    compares (D146: the duplicated rule gets a test, not a comment).

    What the payload leaves out, and why that is safe: `viewNote` is "" (there
    is nothing this picture fails to show — nobody cropped it), and there is no
    `size` (the browser drew every picture it kept, or the upload endpoint
    transcoded it). `paneShotIn` reads whatever parses and every consumer treats
    a missing field as absent, so an entry is exactly `{kind, view, name,
    viewNote}` rather than a row of nulls."""
    shots = [a for a in _stored_attachments(entry) if a.get("path")]
    if not shots:
        return ""
    # WHAT they are, in one word, before the per-kind paragraph — the same
    # three-way choice `paneShotBlock` makes, and for its reason: a list that is
    # nothing but files must not be announced as pictures, and a mixed one has
    # no honest singular noun at all.
    files = sum(1 for a in shots if a["kind"] == "file")
    if files == len(shots):
        noun = "a file" if len(shots) == 1 else "files"
    elif files:
        noun = "attachments"
    else:
        noun = "a picture" if len(shots) == 1 else "pictures"
    what = noun if len(shots) == 1 and noun.startswith("a ") \
        else f"{len(shots)} {noun}"
    # Forward slashes on the wire on every platform — the chat template's
    # reader and the agent's Read rule both spell paths that way (agent.py
    # `_wire_path`), and a stored Windows path still carries its backslashes.
    payload = json.dumps([{"kind": a["kind"], "view": a["path"].replace("\\", "/"),
                           "name": a["name"], "viewNote": ""} for a in shots])
    # The chat's own two kind sentences, minus the two kinds a scheduled run
    # cannot have: "pane" and "overview" are pictures of a screen taken at send
    # time, and there was no screen (`_outgoing` says so in the block above
    # this one). Saying WHEN they were attached instead: the user chose these
    # files when they wrote the task, which may have been days ago, and a model
    # told "to this message, deliberately" would be reading a claim about a
    # conversation that never happened.
    return (f"<{_PANE_SHOT_TAG}>\n"
            f"The user attached {what} to this task when they scheduled it — "
            "not to a conversation, and nobody is at the screen for this run. "
            "Each entry's `view` is a path to read and `name` is what they "
            "called it. `kind` says what you are looking at: \"image\" is a "
            "picture the user brought in from somewhere else, so it is NOT a "
            "picture of this app; \"file\" is a file that is not a picture at "
            "all — read it as text, and say so plainly rather than guessing if "
            "it is a binary format (xlsx, zip, a PDF) that will not parse.\n"
            f"{payload}\n"
            f"</{_PANE_SHOT_TAG}>")


def _composed(entry: dict) -> str:
    """Everything the scheduler prepends to the user's words, in the claude
    page's own reading order — state block, attachments block, message.

    The inverse of `composeOutgoing` in template.html, and the order is that
    function's: the machinery first, the words last. It matters for more than
    tidiness — `tasks_store`'s and `agent.py`'s strips only peel a LEADING
    block, so a block wedged after the message would be read as something the
    user typed and would title the row with itself.

    Kept out of `_outgoing`, which stays exactly what it was: one block, about
    the target, testable on its own."""
    parts = [p for p in (_outgoing_state(entry), _attachments_block(entry)) if p]
    message = entry.get("message") or ""
    if not message.strip():
        # A send that is pure machinery — see `_outgoing`. The blocks are a
        # PREFIX to something a human said, and there is nothing to prefix.
        return message
    return "\n\n".join(parts + [message])


def _host_send(entry: dict) -> dict | None:
    """Hand this entry's message to the session host the conversation ALREADY
    has, and answer `{"run_id": …}`; None means there is no such host and the
    caller should spawn as it always did.

    **NEVER A SECOND PROCESS ON ONE CONVERSATION** (Akshil, 2026-09-12). A chat
    whose host is up and idle between turns is exactly the thing a scheduled
    message for that session used to start a `claude --resume` beside: two hosts
    on one session id, two writers on one transcript, in one working tree. The
    chat's own composer has never done that — a follow-up goes into the live
    host's inbox (`agent._send`) and is absorbed into the session — and this is
    that same path, taken by the scheduler.

    `_live_host` is the question "is there a session I can hand a follow-up to",
    which is true for the whole life of a chat, turn or no turn; `_live_run`
    (turn open) is the wrong one here, because a host idling between turns is
    precisely the case this exists for.

    **A LIVE HOST ALWAYS WINS, AND THE GUEST ADAPTS TO IT** (bugbot HIGH,
    2026-09-12). The round before this one asked the host first whether it could
    serve the message unchanged — an attachment directory it was not granted, an
    effort or a model the entry named and it was not on — and took the spawn
    where it could not. But the spawn is a `claude --resume` on that same session
    **while the idle host is still alive**: two writers on one transcript, which
    is the exact failure this whole path exists to prevent. A mismatch is not a
    reason to start a second process next to a live one; it is a reason for the
    GUEST to give something up. So where there is a host, the message goes into
    it with **nothing of the entry's own settings attached**:

    * **No read-dir grant.** `--allowed-tools` is fixed at spawn, so a directory
      the host was not started with cannot be added by sending; asking for one
      makes `agent._send` TREE-KILL the chat's session to force a respawn. Sent
      with `read_dirs=""`, an image the host cannot read raises an ordinary
      permission card in a chat that is open, which the user is sitting in front
      of and can answer — strictly better than either ending their session or
      running a second `claude` beside it.
    * **The host's own model and effort**, and (as before) its own permission
      mode. `effort` is fixed at spawn; `set_model`/`set_permission_mode` are
      applied MID-SESSION and stay applied, so an entry naming either would
      quietly rewrite the settings of a chat somebody is using, for every turn
      after this one. An inbox message runs under the settings its session
      already has, exactly as a message typed into that composer would.

    With all four empty, `agent._send` can never reach its respawn arm: the host
    is never killed, and no second process is ever spawned beside it. Only when
    there is NO live host does `_send` spawn.

    An `{"error": …}` back — the host died between `_live_host` and the write —
    is None, and the caller spawns: the host is gone, so there is nothing to
    race, and the message is owed either way.

    **The entry is marked `host_sent`**, because what this returns is not a run
    of ours: the run id is the CHAT'S session host, shared with the page, and a
    cancel aimed at this entry must never tear it down (see `_send` and
    `_turn_tick`).

    Flag-gated and best-effort: with the project queue off this is not reached
    at all and the pass is the one that shipped, and any failure here is simply
    a spawn, which is what the scheduler did before this existed.
    """
    if not _pq().enabled():
        return None
    session = str(entry.get("session_id") or "")
    target = str(entry.get("target") or "")
    if not (session and target):
        # No session is a fresh conversation, which by definition has no host.
        return None
    # The queue's own accessor rather than `claude_spawn.load_agent`: it
    # memoizes the exec of a 6000-line template for the life of the process (and
    # caches the failure), and this is asked on every scheduled send that names
    # a session. No agent module is no host, which is the spawn this always was.
    agent = _pq().agent_module()
    if agent is None:
        return None
    try:
        run_id = str((agent._live_host(target, session) or {}).get("run_id") or "")
        if not run_id:
            return None
        logger.debug(
            "%s: delivered into the live host %s with its own model/effort; "
            "%d attachment dirs not pre-granted", entry.get("id"), run_id,
            _ungranted_dirs(agent, run_id, entry))
        # Four empty strings, and every one of them is load-bearing — see the
        # docstring. read_dirs and effort keep `agent._send` off its respawn
        # arm (which tree-kills the chat's session); model and permission_mode
        # keep it from queueing a `set_model`/`set_permission_mode` the CLI
        # would apply for the rest of somebody's live session.
        res = agent._send(run_id, _composed(entry), "", "", "", "")
    except Exception:  # noqa: BLE001 — a host we cannot reach is a spawn
        logger.debug("could not send %s into a live host; spawning instead",
                     entry.get("id"), exc_info=True)
        return None
    if isinstance(res, dict) and res.get("sent"):
        return {"run_id": run_id}
    return None


def _ungranted_dirs(agent, run_id: str, entry: dict) -> int:
    """How many of this entry's attachment directories the live host was NOT
    spawned with — i.e. what the guest gives up by asking for no new grant.

    Debug only, and it is the whole of what is left of the old `_host_serves`:
    the answer no longer changes what happens (a live host takes the message
    either way), it only says in the log why an attachment may card. Reads
    `host.json` at all only when there is an attachment to give up."""
    if not _stored_attachments(entry):
        return 0
    granted: set[str] = set()
    try:
        with open(os.path.join(agent.RUNS, run_id, "host.json"),
                  encoding="utf-8") as fh:
            host = json.load(fh)
        if isinstance(host, dict):
            granted = {str(d) for d in (host.get("read_dirs") or [])}
    except (OSError, ValueError):
        pass
    return len([d for d in (shots_dir(),) if d not in granted])


def _send(entry: dict) -> None:
    """Spawn one claimed entry's session and record the outcome.

    **…or hand it to the session host that conversation already has**, with the
    project queue on (`_host_send`). Everything after the send is identical
    either way: the entry records the run it went into and one watcher follows
    that run's turn to its verdict.

    Every failure lands on the ENTRY (state `error`, with the reason) rather
    than propagating: one bad target must not stop the rest of the tick, and a
    scheduled message that failed is exactly the thing the user needs to be able
    to read afterwards."""
    host_sent = False
    try:
        res = _host_send(entry)
        host_sent = res is not None
        if res is None:
            # The extra Read pre-allowance is passed only when this run actually
            # HAS attachments — not as `None` on every other send. Two reasons, and
            # the second is the load-bearing one: a directory rule on a run with
            # nothing to read there is standing permission for no reason, and every
            # send without images keeps the exact call shape it has always had.
            attachments = ({"extra_read_dirs": [shots_dir()]}
                           if _stored_attachments(entry) else {})
            res = claude_spawn.spawn_helper(
                entry["target"], _composed(entry),
                entry.get("permission_mode")
                or _SCHEDULED_PERMISSION_MODE, entry.get("session_id") or "",
                model=str(entry.get("model") or ""),
                effort=str(entry.get("effort") or ""),
                **attachments)
    except Exception as exc:  # noqa: BLE001 — the reason belongs on the entry
        _fail(entry, f"failed to start session: {exc}")
        return
    run_id = res.get("run_id")
    if res.get("error") or not run_id:
        _fail(entry, str(res.get("error") or "failed to start session"))
        return
    # Registered BEFORE the store says `sent`, and that order is the point: the
    # sweep treats a `sent` entry with nothing watching it as abandoned, so a
    # window where this one is already `sent` but not yet registered is a window in
    # which a concurrent sweep would close a turn that is about to be watched
    # perfectly well.
    _watching(entry["id"], True)
    # `host_sent` is written ONLY when it is true, so an ordinary spawn's stored
    # entry is the one main writes, field for field. It is the fact everything
    # downstream needs and cannot re-derive: the run id on this entry is a
    # session host the CHAT owns, not a process this send started.
    _update(entry["id"], state=SENT, run_id=str(run_id), error="",
            **({"host_sent": True} if host_sent else {}))
    entry["host_sent"] = host_sent
    # The row opens `running` and stays that way for the whole TURN, not just the
    # spawn — the spawn takes a moment and the turn can take minutes, and the
    # minutes are the part worth being able to see. `cancellable` is honest here
    # in a way it is not for most reporters: this process can actually stop the
    # run (agent._cancel), so the manager's ✕ is an action.
    #
    # …EXCEPT FOR A MESSAGE THAT WENT INTO A LIVE CHAT'S INBOX (bugbot,
    # 2026-09-12). There the run is the chat's own session host: `agent._cancel`
    # would kill the process the user is typing into, ending their live session
    # to withdraw one scheduled follow-up. Nothing in agent.py can take a
    # message back out of the inbox without also throwing away whatever else is
    # queued there, so the honest answer is that this row has no stop button —
    # the chat's own Stop does, and it is the one that knows what it is
    # stopping. See `_turn_tick`, which refuses the same call from the other
    # side for a flag the registry should never carry anyway.
    _report(entry["id"], title=_job_title(entry), kind="task",
            detail=entry["target"], state="running", cancellable=not host_sent)
    # Nothing else will poll the run, so without this thread the finished
    # turn is never committed — and, since
    # this feature added an observer, nobody would ever learn how the turn went.
    try:
        threading.Thread(
            target=_watch_turn, args=(dict(entry), str(run_id)),
            daemon=True, name="fused-schedule-session-record").start()
    except Exception:  # noqa: BLE001
        logger.debug("could not start the turn-recording thread", exc_info=True)
        # Nothing is watching and nothing will, so say so now rather than leave the
        # sweep to notice a turn it cannot distinguish from one abandoned by a dead
        # process — this one is abandoned in a live process, and immediately.
        _watching(entry["id"], False)
        _close_unwatched(entry, "could not start the watcher for this turn")


def _turn_ended(entry: dict) -> None:
    """A turn just finished: ring the loop and the Tasks watchers.

    The 30-second poll is the right interval for "did anything come due", and
    the wrong one for "the thing that was in the way has stopped". Whatever was
    waiting on this conversation — or, with the project queue on, on the folder
    it was editing — should go in about a second, not on the next timer.

    Flag-agnostic and deliberately so. With the queue off the ring costs one
    early pass that finds the same nothing it would have found later, and the
    notify is simply true: this row changed, and the page drawing it has been
    long-polling for exactly that news."""
    wake()
    _notify({_task_key(entry)})


def _turn_tick(entry: dict, run_id: str, agent, data: dict) -> bool:
    """One observation of a live turn. False stops the watch.

    Does three things, in the order they matter: honour a cancel the user asked
    for, record the outcome once the turn ends, and otherwise say what the run is
    DOING. The last one is why `permissions` is checked first — a turn parked on
    a card nobody has answered looks identical to a slow one from the outside,
    and for an unattended session that is the single most likely way to be
    stuck."""
    entry_id = entry["id"]
    # CAPTURE THE SESSION THE TURN RAN IN, on whichever tick first reports it.
    # `session_id` on the entry is an INPUT — "resume this one", empty meaning
    # "start a fresh one" — so it cannot double as the answer without retroactively
    # relabelling every fresh send as a continuation. This is the answer, and it is
    # what makes the row linkable: the Inbox addresses a session by exactly this id
    # (`?peek=<id>`), and for a fresh send nothing else in the app knows it.
    ran = str(data.get("session_id") or "")
    if ran and ran != entry.get("claude_session_id"):
        entry["claude_session_id"] = ran
        _update(entry_id, claude_session_id=ran)
        # …and if this was a chaining template's run, the answer becomes the
        # INPUT of the next one. Without this the default never happens: a
        # template created from the Tasks page has no session id, every
        # occurrence inherits "", and every run opens a new thread — which is
        # exactly what "new task each run" is supposed to be the opt-IN to.
        # `_chain_session` owns every condition on that; here we only know that
        # this is the first tick that had an id to offer.
        _chain_session(str(entry.get("template_id") or ""), ran)
    if data.get("done"):
        reason = str(data.get("error") or "")
        if data.get("cancelled"):
            # THE OTHER STOP BUTTON. The queue card's ✕ arrives as a
            # `cancel_requested` flag on the job row and is handled below, so
            # this module knows that stop was asked for. The CHAT's own Stop
            # calls `agent._cancel` directly and tells this module nothing — so
            # all the watcher used to see was the kill's error, which it filed
            # as a FAILED turn: the chat said "Stopped." and the board flew a
            # red Failed mark for the same act on the same run, which is the
            # disagreement this whole branch exists to end.
            #
            # The run's own cancel marker is the shared fact (agent.py
            # `_cancel` writes it, `_poll` reports it), so both stops are
            # recorded identically — and the kill's error is deliberately NOT
            # kept: it describes a truncated reply, which is what a stop IS,
            # and storing it would put a reason on the row for something that
            # went exactly as asked. No event either, for the same reason the ✕
            # below emits none: a stop needs no toast to tell the person who
            # pressed it.
            _update(entry_id, turn="cancelled", turn_at=_now().isoformat())
            _report(entry_id, state="cancelled")
            _turn_ended(entry)
            return False
        if reason:
            _update(entry_id, turn="failed", error=reason,
                    turn_at=_now().isoformat())
            _report(entry_id, state="error", message=reason)
            _emit(EVENT_FAILED, entry, reason)
        else:
            # `turn_at` is WHEN the verdict landed, stamped wherever `turn` is
            # written. The Tasks page needs the moment, not just the word: a
            # transcript still being written to meaningfully after this stamp
            # is new work, and only the verdict's own closing echo may be set
            # aside (`tasks._verdict_outvotes_live`).
            _update(entry_id, turn="ok", turn_at=_now().isoformat())
            _report(entry_id, state="done", detail="finished")
            _emit(EVENT_DONE, entry)
        _turn_ended(entry)
        return False

    # UNANSWERED ONLY. `_poll` hands back every request the run has raised,
    # answered ones included, so that a re-attaching frame can rebuild the cards
    # it never saw (agent.py `_permissions`) — reading the whole list as "parked"
    # would call a run that was carded once and allowed twenty minutes ago
    # blocked for the rest of its life.
    parked = [p for p in (data.get("permissions") or []) if not p.get("decision")]
    detail = "waiting for permission" if parked else str(data.get("phase") or "working")
    tokens = data.get("tokens") or 0
    if tokens and not parked:
        detail = f"{detail} · {int(tokens)} tokens"
    # One call: reporting the tick is also how the cancel flag is read back.
    record = _report(entry_id, detail=detail)
    if record and record.get("cancel_requested"):
        # NEVER KILL A HOST THIS ENTRY MERELY WROTE TO (bugbot, 2026-09-12).
        # With the project queue on, a scheduled message for a conversation that
        # already has a live session host is delivered into that host's inbox
        # rather than spawned (`_host_send`), and the run id recorded here is
        # THE CHAT'S — the process the user has a page open on. `agent._cancel`
        # ends that process: a stop asked of this row would have closed the
        # reader's live session, kill the turn they were watching and any
        # message they had queued behind it. The row is reported not-cancellable
        # for exactly this reason, so a flag here is either a stale registry
        # record or a client that ignored it; either way the entry stops being
        # watched and says so, and the session is left alone. There is no
        # middle road to take: `interrupt` aborts the user's turn just the same,
        # and `_discard_inbox` throws away every undrained message in the
        # session, the user's own included.
        if not entry.get("host_sent"):
            try:
                agent._cancel(run_id)
            except Exception:  # noqa: BLE001 — a cancel that fails is still a stop attempt
                logger.debug("could not cancel scheduled run %s", run_id,
                             exc_info=True)
        _update(entry_id, turn="cancelled", turn_at=_now().isoformat())
        _report(entry_id, state="cancelled")
        return False
    return True


def _chain_session(template_id: str, ran: str) -> None:
    """Teach a chaining template which conversation its runs live in — once.

    THIS IS WHAT MAKES CHAINING THE DEFAULT rather than merely the documented
    intention. `_materialize` gives an occurrence the template's `session_id`,
    so a template that has one repeats into one thread; but a template created
    from the Tasks page has none, and nothing else ever gave it one — the
    watcher writes the session a run landed in onto the OCCURRENCE
    (`claude_session_id`), and an occurrence is thrown away. So the template
    stayed empty for ever and every run started fresh, making the "new task
    each run" opt-out indistinguishable from leaving it unticked. The first run
    of a chaining template answers the question the template could not, and
    this is where that answer is carried back to it.

    The distinction the module rests on is NOT weakened by that. `session_id`
    is still the input ("resume this one", "" meaning start fresh) and
    `claude_session_id` still the answer ("what it ran in"); no entry's own
    session_id is rewritten to match its own answer, which is the move that
    would retroactively relabel a fresh send as a continuation. Run 1 keeps
    saying it started fresh. What travels is run 1's answer into run 2's input,
    across two different entries, which is the ordinary direction of the link.

    Four conditions, each load-bearing:

    * **Only a live template.** An occurrence's `template_id` is the only way
      up; a one-off has none and never reaches here.
    * **Only when the template is CHAINING.** With `new_task_each_run` ticked
      the template must keep minting fresh sessions for ever, so it must never
      learn one.
    * **Only into an EMPTY `session_id`.** A template that already has one was
      told which conversation to continue (a task handed off from a chat), and
      that is the user's decision, not this function's. It is also what makes
      the writeback idempotent and the thread stable: the first run wins, later
      ticks of the same turn re-report the same id and find the field taken, so
      the store is not rewritten.
    * **The pending successor is fixed up too.** `_materialize` runs on every
      tick and only waits for an occurrence to leave `pending`/`sending` — so
      run 2 can already exist, minted with "", before run 1's session is
      reported. Filling in that one entry here is what stops the race from
      costing a whole occurrence its thread. Only `pending` ones: a finished
      occurrence's input is a historical fact.

    Same locked read-modify-write as every other mutation here, and for the
    same reason — this runs on a watcher thread, mutating a DIFFERENT entry
    than the one being watched, while other watchers may be reporting. No
    `_sync_wake`: not one due time moves, and the stub costs two subprocesses.
    """
    if not template_id or not ran:
        return
    with _lock:
        entries = _read()
        template = next((e for e in entries
                         if str(e.get("id") or "") == template_id), None)
        if (template is None
                or template.get("state") != RECURRING
                or _flag(template.get("new_task_each_run"))
                or str(template.get("session_id") or "")):
            return
        template["session_id"] = ran
        # …and WHO decided it, written in the same breath, because this is the
        # only moment anything knows. An edit is cancel + re-create, and the
        # form has to keep a learned id while refusing a chat's; it used to tell
        # them apart by asking whether the entry repeated, which a repeat →
        # one-off → repeat round trip breaks. Recorded provenance does not care
        # what the entry became.
        template["session_learned"] = True
        for entry in entries:
            if (str(entry.get("template_id") or "") == template_id
                    and entry.get("state") == PENDING
                    and not _flag(entry.get("new_task_each_run"))
                    and not str(entry.get("session_id") or "")):
                entry["session_id"] = ran
                entry["session_learned"] = True
        _write(entries)


def _watch_turn(entry: dict, run_id: str) -> None:
    """Thread body: follow one sent message's turn to its end.

    Wraps `record_session_when_ready` rather than replacing it — that function
    owns the run bookkeeping and the commit, which must happen whether or not
    anything is watching. This only adds the observer.

    **The watch can end without a verdict**, and every one of those paths used to
    leave the entry `turn: ""` — which the page reads as "Running…" and the toast
    logic as "nothing to say", so a row sat live forever and the user was never
    told. `_close_unwatched` is the floor under all of them: a `load_agent` that
    raises, a `_poll` that raises, and the tick cap (~1h) that a genuinely long
    turn can outrun.

    What it is NOT a floor under is the process dying, because this thread dies
    with it. That case belongs to the sweep, which is why the `finally` here has to
    run on every path: while this id is registered the sweep leaves the entry
    alone, so failing to deregister would make a finished turn look permanently
    live to the next sweep — the same stuck row, arrived at from the other side."""
    try:
        try:
            agent = claude_spawn.load_agent()
        except Exception:  # noqa: BLE001
            logger.debug("could not load the agent backend to watch a run",
                         exc_info=True)
            _close_unwatched(entry, "could not read the run's progress")
            return
        claude_spawn.record_session_when_ready(
            agent, run_id,
            on_tick=lambda data: _turn_tick(entry, run_id, agent, data))
        # Back here means the poll loop is finished. If `_turn_tick` saw `done` it
        # already recorded the outcome and this is a no-op; anything else and the
        # watch ended without one.
        _close_unwatched(entry, "stopped reporting before the turn finished")
    finally:
        _watching(entry["id"], False)


def _close_unwatched(entry: dict, reason: str) -> None:
    """Resolve an entry whose watch ended without a verdict — once.

    Re-reads the store rather than trusting the copy this thread has held since
    the send: `_turn_tick` may have resolved it seconds ago, and re-closing would
    overwrite a real outcome with a shrug.

    `turn` becomes `unknown`, which is the honest word. The work may well have
    finished — the transcript knows, and the run id on the entry is how to go and
    read it — but this app stopped being able to say, and a row that claims to be
    running when nothing is watching it is the lie the job registry's `stalled`
    state exists to avoid telling."""
    entry_id = entry["id"]
    with _lock:
        stored = next((e for e in _read() if e.get("id") == entry_id), None)
        if stored is None or stored.get("state") != SENT or stored.get("turn"):
            return  # already resolved, or never got far enough to need this
    _update(entry_id, turn="unknown", error=reason,
            turn_at=_now().isoformat())
    _report(entry_id, state="error", message=reason)
    _emit(EVENT_FAILED, entry, reason)
    # A turn nobody could follow to the end still ENDED — whatever was waiting
    # on this conversation has been waiting on a watch, not on work. Keyed off
    # the stored copy, which knows the session the run reported; the copy this
    # thread has held since the send may predate it.
    _turn_ended(stored)


def _busy_sessions(entries: list[dict]) -> set[str]:
    """Session ids with a scheduled send already in flight — claimed but not yet
    spawned (`sending`), or spawned with a turn still running (`sent`, no `turn`
    verdict). Fresh-session entries (`session_id` "") are never busy: they collide
    with nothing.

    BOTH ids count, and the answer is why. An entry that asked for a fresh
    session occupies the session it GOT just as completely as a resume occupies
    the one it named — the difference is only that nobody knew which one that
    would be until the turn said so. The first run of a chaining template is
    exactly that case: it sends with "" and `_chain_session` then puts the id it
    reported on the template, so run 2 arrives resuming a conversation whose
    only record of being busy is run 1's `claude_session_id`. Reading just the
    input here would let run 2 resume a thread mid-turn."""
    busy = set()
    for entry in entries:
        state = entry.get("state")
        if state != SENDING and not (state == SENT and not entry.get("turn")):
            continue
        for key in ("session_id", "claude_session_id"):
            session = str(entry.get(key) or "")
            if session:
                busy.add(session)
    return busy


def busy_sessions(entries: list[dict]) -> set[str]:
    """`_busy_sessions` for readers outside the loop.

    The Tasks router asks the scheduler's own question — "is there a send in
    this conversation I have not heard back from?" — when it decides whether an
    `in_progress` pin is still describing something. It has to be the SAME
    answer, for the reason `session_liveness`'s docstring gives about the
    liveness rule: a second, nearly-identical notion of "still running" that
    disagreed by one state would put a card in a lane the scheduler does not
    believe in. So this is a re-export and deliberately not a reimplementation.
    """
    return _busy_sessions(entries)


def _session_live(session_id: str, now: datetime, seen: dict | None = None) -> bool:
    """Is a HUMAN (or anything else) mid-turn in this session right now?

    The other half of "busy", and the gap `tick`'s docstring used to name as
    known and unfixable. `_busy_sessions` reads the schedule store, so it knows
    about the sends THIS module has in flight and nothing whatever about the
    user typing into the same conversation in the explorer's chat. Resuming a
    session mid-turn is not a race to lose politely: `claude --resume S` and the
    chat's own process both append to one transcript, and the transcript IS the
    session.

    The answer comes from the transcript itself (`session_liveness`), which is
    the only place that records the turn regardless of who started it — and is
    the same rule, one copy of it, that paints the `running` badge on the Inbox
    and the Board. Imported from `fused_render`, never from a router: this
    module is below `fused_render.server` and must stay there.

    Unreadable, missing, or no transcript at all answers **False**, and that
    direction is deliberate. A liveness read this module cannot make must not be
    able to hold a message back for ever; not-live restores exactly the
    behaviour that shipped before this check existed.

    `seen` memoizes within one tick — a batch of messages into one conversation
    would otherwise stat and tail-read the same file once each.
    """
    if not session_id:
        return False
    if seen is not None and session_id in seen:
        return seen[session_id]
    try:
        from fused_render import session_liveness

        live = session_liveness.session_running(session_id, now.timestamp())
    except Exception:  # noqa: BLE001 — never stop a send over a failed read
        logger.debug("could not read session liveness for %s", session_id,
                     exc_info=True)
        live = False
    if seen is not None:
        seen[session_id] = live
    return live


# THE VERDICTS THAT MEAN THE WATCHER SAW THE TURN END — the only two a
# live-looking transcript may be measured against (`_verdict_echo`).
#
# `turn` is truthy for four words and the other two are not evidence that
# anything is over. `unknown` is the dangerous one: `_claim_due`'s sweep stamps
# it on every SENT entry whose turn was still open when the app stopped, so on
# the first tick after a restart it lands on runs whose detached `claude` is
# still writing — reading that as a verdict would call a live transcript's own
# pulse an echo and put a second `claude --resume` on it, which is the one
# outcome this whole gate exists to prevent. `cancelled` is quieter and just as
# wrong: `_turn_tick`'s ✕ arm calls `agent._cancel` and stamps the word in the
# next line, and `_cancel`'s gentler road is an `interrupt` control request —
# the CLI has been TOLD to abort and writes the interrupted turn's own closing
# rows afterwards, so the process is alive at the moment the verdict is
# recorded. Only `ok` and `failed` are written off a `_poll` that reported
# `done` (round-3 review, 2026-09-12).
_WATCHED_VERDICTS = frozenset({"ok", "failed"})


def _verdict_at(session_id: str, entries: list[dict]) -> float:
    """The newest turn verdict THIS MODULE WATCHED LAND for `session_id`, as a
    unix timestamp — 0.0 when it has none.

    Both id fields, exactly as `_busy_sessions` reads them and for the same
    reason: a fresh send occupies the session it GOT, and that id lives on
    `claude_session_id` while the input stays "". An entry whose `turn` a
    pre-stamp store wrote has no `turn_at` and is skipped rather than guessed
    at — with no moment to measure the tail against, the old rule is the honest
    one; and a `turn` that is not one of `_WATCHED_VERDICTS` is skipped because
    nothing watched that turn finish."""
    newest = 0.0
    for entry in entries:
        if str(entry.get("turn") or "") not in _WATCHED_VERDICTS:
            continue
        if session_id not in (str(entry.get("session_id") or ""),
                              str(entry.get("claude_session_id") or "")):
            continue
        try:
            when = parse_due(entry.get("turn_at")).timestamp()
        except ValueError:
            continue
        newest = max(newest, when)
    return newest


def _verdict_echo(session_id: str, entries: list[dict], now: datetime,
                  seen: dict | None = None) -> bool:
    """Is this session's transcript liveness only the ECHO of a turn this module
    has already filed a verdict for?

    THE 45-SECOND WINDOW LIES IN EXACTLY ONE DIRECTION, and the scheduler was
    the surface paying for it (browser QA, 2026-09-12). A leader finished its
    turn at T — `turn_at` stamped, `_turn_ended` rang the loop — and the message
    typed behind it did not go until T+60, three whole ticks later. Nothing was
    busy and the folder was free: `_session_live` was reading the finished
    turn's OWN closing rows as a live turn, because a non-interactive run writes
    no `turn_duration` record for `tail_activity` to find and the window has
    nothing else to go on. The hold was holding a message against the very turn
    it was waiting for.

    So the same reasoning the Tasks router already applies to the same lie
    (`tasks._verdict_outvotes_live`): when the newest verdict we WATCHED land
    for this conversation is stamped, and the tail after it is that turn's own
    closing rows, the tail is an obituary and not a pulse.

    **THE TAIL'S SHAPE DECIDES, AND THE CLOCK IS ONLY A CEILING** (round-3
    review, 2026-09-12). Measuring by the stamp alone silenced a HUMAN turn
    that genuinely began inside the window — the user typing three seconds
    after the leader finished is activity "younger than verdict + 15s" and was
    read as the leader's echo, which is two processes on one transcript
    arriving through the very rule that was meant to stop them. A transcript
    says which it is: `session_liveness.session_turn_open` walks back to the
    last MESSAGE, and a `user` row (or an assistant row mid-tool-call) is a
    turn somebody has open right now — never an echo, whatever the clock says.
    Only once the file's last word is a finished assistant reply does
    `VERDICT_ECHO_SEC` get asked, and there it does the job it was widened for:
    activity still appending 15 seconds past the verdict is sustained work
    keeping its vote, not a handful of closing rows.

    **Re-read only where it can change the answer.** The verdict is looked up
    first (in memory, off entries this pass already holds), so the transcript is
    re-read only for a session that both reads live AND has a watched, stamped
    verdict; the shape read settles most of those on its own and the freshness
    read happens only behind it. `seen` memoizes the whole answer within one
    pass, like `_session_live`'s own.

    **Flag-gated** (`project_queue.enabled()`). It is a correctness fix for the
    pre-existing per-session hold too, and the blast radius is what decides it:
    with the queue off the schedule behaves byte-for-byte as it shipped, and the
    30-second poll that covers the same case there is exactly what the queue's
    "next task starts in about a second" promise cannot live with.

    Never raises: a read that fails answers False, which leaves the hold exactly
    as strict as it was."""
    if not session_id or not _pq().enabled():
        return False
    if seen is not None and session_id in seen:
        return seen[session_id]
    echo = False
    verdict = _verdict_at(session_id, entries)
    if verdict > 0.0:
        try:
            from fused_render import session_liveness

            stamp = now.timestamp()
            if not session_liveness.session_turn_open(session_id, stamp):
                active = session_liveness.session_activity(session_id, stamp)
                echo = active <= verdict + session_liveness.VERDICT_ECHO_SEC
        except Exception:  # noqa: BLE001 — a failed read holds nothing back
            logger.debug("could not read the tail for %s", session_id,
                         exc_info=True)
            echo = False
    if seen is not None:
        seen[session_id] = echo
    return echo


def _made(entry: dict) -> int:
    """How many occurrences a rule template has materialized. Read defensively:
    the store is a JSON file a human can edit, and a `made` that came back as a
    string must not stop the schedule."""
    value = entry.get("made")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _next_template_due(entry: dict, base: datetime) -> datetime | None:
    """The next occurrence for one live template, or None when it is spent.

    THE one place the two kinds of template differ. Everything else — claiming,
    firing, skipping, restoring, reporting — is written once for both, which is
    the whole reason the structured rule reuses the RECURRING state rather than
    bringing its own lifecycle.

    None means the series is over: `count` reached, or `until` passed, or off
    the end of the calendar. A cron line never returns it — a standing rule has
    no end — so this is the seam that "ends after 13 occurrences" arrives
    through, and the caller's answer to it is to do nothing, which leaves the
    template `recurring` with nothing ahead of it.

    Raises ValueError when the stored schedule no longer reads (a hand-edited
    store); the caller turns that into a loud `error`, because silently never
    firing again is the one outcome this feature must not have."""
    spec = entry.get("rule")
    if isinstance(spec, dict):
        spec = recur.validate_rule(spec)
        count = spec.get("count")
        if isinstance(count, int) and _made(entry) >= count:
            return None
        # `anchor` falls back to `due` only for a store written before the field
        # existed; on a live template the two differ from the first tick.
        anchor = parse_due(entry.get("anchor") or entry.get("due"))
        when = recur.next_occurrence(spec, _local_naive(anchor), _local_naive(base))
        return _from_local(when) if when is not None else None
    line = cron.parse(str(entry.get("repeats") or ""))
    return _from_local(line.next_after(_local_naive(base)))


def _skipped(entry: dict) -> int:
    """How many runs this entry has already absorbed. Read defensively, like
    `_made`: a hand-edited count must not stop the schedule."""
    value = entry.get("skipped")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _walk_latest(template: dict, when: datetime, now: datetime,
                 spend: bool) -> tuple[datetime, int]:
    """Step a template's recurrence forward from `when` to the LATEST occurrence
    still at or before `now`; returns (that time, how many steps it took).

    The one walk, shared by the two callers that both mean "collapse a run of
    past slots into the last of them, and never replay the rest":

    * `_coalesce`, for a repeat the app slept through — those slots were on the
      calendar, so `spend=True` bills each to `made` and the caller counts them
      onto the survivor as `skipped`;
    * `_catch_up_base`, for a rule created with an anchor already in the past —
      those slots were never on any calendar, because the rule did not exist
      when they went by, so `spend=False`: they cost the `count` budget nothing
      and there is nothing to report as skipped. (That is also the rule
      `_upcoming_rule` already projects by; Bugbot, PR #541.)

    Three ways to stop, all of them load-bearing:

    * **`ValueError`** — a hand-edited schedule that no longer parses. Left for
      `_materialize`, which is where that verdict is announced; the caller keeps
      the time it has already reached.
    * **`None` or past `now`** — the ordinary end: the series is spent, or the
      next slot is in the future and is therefore not a catch-up at all.
    * **a step that does not MOVE.** Recurrence math is done on local
      wall-clock time (a repeat is a wall-clock promise), so the autumn
      fall-back — where one local hour happens twice — can hand back a later
      local time that converts to an earlier or equal UTC instant. Stepping on
      it would walk the same hour for as long as the cap allows. The repo
      already accepts a cosmetic DST ghost as out of scope; this only refuses to
      spin on one.

    `_COALESCE_MAX_STEPS` bounds the WORK, never the lateness — see there.
    """
    steps = 0
    while steps < _COALESCE_MAX_STEPS:
        try:
            following = _next_template_due(template, when)
        except ValueError:
            break
        if following is None or following > now:
            break
        if following <= when:
            break
        when = following
        steps += 1
        if spend and isinstance(template.get("rule"), dict):
            template["made"] = _made(template) + 1
    return when, steps


def _coalesce(now: datetime) -> None:
    """Collapse a recurring template's backlog into ONE run — the latest.

    The half of the old 120-second occurrence bound that survives. That bound
    discarded every late recurring run; this keeps the last one and drops the
    rest, because "daily at 9am" replayed seven times into one thread on Monday
    morning is not what the words meant, while running it once — late — is.

    **Two shapes of backlog reach here, and both have to work.** Which one the
    store holds depends only on how the app was closed:

    * **One stale pending occurrence** is the ordinary case. `_materialize`
      keeps exactly one run ahead of a template and refuses to make another
      while it is still pending, so an app closed for a week reopens with a
      single occurrence dated a week ago — and the six runs in between exist
      only in the recurrence, never in the store. Those are found by WALKING
      (`_next_template_due`), not by counting ticks: the walk asks the rule
      which occurrences lie between that due time and now.
    * **Several past-due pending occurrences** happen when something else put
      them there — `restore` un-skipping a run beside its successor, or a
      hand-edited store. All but the newest are marked `missed`; the newest is
      the survivor, and the walk continues from it.

    The dropped runs are COUNTED, never replayed: `skipped` accumulates on the
    survivor and `skipped_note` is the sentence the UI shows ("5 earlier runs
    skipped"). One `missed` event is emitted for the whole collapse rather than
    one per dropped run — a toast per skipped run is precisely the storm this
    exists to prevent.

    `made` moves with the walk for a rule template, because it must: it counts
    what the template put on the calendar, and the docstring on `create` is
    explicit that skipped runs count. That also ends a `count` series honestly —
    `_next_template_due` answers None once the budget is spent, which stops the
    walk exactly where materialization would have stopped.

    Occurrences it touches lose any legacy `max_late`, so the survivor is sent
    rather than swept to `missed` by the bound coalescing replaced.

    Only PENDING entries are read and only PENDING entries are written, which is
    what keeps this from resurrecting anything: an entry the old bound already
    called `missed` is terminal and invisible here."""
    announce: list[tuple[str, dict, str]] = []
    with _lock:
        entries = _read()
        templates = {str(e.get("id")): e for e in entries
                     if e.get("state") == RECURRING}
        backlog: dict[str, list[tuple[datetime, dict]]] = {}
        for entry in entries:
            tid = str(entry.get("template_id") or "")
            if not tid or entry.get("state") != PENDING:
                continue
            # A past-anchored rule's catch-up occurrence (`catch_up`) is NOT
            # special-cased here, and that is worth stating because an earlier
            # cut of this did. It is created already sitting on the latest slot
            # at or before the moment it was made — by this very walk — so a
            # coalesce in the same tick finds nothing to move. If it is still
            # pending days later (held back by a busy conversation, say) then it
            # genuinely IS a backlog by then, and collapsing it forward is the
            # right answer rather than an exception to be carved out.
            try:
                when = parse_due(entry.get("due"))
            except ValueError:
                continue  # `_claim_due` owns the unreadable-due verdict
            if when <= now:
                backlog.setdefault(tid, []).append((when, entry))
        changed = False
        for tid, occurrences in backlog.items():
            # Newest last. The id breaks ties and is itself due-time-derived, so
            # two occurrences on the same second still order deterministically.
            occurrences.sort(key=lambda pair: (pair[0], str(pair[1].get("id"))))
            when, survivor = occurrences[-1]
            dropped = 0
            for _, earlier in occurrences[:-1]:
                earlier["state"] = MISSED
                earlier["error"] = ("skipped: only the latest missed run of a "
                                    "repeating message is sent")
                dropped += 1
                changed = True
            template = templates.get(tid)
            steps = 0
            if template is not None:
                when, steps = _walk_latest(template, when, now, spend=True)
                dropped += steps
            if steps:
                # The survivor MOVES to the latest missed occurrence rather than
                # a new entry being created for it: one run was missed many
                # times over, and one row is the honest way to say that.
                survivor["due"] = when.isoformat()
                if template is not None:
                    # The template's `due` mirrors its latest occurrence, which
                    # is what the listing sorts and shows for the recurring row.
                    template["due"] = when.isoformat()
                changed = True
            if survivor.get("max_late") is not None:
                # The bound this pass replaced. Left in place it would sweep the
                # very run we just decided to send.
                survivor.pop("max_late", None)
                changed = True
            if dropped:
                total = _skipped(survivor) + dropped
                survivor["skipped"] = total
                survivor["skipped_note"] = (
                    f"{total} earlier run{'' if total == 1 else 's'} skipped")
                announce.append((EVENT_MISSED, dict(survivor),
                                 survivor["skipped_note"]))
                changed = True
        if changed:
            _write(entries)
    for kind, entry, detail in announce:
        _emit(kind, entry, detail)
    if changed:
        _sync_wake()


def _catch_up_base(entry: dict, existing: list[dict],
                   now: datetime) -> datetime | None:
    """For a template anchored in the PAST that has never run: the instant to
    materialize its ONE catch-up occurrence from. None for every other template,
    which then materializes from `now` exactly as before.

    **The inconsistency this fixes.** A one-off scheduled for last Tuesday runs
    the moment the app opens — the queue sorts it to the head and sends it, and
    the docstring on `create` is explicit that this is what picking a past date
    means. A REPEAT anchored last Tuesday did nothing at all until the next slot
    came round, because materialization computed from `now` and a past anchor
    therefore only ever set the PHASE. Two ways of saying "starting last
    Tuesday", two different answers, and nothing on the form to tell you which
    one you were about to get.

    **Which past slot runs is the LATEST one, not the anchor.** The anchor sets
    the pattern; the run that goes is the most recent slot at or before now:

        anchor Aug 15 09:00 · daily · now = Aug 17 10:00
          Aug 15 09:00   no run        <- the anchor only sets the pattern
          Aug 16 09:00   no run
          Aug 17 09:00   RUNS NOW      <- one catch-up, due stays Aug 17 09:00
          Aug 18 09:00   upcoming

    That is the same rule `_coalesce` applies to a repeat the app slept through,
    and it is the same walk (`_walk_latest`) — "daily at 9am" started last
    Tuesday does not mean three mornings replayed, it means this morning's, run
    late. The intervening slots are never materialized: they did not happen and
    never will. And the `due` that survives is that slot's own real time, not
    `now`, so the chip stays in the column it belongs to (see the `at` /
    `ran_at` split on the Tasks side).

    `spend=False` on the walk, because those slots cost the `count` budget
    nothing — the rule did not exist when they went by, so nothing put them on a
    calendar. Only the occurrence `_materialize` actually creates is billed, and
    it bills it in the ordinary place. This is the same accounting
    `_upcoming_rule` already projects by (Bugbot, PR #541).

    Two conditions guard it, each preventing a double helping:

    * **a rule template only.** A cron template has no anchor; `create` computes
      its first occurrence from `now` by construction, so there is no past slot
      to catch up to and nothing to decide.
    * **no occurrences, ever.** `existing` empty AND `made` zero. A template
      that has been running has its backlog handled by `_coalesce` already;
      reaching back here as well would collapse the same run twice, and a
      template whose only occurrence was CANCELLED must not have that cancel
      undone by a fresh catch-up.

    The returned base is a hair BEFORE the chosen slot, because
    `recur.next_occurrence` is strictly-after and `_materialize` asks it for
    "the next one after base" — this is how the slot itself comes back. Every
    occurrence after it is computed from `now` in the ordinary way, so the
    series continues rather than replaying.
    """
    if not isinstance(entry.get("rule"), dict):
        return None
    if existing or _made(entry):
        return None
    try:
        anchor = parse_due(entry.get("anchor") or entry.get("due"))
    except ValueError:
        return None
    if anchor >= now:
        return None
    try:
        # The first slot of the series at or after the anchor. Not the anchor
        # itself: a weekly rule anchored on a Tuesday with only Thursday chosen
        # starts on the Thursday, and `recur` is the only thing that knows.
        first = _next_template_due(entry, anchor - timedelta(microseconds=1))
    except ValueError:
        return None
    if first is None or first > now:
        return None
    latest, _steps = _walk_latest(entry, first, now, spend=False)
    return latest - timedelta(microseconds=1)


def _materialize(now: datetime) -> None:
    """Ensure every live recurring template has exactly ONE pending occurrence.

    Idempotent by construction, which is the whole trick: it does not remember
    what it did, it looks at what exists. A template whose occurrence is still
    `pending` or `sending` is left alone; one whose occurrence has finished
    (sent, missed, error, cancelled — any of them) gets the next one. The next
    time is computed from the LATEST occurrence ever materialized, not from
    `now`, so a run finishing early can never pull the next one earlier, and a
    cancelled occurrence stays skipped instead of being re-offered.

    ONE exception, and it only ever fires once per template: a rule template
    anchored in the past with nothing materialized yet computes from the LATEST
    slot at or before now instead, so its first occurrence is already overdue
    and runs immediately — the same thing a past-dated one-off does, and the
    same collapse `_coalesce` performs on a backlog. `_catch_up_base` is the
    whole of that decision and says why it is bounded to exactly one run.

    Both kinds of template come through here identically; `_next_template_due`
    is the only line that knows whether it is reading a cron expression or a
    structured rule, and it is also where a rule's `count` and `until` end the
    series (by answering None, which materializes nothing).

    A template whose schedule no longer parses (a hand-edited store) is moved
    to `error` and announced — silently never firing again is the one outcome
    this feature must not have."""
    announce: list[tuple[str, dict, str]] = []
    with _lock:
        entries = _read()
        occurrences: dict[str, list[dict]] = {}
        for entry in entries:
            tid = str(entry.get("template_id") or "")
            if tid:
                occurrences.setdefault(tid, []).append(entry)
        changed = False
        fresh: list[dict] = []
        for entry in entries:
            if entry.get("state") != RECURRING:
                continue
            existing = occurrences.get(str(entry["id"]), [])
            if any(o.get("state") in (PENDING, SENDING) for o in existing):
                continue
            base = now
            for occurrence in existing:
                try:
                    when = parse_due(occurrence.get("due"))
                except ValueError:
                    continue
                base = max(base, when)
            catch_up = _catch_up_base(entry, existing, now)
            if catch_up is not None:
                base = catch_up
            try:
                next_due = _next_template_due(entry, base)
            except ValueError as exc:
                entry["state"] = ERROR
                entry["error"] = f"recurring schedule stopped: {exc}"
                announce.append((EVENT_FAILED, dict(entry), entry["error"]))
                changed = True
                continue
            if next_due is None:
                # The series is over (its `count` is used up, or its `until` has
                # passed). The template stays RECURRING with nothing ahead of it
                # rather than acquiring a new state: it is still a schedule, it
                # has simply run out of dates, and `upcoming` says so by being
                # empty. A terminal state here would also make the row jump from
                # the live half of the listing to the handled half at a moment
                # nothing actually happened.
                continue
            occurrence = {
                "id": next_due.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex(),
                "target": entry.get("target", ""),
                "message": entry.get("message", ""),
                "due": next_due.isoformat(),
                # THE THREADING DECISION, and the one line where
                # `new_task_each_run` does its whole job.
                #
                # A task is a Claude session, so a repeating message appending
                # into one thread is what inheriting the template's session id
                # gets — chaining is the default by construction, with no
                # separate flag for it. Ticking "new task each run" is the
                # opposite ask, and "" is exactly how the rest of this module
                # already spells it: `_send` passes the empty string to
                # `spawn_helper`, which starts a fresh session rather than
                # resuming, and `_busy_sessions` treats "" as colliding with
                # nothing, so independent runs are not serialised against each
                # other the way one thread's turns must be.
                #
                # A template created from the Tasks page has NO session id to
                # inherit, and its first occurrence therefore starts one. That
                # is not a hole in the default, it is how the default begins:
                # `_chain_session` writes the session that run reported back
                # onto the template, so this line has a thread to hand run 2.
                "session_id": "" if _flag(entry.get("new_task_each_run"))
                              else str(entry.get("session_id") or ""),
                # The id's provenance travels with the id, on the same
                # condition — an occurrence that inherited nothing has nothing
                # to be the provenance OF. Copied for the same reason
                # `new_task_each_run` below is: an occurrence reads the same
                # shape as any other entry.
                "session_learned": False if _flag(entry.get("new_task_each_run"))
                                   else _flag(entry.get("session_learned")),
                "permission_mode": entry.get("permission_mode")
                                   or _SCHEDULED_PERMISSION_MODE,
                # The model and effort the user picked travel with every run
                # too: `_send` reads them off the occurrence it fires, so a
                # template that kept them to itself would run every repeat on
                # the default (bugbot, PR #968).
                "model": str(entry.get("model") or ""),
                "effort": str(entry.get("effort") or ""),
                # The user's words travel with every run, like the message and
                # the target: an occurrence is that template's run, so a list
                # showing occurrences must be able to name it without going back
                # to the template for the label.
                "title": _text(entry.get("title")),
                "description": _text(entry.get("description")),
                # The attachments travel with every run for the same reason the
                # words above do: an occurrence IS that template's run.
                "images": list(entry.get("images") or []),
                # …and their names/kinds with them, so the occurrence's own
                # `<pane-shot>` block reads like the template's would. Derived
                # for a template stored before this field existed, which is the
                # same fallback `_attachments` takes on the wire.
                "attachments": _stored_attachments(entry),
                # Carried so an occurrence reads the same shape as any other
                # entry. It is the TEMPLATE's answer that decided the session id
                # above; copying it keeps the record of which way that went.
                "new_task_each_run": _flag(entry.get("new_task_each_run")),
                # NEVER INHERITED, and spelled out rather than left to default.
                # A skip is a decision about ONE waiting run — "this one, next"
                # — and a template that had one of its occurrences skipped
                # would otherwise mint every future run at the head of its
                # folder's line for ever, which nobody asked for and nobody
                # could see they had asked for.
                "priority": False,
                # …and neither is the moment somebody pressed Run now on an
                # earlier occurrence, for exactly the same reason.
                "run_now_at": "",
                "state": PENDING,
                "repeats": "",
                "template_id": str(entry["id"]),
                # The one run a rule created with an anchor already in the past
                # catches up on (see `_catch_up_base`): the latest slot at or
                # before now, overdue the instant it exists, so it goes on the
                # next tick the way a past-dated one-off does. Recorded rather
                # than inferred because nothing later can tell — an occurrence
                # this old is otherwise indistinguishable from one the app slept
                # through, and the two are different news to a reader.
                "catch_up": catch_up is not None,
                # NO `max_late`. An occurrence used to carry 120s — the
                # skip-not-catch-up bound — and `_coalesce` is what replaced it:
                # a missed recurring run is no longer discarded, it is collapsed
                # into the latest one and sent. An occurrence an older version
                # wrote still carries the field, and `_entry_bound` still honours
                # it until coalescing clears it.
                "created": now.isoformat(),
                "fired": "",
                "run_id": "",
                "error": "",
                "turn": "",
                "turn_at": "",
                "claude_session_id": "",
            }
            fresh.append(occurrence)
            # The template's own `due` mirrors its next occurrence — it is what
            # the listing sorts and shows for the recurring row.
            entry["due"] = next_due.isoformat()
            if isinstance(entry.get("rule"), dict):
                # Incremented HERE, with the occurrence, and never anywhere
                # else: `made` is "how many did this template put on the
                # calendar", so the write that creates one is the only write
                # that may move it.
                entry["made"] = _made(entry) + 1
            changed = True
        if changed:
            entries.extend(fresh)
            _write(entries)
    for kind, entry, detail in announce:
        _emit(kind, entry, detail)
    if changed:
        _sync_wake()


def _upcoming_rule(entry: dict, spec: dict, horizon_days: int,
                   limit: int) -> list[str]:
    """`upcoming` for a structured rule. Same contract, different arithmetic —
    and one real difference, which is how an END is honoured.

    `until` needs nothing special: the walk stops at it. `count` must agree
    with MATERIALIZATION, which spends the budget only on occurrences it
    actually creates — theoretical runs between a past anchor and now were
    never made and cost nothing (a past anchor is a legitimate phase, per
    create()). So the projection is built the way the store will act: the
    latest materialized occurrence (mirrored on the template's own `due`)
    when it is still ahead, then exactly the `count - made` occurrences the
    sweep will still create after it. Numbering the theoretical series from
    the anchor looked equivalent and was not (Bugbot, PR #541): with a past
    anchor it billed the budget for runs that never existed and the calendar
    under-drew the series' tail."""
    try:
        anchor = _local_naive(parse_due(entry.get("anchor") or entry.get("due")))
    except ValueError:
        return []
    now = _local_naive(_now())
    end = now + timedelta(days=horizon_days)
    count = spec.get("count")
    times: list[str] = []
    if isinstance(count, int):
        made = _made(entry)
        remaining = max(0, count - made)
        # The template's `due` mirrors its latest occurrence (written at
        # materialize). Ahead of now it IS the next run and leads the
        # projection; the ghost dedupe on the client drops it again if that
        # occurrence was skipped, so including it cannot double-draw.
        latest = None
        if made:
            try:
                latest = _local_naive(parse_due(entry.get("due")))
            except ValueError:
                latest = None
        if latest is not None and now < latest <= end:
            times.append(_from_local(latest).isoformat())
        if remaining == 0:
            # occurrences() clamps limit AFTER appending, so a 0 must not
            # reach it — and a spent series has nothing to walk for anyway.
            return times
        after = latest if (latest is not None and latest > now) else now
        for when in recur.occurrences(spec, anchor, after, remaining):
            if when <= after:
                continue
            if when > end or len(times) >= limit:
                break
            times.append(_from_local(when).isoformat())
        return times
    for when in recur.occurrences(spec, anchor, now, limit):
        if when <= now:
            continue  # a run already behind us; the store has its own record
        if when > end:
            break
        times.append(_from_local(when).isoformat())
        if len(times) >= limit:
            break
    return times


def upcoming(entry: dict, horizon_days: int = 14, limit: int = 500) -> list[str]:
    """Projected occurrence times (UTC ISO) for a recurring template, `now`
    forward — what lets the calendar draw future runs without the client
    growing a cron parser (or, now, a recurrence engine). Projection only:
    nothing here is stored, and an unreadable schedule projects as nothing
    rather than raising into a listing.

    The cap must clear the horizon for the schedules the FORM offers, or the
    calendar lies: hourly over 14 days is 336 instants, and the first cut's
    cap of 50 blanked the week view two days out. 500 covers every preset
    with room; a deliberately denser custom line (every minute) hits the cap
    early, which is the honest trade against a megabyte of ISO strings on a
    listing poll."""
    spec = entry.get("rule")
    if isinstance(spec, dict):
        try:
            spec = recur.validate_rule(spec)
        except ValueError:
            return []
        return _upcoming_rule(entry, spec, horizon_days, limit)
    try:
        rule = cron.parse(str(entry.get("repeats") or ""))
    except ValueError:
        return []
    cursor = _local_naive(_now())
    end = cursor + timedelta(days=horizon_days)
    times: list[str] = []
    while len(times) < limit:
        try:
            cursor = rule.next_after(cursor)
        except ValueError:
            break
        if cursor > end:
            break
        times.append(_from_local(cursor).isoformat())
    return times


# ------------------------------------------------------- the folder, one at a time
#
# The per-SESSION hold below (`_busy_sessions`, `_session_live`) stops two
# processes appending to one transcript. It says nothing about two DIFFERENT
# conversations editing one working tree at the same second, which is the thing
# the project queue exists to stop. Both rules apply with the flag on and the
# session rules are untouched by it: this is a second gate in front of the same
# dispatch, never a replacement for the first.


def _folder_free(holder: dict | None, session: str) -> bool:
    """May an entry whose conversation is `session` run in a folder whose
    holder is `holder`?

    Mirrors `project_queue.is_free` deliberately — same rule, applied to a
    holder map computed ONCE for the whole pass instead of re-derived per entry
    (that derivation scans the runs tree). Keep the two in step.

    A free folder is free. A folder this very session is already holding is
    free too: a second message into a conversation that is running is the
    inbox-absorb case the chat has always had, and it is the session rules
    below — not this one — that decide what to do with it. An entry with NO
    session id can never be the holder, so it always waits; that is what stops
    two brand-new tasks claiming one folder in a single pass."""
    if holder is None:
        return True
    session = str(session or "")
    return bool(session) and session in (holder.get("session_id"),
                                         holder.get("task_key"))


def _follow_session(entry: dict, by_id: dict) -> tuple[str, bool]:
    """`(the conversation this entry should resume, may it go yet)` — the
    dispatch half of `follow_of`.

    A message typed into a chat whose FIRST message is still queued names that
    first message instead of a session, because there is no session yet. Three
    answers, and each is the leader's state read plainly:

    * **the leader has RUN** (`claude_session_id`) — go, resuming it. That id is
      written onto this entry as it is claimed (`_claim`), which is the one-off
      twin of `_chain_session` feeding a template's next run: the answer of one
      run becomes the input of the next, across two entries.
    * **the leader has not run yet** — pending, claimed for sending, or sent
      with its turn still open — HOLD. Left `pending` and untouched like every
      other hold here. The folder rule mostly does this already (the leader is
      usually the holder), but it is stated here because the two messages are
      ONE task: a folder freed by something else must not let the second message
      of a conversation open the conversation.
    * **the leader ended without a session** — cancelled, missed, or a send that
      broke before Claude Code minted one — run FRESH. The message is still
      owed, and there is no thread to continue; an orphan that waited for ever
      would be this feature losing the user's words.

    The IMMEDIATE leader, not the head of the chain: a follower of a follower is
    held by its own leader, which is in turn held by its own, so the order falls
    out one link at a time. An erased leader means the entry stands alone —
    exactly what it did before the field existed, and so does a chain too long
    to file (`leader_of` answers None past `FOLLOW_HOPS`) — dispatch must agree
    with the filing, or a message would wait on a leader whose row it is not
    even on."""
    leader_id = str(entry.get("follow_of") or "")
    if not leader_id:
        return "", True
    leader = by_id.get(leader_id)
    if leader is None or leader_of(entry, by_id) is None:
        return "", True
    ran = str(leader.get("claude_session_id") or "")
    if ran:
        return ran, True
    state = str(leader.get("state") or "")
    if state in (PENDING, SENDING) or (state == SENT and not leader.get("turn")):
        return "", False
    return "", True


def _claimed_holder(session: str, entry_id: str) -> dict:
    """The holder record this pass's own claim installs, so the entries after
    it in the same pass see the folder as taken.

    The same move as `busy.add(session)` one line below, and needed for the
    same reason: the claim is on disk as `sending` but `holders` was derived
    before it, and re-deriving per entry would cost a runs-tree scan each time."""
    from fused_render import tasks_store

    return {"session_id": session, "run_id": "",
            "task_key": session or tasks_store.pending_key(entry_id),
            "kind": "claimed"}


def _rehold(pq, answer: dict, why: str) -> None:
    """Put one popped answer back where it was — `at` and all.

    Its own `at` is passed rather than left to default, which is the whole
    point: `at` is the place in the line, and a record re-held with a fresh
    stamp goes to the back of a line it was at the front of (round-3 review,
    2026-09-12)."""
    try:
        pq.hold_answer(answer["queue_key"], answer["session_id"],
                       answer["run_id"], answer["request_id"],
                       answer["payload"], at=answer["at"])
    except Exception:  # noqa: BLE001 — a re-hold that fails is one lost answer
        logger.debug("could not re-hold answer %s/%s (%s)", answer["run_id"],
                     answer["request_id"], why, exc_info=True)


def _deliver_held_answers(holders: dict) -> None:
    """Hand every card decision that was parked on a busy folder to its run,
    now that the folder is free — and hold the folder for the rest of the pass.

    **Before any message is dispatched**, which is the whole ordering: a held
    answer is at the head of its folder's line by definition (project_queue's
    docstring: the run is already open, the user already decided, and the work
    it is mid-way through is older than anything queued behind it). Delivering
    after a message had claimed the folder would leave the answer waiting on a
    turn that started after the user answered it.

    `agent._decide` does the delivery, NOT a decision file written from here.
    `_decide` owns the validation this module has no business restating —
    narrowing a session grant to the tools its card offered, the question-card
    answers, the "keep planning" sentence on a plan deny, and the dead-run
    verdict — and a second copy of that would drift the first time one of them
    changed.

    **One conversation per folder per pass.** Every answer popped for a folder
    belongs to the run the user was answering, and a run may have several cards
    open at once — so they all go. Answers held for a DIFFERENT session in the
    same folder are put straight back: resuming two parked runs into one
    working tree is precisely what this feature exists to prevent, and the next
    pass (or the next free moment) delivers them.

    Every delivery is wrapped: one answer whose run has gone strange must not
    cost the tick the messages it was about to send — and an answer whose
    `_decide` raised is put back with the `at` it had (`_rehold`), because the
    pop that keeps two ticks from delivering one answer twice happens before
    the call and would otherwise be where the user's decision went."""
    pq = _pq()
    agent = pq.agent_module()
    if agent is None:
        return
    try:
        # Cheap, and the store is about to be trusted: an answer whose run died
        # while the folder was busy is written out as `expired` here rather than
        # delivered to a corpse.
        pq.validate_held_answers(agent)
    except Exception:  # noqa: BLE001 — a failed sweep is not a failed tick
        logger.debug("could not validate the held answers", exc_info=True)
    try:
        waiting = pq.held_answers()
    except Exception:  # noqa: BLE001
        logger.debug("could not read the held answers", exc_info=True)
        return
    for key in sorted({a["queue_key"] for a in waiting if a["queue_key"]}):
        if holders.get(key) is not None:
            continue
        try:
            taken = pq.pop_held_answers(key)
        except Exception:  # noqa: BLE001
            logger.debug("could not take the held answers for %s", key,
                         exc_info=True)
            continue
        if not taken:
            continue
        # Oldest first by `at`, so the session that has been waiting longest is
        # the one that resumes; the rest of the folder's answers go back with
        # the `at` they came out with, keeping the place they had in the line.
        session = taken[0]["session_id"]
        for answer in taken:
            if answer["session_id"] != session:
                _rehold(pq, answer, "another session holds this folder's turn")
                continue
            try:
                # The ORIGINAL decide arguments, replayed. See the docstring:
                # `_decide` is the validation, not a formality in front of it.
                agent._decide(**(answer["payload"] or {}).get("raw", {}))
            except Exception:  # noqa: BLE001 — one bad answer, not one bad tick
                logger.debug("could not deliver held answer %s/%s",
                             answer["run_id"], answer["request_id"],
                             exc_info=True)
                # PUT BACK, NOT DROPPED (round-3 review, 2026-09-12). The pop
                # is the latch that stops two ticks delivering one answer, so
                # it has to happen before the call — which means a `_decide`
                # that raises (a store mid-write, a run dir that vanished under
                # it) used to take the user's decision with it and leave the
                # card asking for ever. The folder is held for the rest of this
                # pass either way, so the next pass simply tries again; a
                # failure that repeats repeats a log line and nothing worse,
                # and `_decide` itself is what writes `expired` once the run is
                # properly gone.
                _rehold(pq, answer, "it could not be delivered")
        # Held for the rest of the pass whether or not the write landed: the
        # answers are out of the store and that run is the folder's business
        # now. A message dispatched in behind it would be the second process in
        # the tree this whole gate exists to prevent.
        holders[key] = {"session_id": session, "run_id": taken[0]["run_id"],
                        "task_key": session, "kind": "resumed"}
        _notify({session})


def tick(now: datetime | None = None) -> list[dict]:
    """One pass: sweep, then claim-and-send each due message ONE AT A TIME.

    The claim happens inside this loop rather than in the sweep so that a
    process dying inside one helper leaves its siblings `pending` — still
    sendable on the next tick — instead of stranded mid-claim (see `_claim_due`).

    **One send at a time per session, whoever is holding it.** A spawn returns as
    soon as the detached process is away, not when the turn ends, so without this
    two messages that resume the SAME session — two "in 5 minutes" landing in one
    tick, or a follow-up coming due while an earlier one is still working — would
    run concurrent `claude --resume` processes over one transcript.

    Two things make a session busy and both are checked here, because the
    scheduler is not the only thing that can be talking to it:

    * **a scheduled send in flight** (`_busy_sessions`), read from this module's
      own store;
    * **a live turn**, read from the transcript (`_session_live`). That covers
      the user typing in the explorer's chat, which the store cannot see and
      which used to be a stated known gap here. The transcript records the turn
      without recording who started it, which is exactly the property needed —
      and the one thing it cannot tell apart on its own is a turn that just
      ENDED from one still going, which is what `_verdict_echo` settles with
      this module's own record of the verdict.

    **One send at a time per FOLDER, with the project queue on.** A second gate
    in front of the same dispatch, not a replacement for the first: two
    different conversations editing one working tree is not parallelism, and
    the session rules above cannot see it because the two have no session in
    common. The holder map is derived once per pass (`project_queue.holders`)
    and moved forward by this pass's own claims, and held card answers are
    delivered before any message so a run already open in a folder resumes
    ahead of work that has not started. With the flag off none of it runs and
    the pass is byte-for-byte the one that shipped.

    **A follower waits for the chat it was typed into.** With the flag on, an
    entry carrying `follow_of` and no session of its own resolves the
    conversation it joins from its leader at the moment it is claimed
    (`_follow_session`, written by `_claim`): the leader's session if it has
    opened one, a hold if it has not, a fresh session if it ended without one.
    Two messages typed into one chat are one task and one conversation, in the
    order they were typed.

    **Deferred, never dropped.** An entry targeting a busy session (or a busy
    folder) is left `pending` and untouched — no state is written for it at all
    — so a later tick sends it once the turn ends. Catch-up is unbounded by default, so waiting
    costs it nothing; only an install that set `FUSED_RENDER_SCHEDULE_MAX_LATE`
    can eventually see one swept to `missed`, which is that operator's bound
    doing what they asked and is the same answer the hold has always given.

    **A hold that ends on a clock asks to be woken.** Most holds end on an event
    that already rings the loop (`_turn_ended`, the watcher seeing a holder's
    process go). Two do not — a transcript that reads live until its window
    lapses, and a `starting` or `reserved` folder holder — so a pass that held
    for one of those arms one timer for the moment the earliest of those clocks
    actually runs out (`_rearm`) rather than leaving the work to the 30-second
    poll.

    Returns the entries actually claimed and attempted, which is the seam the
    tests drive directly instead of waiting on the loop."""
    now = now or _now()
    sent: list[dict] = []
    # Coalesce first, materialize second, sweep third — and that order is the
    # one thing about this sequence worth stating.
    #
    # `_coalesce` collapses a recurring backlog into the one run that should go,
    # possibly MOVING that occurrence's due time forward, so it has to finish
    # before anything reads a due time. `_materialize` then keeps exactly one run
    # ahead of every template (it sees the coalesced state and correctly leaves
    # a template alone while its survivor is still pending). Only then does the
    # sweep look, so an occurrence coming due THIS tick exists by the time it
    # does, and a finished occurrence's successor is created above and correctly
    # ignored below until its own time comes.
    _coalesce(now)
    _materialize(now)
    due = _claim_due(now)
    # ONE derivation for the whole pass. `holders` scans the runs tree, the live
    # registry and this store; asking it per due entry would pay that per
    # message, and both the answers and this pass's own claims then move it
    # forward by hand exactly the way `busy` is moved forward.
    #
    # Computed BEFORE the early return, because a held answer is work too: a
    # user who answered a card while the folder was busy is owed that delivery
    # whether or not any message happens to be due in the same pass.
    folders = _pq().holders(now.timestamp()) if _pq().enabled() else None
    if folders is not None:
        _deliver_held_answers(folders)
    if not due:
        return sent
    with _lock:
        entries = _read()
    busy = _busy_sessions(entries)
    live_seen: dict[str, bool] = {}
    echo_seen: dict[str, bool] = {}
    # How long each hold made for a reason that expires on ITS OWN CLOCK —
    # rather than on an event anything rings — has left to run. The earliest is
    # what this pass asks to be woken for; see `_rearm`.
    held_soon: list[float] = []
    sessions = {str(e["id"]): str(e.get("session_id") or "") for e in entries}
    targets = {str(e["id"]): str(e.get("target") or "") for e in entries}
    by_id = {str(e.get("id") or ""): e for e in entries}
    for entry_id in due:
        session = sessions.get(entry_id, "")
        # The conversation to WRITE onto a follower as it is claimed, "" for
        # everything else. Resolved before either gate below, because both of
        # them are about a session and a follower does not know its own until
        # its leader has one.
        resolved = ""
        if folders is not None and not session:
            resolved, ready = _follow_session(by_id.get(entry_id) or {}, by_id)
            if not ready:
                # The message this one was typed behind has not opened the
                # conversation yet. A wait, never a verdict — same as every
                # other hold in this loop.
                logger.debug("holding %s: the message it follows has not run "
                             "yet", entry_id)
                continue
            session = resolved
        key = ""
        if folders is not None:
            key = _pq().queue_key(targets.get(entry_id, ""))
            # `""` is "no folder" — a target this module will not gate ($HOME,
            # `/`, a relative path). Never held, always free, exactly as
            # `project_queue.is_free` answers for it: a task the app cannot
            # place must run rather than queue behind something it cannot name.
            holder = folders.get(key) if key else None
            if not _folder_free(holder, session):
                # Another conversation is editing this working tree. Left
                # PENDING and untouched, like every other hold here: a wait,
                # never a verdict.
                logger.debug("holding %s: folder %s busy with %s", entry_id,
                             key, (holder or {}).get("task_key", ""))
                if (holder or {}).get("kind") in ("starting", "reserved"):
                    # Both are grace periods with a clock on them (a spawn that
                    # has not named itself, an admission whose process has not
                    # appeared): nothing rings when they lapse, so this pass
                    # asks to be woken when they do. See `_rearm`.
                    left = _pq().holder_expires_in(key, holder, now.timestamp())
                    if left:
                        held_soon.append(left)
                continue
        if session and session in busy:
            logger.debug("holding %s: session %s already has a send in flight",
                         entry_id, session)
            continue
        if (session and _session_live(session, now, live_seen)
                and not _verdict_echo(session, entries, now, echo_seen)):
            # A turn is open in that conversation and it is not one of ours —
            # the user is typing. Left PENDING, so this is a wait and not a
            # verdict; the next tick after the turn ends sends it.
            #
            # …unless what the transcript is showing is the closing rows of a
            # turn we have ALREADY filed a verdict for, which is the one case
            # where the 45-second window holds a message against the very turn
            # it is waiting for (`_verdict_echo`).
            logger.debug("holding %s: session %s has a live turn", entry_id,
                         session)
            left = _live_expires_in(session, now)
            if left:
                held_soon.append(left)
            continue
        entry = _claim(entry_id, now, resolved)
        if entry is None:
            continue  # cancelled in the window between the sweep and the claim
        if session:
            # This tick's own sends count too, or two entries due in the same pass
            # would both pass the check above.
            busy.add(session)
        if folders is not None and key:
            # …and so does the folder it went into, for the same reason one
            # line up: two entries into one working tree in one pass is the
            # case the whole gate is for.
            folders[key] = _claimed_holder(session, entry_id)
        sent.append(entry)
        _send(entry)
    _rearm(held_soon)
    if folders is not None and sent:
        # The runs tree has just gained a process this pass authorised, and the
        # walk behind `holders` is memoized for a second (project_queue.SCAN_TTL).
        # Dropping it here means the next reader — the listing a notify is about
        # to wake — sees the folder as busy rather than as free for a beat.
        _pq().invalidate_holders()
    return sent


def _run_now_refusal(entry: dict) -> str:
    """Why this entry cannot be run now, in the words the user needs.

    One sentence per terminal state rather than one "not pending" for all of
    them: the Board's drag is a physical gesture the user believes in, and
    "already sent" and "you cancelled this" are different pieces of news."""
    state = str(entry.get("state") or "")
    if state == SENDING:
        return ("already sending — it was claimed for sending a moment before "
                "this arrived")
    if state == SENT:
        return "already sent"
    if state == CANCELLED:
        return "cancelled — restore it before it can run"
    if state == MISSED:
        return "already missed — it was past its catch-up bound"
    if state == ERROR:
        return f"already tried and failed: {entry.get('error') or 'unknown error'}"
    if state == RECURRING:
        return ("that is a repeating schedule, not a single message — run one "
                "of its occurrences instead")
    return f"not pending (it is {state or 'in an unknown state'})"


def _queue_position(entry: dict, key: str, now: datetime) -> int:
    """Where `entry` stands in its folder's line, 1-based.

    Counted in the order the line actually moves (`project_queue.order_key`)
    over the same set the tick will consider — past-due pending entries whose
    target resolves to this folder — plus the answers held for it, which are
    ahead of every message by definition (see `_deliver_held_answers`).

    A number, not a promise: it is true as of this read, and the entry the user
    just skipped can be overtaken by another skip a second later. That is why
    the Tasks page re-reads it on every poll rather than counting down."""
    pq = _pq()
    try:
        ahead = len([a for a in pq.held_answers() if a["queue_key"] == key])
    except Exception:  # noqa: BLE001 — an unreadable store holds nothing
        ahead = 0
    entry_id = str(entry.get("id") or "")
    with _lock:
        entries = _read()
    # The same index every other reader of the line builds, so a follower is
    # ranked behind the message it was typed behind here too.
    by_id = {str(e.get("id") or ""): e for e in entries}
    mine = pq.order_key(entry, by_id)
    for other in entries:
        if other.get("state") != PENDING or str(other.get("id") or "") == entry_id:
            continue
        try:
            if _queue_due(other, parse_due(other.get("due"))) > now:
                continue
        except ValueError:
            continue
        if pq.queue_key(str(other.get("target") or "")) != key:
            continue
        if pq.order_key(other, by_id) < mine:
            ahead += 1
    return ahead + 1


def _asked_now(entry: dict, now: datetime) -> dict:
    """Stamp `run_now_at` on one entry and hand back the stored copy.

    The whole of what a deferred Run now leaves behind. Both arms that defer
    write it — a busy FOLDER (a skip, `_run_now_queued`) and a busy SESSION (a
    refusal that promises the ordinary tick will send it) — because both make
    the same promise about a message whose `due` may be days away, and only one
    of them used to be able to keep it.

    Written under the flag only, like every other queue field the client can
    produce: with the queue off the busy-session arm's promise is the one it has
    always made, and the 30-second poll that keeps it is unchanged.

    Idempotent in the way that matters: pressing Run now twice re-stamps a
    moment that is already in the past, and the line's order reads the earlier
    of `due` and this, so nothing moves."""
    if not _pq().enabled():
        return entry
    entry_id = str(entry.get("id") or "")
    stamp = now.isoformat()
    _update(entry_id, run_now_at=stamp)
    with _lock:
        stored = next((e for e in _read()
                       if str(e.get("id") or "") == entry_id), None)
    return stored or dict(entry, run_now_at=stamp)


def _run_now_queued(entry: dict, session: str, now: datetime) -> dict | None:
    """Run-now's answer when another task holds this entry's folder, or None
    when the folder is this entry's to run in (or the flag is off).

    Everything here is `run_now`'s docstring in code: nothing is claimed, the
    entry is SKIPPED to the head of its folder's line, and the caller is told
    who is in front so the row can say "behind TASK-041" rather than "behind
    something".

    `ahead_task_key` is the holder's TASK key — a session id, or `pending:<id>`
    for a claimed message that has not minted one yet — and it is the one the
    caller should name the holder by: a `sending` holder has no session at all,
    and asking the Tasks page for the number of `""` would print "behind "
    with nothing after it on exactly the rows that most need it.
    `ahead_session` stays beside it, unchanged, for a caller that wants the
    conversation rather than the task.

    `position` is this MODULE'S count — entries in front, plus the answers held
    for the folder — and the Tasks page counts the same line in tasks (one slot
    per task, however many messages it has queued). The two disagree by design
    and the router prefers the page's, because the page's is the number the user
    is reading everywhere else. See `server/routers/schedule.py`."""
    pq = _pq()
    if not pq.enabled():
        return None
    key = pq.queue_key(str(entry.get("target") or ""))
    holder = pq.holder_for(key, now.timestamp())
    if _folder_free(holder, session):
        return None
    entry_id = str(entry.get("id") or "")
    set_priority([entry_id], True)
    stored = _asked_now(dict(entry, priority=True), now)
    return {"ok": False, "found": True, "entry": stored, "reason": "queued",
            "queued": True, "position": _queue_position(stored, key, now),
            "ahead_task_key": str(holder.get("task_key") or ""),
            "ahead_session": str(holder.get("session_id") or ""),
            "ahead_run": str(holder.get("run_id") or "")}


def run_now(entry_id: str, now: datetime | None = None) -> dict:
    """Send one PENDING message immediately: `{"ok", "entry", "reason", "found"}`.

    What the Board's Upcoming -> In Progress drag means. Everything about it is
    the ordinary send brought forward; nothing about it is a second way to send.

    **`due` is not touched, and that is the point.** The obvious implementation
    rewrites the due time to now so the row "looks" consistent, and it destroys
    the only record of what was asked for: the schedule time is a fact about the
    ask, and a message that ran early is a message that ran early. The row reads
    `due` in the future and `fired` now, which is exactly what happened — and it
    is the same split `_entry_at` / `_entry_ran_at` draws on the Tasks side, so
    the calendar still draws the chip on the day the user picked.

    **What a DEFERRED run-now leaves behind is `run_now_at`, not a moved `due`.**
    The rule above held right up until this could answer "queued" instead of
    "sent": a message due tomorrow, skipped to the head of a busy folder's line,
    was in a line nothing could see it in — every reader asks "due ≤ now" and
    tomorrow is not, so the row read `upcoming` again the moment the optimistic
    paint cleared and the scheduler would have waited until tomorrow (browser
    QA, 2026-09-12). A second stamp is the answer: `due` stays the ask, and
    everything that asks "is this waiting to go right now" reads the earlier of
    the two (`_queue_due`, `project_queue.order_key`, `tasks._queue_at`). Both
    deferring arms write it — see `_asked_now`.

    **The claim is reused, not reimplemented.** `_claim` is the single
    `pending -> sending` transition in this module and it re-reads under the
    lock, so run-now and the tick race each other exactly the way two ticks
    would: one wins, the other is told the entry is no longer pending. Nothing
    can be sent twice, and there is no second spawn path to keep in step with
    claim-before-spawn.

    **A recurring occurrence runs alone.** An occurrence is an ordinary one-shot
    with a `template_id`; running it early leaves the template's `due`, its
    `made`, and its rule untouched, and `_materialize` computes the successor
    from that occurrence's own (unmoved) due time exactly as it would have if
    the occurrence had fired at its proper minute. One run happened sooner; the
    series did not move.

    **A busy session is refused, not forced.** If a turn is already open in the
    conversation this message resumes — one of ours, or the user's own typing —
    sending would put two processes on one transcript, which is the hazard
    `_session_live` exists for and is not one a drag gesture can consent to. The
    entry stays pending, stamped `run_now_at` so "the ordinary tick sends it
    when the conversation goes quiet" is true whatever its `due` says, and the
    reason says so. A turn this module has already filed a verdict for is not
    open, however fresh the transcript looks — `_verdict_echo`.

    **A busy FOLDER is a skip, not a refusal** (project queue on). Another task
    is editing this working tree, so this one cannot go now — but the gesture
    still means something exact, and it is not "nothing happened": Run now on a
    queued task is the Skip verb. The entry is marked `priority`, which puts it
    at the head of its folder's line, and stamped `run_now_at`, which is what
    puts it in that line at all when its `due` is still ahead; it comes back
    `ok: false` with
    `reason: "queued"`, `queued: true` and the position it now holds, so the
    row can read `#1 in line · behind TASK-041` instead of an error. It really
    does run next, within a second or two of that folder freeing (`wake`).

    `found` distinguishes "no such id" (a 404) from "cannot run this one"
    (a 409); the router is what turns them into status codes."""
    now = now or _now()
    with _lock:
        entries = _read()
        entry = next((e for e in entries
                      if str(e.get("id") or "") == entry_id), None)
        if entry is None:
            return {"ok": False, "found": False, "entry": None,
                    "reason": f"no scheduled message with id {entry_id!r}"}
        if entry.get("state") != PENDING:
            return {"ok": False, "found": True, "entry": dict(entry),
                    "reason": _run_now_refusal(entry)}
        session = str(entry.get("session_id") or "")
        busy = _busy_sessions(entries)
    queued = _run_now_queued(entry, session, now)
    if queued is not None:
        return queued
    if session and (session in busy
                    or (_session_live(session, now)
                        and not _verdict_echo(session, entries, now))):
        # "It will go on its own as soon as that turn ends" is a promise, and
        # for a message due next Tuesday it was not true: nothing would look at
        # it again until Tuesday. `_asked_now` is what makes it true — the
        # entry joins the line NOW while its `due` stays what was asked for.
        return {"ok": False, "found": True, "entry": _asked_now(entry, now),
                "reason": ("the conversation this message continues has a turn "
                           "running right now — it will go on its own as soon "
                           "as that turn ends")}
    claimed = _claim(entry_id, now)
    if claimed is None:
        # Lost the race with a tick (or another run-now) between the read above
        # and the claim. Refused rather than forced — the same answer
        # `cancel_queued` gives to the same race, and for the same reason.
        return {"ok": False, "found": True, "entry": None,
                "reason": ("already claimed for sending — the scheduler got to "
                           "it first")}
    _send(claimed)
    with _lock:
        stored = next((e for e in _read()
                       if str(e.get("id") or "") == entry_id), None)
    return {"ok": True, "found": True, "entry": stored or claimed, "reason": ""}


# ------------------------------------------------------------------ re-sending
#
# A message that RAN and broke has no pending entry — `run_now` claims a pending
# one, and the run that failed spent itself. So the Re-run affordance the user
# asked for could not be offered in the exact case they asked for it, and no
# amount of button wiring fixes that: the store had no verb for it.
#
# **The verb is not "run that row again", it is "ask again".** A task is a
# thread and asking for the work a second time is another MESSAGE in that
# thread, not a rewriting of the message that failed. Everything below falls out
# of that one sentence:
#
#   * the original entry is untouched — its `state`, `due`, `fired` and `error`
#     stay, so history keeps saying that run happened and broke. Same principle
#     as run-now not touching `due`: what was asked for, and what happened, are
#     facts and not fields to tidy;
#   * the new entry is an ordinary one-off `pending` at `due = now`, created by
#     `create` like any other message and sent by `run_now` like any other
#     early send. No second construction path and no second spawn path;
#   * it resumes the session the original actually RAN in
#     (`claude_session_id`), so the re-ask continues the conversation rather
#     than opening a second one beside it.

# The states a message may be re-sent FROM: a run that went and ended.
#
# `sent` covers every way a turn resolved — ok, failed, cancelled, unknown —
# because all of them are "it went", and a turn that ended badly is the ordinary
# reason to ask again. `error` is a send that never got off the ground, which is
# the same news one step earlier.
#
# A `sent` entry whose turn is still OPEN is deliberately included rather than
# refused: the new message simply queues behind it. `_busy_sessions` holds it
# until the turn ends, which is the serialisation two messages into one thread
# have always had, and is a better answer than a refusal the user would have to
# re-issue by hand a minute later.
RESENDABLE = (SENT, ERROR)


def _resend_refusal(entry: dict) -> str:
    """Why this entry cannot be re-sent, in the words the user needs — the
    counterpart of `_run_now_refusal`, and deliberately as specific.

    The two live states point AT run-now rather than away: "nothing happened"
    is not a reason to do nothing, it is a reason to use the other button.

    **`missed` and `cancelled` are refused, and for the same reason:** neither
    ever went, so there is no message to send *again*. `missed` is the sharper
    of the two — its commonest source is `_coalesce` dropping a repeat's stale
    runs, and a re-send button that replayed them one at a time would undo,
    click by click, the one rule that stops a week of "daily at 9am" landing in
    a thread on Monday morning. `cancelled` is a decision the user made; undoing
    it is `restore`'s job on the one cancel worth walking back (a skipped
    occurrence), not this one's. Both are told to schedule it again, which is
    the honest way to ask for work that never ran."""
    state = str(entry.get("state") or "")
    if state == PENDING:
        return ("not sent yet — this message is still scheduled, so there is "
                "nothing to send again; run it now, or let it go at its time")
    if state == SENDING:
        return ("already sending — it was claimed for sending a moment before "
                "this arrived; wait for that run before asking again")
    if state == CANCELLED:
        return ("cancelled — it never went, so there is nothing to send again; "
                "schedule it again instead")
    if state == MISSED:
        return ("never ran — a missed run was skipped rather than sent (only "
                "the latest missed run of a repeat goes), so re-sending it "
                "would replay work the schedule decided against; schedule it "
                "again if you want it now")
    if state == RECURRING:
        return ("that is a repeating schedule, not a message that ran — "
                "re-send one of its runs instead")
    return f"cannot be re-sent (it is {state or 'in an unknown state'})"


def resend(entry_id: str, now: datetime | None = None) -> dict:
    """Ask again: store the original's message as a NEW one-off due now and send
    it. `{"ok", "entry", "reason", "found"}`, where `entry` is the NEW entry.

    **The original is not modified in any way.** It is read, and that is all.
    A row that says "this ran at 09:00 and broke" goes on saying it.

    **The new entry continues the same thread.** `session_id` is copied from the
    original's `claude_session_id` — the session its turn actually ran in, which
    is the only field that knows — so the re-ask resumes that conversation. It
    is stamped `session_learned`, because that id was learned by the system from
    a run rather than chosen by a user, which is exactly the provenance
    `_chain_session` records when it teaches a template the same fact. An
    original that never reached a session (a send that failed before Claude
    Code minted one) copies "", and the new message opens a fresh thread — the
    only honest answer when there is no thread to continue.

    **`template_id` is NOT carried, and that is the load-bearing decision.** A
    re-send is a manual re-ask, not a scheduled run of the rule, and counting it
    as an occurrence would corrupt the series three ways at once: `_materialize`
    refuses to make a template's next run while any occurrence of it is pending,
    so a re-send would BLOCK the schedule for as long as it sat in the queue;
    `_coalesce` would read it as backlog and could move its due time or mark it
    missed; and cancelling the template would cascade onto it. The `count` /
    `until` budgets are the fourth: `made` measures what the template put on the
    calendar, and a button press never did. So the new entry is a plain one-off
    — the template's `made`, its `due` and its future occurrences are as
    untouched as the original entry is.

    **The claim path is reused, not reimplemented.** The new entry is created by
    `create` and sent by `run_now`, which claims through `_claim` like
    everything else. Claim-before-spawn is unchanged, and there is no second way
    to spawn to keep in step.

    `ok` is true once the new message EXISTS, not only when it went out
    immediately. If the conversation it resumes has a turn running, `run_now`
    refuses the early send and says so; the entry stays pending at the head of
    the queue and the ordinary tick sends it when the turn ends. Reporting that
    as a failure would be a lie about a message that is really scheduled — so it
    comes back in `reason` as a note beside `ok: true`.

    `found` distinguishes "no such id" (a 404) from "cannot re-send this one"
    (a 409), exactly as `run_now` does; the router maps them.

    Raises ValueError for a target that has since been deleted (`create`'s own
    validation), which the router turns into a 400."""
    now = now or _now()
    with _lock:
        original = next((e for e in _read()
                         if str(e.get("id") or "") == entry_id), None)
    if original is None:
        return {"ok": False, "found": False, "entry": None,
                "reason": f"no scheduled message with id {entry_id!r}"}
    if original.get("state") not in RESENDABLE:
        return {"ok": False, "found": True, "entry": dict(original),
                "reason": _resend_refusal(original)}

    session = str(original.get("claude_session_id") or "")
    fresh = create(
        str(original.get("target") or ""), str(original.get("message") or ""),
        now,
        session_id=session,
        # Copied rather than defaulted: the mode is a choice made per message
        # (see `_SCHEDULED_PERMISSION_MODE`), and asking the same thing again
        # under a different one would be a different ask.
        permission_mode=str(original.get("permission_mode") or ""),
        title=original.get("title"), description=original.get("description"),
        # The system learned this id from a run; nobody chose it. Same marker
        # `_chain_session` writes, for the same fact.
        session_learned=bool(session),
        # KEPT, where there is one: a re-ask of a message a chat queued is
        # still that chat's message, and the composer it belongs to should no
        # more be shut by the second ask than it was by the first. An original
        # with no origin copies "" and the new entry has none either.
        origin=str(original.get("origin") or ""))
    # Provenance, and the ONLY link between the two rows — the original is not
    # written to, so without this nothing records that the second message is a
    # re-ask of the first. Written after creation rather than threaded through
    # `create`: a re-send is the only caller that has anything to say here, and
    # a merge on a pending entry is safe whatever a tick does in between.
    _update(fresh["id"], resent_from=entry_id)
    outcome = run_now(fresh["id"], now)
    with _lock:
        stored = next((e for e in _read()
                       if str(e.get("id") or "") == fresh["id"]), None)
    entry = stored or dict(fresh, resent_from=entry_id)
    # A note only while it is still waiting. Once it has gone (or a tick got to
    # it first) `run_now`'s refusal describes a race that is over, and repeating
    # it to the user would report a problem they do not have.
    note = str(outcome.get("reason") or "") if entry.get("state") == PENDING else ""
    return {"ok": True, "found": True, "entry": entry, "reason": note}


def _loop() -> None:
    """Daemon-thread body: tick() on a timer — or as soon as something rings —
    forever. `tick` already keeps a per-entry failure on its entry, but wrap
    here too so nothing, not even an unreadable store, can kill the loop and
    take the schedule with it.

    `_wake.wait(POLL_INTERVAL_S)` in place of `time.sleep`: the timer is
    unchanged (an entry that comes due on its own is found by the next pass, as
    it always was) and the wait is what lets work stored ALREADY DUE go
    immediately — see `_wake`. Cleared after the wait and before the tick, so a
    ring that arrives while a tick is running wakes the pass after it."""
    while True:
        try:
            tick()
        except Exception:
            logger.exception("scheduled-message tick failed")
        _wake.wait(POLL_INTERVAL_S)
        _wake.clear()


def start() -> None:
    """Start the background loop. Idempotent — safe to call once at server
    startup; a redundant call while the thread is alive is a no-op.

    The FIRST tick is what catches up anything that came due while the app was
    closed, so this deliberately does not sleep before its first pass."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_loop, daemon=True,
                                   name="fused-schedule")
        _thread.start()
