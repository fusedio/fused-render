"""Scheduled Claude messages: list, schedule, cancel.

The model — the store, the firing decision, the catch-up bound — is
`fused_render/schedule.py`; this is the HTTP skin over it. Two things live here
rather than there, both because they need what only this layer knows:

* **the mount refusal.** A scheduled turn is an agent turned loose on a path,
  and the bytes under the mounts dir come from a remote over FUSE. Every peer
  gate refuses those paths (the claude template's own `condition.py` exists for
  this single refusal), so scheduling a message against one would route around
  that gate. The mounts registry lives above the schedule module, so the check
  belongs on this side of the import.
* **ValueError -> 400.** The model raises with a message written for a human;
  the route is what turns that into a status code.

Reads are unguarded like every other read endpoint. Both POSTs carry the D3
X-Fused guard: one of them schedules code execution, and the other stops it.
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Header

from fused_render import recur, schedule
from fused_render.server.common import _error, _require_fused

router = APIRouter()


@router.get("/api/schedule")
def api_schedule():
    """Every scheduled message, live ones first.

    `max_late_seconds` rides along because the UI cannot explain a `missed`
    entry without it — the bound is configurable (FUSED_RENDER_SCHEDULE_MAX_LATE),
    so the number has to come from the server rather than be restated in the
    page. **It is now null by default**: catch-up is unbounded, missed work
    queues instead of expiring, and only an install that sets the env var can
    produce a `missed` one-off at all. Null rather than a sentinel number, so a
    page that shows the bound has to decide what to say when there is none
    rather than printing a made-up one."""
    entries = schedule.list_entries()
    for entry in entries:
        # Server-side recurrence math, so the calendar can draw a recurring
        # job's future runs without the client growing a cron parser or a
        # second copy of the rule engine. Projection only — the store holds
        # just the ONE materialized next occurrence. Both kinds of template
        # answer here: `upcoming` reads `rule` or `repeats`, whichever the
        # entry carries, and the entry itself ships both (plus `made`, which
        # is what an "ends after N" row needs to say how far along it is)
        # because the store's dicts are serialized as they are.
        if entry.get("state") == schedule.RECURRING:
            entry["upcoming"] = schedule.upcoming(entry)
    return {"entries": entries,
            "max_late_seconds": schedule.max_late_seconds(),
            "permission_modes": list(schedule.PERMISSION_MODES)}


@router.get("/api/schedule/events")
def api_schedule_events():
    """What scheduled messages did that nobody has been told about yet — what the
    shell polls to raise a toast for a message that ran, failed, or was missed
    while the user was elsewhere.

    A SEPARATE endpoint from the listing above, for the reason the mount-health
    log is separate: this one is polled app-wide, by every shell, forever, and
    making that poll carry the full entry list would be paying for the page's
    payload on a request that only ever reads a handful of ids.

    Undelivered-only, and the SERVER is what remembers which those are. The
    alternative — a client-side "first poll is a silent baseline", copied from the
    mount-health poller — is wrong for this log specifically: the catch-up pass
    emits its `missed` verdicts on the scheduler's first tick, long before a shell
    has loaded, so the baseline swallowed precisely the events the log exists to
    deliver."""
    return {"events": schedule.undelivered_events()}


@router.post("/api/schedule/events/ack")
def api_schedule_events_ack(body: dict = Body(...),
                            x_fused: str | None = Header(default=None)):
    """Confirm the shell has narrated every event up to `id`.

    Guarded like the other writes, and not folded into the GET above for exactly
    that reason: a drain-on-read would let any page the user visits silently
    consume their notifications with a no-cors fetch, which is the shape D3's
    header guard exists to refuse."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    event_id = body.get("id")
    if not isinstance(event_id, int) or isinstance(event_id, bool):
        return _error("id: expected an integer event id", status=400)
    return {"delivered": schedule.ack_events(event_id)}


