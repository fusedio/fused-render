"""Tasks — one row per Claude Code session, with its thread of messages.

A **task is a session**, 1:1, and a thread is the messages that entered it —
from the explorer's Claude chat, from the template chat, or from a schedule. The
thread does not care which; that is the whole point of collapsing the two stores
the app used to keep side by side (the scheduled-message list it owns, and the
session transcripts Claude Code owns, joined by one field).

Three endpoints, and the split between the first two is the design constraint
this file is written around:

* ``GET /api/tasks`` — every task, newest first, each carrying its **three most
  recent** messages. This runs for every row on the page and is polled, so
  nothing in it may scale with transcript size. Transcripts are append-only, so
  the scan below reads each file **once** and thereafter only the bytes that
  were appended since — a multi-MB transcript is never re-read.
* ``GET /api/tasks/{key}/messages`` — one task's FULL thread. This is the
  "Show more" click: a whole-transcript parse, which is affordable exactly
  because it happens for one task at a time and never for a listing.
* ``GET /api/tasks/scheduled?from&to`` — every SCHEDULED message in a window,
  for the calendar. Separate from the listing rather than a window parameter on
  it, because the listing's three-message tail is right for an accordion and
  wrong for a time axis, and one field cannot mean both.
* ``POST /api/tasks/read`` — mark one message read.

**What a message is.** A user prompt in the transcript, or a scheduled entry.
Those two overlap: a scheduled message that fired IS a prompt in the transcript
(`_send` hands `entry["message"]` over verbatim), so listing both would show it
twice. The full thread therefore JOINS them on the message body, nearest in
time, and the listing — which cannot afford to look at every prompt — counts the
same thing arithmetically: the transcript's prompts, plus the scheduled entries
that never reached a transcript at all (pending, missed, cancelled). Message ids
follow from that count and nothing else: the Nth message in time order is MSG-N
(`tasks_store.message_ids`), so nothing has to be stored and nothing can drift.

**A task key** is the session id, or `pending:<entry-id>` for a task scheduled
for a session that does not exist yet (§5). The number a pending row is showing
follows it onto the session id at the first run — see `tasks_store.rekey` — so
the row the user has been watching does not silently renumber the moment it
finally runs.

Every field degrades rather than fails. An unreadable transcript, a truncated
line, a session whose cwd is gone, a store that is not there yet — each costs
that one task, or that one fact about it, and never the listing. That is the
posture of every module this one reads from (claude_sessions.py,
claude_artifacts.py, schedule.py) and it is the posture here.

Reads are unguarded, like every other read endpoint. The one write marks a
message read — the same weight of change as `POST /api/claude-sessions/triage`
next door, which carries no guard either: it moves a badge, it does not run
code.
"""
import json
import os
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from fused_render import schedule, tasks_store
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server.routers import claude_sessions as sessions

router = APIRouter()

# A task's status, decided HERE and read by every view. `upcoming` and `failed`
# are the two triage does not have a word for — a session cannot be "not yet",
# and the Inbox's three columns have no place to put a broken run — which is why
# the union below is not symmetric.
#
# `failed` is a STATUS and not only the `failed` boolean beside it, because a
# run that broke is not a kind of done. It used to be exactly that: `done` with
# a flag, so every view had to remember to read the flag and paint it, and any
# view that did not simply lost the news. One decision, made once, on the
# server.
STATUSES = ("upcoming", "in_progress", "done", "failed", "archived")
_TRIAGE_STATUSES = ("in_progress", "done", "archived")

# How many messages a listing row carries. The accordion shows three and offers
# "Show more"; the fourth costs another row of tail to keep in memory for every
# session on the machine.
_LISTING_MESSAGES = 3

# How much of a prompt the listing keeps per message. Long enough to read a
# scheduled message whole (they are one-liners), short enough that keeping three
# per session for a few thousand sessions is megabytes, not gigabytes. The full
# thread endpoint does not truncate.
_BODY_MAX = 2000

# Scheduled states whose body was actually handed to a session, and therefore
# appears in the transcript as a prompt. Everything else (pending, missed,
# cancelled, error) never reached one, so it is a message the thread has to
# supply itself.
_IN_TRANSCRIPT = (schedule.SENT, schedule.SENDING)

# path -> incremental scan record. See `_scan`.
_SCAN: dict[str, dict] = {}
# path -> (size, [every prompt]). The expensive parse, kept only for the handful
# of threads a user actually opens.
_FULL: dict[str, tuple[int, list[dict]]] = {}
_FULL_MAX = 64


