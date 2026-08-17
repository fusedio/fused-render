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
  were appended since — a multi-MB transcript is never re-read. One field on the
  row is deliberately NOT read from that window: `next_run` (with the entry it
  names) is `min(at)` over every pending entry, because the Board orders Upcoming
  by it and three messages cannot answer it. See `_next_run`.
* ``GET /api/tasks/{key}/messages`` — one task's FULL thread. This is the
  "Show more" click: a whole-transcript parse, which is affordable exactly
  because it happens for one task at a time and never for a listing.
* ``GET /api/tasks/scheduled?from&to`` — every SCHEDULED message in a window,
  for the calendar. Separate from the listing rather than a window parameter on
  it, because the listing's three-message tail is right for an accordion and
  wrong for a time axis, and one field cannot mean both.
* ``POST /api/tasks/read`` — mark one message read, or (``all: true``) every
  message in one task, in one request and one store write.

**What a message is.** A user prompt in the transcript, or a scheduled entry.
Those two overlap: a scheduled message that fired IS a prompt in the transcript
(`_send` hands `entry["message"]` over verbatim), so listing both would show it
twice. The full thread therefore JOINS them on the message body, nearest in
time, and the listing — which cannot afford to look at every prompt — counts the
same thing arithmetically: the transcript's prompts, plus the scheduled entries
that never reached a transcript at all (pending, missed, cancelled). Message ids
follow from that count and nothing else: the Nth message in time order is MSG-N
(`tasks_store.message_ids`), so nothing has to be stored and nothing can drift.

**When a task stops being one.** A task with no session whose every scheduled
message has reached a terminal state that never ran — cancelled, skipped, missed
— is not a task any more and is not listed anywhere (`_is_task`). Deleting a
message that never ran leaves no session, no transcript and no history, so there
is nothing for a row to be about; leaving one behind meant an empty shell sitting
in Archive forever. A task that HAS run keeps its row whatever happens to its
entries, because it has a transcript and this app does not destroy transcripts
(D306) — Archive is the honest resting place for that one. This is decided in
`_collect`, which every endpoint below reads, so the listing, the board and the
calendar agree by construction rather than each learning the rule; it is not a
filter, and the default filters are unchanged.

**A task key** is the session id, or `pending:<entry-id>` for a message that
names no session at all and so has none to be filed under yet (§5). A message
that DOES name one — a re-send, a message scheduled out of an open chat — is
that session's task from the moment it is created, even before it runs; see
`_entry_session`. The number a pending row is showing follows it onto the
session id at the first run — see `tasks_store.rekey` — so the row the user has
been watching does not silently renumber the moment it finally runs.

Every field degrades rather than fails. An unreadable transcript, a truncated
line, a session whose cwd is gone, a store that is not there yet — each costs
that one task, or that one fact about it, and never the listing. That is the
posture of every module this one reads from (claude_sessions.py,
claude_artifacts.py, schedule.py) and it is the posture here.

Reads are unguarded, like every other read endpoint. The one write marks a
message read (or a whole task's worth of them, which is the same write with a
wider object) — the same weight of change as `POST /api/claude-sessions/triage`
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


def _prompt(obj) -> dict | None:
    """One transcript record as a chat message, or None if it isn't one.

    Three kinds of `type: user` record are not things a human said. `isMeta`
    records are Claude Code's own caveats. Tool-result-only content has no words
    in it at all. And a whole class is written on the user's BEHALF — a finished
    subagent reporting back, a slash command's envelope, the stdout it captured —
    which on a real machine were a third of every "message" in the store (889
    task-notifications in 2519 records), enough to make a thread mostly
    machinery.

    That last class used to be a local list of leading tags and a blanket drop,
    and the drop was half wrong. `<live-app-state>` and `<pane-shot>` are not
    machinery-only: they are blocks the fused-render Claude page PREPENDS to what
    the user typed, so dropping the record threw the human's words away with
    them — 43 rows in one real store reported no messages at all, and 33 of them
    were this. `tasks_store` now owns the policy and splits the two cases (see
    its tag lists for the corpus counts); this asks it both questions, because
    they are different questions: is the record machinery WHOLE, and if not, what
    is left once the prefixes come off.
    """
    if obj.get("type") != "user" or obj.get("isMeta"):
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    text = tasks_store.first_text(message.get("content")).strip()
    if not text or tasks_store.is_machinery(text):
        return None
    # The remainder can still be empty — annotations or a screenshot sent with no
    # typed words. That IS something the user did, and the chat page labels it
    # with a marker, but a listing row has no body to show for it and an empty
    # bubble in a thread is worse than none.
    body = tasks_store.strip_machinery(text)
    if not body:
        return None
    anchor = obj.get("uuid")
    return {"body": body,
            "at": tasks_store.epoch(obj.get("timestamp")),
            "anchor": anchor if isinstance(anchor, str) else ""}


