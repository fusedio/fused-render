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
* **A bound on how late is still worth sending** (`max_late_seconds`). Catch-up
  with no bound is its own bug: a message scheduled for Tuesday's 9am standup,
  fired unattended on Friday afternoon against a repo that has moved on, is
  worse than one that never fired. Past the bound an entry becomes `missed` —
  visible, never sent. The default is a day, because "I opened the laptop later
  than I meant to" is the case worth serving and "I was away all week" is not.

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

from fused_render import claude_spawn
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
STATES = (PENDING, SENDING, SENT, MISSED, ERROR, CANCELLED)

# How often the loop looks. A scheduled message is a minute-granularity promise
# at best (the user picks a wall-clock time, not a deadline), and a tick is one
# small JSON read, so this is chosen for "fires close enough to the stated
# minute" rather than for precision.
POLL_INTERVAL_S = 30

# How late an overdue message may still be sent. See the module docstring for
# why this is bounded at all; the env var is there because "a day" is a judgement
# about the user's habits, not a fact.
_DEFAULT_MAX_LATE_S = 24 * 3600
_MAX_LATE_ENV = "FUSED_RENDER_SCHEDULE_MAX_LATE"

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

_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def store_path() -> str:
    return os.path.join(storage.home_dir(), _STORE_NAME)


def max_late_seconds() -> int:
    """The catch-up bound, in seconds. A nonsense value falls back to the
    default rather than producing a scheduler that fires everything ever
    scheduled (0/negative) or nothing at all."""
    raw = os.environ.get(_MAX_LATE_ENV)
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return _DEFAULT_MAX_LATE_S
    return seconds if seconds > 0 else _DEFAULT_MAX_LATE_S


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
    same posture as every other registry here (linked_apps, recents): the
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


def _pending_due(entries: list[dict]) -> list[str]:
    return [str(e.get("due") or "") for e in entries if e.get("state") == PENDING]


def _sync_wake(due: list[str]) -> None:
    """Tell the OS-side wake stub which times still matter.

    **Called OUTSIDE `_lock`, always.** On macOS this shells out to `launchctl`
    twice, and holding the store lock across two subprocesses would make every
    tick able to stall a `GET /api/schedule` for as long as launchd takes to
    answer. Callers therefore snapshot the pending times under the lock and sync
    after releasing it.

    Best-effort by construction: the wake stub only makes the app more likely to
    be RUNNING at a due time, and everything about firing works without it. A
    platform that has no stub, or a dev checkout with no app bundle to relaunch,
    is a no-op — never an error that fails the write that got here."""
    try:
        from fused_render import schedule_wake

        schedule_wake.sync(due)
    except Exception:  # noqa: BLE001 — a wake stub must never break the schedule
        logger.debug("could not sync the schedule wake stub", exc_info=True)


def list_entries() -> list[dict]:
    """Every entry, soonest-due first, with the terminal ones after the live
    ones. What the UI lists; no side effects."""
    live = {PENDING: 0, SENDING: 1}
    return sorted(_read(), key=lambda e: (live.get(e.get("state"), 2),
                                          str(e.get("due") or "")))


def create(target: str, message: str, due, session_id: str = "",
           permission_mode: str = "") -> dict:
    """Validate and store one scheduled message; return the stored entry.

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

    when = due if isinstance(due, datetime) else parse_due(due)
    if when.tzinfo is None:
        when = when.astimezone()
    when = when.astimezone(timezone.utc)
    # A due time already past the catch-up bound would be stored only to be
    # swept to `missed` on the very next tick. Refusing it up front tells the
    # caller why, instead of accepting the message and quietly never sending it.
    if when < _now() - timedelta(seconds=max_late_seconds()):
        raise ValueError("due: further in the past than the catch-up bound "
                         f"({max_late_seconds()}s) — it would never be sent")

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
        "permission_mode": mode,
        "state": PENDING,
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
    }
    with _lock:
        entries = _read()
        entries.append(entry)
        _write(entries)
        due_times = _pending_due(entries)
    _sync_wake(due_times)
    return entry


def cancel(entry_id: str) -> dict | None:
    """Cancel a pending entry; return it, or None if there is no such pending
    entry. A `sending` entry is deliberately NOT cancellable — the helper is
    already away and the turn may have started, so "cancelled" would be a claim
    this module cannot make good on."""
    cancelled = None
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") == entry_id and entry.get("state") == PENDING:
                entry["state"] = CANCELLED
                _write(entries)
                cancelled = entry
                due_times = _pending_due(entries)
                break
    if cancelled is None:
        return None
    _sync_wake(due_times)
    return cancelled


def _update(entry_id: str, **fields) -> None:
    """Merge `fields` into one entry, re-reading under the lock so a concurrent
    cancel or create is not clobbered by a stale copy."""
    due_times = None
    with _lock:
        entries = _read()
        for entry in entries:
            if entry.get("id") == entry_id:
                entry.update(fields)
                _write(entries)
                due_times = _pending_due(entries)
                break
    if due_times is not None:
        _sync_wake(due_times)


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
    cutoff = now - timedelta(seconds=max_late_seconds())
    due: list[str] = []
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
            if when < cutoff:
                changed = True
                entry["state"] = MISSED
                entry["error"] = ("not sent: the app was not running between "
                                 "this time and the catch-up bound")
                announce.append((EVENT_MISSED, dict(entry), entry["error"]))
                continue
            # Due and sendable. Left PENDING — `_claim` takes it, one at a time.
            due.append(str(entry["id"]))
        due_times = _pending_due(entries) if changed else None
        if changed:
            _write(entries)
    for kind, entry, detail in announce:
        _emit(kind, entry, detail)
    if due_times is not None:
        _sync_wake(due_times)
    return due


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
            due_times = _pending_due(entries)
            break
        else:
            return None
    _sync_wake(due_times)
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


def _turn_tick(entry: dict, run_id: str, agent, data: dict) -> bool:
    """One observation of a live turn. False stops the watch.

    Does three things, in the order they matter: honour a cancel the user asked
    for, record the outcome once the turn ends, and otherwise say what the run is
    DOING. The last one is why `permissions` is checked first — a turn parked on
    a card nobody has answered looks identical to a slow one from the outside,
    and for an unattended session that is the single most likely way to be
    stuck."""
    entry_id = entry["id"]
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
    turn can outrun."""
    try:
        agent = claude_spawn.load_agent()
    except Exception:  # noqa: BLE001
        logger.debug("could not load the agent backend to watch a run", exc_info=True)
        _close_unwatched(entry, "could not read the run's progress")
        return
    claude_spawn.record_session_when_ready(
        agent, run_id,
        on_tick=lambda data: _turn_tick(entry, run_id, agent, data))
    # Back here means the poll loop is finished. If `_turn_tick` saw `done` it
    # already recorded the outcome and this is a no-op; anything else and the watch
    # ended without one.
    _close_unwatched(entry, "stopped reporting before the turn finished")


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
    with nothing."""
    busy = set()
    for entry in entries:
        session = str(entry.get("session_id") or "")
        if not session:
            continue
        state = entry.get("state")
        if state == SENDING or (state == SENT and not entry.get("turn")):
            busy.add(session)
    return busy


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
