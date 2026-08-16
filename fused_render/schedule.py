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
     at the head of the queue and goes on the next tick.

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


def store_path() -> str:
    return os.path.join(storage.home_dir(), _STORE_NAME)


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
    plain `_report(id)` with no fields is a legitimate "read it back" call."""
    try:
        from fused_render import jobs

        return jobs.upsert({"id": _job_id(entry_id), **fields}, server=True)
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
           new_task_each_run=None, session_learned=None) -> dict:
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

    Raises ValueError for everything a caller can get wrong (the router maps it
    to a 400). The one validation deliberately NOT here is "is this path
    mount-backed" — that needs the mounts registry, which lives above this
    module; the router refuses those before calling, exactly as the claude
    template's own gate does."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message: cannot be empty")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("target: required")
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.exists(target):
        raise ValueError(f"target: no such file or directory: {target}")

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
        # the phase that started last Monday" — nothing fires late for it,
        # because materialization only ever asks for occurrences after `now`.
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
        # The user's own words, both optional and both "" by default. Read
        # through `_text` because the router hands this module the request body
        # unvalidated, exactly as it does for every other field here.
        "title": _text(title),
        "description": _text(description),
        # Threading, not scheduling: whether each run of a REPEAT opens its own
        # session. Stored on one-shots too (as False) so every entry has the
        # same shape and the form reads it back the same way — a one-shot has
        # one run, so there is nothing for it to mean there.
        "new_task_each_run": _flag(new_task_each_run),
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
        # The Claude Code session this message's turn actually ran in, filled in by
        # the watcher from the run's first reporting tick. Distinct from
        # `session_id` above (the input) precisely so a fresh send does not end up
        # looking like a continuation, and it is what the page links to the Inbox
        # with — that app addresses a session by this id and nothing else.
        "claude_session_id": "",
    }
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
    with _lock:
        entries = _read()
        entries.append(entry)
        _write(entries)
    _sync_wake()
    if repeats or spec is not None:
        # First occurrence, immediately — so the schedule the user just wrote
        # is visible (and wake-synced) without waiting for the next tick.
        _materialize(_now())
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
    listed in one order and run in another is worse than no queue at all.
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
        if when > now:
            continue
        bound = _entry_bound(entry)
        if bound is not None and when < now - timedelta(seconds=bound):
            continue
        queued.append((when, str(entry.get("id") or ""), entry))
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
    due: list[tuple[datetime, str]] = []
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
            due.append((when, str(entry["id"])))
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
    due.sort(key=lambda pair: (pair[0], pair[1]))
    return [entry_id for _, entry_id in due]


def _claim(entry_id: str, now: datetime) -> dict | None:
    """Take ONE entry for sending: `pending` -> `sending`, written before the
    caller spawns anything. Returns the claimed copy, or None if it is no longer
    pending (cancelled between the sweep and here, or already taken).

    The re-read under the lock is what makes that None real rather than
    theoretical: the sweep's verdict is a moment old by the time we get here, and
    a cancel landing in that window must win."""
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") != entry_id:
                continue
            if entry.get("state") != PENDING:
                return None
            entry["state"] = SENDING
            entry["fired"] = now.isoformat()
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


def _send(entry: dict) -> None:
    """Spawn one claimed entry's session and record the outcome.

    Every failure lands on the ENTRY (state `error`, with the reason) rather
    than propagating: one bad target must not stop the rest of the tick, and a
    scheduled message that failed is exactly the thing the user needs to be able
    to read afterwards."""
    try:
        res = claude_spawn.spawn_helper(
            entry["target"], entry["message"], entry.get("permission_mode")
            or _SCHEDULED_PERMISSION_MODE, entry.get("session_id") or "")
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
    _update(entry["id"], state=SENT, run_id=str(run_id), error="")
    # The row opens `running` and stays that way for the whole TURN, not just the
    # spawn — the spawn takes a moment and the turn can take minutes, and the
    # minutes are the part worth being able to see. `cancellable` is honest here
    # in a way it is not for most reporters: this process can actually stop the
    # run (agent._cancel), so the manager's ✕ is an action.
    _report(entry["id"], title=_job_title(entry), kind="task",
            detail=entry["target"], state="running", cancellable=True)
    # Nothing else will poll the run, so without this thread the session never
    # reaches its sidecar and the finished turn is never committed — and, since
    # this feature added an observer, nobody would ever learn how the turn went.
    try:
        threading.Thread(
            target=_watch_turn, args=(dict(entry), str(run_id)),
            daemon=True, name="fused-schedule-session-record").start()
    except Exception:  # noqa: BLE001
        logger.debug("could not start the sidecar-recording thread", exc_info=True)
        # Nothing is watching and nothing will, so say so now rather than leave the
        # sweep to notice a turn it cannot distinguish from one abandoned by a dead
        # process — this one is abandoned in a live process, and immediately.
        _watching(entry["id"], False)
        _close_unwatched(entry, "could not start the watcher for this turn")


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
        if reason:
            _update(entry_id, turn="failed", error=reason)
            _report(entry_id, state="error", message=reason)
            _emit(EVENT_FAILED, entry, reason)
        else:
            _update(entry_id, turn="ok")
            _report(entry_id, state="done", detail="finished")
            _emit(EVENT_DONE, entry)
        return False

    parked = data.get("permissions") or []
    detail = "waiting for permission" if parked else str(data.get("phase") or "working")
    tokens = data.get("tokens") or 0
    if tokens and not parked:
        detail = f"{detail} · {int(tokens)} tokens"
    # One call: reporting the tick is also how the cancel flag is read back.
    record = _report(entry_id, detail=detail)
    if record and record.get("cancel_requested"):
        try:
            agent._cancel(run_id)
        except Exception:  # noqa: BLE001 — a cancel that fails is still a stop attempt
            logger.debug("could not cancel scheduled run %s", run_id, exc_info=True)
        _update(entry_id, turn="cancelled")
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
    owns the sidecar write and the commit, which must happen whether or not
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
    _update(entry_id, turn="unknown", error=reason)
    _report(entry_id, state="error", message=reason)
    _emit(EVENT_FAILED, entry, reason)


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
            while template is not None and steps < _COALESCE_MAX_STEPS:
                try:
                    following = _next_template_due(template, when)
                except ValueError:
                    # A hand-edited schedule that no longer parses. Left for
                    # `_materialize`, which is where that verdict is announced;
                    # the survivor still fires at the time it already has.
                    break
                if following is None or following > now:
                    break
                if following <= when:
                    # The walk must MOVE. Recurrence math is done on local
                    # wall-clock time (a repeat is a wall-clock promise), so the
                    # autumn fall-back — where one local hour happens twice —
                    # can hand back a later local time that converts to an
                    # earlier or equal UTC instant. Stepping on it would walk
                    # the same hour for as long as the cap allows. The repo
                    # already accepts a cosmetic DST ghost as out of scope; this
                    # only refuses to spin on one.
                    break
                when = following
                dropped += 1
                steps += 1
                if isinstance(template.get("rule"), dict):
                    template["made"] = _made(template) + 1
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


def _materialize(now: datetime) -> None:
    """Ensure every live recurring template has exactly ONE pending occurrence.

    Idempotent by construction, which is the whole trick: it does not remember
    what it did, it looks at what exists. A template whose occurrence is still
    `pending` or `sending` is left alone; one whose occurrence has finished
    (sent, missed, error, cancelled — any of them) gets the next one. The next
    time is computed from the LATEST occurrence ever materialized, not from
    `now`, so a run finishing early can never pull the next one earlier, and a
    cancelled occurrence stays skipped instead of being re-offered.

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
                # The user's words travel with every run, like the message and
                # the target: an occurrence is that template's run, so a list
                # showing occurrences must be able to name it without going back
                # to the template for the label.
                "title": _text(entry.get("title")),
                "description": _text(entry.get("description")),
                # Carried so an occurrence reads the same shape as any other
                # entry. It is the TEMPLATE's answer that decided the session id
                # above; copying it keeps the record of which way that went.
                "new_task_each_run": _flag(entry.get("new_task_each_run")),
                "state": PENDING,
                "repeats": "",
                "template_id": str(entry["id"]),
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