def _command(obj) -> str:
    """The slash command a non-message user record carries, or "".

    Read only from records `_prompt` has just refused, which is the only place it
    can come from — the envelope IS the whole record. Its one consumer is
    `_title`: a session containing nothing but `/making-a-release` has no prose
    to be named from, and the command the user typed is a truer name than
    nothing."""
    if obj.get("type") != "user":
        return ""
    message = obj.get("message")
    if not isinstance(message, dict):
        return ""
    return tasks_store.slash_command(
        tasks_store.first_text(message.get("content")))


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
        # Not a message — but a slash-command envelope is still worth ONE fact,
        # and for some sessions it is the only fact there is. First one wins,
        # like every other "first message" in this module. See `_title`.
        if not rec.get("command"):
            rec["command"] = _command(obj)
        return
    prompt["body"] = prompt["body"][:_BODY_MAX]
    rec["count"] += 1
    rec["tail"].append(prompt)
    if len(rec["tail"]) > _LISTING_MESSAGES:
        rec["tail"].pop(0)


def _new_scan() -> dict:
    # Every reader of `command` uses `.get`, so a record built before this key
    # existed — one already in `_SCAN` when the module is hot-reloaded under the
    # dev server — degrades to "no command" instead of raising.
    return {"offset": 0, "size": -1, "count": 0, "tail": [], "title": "",
            "command": ""}


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


