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

# The modes agent.py accepts. Hardcoded rather than imported: importing it means
# loading the template backend (a module-level `exec_module`) on every
# validation, and this list is a four-word contract that a stricter mode would
# only ever be REMOVED from. `_start` re-validates anyway and falls back to the
# strictest on anything it does not recognise, so a drift here cannot buy a
# scheduled turn more auto-approval than the template offers.
PERMISSION_MODES = ("prompt", "auto", "plan")

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

    ONE locked read-modify-write for the whole tick: claiming and sweeping
    together means a due entry cannot be read as pending by a second caller
    between the two, and a tick that spawns nothing still persists its
    `missed` verdicts.

    Returned entries are copies already written as `sending`; the caller spawns
    for each and reports back through `_update`."""
    cutoff = now - timedelta(seconds=max_late_seconds())
    claimed: list[dict] = []
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
                    changed = True
                continue
            if state != PENDING:
                continue
            try:
                when = parse_due(entry.get("due"))
            except ValueError:
                entry["state"] = ERROR
                entry["error"] = f"unreadable due time: {entry.get('due')!r}"
                changed = True
                continue
            if when > now:
                continue
            changed = True
            if when < cutoff:
                entry["state"] = MISSED
                entry["error"] = ("not sent: the app was not running between "
                                 "this time and the catch-up bound")
                continue
            entry["state"] = SENDING
            entry["fired"] = now.isoformat()
            claimed.append(dict(entry))
        due_times = _pending_due(entries) if changed else None
        if changed:
            _write(entries)
    if due_times is not None:
        _sync_wake(due_times)
    return claimed


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
        _update(entry["id"], state=ERROR, error=f"failed to start session: {exc}")
        return
    run_id = res.get("run_id")
    if res.get("error") or not run_id:
        _update(entry["id"], state=ERROR,
                error=str(res.get("error") or "failed to start session"))
        return
    _update(entry["id"], state=SENT, run_id=str(run_id), error="")
    # Same reason the apps API does this: nothing else will poll the run, so
    # without this thread the session never reaches its sidecar and the finished
    # turn is never committed. Bookkeeping — it cannot fail the send.
    try:
        threading.Thread(
            target=claude_spawn.record_session_when_ready,
            args=(claude_spawn.load_agent(), str(run_id)),
            daemon=True, name="fused-schedule-session-record").start()
    except Exception:  # noqa: BLE001
        logger.debug("could not start the sidecar-recording thread", exc_info=True)


def tick(now: datetime | None = None) -> list[dict]:
    """One pass: claim what is due, send each claim. Returns the claimed
    entries (what this pass acted on) — the seam the tests drive directly
    instead of waiting on the loop."""
    claimed = _claim_due(now or _now())
    for entry in claimed:
        _send(entry)
    return claimed


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
