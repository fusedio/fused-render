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

from fastapi import APIRouter, Body, File, Header, UploadFile

from fused_render import recur, schedule, tasks_store
from fused_render.server import image_convert
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


# What the New task form's attach/drop accepts, and the one place bytes are
# written: everything else (the create endpoint, the model) deals only in the
# returned paths, which `schedule._images` refuses unless they live under
# `schedule.shots_dir()`.
#
# MULTIPART, not the data-URL JSON this used to take (2026-08-28). The form no
# longer holds the file as a data URL at all — the chip's thumbnail is a
# `blob:` URL and only pictures get one — and base64 is a 33% tax paid twice
# (once in the browser's string, once in the body) on a gesture whose whole
# point is now that a 40 MB log can be dropped on the card. `UploadFile`
# streams to a spooled temp file instead.
#
# NO CAPS AND NO TYPE GATE (D618, following D615 for the chat): any file, any
# size. The image-only MIME check and the 4 MB byte ceiling are both gone; 4 MB
# survives as `image_convert.PNG_MAX_BYTES`, which is a DOWNSCALE TRIGGER for a
# picture and never a refusal.


@router.post("/api/schedule/shot")
async def api_schedule_shot(file: UploadFile | None = File(default=None),
                            x_fused: str | None = Header(default=None)):
    """Store one task attachment; return the path to schedule with.

    `{path, kind, width?, height?}` — `kind` is "image" or "file", which is the
    one thing the card cannot work out for itself once the extension may be
    anything (and which the transcode below can CHANGE: a `.tif` arrives as
    bytes no browser draws and leaves as a PNG the chip can show).

    The file is OPTIONAL in the signature so that a body which is not multipart
    at all reaches this function and gets the D3 guard's answer first: a
    required `File(...)` is a validation error raised before any of our code
    runs, which would have made an unguarded 422 the reply to a cross-origin
    POST."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if file is None:
        return _error("file: expected a multipart upload", status=400)
    raw = await file.read()
    if not raw:
        return _error("file: empty upload", status=400)

    mime = file.content_type or ""
    ext = image_convert.ext_for(mime, file.filename)
    shots = schedule.shots_dir()
    os.makedirs(shots, mode=0o700, exist_ok=True)
    # Server-minted name, never the client's: the path returned here is what
    # `schedule._images` later trusts, and a filename is the one field in a
    # multipart body a page chooses freely. Only the EXTENSION is taken from the
    # client (sanitised by `ext_for`), because it is what decides the template a
    # preview opens the file in.
    name = (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            + "-" + os.urandom(4).hex() + ext)
    path = os.path.join(shots, name).replace("\\", "/")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)

    if not image_convert.is_image(ext, mime):
        return {"path": path, "kind": "file"}

    # A PICTURE gets one more decision, and it is taken HERE rather than in the
    # browser (the chat takes it in the page, with a canvas; D613). Server-side
    # for this form because the two cases are one call from here — a format no
    # engine can decode and a picture merely too big share a ladder — where in
    # the client they are two code paths, one of which cannot exist at all: a
    # canvas cannot downscale bytes it cannot draw. The card stays small and the
    # bytes are already on disk by the time the question is asked.
    out, width, height = path, None, None
    blind = image_convert.browser_blind(ext, mime)
    if blind or len(raw) > image_convert.PNG_MAX_BYTES:
        # A DIFFERENT name, not a same-extension sibling: a 6 MB `.png` being
        # downscaled would otherwise be asked to overwrite itself.
        conv = image_convert.transcode(
            path, os.path.splitext(path)[0] + "-view")
        if conv.get("path"):
            out = conv["path"]
            width, height = conv.get("width"), conv.get("height")
    if width is None:
        size = image_convert.dimensions(out)
        if size:
            width, height = size
    body = {"path": out, "kind": "image"}
    if width and height:
        body["width"], body["height"] = width, height
    return body


def resolve_target(value, field: str = "target"):
    """`(resolved path, None)` for a create body's target, or `("", error)`.

    TWO refusals, both of which have to happen before anything is stored, and
    both of which every caller that creates an entry owes:

    * **it is required.** A scheduled turn with no path is an agent turned loose
      on whatever the process happens to be sitting in.
    * **it may not be mount-backed.** The bytes under the mounts dir come from a
      remote over FUSE and every peer gate refuses those paths (the claude
      template's `condition.py` exists for this single refusal), so scheduling
      against one would route around that gate. `is_mount_backed` is imported per
      call, not at module scope: binding the name at import would freeze it past
      the mounts registry's own seams, the same reason the peer gates resolve it
      late.

    Resolved the way the model will resolve it (expanduser + abspath), so the
    path this clears is the path that gets scheduled. `field` names the field in
    the message because the queue's admission calls it `project` and a person
    reading "target: required" after sending a `project` learns nothing.
    """
    if not isinstance(value, str) or not value.strip():
        return "", _error(f"{field}: required", status=400)

    from fused_render.shell.mounts import is_mount_backed

    resolved = os.path.abspath(os.path.expanduser(value.strip()))
    if is_mount_backed(resolved):
        return "", _error(
            f"{field}: refused — a scheduled session must not run against a "
            "remote mount", status=400)
    return resolved, None


def create_entry(target: str, body: dict, due, *, repeats: str = "",
                 rule: dict | None = None, create_target: bool = False,
                 origin: str = "") -> dict:
    """`schedule.create` with a request body's optional fields forwarded exactly
    as this router forwards them. Raises `ValueError` with the model's sentence.

    ONE copy of the forwarding list, because there are now two endpoints that
    create an entry from a request body — the New task form here, and the chat
    admission next door (`POST /api/tasks/queue/admit`), which stores the very
    message a user just typed when its folder is busy. A queued chat send that
    silently dropped `model`, `effort` or the attachments would be a different
    message from the one that would have gone had the folder been free, which is
    the one thing the queue must never be: it delays work, it does not change it.

    `title`, `description`, `new_task_each_run`, `session_learned`, `immediate`,
    `images` and `attachments` are passed straight through and normalised by the
    model (`_text`/`_flag`/`_images`/`_attachments`), not validated here — the
    form omits a field rather than sending a blank one, so "absent", "null" and
    "" all have to mean the same thing, and this layer must not hold a second
    copy of the model's rules. What it must not do is DROP them; a body-dict
    endpoint silently ignores what it does not name, which is how they went
    missing in the first place.

    `follow_of` is forwarded the same way and validated by the model, which is
    the only layer that can: it names an entry in the scheduler's store, so
    "does this exist" is a question only that store answers. The admission
    endpoint is its one sender — a message typed into a chat whose first message
    is still queued has no session id to pass, and this is what keeps the two
    of them one task.

    `origin` is the one field here that is NOT taken from the body, and that is
    the whole of it: it says which surface asked for this message, and a request
    cannot be trusted to say. The chat admission passes `"chat"` because it IS
    the chat's send path; the New task form and the calendar pass nothing, and
    their entries carry none — which is what lets a chat tell a message it
    queued itself from one somebody scheduled into it (`schedule.create`).

    `model` and `effort` are likewise not validated into a 400: `--model` takes
    an alias or a full id and `--effort` one of five levels, and the authority on
    both is the CLI this server shells out to, not a list in this file that would
    go stale the day Claude Code learns a new one. A value the CLI refuses fails
    the RUN, with the CLI's own sentence on the entry, which is a better error
    than a 400 written from a guess.
    """
    return schedule.create(
        target, body.get("message"), due,
        session_id=str(body.get("session_id") or ""),
        permission_mode=str(body.get("permission_mode") or ""),
        model=str(body.get("model") or ""),
        effort=str(body.get("effort") or ""),
        repeats=repeats, rule=rule,
        title=body.get("title"), description=body.get("description"),
        new_task_each_run=body.get("new_task_each_run"),
        session_learned=body.get("session_learned"),
        immediate=body.get("immediate"),
        images=body.get("images"),
        attachments=body.get("attachments"),
        follow_of=str(body.get("follow_of") or ""),
        origin=origin,
        create_target=create_target)


@router.post("/api/schedule")
def api_schedule_create(body: dict = Body(...),
                        x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    # Refused before anything is stored — see `resolve_target`.
    resolved, refusal = resolve_target(body.get("target"))
    if refusal is not None:
        return refusal

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

    # Everything optional on the body is forwarded by `create_entry`, which
    # documents at length what is passed through unvalidated and why. THE ONE
    # ENDPOINT ALLOWED TO MAKE A FOLDER is this one: the New task form lets you
    # name a folder that does not exist yet — it shows the path as a new folder
    # while you type it — and `create_target=True` is where that promise is kept.
    # One missing leaf under an existing parent is created, two missing levels
    # are still a 400. See `schedule.create`.
    try:
        entry = create_entry(resolved, body, due, repeats=repeats, rule=rule,
                             create_target=True)
    except ValueError as exc:
        return _error(str(exc), status=400)

    # THE TASK NUMBER SURVIVES AN EDIT. Editing a scheduled message is cancel +
    # re-create — there is no PATCH — so the entry the user was looking at stops
    # existing and this one, with a brand new id, takes its place. A task that
    # has not run yet is numbered on that entry id (`pending:<entry-id>`, §5 of
    # the design), so the listing saw a key it had never seen, allocated the next
    # number in the project, and the user watched TASK-078 become TASK-079 for a
    # task whose only change was its time — with no duplicate row to explain it.
    # `allocate once, never renumber` is the rule that broke.
    #
    # `rekey` is the same move a first run already makes when a pending key
    # becomes a session id, with the same guarantees: it MOVES a number rather
    # than minting one, refuses to overwrite a number the new key somehow already
    # has, and never releases one (so the project's high-water mark stands and no
    # number is ever handed out twice).
    #
    # A no-op in every case but the one it is for: an entry with no number (it
    # ran, so its task is keyed on the session id, which an edit does not touch),
    # a `replaces` naming nothing, or a caller that is not the edit form.
    #
    # Failure here is not the caller's problem — the message IS scheduled, and a
    # read-only state dir must not turn that into a 500. The row keeps the number
    # it would have got anyway.
    replaces = str(body.get("replaces") or "").strip()
    if replaces:
        try:
            tasks_store.rekey(tasks_store.pending_key(replaces),
                              tasks_store.pending_key(str(entry.get("id") or "")))
        except OSError:
            pass
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
    owns every state change, so opening the popover cannot change what runs.

    **`live` is the third list**, added for the queue dock (Akshil, 2026-08-17):
    the entries whose TURN is still going. `schedule.queue()` deliberately stops
    at `sending` — a spawned run has its own cancel in the job registry — but the
    registry's row knows only a title and a status line, so a run parked on a
    permission prompt was visible and still unreachable: nothing on screen said
    where to go and answer it. These entries carry the target and the session the
    turn landed in, which is exactly what an "Open in Explorer" link needs, and
    the dock joins them onto its job rows by id (`sys:schedule:<entry id>`).

    Live is `sent` with an EMPTY `turn` — the same rule the client's `isLive`
    applies, and it is a rule about the pair: `state` says the message was sent,
    `turn` is written once when the turn ends, so an unwritten turn is one still
    in flight. Typically nought to two rows; a historical `sent` entry has a turn
    and does not appear."""
    result = schedule.queue()
    result["live"] = [e for e in schedule.list_entries()
                      if e.get("state") == schedule.SENT and not e.get("turn")]
    return result


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


@router.post("/api/schedule/run-now")
def api_schedule_run_now(body: dict = Body(...),
                         x_fused: str | None = Header(default=None)):
    """Send a pending message NOW: `{"entry_id": "..."}` -> `{"ok", "entry"}`.

    The Board's Upcoming -> In Progress drag. Guarded like every other write
    here, and more obviously than most: this one starts an unattended agent turn
    on the spot, which is the exact thing D3's header guard keeps a foreign page
    from doing.

    **It does not move `due`.** The schedule time is a fact about what was asked
    for, so the row reads as having run early rather than as having been
    scheduled for now — see `schedule.run_now`, which is where every rule about
    this lives. This layer only maps the model's honest refusal onto a status
    code: 404 when there is no such entry, 409 when there is one and it cannot
    run (already sent, already sending, cancelled, or its conversation is
    mid-turn). Both carry the model's sentence, because "it didn't run" without
    a reason is what makes a dragged card feel broken.

    **QUEUED IS NOT A REFUSAL, and it is a 200.** With the project queue on
    (`project_queue`), a folder another task is holding does not refuse this
    message — it HOLDS it: the entry stays pending, gains `priority` (asking for
    something now IS a skip, and skipping is what run-now means once the folder
    is busy), and the row reads `queued` at position 1. Nothing went wrong and
    nothing was lost, so an error status would be a lie the client then has to
    undo — the dragged card would snap back over work that is going out the
    moment the folder frees. The answer carries `ok: false` with a `reason` of
    `"queued"` and the name of the task in front, which is the sentence the card
    prints.

    Naming who is ahead is a TASKS fact — the numbering and the title
    precedence — so it is asked of the tasks router (`queue_ahead_of`), which
    owns both. Imported inside the function: the two routers are siblings and a
    module-level import in this direction is a cycle waiting for the first person
    to add one in the other.

    **AND SO IS THE POSITION.** The model counts a folder's line in ENTRIES
    (`schedule._queue_position`); the Tasks page counts it in TASKS — one slot
    each, however many messages a task has queued — and that is the number on
    the row, in the chip and in "#2 in line". Two numbers for one line is one
    number wrong, so the page's is taken here too (`tasks._queue_place`, the
    same derivation the admit and skip verbs answer with) and the model's is
    kept only as the fallback for a task the listing does not place. Both are
    read off ONE collection, because they are two facts about one set of tasks.

    Naming it carries the LINK as well as the words — `ahead_session` and
    `ahead_target`, `tasks-lib.taskHref`'s own pair — so the card that says
    "behind TASK-041" can be clicked through to the conversation in front. Both
    "" for a holder that has no session yet, which is the case the sentence
    below is about.

    The holder is named by its TASK key (`ahead_task_key`) rather than by its
    session: an entry the scheduler has claimed but not yet spawned has no
    session at all, and it is `pending:<id>` — a real row, with a real number —
    that the user is waiting behind."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    entry_id = body.get("entry_id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        return _error("entry_id: required", status=400)
    result = schedule.run_now(entry_id.strip())
    if result.get("queued"):
        from fused_render.server.routers import tasks as tasks_api

        ahead_key = str(result.get("ahead_task_key")
                        or result.get("ahead_session") or "")
        # ONE COLLECTION FOR BOTH HALVES. Naming the holder and placing this row
        # are two questions about the same set of tasks, and collecting is a
        # glob over every transcript on the machine — asking each to collect for
        # itself walked it twice for one reply (round-2 review, 2026-09-12).
        tasks = tasks_api._collect()
        ahead = tasks_api.queue_ahead_of(ahead_key, tasks)
        place = tasks_api._queue_place(
            schedule._task_key(result["entry"]), tasks)
        return {"ok": False, "reason": "queued", "entry": result["entry"],
                "position": place["position"] or int(result.get("position") or 1),
                **ahead}
    if not result["ok"]:
        return _error(result["reason"],
                      status=404 if not result["found"] else 409)
    return {"ok": True, "entry": result["entry"]}


@router.post("/api/schedule/resend")
def api_schedule_resend(body: dict = Body(...),
                        x_fused: str | None = Header(default=None)):
    """Ask again: `{"entry_id": "..."}` -> `{"ok", "entry", "note"}`.

    The Re-run button on a task whose run already went and broke. Run-now cannot
    serve that case — it claims a PENDING entry, and the run that failed spent
    itself — so this is the other half of the same affordance: a task is a
    thread, and asking for the work again is a NEW message in it. The original
    entry is left exactly as it is; `entry` in the response is the new one.

    Guarded like run-now, and for the identical reason: it starts an unattended
    agent turn on the spot.

    Same three status codes and the same sentences underneath them — 404 for no
    such id, 409 for an entry that cannot be re-sent (still pending, still
    sending, cancelled, missed, or a template), 400 for a body without an id or
    a target that has since been deleted. `note` rides along on SUCCESS: the new
    message may be queued rather than away (its conversation can be mid-turn),
    and that is a sentence to show, not an error to raise.

    **The mount refusal is re-checked**, not inherited from the original's
    creation. It passed the gate whenever it was scheduled, and a path can
    become mount-backed after that; spawning against the stored target without
    asking again would route around the gate the whole check exists to be."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    entry_id = body.get("entry_id")
    if not isinstance(entry_id, str) or not entry_id.strip():
        return _error("entry_id: required", status=400)
    entry_id = entry_id.strip()

    original = next((e for e in schedule.list_entries()
                     if str(e.get("id") or "") == entry_id), None)
    if original is None:
        return _error(f"no scheduled message with id {entry_id!r}", status=404)

    from fused_render.shell.mounts import is_mount_backed

    if is_mount_backed(str(original.get("target") or "")):
        return _error(
            "target: refused — a scheduled session must not run against a "
            "remote mount", status=400)

    try:
        result = schedule.resend(entry_id)
    except ValueError as exc:
        return _error(str(exc), status=400)
    if not result["ok"]:
        return _error(result["reason"],
                      status=404 if not result["found"] else 409)
    return {"ok": True, "entry": result["entry"], "note": result["reason"]}


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