@router.post("/api/schedule")
def api_schedule_create(body: dict = Body(...),
                        x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    target = body.get("target")
    if not isinstance(target, str) or not target.strip():
        return _error("target: required", status=400)

    # Refused before anything is stored — see the module docstring. Resolved the
    # same way the model will resolve it (expanduser + abspath), so the path this
    # check clears is the path that gets scheduled.
    #
    # Imported per call, not at module scope: binding the name at import would
    # freeze it past the mounts registry's own seams (the same reason the peer
    # gates resolve it late).
    from fused_render.shell.mounts import is_mount_backed

    resolved = os.path.abspath(os.path.expanduser(target.strip()))
    if is_mount_backed(resolved):
        return _error(
            "target: refused — a scheduled session must not run against a "
            "remote mount", status=400)

    # `delay_seconds` is the other way to say when: a page offering "in 30
    # minutes" should not have to do timezone arithmetic to say it. Exactly one
    # of the two, so a request carrying both cannot half-mean each. A `repeats`
    # cron line replaces both — it already says every time it means — so a
    # request carrying it alongside either would half-mean two schedules.
    #
    # A `rule` object is the third way, and the one that reads DIFFERENTLY: a
    # structured repeat counts from an anchor, so it needs `due` rather than
    # refusing it. What it cannot be combined with is the other two ways of
    # repeating and of saying "later" — `repeats` (a second schedule) and
    # `delay_seconds` (a relative anchor, which is not a date the user picked).
    due = body.get("due")
    delay = body.get("delay_seconds")
    repeats = str(body.get("repeats") or "").strip()
    rule = body.get("rule")
    if rule is not None:
        if not isinstance(rule, dict):
            return _error("rule: expected an object describing the repeat",
                          status=400)
        if repeats or delay is not None:
            return _error("rule: cannot be combined with `repeats` or "
                          "`delay_seconds` — a structured repeat says when "
                          "on its own, counting from `due`", status=400)
        if due is None:
            return _error("rule: needs `due` — the date and time of the first "
                          "run, which is what the repeat counts from",
                          status=400)
        try:
            # Validated HERE and not only in the model, so the message a person
            # reads is written by the module that knows the vocabulary.
            rule = recur.validate_rule(rule)
        except ValueError as exc:
            return _error(str(exc), status=400)
    elif repeats:
        if due is not None or delay is not None:
            return _error("repeats: cannot be combined with `due` or "
                          "`delay_seconds` — the cron line says when",
                          status=400)
    elif (due is None) == (delay is None):
        return _error("expected exactly one of `due` or `delay_seconds`",
                      status=400)
    if delay is not None:
        try:
            seconds = float(delay)
        except (TypeError, ValueError):
            return _error("delay_seconds: expected a number", status=400)
        if seconds <= 0:
            return _error("delay_seconds: must be positive", status=400)
        due = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    # `title`, `description` and `new_task_each_run` are passed straight through
    # and normalised by the model (`_text`/`_flag`), not here. Deliberately NOT
    # validated into a 400: the form omits them when they are blank or unticked,
    # so "absent" and "empty" have to mean the same thing, and a request that
    # carries a stray null for one of them should still schedule the message
    # rather than be refused over a label. What this layer must not do is DROP
    # them — a body-dict endpoint silently ignores what it does not name, which
    # is how they went missing in the first place.
    try:
        entry = schedule.create(
            resolved, body.get("message"), due,
            session_id=str(body.get("session_id") or ""),
            permission_mode=str(body.get("permission_mode") or ""),
            repeats=repeats, rule=rule,
            title=body.get("title"), description=body.get("description"),
            new_task_each_run=body.get("new_task_each_run"))
    except ValueError as exc:
        return _error(str(exc), status=400)
    return {"entry": entry}


@router.get("/api/schedule/queue")
def api_schedule_queue():
    """What is past due and waiting, and what is mid-flight.

    A separate endpoint from the listing rather than a field on it, and for the
    reason that shapes both: the listing is the whole schedule (everything ever
    created, with a recurrence projection computed per template), while this is
    the handful of rows the queue popover draws on app open. Folding it in would
    make a poll for "is anything waiting?" pay for the page's payload; splitting
    it out also lets the popover ask again after a cancel without redrawing the
    calendar.

    Unguarded like the other reads, and side-effect-free on purpose — the tick
    owns every state change, so opening the popover cannot change what runs."""
    return schedule.queue()


@router.post("/api/schedule/queue/cancel")
def api_schedule_queue_cancel(body: dict = Body(...),
                              x_fused: str | None = Header(default=None)):
    """Cancel queued messages: `{"entry_ids": [...]}` or `{"all": true}`.

    Guarded like the other writes — it stops unattended agent turns, which is
    exactly the pair (schedule / unschedule) D3's header guard covers.

    **Partial success is the normal outcome, not an error**, which is why this
    answers 200 with two lists rather than a status code. Cancelling races the
    claim: an entry already taken for sending is refused rather than corrupted
    (see `schedule.cancel_queued`), and it comes back in `refused` with a reason
    so the popover can say "already running" instead of silently dropping a row
    the user thought they had stopped. A request naming ten entries where one
    got away is nine cancellations and one honest report."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    all_queued = bool(body.get("all"))
    entry_ids = body.get("entry_ids")
    if not all_queued:
        if not isinstance(entry_ids, list) or not entry_ids:
            return _error("expected `entry_ids` (a non-empty list of ids) or "
                          "`all: true`", status=400)
        if not all(isinstance(i, str) and i for i in entry_ids):
            return _error("entry_ids: expected a list of entry id strings",
                          status=400)
    result = schedule.cancel_queued(entry_ids=entry_ids, all_queued=all_queued)
    return {"ok": True, **result}


@router.post("/api/schedule/restore")
def api_schedule_restore(body: dict = Body(...),
                         x_fused: str | None = Header(default=None)):
    """Un-skip a skipped recurring run. Guarded like the other writes — it
    re-arms an unattended agent turn, which is exactly what D3's header guard
    exists to keep foreign pages from doing."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    entry_id = body.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        return _error("id: required", status=400)
    entry = schedule.restore(entry_id)
    if entry is None:
        return _error(
            f"no restorable skipped run with id {entry_id!r} — only a skipped "
            "run of a still-active schedule, before its time, can be unskipped",
            status=404)
    return {"entry": entry}


@router.post("/api/schedule/cancel")
def api_schedule_cancel(body: dict = Body(...),
                        x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    entry_id = body.get("id")
    if not isinstance(entry_id, str) or not entry_id:
        return _error("id: required", status=400)
    entry = schedule.cancel(entry_id)
    if entry is None:
        # One 404 for both "no such id" and "not pending any more": the second
        # is the race worth being honest about — a message that sent while the
        # user was reaching for Cancel is not cancellable, and saying "already
        # sent" would be a guess this layer cannot make (the entry may equally
        # have been cancelled a moment ago in another tab).
        return _error(f"no cancellable scheduled message with id {entry_id!r}",
                      status=404)
    return {"entry": entry}