def tick(now: datetime | None = None) -> list[dict]:
    """One pass: sweep, then claim-and-send each due message ONE AT A TIME.

    The claim happens inside this loop rather than in the sweep so that a
    process dying inside one helper leaves its siblings `pending` — still
    sendable on the next tick — instead of stranded mid-claim (see `_claim_due`).

    **One send at a time per resumed session.** A spawn returns as soon as the
    detached process is away, not when the turn ends, so without this two messages
    that resume the SAME session — two "in 5 minutes" landing in one tick, or a
    follow-up coming due while an earlier one is still working — would run
    concurrent `claude --resume` processes over one transcript. Entries targeting a
    busy session are simply left `pending` and picked up by a later tick; they can
    in principle be deferred until the catch-up bound calls them `missed`, which is
    the honest outcome for "the conversation it belongs to never went quiet".

    KNOWN GAP, stated rather than implied: this serialises the SCHEDULER against
    itself. A scheduled resume can still land while the user's own interactive turn
    on that session is live, which this module cannot see — the chat owns that run,
    and the schedule store has no record of it.

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
    if not due:
        return sent
    with _lock:
        entries = _read()
    busy = _busy_sessions(entries)
    sessions = {str(e["id"]): str(e.get("session_id") or "") for e in entries}
    for entry_id in due:
        session = sessions.get(entry_id, "")
        if session and session in busy:
            logger.debug("holding %s: session %s already has a send in flight",
                         entry_id, session)
            continue
        entry = _claim(entry_id, now)
        if entry is None:
            continue  # cancelled in the window between the sweep and the claim
        if session:
            # This tick's own sends count too, or two entries due in the same pass
            # would both pass the check above.
            busy.add(session)
        sent.append(entry)
        _send(entry)
    return sent


def _loop() -> None:
    """Daemon-thread body: tick() on a timer, forever. `tick` already keeps a
    per-entry failure on its entry, but wrap here too so nothing — not even an
    unreadable store — can kill the loop and take the schedule with it."""
    while True:
        try:
            tick()
        except Exception:
            logger.exception("scheduled-message tick failed")
        time.sleep(POLL_INTERVAL_S)


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