# (window, what the window contains) -> the built items. See
# `api_tasks_scheduled` for why the key is shaped the way it is.
_WINDOW: dict[tuple, list] = {}
_WINDOW_MAX = 16


def reset_cache() -> None:
    """Forget every cached transcript read. For tests, and for any caller that
    wants the next listing to re-read from disk unconditionally."""
    _SCAN.clear()
    _FULL.clear()
    _WINDOW.clear()
    tasks_store.reset_cache()


# --------------------------------------------------------------- transcripts


# User-role records Claude Code writes on the user's behalf rather than the
# user writing them: a finished subagent reporting back, a slash command's name
# and its stdout, the app's own state injection. They are `type: user` and carry
# no `isMeta`, so the role alone cannot tell them apart — and on a real machine
# they were a THIRD of every "message" in the store (88 task-notifications in
# 270 records), which would have made a thread mostly machinery. Matched at the
# very start of the body, where they always sit; a tag appearing further in
# (`<system-reminder>` appended to something a human typed) leaves a real
# message real.
_MACHINERY = (
    "<task-notification>", "<command-name>", "<command-message>",
    "<local-command-stdout>", "<local-command-stderr>", "<live-app-state>",
    "<user-prompt-submit-hook>", "<system-reminder>",
)


def _prompt(obj) -> dict | None:
    """One transcript record as a chat message, or None if it isn't one.

    `isMeta` records (Claude Code's own caveats), tool-result-only user records
    and the machinery above are not things a human said, and a thread that
    listed them would be mostly machinery."""
    if obj.get("type") != "user" or obj.get("isMeta"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    text = tasks_store.first_text(message.get("content")).strip()
    if not text or text.startswith(_MACHINERY):
        return None
    anchor = obj.get("uuid")
    return {"body": text,
            "at": tasks_store.epoch(obj.get("timestamp")),
            "anchor": anchor if isinstance(anchor, str) else ""}


def _absorb(rec: dict, line: str) -> None:
    """Fold one raw transcript line into a scan record. Screened before parsing:
    a transcript is mostly assistant turns and tool results, and `json.loads` on
    every one of them is the cost this endpoint cannot pay."""
    if '"user"' not in line and sessions.AI_TITLE_HINT not in line:
        return
    try:
        obj = json.loads(line)
    except ValueError:
        return  # truncated / partially-written line: skip it, keep the file
    if not isinstance(obj, dict):
        return
    title = sessions.ai_title(obj)
    if title:
        # Last one wins — the record is re-emitted every turn and the title
        # tracks the conversation. See claude_sessions.ai_title.
        rec["title"] = title
        return
    prompt = _prompt(obj)
    if prompt is None:
        return
    prompt["body"] = prompt["body"][:_BODY_MAX]
    rec["count"] += 1
    rec["tail"].append(prompt)
    if len(rec["tail"]) > _LISTING_MESSAGES:
        rec["tail"].pop(0)


def _new_scan() -> dict:
    return {"offset": 0, "size": -1, "count": 0, "tail": [], "title": ""}


def _scan(path: str) -> dict | None:
    """One transcript's cheap facts — how many messages, the last three, and the
    current ai-title — read INCREMENTALLY.

    Transcripts are append-only, so a file that grew is re-read only from the
    byte where the last complete line ended. That is what makes a listing over
    a machine's whole session history affordable on every poll: the first call
    pays for the file once, and every call after it pays for the turn that
    happened since.

    The offset only ever advances to a newline, so a record caught half-written
    is re-read whole on the next call rather than being dropped.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None  # vanished mid-listing: costs this task, not the listing
    rec = _SCAN.get(path)
    if rec is not None and rec["size"] == size:
        return rec
    if rec is None or size < rec["offset"]:
        rec = _new_scan()  # a shrunk file was replaced: re-read from the top
    try:
        with open(path, "rb") as f:
            f.seek(rec["offset"])
            chunk = f.read()
    except OSError:
        return rec if rec["size"] >= 0 else None
    cut = chunk.rfind(b"\n")
    if cut >= 0:
        text = chunk[:cut + 1].decode("utf-8", "replace")
        rec["offset"] += cut + 1
        for line in text.split("\n"):
            if line.strip():
                _absorb(rec, line)
    rec["size"] = size
    _SCAN[path] = rec
    return rec


def _full_prompts(path: str) -> list[dict]:
    """Every prompt in a transcript, bodies untruncated. The expensive read, and
    the one only the Show-more endpoint makes."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    cached = _FULL.get(path)
    if cached is not None and cached[0] == size:
        return cached[1]
    prompts: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if '"user"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                prompt = _prompt(obj)
                if prompt is not None:
                    prompts.append(prompt)
    except OSError:
        return []
    if len(_FULL) >= _FULL_MAX:
        _FULL.clear()
    _FULL[path] = (size, prompts)
    return prompts


# ------------------------------------------------------------------ messages


def _body_key(text) -> str:
    """The form a body is compared in, on both sides of the join. Truncated the
    same way the listing truncates, so a listing message and a full-thread
    message of the same prompt still match each other."""
    return str(text or "").strip()[:_BODY_MAX].strip()


def _entry_at(entry: dict) -> float:
    """**When the message was SCHEDULED FOR** — its due time, and nothing else.

    This used to read `fired` first, and that was the bug behind the one thing
    the calendar must not get wrong. A message scheduled for Thursday and caught
    up on Saturday (which is the ordinary outcome of an unbounded queue, SCH-3b)
    was stamped Saturday, so its chip left the column the user picked and
    appeared on the day they happened to reopen the app — a row that silently
    rewrote what had been asked for.

    `due` is a fact about the ASK and never moves; when it actually ran is a
    second, separate fact, and `_entry_ran_at` is where that lives. The
    fallback to `fired` covers only an entry with no readable due time at all
    (a hand-edited store), where the alternative is placing it at the epoch.
    """
    return (tasks_store.epoch(entry.get("due"))
            or tasks_store.epoch(entry.get("fired")) or 0.0)


def _entry_ran_at(entry: dict) -> float:
    """When a scheduled message ACTUALLY ran — 0.0 for one that has not.

    `fired` is the claim stamp, written the instant before the helper is
    spawned, so it is this side's best answer for every entry that got away.
    The join in `_merge` improves on it where it can: a message that reached a
    transcript has the session's own timestamp for the prompt, which is what
    the thread is ordered against everywhere else."""
    return tasks_store.epoch(entry.get("fired")) or 0.0


def _entry_state(entry: dict) -> str:
    """A scheduled entry's state in the thread's vocabulary.

    Two narrowings, both taken from schedule-lib.ts so the server and the page
    cannot describe one entry differently:

    * an OCCURRENCE that did not run was **skipped**, whoever decided it — the
      user's cancel and the loop's own missed verdict both mean "this run of a
      repeating message did not happen", and filing the second under a fault
      made routine behaviour look like breakage;
    * `sent` only means the session STARTED. A turn that then died is an
      **error**, because `state` is the only field in the frozen message shape
      that can say so (`turn` is a lifecycle, not an outcome).
    """
    state = str(entry.get("state") or "")
    turn = str(entry.get("turn") or "")
    if state in (schedule.CANCELLED, schedule.MISSED) and entry.get("template_id"):
        return "skipped"
    if state == schedule.SENT and turn in ("failed", "unknown"):
        return "error"
    return state


def _entry_turn(entry: dict, live: bool) -> str:
    """How the turn a scheduled message started is going: "" while it is in
    flight, `done` once it ended, `idle` for one that started and stopped
    reporting with nothing running now, `unknown` where the watcher said so."""
    turn = str(entry.get("turn") or "")
    if turn == "unknown":
        return "unknown"
    if turn:
        return "done"
    if str(entry.get("state") or "") in _IN_TRANSCRIPT:
        return "" if live else "idle"
    return ""


def _scheduled_message(entry: dict, live: bool, at: float, ran_at: float,
                       anchor: str) -> dict:
    return {
        "message_id": "",
        "kind": "scheduled",
        "body": str(entry.get("message") or ""),
        # TWO times, because a scheduled message genuinely has two and they
        # disagree whenever the app was closed at the wrong moment. `at` is what
        # was asked for; `ran_at` is what happened. See `_entry_at`.
        "at": at,
        "ran_at": ran_at,
        "state": _entry_state(entry),
        "unread": False,
        "entry_id": str(entry.get("id") or ""),
        "template_id": str(entry.get("template_id") or ""),
        "turn": _entry_turn(entry, live),
        "anchor": anchor,
    }


def _chat_message(prompt: dict) -> dict:
    return {
        "message_id": "",
        "kind": "chat",
        "body": prompt["body"],
        "at": prompt["at"] or 0.0,
        # A typed message was scheduled for the moment it was typed: there is
        # no gap for the two stamps to disagree across, so they are the same
        # number rather than `ran_at` being an absence the client has to
        # special-case per kind.
        "ran_at": prompt["at"] or 0.0,
        # A typed message was delivered the moment it was typed. The state
        # vocabulary is the schedule's, and `sent` is the word in it for that.
        "state": schedule.SENT,
        "unread": False,
        "entry_id": "",
        "template_id": "",
        "turn": "done",
        "anchor": prompt["anchor"],
    }


def _merge(prompts: list[dict], entries: list[dict], live: bool) -> list[dict]:
    """One thread, oldest first, with the scheduled entries joined onto the
    prompts they became.

    The join is on the body — `_send` hands the entry's message to the session
    verbatim — and where several prompts carry the same body (a daily message
    into a chained session, which is the normal case, not the exotic one) the
    nearest in time wins and is consumed, so N occurrences match their own N
    prompts in order rather than all piling onto the first.

    **What the match may and may not move.** It fills in `ran_at` (the
    transcript's own timestamp for the prompt, which is the most accurate answer
    anything here has to "when did this actually happen") and the `anchor` that
    scrolls to it. It does NOT touch `at`. Writing the prompt's timestamp over
    `at` was the original shape and it lost the schedule: a message due two days
    ago and caught up today became a message due today, so the calendar drew its
    chip in a column the user had never picked. The distance heuristic still runs
    on `at`, which is right — the due time is what an occurrence is nearest to.
    """
    taken: set[int] = set()
    messages: list[dict] = []
    for entry in entries:
        at = _entry_at(entry)
        ran_at = _entry_ran_at(entry)
        anchor = ""
        if str(entry.get("state") or "") in _IN_TRANSCRIPT:
            body = _body_key(entry.get("message"))
            best = None
            for j, prompt in enumerate(prompts):
                if j in taken or _body_key(prompt["body"]) != body:
                    continue
                distance = abs((prompt["at"] or 0.0) - at)
                if best is None or distance < best[0]:
                    best = (distance, j)
            if best is not None:
                taken.add(best[1])
                matched = prompts[best[1]]
                ran_at = matched["at"] or ran_at
                anchor = matched["anchor"]
        messages.append(_scheduled_message(entry, live, at, ran_at, anchor))
    for j, prompt in enumerate(prompts):
        if j not in taken:
            messages.append(_chat_message(prompt))
    # Ascending, because that is the order the ids are in. Position in the file
    # breaks a tie between two messages recorded in the same second — a
    # transcript is append-only, so later in the file is later in time.
    messages.sort(key=lambda m: m["at"])
    for n, message in enumerate(messages, 1):
        message["message_id"] = tasks_store.format_message_id(n)
    return messages


def _turn_of_newest_chat(messages: list[dict], live: bool) -> None:
    """The newest chat message is the one whose turn may still be running; every
    older one has been answered by definition."""
    for message in reversed(messages):
        if message["kind"] == "chat":
            message["turn"] = "" if live else "idle"
            return


# --------------------------------------------------------------- the statuses


def _board_column(message: dict | None) -> str:
    """A task's status from its newest message, mapped state for state.

    Two mappings are worth naming because they look alike and are not:

    * **`error` is `failed`**, its own column. A run that started and broke is
      news, and filing it under `done` meant every view had to remember to read
      the `failed` flag separately to say so.
    * **`cancelled` and `skipped` are `archived`**, NOT failed. A skipped
      occurrence was filed away and never attempted — the coalescer dropped it,
      or the user did — which is a different thing from a run that tried and
      broke.

    `missed` stays `done`, unchanged and deliberately not touched here. It is
    only reachable at all on an install that set FUSED_RENDER_SCHEDULE_MAX_LATE
    (a missed OCCURRENCE reads as `skipped` and archives above), and by the
    reasoning that archives a skip it arguably belongs there too — but that is a
    separate decision from this one and nobody has made it.
    """
    if message is None:
        return "done"  # a task with nothing in it happened and is over
    state = message["state"]
    if state in ("cancelled", "skipped"):
        return "archived"
    if state == "sending":
        return "in_progress"
    if state == "error":
        return "failed"
    if state in ("sent", "missed"):
        return "done"
    return "upcoming"


def _status(newest: dict | None, session_id: str, triage: dict, live: bool) -> str:
    """The status a task sits in — ONE decision, made here, for every view.

    Derived from the newest message, then overridden by an explicit triage
    record. Triage wins on disagreement because it is the user's own act: they
    dragged the card, and a derivation that undid that on the next poll would
    make the board unusable. Only a RECORDED status overrides — the default
    claude_sessions applies to an untriaged session (running -> in_progress) is
    a derivation too, and a weaker one than the message's own state. Triage has
    only three words, so a user cannot file a task as `failed`; that is a fact
    about the run, not a place to put it.

    `failed` is decided here rather than left to the `failed` boolean beside it
    because a broken run is not a kind of `done`, and a status every view reads
    is the only way to be sure every view says so. It covers both halves of
    `_failed`: a message whose state is `error`, and one whose watcher stopped
    being able to say how the turn went (`turn: unknown`) — the second is
    invisible to `_board_column`, which only sees the state.

    A LIVE session still reads `in_progress` even over a failed newest message:
    something is running in that conversation right now, which is the more
    urgent fact and the one that stops being true on its own."""
    record = triage.get(session_id) if session_id else None
    if isinstance(record, dict):
        status = record.get("status")
        if status in _TRIAGE_STATUSES:
            return status
    if live and newest is not None:
        return "in_progress"
    if _failed(newest):
        return "failed"
    return _board_column(newest)


def _failed(newest: dict | None) -> bool:
    """Did the newest message's run break?

    Kept as its own field on the row as well as feeding `status` above, because
    the two can disagree in exactly one direction and the difference is worth
    keeping: a user who has triaged a task to `done`, or a session that is live
    again, reads `status` as something other than `failed` while this stays
    true. Anything that only wants "which column" should read `status`."""
    return newest is not None and (
        newest["state"] == "error" or newest["turn"] == "unknown")


# ----------------------------------------------------------------- the titles


def _title(task: dict, rec: dict | None, first_prompt: str) -> tuple[str, str]:
    """(title, where it came from). The precedence is §4's: what the user called
    it, then Claude Code's own one-liner for the session, then the first line of
    the first message. No summarisation call anywhere — the title we want is
    already written into the transcript once per turn."""
    for entry in reversed(task["entries"]):
        title = str(entry.get("title") or "").strip()
        if title:
            return title[:200], "user"
    if rec is not None and rec.get("title"):
        return str(rec["title"])[:200], "ai"
    body = first_prompt
    if not body:
        for entry in task["entries"]:
            body = str(entry.get("message") or "").strip()
            if body:
                break
    line = body.strip().splitlines()[0].strip() if body.strip() else ""
    return line[:200], "message"


# ------------------------------------------------------------ task collection


def _new_task(key: str, session_id: str, path: str | None) -> dict:
    return {"key": key, "session_id": session_id, "path": path, "entries": []}


def _collect() -> dict[str, dict]:
    """Every task on this machine: one per transcript, plus one per scheduled
    message that has no session yet.

    A scheduled entry whose `claude_session_id` names a session with no
    transcript on disk still makes a task — the session may be seconds old, or
    the transcript may have been moved — rather than dropping the user's
    message on the floor."""
    tasks: dict[str, dict] = {}
    for path in tasks_store.transcripts():
        session_id = os.path.splitext(os.path.basename(path))[0]
        tasks[session_id] = _new_task(session_id, session_id, path)
    for entry in schedule.list_entries():
        if entry.get("state") == schedule.RECURRING:
            # A template never fires and is not a message; its materialised
            # occurrences are, and they are ordinary entries in this list.
            continue
        session_id = str(entry.get("claude_session_id") or "")
        if session_id:
            task = tasks.get(session_id)
            if task is None:
                task = tasks[session_id] = _new_task(session_id, session_id, None)
            task["entries"].append(entry)
        else:
            key = tasks_store.pending_key(str(entry.get("id") or ""))
            task = tasks.setdefault(key, _new_task(key, "", None))
            task["entries"].append(entry)
    for task in tasks.values():
        task["entries"].sort(key=_entry_at)
    return tasks


def _workdir(target: str) -> str:
    """A target's project folder — the rule agent.py:_workdir applies before
    Claude Code ever sees the path, restated here because it is what decides
    which project a task belongs to (§2). A target that no longer exists is
    read as a file, which is the common case for one that was deleted."""
    target = str(target or "")
    if not target:
        return ""
    return target if os.path.isdir(target) else os.path.dirname(target)


def _place(task: dict) -> None:
    """Fill in a task's project, target and creation order.

    The project is the transcript's own `cwd` where there is one — the encoded
    directory name is lossy (Claude Code turns literal hyphens into separators
    too), so it is only the fallback — and otherwise the folder of whatever the
    scheduled message was pointed at."""
    cwd = None
    first_ts = None
    prompt = ""
    if task["path"]:
        cwd, first_ts, prompt = tasks_store.head(task["path"])
    task["first_prompt"] = prompt
    entries = task["entries"]
    target = str(entries[-1].get("target") or "") if entries else ""
    if not cwd:
        cwd = _workdir(target) or (
            sessions._decode_project_dir(os.path.basename(os.path.dirname(
                task["path"]))) if task["path"] else "")
    task["project"] = tasks_store.project_of(cwd or "")
    task["target"] = target or task["project"]
    if first_ts is None and entries:
        first_ts = (tasks_store.epoch(entries[0].get("created"))
                    or _entry_at(entries[0]))
    task["order"] = first_ts


def _numbers(tasks: dict[str, dict]) -> dict[str, str]:
    """Task numbers for everything in the listing, allocating what is missing.

    The rekey pass is what makes §5 hold: a pending row that has just run now
    has a session id, and its number moves onto that key instead of a second one
    being allocated under it."""
    store = tasks_store.task_ids()
    rekeys = []
    for task in tasks.values():
        if not task["session_id"]:
            continue
        for entry in task["entries"]:
            old = tasks_store.pending_key(str(entry.get("id") or ""))
            if old in store and task["key"] not in store:
                rekeys.append((old, task["key"]))
                break
    items = [(task["key"], task["project"], task["order"])
             for task in tasks.values()]
    try:
        return tasks_store.ensure_ids(items, rekeys)
    except OSError:
        # A read-only state dir must not cost the user their task list; the
        # numbers simply stay blank until it is writable again.
        return {}


# ------------------------------------------------------------------ liveness


def _live(path: str | None, now: float) -> tuple[bool, float]:
    """(is this session running, when was it last active).

    The same 45-second rule as the sessions inbox, and the same tail read — a
    transcript's mtime alone lies, because Claude Code appends housekeeping
    records after the turn is over. Skipped entirely for a file nothing has
    touched in 90 seconds: it is stale either way, so the read would only be
    deciding what kind of stale."""
    if not path:
        return False, 0.0
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False, 0.0
    if now - mtime > sessions._STALE_TAIL_SEC:
        return False, mtime
    activity, last = sessions._tail(path, mtime)
    running = (now - activity) < sessions._RUNNING_WINDOW_SEC
    return running, (last.timestamp() if last is not None else mtime)


# --------------------------------------------------------------- the endpoints


def _row(task: dict, number: str, triage: dict, read: dict, now: float) -> dict:
    """One listing row. The tail parse only: three messages, and a count."""
    rec = _scan(task["path"]) if task["path"] else None
    live, active = _live(task["path"], now)
    prompts = list(rec["tail"]) if rec else []
    # The transcript's prompts already include every scheduled message that
    # fired, so only the ones that never reached a session are added — and with
    # no transcript to read at all, every entry is a message the thread has to
    # supply itself rather than one the user loses.
    unfired = [e for e in task["entries"]
               if rec is None or str(e.get("state") or "") not in _IN_TRANSCRIPT]
    total = (rec["count"] if rec else 0) + len(unfired)

    # The tail of the merged thread can only be drawn from the last three
    # prompts and the unfired entries — a prompt outside that window cannot be
    # in the last three of a list it is in the same order as. Their ids follow
    # from the total, whatever else is below them.
    tail = _merge(prompts, task["entries"], live)
    tail = tail[-_LISTING_MESSAGES:]
    _turn_of_newest_chat(tail, live)
    for offset, message in enumerate(reversed(tail)):
        message["message_id"] = tasks_store.format_message_id(total - offset)
    _mark_unread(tail, task["key"], read)

    newest = tail[-1] if tail else None
    if newest is not None:
        # BOTH stamps, because the list sorts on this and the two answer
        # different halves of "recent": `ran_at` is when the task last did
        # something (a caught-up run is news today, whatever day it was due),
        # and `at` is what keeps a message scheduled for tomorrow near the top
        # where it can be seen before it fires.
        active = max(active, newest["at"] or 0.0, newest["ran_at"] or 0.0)
    if not active and task["entries"]:
        active = (tasks_store.epoch(task["entries"][-1].get("created"))
                  or _entry_at(task["entries"][-1]))
    title, source = _title(task, rec, task["first_prompt"])
    return {
        "key": task["key"],
        "task_id": number,
        # Canonicalized on the way out, like every other fs path this server
        # hands the shell: the frontend's path helpers are forward-slash-only.
        "project": canonical_fs_path(task["project"]),
        "target": canonical_fs_path(task["target"]),
        "session_id": task["session_id"],
        "title": title,
        "title_source": source,
        # Deferred by §12 — Claude Code stores no summary, so filling this needs
        # an LLM call. Read from the entry so a store that grows the field later
        # starts working without a change here.
        "description": _description(task),
        "status": _status(newest, task["session_id"], triage, live),
        "failed": _failed(newest),
        "live": live,
        "unread": _unread_count(task, total, unfired, read),
        "last_active": active,
        "message_count": total,
        # Newest first, which is how every list in this feature reads.
        "messages": list(reversed(tail)),
    }


def _description(task: dict) -> str:
    for entry in reversed(task["entries"]):
        text = str(entry.get("description") or "").strip()
        if text:
            return text
    return ""


def _mark_unread(messages: list[dict], key: str, read: dict) -> int:
    """Set each message's `unread` flag; return how many were unread.

    A message is unread when it has HAPPENED and has not been marked read.
    Something still waiting for its time has no response to have missed, so a
    task scheduled for tomorrow does not sit there claiming a notification.

    Nothing is unread until the store has been through its one-time baseline
    (`tasks_store.initialize`, stamped by the listing below): before that,
    "unread" would mean "exists", which is a badge on every message ever
    written."""
    if not tasks_store.initialized(read):
        for message in messages:
            message["unread"] = False
        return 0
    count = 0
    for message in messages:
        happened = message["state"] not in (
            "pending", "sending", "cancelled", "skipped")
        message["unread"] = happened and not tasks_store.is_read(
            read, key, message["message_id"])
        if message["unread"]:
            count += 1
    return count


def _unread_count(task: dict, total: int, unfired: list[dict],
                  read: dict) -> int:
    """How many of a task's messages are unread, WITHOUT reading the whole
    thread.

    Arithmetic, not enumeration: every message is unread unless it has been
    marked read, or it has not happened yet. The first is counted from the read
    store, the second from the scheduled entries (the only messages that can be
    in the future — a typed one is in the past by definition). Clamped at zero
    because the two counts can overlap on a message that was marked read and
    then cancelled, which is a real sequence and not worth a whole-thread parse
    to resolve exactly. The Show-more endpoint is exact."""
    if not tasks_store.initialized(read):
        return 0  # the day-one baseline has not been stamped yet
    waiting = sum(1 for entry in unfired
                  if _entry_state(entry) in ("pending", "cancelled", "skipped"))
    return max(0, total - tasks_store.read_count(read, task["key"], total)
               - waiting)


@router.get("/api/tasks")
def api_tasks():
    """Every task, newest activity first, each with its three newest messages.

    Includes tasks that have never been scheduled (a chat session is a task) and
    tasks that have never run (a message scheduled for tomorrow is a task, §5).
    """
    triage = sessions._load_state("triage.json")
    read = tasks_store.read_state()
    now = time.time()
    tasks = _collect()
    for task in tasks.values():
        _place(task)
    numbers = _numbers(tasks)
    rows = []
    for task in tasks.values():
        try:
            row = _row(task, numbers.get(task["key"], ""), triage, read, now)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # one unreadable task, not an unreadable page
        rows.append(row)
    # Day one: everything that already exists is read. Done HERE, from the
    # counts the rows just produced, because this is the only place that knows
    # them — and done after the rows are built rather than before, so it costs
    # one extra pass on exactly one request in the store's lifetime.
    if not tasks_store.initialized(read):
        tasks_store.initialize([(r["key"], r["message_count"]) for r in rows])
    rows.sort(key=lambda r: r["last_active"], reverse=True)
    return {"tasks": rows}


def _thread(task: dict, read: dict, now: float) -> list[dict]:
    """One task's whole thread, oldest first, ids and unread flags set."""
    live, _active = _live(task["path"], now)
    prompts = _full_prompts(task["path"]) if task["path"] else []
    messages = _merge(prompts, task["entries"], live)
    _turn_of_newest_chat(messages, live)
    _mark_unread(messages, task["key"], read)
    return messages


@router.get("/api/tasks/{key}/messages")
def api_task_messages(key: str):
    """One task's FULL thread, newest first — the "Show more" endpoint.

    Allowed to be expensive in the way the listing is not: it parses the whole
    transcript, which is affordable precisely because it happens for the one
    thread a user opened.
    """
    tasks = _collect()
    task = tasks.get(key)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task with key {key!r}")
    _place(task)
    messages = _thread(task, tasks_store.read_state(), time.time())
    return {"messages": list(reversed(messages))}


# How far outside the asked-for window a scheduled entry is still considered a
# candidate. The window is filtered EXACTLY, on each built message's final `at`
# — and since `at` is now the entry's own due time and the join can no longer
# move it (see `_entry_at`), the two agree and this slack is belt-and-braces:
# it costs at most a few extra threads parsed, and it is what keeps a store
# whose `due` a human has hand-edited mid-parse from silently under-drawing.
_WINDOW_SLACK_S = 86400.0


@router.get("/api/tasks/scheduled")
def api_tasks_scheduled(window_from: float = Query(..., alias="from"),
                        to: float = Query(...)):
    """Every SCHEDULED message due in a window — what the calendar draws.

    The listing carries three messages per task, which is right for an
    accordion and wrong for a time axis: a task whose runs fall outside its last
    three messages would simply not be drawn on those days, and the design's
    hourly case (one chip carrying `+23`) could never occur at all. So the
    calendar asks by WINDOW instead, and gets everything in it.

    **Complete for the window, by contract.** The client replaces a task's
    messages for these bounds with what comes back, so a partial answer silently
    drops chips. Every scheduled message a task has inside the bounds is here.

    `from` is inclusive and `to` exclusive, in epoch seconds — the client sends
    local-midnight bounds because its columns are local days, and the message at
    23:59 on the last column has to survive.

    Chat messages never appear: a typed message has no time the calendar could
    place it at, only the one it happened to be typed at.

    Allowed to be more expensive than the listing — it parses whole threads —
    but it is called on every arrow press, so it is cached. The key carries the
    bounds (the window is a QUERY, not a file) alongside a signature of what
    could change the answer: the size of each thread's transcript, and the state
    of each of its scheduled entries.

    Projected future occurrences of a recurring rule are deliberately NOT here.
    The client synthesises those from `/api/schedule`'s `upcoming[]`, which is
    tested and working; this endpoint answers for messages that exist.
    """
    if not to > window_from:
        # An inverted or empty window is a question with an empty answer, not an
        # error: the calendar can ask for one while it is still settling on its
        # bounds.
        return {"items": []}

    tasks = _collect()
    candidates = [
        task for task in tasks.values()
        if any(window_from - _WINDOW_SLACK_S <= _entry_at(entry)
               < to + _WINDOW_SLACK_S for entry in task["entries"])
    ]
    signature = []
    for task in candidates:
        size = -1
        if task["path"]:
            try:
                size = os.path.getsize(task["path"])
            except OSError:
                size = -1
        signature.append((
            task["key"], size,
            tuple((str(e.get("id") or ""), str(e.get("state") or ""),
                   str(e.get("due") or ""), str(e.get("fired") or ""),
                   str(e.get("turn") or "")) for e in task["entries"])))
    cache_key = (window_from, to, tuple(signature))
    cached = _WINDOW.get(cache_key)
    if cached is not None:
        return {"items": cached}

    read = tasks_store.read_state()
    now = time.time()
    items = []
    for task in candidates:
        try:
            _place(task)
            messages = _thread(task, read, now)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # one unreadable thread, not an unreadable calendar
        for message in messages:
            if message["kind"] != "scheduled":
                continue
            if window_from <= message["at"] < to:
                items.append({"task_key": task["key"], "message": message})
    items.sort(key=lambda item: item["message"]["at"])
    if len(_WINDOW) >= _WINDOW_MAX:
        _WINDOW.clear()
    _WINDOW[cache_key] = items
    return {"items": items}


class ReadPatch(BaseModel):
    key: str
    message_id: str


@router.post("/api/tasks/read")
def api_task_read(patch: ReadPatch):
    """Mark ONE message read, and report the task's remaining unread count.

    One message, never a prefix: the user clicked MSG-003 and scrolled to it,
    which says nothing about the MSG-002 they skipped. `tasks_store` keeps an
    explicit set for exactly this reason.
    """
    key = patch.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="missing task key")
    number = tasks_store.message_number(patch.message_id)
    if number <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"message_id: expected MSG-nnn, got {patch.message_id!r}")

    tasks_store.mark_read(key, patch.message_id)

    # Recounted from the thread rather than decremented, so the badge the page
    # paints is the truth on disk and not an optimistic guess that drifts.
    task = _collect().get(key)
    if task is None:
        return {"ok": True, "unread": 0}
    _place(task)
    messages = _thread(task, tasks_store.read_state(), time.time())
    return {"ok": True,
            "unread": sum(1 for m in messages if m["unread"])}