def _pin_at(record: dict) -> float:
    """When an `in_progress` pin was placed, or 0.0 for one that does not say.

    0.0 is the answer for every pin the Inbox's `autoFlow` wrote — it sends
    `{status}` and nothing else — and for every pin written before the stamp
    existed. Both are exactly the pins that have to be reapable, so the absence
    reads as "older than anything that has happened", which is what 0.0 is.

    Stored as a string because that is the shape of the record: `set_triage.py`
    coerces every field it writes with `str(value).strip()`, so `read` is "1"
    and not 1. Parsed defensively for the same reason — a hand-edited file must
    cost a pin, not the page."""
    try:
        return float(record.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _pin_holds(record: dict, session_id: str, live: bool, busy: set[str],
               active: float) -> bool:
    """Is a recorded `in_progress` still describing something?

    This is the one triage word that is a CLAIM ABOUT THE PRESENT. `done` and
    `archived` are filing decisions and are timeless — they stay true however
    long the card sits in the lane — so they are never asked this question and
    never reaped. "Something is running in this conversation" is falsifiable,
    and nothing was falsifying it.

    Why it had to be: the sessions Inbox's `autoFlow` writes `in_progress` for
    every session it sees running, and only writes it back to `done` if THAT
    SAME PAGE is still open to witness the stop — it gates on an in-memory
    `RUNNING` set that is empty on every load. So every run whose finish no
    Inbox tab watched leaves a pin on disk that outlives it forever, and
    `_status` honoured them unconditionally as "the user's own act". The board
    held five cards in In Progress for days over runs that had recorded `done`
    hours or a day earlier. The Inbox's own `_summarize` names this exact
    failure ("a session that finished while nothing was watching shouldn't sit
    in in_progress forever") and then only guards the untriaged default, which
    is the half that was never the problem.

    THREE independent reasons to hold, and a pin needs only one. They are
    independent on purpose, because each one is wrong on its own in a different
    direction:

    * **`live`** — the shared liveness rule says the transcript is mid-turn.
      Not sufficient alone: a turn thinking through a long tool call appends
      nothing for minutes and reads as not-live while genuinely running.
    * **`busy`** — `schedule.busy_sessions`, the scheduler's own record of a
      send it has not heard back from (`sending`, or `sent` with no verdict).
      This is what covers the liveness gap above for a scheduled run, and it is
      the scheduler's answer rather than a second one of ours. Not sufficient
      alone either: a session a human is typing into has no entry at all.
    * **a stamp later than the session's last activity** — a deliberate pin
      that no run has contradicted. This is what keeps the reopen drag (a
      `done` card dropped back on In Progress, which `dropLanes` offers) from
      being undone on the next 20s poll. `active` is therefore what HAPPENED
      and only that: a due time in the future is later than any stamp a user
      can make, so admitting one here inverts this guard for exactly the tasks
      that have work coming. `_row` keeps the two apart.

    A turn is therefore only reaped when the session is quiet, the scheduler is
    waiting on nothing in it, and the pin predates the last thing that happened
    — three ways of being over, all at once. What is NOT relied on is the
    rendered message's `turn`: `_entry_turn` folds liveness into that field and
    has no word for "sent, no verdict, not live" (it says `idle`), so reading it
    would have collapsed the second guard into the first.
    """
    if live or session_id in busy:
        return True
    return _pin_at(record) > active


def _status(newest: dict | None, session_id: str, triage: dict, live: bool,
            busy: set[str], active: float) -> str:
    """The status a task sits in — ONE decision, made here, for every view.

    Derived from the newest message, then overridden by an explicit triage
    record. Triage wins on disagreement because it is the user's own act: they
    dragged the card, and a derivation that undid that on the next poll would
    make the board unusable. Only a RECORDED status overrides — the default
    claude_sessions applies to an untriaged session (running -> in_progress) is
    a derivation too, and a weaker one than the message's own state. Triage has
    only three words, so a user cannot file a task as `failed`; that is a fact
    about the run, not a place to put it.

    The one exception to triage winning is a stale `in_progress` — a pin whose
    run has ended, which is not the user's act at all but a claim the Inbox
    wrote automatically and had no way to take back. See `_pin_holds`.

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
        if status in _TRIAGE_STATUSES and (
                status != "in_progress"
                or _pin_holds(record, session_id, live, busy, active)):
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
    it (`user`), then Claude Code's own one-liner for the session (`ai`), then
    the first line of the first message. No summarisation call anywhere — the
    title we want is already written into the transcript once per turn.

    FIVE sources, because the last step has three and they are not equally
    trustworthy:

    * `message` — the session's own first prompt, read out of the transcript
      (`tasks_store.head`). This is the step §4 asks for: "the first message
      that we had", a line the session actually opened with.
    * `entry` — the earliest scheduled message asked OF this session, used only
      when there is no readable transcript to take the line above from. It is
      still the best name here, but for a task scheduled from the New task form
      it is the very message being scheduled, so a client prefilling a Title
      field from it writes the description into the name.
    * `command` — the slash command the session ran, when it contains no prose
      at all. Some sessions really are just `/making-a-release` or `/clear`, and
      that is worth saying: six of the ten genuinely-wordless rows on one real
      machine are this shape, and the row's alternative was the envelope quoted
      back at the user as the name of their own conversation
      (`<command-message>making-a-release</command-message>` — a real title on a
      real machine before this).

    `message` and `entry` are the same shape of string and the client has no way
    to tell them apart by looking. It used to guess — refusing a `message` title
    whenever the composed ask began with it — and a guess cannot tell a
    continuation ("pull today's news" as the session's real first prompt, "pull
    today's news and file it" as the new ask) from an echo, so a session lost the
    name the app already knew. Naming the source is the same information without
    the guess.

    Nothing at all is a real answer, and it stays "": a session with no prose and
    no command has nothing true to be called, and the WORDING of that absence is
    the client's to choose. A placeholder invented here would not stay a
    placeholder — it is what the New task form prefills a Title field with, and a
    prefill the user saves becomes the task's permanent name."""
    for entry in reversed(task["entries"]):
        title = str(entry.get("title") or "").strip()
        if title:
            return title[:200], "user"
    if rec is not None and rec.get("title"):
        return str(rec["title"])[:200], "ai"
    body, source = first_prompt, "message"
    if not body:
        for entry in task["entries"]:
            text = str(entry.get("message") or "").strip()
            if text:
                body, source = text, "entry"
                break
    line = body.strip().splitlines()[0].strip() if body.strip() else ""
    if not line and rec is not None and rec.get("command"):
        # No prose anywhere, but the session is not featureless — it ran a slash
        # command, and `_absorb` kept the first one for exactly this. Taken
        # verbatim: it is already a name, and a name a user typed.
        return str(rec["command"])[:200], "command"
    return line[:200], source


# ------------------------------------------------------------ task collection


def _new_task(key: str, session_id: str, path: str | None) -> dict:
    return {"key": key, "session_id": session_id, "path": path, "entries": []}


def _entry_session(entry: dict) -> str:
    """Which session a scheduled entry belongs to: **the answer where the run
    has given one, else the input it named**, and "" for neither.

    The two fields are not synonyms and the difference is the whole of this
    function. `claude_session_id` is the ANSWER — the session the turn actually
    ran in, filled in by the watcher from the run's first reporting tick.
    `session_id` is the INPUT — "resume this conversation", empty meaning
    "start a fresh one".

    Reading only the answer meant an entry that NAMES a conversation but has
    not run yet matched nothing, and fell to `pending:<entry-id>` — a second
    row beside the very task it belongs to, which merged into it the moment the
    watcher reported. Two ordinary things do that: a re-send (it carries the
    failed run's session as its `session_id`, and queues instead of going
    immediately whenever that conversation is mid-turn) and a message scheduled
    out of an open chat.

    **Answer first**, because the two can disagree: a resume that forked into a
    new session RAN in `claude_session_id`, and that is the thread the message
    is in whatever it asked for.

    **"" is not an id.** A message with no `session_id` is asking for a fresh
    session, so it must keep falling through to its own `pending:` key —
    grouping on "" would collapse every unrelated fresh-session message in the
    store into a single row.
    """
    return (str(entry.get("claude_session_id") or "")
            or str(entry.get("session_id") or ""))


# States that mean a scheduled message will never run and never did. In
# `_entry_state`'s vocabulary, so a cancelled or missed OCCURRENCE — which reads
# as `skipped` — is covered by the same tuple, and `error` is NOT: a send that
# broke is news, and `_board_column` gives it its own column.
#
# `sent` and `sending` are obviously excluded, and so is `pending`: a message
# waiting for its time is work that has not happened yet, not work that never
# will.
_NEVER_RAN = ("cancelled", "skipped", "missed")


def _is_task(task: dict) -> bool:
    """Is this still a task at all?

    A task that never ran DISAPPEARS when its work is cancelled; a task that has
    run keeps its row, in Archive. Deleting a scheduled message cancels its
    entry, and for a message that already fired that is exactly right — there is
    a Claude session behind it with a real transcript, and the row is how the
    user reaches it. For a message that never fired there is nothing behind it
    at all: no session, no transcript, no history. The row that used to survive
    was an empty shell filed under Archive, describing work that did not happen
    and cannot be reached.

    Two boundaries, both of which have to hold or the rule destroys something:

    * **A session keeps the row, always.** `session_id` here is what
      `_entry_session` resolved (the run's answer, else the conversation the
      message named), and a transcript-derived task always has one. If a
      conversation exists, the row stays even with every entry cancelled — the
      transcript is the thing worth keeping (D306), and Archive is where it
      belongs.
    * **Anything left to run keeps the row.** One `pending` entry among a
      hundred cancelled ones is upcoming work, and a row it must appear in. Only
      when NOTHING is left to run, and nothing ever ran, does the task go.

    A mixed thread — one cancelled entry, one that sent — has run, so it stays.
    """
    if task["session_id"] or not task["entries"]:
        # No entries and no session cannot happen — a session-less task exists
        # only because an entry made it — and is kept rather than dropped
        # anyway, because `all()` over nothing is true and a bug upstream must
        # not turn into a row silently disappearing.
        return True
    return not all(_entry_state(entry) in _NEVER_RAN
                   for entry in task["entries"])


def _collect() -> dict[str, dict]:
    """Every task on this machine: one per transcript, plus one per scheduled
    message that names no session at all — minus the ones that are no longer
    tasks (`_is_task`).

    A scheduled entry whose session has no transcript on disk still makes a
    task — the session may be seconds old, it may not have been started yet
    (`_entry_session` groups a pending entry onto the conversation it is going
    to continue), or the transcript may have been moved — rather than dropping
    the user's message on the floor. That task is the same one the run joins
    later, because the key it is filed under does not change when the watcher
    fills the answer in."""
    tasks: dict[str, dict] = {}
    for path in tasks_store.transcripts():
        session_id = os.path.splitext(os.path.basename(path))[0]
        tasks[session_id] = _new_task(session_id, session_id, path)
    for entry in schedule.list_entries():
        if entry.get("state") == schedule.RECURRING:
            # A template never fires and is not a message; its materialised
            # occurrences are, and they are ordinary entries in this list.
            continue
        session_id = _entry_session(entry)
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
    # The drop happens HERE, once, rather than in each endpoint: the listing,
    # the full thread, the calendar window and the read endpoint all collect
    # through this function, so a task that is no longer a task is absent from
    # every one of them and no view has to know why.
    return {key: task for key, task in tasks.items() if _is_task(task)}


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


def _next_run(entries: list[dict]) -> tuple[float, str]:
    """When this task NEXT runs, and WHICH entry that run is — `(0.0, "")` for a
    task with nothing pending.

    This exists because the three messages a row carries cannot answer it. The
    tail is the three newest by `at`, and on this branch an OVERDUE pending is an
    ordinary state (past scheduling is allowed, catch-up is unbounded), so two
    sent runs plus next month's occurrence are enough to push the run that should
    happen FIRST out of the window entirely. The Board orders Upcoming by
    soonest-next-run; read from the window alone that order buries exactly the
    work it exists to surface. `min(at)` over every pending entry is the fact the
    lane actually wants, and here — where the whole set is already in hand,
    before the tail is cut — it is free.

    Widening `task.messages` was the other way to close it and is the wrong one:
    a fourth (or twentieth) message is another row of tail held per session for
    every session on the machine, paid on every poll, to fix a minority of rows.

    TWO fields rather than one, because the sort and the button have to widen
    TOGETHER. `runNowTarget` fires an ENTRY ID, and a card promoted to the top of
    Upcoming on a run whose id the row does not carry would Run now some other
    message than the one the order just promised. So an entry with no readable id
    is not eligible to be the named next run at all: naming it would put the lie
    back, one field further along.

    `_entry_at` is the due time and never `fired` (see there), and an entry with
    no readable due time is skipped for the same reason — the alternative is
    claiming the task runs next at the epoch, which would pin it to the top of
    the lane forever.

    On an exact tie the FIRST in store order wins, which is the older of the two
    (the store appends). A tie is also the one case where the client may fire a
    different entry than the one named here: `runNowTarget` prefers a message it
    is HOLDING over an equally-due one it can only name, because that one has a
    printed id. Both are due at the same second, so the time the lane orders by
    is the same either way and the order still promises what the button sends.
    """
    best_at = 0.0
    best_id = ""
    for entry in entries:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        entry_id = str(entry.get("id") or "")
        if not entry_id:
            continue
        at = _entry_at(entry)
        if not at:
            continue
        if not best_at or at < best_at:
            best_at, best_id = at, entry_id
    return best_at, best_id


def _row(task: dict, number: str, triage: dict, read: dict, now: float,
         busy: set[str]) -> dict:
    """One listing row. The tail parse only: three messages, and a count.

    `busy` is `schedule.busy_sessions` over the WHOLE store, computed once by
    the caller — see `_pin_holds`. Over the whole store rather than this task's
    own entries because a resume that forked is filed under the session it RAN
    in (`_entry_session` reads the answer first) while it still holds the
    session it NAMED busy, and that one is another row."""
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
    # BEFORE the cut, from the whole set: the one fact about the future that the
    # three-message window cannot be trusted to hold. See `_next_run`.
    next_run, next_run_entry = _next_run(task["entries"])
    tail = tail[-_LISTING_MESSAGES:]
    _turn_of_newest_chat(tail, live)
    for offset, message in enumerate(reversed(tail)):
        message["message_id"] = tasks_store.format_message_id(total - offset)
    _mark_unread(tail, task["key"], read)

    newest = tail[-1] if tail else None
    # TWO times, because "recent" is two questions here and one number could
    # only answer them by lying to one of them.
    #
    # `active` is the last thing that actually HAPPENED in this session, and
    # nothing that has not happened may enter it — it is the clock `_pin_holds`
    # measures a deliberate `in_progress` stamp against. `ran_at` is when a
    # message ran (a caught-up run is news today, whatever day it was due) and
    # 0.0 until it does; `at` is the due time, which never moves and can be in
    # the FUTURE (see `_entry_at`). Reading `at` here was reaping every pin on a
    # task with a message still to come, because no stamp a user can make is
    # later than tomorrow — the exact reopen the stamp exists to protect.
    if newest is not None:
        active = max(active, newest["ran_at"] or 0.0)
    if not active and task["entries"]:
        # Nothing has run and there is no transcript to date: what happened is
        # that the message was ASKED for, and `created` is when. Deliberately
        # not `_entry_at` — a due time is the other question, below — and `or
        # 0.0` because `epoch` answers None for a stamp it cannot read, while
        # every time on this row is a float and 0.0 is how it says "never".
        active = tasks_store.epoch(task["entries"][-1].get("created")) or 0.0
    # The sort's question is the other one: the List is read newest-first, and a
    # message scheduled for tomorrow belongs near the top where it can be seen
    # BEFORE it fires. So the row's `last_active` keeps the due time — only the
    # pin's clock stops at what has happened.
    surfaced = max(active, (newest["at"] or 0.0) if newest is not None else 0.0)
    if not surfaced and task["entries"]:
        surfaced = _entry_at(task["entries"][-1])
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
        # `active`, not `surfaced`: a pin is stale exactly when it predates the
        # last thing that HAPPENED, and a run still ahead has not happened.
        "status": _status(newest, task["session_id"], triage, live, busy,
                          active),
        "failed": _failed(newest),
        "live": live,
        "unread": _unread_count(task, total, unfired, read),
        "last_active": surfaced,
        "message_count": total,
        # The next run, and the entry it belongs to — `min(at)` over every
        # pending entry, not over the window below. 0.0 / "" when the task has
        # nothing pending, which is how every other absent time on this row
        # reads (`last_active`, a message's `ran_at`). See `_next_run`.
        "next_run": next_run,
        "next_run_entry": next_run_entry,
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
    Excludes the ones that stopped being tasks: no session, and nothing left to
    run — see `_is_task`. That is an absence of a task, not a filter hiding one.
    """
    triage = sessions._load_state("triage.json")
    read = tasks_store.read_state()
    now = time.time()
    tasks = _collect()
    # One pass over the store for every row: which conversations the scheduler
    # is still waiting on. See `_pin_holds`.
    busy = schedule.busy_sessions(schedule.list_entries())
    for task in tasks.values():
        _place(task)
    numbers = _numbers(tasks)
    rows = []
    for task in tasks.values():
        try:
            row = _row(task, numbers.get(task["key"], ""), triage, read, now,
                       busy)
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

    A task that is no longer a task (`_is_task`) has no chips here either — the
    collection this reads has already dropped it. That is the point of deciding
    it once: a cancelled never-run message still drawing a chip for a task the
    listing does not contain would be a visible disagreement between two views
    of the same store, and the chip's own row would be unreachable.

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
    # ONE message, or — with `all` — every message in the task. Exactly one of
    # the two, and `message_id` stays optional rather than becoming a magic
    # empty string: a request that names neither is a client bug and is told so
    # (400) rather than quietly clearing a whole thread.
    message_id: str | None = None
    all: bool = False


@router.post("/api/tasks/read")
def api_task_read(patch: ReadPatch):
    """Mark ONE message read — or the WHOLE task — and report what is left.

    One message is the default and still means only that message: the user
    clicked MSG-003 and scrolled to it, which says nothing about the MSG-002 they
    skipped, and `tasks_store` keeps an explicit set for exactly this reason.

    `{"key": ..., "all": true}` is the other ask, and it is the same endpoint
    rather than a sibling because it is the same sentence with a different
    object: this verb has always been "mark read", and the only thing that
    changed is how much. It stays ONE request either way — a task with 89
    messages was 89 posts and 89 recounts through the per-message route, which
    is what made "clear this task" something a person did by clicking through
    every row.

    Both are exact. The whole-task branch enumerates the thread and marks the
    messages that are actually unread, so a message still PENDING is left alone
    (it has not happened, so there is nothing to have missed) and cannot come
    back already-read when it fires.
    """
    key = patch.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="missing task key")

    if patch.all:
        if patch.message_id:
            raise HTTPException(
                status_code=400,
                detail="send message_id or all, not both")
        return _read_whole_task(key)

    if patch.message_id is None:
        raise HTTPException(
            status_code=400,
            detail="missing message_id (or all: true to mark the task read)")
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


def _read_whole_task(key: str) -> dict:
    """Mark every message of one task read, in one store write.

    The thread is parsed FIRST because it is what defines the mark: the ids come
    from the messages that are unread right now, not from a count, so nothing
    that has yet to happen is swept in. `_mark_unread` is then re-run over the
    same list to recount — the same "read it back off disk" rule the
    per-message branch follows, and for the same reason: the number the page
    paints has to be the store's answer, not an optimistic guess.

    A task that has gone (deleted transcript, expired pending entry) is not an
    error and writes nothing: a whole-task mark is defined by a thread, and there
    is no thread to define it.
    """
    task = _collect().get(key)
    if task is None:
        return {"ok": True, "unread": 0}
    _place(task)
    now = time.time()
    messages = _thread(task, tasks_store.read_state(), now)
    unread_ids = [m["message_id"] for m in messages if m["unread"]]
    if unread_ids:
        tasks_store.mark_read_many(key, unread_ids)
        _mark_unread(messages, key, tasks_store.read_state())
    return {"ok": True, "unread": sum(1 for m in messages if m["unread"])}
