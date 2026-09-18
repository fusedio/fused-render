"""Tasks — one row per Claude Code session, with its thread of messages.

A **task is a session**, 1:1, and a thread is the messages that entered it —
from the explorer's Claude chat, from the template chat, or from a schedule. The
thread does not care which; that is the whole point of collapsing the two stores
the app used to keep side by side (the scheduled-message list it owns, and the
session transcripts Claude Code owns, joined by one field).

The endpoints, and the split between the first three is the design constraint
this file is written around:

* ``GET /api/tasks`` — every task, newest first, each carrying its **three most
  recent** messages. This runs for every row on the page and is polled, so
  nothing in it may scale with transcript size. Transcripts are append-only, so
  the scan below reads each file **once** and thereafter only the bytes that
  were appended since — a multi-MB transcript is never re-read. One field on the
  row is deliberately NOT read from that window: `next_run` (with the entry it
  names) is `min(at)` over every pending entry, because the Board orders Upcoming
  by it and three messages cannot answer it. See `_next_run`.
* ``GET /api/tasks/pulse`` — the same task states reduced to the four fields the
  global sidebar needs. It deliberately carries no titles, paths, descriptions,
  or message bodies; Home should not download the Tasks page just to draw its
  status dot and unread count.
* ``GET /api/tasks/{key}/messages`` — one task's FULL thread. This is the
  "Show more" click: a whole-transcript parse, which is affordable exactly
  because it happens for one task at a time and never for a listing.
* ``GET /api/tasks/scheduled?from&to`` — every SCHEDULED message in a window,
  for the calendar. Separate from the listing rather than a window parameter on
  it, because the listing's three-message tail is right for an accordion and
  wrong for a time axis, and one field cannot mean both.
* ``POST /api/tasks/read`` — mark one message read, or (``all: true``) every
  message in one task, in one request and one store write.
* ``POST /api/tasks/archive`` — file one task away. ONE gesture with two halves
  (cancel the work, archive the session), which is why it is a verb here rather
  than a triage write the client composes. See the archiving section at the end.
* ``POST /api/tasks/unarchive`` — take that filing back, and nothing else: the
  work archiving cancelled stays cancelled, no run starts, and the task lands in
  whatever lane it DERIVES into rather than one a caller names. Same section.
* ``POST /api/tasks/delete`` — take the ROW away for good: cancel the pending
  work exactly as archive does, then tombstone the key so no listing shows it.
  The transcript is not touched (D306) and new activity in the conversation
  revives the row rather than running invisibly. Same section.
* ``POST /api/tasks/erase`` — the same, and then the SESSION itself: the
  transcript, its subagent sidecars, and every record this app keeps about it.
  Nothing survives to revive, which is the whole difference from delete (D307).
  Same section.

**Drafts ride on this listing** (fused_render/drafts.py, routers/drafts.py).
Two joins, no new list endpoint: every session row gains `draft` — the one-line
preview of whatever is sitting unsent in that conversation's composer, or None
— and every TASK draft is emitted as a row of its own (`kind:"draft"`,
`state:"draft"`, key `draft:<id>`, no number). Both travel down
`/api/tasks/changes` like any other change to a row, which is what makes the
`✎ Draft` chip appear and a new draft land in Upcoming without a reload. Both
are absent from the pulse, deliberately — see `api_tasks_pulse`.

A task draft that names a `session_id` is the exception to all of that: it is
the next message of a conversation that already has a row and a number, so it
is NOT A ROW AT ALL. No draft row is emitted for it and no number is minted
(`_draft_rows`, `_draft_numbers`); the session's own row stands exactly as it
always did — same title, same lane, same number, same click into the chat — and
merely gains two fields: `bound_draft`, the form's id, and `draft`, its preview,
so the row wears the red `✎ Draft` chip like any other unsent words
(`_bound_chips`). Nothing is hidden anywhere, which is the point: a stand-in row
was a second thing to read, it took the conversation off the Cards wall (a wall
of transcripts, which a form has none of), and it made one session row open a
modal where every other opens the chat (bugbot, PR #1126; Akshil, 2026-09-12).
The way back into the form is the composer's Schedule hop, which reopens the
draft already bound to that session instead of minting another.

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
import logging
import os
import re
import shutil
import threading
import time
import uuid

from fastapi import APIRouter, Body, Header, HTTPException, Query
from pydantic import BaseModel

from fused_render import (
    current_apps,
    drafts,
    project_queue,
    queue_manager,
    schedule,
    session_liveness,
    tasks_store,
    tasks_watch,
)
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers import claude_sessions as sessions
from fused_render.server.routers import schedule as schedule_api
from fused_render.shell import prefs as shell_prefs

router = APIRouter()

logger = logging.getLogger(__name__)

# A task's status, decided HERE and read by every view. `upcoming` and `blocked`
# are the two triage does not have a word for — a session cannot be "not yet",
# and the Inbox's three columns have no place to put a run that stopped — which
# is why the union below is not symmetric.
#
# `blocked` is a STATUS and not only the `failed` boolean beside it, because a
# run that broke is not a kind of done. It used to be exactly that: `done` with
# a flag, so every view had to remember to read the flag and paint it, and any
# view that did not simply lost the news. One decision, made once, on the
# server.
#
# THE LANE IS CALLED `blocked` AND NOT `failed` (Akshil, 2026-09-03: "failed
# status could be renamed as blocked … for fail retry, for block a reason"). The
# word names what the reader has to do about it rather than what happened: a run
# that broke and a run parked on a card nobody answered are the same fact from
# the outside — this task is not moving until a person does something — and one
# lane holding both is what stops a parked run being read as work in flight. The
# `failed` boolean is untouched and still says which of the two it was; it is
# what paints the ring red and what `blocked_reason` reports as "failed".
#
# `needs_attention` is the one status above In Progress: the run IS in flight,
# and it is in flight in the one way that will never end on its own. It is a
# status rather than a flag for the same reason `blocked` is — a view that has
# to remember to read a flag is a view that will one day not, and this is the
# fact the whole feature exists to carry.
#
# `queued` is a STATUS and not a flag beside `upcoming`, for the reason `blocked`
# gives and one more of its own. The reason it shares: a lane the reader has to
# assemble from a boolean is a lane some view will one day forget to assemble,
# and this one has three surfaces (List chip, Board lane, chat bubble) that must
# agree about one task. The reason that is its own: a queued task is NOT upcoming
# — its message is due, it would be running this second, and the only thing
# between it and a process is another task holding the same folder
# (`project_queue`). "Upcoming" is a promise about the clock; this is a fact
# about the machine, and merging them would put work that is late into a lane
# the reader scans for work that is early. It sits between `upcoming` and
# `in_progress` because that is where it sits in time: asked for, not yet
# running. It only ever appears with the project queue flag on
# (`project_queue.enabled`); with the flag off nothing derives it.
STATUSES = ("upcoming", "queued", "in_progress", "needs_attention", "blocked",
            "done", "archived")

# There was a `LIVE_STATUSES` here, read by a swap that hid a session's row
# behind the task draft bound to it and narrowed to the settled statuses so a
# run in flight could never vanish. The swap is gone (bugbot, PR #1126): this
# listing hides nothing, the row says `bound_draft` instead, and there is no
# pair left for any view to fold together.

# What `blocked_reason` may say. "" is the answer for every task that is neither
# blocked nor waiting on anybody — most of them.
BLOCKED_REASONS = ("permission", "question", "failed", "usage_limit", "")

# The ONE triage word this router still reads. `archived` is a FILING state —
# the user put the task away — and filing is the only decision about a task that
# is the user's to make. `in_progress` and `done` were read here too and are
# not any more: a task's status is now derived from what its messages did (see
# `_status`), and In Progress in particular is Claude's output rather than a
# lane a person may drop a card into. A recorded `in_progress` (the sessions
# Inbox's `autoFlow` writes one for every session it sees running, and cannot
# take it back once its page closes) is therefore ignored rather than reaped —
# which is the same outcome the reaping machinery was built to reach, without
# the machinery.
_FILED = "archived"

# How many messages a listing row carries. The accordion shows three and offers
# "Show more"; the fourth costs another row of tail to keep in memory for every
# session on the machine.
_LISTING_MESSAGES = 3

# How much of a prompt the listing keeps per message. Long enough to read a
# scheduled message whole (they are one-liners), short enough that keeping three
# per session for a few thousand sessions is megabytes, not gigabytes. The full
# thread endpoint does not truncate.
_BODY_MAX = 2000

# How much of the NEWEST message a listing row carries as its `last_message` —
# one line of it, and this many characters of that line. A card's title row is
# one clamped line wide, so anything past it is bytes on every poll for text no
# surface can show.
_LAST_MESSAGE_MAX = 200

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
    tasks_watch.reset()


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
    # `isSidechain` is a subagent's brief, which the user never typed — skipped
    # by every other reader of a transcript's prompts (tasks_store.head,
    # claude_sessions, agent.py) and, until 2026-09-15, not by this one.
    if (obj.get("type") != "user" or obj.get("isMeta")
            or obj.get("isSidechain")):
        return None
    message = obj.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    text = tasks_store.first_text(message.get("content")).strip()
    if not text or tasks_store.is_machinery(text):
        return None
    # The remainder can still be empty — annotations or a screenshot sent with no
    # typed words. That IS something the user did, and until 2026-09-18 this
    # dropped it, which is worse than an empty bubble by a long way: `_status`
    # derives the status FROM the messages, so a chat whose every send was
    # wordless (annotate the app, hit send, type nothing — both real sends in
    # one reported session) had no messages to derive from, vanished from this
    # page entirely, and read `done` in the chat list while its turn ran. The
    # chat list disagreed because it is a different reader that already took the
    # second step below; nothing took the third.
    #
    # `user_words` is the whole answer and it is spelled once, in `tasks_store`:
    # the typed words, else the notes the user wrote on their pins, else the
    # chat's own name for what the send carried ("pane screenshot"). "" now
    # means a record that carried nothing a reader could name at all, which is
    # still not a message.
    body = tasks_store.user_words(text)
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


# An API failure — Wi-Fi off, a usage limit, a 429 — never reaches the schedule
# store: Claude Code writes it into the TRANSCRIPT as an assistant row flagged
# `isApiErrorMessage`, whose text is the whole report ("You've hit your session
# limit · resets 7:20pm"). Nothing in this module used to read an assistant row
# at all, which is exactly why a chat turn that never got an answer read `done`
# in /tasks (R2-3, R2-14): the prompt was in the file, so the message was
# in the thread, so the task had happened.
#
# Read by SUBSTRING, never by parsing. Both read paths screen a line before
# `json.loads` precisely because a transcript is mostly assistant turns and
# tool results, and widening that screen into "parse every assistant row" would
# trade this bug for the cost these endpoints exist to avoid. Two spellings of each
# hint because Claude Code writes compact JSON while a hand-written fixture
# writes `json.dumps` defaults; the alternative is de-spacing every line.
_API_ERROR_HINTS = ('"isApiErrorMessage":true', '"isApiErrorMessage": true')
# …and the one API failure that is not a failure. Claude Code writes the plan's
# usage limit into the transcript as an ordinary `isApiErrorMessage` row whose
# text is the CLI's own sentence ("Claude usage limit reached", "You've hit your
# session limit · resets 7:20pm"), so it arrives here indistinguishable from a
# 500 — and the chat, meanwhile, has SCHEDULED a continuation for the moment the
# window reopens (PR #1107). Nothing is broken and there is nothing to retry:
# the run is waiting on a clock. Matched by substring on the same screened line,
# lowercased, for the same reason every other hint here is — see `_reply_fate`.
_LIMIT_HINTS = ("usage limit", "session limit")
_ASSISTANT_HINTS = ('"type":"assistant"', '"type": "assistant"')
_TEXT_HINTS = ('"type":"text"', '"type": "text"')
_USER_HINTS = ('"type":"user"', '"type": "user"')
# A subagent's rows — its brief AND its replies — ride the same transcript
# with `isSidechain: true`. `_prompt` refuses the brief; this screen refuses
# the reply, so a subagent's prose is never the row's `last_message` and its
# API error is never pinned on the user's own prompt.
_SIDECHAIN_HINTS = ('"isSidechain":true', '"isSidechain": true')


def _reply_fate(line: str) -> bool | None:
    """What one raw transcript line says about the newest prompt's LAST answer:
    True for "it failed with an API error", False for "it was answered
    normally", None for a line that is not an assistant reply.

    False is as load-bearing as True. A turn that errored and was RETRIED — the
    Wi-Fi came back, the limit reset — has an ordinary reply after the error
    row, and reporting that turn as blocked would be the same defect pointing
    the other way. So an ordinary assistant text row clears the mark, which is
    what makes the rule "the last reply" instead of "any reply".

    `isApiErrorMessage` rides on ordinary assistant rows too, as `false`, so
    the test is the flag's VALUE and never its presence.

    A `type: user` record is refused up front whatever it quotes: the hints are
    substrings, a human can paste `"type":"assistant"` into a prompt, and
    swallowing that line here would throw away the message the user typed —
    which is the bug this reader is supposed to be fixing, not causing. An
    assistant row that quotes `"type":"user"` loses its vote by the same rule
    and the turn keeps its old reading; a missed mark costs the old answer,
    a stolen prompt costs a message.
    """
    if any(hint in line for hint in _USER_HINTS):
        return None
    if any(hint in line for hint in _SIDECHAIN_HINTS):
        return None  # a subagent's reply: not this conversation's turn
    if any(hint in line for hint in _API_ERROR_HINTS):
        return True  # checked first: an error row is a text row as well
    if (any(hint in line for hint in _ASSISTANT_HINTS)
            and any(hint in line for hint in _TEXT_HINTS)):
        return False
    return None


def _limit_hit(line: str) -> bool:
    """Is this API-error line the plan's usage limit rather than a break?

    Asked only of a line `_reply_fate` has already called an error, so the test
    is just which KIND. See `_LIMIT_HINTS` for why it is a substring."""
    text = line.lower()
    return any(hint in text for hint in _LIMIT_HINTS)


def _mark_fate(prompts: list[dict], fate: bool, line: str = "") -> None:
    """Record an assistant reply's fate on the newest prompt seen SO FAR — the
    prompt it is a reply to. The mark lives on the prompt dict, which is where
    both read paths keep their state (`_SCAN`'s `tail`, `_FULL`'s list), so a
    scan that resumes from its saved offset carries the mark across polls
    instead of re-deriving it from bytes it will never read again.

    `limit` rides beside `failed` and is cleared by the same ordinary reply
    that clears it: a turn the usage limit ended and a turn the network ended
    are both failures, and only the first is one the user can do nothing about
    but wait. See `_LIMIT_HINTS` and the row's `blocked_reason`."""
    if prompts:
        prompts[-1]["failed"] = fate
        prompts[-1]["limit"] = bool(fate) and _limit_hit(line)


def _absorb(rec: dict, line: str) -> None:
    """Fold one raw transcript line into a scan record. Screened before parsing:
    a transcript is mostly assistant turns and tool results, and `json.loads` on
    every one of them is the cost this endpoint cannot pay."""
    # An assistant reply is the one non-user line with a fact to contribute:
    # whether the newest prompt's last answer was an API error. Substring tests
    # only, and it stays on this side of `json.loads`. See `_reply_fate`.
    fate = _reply_fate(line)
    if fate is not None:
        _mark_fate(rec["tail"], fate, line)
        # ...and an ORDINARY reply is also the newest thing Claude has said, so
        # the line is KEPT — as the raw string, still unparsed. That is the
        # whole of how `last_message` stays on the cheap side of the screen
        # above: the candidate is overwritten by every later reply and only the
        # survivor is ever handed to `json.loads` (`_condense_reply`), so a
        # transcript of a thousand assistant turns costs one parse, not a
        # thousand. An API-error row (`fate is True`) is not kept: its text is
        # the failure report, which the row already says in `blocked_reason`.
        #
        # The raw line lives no longer than the scan that read it — `_scan`
        # condenses it to the one-line row before returning, and nothing that
        # wide is ever parked in `_SCAN` between polls. See `_condense_reply`.
        if fate is False:
            rec["reply_line"] = line
            rec["reply_last"] = True
        return
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
    # A prompt read AFTER the kept reply is the later turn, whatever the two
    # timestamps say — the file is append-only. So the reply is DROPPED, not
    # merely outranked: it answered an older prompt and can never again be the
    # newest thing said. Deciding that by timestamp instead let a reply with a
    # real `at` beat a prompt whose timestamp did not parse (read as 0.0), and
    # a row wore a days-old reply as its title (Akshil, 2026-09-15).
    rec["reply"] = None
    rec["reply_line"] = ""
    rec["reply_last"] = False
    rec["count"] += 1
    rec["tail"].append(prompt)
    if len(rec["tail"]) > _LISTING_MESSAGES:
        rec["tail"].pop(0)


def _new_scan() -> dict:
    # Every reader of `command` uses `.get`, so a record built before this key
    # existed — one already in `_SCAN` when the module is hot-reloaded under the
    # dev server — degrades to "no command" instead of raising.
    # `reply_line`/`reply`/`reply_last` are read with `.get` for the same
    # reason — a record built before they existed degrades to "nothing said
    # yet" (and, for `reply_last`, to the stricter timestamp rule) rather than
    # raising under the dev server's hot reload.
    return {"offset": 0, "size": -1, "count": 0, "tail": [], "title": "",
            "command": "", "reply_line": "", "reply": None, "reply_last": False}


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
        _condense_reply(rec)
    rec["size"] = size
    _SCAN[path] = rec
    return rec


def _one_line(text: str) -> str:
    """A message as a ROW can show it: its first non-empty line, capped.

    The shell clamps the line it draws to one line anyway; doing it here is what
    keeps the clamp from being paid for in bytes on every poll, and it is the
    same rule the title takes (`_title` → first line, 200 characters)."""
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:_LAST_MESSAGE_MAX]
    return ""


def _reply_said(line: str) -> dict | None:
    """One kept assistant line as `{role, text, at}`, or None when there is
    nothing a row could show for it.

    The ONLY `json.loads` this feature adds, and it runs at most once per scan
    (see `_condense_reply`). A row with no text block — a turn that was pure
    tool_use — is None: the substring screen lets one through whenever the LINE
    holds a text hint anywhere, and "Claude ran a tool" is not something Claude
    said.
    """
    try:
        obj = json.loads(line)
    except ValueError:
        return None  # truncated line: the next scan re-reads it whole
    if not isinstance(obj, dict) or obj.get("type") != "assistant":
        return None
    message = obj.get("message")
    if not isinstance(message, dict):
        return None
    text = _one_line(tasks_store.first_text(message.get("content")))
    if not text:
        return None
    return {"role": "assistant", "text": text,
            "at": tasks_store.epoch(obj.get("timestamp")) or 0.0}


def _condense_reply(rec: dict) -> None:
    """Boil the kept assistant line down to the one-line `{role, text, at}` the
    row is emitted as, and drop the raw bytes. Called once per scan that read
    new bytes, from `_scan`.

    ONE `json.loads` PER SCAN, not one per assistant turn — the promise
    `_absorb` makes — and now also one per scan rather than one per emitted
    row, which is what keeps `_SCAN` small: the raw line is a whole transcript
    record wide (a turn with a big tool payload is kilobytes), and parked on
    the record it stayed that wide for the life of the process for every
    transcript whose `last_message` nobody ever asked for — a listing narrowed
    past the task, or the `task_card_last_message` pref simply off. Condensed
    here it is a few hundred bytes per transcript, bounded by
    `_LAST_MESSAGE_MAX`, whoever reads it. Parsing back in `_absorb` would
    bound it too and cost a parse per assistant turn instead of per scan, which
    is the cost this screen exists to avoid.

    A line that would not parse leaves the previous answer standing rather than
    blanking the row: the scan's offset only ever advances to a newline, so the
    line comes back whole on the next call.

    So does a turn that said NOTHING — a pure tool_use row that passed the
    substring screen — and deliberately: "Claude ran a tool" does not un-say
    the words before it, so the newest thing said is still the reply it stands
    on, and blanking that would hand the row back to a prompt Claude has
    already answered. What keeps THAT honest is `reply_last`: a line with no
    words in it is not the newest thing said, so the standing reply stops
    winning ties it can no longer claim (`_last_message`), and a prompt read
    after it wins outright. (One reply per scan is all the candidate slot
    holds, so a wordless turn that arrives in the SAME scan as the reply before
    it does take that reply's place — the row falls back to the prompt for a
    poll. That is the screen's standing trade: one parse, the survivor's.)
    """
    line = rec.get("reply_line") or ""
    if not line:
        return
    rec["reply_line"] = ""
    said = _reply_said(line)
    if said is not None:
        rec["reply"] = said
    else:
        rec["reply_last"] = False


def _last_message(rec: dict | None) -> dict | None:
    """THE NEWEST THING SAID IN THIS CONVERSATION — `{role, text, at}` — or
    None for a task nobody has said anything in yet.

    Both halves come from the incremental scan and neither is a second read:
    the newest prompt is the last of the three the row already carries, and the
    reply is the line `_absorb` kept. Whichever is newer wins, which is the
    whole claim the field makes — a card titled by it is showing the last turn
    of the conversation, whoever took it.

    A prompt read AFTER the reply DROPS it in `_absorb` — the file is
    append-only, so the later line is the later turn and no clock is asked —
    which is why the comparison below never sees that case. What it still
    settles is the other way a kept reply stops being newest: a later turn
    with NO words in it (`_condense_reply` clears `reply_last`, keeps `reply`).
    `>=` while `reply_last` holds, so a reply with no usable timestamp (0.0,
    beside a prompt with the same) still beats the prompt it answers; `>` once
    it is gone, so the tie goes to the prompt. A record from before the key
    existed reads as False and takes the stricter test, which loses nothing a
    timestamp can settle.
    """
    if rec is None:
        return None
    said = None
    tail = rec.get("tail") or []
    if tail:
        text = _one_line(tail[-1].get("body"))
        if text:
            said = {"role": "user", "text": text, "at": tail[-1].get("at") or 0.0}
    reply = rec.get("reply")
    if reply is not None and (
            said is None
            or (reply["at"] >= said["at"] if rec.get("reply_last")
                else reply["at"] > said["at"])):
        said = reply
    return said


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
                # The same widening as `_absorb`'s, for the same reason and
                # ahead of the same parse: the thread path and the listing path
                # have to agree about a failed turn or the row and the messages
                # it opens contradict each other.
                fate = _reply_fate(line)
                if fate is not None:
                    _mark_fate(prompts, fate, line)
                    continue
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


def _queue_at(entry: dict) -> float:
    """**When the message joined the LINE** — its due time, or the moment Run
    now was pressed on it if that came first.

    The counterpart of `_entry_at`, and split from it for the reason that
    function exists at all: the two questions are different facts about one
    entry. `_entry_at` is what was ASKED FOR — it places the chip on the
    calendar and must never move, which is why `run_now` does not rewrite `due`.
    This is what is WAITING TO GO, and Run now on a message due next Tuesday, in
    a folder somebody else is holding, really is waiting to go (browser QA,
    2026-09-12: the row fell back to `upcoming` the moment the optimistic paint
    cleared, and the scheduler would not have sent it until Tuesday).

    Everything that reads "due ≤ now" for a QUEUE purpose reads this;
    everything that draws a time reads `_entry_at`. `schedule._queue_due` is the
    same rule on the scheduler's side, and a line listed here in one order and
    sent there in another is worse than no line at all.
    """
    due = _entry_at(entry)
    asked = tasks_store.epoch(entry.get("run_now_at"))
    if not asked:
        return due
    return min(due, asked) if due else asked


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


def _entry_turn(entry: dict) -> str:
    """How the turn a scheduled message started is going: "" while it is in
    flight, `done` once it ended, `cancelled` for one the user stopped,
    `unknown` where the watcher said so.

    THE STORE IS THE AUTHORITY on "in flight", and this deliberately no longer
    folds transcript liveness in. `turn` is written exactly once, when the
    turn ends, and the sweep closes an abandoned turn as `unknown` within one
    tick (schedule.py `_claim_due`) — so `sent` with no `turn` IS a running
    turn, whatever the transcript's mtime says. Folding liveness in was the
    board's half of the queue/board desync: a turn thinking through a long
    tool call appends nothing for minutes, this read that as `idle`, and
    `_message_verdict` then filed a RUNNING task under Done — the board said
    finished while the dock, reading the store, said thinking. The dock's own
    live list has always been `sent && !turn`; this makes the board read the
    same store fact instead of second-guessing it with a heuristic that
    exists for pure chat turns (which have no watcher and keep it —
    `_turn_of_newest_chat`).

    `cancelled` passes through by name rather than collapsing into `done`:
    the dock and the schedule list say "Stopped" for it, and the board's rows
    must use the same word. The LANE is still Done (`_message_verdict`) —
    a stop the user asked for is a settled outcome, not a fault."""
    turn = str(entry.get("turn") or "")
    if turn in ("unknown", "cancelled"):
        return turn
    if turn:
        return "done"
    return ""


def _scheduled_message(entry: dict, at: float, ran_at: float,
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
        # One shape for both kinds. A scheduled run's verdict comes from the
        # watcher and not from the transcript, so nothing sets this on that
        # road; the field is here so no reader has to branch on `kind` to ask.
        "limited": False,
        # When the turn's verdict LANDED — 0.0 until it has (and for entries a
        # pre-stamp version of the store wrote). `ran_at` is when the run
        # started; this is when it was pronounced over, which is the moment
        # `_verdict_outvotes_live` measures the transcript's tail against.
        "turn_at": tasks_store.epoch(entry.get("turn_at")) or 0.0,
        "unread": False,
        "entry_id": str(entry.get("id") or ""),
        "template_id": str(entry.get("template_id") or ""),
        # Was this message PLANNED for its time, or does it merely have one?
        # A task typed into the List or the Board with the when-row untouched
        # runs now because now is the form's default — nobody put it on a
        # calendar, so the calendar does not draw it (schedule-lib.taskChips).
        # False for every entry stored before the flag existed, which is the
        # right reading: they all came from a form that asked for a time.
        "immediate": bool(entry.get("immediate")),
        "turn": _entry_turn(entry),
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
        # A typed message carries no verdict stamp — only the watcher writes
        # one — and 0.0 is how every stamp here says "never".
        "turn_at": 0.0,
        "unread": False,
        "entry_id": "",
        "template_id": "",
        # A chat turn whose LAST answer was an API error did not happen, and
        # `error` is the word `_message_verdict` reads as blocked. Hardcoding
        # `done` here was the whole of R2-3/R2-14: a turn that died with the
        # Wi-Fi sat in the Done lane looking answered. `state` stays `sent` —
        # the message really was delivered, and the verdict is what changes.
        "turn": "error" if prompt.get("failed") else "done",
        # …AND WHETHER THAT FAILURE WAS THE PLAN'S USAGE LIMIT, which is a
        # different thing to a reader and to the row: nothing is broken, there
        # is nothing to retry, and the chat has already scheduled the
        # continuation for the moment the window reopens (PR #1107). The row
        # turns it into `blocked_reason: "usage_limit"` and `resumes_at`; the
        # verdict itself stays `error`, because the turn really did not finish.
        "limited": bool(prompt.get("limit")),
        "anchor": prompt["anchor"],
    }


# How far back of the merged thread a live send looks for ITSELF before deciding
# it is not there yet. The transcript's own clock and this process's clock are
# the same machine's, but a prompt is stamped by Claude Code when it WRITES the
# record and the mark is stamped when the page pressed send — and the CLI takes
# a moment to get going. Wide enough to cover that gap, and no wider: beyond it
# the same words are a person saying the same thing twice, which is two
# messages and must read as two.
_SENT_MARK_WINDOW_SEC = 15.0


def _sent_message(mark: dict) -> dict:
    """The live send, shaped as a message — the newest one in the thread.

    A SCHEDULE-ENTRY message, deliberately, and not a chat one: `state: "sent"`
    with `turn: ""` is the one shape `_message_running` reads as a turn in
    flight, so the row derives `in_progress` from the thread itself rather than
    from a second rule about marks that `_status` would have to know. The row
    shows the words and turns its ring on in the same poll, off one fact.

    Every id field is empty because there is nothing on disk to point at yet:
    no entry made this message, no template, and there is no transcript record
    to anchor a scroll on. `at` and `ran_at` are both the moment the send
    happened — the two can only disagree for a message that WAITED for a time,
    and this one was typed and sent in the same breath (`_chat_message` says
    the same thing for the same reason). `immediate` is True for that same
    reason, so no calendar draws a chip for it.
    """
    at = float(mark.get("at") or 0.0)
    return {
        "message_id": "",
        "kind": "scheduled",
        "body": str(mark.get("text") or ""),
        "at": at,
        "ran_at": at,
        "state": schedule.SENT,
        "turn_at": 0.0,
        "unread": False,
        "entry_id": "",
        "template_id": "",
        "immediate": True,
        "turn": "",
        "anchor": "",
    }


def _fold_sent_mark(messages: list[dict], mark: dict) -> bool:
    """Add the live send to `messages` as its newest — unless it is ALREADY
    there. True when one was added (the caller owes the count a message).

    THE DEDUPE IS THE WHOLE FUNCTION. A mark is a claim about a message that is
    on its way to disk, and the moment it arrives the thread holds it for real:
    keeping both would show the user their own sentence twice for whatever was
    left of the fifteen seconds, in a list whose entire promise is that a
    scheduled message and the prompt it became are ONE message (`_merge`).

    Two ways the same send can already be in the thread, and both are checked
    on the body, stripped and compared whole — the same join `_merge` makes,
    for the same reason: the text is handed to the session verbatim.

    * **A TRANSCRIPT PROMPT.** Time-qualified, at or after the mark's own moment
      less `_SENT_MARK_WINDOW_SEC`: an OLD prompt with the same words is a
      different message (the user asking the same thing again is the ordinary
      way a chat works), and only one written around the moment of this send can
      be this send.
    * **A SCHEDULED ENTRY.** NOT time-qualified, and deliberately: an entry
      already in the store IS this message — it has its own row in the thread,
      its own state, its own verdict — and its `at` is the time it was DUE,
      which for a caught-up run is days away from when it actually went. Time
      cannot be the test there, and the body is enough: the entry says what was
      sent.

    A mark with no words at all (a caller that only wanted the liveness floor)
    adds nothing: there is no message to show, and an empty bubble in the thread
    would be worse than the ring on its own.
    """
    body = _body_key(mark.get("text"))
    if not body:
        return False
    floor = float(mark.get("at") or 0.0) - _SENT_MARK_WINDOW_SEC
    for message in messages:
        if _body_key(message["body"]) != body:
            continue
        if message["kind"] != "chat":
            return False  # the entry this send came from is already the message
        if max(message["at"] or 0.0, message["ran_at"] or 0.0) >= floor:
            return False  # the prompt landed; the mark has nothing left to say
    messages.append(_sent_message(mark))
    return True


def _merge(prompts: list[dict], entries: list[dict]) -> list[dict]:
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
        messages.append(_scheduled_message(entry, at, ran_at, anchor))
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
            # NOT over an API failure. The failed turn is a FACT read out of
            # the transcript (`_reply_fate`); liveness is a heuristic about the
            # file's mtime, and letting it write "" / "idle" here handed the
            # broken turn straight back to Done — the R2-3 defect surviving its
            # own fix, one function later.
            if message["turn"] != "error":
                message["turn"] = "" if live else "idle"
            return


# --------------------------------------------------------------- the statuses


def _message_running(message: dict) -> bool:
    """Is THIS message's run happening right now?

    Two shapes and no third. `sending` is the scheduler holding a send it has
    spawned and not heard back from; `sent` with a turn that has not reported an
    end is a turn in flight. `_entry_turn` writes "" for exactly that case (and
    `_turn_of_newest_chat` writes "" over the newest typed prompt while the
    transcript is live), which is why the empty string is the running answer
    here rather than an absence to be defaulted away.

    `unknown` is deliberately NOT running: the watcher said it stopped being
    able to tell, and reporting that as work in progress is the frozen
    progress-bar lie. `error` is not running either, for the stronger reason:
    the transcript already holds the API failure that ended the turn.
    """
    state = message["state"]
    if state == "sending":
        return True
    return state == "sent" and message["turn"] in ("", "running")


def _message_archived(message: dict, filed: bool) -> bool:
    """Is this message filed away — out of the conversation the task is having?

    TWO ways in, and the second is the cascade:

    * the message's own state says so. `cancelled` and `skipped` are the two —
      a run the user called off, and an occurrence that never happened. Neither
      is an outcome anybody is waiting to read.
    * the TASK is archived, and archiving a task archives what is in it. The one
      exception is a message that is still RUNNING: a run cannot be filed away
      while it is happening, so it keeps going, the task keeps reading In
      Progress (`_status` asks about running first), and the whole task falls
      into Archive by itself the moment the run ends. Nothing has to remember to
      finish the job later — the derivation simply answers differently once the
      last message stops running.
    """
    if message["state"] in ("cancelled", "skipped"):
        return True
    return filed and not _message_running(message)


def _message_verdict(message: dict) -> str | None:
    """What this message has to SAY about how it went — or None when it has
    nothing to say yet.

    None is the interesting answer and it is what makes a recurring task read
    correctly. A `pending` message is a promise, not a report: a task whose last
    run finished and whose next occurrence is already on the books has unread
    OUTPUT sitting in it, and filing it under Upcoming because the newest row in
    the thread happens to be in the future hides exactly the thing the person
    has to look at. So a pending message says nothing and the run before it
    speaks (`_status`).

    `missed` stays `done`, unchanged: it is only reachable at all on an install
    that set FUSED_RENDER_SCHEDULE_MAX_LATE (a missed OCCURRENCE reads as
    `skipped`), the row already paints it red off the `failed` flag, and
    promoting it to the Blocked lane is a separate decision nobody has made.

    A scheduled `sent` with NO turn yet is the other promise: the session
    started and the watcher has not pronounced it over, so it has nothing to
    say — it is RUNNING (`_message_running`), not done. Answering `done` here
    was the premature-Done half of the queue/board desync: in the window
    between the spawn and the watcher's first report (no claude_session_id in
    the busy set yet, no transcript to be live) the board filed a running task
    under Done while the dock said thinking. Scheduled only: a CHAT message's
    empty turn means the transcript's own liveness ran out, which has no
    watcher behind it and keeps its old reading.

    A turn the user STOPPED (`cancelled`) still answers `done` — the lane for
    a settled outcome — and the word "Stopped" rides on the message's `turn`
    (`_entry_turn`), so the board and the dock describe the stop identically.

    `error` joins `unknown` in the blocked answers: it is a chat turn whose
    last reply out of the transcript was an API failure (`_reply_fate`), which is a
    turn nobody answered — the same reader-must-do-something fact `unknown`
    carries, arrived at from the transcript instead of from the watcher.
    """
    state = message["state"]
    if state == "error":
        return "blocked"
    if state == "sent":
        if message["turn"] in ("unknown", "error"):
            return "blocked"
        if message["kind"] == "scheduled" and not message["turn"]:
            return None
        return "done"
    if state == "missed":
        return "done"
    return None


def _waiting(messages: list[dict], filed: bool) -> bool:
    """Is there anything in this task still to come?

    THE OTHER HALF OF UPCOMING, and the half that was missing (Akshil,
    2026-08-18: an Upcoming card could not be dragged into In Progress any more).
    "No output yet" was read as enough on its own, which put every session whose
    transcript surfaces no prompt at all — one that ran only `/clear`, or
    `/making-a-release` — into Upcoming. On one real machine that was every card
    in the lane: nine of them, each with `message_count: 0`.

    Those cards are unrunnable BY CONSTRUCTION and correctly so — the drag into
    In Progress fires a pending message and they have none — so the lane filled
    up with the only cards in it that could not do the one thing it exists for.
    The lane was the lie, not the drag: `dropLanes` was refusing a drop on a card
    that had nothing to drop.

    So Upcoming means work that has not happened but is going to, which is a
    message still WAITING: not filed away, and with no verdict yet
    (`_message_verdict` answers None for exactly the promises). A task with none
    of those and nothing to report is over, and `done` is where it goes — the
    same answer this server gave before the derivation landed ("a task with
    nothing in it happened and is over"), for the same reason.
    """
    return any(not _message_archived(m, filed) and _message_verdict(m) is None
               for m in messages)


def _speaker(messages: list[dict], filed: bool) -> dict | None:
    """The message a task's status is reading off: the most recent one that is
    neither filed away nor still waiting to happen.

    "Most recent" is position in the thread, which `_merge` has already ordered
    by time. Skipping the archived ones is what makes filing a message a real
    gesture: cancel the newest message and the one before it speaks again.
    """
    for message in reversed(messages):
        if _message_archived(message, filed):
            continue
        if _message_verdict(message) is not None:
            return message
    return None


def _archive_record(session_id: str, triage: dict) -> dict | None:
    """This session's `archived` record, or None. The only triage word still
    read here — see `_FILED`."""
    record = triage.get(session_id) if session_id else None
    if isinstance(record, dict) and record.get("status") == _FILED:
        return record
    return None


def _filed_at(record: dict) -> float:
    """When the filing was made, epoch seconds, or 0.0 for a record that does
    not say.

    Stored as a string because that is the shape of the record (`set_triage.py`
    coerces every field it writes, so `at` is "1.0" and not 1.0), and parsed
    defensively for the same reason: a hand-edited file must cost a filing, not
    the page.

    0.0 means the record does not say WHEN, and `_revived` reads that as "no
    revival": a filing whose date is unknown cannot be shown to have been
    overtaken, and the alternative — treating it as older than everything —
    would make every archive the sessions Inbox has ever written (its own
    `set_triage.py` stamps nothing) revive itself on the very next poll. Every
    archive this app writes carries a stamp (`claude_sessions.write_triage`),
    so the door below is open for every filing a person can make here."""
    try:
        return float(record.get("at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _revived(messages: list[dict], filed_at: float) -> bool:
    """Has this task DONE something since it was filed away?

    THE AUTOMATIC WAY OUT OF ARCHIVE IS ACTIVITY (Akshil, 2026-08-18): "if you
    want to move it to in progress or done, just type in a message inside that
    chat and it will automatically move". This door has to be a real one: the
    filing is dropped (`clear_triage`), not overlooked for one poll.

    The other door is the drag — a card lifted out of the Archive lane
    (`api_task_unarchive`) — and it is the SAME drop of the SAME record, which is
    why neither has to know about the other. Nothing here changes because a
    gesture exists: activity still un-files a task nobody dragged.

    WHICH ACTIVITY, and the distinction is the whole function. `ran_at` is when
    a message actually happened, so:

    * a run that was ALREADY IN FLIGHT when the task was filed started before
      the stamp. It does not revive anything — it keeps going, the card reads In
      Progress while it does (rule 1 in `_status`), and the task settles back
      into Archive when it ends. That is the promise the archive cascade already
      makes and it is unchanged.
    * a message that arrives AFTERWARDS — a prompt typed into the conversation,
      a run someone started — happened after the stamp, and that is new work in
      a task somebody had finished with. The filing is stale and goes.

    A message that has not happened yet does not count: `ran_at` is 0.0 until it
    does, so a run scheduled into an archived task revives it when it RUNS,
    which is when there is something to come back for.

    An unstamped filing revives on nothing at all — see `_filed_at`.
    """
    if filed_at <= 0:
        return False
    return any((m["ran_at"] or 0.0) > filed_at for m in messages)


def _running_now(session_id: str, live: bool, busy: set[str]) -> bool:
    """Is something happening in this conversation RIGHT NOW, whatever its
    messages say?

    Two independent halves, either of which is enough and neither of which is
    sufficient alone: `live` is the transcript mid-turn — or the sender's own
    word that a turn just started (`tasks_watch.mark_running`, folded into
    `live` by `_live`) — and `busy` is the scheduler waiting on a send it has
    not heard back from. A turn thinking through a long tool call appends
    nothing and reads as not-live; a session a human is typing into has no
    scheduler entry at all.

    The first seconds of a send used to have a third half here — the queue's
    admission reservation read as running (2026-09-12) — because nothing on
    disk said a turn had started until `claude` registered. #1163 answers that
    for every send, flag or no flag: the page marks the session the moment it
    sends and `_live` believes the mark. One mechanism for one fact; closing the
    double-spawn gap is the queue's own job and is now the manager's owner
    (`queue_manager.started`, filed by the admission).

    Its own function so `_status`'s first rule and anything else that has to ask
    cannot drift apart about what "running" means. The third way — a message of
    this task's own that is in flight — is `_message_running`, and `_status`
    asks both.

    A FOURTH half, checked first and only to say no: the queue manager's own
    word that this session's turn already ended (`tasks_watch.mark_turn_ended`,
    off the session host's `turn_ended`/`exited` event), for the one gap
    neither `live` nor `busy` can see through. On a folder handoff the manager
    knows the turn is over before anything on disk agrees — the registry row
    can still read `busy` until Claude Code next rewrites it, and the
    transcript's own tail is inside `session_liveness`'s window for the CLI's
    closing records, which land a beat later still. Both of those feed `live`
    (`_live`, `tasks_watch.live_from_registry`), so without this a finished
    task and the one the manager just handed the folder to could both read
    `in_progress` for as long as that gap lasts — never two OWNERS, but two
    rows saying so. `tasks_watch.is_turn_ended` is itself "ended, and nothing
    NEWER disagrees" (a fresher mark, or a registry row truly rewritten busy
    since), so this does not discount a genuinely new turn on the same
    session — only the stale echo of the one that just closed. `busy` is left
    alone: the scheduler holding a send in flight is an independent claim this
    function has never adjudicated, on this session or any other.
    """
    if not session_id:
        return live
    if session_id in busy:
        return True
    if live and tasks_watch.is_turn_ended(session_id):
        return False
    return live


# How much newer than its verdict the transcript's tail must be before the tail
# stops being the finished turn's own echo and starts being evidence of new
# work. The closing records land in the same breath as the verdict (the watcher
# stamps `turn_at` the moment the run reports done, seconds after the last real
# record) — this window absorbs that ordering jitter and clock skew, while a
# session genuinely still working appends records well past it.
#
# WIDENED FROM 5s (2026-08-21), then MOVED to `session_liveness` (2026-09-12).
# Five seconds only covered the watcher's own ordering jitter, and the CLI's
# teardown is slower than that on a busy machine: one late record dated 6-20s
# after `turn_at` put the board back on In Progress for the rest of the
# 45-second liveness window while the dock, which reads the store, had said
# finished — the same two-surface disagreement from the other side. 15s is the
# largest value that still lets a session which is GENUINELY still working keep
# its vote (TASK-001's pin is 27s past the verdict and must stay In Progress).
#
# The move is what the scheduler needed: `schedule._verdict_echo` weighs the
# same tail against the same stamp to stop the per-session hold holding a
# message against a turn that has already ended, and it may not import this
# router. One constant, two readers — see `session_liveness.VERDICT_ECHO_SEC`.
_VERDICT_ECHO_SEC = session_liveness.VERDICT_ECHO_SEC


def _verdict_outvotes_live(messages: list[dict], active: float) -> bool:
    """Is the transcript's liveness just the echo of a run that has already
    reported its verdict?

    THE 45-SECOND WINDOW LIES IN EXACTLY ONE DIRECTION (Akshil, 2026-08-19: "if
    finished in one, finished in the other"). The bottom-right queue card flips
    to finished within seconds of the result row landing — the watcher records
    the turn's verdict the moment it sees it — while this page kept answering In
    Progress for the rest of the liveness window, because the transcript's tail
    had been written to seconds ago. Of course it had: the records were the
    finished turn's OWN closing rows. Counting a run's obituary as a pulse is
    how the two surfaces disagreed about the same run for up to a minute.

    So: when the newest thing that actually HAPPENED in this thread is a
    scheduled run that has spoken (`_message_verdict` — done or failed), and no
    message claims to be running by its own state, bare liveness has nothing
    left to attest and the caller sets it aside. `ran_at` is the comparison on
    both sides because it is the one stamp that means "happened": a prompt the
    user types after the run is a chat message with a newer `ran_at`, so a
    genuinely new turn keeps its In Progress. The tie goes to the verdict — the
    prompt a scheduled run fired is consumed by the join (`_merge`) and cannot
    also stand on the chat side, so an equal stamp is the run's own.

    BUT THE VERDICT MAY ONLY SILENCE ITS OWN ECHO (TASK-001, Akshil,
    2026-08-19: a task wore the green Done ring while Claude was visibly still
    building the app in its session). A resolved turn is not the end of a
    conversation — the session can keep working with no new prompt to show for
    it (follow-up turns, background work), and none of that is a "message"
    here, so the newest thing that HAPPENED stayed the verdict for as long as
    the work ran and the suppression never lifted. `active` is the transcript's
    own answer — the timestamp of its newest real (non-housekeeping) record —
    and `turn_at` is the moment the watcher pronounced the turn over: a tail
    meaningfully newer than the verdict (`_VERDICT_ECHO_SEC`) is new work, and
    the transcript keeps its vote. An entry a pre-stamp store wrote has no
    `turn_at` and keeps the old rule — by the time such an entry matters its
    transcript has long gone stale anyway.

    Deliberately NOT consulted: `busy_sessions`. The scheduler holding a send in
    flight is an independent claim and `_running_now` keeps asking it whatever
    this answers — this function only decides whether the TRANSCRIPT gets a
    vote.
    """
    if any(_message_running(m) for m in messages):
        return False  # something really is in flight; liveness corroborates it
    verdict_at = 0.0
    resolved_at = 0.0
    other_at = 0.0
    for message in messages:
        happened = message["ran_at"] or 0.0
        if message["kind"] == "scheduled" and _message_verdict(message) is not None:
            verdict_at = max(verdict_at, happened)
            resolved_at = max(resolved_at, message["turn_at"] or 0.0)
        else:
            other_at = max(other_at, happened)
    if not (verdict_at > 0.0 and verdict_at >= other_at):
        return False
    if resolved_at > 0.0 and active > resolved_at + _VERDICT_ECHO_SEC:
        return False  # written to well after the verdict: that is a pulse
    return True


def _limit_outvotes_live(messages: list[dict]) -> bool:
    """Is the transcript's liveness the echo of a turn the PLAN'S USAGE LIMIT
    ended?

    The same shape as `_verdict_outvotes_live` and for the same reason, from
    the other road. A chat turn that hits the limit is over — the CLI writes
    the failure row and the `-p` run is reaped — but the failure row is itself a
    write, so the 45-second window read the corpse as a pulse and the board put
    the task in In Progress with the red failed ring on it, which is two
    answers about one run (Akshil's screenshot, 2026-09-12). Worse than
    untidy: the chat has already scheduled the continuation for the reset (PR
    #1107), so In Progress is the one lane that hides the fact that nothing
    will happen until then.

    No clock in it, because there is nothing to be an echo OF: a usage-limit
    row is not a verdict the watcher stamped, it is the end of the turn stated
    in the transcript itself. The guard is the ordinary one — a message that
    claims to be running by its own state is believed, so a later turn (the
    comeback firing, a line typed after the reset) takes the vote straight
    back."""
    if any(_message_running(m) for m in messages):
        return False
    for message in reversed(messages):
        if _message_verdict(message) is None:
            continue
        return bool(message.get("limited"))
    return False


# --------------------------------------------------- what a run is waiting on
# A run parked on a card nobody has answered is invisible from every fact this
# module already reads. The transcript stops growing, the process stays alive,
# `busy_sessions` still holds the send — which is precisely the shape of a slow
# turn, and precisely why an unattended session gets stuck for hours (Akshil,
# 2026-09-03: "when a task is waiting for approval it should give a notification
# and indicator in task needs attention").
#
# The one place the fact is written down is the RUN DIR: `perm/<id>.req.json`
# with no `.res.json` beside it (agent.py `_permissions`). So the listing looks
# there — once per listing, over the runs tree, rather than once per row: the
# scan is `os.listdir` plus a couple of small reads per live run, and doing it
# per row would re-read every run dir once for every task on the machine.
#
# READ ONLY, and through the sanctioned seam. agent.py is a TEMPLATE, outside the
# package's import graph by design (SPEC PY-15), and `claude_spawn.load_agent()`
# is how in-process readers reach it — canvases.py's `_agent_module` is the same
# door for the same reason. Nothing here writes a decision: answering a card is
# the chat's job, and a listing that could deny one would be a poll with a
# verdict in it.

# How many run dirs one scan reads, and the walk itself, are both
# `project_queue.scan_runs` — ONE bounded pass over the tree, shared with the
# holder derivation. There used to be two of them, and a listing paid for both:
# the same 120 directories opened twice for one /api/tasks, which is exactly the
# cost the bound was there to avoid. See `project_queue.RUN_SCAN_LIMIT` (why the
# tail is cut at all) and `project_queue.SCAN_TTL` (why one walk answers both).

def _agent_module():
    """The claude template's agent.py, loaded once, or None if it will not load.

    `project_queue.agent_module()` does the loading and the caching — of the
    FAILURE as well as the success, because `load_agent` execs the whole module
    on every call and this is on the listing's path. Kept as a name HERE rather
    than called through at each use because two things depend on it being one:
    the listing degrades to "nothing is parked" when it answers None, and the
    router's own suite replaces exactly this name with a stand-in (agent.py is a
    TEMPLATE outside the import graph, SPEC PY-15).

    ONE COPY, not two. This function and the queue's were the same twenty lines
    with different log lines, and two caches over one exec meant a machine whose
    agent.py will not load paid for the discovery twice and could answer
    differently on the two paths — the listing saying nothing is parked while the
    queue said every folder is free.
    """
    return project_queue.agent_module()


def _run_sessions(agent, run_dir: str, meta: dict) -> set:
    """Every session id this run answers to — `project_queue.run_sessions`.

    BOTH SPELLINGS, for the reason `agent._live_run` spells out and that module
    documents at length: a run knows the session it RESUMED and the one the CLI
    minted for it, and either can be the id a task row carries. The parked scan
    below and the holder derivation next door have to agree about which sessions
    a run answers to, or a run could be parked for one of them and holding a
    folder for the other.
    """
    return project_queue.run_sessions(agent, run_dir, meta)


def _attention_of(agent, perm: dict) -> dict:
    """One unanswered request, as the row's `attention` — tool, and one line.

    The line is the same one the chat's own status line prints for a tool call
    (`agent._tool_detail`), so the List row and the transcript describe the same
    call in the same words. A QUESTION card has no tool call to describe — the
    model is asking the user something — so the question itself is the line;
    without it the row would say "AskUserQuestion" and nothing else, which names
    the machinery rather than the ask.
    """
    tool = str(perm.get("tool") or "")
    inp = perm.get("input") if isinstance(perm.get("input"), dict) else {}
    summary = ""
    if tool == getattr(agent, "ANSWERABLE_TOOL", "AskUserQuestion"):
        questions = inp.get("questions")
        if isinstance(questions, list) and questions:
            first = questions[0]
            if isinstance(first, dict):
                summary = " ".join(str(first.get("question") or "").split())
        if len(summary) > 80:
            summary = summary[:77] + "…"
    else:
        try:
            summary = agent._tool_detail(tool, inp)
        except Exception:  # noqa: BLE001 — a summary we cannot make is ""
            summary = ""
    return {"tool": tool, "summary": summary}


def _parked_runs() -> dict:
    """`session id -> {reason, tool, summary}` for every LIVE run parked on a
    card nobody has answered. Empty when nothing is waiting, which is the
    ordinary case and the cheap one.

    Three conditions, and all three are load-bearing:

    * **Unanswered.** `_permissions` returns the whole list — answered cards
      included, so a re-attaching frame can rebuild them — and a run whose cards
      were all answered minutes ago is a run that is simply working. Only a
      request with no `decision` is somebody being waited on.
    * **Alive.** A dead process cannot be unblocked, and its last card sits in
      the run dir for ever. A needs-attention row for it would be a lane that
      only ever fills up, with an Open button onto a conversation nobody can
      answer.
    * **Oldest first.** `_permissions` sorts by file name, which is the order the
      requests were raised, so the FIRST unanswered one is the one the run is
      actually blocked on. A later card can exist (a sub-agent's), and reporting
      it would describe a question the user is not being asked yet.

    AND A FOURTH, ONLY WITH THE PROJECT QUEUE ON: **nobody is being waited on if
    the user has already answered.** A card answered while the folder was busy
    leaves no `.res.json` in the run dir — the decision is held
    (`queue_manager.held_answer`) and written when the folder frees — so the
    request still reads as unanswered here. Reporting it would make the row
    `needs_attention`, which sits above `queued` in `_status` and would put
    "Answer queued — runs next" permanently out of reach: the one row the whole
    held-answer path exists to paint could never be painted. The user has
    answered; what the task is waiting on is the folder, which is what `queued`
    says.

    One walk of the runs tree, shared with the manager's `blocked` check
    (`project_queue.scan_runs`) — the `meta.json` and the session ids come back
    already read, and only the pid is touched here.

    AND ONE `_permissions` PER RUN, shared with it too. Both readers ask the
    same run the same question on the same listing — this one what it is parked
    ON, the manager's `blocked` check whether it is parked at all — and the
    answer is a directory listing plus a file per card.
    `project_queue.run_permissions` reads it once
    onto the scanned record, which lives exactly as long as the walk does
    (round-2 review, 2026-09-12).

    Best-effort throughout: a run dir that will not read costs that run's news,
    never the listing.
    """
    agent = _agent_module()
    if agent is None:
        return {}
    out: dict = {}
    for run in project_queue.scan_runs(agent):
        run_dir = run["run_dir"]
        held = _held_requests(run)
        perms = project_queue.run_permissions(agent, run)
        waiting = [p for p in perms
                   if not p.get("decision")
                   and str(p.get("id") or "") not in held]
        if not waiting:
            continue
        # Liveness LAST: it is the only check that touches a pid, and the one
        # above has already thrown out every run that is not waiting on anybody.
        if not project_queue.run_alive(agent, run_dir):
            continue
        perm = waiting[0]
        news = _attention_of(agent, perm)
        reason = ("question"
                  if perm.get("tool") == getattr(agent, "ANSWERABLE_TOOL",
                                                 "AskUserQuestion")
                  else "permission")
        for session_id in run["sessions"]:
            # NEWEST RUN WINS: `scan_runs` answers newest-first and run ids lead
            # with a timestamp, so the first answer for a session is its most
            # recent run. A resumed conversation can have several runs in the
            # tree and only the newest is the one on screen.
            out.setdefault(session_id, dict(news, reason=reason))
    return out


def _held_requests(run: dict) -> set:
    """`{request id}` on THIS run the user has already answered — decisions the
    queue manager is holding until the folder frees
    (`queue_manager.held_answer`).

    Empty with the flag off, and empty on the ordinary machine where nothing has
    been held. See `_parked_runs`'s fourth condition for why an
    answered-but-undelivered card must not read as waiting on anybody.

    ASKED PER RUN, not once for the whole tree (PR 2, 2026-09-17). The manager
    files an answer under the TASK KEY it belongs to — the same key the listing
    keys a row by — rather than in a flat list of decisions, so the question
    this asks is "has any session of this run got one held, and is it against
    this very run". The run id has to match: a held answer names the run it was
    raised against, and replaying it into a later run of the same conversation
    would be a verdict about a question nobody asked.

    Best-effort like everything else on this walk: a manager that cannot answer
    costs the held-answer refinement, never the listing."""
    if not project_queue.enabled():
        return set()
    try:
        manager = queue_manager.peek()
        if manager is None:
            return set()
        run_id = str(run.get("run_id") or "")
        out = set()
        for session_id in run.get("sessions") or ():
            answer = manager.held_answer(session_id) or {}
            if str(answer.get("run_id") or "") != run_id:
                continue
            request_id = str(answer.get("request_id") or "")
            if request_id:
                out.add(request_id)
        return out
    except Exception:  # noqa: BLE001 — an unreadable index holds nothing
        return set()


# ------------------------------------------------------------------ the queue
# ONE TASK IN PROGRESS PER FOLDER (`project_queue`, flag `project_queue_enabled`).
# A task whose work is due into a working tree another task is holding does not
# run and does not fail — it WAITS, and `queued` is the word for that. Everything
# under this heading is the listing's half of that rule: reading who is waiting,
# where they stand in the line, and who is in front of them. NOTHING HERE
# DECIDES ANY OF IT any more (PR 2, 2026-09-17) — the order and the owner are
# `fused_render/queue_manager.py`, one event-driven index, and this end reads it
# the way `busy` and `parked` beside it read theirs.
#
# Read once per listing and handed to `_row`, for the reason every other join on
# this page is: it is a fact about every OTHER task on the machine, and asking
# it per row would pay for it once per task.


def _queue_lines(tasks: dict[str, dict], now: float,
                 by_id: dict | None = None) -> dict[str, dict]:
    """`task key -> {"key", "position", "ahead_key", "ahead", "priority"}` for
    every task WAITING on a folder somebody else is holding. Empty with the flag
    off, which is the ordinary case and the cheap one.

    THE LINE IS NOT DERIVED HERE ANY MORE (PR 2, 2026-09-17). Where a task
    stands is the queue manager's index — an event-driven pointer table, one
    line per folder, position = array index — and this READS it
    (`queue_manager.positions()`). The stamp-ranked pass that used to live here
    rebuilt the order out of the scheduler's store and the held-answer store on
    every listing, which is exactly the derivation the manager replaces: two
    readers of one order is how a line gets printed one way and run another.

    THE SHAPE IS UNCHANGED, deliberately: `_row` reads these five fields and
    `_name_ahead` fills the other four, so the listing and the client below it
    never learn where the order came from.

    `ahead_key` IS THE TASK DIRECTLY IN FRONT OF THIS ONE, not always the holder
    (Akshil, 2026-09-12) — the manager answers it per item: position 1 behind
    the folder's owner, position n behind position n - 1, with held answers
    stepped over rather than named. "Behind TASK-041" on every card in a line of
    four said the same thing four times.

    `ahead` is the number a reader sees, `ahead_title` the name, and
    `ahead_session` / `ahead_target` the pair a reader CLICKS (tasks-lib
    `taskHref`) — all four filled in by `_name_ahead`, which needs the listing's
    own allocation pass and therefore cannot happen here.

    `tasks`, `now` and `by_id` are the caller's collection, clock and entry map.
    Nothing here needs them now that the order is read rather than computed;
    they stay in the signature because every caller already holds them and the
    flag-off road (which answers `{}` above) is untouched until PR 3.
    """
    if not project_queue.enabled():
        return {}
    # `peek`, NEVER `get`: a listing must not be the thing that builds the
    # manager. The build reconciles, and reconciling pumps — so the first GET in
    # a fresh process would claim the head of every line and rekey the very rows
    # it was asked to draw. Nothing has queued anything yet in that process, and
    # `{}` is the true answer (see `queue_manager.peek`).
    manager = queue_manager.peek()
    if manager is None:
        return {}
    return {task_key: _queue_row(place)
            for task_key, place in manager.positions().items()}


def _queue_row(place: dict) -> dict:
    """One slot in the shape `_row` reads: the manager's answer, plus the four
    naming fields `_name_ahead` fills in afterwards.

    ONE CONSTRUCTOR FOR BOTH READERS — the listing (`_queue_lines`) and the
    endpoints that have just moved a line and have to redraw it
    (`_queue_place`) — so a row and the reply about that row cannot carry
    different fields. Every value is coerced here rather than trusted, because
    the manager's index is a file on disk and a half-written slot must cost a
    "not queued", never a broken listing.

    `priority` IS A FACT ABOUT THE HEAD OF THE LINE, NOT ABOUT THE GESTURE
    (Akshil, 2026-09-16): the manager sets it only for the item standing first
    that got there by a skip or an answer, so a promoted task outranked by a
    newer press reads "2nd in line" and keeps the button that would make it
    first.
    """
    return {
        "key": str(place.get("key") or ""),
        "position": int(place.get("position") or 0),
        "ahead_key": str(place.get("ahead_key") or ""),
        "ahead": "",
        "ahead_title": "",
        # "Behind TASK-041" is a sentence the reader wants to FOLLOW, and the
        # chat in front is one link away: the same two fields every other thread
        # link in this app is built from. "" for a holder nothing can name — a
        # run that has not published its session yet — and the client then
        # prints the words without the link rather than offering a click that
        # goes nowhere.
        "ahead_session": "",
        "ahead_target": "",
        "priority": bool(place.get("priority")),
    }


def _name_ahead(queue: dict[str, dict], numbers: dict[str, str],
                tasks: dict[str, dict]) -> None:
    """Fill each queued task's `ahead`, `ahead_title`, `ahead_session` and
    `ahead_target` — the number and the name of the task in front, which is the
    only way a reader ever refers to one ("behind TASK-041 · Nightly deploy"),
    and the pair that opens its conversation.

    `numbers` is the listing's own allocation pass where there is one, because
    that is the map that has just MINTED a number for a task seeing its first
    listing. The stored table answers for everybody else — the holder of a folder
    is very often outside a narrowed `/api/tasks/changes` answer, and a chip that
    said "behind " with nothing after it would be worse than saying nothing.
    Read once, and only when something is actually missing.

    A holder the collection does not contain (a terminal `claude` the app never
    started, a session erased between the scan and here) leaves both fields "",
    and the client says "behind a run in this folder". Naming half of it would be
    worse than naming none: a number with no title reads as a row the user can
    click through to, and there would be nothing there.

    The LINK is the same answer read one field further on. `ahead_session` and
    `ahead_target` are `tasks-lib.taskHref`'s two arguments, taken off the same
    collected task as the title so the three cannot describe different rows, and
    both "" for a holder that has not got a session yet (a `starting` run, a
    claimed message the scheduler has not spawned) — there is no conversation to
    open until one exists, and `taskHref` itself answers null for exactly that.
    """
    wanted = {q["ahead_key"] for q in queue.values() if q["ahead_key"]}
    if not wanted:
        return
    stored = tasks_store.task_ids() if wanted - set(numbers) else {}
    facts = {key: _ahead_facts(tasks, key) for key in wanted}
    for q in queue.values():
        key = q["ahead_key"]
        if not key:
            continue
        number = numbers.get(key) or ""
        if not number:
            record = stored.get(key) or {}
            if record.get("n"):
                number = tasks_store.format_task_id(record["n"])
        fact = facts.get(key) or _NO_AHEAD
        q["ahead"] = number
        q["ahead_title"] = fact["title"]
        q["ahead_session"] = fact["session"]
        q["ahead_target"] = fact["target"]


_NO_AHEAD = {"title": "", "session": "", "target": ""}


def _ahead_facts(tasks: dict[str, dict], key: str) -> dict:
    """One task's `{"title", "session", "target"}` — everything the queue's
    surfaces say about the row in FRONT, for a caller that has the collection
    but not the rows.

    The surfaces print who is ahead ("behind TASK-041 · Nightly deploy") and
    open it, and the task ahead is by definition another row — often one a
    narrowed answer was never asked about. The title is built from the same
    three-source precedence the listing uses (`_title`), off the same cached
    transcript head, so the two cannot name one task differently; the session
    and the target are `_place`'s own answers, which is what the row itself
    carries. `_NO_AHEAD` for a task that is not collected, which is the honest
    answer and what every caller prints as nothing at all."""
    task = tasks.get(key)
    if task is None:
        return _NO_AHEAD
    try:
        _place(task)
        record = _scan(task["path"]) if task["path"] else None
        title, _source = _title(task, record, task["first_prompt"])
        return {"title": title,
                # A `pending:<entry>` row has no session and therefore no chat
                # to open: "" here is the same absence `taskHref` refuses on.
                "session": str(task["session_id"] or ""),
                "target": canonical_fs_path(task["target"] or task["project"])}
    except (OSError, ValueError, KeyError, TypeError):
        return _NO_AHEAD


def _ahead_name(tasks: dict[str, dict], key: str) -> dict:
    """`{"ahead", "ahead_title", "ahead_session", "ahead_target"}` for the task
    holding a folder — the four fields every queue answer carries, resolved in
    one place so the three endpoints and the row cannot disagree about who is in
    front or where the click goes."""
    if not key:
        return {"ahead": "", "ahead_title": "",
                "ahead_session": "", "ahead_target": ""}
    queue = {"": {"ahead_key": key, "ahead": "", "ahead_title": "",
                  "ahead_session": "", "ahead_target": ""}}
    _name_ahead(queue, {}, tasks)
    answer = queue[""]
    return {field: answer[field] for field in
            ("ahead", "ahead_title", "ahead_session", "ahead_target")}


def queue_ahead_of(key: str, tasks: dict[str, dict] | None = None) -> dict:
    """`{"ahead", "ahead_title", "ahead_session", "ahead_target"}` for one task
    key, collecting for itself.

    The seam the schedule router reads: `POST /api/schedule/run-now` can be
    answered "queued", and the sentence it owes the user names the task in front
    — which is a Tasks-page fact (the numbering, the title precedence) and lives
    here, not beside the scheduler's store.

    `tasks` is that collection where the caller already holds one. Run-now needs
    both halves of the queue answer — who is ahead and where this row stands —
    and collecting once for each meant two globs over every transcript on the
    machine for one reply (round-2 review, 2026-09-12)."""
    return _ahead_name(_collect() if tasks is None else tasks, key)


def _status(messages: list[dict], filed: bool, session_id: str, live: bool,
            busy: set[str], parked: bool = False, queued: bool = False) -> str:
    """The status a task sits in — ONE decision, made here, for every view.

    Derived from the MESSAGES, in this order, and the order is the whole model:

    0. **Running AND parked ⇒ Needs attention.** Above every other rule,
       including the queued one just below, because it is the same fact told
       at a finer grain: the run IS in flight, and it is in flight in the one
       way that never ends on its own — a permission card or a question card
       nobody has answered (`parked`, from `_parked_runs`). "In Progress" is a
       true sentence about it and a useless one: it is the sentence a reader
       waits out, and this run will still be there tomorrow. The moment the
       card is answered the run is ordinary again and rule 2 (running) has it
       back, with nothing to undo — this reads the decision files on every
       poll rather than remembering a verdict.

       PARKED WITHOUT RUNNING IS NOT THIS. `parked` is only ever true for a
       process that is alive (see `_parked_runs`), and the `and` below is
       belt-and-braces for a task whose messages say nothing is going on: a
       needs-attention row nobody can answer any more would be a lane that only
       fills up.

       PARKED IS CHECKED ON ITS OWN, not only as a multiplier on rule 2: a
       hand-typed chat parked on a card fails both of rule 2's tests (the live
       registry reports `waiting`, not running, and the transcript has gone
       quiet), so gating on `_running_now`/`_message_running` first would file
       it `done` before parked ever got a look. `_parked_runs` already proves
       the process is alive — that is enough on its own.

    1. **Standing in the manager's line ⇒ Queued, ABOVE running.** One task in
       progress per folder (`project_queue`): this task has work that is DUE
       and cannot start because another task is holding the working tree it
       edits, or it has a card decision held for delivery when that tree
       frees. The caller decides it (`_queue_lines` → `queue_manager.positions`,
       one derivation per listing, the way `busy` and `parked` are) and hands
       the answer in, because it is a fact about every OTHER task on the
       machine and a function that sees one task's messages cannot reach it.

       ABOVE RULE 2 (running), moved there in the review round of 2026-09-18:
       an answered-blocked task the manager has just promoted to line[0] can
       still have a session that READS live — the registry row has not caught
       up, or the sender's mark has not expired — which is precisely what rule
       2 below is built to trust. The manager, not a stale transcript or
       registry read, is the source of truth for who is running a folder, and
       standing in its line is proof this task is NOT that folder's owner: the
       owner is never a member of `positions()` (`queue_manager.positions`
       walks each folder's `line`, never its `owner`), so `queued` can never
       be true for the task actually holding the folder — nothing further
       needs to check that here.

       ABOVE ARCHIVED too (an unlabelled corollary of the same move): the one
       shape that could read both filed and queued is a filing the thread has
       already overtaken, and by the time this is asked `filed` is already
       False for exactly that case (see `_revived`) — so the two checks do
       not actually compete and the order between them is inert. Above the
       speaker, because the speaker is the last thing that HAPPENED and this
       is the thing that is about to: a task that ran yesterday and has a
       message due into a busy folder today is waiting, not done.

       ONLY WITH THE FLAG ON. `_queue_lines` answers empty when
       `project_queue.enabled()` is false, so nothing here derives `queued` and
       the status is the one main computes.
    2. **Anything else running ⇒ In Progress.** Activity beats recency: a task whose
       newest message is next Tuesday's occurrence, with a run still going in
       it, is a task that is working. Three things say a run is happening and a
       task needs only one — a message of its own that is in flight
       (`_message_running`), a transcript that is live (or a sender's own mark
       that a turn just started, `tasks_watch.mark_running`, which `_live`
       folds in), and `schedule.busy_sessions`, the scheduler's record of a
       send it has not heard back from (see `_running_now`). They are
       independent because each is wrong on its own in a different direction:
       a turn thinking through a long tool call appends nothing for minutes and
       reads as not-live, and a session a human is typing into has no busy
       entry at all. The transcript's vote is withdrawn by the caller for exactly one
       case — the tail's freshness is the finished run's own closing records —
       see `_verdict_outvotes_live`.
    3. **Archived is a filing state.** The task the user put away is archived,
       and so is a task whose every message ended up filed (cancelling the last
       live message in a thread archives the task, without anybody having to say
       so twice). Rule 2 is above this on purpose: a run in flight when the task
       was filed keeps running and the card reads In Progress until it stops.
    4. **Otherwise the newest message that has something to say speaks** —
       `blocked` for a run that broke, `done` for one that ended. See `_speaker`.
    5. **Nothing said yet, but something COMING ⇒ Upcoming**, and that second
       half is the whole of it: the lane is what has not happened *yet*, so it
       needs a message still waiting to happen. A task with nothing coming and
       nothing to report is over, and `done` is where a spent session goes — see
       `_waiting`.

    What is NOT here any more is triage's other two words. A person cannot file
    a task as In Progress (the lane is Claude's output, and the Board no longer
    offers the drop) and cannot file one as Done (a run says that, not a
    reader) — so the stale-pin machinery that used to decide when an automatic
    `in_progress` had outlived its run is gone with the pin it guarded.

    FILING SOMETHING DOES NOT STOP IT (Akshil, 2026-08-18), which is rule 2
    standing above rule 3 and nothing more: Archive is a timeless decision and
    the record is never touched here, but while a turn is genuinely in flight a
    row that says `archived` is a lie the reader can watch. The moment the run
    ends the task drops back into Archive on the next poll.

    `filed` is the ANSWER, not the record: the caller has already asked whether
    the filing still stands (`_archive_record` and `_revived`), because a filing
    a new message has overtaken is dropped from disk rather than argued with on
    every poll. This function reads no triage of its own.
    """
    if parked:
        return "needs_attention"
    if queued:
        return "queued"
    if messages and (_running_now(session_id, live, busy)
                     or any(_message_running(m) for m in messages)):
        return "in_progress"
    if filed:
        return "archived"
    if messages and all(_message_archived(m, filed) for m in messages):
        return "archived"
    speaker = _speaker(messages, filed)
    if speaker is not None:
        return _message_verdict(speaker) or "done"
    return "upcoming" if _waiting(messages, filed) else "done"


def _failed(speaker: dict | None) -> bool:
    """Did the run this task is reading off break?

    Kept as its own field on the row as well as feeding `status` above, because
    the two can disagree in exactly one direction and the difference is worth
    keeping: a task that is archived, or live again, reads `status` as something
    other than `blocked` while this stays true. Anything that only wants "which
    column" should read `status`.

    It is also the other half of what the Blocked lane holds. The lane took the
    wider word on 2026-09-03 and now carries two different things — a run that
    broke and a run parked on a card — so the row has to say which; this flag
    and `blocked_reason` are how (see `_row`).

    THE THREE SPELLINGS OF A BROKEN RUN, and all three have to be here or the
    row contradicts itself (whole-stack review, PR1). `_message_verdict` reads
    `state == "error"` and, for a delivered message, `turn in ("unknown",
    "error")` as `blocked`; this used to read only the first two. So a chat turn
    whose last reply out of the transcript was an API failure (`_reply_fate`
    writes `turn: "error"`) filed under `blocked` with `failed: False` on the
    row, while `_row`'s `blocked_reason` — which falls back on the status — said
    `"failed"` in the same breath. One row, two answers: the ring stayed un-red
    and the caption said the run broke."""
    return speaker is not None and (
        speaker["state"] == "error" or speaker["turn"] in ("unknown", "error"))


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


def _entry_session(entry: dict, by_id: dict | None = None) -> str:
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

    **A FOLLOWER READS ITS LEADER'S ANSWER.** `follow_of` names the message
    this one was typed behind, and it exists for the one shape that has no id of
    its own to give: a second message typed into a brand-new chat whose first
    message is still queued. Grouping it anywhere but with the leader forks a
    second `pending:<id>` row beside the very chat it was typed into, which is
    the bug this field was added for (browser QA, 2026-09-12). So the leader's
    own answer is this entry's answer — and "" here is not a failure, it means
    "the leader has not run either", which `_entry_key` finishes by filing both
    under the LEADER's pending key.

    `by_id` is the store keyed by entry id; a caller mid-pass over the entries
    passes its own map, and one holding a single entry passes nothing and has
    the store read for it — but only for an entry that actually follows one.
    """
    session = (str(entry.get("claude_session_id") or "")
               or str(entry.get("session_id") or "")
               or _run_session(entry))
    if session or not str(entry.get("follow_of") or ""):
        return session
    leader = schedule.leader_of(
        entry, _by_entry_id() if by_id is None else by_id)
    if leader is None:
        return ""
    # The leader's run names the session before the scheduler stamps it, for
    # the follower exactly as for the leader (Bugbot): otherwise the leader
    # joined the live session while its follow-ups stayed on `pending:` — one
    # chat split in two rows, and a number burnt.
    return (str(leader.get("claude_session_id") or "")
            or str(leader.get("session_id") or "")
            or _run_session(leader))


def _run_session(entry: dict) -> str:
    """The session a SENT entry's run has opened, read off the run itself, for
    the seconds before the scheduler writes it onto the entry.

    THE NUMBER MUST NOT CHANGE WHEN A QUEUED CHAT STARTS (browser QA,
    2026-09-16: TASK-056 waiting became TASK-057 running). A brand-new chat's
    transcript is on disk within a second of the spawn; the scheduler learns
    the session from its own poll of the run two seconds later
    (`claude_spawn.record_session_when_ready`) and only then stamps
    `claude_session_id`. A listing in that window saw a session with no entry
    and a pending row with no session, numbered the session afresh, and the
    rekey that keeps the pending row's number found the session already
    numbered. The run dir knows sooner — its `session` file, or the live
    registry by pid (`project_queue.run_sessions`) — so the entry is grouped
    onto its session from the first listing after the CLI comes up.

    Only an entry with a run and no session is asked, so the cost is a run-dir
    read per in-flight spawn, never per entry. Best-effort: nothing readable is
    "", which is the answer the entry gave before."""
    run_id = str(entry.get("run_id") or "")
    if not run_id or entry.get("state") not in (schedule.SENDING, schedule.SENT):
        return ""
    # Under the flag only: with the queue off the listing groups exactly as
    # main does — the scheduler's stamp, else `pending:<id>` — and the ring
    # keys `schedule._entry_keys` computes stay the row's own.
    if not project_queue.enabled():
        return ""
    try:
        agent = _agent_module()
        if agent is None:
            return ""
        run_dir = os.path.join(str(agent.RUNS), run_id)
        if not os.path.isdir(run_dir):
            return ""
        found = project_queue.run_sessions(agent, run_dir, {})
    except Exception:  # noqa: BLE001 — a run dir we cannot read names nobody
        return ""
    return next(iter(sorted(found)), "") if found else ""


def _by_entry_id(entries: list[dict] | None = None) -> dict:
    """The scheduler's store keyed by entry id — what `leader_of` walks. An
    INDEX and never the list itself: a caller with entries to visit visits its
    own list, so an entry with no id at all costs a lookup and not a row."""
    if entries is None:
        entries = schedule.list_entries()
    return {str(entry.get("id") or ""): entry for entry in entries}


def _entry_key(entry: dict, by_id: dict | None = None) -> str:
    """The TASK key one scheduled entry is filed under: the session
    `_entry_session` resolved, else `pending:<entry-id>` — the LEADER's id for a
    message typed behind another, its own for everything else.

    `_collect`'s filing rule in one place, because four callers need it and a
    fifth spelling of it would be a row nobody could ring."""
    if by_id is None and str(entry.get("follow_of") or ""):
        by_id = _by_entry_id()
    session = _entry_session(entry, by_id)
    if session:
        return session
    leader = schedule.leader_of(entry, by_id)
    owner = entry if leader is None else leader
    return tasks_store.pending_key(str(owner.get("id") or ""))


# States that mean a scheduled message will never run and never did. In
# `_entry_state`'s vocabulary, so a cancelled or missed OCCURRENCE — which reads
# as `skipped` — is covered by the same tuple, and `error` is NOT: a send that
# broke is news, and `_message_verdict` reports it as `failed`.
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
    # A SEND IS A TASK BEFORE ANY FILE SAYS SO. `tasks_watch.sent_marks()` is
    # the page that made the send telling this process it made it (see
    # `/api/tasks/running`), which is earlier than the transcript, earlier than
    # the CLI's registry row, and — for a brand-new chat — earlier than there
    # being anything on disk at all. A mark on a session nothing else lists
    # becomes a PLACEHOLDER row here, keyed by THE SESSION ID, so the row the
    # transcript makes a moment later is literally this row: same key, same
    # number, no swap for the reader to watch.
    #
    # ITS LIFETIME IS THE MARK'S, and that is the whole of the rule. While the
    # mark stands the row stands; when the mark runs out
    # (`tasks_watch.MARK_TTL_SEC`, or the sender saying the turn ended) the row
    # is whatever the disk says. With a transcript that is a real row and this
    # was only its first fifteen seconds. WITHOUT one — a send whose run died on
    # the spot, a bad model id, a refused spawn — the row DISAPPEARS, because
    # nothing happened: there is no session, no transcript, no entry, nothing to
    # open and nothing to report. Everything a reload can reproduce comes off
    # disk; this is the one thing that cannot, and it is why it has a fuse.
    marks = tasks_watch.sent_marks()
    for session_id, mark in marks.items():
        # Only a send WITH WORDS is a row on its own: a wordless mark (an
        # attachment-only send, a bare liveness ping) has nothing to show and
        # would be a blank card for fifteen seconds (regression review). A
        # session that already has a row keeps its liveness floor either way.
        if session_id not in tasks and str(mark.get("text") or "").strip():
            tasks[session_id] = _new_task(session_id, session_id, None)
    # Indexed once for the whole pass: a follower is filed under the entry it
    # was typed behind, and `leader_of` walks this map rather than the store.
    entries = schedule.list_entries()
    by_id = _by_entry_id(entries)
    for entry in entries:
        if entry.get("state") == schedule.RECURRING:
            # A template never fires and is not a message; its materialised
            # occurrences are, and they are ordinary entries in this list.
            continue
        session_id = _entry_session(entry, by_id)
        if session_id:
            task = tasks.get(session_id)
            if task is None:
                task = tasks[session_id] = _new_task(session_id, session_id, None)
            task["entries"].append(entry)
        else:
            key = _entry_key(entry, by_id)
            task = tasks.setdefault(key, _new_task(key, "", None))
            task["entries"].append(entry)
    for task in tasks.values():
        task["entries"].sort(key=_entry_at)
        # The live send this task is carrying, or None — read once, here, and
        # spent by `_place` (which folder the placeholder belongs to) and `_row`
        # (the words, and the message that shows them). On the task rather than
        # threaded through as a parameter because a task's own send is a fact
        # about the task, and the two readers must not be able to disagree about
        # whether one is standing.
        task["sent"] = marks.get(task["session_id"]) if task["session_id"] else None
    # The drop happens HERE, once, rather than in each endpoint: the listing,
    # the full thread, the calendar window and the read endpoint all collect
    # through this function, so a task that is no longer a task — or one the
    # user deleted — is absent from every one of them and no view has to know
    # why.
    deleted = tasks_store.deleted_state()
    return {key: task for key, task in tasks.items()
            if _is_task(task) and not _deleted(task, deleted)}


def _entries_for(keys) -> dict[str, list[dict]]:
    """`task key -> its scheduled entries`, for the named keys only.

    `_collect`'s entry pass and nothing else: the same filing rule (`_entry_key`
    — the session an entry resolves to, else the leader's pending key, else its
    own) and the same
    exclusion of recurring TEMPLATES, which never fire and are not messages. What
    it does NOT do is the expensive half — the glob over every transcript on the
    machine, the deleted store, the `_is_task` decision — because a caller that
    already holds a set of keys it has just listed has had all three answered.

    For a caller that has not (a key the listing dropped), this answers with an
    empty list rather than a verdict; it is a narrowing of a collection, never a
    second way to decide what a task is."""
    out: dict[str, list[dict]] = {key: [] for key in keys}
    if not out:
        return out
    entries = schedule.list_entries()
    by_id = _by_entry_id(entries)
    for entry in entries:
        if entry.get("state") == schedule.RECURRING:
            continue
        key = _entry_key(entry, by_id)
        if key in out:
            out[key].append(entry)
    return out


def _rekeyed_pendings(keys) -> dict[str, str]:
    """Rung `pending:<entry-id>` key -> the key the LISTING files that entry
    under, for the keys where the two have drifted apart.

    THE RING AND THE LISTING KEY THE SAME ENTRY OFF DIFFERENT FACTS (Akshil, QA
    2026-09-16: "Recent chats loses tasks when the queue moves forward"). The
    watcher names an entry with `schedule._task_key` — the scheduler's stamp,
    else `pending:<id>` — while the listing has a third source, the run dir the
    entry already spawned (`_entry_session` -> `_run_session`). In the seconds
    between a promoted leader's CLI opening its session and
    `record_session_when_ready` stamping `claude_session_id`, the ring says
    `pending:<id>` and the listing says `<session>`: no row is built for the
    rung key, so it was answered `gone` WITH NOTHING BEHIND IT and the client
    deleted a live row whose replacement had never been rung — it arrived only
    on the 20 s floor read, and a batch promotion rings several keys at once
    ("blank, then only a few tasks").

    So the rung key is translated through the listing's own rule and the row it
    now names is added to the answer. The pending key still leaves in `gone`:
    the client merges the two halves of one payload atomically
    (`mergeTaskChanges`), so gone-plus-row is a clean SWAP — one task, one row,
    under the name the listing gives it — where gone-alone was the data loss.

    Flag-off parity is free: with the queue off `_run_session` answers "" and
    `_entry_key` is `schedule._task_key` spelled again, so nothing here
    translates and the pending key stays the pending key.
    """
    pendings = {key: tasks_store.pending_entry(key) for key in keys}
    pendings = {key: eid for key, eid in pendings.items() if eid}
    if not pendings:
        return {}
    by_id = _by_entry_id()
    out: dict[str, str] = {}
    for key, entry_id in pendings.items():
        entry = by_id.get(entry_id)
        if entry is None:
            continue
        listing_key = _entry_key(entry, by_id)
        if listing_key and listing_key != key:
            out[key] = listing_key
    return out


def _deleted(task: dict, deleted: dict) -> bool:
    """Has the user deleted this task — and has nothing happened since?

    The tombstone (`tasks_store.deleted_at`) carries its WHEN precisely so this
    can be a comparison and not a verdict: the row stays gone only while the
    tombstone is the newest thing about the task. Activity that postdates it
    brings the row back, because the alternative is the one thing a hidden task
    must never do — run invisibly. Two kinds of activity can postdate it:

    * **an entry created after the deletion** — a message scheduled into the
      same conversation later. `created` is the entry's own birth stamp; its
      `due` is deliberately not read, because delete cancels pending entries
      and a cancelled entry's future due time is not news, it is a corpse with
      a date on it;
    * **the transcript grew** — the user (or a run that was already in flight
      when the delete landed, which the endpoint refuses but a race can slip)
      said something in the session. Append-only files move their mtime for
      exactly one reason. An unreadable mtime counts as no evidence, not as
      revival: degrading widens nothing here because the tombstone still
      answers, and a task wrongly hidden is recoverable while a delete that
      silently failed is not — the endpoint's answer is the receipt.

    This is `_revived` restated for a stronger filing — same promise, same
    shape, different record.
    """
    at = tasks_store.deleted_at(deleted, task["key"])
    if at <= 0:
        return False
    for entry in task["entries"]:
        created = tasks_store.epoch(entry.get("created"))
        if created is not None and created > at:
            return False
    path = task["path"]
    if path:
        try:
            if os.path.getmtime(path) > at:
                return False
        except OSError:
            pass
    return True


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
    scheduled message was pointed at.

    The target, for a chat with no scheduled entry, prefers the FILE the chat's
    pane was on (`tasks_store.head`'s fourth answer, read out of the
    `<live-app-state>` block) over the project folder: opening the task should
    land where the user was actually looking. A scheduled entry's own target
    still wins — it is the thing the job was pointed at — and a pane file that
    no longer exists on disk falls back to the folder rather than opening a
    view of nothing."""
    cwd = None
    first_ts = None
    prompt = ""
    pane = ""
    if task["path"]:
        cwd, first_ts, prompt, pane = tasks_store.head(task["path"])
    entries = task["entries"]
    target = str(entries[-1].get("target") or "") if entries else ""
    # A GUESSED project mints no number (`_numbers`). The directory-name
    # decode below is lossy — every hyphen reads as a separator, so
    # `-private-tmp-pqueue-qa-alpha` comes back as `/private/tmp/pqueue/qa/alpha`
    # — and a number allocated against that string lives in a counter of its
    # own; the folder's real counter then hands the same number out again the
    # moment the transcript says where it is (two TASK-002s in one folder,
    # browser QA 2026-09-16). The row is still FILED under the guess for this
    # one listing, so it has a lane and a folder chip; only the allocation
    # waits for the first row that carries a cwd, which is seconds away.
    task["project_guessed"] = False
    # A PLACEHOLDER HAS ONLY THE SEND TO GO ON — no transcript to read a `cwd`
    # out of, no entry to take a target from — so the mark supplies both: the
    # file the message was sent about is the target, and its folder (`_workdir`,
    # the same rule the run itself applies) is the project. Gated on having
    # neither of the other two sources, so a session that HAS a transcript keeps
    # reading its own `cwd` and its own pane file; the mark is a floor under a
    # row with nothing, never an override of something.
    sent = task.get("sent") or {}
    if not task["path"] and not entries and sent.get("file"):
        target = str(sent["file"])
    if not prompt:
        # …and the same for the title. `_title`'s `message` source is "the line
        # the session opened with", which for a send this app has not seen land
        # yet is exactly the words that were sent — a placeholder row named
        # after the message, in the same source the transcript will name it in a
        # moment, rather than a nameless row that acquires a title on the next
        # poll.
        prompt = str(sent.get("text") or "")
    task["first_prompt"] = prompt
    if not cwd:
        cwd = _workdir(target)
        if not cwd and task["path"]:
            cwd = sessions._decode_project_dir(
                os.path.basename(os.path.dirname(task["path"])))
            task["project_guessed"] = bool(cwd)
    task["project"] = tasks_store.project_of(cwd or "")
    task["target"] = target or (
        pane if pane and os.path.isfile(pane) else task["project"])
    if first_ts is None and entries:
        first_ts = (tasks_store.epoch(entries[0].get("created"))
                    or _entry_at(entries[0]))
    if first_ts is None and sent.get("at"):
        # A placeholder's only clock. It is fixed for the row's whole life in
        # exactly the way this value has to be: the transcript that lands a
        # moment later has a FIRST record older than nothing, so the minimum
        # below still settles on the file's own answer and the number this
        # allocates is the number that row keeps (same key — see `_collect`).
        first_ts = float(sent["at"])
    task["order"] = first_ts
    # WHEN THE TASK BEGAN — the row's `started`, and deliberately NOT `order`
    # above, which this must not disturb: `order` decides the sequence new task
    # NUMBERS are handed out in (`_numbers` → tasks_store.ensure_ids), a
    # once-per-key decision that has already been made for every task that has a
    # number, so changing what it means would renumber nothing and confuse
    # everything.
    #
    # WHY THE TWO CANNOT BE THE SAME VALUE (bugbot, PR #984). `order` SWITCHES
    # SOURCE the moment a transcript exists: before that it is the entry's
    # `created`, and after it is the transcript's first record. For allocation
    # that is harmless — a number is allocated once and then kept — but a row's
    # `started` is a listed fact, and a fact that changes under a listed row is
    # the kind of thing a view sorting by it would move on. (The Cards wall did
    # sort by it for a while; it now shares the List's order — the time each
    # row prints, shell/tasks-lib.sortByLane — and nothing on the client orders
    # by `started` today. The field stays fixed regardless, for whoever next
    # reads it.) Two tasks created seconds apart could swap places when the
    # second's transcript landed; a run scheduled days before it fires would
    # jump the moment it spoke.
    #
    # So: THE EARLIEST CLOCK WE HAVE. `created` is written when the message is
    # scheduled and always precedes the first thing the run says, so once a task
    # has one the minimum is fixed for good and the transcript's arrival cannot
    # move it. A hand-typed session — no entry, no `created` — takes its
    # transcript's first record, which is equally fixed: a transcript's FIRST
    # line is the one line in it that never changes.
    #
    # `first_ts` is read after the fallback above, so an entry with no `created`
    # stamp at all contributes its due time here rather than nothing. That is the
    # one shape whose `started` can still move (due time, then the transcript's
    # first record if it is earlier) — and the scheduler writes `created` on
    # every entry it makes, so it is a shape this server does not produce.
    created = (tasks_store.epoch(entries[0].get("created")) or 0.0) if entries else 0.0
    began = [stamp for stamp in (created, first_ts) if stamp]
    task["started"] = min(began) if began else 0.0


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
    # A task whose project is only a GUESS (`_place`) is left out of the
    # ALLOCATION — a number it does not have yet is minted, from the right
    # counter, by the first listing that reads its cwd. A number it already
    # HAS is still its number (Bugbot, PR #1124): the store is read for it below
    # rather than written, so a session numbered before this rule, or one whose
    # transcript never records a cwd, keeps wearing what it was given.
    items = [(task["key"], task["project"], task["order"])
             for task in tasks.values()
             if not task.get("project_guessed")]
    try:
        numbers = tasks_store.ensure_ids(items, rekeys)
    except OSError:
        # A read-only state dir must not cost the user their task list; the
        # numbers simply stay blank until it is writable again.
        return {}
    for task in tasks.values():
        if not task.get("project_guessed") or task["key"] in numbers:
            continue
        stored = tasks_store.stored_number(store, task["key"])
        if stored:
            numbers[task["key"]] = stored
    return numbers


def _task_number(task_key: str, tasks: dict[str, dict]) -> str:
    """The number for ONE task, allocated now if it has none — the listing's own
    `_numbers`, over a collection the caller already holds.

    **A QUEUED SEND'S ANSWER HAS TO BE ABLE TO NAME ITS OWN TASK** (Akshil,
    2026-09-12). A brand-new chat whose first message queues is a
    `pending:<entry>` task that nothing has listed yet, so `POST
    /api/tasks/queue/admit` used to answer with a position, a holder and no
    number at all: the bubble said "queued · behind TASK-041" about a task it
    could not call anything, and the name only appeared when the next full
    listing came round. Minting HERE is what makes it the same number — not a
    second one — that the row is built with a moment later:
    `tasks_store.ensure_ids` is allocate-once and keyed by the task key, so the
    listing finds the record already there and writes nothing.

    `_place` first, because the allocation reads two facts a freshly collected
    task does not have yet: the project (which counter the number comes out of)
    and the order (the sequence backfilled numbers are handed out in). The same
    two, computed the same way, as the listing — which is the whole reason this
    goes through `_place`/`_numbers` rather than calling `ensure_ids` with a
    project of its own guessing.

    "" for a key the collection does not hold — the entry was cancelled from
    another window between the write and this read — which is how every other
    absent id on this wire reads, and for a state dir that cannot be written
    (`_numbers` swallows that: no numbers is a cost the task list survives)."""
    task = tasks.get(task_key)
    if task is None:
        return ""
    _place(task)
    return _numbers({task_key: task}).get(task_key, "")


# ------------------------------------------------------------------ liveness


def _live(path: str | None, now: float,
          session_id: str = "") -> tuple[bool, float, bool]:
    """(is this session running, when was it last active, did the SENDER say a
    turn had just started here).

    The same 45-second rule as the sessions inbox, and the same tail read — a
    transcript's mtime alone lies, because Claude Code appends housekeeping
    records after the turn is over. Skipped entirely for a file nothing has
    touched in 90 seconds: it is stale either way, so the read would only be
    deciding what kind of stale.

    The third value is EVIDENCE vs TESTIMONY. The first two are inferred from
    files — timestamps a later rule is entitled to re-read and discount
    (`_verdict_outvotes_live`). The mark is the page that made the send saying
    it made it, which no reading of the transcript can outvote, and a caller
    that discounts it has thrown away the one fact this whole path exists to
    carry.

    It is the state of the mark, NOT which branch below won (Bugbot, PR #1163).
    Reporting it only where the mark decided the answer meant it went false the
    instant the registry caught up and said `busy` — so a row could show the
    send, drop back to done two seconds later when the echo rule got its vote
    back, and only return when the prompt reached the transcript. Same lag,
    with a flicker in front of it. The send either happened in the last fifteen
    seconds or it did not, and no other fact makes it un-happen.

    True implies running: `is_marked_running` is the one thing consulted for
    it, and every way a mark is stood down — its own TTL, a registry that went
    `busy` then wasn't, or the sender itself saying the turn ended
    (`mark_idle`, `POST /api/tasks/idle`) — lives there, not here. That last
    one is what keeps a FAST turn from wearing the ring for the rest of the
    mark's fifteen seconds: the reply landing is news the same page can say
    the instant it knows it, same as the send was.

    `session_id` is for the ONE task shape that has no transcript to name
    itself with: a placeholder built out of a live send alone (`_collect`).
    There is no file to read, so the mark is not merely the best evidence
    here — it is all of it. Optional, because every other caller has a path
    and the path already carries the id (the transcript is named for its
    session), and reading it twice could only produce two answers."""
    if not path:
        if tasks_watch.is_marked_running(session_id):
            # A send in flight into a session nothing has written yet. `now` is
            # the honest last-active — the turn is happening as this is read —
            # exactly as it is on the marked branch below.
            return True, now, True
        return False, 0.0, False
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False, 0.0, False
    session_id = os.path.splitext(os.path.basename(path))[0]
    marked = tasks_watch.is_marked_running(session_id)
    # The live registry (tasks_watch) knows what a running `claude` SAYS it is
    # doing, which beats inferring it from the file: `busy` is running whatever
    # the tail's timestamps add up to, and `idle` is not, even if housekeeping
    # touched the file a second ago. Its last-active stamp is used only when it
    # is newer than the transcript's — a registry row is rewritten on status
    # changes, not on every message, so the file can know the later moment.
    from_registry = tasks_watch.live_from_registry(session_id, mtime)
    # …EXCEPT IN THE FIRST SECONDS OF A TURN THIS APP SENT (tasks_watch
    # `mark_running`). A chat here runs `claude -p`, whose registry row lands two
    # to four seconds after the process starts — so both "no row at all" and "the
    # previous turn's idle row" read as done, and every turn sent from this app
    # wore a done ring for its first seconds; a short turn for the whole of it
    # (Akshil, 2026-09-15).
    #
    # While the send's mark is alive, ONLY `busy` outranks it: a registry saying
    # the turn is running is the same answer from a better source, and every
    # other answer is a file that has not caught up. `now` is the honest
    # last-active — the turn is happening as this is read — and the mark expires
    # on its own, so a run that died on the spot settles without a write.
    if marked and not (from_registry and from_registry[0]):
        return True, now, True
    if from_registry is not None:
        running, active = from_registry
        if now - mtime <= sessions._STALE_TAIL_SEC:
            _activity, last = sessions._tail(path, mtime)
            file_active = last.timestamp() if last is not None else mtime
        else:
            file_active = mtime
        return running, max(active, file_active), marked
    if now - mtime > sessions._STALE_TAIL_SEC:
        return False, mtime, marked
    activity, last = sessions._tail(path, mtime)
    running = (now - activity) < sessions._RUNNING_WINDOW_SEC
    return running, (last.timestamp() if last is not None else mtime), marked


# --------------------------------------------------------------- the endpoints


def _next_run(entries: list[dict]) -> tuple[float, str, bool]:
    """When this task NEXT runs, WHICH entry that run is, and whether it REPEATS
    — `(0.0, "", False)` for a task with nothing pending.

    The third is the row's repeat glyph (Akshil, 2026-09-11: "for repeating
    tasks we say 'in 1h [repeat icon]'"): an occurrence carries its template's
    id, and that is the whole test. Decided here, over the same entry the time
    and the id name, so the glyph cannot describe a different run than the one
    the chip is timing — and here rather than on the client because the window
    may not hold the run at all (the paragraph below).

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
    best_repeats = False
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
            best_repeats = bool(entry.get("template_id"))
    return best_at, best_id, best_repeats


# THE TITLE PR #1107's COMEBACK FILES ITSELF UNDER — `protocol/quota.ts`
# CONTINUE_TITLE, restated here because it is a wire fact between the chat that
# schedules the continuation and the row that times it. The chat is the only
# thing that writes it, and it writes it on that entry alone.
COMEBACK_TITLE = "Continue after usage limit"


def _comeback_at(entries: list[dict]) -> float:
    """WHEN THE USAGE-LIMIT CONTINUATION RUNS — the soonest pending entry of this
    task that is the comeback the chat scheduled, and 0.0 when there is none.

    NOT `_next_run` (🟡 review, 2026-09-12). `resumes_at` is read as "this
    stopped run picks itself back up then", and the next run of ANY kind is a
    different fact: a task that hit the limit at noon and also has a nightly
    repeat due at 6pm would have told the reader the window reopens at 6pm — a
    sentence about the plan's clock built out of somebody's calendar.

    The title is the whole test, because it is the only mark the comeback
    carries: `scheduleComeback` posts one entry, on this session, with that
    title and a fixed prompt. 0.0 when the comeback was cancelled or its POST
    failed, which is how every other absent time on the row reads.
    """
    best = 0.0
    for entry in entries:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        if str(entry.get("title") or "").strip() != COMEBACK_TITLE:
            continue
        at = _entry_at(entry)
        if not at:
            continue
        if not best or at < best:
            best = at
    return best


def _row(task: dict, number: str, triage: dict, read: dict, now: float,
         busy: set[str], revived: list[str], parked: dict | None = None,
         queue: dict | None = None, queue_on: bool | None = None,
         chat_drafts: dict | None = None,
         bound_chips: dict | None = None,
         last_message: bool = False) -> dict:
    """One listing row. The tail parse only: three messages, and a count.

    `parked` is `_parked_runs()` — every session whose live run is waiting on a
    card — computed once by the caller for the same reason `busy` is: it is one
    scan of the runs tree, and asking it per row would re-read every run dir once
    per task on the machine.

    `busy` is `schedule.busy_sessions` over the WHOLE store, computed once by
    the caller — one of the three things that say a run is happening (see
    `_status`). Over the whole store rather than this task's
    own entries because a resume that forked is filed under the session it RAN
    in (`_entry_session` reads the answer first) while it still holds the
    session it NAMED busy, and that one is another row.

    `queue` is `_queue_lines()` — every task waiting on a busy folder, with its
    place in the line — computed once by the caller for the third time on this
    list of parameters and for the same reason: it is one scan of the runs tree
    and one pass over the scheduler's store, and it answers about tasks other
    than this one. None means nobody asked (the single-row callers below) or the
    project-queue flag is off, and the row then carries no queue fields at all.

    `queue_on` is the flag, read once by the caller. FLAG OFF IS MAIN, FIELD FOR
    FIELD: `queue_key` is a folder derivation only this feature has a use for,
    and resolving one per row — walking a task's ancestors looking for a `.git`
    — is work main never did. So it is `""` with the flag off, like every other
    queue field on this row, and the folder appears the moment the feature is
    turned on. None means "ask", for the single-row callers that have no listing
    to have read it in.


    `chat_drafts` is `drafts.list_chat()`, read once by the caller for the same
    reason `read` and the numbers are: it is one file, and asking it per row
    would open it once per task on the machine. Joined on the SESSION ID rather
    than the key, because that is what the composer keys on — a `pending:` row
    has no conversation to have been typed into, and a `new:<file>` draft has no
    row at all until its first send makes the session (design.md, "Chat draft
    key").

    `bound_chips` is `_bound_chips(task_drafts)` — the OTHER draft that can be
    about this conversation: a New task form somebody opened out of it and has
    not sent. Read once by the caller, like `chat_drafts`, and joined on the
    same session id. It fills two fields and nothing else: `bound_draft`, which
    names the draft row standing in for this one, and — only when the composer
    itself is empty — `draft`, so the row wears the chip either way. A chat
    draft WINS, because that one is literally sitting in this conversation's
    composer while the form is a message about to be scheduled into it.

    `last_message` is `shell_prefs.task_card_last_message()`, read once per
    request by the caller for the same reason the joins above are: it is one
    file, and asking it per row would open it once per task on the machine.
    False — the default, and what the three single-row callers take, none of
    which reads the field — leaves the key OFF the row entirely rather than
    sending a null: the only surface for it is the Cards wall's experimental
    title, and the client already reads an absent key as "nothing said".

    `revived` is an OUT parameter and the only one: a session whose archive
    record this row has just found stale is appended to it, and the caller does
    the write. Collected rather than written here because building a row is
    inside a per-task `try` that swallows IO errors — a failed write would cost
    the row instead of costing the filing."""
    rec = _scan(task["path"]) if task["path"] else None
    live, active, marked = _live(task["path"], now, task["session_id"])
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
    merged = _merge(prompts, task["entries"])
    # …AND THE SEND THAT HAS NOT REACHED DISK YET, as the newest message in it
    # (`_fold_sent_mark`, which explains when it is already there and drops
    # itself). It is added to the WHOLE merged thread, before the tail is cut,
    # for the same reason `live` is decided there: `_status`, `_speaker` and the
    # row's own `failed` flag all read the whole list, and a send visible to the
    # preview but not to the status would be a row that shows the words and says
    # the task is done.
    #
    # `total` counts it, so the message ids below still number from the end —
    # and the id it takes is the id the transcript's own prompt takes when it
    # lands, because by then it IS that message and the count has not moved.
    if task.get("sent") and _fold_sent_mark(merged, task["sent"]):
        total += 1
    # A run that has already reported its verdict does not keep the row In
    # Progress off its own closing transcript records: the queue card in the
    # corner says finished within seconds of the result row, and this page
    # saying In Progress for the rest of the 45-second liveness window was the
    # two surfaces disagreeing about one run. Decided off the WHOLE merged
    # thread, before the tail is cut, and spent by everything below that reads
    # `live` — the status, the newest chat message's turn, and the row's own
    # `live` flag — so the row cannot half-agree with itself. See
    # `_verdict_outvotes_live` for why a genuinely new turn is safe.
    # `active` rides along because it is the tie-breaker the bug demanded:
    # a transcript still being written to meaningfully after the verdict is a
    # session that kept working, and only the verdict's own echo is set aside.
    #
    # …AND A SEND IS NOT AN ECHO (`marked`, Bugbot PR #1163). This rule discounts
    # TIMESTAMPS: it exists because a finished run's closing records look like a
    # pulse, and the only thing it is entitled to set aside is the transcript's
    # own vote. The mark is not that vote — it is the page that sent the message
    # saying it sent it, a fact no reading of the file can outrank. Both windows
    # are 15 seconds, so without this a follow-up typed into a task whose
    # scheduled run had just reported would have been suppressed for the mark's
    # entire life, and the row would have sat on `done` until the registry row
    # landed — which is the exact lag the mark exists to close.
    #
    # `marked` is the mark's STATE, not the branch `_live` took, so this holds
    # across the registry catching up mid-send as well — see its docstring.
    if live and not marked and _verdict_outvotes_live(merged, active):
        live = False
    # …and a turn the usage limit ended is over too, however fresh the failure
    # row it ended on. See `_limit_outvotes_live`: without this the row read In
    # Progress and wore the failed ring at the same time, and the board put a
    # task that cannot move until the window resets into the lane for work that
    # is happening.
    if live and _limit_outvotes_live(merged):
        live = False
    # BEFORE the cut, from the whole set: the one fact about the future that the
    # three-message window cannot be trusted to hold. See `_next_run`.
    next_run, next_run_entry, next_run_repeats = _next_run(task["entries"])
    tail = merged[-_LISTING_MESSAGES:]
    # The tail's dicts ARE the merged list's dicts (a slice shares them), so the
    # liveness this writes onto the newest chat message is visible to the status
    # derivation below, which reads the whole thread.
    _turn_of_newest_chat(tail, live)
    for offset, message in enumerate(reversed(tail)):
        message["message_id"] = tasks_store.format_message_id(total - offset)
    _mark_unread(tail, task["key"], read)

    newest = tail[-1] if tail else None
    # DOES THE FILING STILL STAND? Asked once, here, and spent by both the
    # status and the speaker below so they cannot read the task as archived and
    # not-archived in the same row. A record the thread has overtaken is not
    # merely ignored — its session id goes into `revived`, and the caller drops
    # it from disk. See `_revived`.
    record = _archive_record(task["session_id"], triage)
    filed = record is not None
    if record is not None and _revived(merged, _filed_at(record)):
        filed = False
        revived.append(task["session_id"])
    # Which message the status is reading off — asked once here so the row's
    # `failed` flag and its `status` cannot be reading two different runs.
    speaker = _speaker(merged, filed)
    # TWO times, because "recent" is two questions here and one number could
    # only answer them by lying to one of them.
    #
    # `active` is the last thing that actually HAPPENED in this session, and
    # nothing that has not happened may enter it. `ran_at` is when a message ran
    # (a caught-up run is news today, whatever day it was due) and 0.0 until it
    # does; `at` is the due time, which never moves and can be in the FUTURE
    # (see `_entry_at`).
    if newest is not None:
        active = max(active, newest["ran_at"] or 0.0)
    # The row's `happened_at`: `active` as it stands HERE — the newest thing
    # that actually ran or was written, 0.0 when nothing has — before the two
    # fallbacks below let a due time or a creation stamp stand in for it. The
    # desk (current_apps.observe) reads this and only this to decide whether a
    # task finished under an app since the app was last opened: a message due
    # tomorrow has not happened, and a task merely asked for has not either.
    # `last_active` cannot serve — it keeps the due time so the List sorts a
    # future message near the top — and two attempts to lean on it anyway (a
    # wall-clock stamp, then a max over it) each broke on exactly that (Bugbot
    # ×2, 2026-09-07).
    happened = active
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
    # WHAT THIS RUN IS WAITING ON, asked once here so the row's `status`, its
    # `blocked_reason` and its `attention` line cannot describe three different
    # states of the same run. Keyed by the session id: a task with none (a
    # `pending:<entry>` row, whose message has not started a conversation yet)
    # has no run to be parked, which is what the empty key would otherwise
    # accidentally match.
    waiting = (parked or {}).get(task["session_id"]) if task["session_id"] else None
    # The flags this task's runs are launched with — see `_run_settings`. Both
    # "" for a task that never chose, which is most of them.
    model, effort = _run_settings(task)
    # WHERE IN THE LINE, asked once here for the same reason `waiting` is: the
    # status, the position and the name of the task in front have to describe one
    # state of one folder. `{}` for a task nobody said was queued, which is every
    # task with the flag off.
    queued = (queue or {}).get(task["key"]) or {}
    if queue_on is None:
        queue_on = project_queue.enabled()
    # The card's two summary facts, asked once here for the same reason `queued`
    # is: they are about this task's own entries, and the row is where those
    # are. Flag off costs one branch and nothing else.
    waiting_count, blocking = _queue_summary(task, now) if queue_on else (0, False)
    # THE LEADER ENTRY OF A ROW THAT HAS NO CONVERSATION YET, and who asked for
    # it. Both read once here, off this task's own entries, and both "" for a
    # task with a session — that row is a chat already, and has a transcript to
    # be found by. See the two fields below.
    entry_id = tasks_store.pending_entry(task["key"])
    entry_origin = _leader_origin(task, entry_id) if entry_id else ''
    status = _status(merged, filed, task["session_id"], live, busy,
                     parked=waiting is not None, queued=bool(queued))
    failed = _failed(speaker)
    # THE ONE FAILURE THAT IS NOT A FAULT. Read off the message the status is
    # reading off, so the reason and the lane cannot describe different runs.
    limited = speaker is not None and bool(speaker.get("limited"))
    # The New task form bound to this conversation, if there is one — same
    # keying as `waiting` above and for the same reason: a row with no session
    # has nothing a draft could be bound TO, and "" must not match.
    bound = ((bound_chips or {}).get(task["session_id"]) or {}
             if task["session_id"] else {})
    row = {
        "key": task["key"],
        "task_id": number,
        # THE LEADER ENTRY BEHIND A TASK THAT HAS NO CONVERSATION YET, and ""
        # for every task that has a session. A `pending:<entry>` row is a chat
        # whose first message is still waiting: there is no transcript to open
        # and no session id to open it with, so the entry id is the only handle
        # a client has on it — it is what the chat is re-entered by, what Skip
        # names (`api_queue_skip`'s `{entry_id}`), and what cancel writes
        # against. Lifted out of the key rather than left for the client to
        # slice, because the key REKEYS onto the session the moment the leader
        # runs (§5) and a reader that parsed it would be parsing a shape that
        # had moved.
        "entry_id": entry_id,
        # WHO ASKED FOR THE WORK BEHIND A ROW THAT HAS NOT RUN — `"chat"` for a
        # message a composer queued through admission (`api_queue_admit` stamps
        # it and nothing else does), `""` for one somebody SCHEDULED from the
        # calendar or the New task form. The two are the same row shape and a
        # very different thing to a reader: one is a conversation waiting to
        # start and belongs in a list of chats, the other is a job with a date
        # on it and does not. `""` for every task that has a session, which is
        # already a chat and already listed as one.
        "entry_origin": entry_origin,
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
        # WHICH CLAUDE THIS TASK'S RUNS USE and how hard it thinks — "" for the
        # overwhelming majority, which chose neither. Not drawn anywhere: it is
        # what the side peek's composer opens on, so a task set up with a model
        # shows that model instead of whatever the folder last used
        # (`_run_settings`).
        "model": model,
        "effort": effort,
        # Over the WHOLE merged thread, not the three-message tail: "is anything
        # running in this task?" and "is every message filed away?" are both
        # questions about all of it, and a run pushed out of the window by two
        # later occurrences is exactly the run that must not be lost.
        "status": status,
        "failed": failed,
        # WHY it is not moving, in one word, for the two statuses that need one.
        # The Blocked lane holds a run that broke and a run parked on a card, and
        # "Blocked" alone cannot tell the reader which button to reach for —
        # Retry for the first, Open for the second (Akshil, 2026-09-03: "for fail
        # retry, for block a reason"). "" for every task that is neither, which
        # is most of them.
        "blocked_reason": (
            waiting["reason"] if waiting is not None
            # `usage_limit` BEFORE `failed`, because it is the same run
            # described more usefully: the button is not Retry, it is nothing —
            # the continuation is already scheduled and `resumes_at` says when.
            # NEVER OVER A RUNNING TURN: `limited` is read off the last
            # completed speaker, so it stays true for the whole of the comeback
            # turn (and any new turn) until a fresh reply lands — a running row
            # must not read "paused" (Bugbot, de311131b). A limited session
            # whose folder is held reads `queued`, and is still paused: the
            # reason stays (Bugbot, 358864c30).
            else "usage_limit" if limited and status in ("blocked", "queued")
            else ("failed" if failed or status == "blocked" else "")),
        # WHEN A BLOCKED RUN PICKS ITSELF BACK UP — the CONTINUATION the chat
        # scheduled at the reset the CLI reported (PR #1107), and that entry
        # alone (`_comeback_at`). The task's next run of any kind was the wrong
        # clock: an unrelated message due sooner made this row promise the plan's
        # window reopened at a time that had nothing to do with the plan. 0.0
        # when nothing is scheduled (the comeback was cancelled, or the POST that
        # made it failed) and 0.0 for every task that is not waiting on a clock,
        # which is how every other absent time on this row reads.
        "resumes_at": _comeback_at(task["entries"]) if limited else 0.0,
        # The one line under the title on a needs-attention row: which tool, and
        # what it wants to do ("Bash · rm -rf build"). None whenever nothing is
        # waiting — the row draws the sub-line off this, so an empty object would
        # be a caption with nothing in it.
        "attention": ({"tool": waiting["tool"], "summary": waiting["summary"]}
                      if waiting is not None else None),
        "live": live,
        # UNSENT TEXT SITTING IN THIS TASK'S COMPOSER — `{preview, updated_at}`
        # or None, which is what the `✎ Draft` chip and its tooltip are drawn
        # from. A join and not a second poll: the chip has to appear and vanish
        # live, and this row already travels down the changes long-poll every
        # time anything about the task moves (design.md, "Joins"). Only the
        # PREVIEW rides along — one line — because the draft itself belongs in
        # the composer the user left it in, and a listing that carried every
        # unsent message on the machine would be paying transcript-sized costs
        # for a badge.
        # …OR THE FORM BEING WRITTEN INTO IT, when the composer itself is empty
        # (Akshil, 2026-09-12). A task draft bound to this session is unsent
        # words about this conversation just as much, and it has no row of its
        # own to wear a chip — so this row wears it, everywhere, the Cards wall
        # included. One field, one question for every view: is there anything
        # unsent here.
        # …and `kind` says WHICH of the two it is, because the two are two
        # different presses (Bugbot, PR #1126, 2026-09-12). The thread's leading
        # draft line quotes this preview, and a `"chat"` preview is words in
        # this conversation's composer — pressing it opens the chat and there
        # they are. A `"form"` preview is a New task card bound to this session,
        # which holds the time, the repeat rule and the model as well as the
        # words, so that press reopens the card instead. Spelled by the store
        # rather than inferred from `bound_draft` by the page: one fact, one
        # place, and no second rule to go stale the first time a row carries
        # both kinds at once.
        #
        # ONE SOURCE, BOTH KINDS. A bound form reads as its session's chat draft
        # now (`drafts.chat_view`, "one record, two doors") — the composer shows
        # and edits the same words — so the join below answers for both and the
        # old second branch here, which built a preview out of `bound` when the
        # chat half had nothing, would be a second way of saying what the chat
        # half already says. `bound` is still read, for `bound_draft` beneath.
        "draft": _chat_draft(task["session_id"], chat_drafts),
        # WHICH FORM IS BEING WRITTEN INTO THIS CONVERSATION — the bound task
        # draft's id, or "" for the overwhelmingly common row with none. It is
        # not drawn: it is what the composer's Schedule hop reopens, so pressing
        # Schedule in a chat that already has an unsent form comes back to THAT
        # form rather than minting a second one (shell/Scheduled `?new=1`).
        "bound_draft": bound.get("id", ""),
        "unread": _unread_count(task, total, unfired, read),
        # WHEN THIS TASK BEGAN — the EARLIEST clock it has (`_place`, which
        # explains the choice at length): the scheduled entry's `created`, else
        # the transcript's first record. It is the one time on this row that
        # never moves — `last_active` climbs on every write, and `order` beside
        # it switches source when a transcript appears. The Cards wall ordered by
        # it for a while; it now takes the List's order (shell/tasks-lib
        # sortByLane), and the field stays on the row as the one fixed clock.
        # 0.0 for a row that has neither a transcript nor an entry to date, the
        # way every other absent time on this row reads.
        "started": task.get("started") or 0.0,
        "last_active": surfaced,
        "happened_at": happened,
        "message_count": total,
        # The next run, and the entry it belongs to — `min(at)` over every
        # pending entry, not over the window below. 0.0 / "" when the task has
        # nothing pending, which is how every other absent time on this row
        # reads (`last_active`, a message's `ran_at`). See `_next_run`.
        "next_run": next_run,
        "next_run_entry": next_run_entry,
        # ...and whether that run is an occurrence of a repeating template —
        # the chip's repeat glyph. False when nothing is pending.
        "next_run_repeats": next_run_repeats,
        # THE FOLDER THIS TASK EDITS (`project_queue.queue_key`) — what the
        # client groups by. "" for a task whose project is nothing the queue will
        # gate (no path at all, the user's home, the filesystem root) and ""
        # WITH THE FLAG OFF, which is the point: resolving it walks a task's
        # ancestors looking for a `.git`, once per row, and main never did that.
        # Two tasks on two files in one repo share it; a worktree keys on itself
        # and not on the repo it was cut from.
        "queue_key": project_queue.queue_key(task["project"]) if queue_on else "",
        # WHERE IN THE LINE, 1-based — and 0 for every task that is not queued,
        # which is how every other absent number on this row reads. Only ever
        # non-zero alongside `status == "queued"`; the client prints it only
        # after reading the status, so the two cannot contradict each other.
        "queue_position": queued.get("position", 0),
        # WHO IS IN FRONT: the number of the task holding this folder
        # ("TASK-041"), and its title when we can name it. Both "" when the
        # holder is something the listing cannot name — a run started outside
        # this app, a task whose number has not been allocated — and the client
        # falls back to "behind a run in this folder" rather than printing a
        # blank.
        "queue_ahead": queued.get("ahead", ""),
        "queue_ahead_title": queued.get("ahead_title", ""),
        # …AND WHERE "behind TASK-041" GOES WHEN IT IS CLICKED: the holder's own
        # session and target, which is the pair every thread link in this app is
        # built from (tasks-lib `taskHref`). Both "" for a holder with no
        # session yet — a run that has not published one, a claimed message that
        # has not spawned — and the client then prints the name unlinked.
        "queue_ahead_session": queued.get("ahead_session", ""),
        "queue_ahead_target": queued.get("ahead_target", ""),
        # The holder's own TASK key — a `pending:<entry>` for a run that has not
        # named its session yet — so "behind TASK-0xx" can still be a link to
        # that chat while the pair above is still empty.
        "queue_ahead_key": queued.get("ahead_key", ""),
        # Does this task's work go out the moment the folder frees? True for a
        # skipped task (`schedule.set_priority`, which Skip and Run-now write)
        # and for a held answer, which is always at the head.
        "queue_priority": bool(queued.get("priority")),
        # HOW MANY MESSAGES OF THIS TASK'S ARE WAITING — the card's own summary
        # line ("2 messages waiting"), counted here because the row is the only
        # place that has the entries. Every kind of waiting message counts: the
        # ones the chat queued and the ones somebody scheduled are the same
        # thing to a reader looking at a card. 0 for a task with nothing due,
        # and 0 with the flag off like every other queue field on this row.
        "queue_waiting": waiting_count,
        # DOES THE COMPOSER HAVE TO SHUT? True only for a message somebody
        # SCHEDULED into this conversation (no `origin`), which is the one the
        # chat cannot order itself against — the scheduler is about to send it
        # into this very session, and a line typed over it is two messages
        # racing into one run. A message the chat itself queued is that
        # conversation's own next line and never blocks it (Akshil,
        # 2026-09-12). See `_queue_summary`.
        "queue_blocking": blocking,
        # Newest first, which is how every list in this feature reads.
        "messages": list(reversed(tail)),
    }
    if last_message:
        # THE LAST TURN OF THE CONVERSATION, whoever took it — one line of it,
        # with `role` saying which — or None for a task nothing has been said
        # in. `messages` above carries PROMPTS only, so this is the one field on
        # the row that can carry a reply, and it is what the Cards wall titles a
        # card by while `task_card_last_message` is on (shell/prefs.py). Off,
        # the key is absent — which the client reads as "nothing said" — and
        # every row on the machine stops carrying a field no surface draws.
        # It costs no read either way: the reply was condensed by the scan that
        # was already reading the bytes (`_condense_reply`).
        row["last_message"] = _last_message(rec)
    return row


def _leader_origin(task: dict, entry_id: str) -> str:
    """`origin` off the entry a `pending:<entry>` row is keyed by — the LEADER,
    not the newest message, because the leader is the one that decides what kind
    of thing this row is: a chat whose first line queued (`"chat"`, stamped by
    `api_queue_admit` and by nothing else), or a message somebody scheduled
    (absent, which is main's field for field).

    "" for a leader that is not among this task's entries, which can only happen
    if it was cancelled out from under the row between collection and here."""
    for entry in task["entries"]:
        if str(entry.get("id") or "") == entry_id:
            return str(entry.get("origin") or "")
    return ""


def _queue_summary(task: dict, now: float) -> tuple[int, bool]:
    """`(how many of this task's messages are waiting, must its composer shut)`.

    **Waiting** is the same three-part test the line itself applies
    (`queue_manager.reconcile`) minus the folder: pending, and due — by
    `_queue_at`, so a
    message Run now was pressed on counts from that moment rather than from next
    Tuesday. Without the folder because this is a count of MESSAGES, not a place
    in a line: a task with two queued sends says "2 messages waiting" whether or
    not the second one is in a folder anybody is holding, and the card would
    otherwise have to count the entries client-side to say so.

    **Blocking** is the older question, and it is `origin` that finally answers
    it. Before the queue, a pending entry aimed at this conversation shut its
    composer — the scheduler was about to send it into this very session, and a
    line typed over it is two messages racing into one run. Under the queue a
    chat's own send is ADMITTED first and takes its place in the order, so
    nothing it queues can race anything: those entries carry `origin: "chat"`
    and never block. What is left is the message somebody SCHEDULED into this
    session from the calendar or the Tasks page, which the chat still has no way
    to order itself against — so it still shuts the box, exactly as it did
    before (Akshil, 2026-09-12).

    NO DUE FILTER on that half, deliberately: a chat holding next Tuesday's
    message is every bit as blocked as one holding the next thirty seconds' (the
    rule the client has always applied, `sched/scheduled.schedPendingHere`), and
    the two halves of this answer are two different questions about one set of
    entries. Aimed at THIS session by either spelling — the id the entry names
    and the id its run landed in — for the reason `schedPendingHere` reads both.
    """
    session = str(task["session_id"] or "")
    waiting, blocking = 0, False
    for entry in task["entries"]:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        due = _queue_at(entry)
        if due and due <= now:
            waiting += 1
        if (session and not str(entry.get("origin") or "")
                and session in (str(entry.get("session_id") or ""),
                                str(entry.get("claude_session_id") or ""))):
            blocking = True
    return waiting, blocking


def _chat_draft(session_id: str, chat_drafts: dict | None) -> dict | None:
    """One row's `draft` field: the preview of the unsent text in its composer,
    or None. `None` and "an empty draft" are the same thing — the store never
    keeps an empty one — so the client has exactly one question to ask.

    `kind` says WHERE these words are, and both answers come out of this one
    join now. `chat_drafts` is the chat half as a reader sees it
    (`drafts.chat_view`), which carries a stored chat record for every composer
    that is holding something AND a synthesized one for every session whose
    words are in a New task form bound to it — the latter marked `bound_draft`,
    which is the whole of the difference. So `"chat"` is text in this
    conversation's composer and opening the chat is the press for it; `"form"`
    is that card, which holds the time, the repeat rule and the model beside the
    words, and the press reopens it (see `_row`). A session that has both keeps
    them apart: the stored record wins, and `bound_draft` on it is `""`."""
    if not session_id or not chat_drafts:
        return None
    record = chat_drafts.get(session_id)
    if not record:
        return None
    line = drafts.preview(record.get("text"))
    rows = record.get("attachments") or []
    if not line and not rows:
        return None
    return {"preview": line,
            "kind": "form" if record.get("bound_draft") else "chat",
            "updated_at": float(record.get("updated_at") or 0.0)}


# What a task draft's row calls itself where a real task names its lane. A
# draft has no derived status — nothing has run, nothing is booked — and
# `upcoming` is the honest one of the six: design.md puts drafts in the Board's
# Upcoming lane ("an unfinished thing is the most upcoming thing") and the
# List's when-column reads `Draft` off `state`, not off this. `kind` and
# `state` are what tell the two apart; `status` is what keeps every view that
# already switches on the six lanes working without learning a seventh
# (Akshil, 2026-09-11).
_DRAFT_STATUS = "upcoming"

#: What a draft with nothing to call itself is called. A draft row has to be
#: clickable — it is the ONLY way back into the modal (design.md, "Reopen
#: path") — so it can never render as a blank line.
_UNTITLED_DRAFT = "Untitled draft"

#: ...and the same for a chat nobody has sent yet. Different word because it is
#: a different way back in: a task draft reopens the modal, an unsent chat
#: opens the folder's conversation with the composer already holding the text.
_UNTITLED_CHAT = "Untitled chat"

#: How long a draft row's title may be. Shorter than `drafts.PREVIEW_MAX` (the
#: chip's tooltip, which is allowed a whole line) because this is a TITLE, and
#: it sits in the same column as a task's — a draft that printed 120 characters
#: there would be the one row on the page setting its own width
#: (Akshil, 2026-09-11).
_DRAFT_TITLE_MAX = 80


def _draft_title(text, fallback: str) -> str:
    """A draft row's title: the first non-empty line of what was typed, clipped
    to `_DRAFT_TITLE_MAX`, else `fallback`. `drafts.preview` already answers the
    first half of that (and clips to its own, larger, bound), so this only has
    the tighter cut to make."""
    line = drafts.preview(text)
    if len(line) > _DRAFT_TITLE_MAX:
        line = line[:_DRAFT_TITLE_MAX - 1].rstrip() + "…"
    return line or fallback


def _draft_row(ident: str, record: dict, number: str = "") -> dict:
    """One task draft as a listing row.

    THE SAME FIELD SET AS `_row`, deliberately and in full: the List, the Board
    and the changes long-poll all read one row shape, and a row missing half of
    it would make every one of them test for a kind before touching a field.
    What differs is what a draft actually is — no messages and nothing that has
    happened (`happened_at` 0.0 — the desk must not read an unfinished form as
    work that finished under an app).

    NEVER A SESSION-BOUND DRAFT (Akshil, 2026-09-12). The composer's Schedule
    button hops out of a chat that may already have run, and the task being
    written is a message INTO that thread — a thread with a row of its own. That
    draft is therefore not listed at all: `_draft_rows` skips it before reaching
    here, the conversation's row wears the `✎ Draft` chip instead
    (`_bound_chips`), and the form is reopened by the same hop rather than by a
    row. So every row this builder makes belongs to nobody, and `session_id`
    below is always "" in practice — emitted anyway because one row shape is one
    row shape.

    IT DOES HAVE A NUMBER. Round 1 printed `task_id: ""` here on the reasoning
    that a number is minted when a task becomes real; round 2 reversed that,
    because the number is how a person NAMES the thing ("what happened to
    TASK-118?") and a draft they have been typing into for ten minutes is
    already a thing they can name. `draft:<id>` is allocated through exactly
    the path `pending:<entry-id>` uses and is rekeyed forward onto
    `pending:<entry-id>` when the draft is scheduled, so the number the row
    showed while it was a form is the number it keeps once it is a task
    (design.md, "Round 2"; Akshil, 2026-09-11). A SESSION-BOUND draft is the
    one exception and it never gets this far: the number already exists, on the
    session, so nothing is allocated for it and nothing is moved when it is
    scheduled (`_draft_numbers`, and the schedule router's own rekey guard).

    The three fields `_row` has no use for — `draft_kind`, `cwd` and `file` —
    are carried by BOTH draft kinds rather than by whichever needs them, for
    the same reason the rest of the set is: one row shape, tested once.

    `form` is the WHOLE stored draft, which is the one field here that is not
    about drawing the row: clicking a draft row reopens the modal on it, and
    the modal needs every field back, not a summary. It is small — one form —
    and fetching it separately would mean a request between the click and the
    modal, which is exactly the pause the feature exists to remove.
    """
    title = str(record.get("title") or "").strip()
    if not title:
        title = _draft_title(record.get("description"), "")
    target = str(record.get("target") or "")
    # THE FOLDER THE FORM POINTS AT, which is the only answer a row here can
    # have: a draft that belongs to a conversation is not a row (see the
    # docstring), so there is never a session's project to prefer over it.
    place = tasks_store.project_of(_workdir(target))
    created = float(record.get("created_at") or 0.0)
    updated = float(record.get("updated_at") or 0.0)
    return {
        "key": drafts.task_key(ident),
        # ALLOCATED, and allocated under `draft:<id>` so it can be moved onto
        # `pending:<entry-id>` by one `tasks_store.rekey` the moment the form
        # is scheduled. "" only when the state dir is unwritable, which is the
        # same "" every other row falls back to.
        "task_id": number,
        "draft_id": ident,
        "kind": "draft",
        # WHICH composer this draft belongs to. The two are opened by different
        # clicks — a task draft reopens the New task modal, a chat draft opens
        # the folder's conversation — and the row is the only thing that knows
        # which (Akshil, 2026-09-11).
        "draft_kind": "task",
        "state": "draft",
        "project": canonical_fs_path(place),
        "cwd": canonical_fs_path(place),
        "target": canonical_fs_path(target),
        # The raw thing the draft was pointed at, file or folder, BEFORE
        # `_workdir` resolved it to a project. The modal reopens on this.
        "file": canonical_fs_path(target),
        # THE CONVERSATION THIS FORM IS A MESSAGE TO — always "" here, because
        # a bound draft is not listed (see the docstring). Read off the record
        # rather than written as a constant so this stays the row's own answer
        # and not a claim about the caller.
        "session_id": str(record.get("session_id") or ""),
        "title": title or _UNTITLED_DRAFT,
        "title_source": "draft",
        "description": str(record.get("description") or ""),
        "status": _DRAFT_STATUS,
        "failed": False,
        "blocked_reason": "",
        "attention": None,
        "live": False,
        # The chat half of the store keys on a session, and a draft has none.
        # Present anyway so one row shape answers one question.
        "draft": None,
        # A draft is never the row being stood in FOR — it is the stand-in.
        # Present for the same one-row-shape reason as the field above.
        "bound_draft": "",
        "unread": 0,
        # WHEN: a draft has none, and null is the difference between "runs at
        # no particular time" (an immediate task, which has a time) and "has
        # not been given one yet". The List's when-column prints `Draft` here.
        "when": None,
        # The row's clock, and there is only one honest source for it: when the
        # form was started. `at` alongside `started` because the two names are
        # already both in use on this page's rows and a draft answers the same
        # for each.
        "at": created,
        "started": created,
        "created_at": created,
        "updated_at": updated,
        # THE NEWEST EDIT, so the Upcoming lane sorts a draft by when it was
        # last touched. `_row_order` reads this and nothing else once the
        # status ranks tie, which is what puts a draft being typed at the top
        # of the lane where the person who is typing it can see it.
        "last_active": updated or created,
        # Nothing has HAPPENED — see the docstring. `current_apps.observe`
        # reads this field and must not put an app on the desk over a form.
        "happened_at": 0.0,
        "message_count": 0,
        "next_run": 0.0,
        "next_run_entry": "",
        "next_run_repeats": False,
        "messages": [],
        # The modal's way back in. See the docstring.
        "form": dict(record),
    }


def _new_chat_draft_row(key: str, record: dict, number: str = "") -> dict:
    """One UNSENT CHAT as a listing row — a draft keyed `new:<file>`.

    Round 1 gave this key no row: it names no session, so there was nothing for
    the `✎ Draft` chip to hang off. That was the wrong way round. A person who
    has typed half a message into a folder they have never chatted in has
    exactly the same unfinished thing as one who has half-filled the New task
    modal, and the only surface that remembered it was the composer they would
    have to find again. So it is a row, with the folder as its project and its
    first line as its title, and clicking it opens that folder's chat with the
    composer already holding the text (design.md, "Round 2").

    THE SAME FIELD SET as `_draft_row` and `_row`, with a draft's answers: no
    session (there is none until the first send — and a send does not take this
    row's number with it either: a saved draft outlives every send made from
    the same folder, `_settle_new_chats`), no messages, nothing that has
    happened.

    `form` IS THE HOP'S SETTINGS WHEN THERE ARE ANY, and null when there are
    none. It used to be null always, on the reasoning that a chat draft is text
    and attachments and there is no form to reopen — true until the Schedule hop
    stopped minting a `draft:<id>` and started editing THIS record
    (design-drafts-one-record.md, §1). A person who pressed Schedule on an
    unsent chat, set it for Friday and went back is looking at this row, and
    without the time on it the row reads as an ordinary half-written message
    rather than as something booked for Friday. Same field as `_draft_row`
    carries and read the same way (`row.form.when`, `row.form.repeat`); `when`
    on the row itself stays null on both, which is how the List knows to print
    `Draft` in its when-column.
    """
    raw = drafts.new_chat_file(key)
    folder = _workdir(raw)
    project = tasks_store.project_of(folder)
    updated = float(record.get("updated_at") or 0.0)
    line = drafts.preview(record.get("text"))
    rows = record.get("attachments") or []
    form = record.get("form") or {}
    return {
        "key": key,
        "task_id": number,
        # There is no `draft:<id>` behind this one — the store keys it on the
        # folder. "" rather than the key so a client testing `draft_id` cannot
        # send this to the task-draft routes, which would 400 on the shape.
        "draft_id": "",
        "kind": "draft",
        "draft_kind": "chat",
        "state": "draft",
        "project": canonical_fs_path(project),
        "cwd": canonical_fs_path(project),
        # A chat opens on the FILE when the key named one — that is where the
        # person was looking — and the project is still the folder above it,
        # which is the rule every other row's target follows (`_place`).
        "target": canonical_fs_path(raw or project),
        "file": canonical_fs_path(raw),
        "session_id": "",
        "title": _draft_title(record.get("text"), _UNTITLED_CHAT),
        "title_source": "draft",
        "description": "",
        "status": _DRAFT_STATUS,
        "failed": False,
        "blocked_reason": "",
        "attention": None,
        "live": False,
        # THIS row's chip is about itself. Every other row joins its draft off
        # the session id; this one IS the draft, so the join is the identity.
        "draft": ({"preview": line, "updated_at": updated, "kind": "chat"}
                  if (line or rows) else None),
        "bound_draft": "",
        "unread": 0,
        "when": None,
        # One clock, because the store keeps one: a chat draft has no
        # `created_at` (it is a single upsert keyed on the folder, not a form
        # with a birth), so `updated_at` answers every time on this row. That
        # also puts it at the top of Upcoming while it is being typed, which is
        # where the person typing it can see it.
        "at": updated,
        "started": updated,
        "created_at": updated,
        "updated_at": updated,
        "last_active": updated,
        "happened_at": 0.0,
        "message_count": 0,
        "next_run": 0.0,
        "next_run_entry": "",
        "next_run_repeats": False,
        "messages": [],
        # The hop's settings, or null — see the docstring. `updated_at` rides
        # along inside it for the same reason it does on a task draft's form:
        # `tasks-lib.draftUpdatedAt` reads it as one of its three sources.
        "form": dict(form, updated_at=updated) if form else None,
    }


def _draft_numbers(task_drafts: dict, chat_drafts: dict) -> dict[str, str]:
    """TASK numbers for every draft, allocating what is missing.

    `tasks_store.ensure_ids` and nothing else — the same call, the same file
    and the same allocate-once promise the listing's `_numbers` uses, because
    the whole point is that the number a draft shows is the number its task
    will keep. The project each draft is numbered in is the FOLDER it is
    pointed at, which is what `_workdir` resolves and what both row builders
    above print.

    Over EVERY draft, never the `only` subset: `ensure_ids` hands numbers out
    in the order it is given them, so numbering a narrowed set would let the
    changes endpoint allocate in a different order than the listing does.

    Sorted by when the draft was started, the same `order` the listing passes,
    so a backfill over a store that predates numbering reads in the order the
    drafts were actually typed. Degrades to no numbers on an unwritable state
    dir, exactly like `_numbers`: blank numbers, never a lost page.

    `reproject=True` is the ONE way this differs from `_numbers`, and it is the
    one thing a draft has that a task does not: a folder the user can still
    change. The number is allocated at the first keystroke, under the folder the
    modal opened on — change the folder afterwards and the number allocated in
    the old project rode along into the new one, which is how a list of
    TASK-001…015 came to show a TASK-202. A draft's number belongs to the
    project it points at NOW, so the store drops the old mapping and allocates
    afresh (`tasks_store.ensure_ids`); the old number stays spent, a gap in the
    project it was minted in. Nothing is renumbered once the draft is scheduled
    — that rekey onto `pending:` is what fixes it for good (Akshil,
    2026-09-11). `new:<file>` chat drafts carry their file IN the key and so can
    never move, which is why one flag covers both kinds here.

    A SESSION-BOUND TASK DRAFT IS NOT NUMBERED AT ALL, and that absence is the
    point (Akshil, 2026-09-12). Such a draft is a message being written INTO a
    conversation that already has a number, and the reported bug was exactly
    that a second one got minted: exit the modal, reopen the draft, press
    Schedule, and the task the reader had been watching as TASK-118 came back as
    TASK-119 in a session of its own. Such a draft has no row of its own to put
    a number on either (`_draft_rows` skips it; the session's row wears the
    chip), so nothing on the page goes blank — what does not happen is an
    allocation. `ensure_ids` is never told about the key, so `reproject` cannot
    spend anything under it either.
    """
    items = []
    for ident, record in task_drafts.items():
        if record.get("session_id"):
            continue  # the session holds this task's number — see the docstring
        target = str(record.get("target") or "")
        items.append((drafts.task_key(ident),
                      tasks_store.project_of(_workdir(target)),
                      float(record.get("created_at") or 0.0)))
    for key, record in chat_drafts.items():
        if not drafts.is_new_chat_key(key):
            continue  # a chat draft on a real session is a chip, not a row
        items.append((key,
                      tasks_store.project_of(_workdir(drafts.new_chat_file(key))),
                      float(record.get("updated_at") or 0.0)))
    if not items:
        return {}
    try:
        return tasks_store.ensure_ids(items, reproject=True)
    except OSError:
        return {}


def _bound_chips(task_drafts: dict) -> dict[str, dict]:
    """`{session-id: {id, preview, updated_at}}` — the New task form somebody
    opened out of a conversation and has not sent yet, keyed by that
    conversation.

    THE WHOLE OF WHAT A BOUND DRAFT DOES TO THIS LISTING (Akshil, 2026-09-12).
    Such a draft gets no row of its own (`_draft_rows`) and no number
    (`_draft_numbers`) — it is the next message of a task that already has both
    — so the only trace of it is here: the session's own row, otherwise exactly
    as it always was, grows `bound_draft` (which form) and, when that
    conversation's composer is empty, `draft` (the preview the red `✎ Draft`
    chip prints). Nothing is hidden, nothing stands in for anything, and a
    click on the row opens the chat like every other session row; the way back
    into the form is the composer's Schedule hop, which reopens it by id.

    THE PREVIEW IS THE DRAFT'S FIRST LINE, title before description: the title
    is what the reader named this thing and what the chip's tooltip should say,
    and the description's first line is the fallback for a form that has only
    been typed into. The same one line and the same clipping every other draft
    preview uses (`drafts.preview`).

    NEWEST WINS when two forms name one session — a shape nothing produces
    today (the hop reopens the form that is already bound) but one an old store
    could hold. Deterministic beats first-seen: a dict order is not an answer.
    """
    out: dict[str, dict] = {}
    for ident, record in task_drafts.items():
        session = str(record.get("session_id") or "")
        if not session:
            continue
        # A WORDLESS FORM IS NOT A DRAFT (2026-09-15). Emptying the composer on a
        # bound session now clears the words and KEEPS the form's settings
        # (`drafts._put_bound`), so a record here can hold a time and a model and
        # nothing anybody typed — and a `✎ Draft` chip over that would point at a
        # composer the reader would find empty. Same answer `drafts.chat_view`
        # gives through the other door.
        if not (str(record.get("title") or "").strip()
                or str(record.get("description") or "").strip()
                or record.get("attachments")):
            continue
        updated = float(record.get("updated_at") or 0.0)
        if session in out and out[session]["updated_at"] >= updated:
            continue
        line = (drafts.preview(record.get("title"))
                or drafts.preview(record.get("description")))
        out[session] = {"id": str(ident), "preview": line,
                        "updated_at": updated}
    return out


# How many run dirs (newest first) one settle pass reads. The run that created
# the session a `new:<file>` key is waiting for is by construction a recent one
# — the draft is the message that started it — and nothing prunes RUNS, so the
# tail of that tree is months of dead runs. Deliberately the same order of
# magnitude as `_PARKED_SCAN_LIMIT` and agent.py's own `_LIVE_SCAN_LIMIT`.
_NEW_CHAT_SCAN_LIMIT = 60


def _settle_new_chats(chat_drafts: dict, task_drafts: dict) -> bool:
    """Walk a STRANDED draft TASK number onto the session the send that SPENT
    it created. True if anything moved.

    TWO KEY SHAPES, one rule. A session-less composer used to draft only under
    `new:<file>`; it can also send out of a TASK DRAFT now, and the key it tags
    that run with is then the draft's own `draft:<id>` (`drafts.task_key`).
    Both shapes are rows with a number of their own, both stop being a row when
    the send spends them, and in both the number has to follow the session the
    send makes — so both are read here, under the same hands-off rule: a key
    whose RECORD still exists (a saved chat draft, a task draft still in the
    drafts store) is not this function's to touch. The composer DELETEs the
    task draft's record as it sends, so the number moves on the first build
    after that delete has landed; a build that gets there first just asks
    again.

    A SEND NEVER TOUCHES A SAVED DRAFT (blocker, 2026-09-16). This used to
    `delete_chat` the record too, and that was the truth while the composer
    autosaved on every keystroke: the record WAS the message going out, and the
    transcript took the words over in the same tick. The composer autosaves
    nothing now (caef75eb1, `ui/Composer.tsx` — "a send just sends"), so a
    `new:<file>` record exists only because the reader ASKED for one — "Save as
    draft" in the leave guard, or Schedule → Continue — and a send is not an
    answer to that question. Type, leave, save as draft, come back, type
    something else, send: the saved Upcoming row and its number both stand, and
    the session the send makes gets a number of its own. So a key that still
    HAS a record is not settled here at all. What is left for this to settle is
    the NUMBER ALONE — a `new:<file>` key that carries one in `task_ids` with
    no record behind it, the row the reader was watching left on a key nothing
    reads again.

    THE SEND NAMES ITS OWN DRAFT, and nothing here infers it. A chat with no
    session drafts under `new:<file>` and is numbered under that key; the first
    send creates the session, and a number with no record left to hold it has
    to follow that session or it is stranded for good. So the
    composer's session-less send puts the key it is spending in the start
    request (`protocol/run-controller.ts`, `draft_key`), `agent._start` writes
    it into `meta.json` verbatim before it spawns anything, and this reads it
    back. The draft key is a RECEIPT written by the process that spent it,
    which is the one thing about that send nobody has to guess at.

    WHY NOT THE TARGET. The first build of this matched a run to a draft by
    "same file, and `resumed_from` is empty" — a first-ever run on that
    folder — and every OTHER way a folder gets its first run passes those same
    two guards: a scheduled task's first fire (`schedule.py::_send` →
    `claude_spawn.spawn_helper` → `agent._start`), a `new_task_each_run` entry,
    canvases.py's own spawn. Any of them would have taken the number off a chat
    the reader was still typing into — and, in the build that still deleted,
    the unsent words with it (review, 2026-09-12). None of them sends a
    `draft_key`, so none of them can claim a number now.

    (The four rounds before that asked the CLIENT which session its own send
    created, and each answer was an inference with a gap — a send that threw, a
    refusal that never left `idle`, a Back before the id landed — that left the
    move owed to whichever session id turned up next. Tagging the run is not
    that: the page is not saying which session it made, it says which draft
    it spent, which it knows at the moment it spends it.)

    WHERE IT RUNS. `agent.py` is a TEMPLATE — outside the package's import
    graph by design (SPEC PY-15), so it cannot call `tasks_store` or `drafts`
    at the moment it learns the id — and a chat's own run is started and polled
    in an executor subprocess, never in this process. The nearest thing the
    server has to that moment is the build below: it is the only reader of the
    number, it already scans this tree for parked runs, and it runs BEFORE
    `_numbers` allocates, so the session this settles is still unnumbered when
    it gets here. Latency is one listing (the changes long-poll the chat itself
    holds, in practice).

    TWO GUARDS BESIDES THE KEY:

    * **A record, and this key is not ours.** A `new:<file>` key with a saved
      draft behind it — or a `draft:<id>` key whose task draft is still in the
      store — is a row the reader put there on purpose; its number is that
      row's, not the send's. Excluded before the tree is read at all, so no
      tagged run can claim it however old or new the save is.
    * **A session id, or nothing happens.** A run that never got one (`_start`
      failed, the CLI died before its first row) has nothing to carry the
      number to. Asked again on the next build.
    """
    records = {key for key in chat_drafts if drafts.is_new_chat_key(key)}
    # …AND THE TASK DRAFTS UNDER THE KEY THE LISTING FILES THEM BY. A record in
    # the task store means the row is still there to wear its own number, on
    # exactly the reasoning the chat half is read for. `list_all()` keys the
    # task section by id, so it is spelled back into a listing key here rather
    # than each candidate being spelled the other way round.
    records |= {drafts.task_key(ident) for ident in task_drafts}
    # A RECORD MEANS HANDS OFF, and the records are read here only to say which
    # keys this must not touch. Nothing under a key the reader saved moves: not
    # the words, not the number, not on any later build. What is left is the
    # number with no record behind it, and the numbers store is the only place
    # that is written down — which is why this read is not gated on `records`
    # being non-empty (review perf note, 2026-09-12): gating it there would
    # skip exactly the case this function exists for. One small json file, read
    # before anything decides to touch the runs tree.
    # A key stamped SPENT (`tasks_store._apply_rekey`) has already been settled
    # by an earlier build — its send landed in a session that was already
    # numbered some other way, so there is nothing left to move — and must not
    # be asked again: `task_ids()` never drops a spent record (it is the
    # high-water mark), so without this exclusion every later build would
    # re-find it, re-run the loop below, and re-notify forever (bugbot / live
    # repro, 2026-09-15 — the `gone` key that pinned a composer shut).
    waiting = {
        key for key, rec in tasks_store.task_ids().items()
        if (drafts.is_new_chat_key(key) or drafts.task_draft_id(key))
        and not rec.get("spent") and key not in records
    }
    if not waiting:
        return False  # nothing unsent is numbered: no reason to read the tree
    agent = _agent_module()
    runs = getattr(agent, "RUNS", "") if agent is not None else ""
    if not runs:
        return False
    try:
        names = sorted(os.listdir(runs), reverse=True)[:_NEW_CHAT_SCAN_LIMIT]
    except OSError:
        return False  # no runs tree yet: nothing has ever chatted here
    moved = False
    for name in names:
        if not waiting:
            break  # every key settled; the older runs have nothing to say
        run_dir = os.path.join(runs, name)
        meta_path = os.path.join(run_dir, "meta.json")
        try:
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            continue  # one unreadable run, not an unsettled draft for ever
        if not isinstance(meta, dict):
            continue
        # VERBATIM ON BOTH SIDES. The key is stored unnormalised on purpose
        # (`drafts.chat_key`, `chatDraftKey`) and rides the request untouched,
        # so this is a string comparison and never a path one: a run tagged for
        # another key — or for a key some earlier run already settled — is not
        # this draft's send.
        key = str(meta.get("draft_key") or "")
        if key not in waiting:
            continue
        # The session this run MADE. `_run_sessions` answers both spellings —
        # the one it resumed and the one the CLI minted — and a tagged run has
        # no `resumed_from` by construction (the composer tags only a send with
        # no session id), so subtracting it is belt and braces rather than a
        # second guess.
        ids = _run_sessions(agent, run_dir, meta) - {
            str(meta.get("resumed_from") or "")}
        if not ids:
            continue  # no id minted yet (or ever): ask again next build
        session_id = sorted(ids)[0]
        try:
            number_moved = tasks_store.rekey_moved(key, session_id)
        except OSError:
            # A read-only state dir costs the number's continuity and nothing
            # else. Same posture as `_numbers` and the schedule router's own
            # rekey: the listing still answers.
            continue
        waiting.discard(key)
        # NOTIFY ONLY ON A REAL CHANGE. `rekey_moved` stamps a no-op key spent
        # the first time it is seen (so `waiting`, above, excludes it from
        # then on) but that stamp alone is not news to any client — the
        # session already had its number, and nothing in the listing reads
        # differently than it did a moment ago. Without this check every build
        # that still found the key (before the `waiting` exclusion took effect
        # on the NEXT build) called notify unconditionally, and the changes
        # long-poll it wakes rebuilt the listing, re-ran this same settle, and
        # notified again — the loop that pinned a composer shut (bugbot / live
        # repro, 2026-09-15).
        if number_moved:
            moved = True
            # The number changed hands: the old key stops answering to it and
            # the session wears it. The chat holding the changes long-poll
            # hears it now rather than on its next full pass.
            tasks_watch.notify({key, session_id})
    return moved


def _draft_shaped(only: frozenset | set) -> bool:
    """Could ANY of these keys name a draft row?

    `_draft_rows` emits exactly two key shapes: `draft:<id>` for a task draft
    and `new:<file>` for a chat that has never been sent. A session key never
    reaches it — a chat draft filed under a real session is a CHIP on that
    session's own row (`_chat_draft`, off the `chat_drafts` the build already
    holds), not a row of its own.

    So a narrowed build asking about anything else has no draft row to find,
    and running the draft half anyway cost it `ensure_ids(reproject=True)` —
    a write-shaped pass under the task_ids lock — on every `/api/tasks/changes`
    poll about an unrelated session. The full build (`only is None`) never
    takes this door.
    """
    return any(key.startswith(("draft:", drafts.NEW_CHAT_PREFIX)) for key in only)


def _draft_rows(only: frozenset | set | None = None,
                chat_drafts: dict | None = None,
                task_drafts: dict | None = None) -> list[dict]:
    """Every draft as a row — task drafts AND unsent new chats — narrowed to
    `only` when the caller is the changes endpoint.

    `chat_drafts` and `task_drafts` are `drafts.list_all()`, read once by the
    caller for the same reason the row join reads the chat half once: it is
    ONE file, and between the row join, the row build and the numbering it was
    being asked three times per listing. Read here when the caller has no copy,
    so the function still answers on its own.
    """
    if only is not None and not _draft_shaped(only):
        return []
    if task_drafts is None or chat_drafts is None:
        loaded_task, loaded_chat = drafts.list_all()
        if task_drafts is None:
            task_drafts = loaded_task
        if chat_drafts is None:
            chat_drafts = loaded_chat
    numbers = _draft_numbers(task_drafts, chat_drafts)
    # Which sessions this machine has ERASED — read at most once per build, and
    # only when a bound draft is actually seen, because it is a file read and
    # nearly every listing has nothing bound at all. See the skip below.
    erased: set[str] | None = None
    rows = []
    for ident, record in task_drafts.items():
        key = drafts.task_key(ident)
        if only is not None and key not in only:
            continue
        # A DRAFT BOUND TO A SESSION IS NOT A ROW (Akshil, 2026-09-12). It is
        # the next message of a conversation that already has one, and the
        # conversation's row is the one the reader knows — its title, its lane,
        # its number, its place in the list. So nothing is emitted here and
        # nothing is hidden anywhere: the session's row simply grows the red
        # `✎ Draft` chip (`_bound_chips`, joined in `_row`), and the way back
        # into the form is the composer's Schedule hop, which reopens THIS
        # draft rather than minting another (shell/Scheduled `?new=1`).
        #
        # Round 3 built it the other way — a draft row wearing the session's
        # identity, with the session's own row held back — and that row was a
        # second thing to read, took the conversation off the Cards wall (a
        # wall of transcripts, which a draft has none of), and made a click on
        # it open a modal where every other session row opens the chat
        # (bugbot, PR #1126).
        #
        # …UNLESS THE CONVERSATION IS GONE. `forget_session` does not remove a
        # number, it stamps the mapping `erased` and keeps it as a reservation,
        # and the row it belonged to is off the page for good — so a draft still
        # naming it has nothing left to wear its chip and would be invisible,
        # with its words unreachable. The erase itself drops such a draft
        # (`drafts.delete_bound`), so this is the answer for the two stores that
        # can still hold one: a draft written between that gesture's two writes,
        # and any older store left bound by a build that only unbound. An
        # ordinary row again, blank-numbered until `_draft_numbers` can mint it
        # one (review, 2026-09-12).
        bound = drafts.bound_session(record.get("session_id"))
        if bound:
            if erased is None:
                try:
                    erased = tasks_store.erased()
                except OSError:
                    erased = set()
            if bound not in erased:
                continue
        rows.append(_draft_row(ident, record, numbers.get(key, "")))
    for key, record in chat_drafts.items():
        if not drafts.is_new_chat_key(key):
            continue
        if only is not None and key not in only:
            continue
        rows.append(_new_chat_draft_row(key, record, numbers.get(key, "")))
    return rows


def _run_settings(task: dict) -> tuple[str, str]:
    """(model, effort) — the flags this task's runs are launched with, or ""
    for a task that never chose.

    THE ENTRY IS THE TRUTH, not a second opinion about it. `schedule._send`
    hands `entry["model"]` / `entry["effort"]` straight to
    `claude_spawn.spawn_helper`, which hands them to `claude --model` /
    `--effort` (`agent._claude_argv`). There is no gap between "what the task
    is set to" and "what the run used", so one field answers both questions and
    nothing has to decide between them.

    WHY THE ROW CARRIES THEM AT ALL, when the design says the card asks and the
    list stays quiet (NewJobModal's own note): nothing here DRAWS them. They are
    for the side peek, whose composer is a real chat — and a chat with no
    opinion handed to it detects the model last used in that FOLDER
    (`agent._defaults`), which is the right answer for a chat somebody opened on
    a folder and the wrong one for a task that was set up with a model. The peek
    showed a reader settings they had not chosen (Akshil, 2026-09-18).

    NEWEST FIRST, and PER FIELD. A task is a thread and a thread can hold
    several scheduled messages; the newest that names a setting is the one a
    reader is about to act on — the same rule `_description` takes over the same
    list. Per field rather than per entry because an entry that pinned only the
    effort must not wipe a model an earlier one pinned; that is also how
    `agent._defaults` fills its own two fields.

    "" IS A REAL ANSWER and the load-bearing one. It means "this task has no
    opinion", which is what leaves the chat's own detection speaking for every
    conversation that never went through the New task card. Answering "sonnet"
    here would pin every hand-typed chat on the machine to a model nobody chose.
    """
    model = effort = ""
    for entry in reversed(task["entries"]):
        model = model or str(entry.get("model") or "").strip()
        effort = effort or str(entry.get("effort") or "").strip()
        if model and effort:
            break
    return model, effort


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


# Which rows the listing puts at the top, ahead of recency. Two ranks and then
# everybody, because there are exactly two kinds of row a person has to DO
# something about (Akshil, 2026-09-03: "in list view they should be at top").
#
# Recency is the right order for a page you READ, and the wrong one for a page
# you WORK: a run parked on a permission card at 6am is the single most urgent
# row on the machine and, sorted by activity, sinks below every chat typed
# since. So the two ranks that want hands come first — the parked one above the
# broken one, because the parked run is still burning a session — and inside
# every rank the page's one honest question is still recency.
#
# The client sorts the LIST for itself (tasks-lib.LIST_ORDER, which ranks all six
# statuses); this is the order the rows arrive in, which is what a reader of the
# raw endpoint and the sidebar's projection see, and what every tie inside a
# client rank falls back to.
_SORT_RANKS = ("needs_attention", "blocked")


def _row_order(row: dict) -> tuple:
    """The listing's order: attention first, then blocked, then newest."""
    status = str(row.get("status") or "")
    rank = _SORT_RANKS.index(status) if status in _SORT_RANKS else len(_SORT_RANKS)
    return (rank, -float(row.get("last_active") or 0.0))


# One listing at a time. The scan caches above are filled by whichever request
# first asks; two requests landing on a cold process (the sidebar's pulse and
# the Tasks page fire within the same second) would otherwise each read every
# transcript on the machine from byte zero. A warm listing is tens of
# milliseconds, so serializing them costs nothing anyone can see, and a request
# that arrives while `warm` is still reading waits for that one scan rather
# than starting a second.
_ROWS_LOCK = threading.Lock()


def warm() -> None:
    """Fill the transcript caches once, so the first real listing is warm.

    The process starts with `_SCAN` and the head cache empty, and the first
    `_task_rows` reads every transcript on the machine from byte zero — close to
    a gigabyte and three seconds on a busy laptop — synchronously, inside
    whichever request asked first. Called from the app's startup event on a
    thread of its own (server/app.py), never from create_app: tests build apps
    without lifespan and must not read the developer's real ~/.claude.
    """
    started = time.monotonic()
    try:
        rows = _task_rows()
    except Exception:  # noqa: BLE001 — a warm that fails costs nothing but the warmth
        logger.debug("tasks warm failed", exc_info=True)
        return
    logger.info("tasks warm: %d rows in %.2fs", len(rows), time.monotonic() - started)


def _task_rows(only: frozenset | set | None = None) -> list[dict]:
    """`_build_task_rows`, one caller at a time. See `_ROWS_LOCK`."""
    with _ROWS_LOCK:
        return _build_task_rows(only)


def _build_task_rows(only: frozenset | set | None = None) -> list[dict]:
    """Build the authoritative task rows shared by the two listing shapes.

    Keeping collection here makes the compact sidebar endpoint a projection of
    exactly the same status, unread and activity decisions as the Tasks page.
    FastAPI serializes only the projection returned by that endpoint, so the
    large titles, descriptions and message bodies never cross the wire there.

    `only` narrows the answer to those task keys — the changes endpoint's
    shape. Collection still runs over everything (it is a glob and a store
    read, and a task's key can depend on entries filed under another), but rows
    are built for the named keys alone, and the day-one read initialisation is
    left to the full listing, which is the only caller that knows every count.
    """
    triage = sessions._load_state("triage.json")
    read = tasks_store.read_state()
    now = time.time()
    tasks = _collect()
    # ONE READ OF THE FLAG for the whole listing, handed to every row: it is a
    # small JSON read (`prefs.project_queue_enabled`) and asking it per row would
    # pay for it once per task on the machine.
    queue_on = project_queue.enabled()
    # THE LINE IS DERIVED OVER EVERYTHING, BEFORE THE NARROWING. Where a task
    # stands is a fact about every other task waiting on the same folder, so a
    # changes answer that asked about one row would otherwise report it at #1
    # with three tasks in front of it. `only` narrows what is BUILT, never what
    # the queue is read from — the same reason collection itself still runs over
    # the whole store.
    # ONE READ OF THE SCHEDULER'S STORE for the whole listing: the line's index
    # (`order_key` walks it to keep a follower behind its leader) and the busy
    # pass below both want the same entries, and reading them twice would be two
    # answers about one file a write could land between.
    entries = schedule.list_entries()
    queue = _queue_lines(tasks, now, _by_entry_id(entries))
    listed = tasks
    if only is not None:
        listed = {key: task for key, task in tasks.items() if key in only}
    # One pass over the store for every row: which conversations the scheduler
    # is still waiting on. See `_status`.
    busy = schedule.busy_sessions(entries)
    # One scan of the runs tree for every row: which conversations are parked on
    # a card nobody has answered. See `_parked_runs`.
    parked = _parked_runs()
    # ONE read of the drafts store for the whole build, for the same reason
    # `read` and `busy` are read once: it is one small file, the join below
    # asks it per session, and `_draft_rows` asks it again.
    task_drafts, chat_drafts = drafts.list_all()
    # A STRANDED DRAFT NUMBER IS SETTLED HERE (`new:<file>` or `draft:<id>`,
    # whichever key the send tagged its run with), before a number is
    # allocated below: a key with no record left behind it hands its number to
    # the session the send created (`_settle_new_chats` — and see its docstring
    # for why this is the server's job, and why a key that still HAS a record
    # is never touched). A settle rewrites the store this build has already
    # read, so the read is taken again; it is one small file, and it only
    # happens on the build a number is settled in.
    if _settle_new_chats(chat_drafts, task_drafts):
        task_drafts, chat_drafts = drafts.list_all()
    # …and the same read, asked the session's way round: which conversation has
    # a New task form being written into it. Off the STORE and not off the rows
    # this build produces, so a `/api/tasks/changes` poll narrowed to the
    # session alone — which builds no draft rows at all — still answers a row
    # that knows about its draft (`_bound_chips`).
    bound_chips = _bound_chips(task_drafts)
    # ONE prefs read for the whole build, like every other join above: whether
    # a row carries `last_message` at all (shell/prefs.py, experimental and
    # default off). Per row it would be one file open per task on the machine,
    # per poll.
    last_message = shell_prefs.task_card_last_message()
    for task in listed.values():
        _place(task)
    numbers = _numbers(listed)
    # The number and the name of whoever is in front, once the listing's own
    # allocation has run: a task seeing its first listing gets its number HERE,
    # and a holder outside a narrowed answer is named from the stored table.
    # See `_name_ahead`.
    _name_ahead(queue, numbers, tasks)
    rows = []
    # Sessions whose archive record the thread has outlived — see `_revived`.
    # Collected across the loop and written once, after it, so the listing is
    # not doing IO in the middle of building rows.
    revived: list[str] = []
    for task in listed.values():
        try:
            row = _row(task, numbers.get(task["key"], ""), triage, read, now,
                       busy, revived, parked, queue, queue_on,
                       chat_drafts, bound_chips, last_message)
        except (OSError, ValueError, KeyError, TypeError):
            continue  # one unreadable task, not an unreadable page
        rows.append(row)
    for session_id in revived:
        # THE WAY OUT OF ARCHIVE IS ACTIVITY, and it has to be a real way out:
        # the row already reads as its derived lane above, and leaving the
        # record on disk would put the task back in Archive the moment it went
        # quiet again. Best-effort — a filing we could not drop costs one poll's
        # worth of the row coming back, never the listing.
        try:
            sessions.clear_triage(session_id)
        except OSError:
            pass
    # Day one: everything that already exists is read. Done HERE, from the
    # counts the rows just produced, because this is the only place that knows
    # them — and done after the rows are built rather than before, so it costs
    # one extra pass on exactly one request in the store's lifetime.
    if only is None and not tasks_store.initialized(read):
        tasks_store.initialize([(r["key"], r["message_count"]) for r in rows])
    # DRAFTS ARE ROWS TOO — the New task modal's half-filled forms AND the
    # chats that have been typed into but never sent (`new:<file>`) — added
    # here, AFTER the day-one read baseline and BEFORE the sort. After, because
    # a draft has no messages and nothing to have read; before, because the
    # Board orders Upcoming off this list and a draft has to arrive already in
    # its place rather than appended past the end of the lane.
    rows.extend(_draft_rows(only, chat_drafts, task_drafts))
    rows.sort(key=_row_order)
    # The Current apps desk (current_apps.py) learns about NEW tasks here —
    # the one place every task on the machine passes, whatever started it.
    # Best-effort: the desk is a side table, and a store that cannot be
    # written costs an app on the sidebar, never the listing.
    # Not from a partial listing: `observe` reads its argument as EVERY live
    # task and prunes what it does not see (bugbot, PR #892).
    if only is None:
        try:
            # WITHOUT THE DRAFTS. `observe` reads a row as a task that exists
            # and puts its folder on the desk; a half-typed form is not a task
            # under an app yet, and a draft that was discarded would leave an
            # app behind that nothing ever ran in.
            current_apps.observe([r for r in rows if r.get("kind") != "draft"])
        except OSError:
            pass
    # ONE TASK, ONE ROW — and it is the CONVERSATION's row (Akshil,
    # 2026-09-12).
    #
    # A task draft bound to a session is not a second thing beside that
    # conversation, it is the conversation's next message being written. Round 3
    # said so by emitting a DRAFT row wearing the session's number and dropping
    # the session's own row behind it, and every part of that was wrong to read:
    # the reader lost the title and the lane they knew, the Cards wall lost the
    # card entirely (it draws transcripts, and `kind:"draft"` has none), and a
    # click on the row opened a modal where every other session row opens the
    # chat.
    #
    # So there is nothing to do here at all. The bound draft is simply not a row
    # (`_draft_rows`) and not a number (`_draft_numbers`); the session's row is
    # untouched but for the chip it now wears (`_bound_chips`, joined in `_row`),
    # and NOTHING IS HIDDEN — no status to check, no pulse to protect, no claim
    # to read off a store because no row is being dropped. This tail used to
    # hold that swap, and the absence is the fix (bugbot, PR #1126).
    return rows


@router.get("/api/tasks")
def api_tasks():
    """Every task, newest activity first, each with its three newest messages.

    Includes tasks that have never been scheduled (a chat session is a task) and
    tasks that have never run (a message scheduled for tomorrow is a task, §5).
    Excludes the ones that stopped being tasks: no session, and nothing left to
    run — see `_is_task`. That is an absence of a task, not a filter hiding one.
    """
    rows = _task_rows()
    return {"tasks": rows, "generation": tasks_watch.generation()}


def _draft_changes(keys) -> dict:
    """What the DRAFTS under these keys are at now: `{changed: [{key, version}],
    gone: [key]}`.

    THE PUSH HALF OF VERSIONED WRITES (design-drafts-one-record.md, §3). A
    composer and a New task modal open on one key in two windows have to hear
    about each other, and the rows this endpoint already sends cannot say it: a
    row carries a preview, not the record, and a draft edited in another tab
    changes nothing about the row's shape. So the keys the watcher announced are
    looked up in the drafts store and reported as a version apiece — enough for
    a client to know whether what it holds is stale without asking for it.

    ONE READ AND NO ROW BUILDING. `list_all` is a single json read, which is
    what makes this cheap enough to ride on every long-poll answer;
    `_task_rows` is not asked anything, and no number is allocated.

    `gone` IS NOISY BY CONSTRUCTION and is documented as such for the client
    (drafts-api-contract.md): these keys are announced for every reason a row
    moves, so most of them never had a draft and "there is no draft under this
    key" is simply true of them. What a client may do with it is bounded by
    that — clear an editor it has already adopted a server version for, and
    never discard words it has not saved yet (the same care #1171's cheap `gone`
    for rows is read with)."""
    task_drafts, chat_drafts = drafts.list_all()
    changed: list[dict] = []
    gone: list[str] = []
    for key in sorted(keys):
        ident = drafts.task_draft_id(key)
        record = task_drafts.get(ident) if ident else chat_drafts.get(key)
        if record is None:
            gone.append(key)
        else:
            changed.append({"key": key, "version": int(record.get("version") or 0)})
    return {"changed": changed, "gone": gone}


@router.get("/api/tasks/changes")
def api_tasks_changes(since: int = Query(-1), wait: float = Query(tasks_watch.MAX_WAIT_SEC)):
    """What moved since generation `since` — the Tasks page's fast lane.

    Long-poll: answers the moment the watcher (tasks_watch) sees a session
    start, resume, take a prompt or grow its transcript, and otherwise after
    `wait` seconds with nothing. The answer is the full rows for exactly the
    tasks that changed (`rows`), plus the keys that changed but are no longer
    listed (`gone` — archived-into-silence, deleted, or a pending message
    whose session id it has just been rekeyed under). A client further behind
    than the watcher remembers gets `full: true` and reloads the listing.

    The 20-second full listing stays the truth; this only makes the page hear
    about a change without waiting for it."""
    gen, keys = tasks_watch.wait(since, wait)
    if keys is None:
        return {"generation": gen, "full": True}
    if not keys:
        return {"generation": gen, "rows": [], "gone": [],
                "drafts": {"changed": [], "gone": []}}
    # Translate before diffing: a rung `pending:<id>` whose entry the listing
    # now files under its run's session is not a task that went away, it is a
    # row that changed its name (`_rekeyed_pendings`, 2026-09-16).
    rekeyed = _rekeyed_pendings(keys)
    rows = _task_rows(only=set(keys) | set(rekeyed.values()))
    listed = {row["key"] for row in rows}
    # A rung key whose translation IS listed still answers `gone` — and that is
    # a SWAP, not the deletion this endpoint used to do: the row it re-keys to
    # rides in the same payload, and the client folds the two together in one
    # pass (`mergeTaskChanges`: drop the gone keys, upsert the rows), so the
    # pending row leaves exactly as the session row arrives. What was broken
    # was the naked `gone` with no row behind it.
    gone = {key for key in keys if key not in listed}
    # A pending message that has just RUN is now filed under its session id
    # (§5): the watcher names the session, the session is listed, and the
    # `pending:<entry>` row the client still shows is nobody's key. Name it
    # gone, or two rows stand for one task until the full poll (bugbot #892).
    #
    # SCOPED, and this is the fast lane's whole cost profile. This used to be a
    # second full `_collect()` — a glob over every project dir on the machine and
    # a re-read of the deleted store — paid on every long-poll answer, to look up
    # the entries of the handful of keys the answer already names. `_entries_for`
    # is `_collect`'s entry pass alone, over the scheduler's store, for exactly
    # those keys: the same grouping rule (`_entry_session`, else the pending
    # key), so the `gone` set is identical, without the transcript walk. The
    # keys it is asked about have already survived collection — they were listed
    # a few lines above — so nothing `_collect` would have dropped can enter here.
    entries = _entries_for(listed)
    for key in listed:
        for entry in entries.get(key, ()):
            pending = tasks_store.pending_key(str(entry.get("id") or ""))
            if pending != key:
                gone.add(pending)
    # `drafts` rides alongside the rows: the same keys the watcher announced,
    # answered out of the drafts store with a version apiece, so a composer or a
    # New task card open on one of them learns that another window has written
    # it (`_draft_changes`, design-drafts-one-record.md §3). The announced keys
    # and not `listed`: a draft that has just been discarded is exactly the news
    # an editor needs and is by then no row at all.
    return {"generation": gen, "rows": rows, "gone": sorted(gone),
            "drafts": _draft_changes(keys)}


# `project` rides along for the sidebar's Current apps section (D487): the
# section's membership is its own store now (current_apps.py) but the running
# dot on a row still reads the pulse — which task is under which app is a
# listing fact, and a second GET /api/tasks poll from the sidebar is the
# double-poll the pulse store exists to prevent.
#
# `task_id`, `title`, `target` and `session_id` ride along for the SAME reason,
# one surface later (2026-09-03): the Notifications section draws a row per task
# that is waiting on an answer, and such a row has to print which task
# ("TASK-097") and what it is about (the title), then open the conversation —
# which is `tasks-lib.taskHref`'s pair of `session_id` and `target`. Four short
# strings on a row that is already being built is cheaper by every measure than
# the second /api/tasks poll the alternative would need, and this endpoint is
# still the compact one: it carries no entries, no messages and no description,
# which is where a task listing's weight actually is.
_PULSE_FIELDS = (
    "key", "status", "unread", "last_active", "project",
    "task_id", "title", "target", "session_id",
    # The desk's change detector (shell/CurrentAppsSection `pulseSignal`): the
    # sidebar refetches the projects table when a task's `happened_at` moves,
    # since that is exactly when `current_apps.observe` can have flipped a
    # row's unread. `last_active` cannot stand in — a recurring task whose run
    # finished early keeps its due time there and the digest would not move.
    "happened_at",
    # The Tasks page paints these rows before its own listing answers
    # (shell/tasks-lib provisionalTasks), and the Board's Upcoming lane sorts by
    # the next run: without it a provisional card sat at the bottom of the lane
    # and jumped into place when the listing landed. One float per row — and
    # the entry it belongs to, because the client reads the time only when it
    # can also name the entry (shell/tasks-lib namedNextRun): a time with no
    # entry is one nobody can fire, and is not sorted by.
    "next_run",
    "next_run_entry",
    "next_run_repeats",
    # ...and where the row stands in its folder's line, for the same surface and
    # the same reason: the sidebar's Current apps section says "n running, n
    # queued", and a queued row that could not say whether it runs next would
    # make the count the only thing it could print. Three short fields — a
    # number, a task id and a flag — on rows that are already being built.
    # `queue_key` and `queue_ahead_title` are deliberately NOT here: the folder
    # is what the Tasks page groups by and the title is a whole string per row,
    # and the sidebar draws neither.
    "queue_position",
    "queue_ahead",
    "queue_priority",
    # …and the session that name opens, because the sidebar's rows are links
    # too: a Notifications row already carries `session_id` and `target` for its
    # OWN thread (`taskHref`), and the row in front is the other thread a queued
    # row can send a reader to. One short string. The target is not here for the
    # same reason `queue_key` is not — the pulse carries the queued row's own
    # `target`, and a folder-key-length path per row for a link the sidebar does
    # not yet draw is weight this endpoint exists to avoid.
    "queue_ahead_session",
    # How many messages are waiting, for the sidebar's "n queued" line: the
    # count of ROWS is what that number is today, and a row that could not say
    # how much work it is holding made a task with three queued sends look like
    # one. A single integer on a row that is already being built.
    "queue_waiting",
)


@router.get("/api/tasks/pulse")
def api_tasks_pulse():
    """The compact task facts used by the global sidebar's status pulse.

    NO DRAFTS. The sidebar's dot, its unread count and its Notifications
    section are about work that is happening; a form somebody has not finished
    is not news, and design.md puts task drafts in the List and the Board only
    — never the Cards wall, never the Calendar, and by the same reasoning never
    here (Akshil, 2026-09-11)."""
    return {
        "tasks": [
            {field: row[field] for field in _PULSE_FIELDS}
            for row in _task_rows()
            if row.get("kind") != "draft"
        ]
    }


class RunningPatch(BaseModel):
    session_id: str
    # The client's `Date.now()` at send (`run-controller.ts` `noteTurnRunning`).
    # Optional so an older client, or a direct call, still works — see
    # `tasks_watch.mark_running`'s stale-turn check, which only runs when this
    # is present.
    turn: float | None = None
    # THE WORDS THAT WERE SENT, and the file they were sent about. Both
    # optional and both defaulting to "" rather than None: a caller that only
    # wants the liveness floor sends neither and gets exactly the mark this
    # endpoint has always made. With them, the mark is enough to BE a row — the
    # message the listing shows, and the project it belongs to — for the
    # seconds before anything reaches disk. See `tasks_watch.mark_running`.
    text: str = ""
    file: str = ""


@router.post("/api/tasks/running")
def api_task_running(patch: RunningPatch):
    """A turn just started on this session — said by the page that sent it.

    THE ONE FACT NO FILE CARRIES IN TIME. A chat sent from this app runs
    `claude -p` through `/api/run`, which means the turn begins in another
    process entirely: this server has no route it could hang the news on, and
    the CLI's own registry row lands two to four seconds later. So the sender
    says it, once, at the moment it sends — `run-controller.ts`, beside the
    `announceTasksChanged` it already fires on both turn boundaries.

    A FLOOR WITH A FUSE, not a status (tasks_watch.mark_running): it expires by
    itself, and a registry that says `busy` replaces it the moment it appears.
    Nothing here can pin a row open — the worst a wrong or malicious call can do
    is spin one ring for fifteen seconds.

    A session id with no task row yet is not an error: a brand-new chat's
    transcript may not exist when its first turn starts, and the mark is simply
    waiting for it. Hence no 404 and no lookup — this endpoint does not read the
    listing at all.

    `turn` lets `tasks_watch.mark_running` recognize a running POST that lost
    the race to its own turn's `/api/tasks/idle` call (two independent
    fetches; nothing here orders their arrival) and drop it, rather than
    reopening a row a later idle call already closed (bugbot #1163).

    `text` and `file` are what the sender SAID and what it said it about, and
    they turn this from a floor under a row into the row itself: a brand-new
    chat has no transcript, no entry and no session on disk for its first
    seconds, so without them the send is a task nothing can list. With them the
    listing has a placeholder — keyed by the session the turn runs in, which is
    what `POST /api/run` hands the page back — that the transcript then simply
    becomes. Both are optional and neither can pin anything: they live and die
    with the mark, and the worst a wrong call does is show one wrong line for
    fifteen seconds in a row that then disappears.
    """
    session_id = patch.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="missing session_id")
    tasks_watch.mark_running(session_id, turn=patch.turn,
                             text=patch.text.strip(), file=patch.file.strip())
    return {"ok": True, "session_id": session_id}


class IdlePatch(BaseModel):
    session_id: str
    # The other half of `RunningPatch.turn` — see `tasks_watch.mark_idle`.
    turn: float | None = None


@router.post("/api/tasks/idle")
def api_task_idle(patch: IdlePatch):
    """A turn just ENDED on this session — said by the page that sent it.

    The other half of `/api/tasks/running`, and for the same reason: the CLI
    running out of process means this server learns a turn is OVER from a
    registry row disappearing (up to a tick late) or from the mark's own
    fifteen-second fuse — both far slower than the page that watched the reply
    arrive. `run-controller.ts` calls this at every turn boundary the poll
    loop's own `finally` sees (a final result, a stop, an error), the same
    place `noteChatActivity` already fires from.

    Retires the send's mark at once (`tasks_watch.mark_idle`) — a FAST turn no
    longer has to sit in a running ring for whatever was left of the mark's
    window. Best-effort like its counterpart: a call that never arrives (a
    closed tab) leaves the registry-corroborated stand-down and the TTL as the
    fallback, so nothing here can pin a row running or wrongly mark one done —
    the worst a wrong or malicious call does is retire a mark early, and the
    registry/tail reading underneath is what a listing shows once it is gone.

    `turn` is kept as the floor a later, out-of-order `mark_running` for this
    same turn is measured against (`tasks_watch.mark_running`).
    """
    session_id = patch.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="missing session_id")
    tasks_watch.mark_idle(session_id, turn=patch.turn)
    return {"ok": True, "session_id": session_id}


def _thread(task: dict, read: dict, now: float) -> list[dict]:
    """One task's whole thread, oldest first, ids and unread flags set."""
    live, _active, _marked = _live(task["path"], now, task["session_id"])
    prompts = _full_prompts(task["path"]) if task["path"] else []
    messages = _merge(prompts, task["entries"])
    # The live send is part of the thread here for the same reason it is part
    # of the listing row: "Show more" on a row that reads "running, on these
    # words" must show those words, not an empty thread until the transcript
    # lands (bugbot). Same fold, same dedupe.
    if task.get("sent") and _fold_sent_mark(messages, task["sent"]):
        # Numbered like the listing numbers it — the next id after everything
        # on disk — so "mark this one read" and the unread recount can name it
        # (bugbot). It is the newest by construction: `_merge` sorted the rest
        # ascending and the mark's `at` is the moment of the send.
        messages[-1]["message_id"] = tasks_store.format_message_id(len(messages))
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
    # THE BADGE IS A LISTED FACT, so the other windows have to be told. `unread`
    # rides on every row (`_unread_count`) and feeds the sidebar's own dot
    # (`/api/tasks/pulse`), and this was the one mutation in this router that
    # changed a row without ringing: reading a message on the Tasks page left a
    # second window, and the rail, counting it for up to a full poll. Every
    # other verb here already announces itself — archive, unarchive, delete,
    # erase — and this is the same kind of write.
    tasks_watch.notify({key})

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
        # Same ring as the per-message branch above, and only when something
        # actually moved: a whole-task mark on a task with nothing unread is a
        # no-op on disk and must not cost every watcher a redraw.
        tasks_watch.notify({key})
    return {"ok": True, "unread": sum(1 for m in messages if m["unread"])}


# ------------------------------------------------------------------ archiving
# Archiving is the only filing decision a person makes about a task, and it is
# ONE gesture with two halves — which is why it is a verb here and not a triage
# write from the client:
#
#   * the SESSION is filed (triage.json `archived`, the same record the Inbox
#     writes and reads), so the transcript keeps its place and its notes;
#   * the WORK IS CALLED OFF. A task with a run booked for tomorrow that is
#     "archived" but still fires is not archived at all — it is a card that
#     re-appears in Upcoming on its own, which is the one thing filing something
#     away must never do. So every pending message is cancelled, and so is every
#     recurring RULE behind one, because a rule that keeps materialising
#     occurrences is a rule that keeps un-archiving the task.
#
# WHAT IS NOT TOUCHED is a run that is happening. `sending` is not cancellable
# (schedule.cancel refuses it, and rightly: the helper is away and the turn may
# have started) and neither is a live turn. Those keep going, the task keeps
# reading In Progress while they do, and it settles into Archive by itself when
# they end — see `_message_archived`. Nothing has to come back and finish the
# job; the derivation simply answers differently once nothing is running.
#
# Deleting exists now (Akshil, 2026-08-19) and D306 still holds, because the
# two were never actually in tension: what the user asks to delete is the ROW —
# the task's place on the List, the Board, the calendar and the sidebar — and
# what D306 protects is the TRANSCRIPT. So delete is archive's first half
# (cancel the pending work, rules included) plus a TOMBSTONE in tasks_store
# where archive writes a triage record; the transcript stays on disk, reachable
# through Claude Code itself, and new activity in the conversation revives the
# row (`_deleted`) rather than running behind a hidden task. A task that never
# ran disappears the moment its work is cancelled anyway — `_is_task`'s rule,
# older than either verb — and the tombstone is what extends that to tasks
# that HAVE a session to keep.


class ArchivePatch(BaseModel):
    key: str


@router.post("/api/tasks/archive")
def api_task_archive(patch: ArchivePatch):
    """File one task away: cancel its pending work, archive its session.

    Answers what it actually did — how many messages were called off, and
    whether the session was filed — rather than a bare ok, because the two
    halves can legitimately come apart (a task with no session id has only the
    first, a pure-chat task has only the second) and the client's note line is
    the place a person finds out which.
    """
    key = patch.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="missing task key")
    task = _collect().get(key)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task with key {key!r}")
    cancelled, filed = archive_task(task)
    # THE UNSENT TEXT IS NOT TOUCHED (Akshil, 2026-09-15; design.md, PR C:
    # "Archive/unarchive touch NO drafts").
    #
    # This route used to `delete_chat` the session's draft, on the reading that
    # filing a task away says you are not coming back. That reading belongs to
    # DELETE, not to archive: archive is the reversible verb — its whole
    # affordance is the Unarchive button beside it — and a reversible gesture
    # that destroys text is a gesture nobody can undo. An archived task's drafts
    # (the chat's and any form bound to it) simply hide with the row and are
    # there again the moment it comes back, which is what every OTHER fact about
    # an archived task already does. Delete and erase, which are the verbs that
    # mean it, drop both halves — see `api_task_delete` and `api_task_erase`.
    tasks_watch.notify({key})
    return {"ok": True, "key": key, "cancelled": cancelled, "filed": filed}


def archive_task(task: dict) -> tuple[int, bool]:
    """The archive gesture on one collected task: how many messages were
    called off, and whether a session was filed. Shared with the Current apps
    router (server/routers/current_apps.py), where removing an app archives
    every task under it."""
    cancelled = 0
    # The rules FIRST: cancelling a template also cancels the occurrence it has
    # already materialised, so doing it the other way round would cancel one
    # occurrence and let the rule mint the next.
    for template_id in _rules_behind(task["entries"]):
        if schedule.cancel(template_id) is not None:
            cancelled += 1
    for entry in task["entries"]:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        entry_id = str(entry.get("id") or "")
        if entry_id and schedule.cancel(entry_id) is not None:
            cancelled += 1

    session_id = task["session_id"]
    if session_id:
        sessions.write_triage(session_id, _FILED)
    return cancelled, bool(session_id)


class UnarchivePatch(BaseModel):
    key: str


@router.post("/api/tasks/unarchive")
def api_task_unarchive(patch: UnarchivePatch):
    """Take the filing back: drop the archive record, nothing else.

    THE MOVE HAS ONE MEANING AND NO DESTINATION. Dragging a card out of the
    Archive lane does not say which lane it should land in — the user drops it
    somewhere because that is how a card leaves a lane, and where it goes is
    DERIVED (`_status`) exactly as it is for every other task on the board. So
    this verb takes a key and no status, and the lane the user happened to drop
    on is not sent, not read and not honoured. `status` in the answer is where
    the task actually landed, which is the one thing the client cannot work out
    for itself before its next poll — and it may well not be the lane under the
    cursor. That is correct: the board shows what the work is doing.

    ONE HALF, unlike archiving's two. Archiving cancels the pending work AND
    files the session; this only un-files. The cancelled runs stay cancelled —
    a booked run that came back to life because somebody unarchived a card
    would be a message firing that nobody asked for twice, and "put this back
    on the board" is not consent to send it. Ask for the run again (or say
    something in the conversation) if that is what is wanted.

    NO RUN IS EVER STARTED HERE, which is why the drop onto In Progress is this
    same call and not a run: In Progress is Claude's output, never a verdict a
    reader hands down, so a card dropped there simply comes back and lands
    wherever its thread puts it — Done, Failed or In Progress if a turn really
    is live.

    `clear_triage` keeps the rest of the record — a note, a tag, a read mark on
    that session is somebody else's data and outlives the status the Board put
    on it. Same function the revival rule calls (`_revived`), so the gesture and
    the automatic way out of Archive drop the filing identically.
    """
    key = patch.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="missing task key")
    task = _collect().get(key)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task with key {key!r}")

    session_id = task["session_id"]
    unfiled = bool(session_id) and sessions.clear_triage(session_id)
    # Where it landed, read the same way the listing reads it — one row built
    # from the same helpers, AFTER the filing is gone, so the answer is the lane
    # the very next poll will draw rather than a guess about it.
    _place(task)
    row = _row(task, "", sessions._load_state("triage.json"),
               tasks_store.read_state(), time.time(),
               schedule.busy_sessions(schedule.list_entries()), [])
    tasks_watch.notify({key})
    return {"ok": True, "key": key, "unfiled": unfiled,
            "status": row["status"]}


class DeletePatch(BaseModel):
    key: str


@router.post("/api/tasks/delete")
def api_task_delete(patch: DeletePatch):
    """Take the row away for good: cancel its pending work, tombstone its key.

    Archive's first half — the rules first, then every pending entry, for the
    reason documented there — and then `tasks_store.mark_deleted` where archive
    writes triage: the tombstone is what `_collect` reads, so the task is
    absent from the listing, the pulse, the calendar and the full thread in one
    decision, exactly the way `_is_task` already drops shells.

    THE RULES, FOUND BY WHERE THEIR NEXT RUN WOULD LAND (bugbot, 2026-08-19,
    twice — once in each direction). Archive's `_rules_behind` reads template
    ids off PENDING occurrences only — right for archive, whose cancelled runs
    must not reach past the task, but blind to a live rule BETWEEN occurrences:
    the materialiser mints lazily, so such a rule has no pending row, survives
    the delete, and its next mint carries a `created` stamp newer than the
    tombstone — the revival rule's front door (`_deleted`). The over-correction
    was as wrong the other way: harvesting `template_id` off every entry
    whatever its state cancelled a `new_task_each_run` SERIES because one of
    its spent runs happened to be the task being deleted — a series whose
    future runs mint into fresh sessions and were never going to touch this
    row. So `_every_rule_behind` asks the one question that matters: would this
    rule's next run land back ON THIS TASK? Those rules die with the row;
    every other rule survives it, and only this task's own pending entries are
    withdrawn.

    REFUSED WHILE THE TASK IS RUNNING (409). A live turn cannot be cancelled
    (`sending` is refused by schedule.cancel, a running job is the registry's
    to stop), so deleting now would hide work that is still happening — a run
    finishing into an invisible task is the exact failure the revival rule
    exists to prevent, and the honest answer is "stop it first". The status
    asked is the same derived row every view paints (`_row`), so this endpoint
    cannot disagree with the pill the user is looking at.

    WHAT IS AND IS NOT ERASED, in the answer as in fact: pending work is
    cancelled (`cancelled` counts it), the row is gone everywhere, and the
    TRANSCRIPT IS NOT TOUCHED (D306) — `erased_transcript` is always False and
    is in the payload so the client never has to guess. The task's number is
    likewise never reallocated (task_ids.json's "max seen plus one" rule), so
    a deleted TASK-007 does not quietly become somebody else's name.
    """
    key = patch.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="missing task key")
    task = _collect().get(key)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task with key {key!r}")

    _place(task)
    row = _row(task, "", sessions._load_state("triage.json"),
               tasks_store.read_state(), time.time(),
               schedule.busy_sessions(schedule.list_entries()), [])
    # BOTH RUNNING WORDS. This row is built without the parked index (one
    # request about one task does not pay for a scan of the runs tree), so a run
    # waiting on a permission card reads `in_progress` here and the first word
    # catches it. The second is the guard against that stopping being true: the
    # question this asks is "is a run in flight", and `needs_attention` is a run
    # in flight — a delete that slipped through would tombstone the row out from
    # under a live process.
    if row["status"] in ("in_progress", "needs_attention"):
        raise HTTPException(
            status_code=409,
            detail="that task is running — stop the run first, then delete")

    cancelled = 0
    for template_id in _every_rule_behind(key):
        if schedule.cancel(template_id) is not None:
            cancelled += 1
    for entry in task["entries"]:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        entry_id = str(entry.get("id") or "")
        if entry_id and schedule.cancel(entry_id) is not None:
            cancelled += 1

    tasks_store.mark_deleted(key)
    # BOTH HALVES OF THIS SESSION'S UNSENT TEXT GO WITH THE ROW (design.md,
    # PR C). The row is the only place either could ever have been drawn — the
    # composer's own draft as the `✎ Draft` chip, a bound New task form as the
    # same chip off `_bound_chips` — so a draft left behind is bytes nothing can
    # show and nobody can reach. Unlike archive above, this verb is the one that
    # means it.
    #
    # The bound form's row key is announced with the task's: it was never a row
    # of its own, but a page holding a stale one (from before the binding, or
    # from an older build) has to be told to drop it.
    dropped = drafts.delete_bound(task["session_id"])
    drafts.delete_chat(task["session_id"])
    tasks_watch.notify({key} | set(dropped))
    return {"ok": True, "key": key, "cancelled": cancelled,
            "erased_transcript": False}


class ErasePatch(BaseModel):
    key: str


@router.post("/api/tasks/erase")
def api_task_erase(patch: ErasePatch):
    """Delete the task AND the Claude session behind it — through and through.

    THIS DELIBERATELY GOES FURTHER THAN `/api/tasks/delete`, whose whole promise
    is the opposite one: that verb cancels the work and tombstones the key while
    the TRANSCRIPT IS NOT TOUCHED (D306), so the conversation is still there to
    open and later activity in it revives the row. That is right for the
    Calendar's soft delete and wrong for what the user asked for (Akshil,
    2026-09-07): deleting a task "through and through: deleting the claude
    session itself". Both verbs therefore exist, the softer one unchanged, and
    this one is D307 — the new decision that a task's delete may destroy the
    session, because the user said so about this task, once, in a modal that
    says it cannot be undone.

    THE SAME FIRST HALVES AS DELETE, for the reasons documented there and not
    repeated here: the 409 while a run is in flight (a live turn cannot be
    cancelled, and erasing the transcript under a writing process is worse than
    hiding it), then `_every_rule_behind` before the task's own pending entries
    so a rule cannot mint one back.

    WHAT COMES OFF THE DISK, and why each is plural. `<session_id>.jsonl` is
    globbed across EVERY project dir rather than read off the row's own path,
    because copy-on-resume can leave the same session id under two encoded cwds
    and erasing one of them would leave the conversation readable — and the row
    revivable — from the other. Beside each transcript, the sibling DIRECTORY
    `<session_id>/` holds the subagent sidecars Claude Code writes for that
    session; it is the same conversation and goes with it (`shutil.rmtree`).
    Everything is realpath-checked against PROJECTS_DIR and anything landing
    outside is SKIPPED, not raised on: this is the one endpoint that removes
    trees, so a symlinked project dir must not be able to aim it at ~.

    WHAT COMES OUT OF STATE: the triage record WHOLE (`forget_triage`, not
    `clear_triage` — there is no session left for a note or a tag to be about),
    and the read marks (`tasks_store.forget_session`), and BOTH shapes of this
    session's unsent text — its chat draft and any task draft bound to it
    (`drafts.delete_bound`, 2026-09-15) — with the bound draft's own key
    announced beside this task's, so a page holding a row under it drops it.
    The task's NUMBER is the
    one thing kept: allocation is "max seen plus one" read straight off
    task_ids.json, so the mapping stays as a reservation and a reused TASK-007
    can never point at somebody else's work. See `forget_session`.

    AND THE TOMBSTONE ANYWAY (`mark_deleted`). Nothing should be left to
    revive, but a straggler — a scheduled entry that lands between the cancel
    and the erase, a watcher mid-lap holding the file it just read — must not
    be able to put the row back for a poll. The tombstone costs a few bytes and
    closes that window; the same reasoning delete's docstring gives for it.
    """
    key = patch.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="missing task key")
    task = _collect().get(key)
    if task is None:
        raise HTTPException(status_code=404, detail=f"no task with key {key!r}")

    _place(task)
    row = _row(task, "", sessions._load_state("triage.json"),
               tasks_store.read_state(), time.time(),
               schedule.busy_sessions(schedule.list_entries()), [])
    # Both running words, for the reason api_task_delete's guard documents —
    # and one more here: what is refused is not a hidden row but a deleted
    # file, under a process still writing to it.
    if row["status"] in ("in_progress", "needs_attention"):
        raise HTTPException(
            status_code=409,
            detail="that task is running — stop the run first, then delete")

    cancelled = 0
    for template_id in _every_rule_behind(key):
        if schedule.cancel(template_id) is not None:
            cancelled += 1
    for entry in task["entries"]:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        entry_id = str(entry.get("id") or "")
        if entry_id and schedule.cancel(entry_id) is not None:
            cancelled += 1

    removed, erased, failed, refused = 0, False, 0, 0
    # The draft rows this erase takes away, announced beside the task's own key
    # at the end. Empty for a task with no session and for the ordinary erase
    # with nothing bound to it, which is nearly all of them.
    dropped: list[str] = []
    session_id = task["session_id"]
    if session_id:
        removed, erased, failed, refused = _erase_session_files(session_id, task["path"])
        # A FILE THAT WOULD NOT GO IS NOT A DELETED TASK (bugbot, PR #1049).
        # Nothing about the session is forgotten and the key is NOT tombstoned:
        # the transcript is still on disk, so the row must stay on the page
        # saying so, rather than vanish over a conversation that is still
        # there and come back the next time anything touches it. The work
        # already cancelled stays cancelled — that half did happen.
        # A REFUSAL IS THE SAME ANSWER (review, PR #1049): a candidate that was
        # not ours to remove was not removed, and reporting a delete over it
        # would tombstone a row whose transcript is still there.
        if failed or refused:
            tasks_watch.notify({key})
            left = failed + refused
            raise HTTPException(
                status_code=500,
                detail=(f"could not remove {left} file"
                        f"{'' if left == 1 else 's'} — the task is still listed; "
                        "see the server log"))
        sessions.forget_triage(session_id)
        tasks_store.forget_session(session_id)
        # Nothing about this session survives an erase, and unsent text is
        # emphatically something about it — there is no conversation left for
        # it to be typed into.
        drafts.delete_chat(session_id)
        # A TASK DRAFT BOUND TO IT GOES TOO (Akshil, 2026-09-15; design.md,
        # PR C: "Delete + erase drop BOTH"). It used to be UNBOUND instead —
        # words kept, binding cut, the form coming back as an ordinary
        # `draft:<id>` row in its own folder — on the reading that a form
        # somebody is still filling in outlives the conversation it was aimed
        # at. What that actually produced was half a message addressed to a
        # thread this gesture had just destroyed, reappearing in a lane the
        # reader had cleared, with a Schedule button that could no longer do
        # what it said. Both shapes of "this session's unsent text" now answer
        # to the same two verbs, which is the one thing delete and erase could
        # never previously agree on.
        #
        # AND THOSE ROWS ARE NEWS (bugbot, PR #1126): a page holding one — from
        # before the binding, or from an older build — has to be told to drop
        # it, so the keys the store hands back are announced with the task's.
        dropped = drafts.delete_bound(session_id)

    tasks_store.mark_deleted(key)
    tasks_watch.notify({key} | set(dropped))
    return {"ok": True, "key": key, "cancelled": cancelled,
            "erased_transcript": erased, "removed": removed}


# What a Claude Code session id looks like on disk — a uuid, in practice — and
# the shape the erase glob will accept at all. Not a dot, not `..`, no
# separator: `glob.escape` keeps a name from being a PATTERN, but `..` is not a
# pattern, it is a path, and `PROJECTS_DIR/*/..` is the projects root itself.
_SESSION_ID_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _erase_session_files(session_id: str, path: str | None) -> tuple[int, bool, int, int]:
    """Take one session off the disk: (how many things went, did a transcript
    go, how many refused to go, how many were refused as not ours).

    Every copy of `<session_id>.jsonl` under any project dir, the sibling
    `<session_id>/` sidecar dir beside each, and the row's own path when it
    names something the walk did not reach.

    WHAT IS OURS (review, PR #1049): a candidate must resolve to a leaf named
    exactly `<session_id>` or `<session_id>.jsonl` whose parent resolves to one
    of the project dirs — compared by REALPATH of the project dir, so a project
    dir that is itself a symlink out of the tree (somebody's real setup) still
    counts, while a leaf that is a symlink to anywhere else does not. Anything
    else is REFUSED and counted, and a refusal is a failure to the caller: an
    erase that skipped something is not an erase, and must not report one.
    A session id that is not a name (`..`, `.`, a separator) is refused before
    any path is built — `PROJECTS_DIR/*/..` is the projects root itself.
    """
    if not _SESSION_ID_SHAPE.match(session_id) or session_id in (".", ".."):
        logger.warning("erase: refusing session id %r", session_id)
        return 0, False, 0, 1
    root = os.path.realpath(sessions.PROJECTS_DIR)
    names = (session_id, session_id + ".jsonl")
    projects: dict[str, str] = {}  # realpath(project dir) -> as listed
    try:
        with os.scandir(sessions.PROJECTS_DIR) as it:
            for entry in it:
                if entry.is_dir():
                    projects[os.path.realpath(entry.path)] = entry.path
    except OSError:
        pass
    targets: list[str] = []
    for listed in projects.values():
        for name in names:
            candidate = os.path.join(listed, name)
            if os.path.lexists(candidate):
                targets.append(candidate)
    if path and os.path.lexists(path):
        targets.append(path)

    # SIDECARS FIRST, TRANSCRIPTS LAST, AND STOP AT THE FIRST REFUSAL (bugbot,
    # PR #1049): the transcript is what makes the row a task at all, so it must
    # be the last thing to go — a sidecar that will not be removed then leaves
    # the transcript in place, the row on the page and a retry possible, instead
    # of a vanished task with orphaned files beside where it was.
    targets.sort(key=lambda t: t.endswith(".jsonl"))
    removed, erased, failed, refused = 0, False, 0, 0
    seen: set[str] = set()
    for target in targets:
        resolved = os.path.realpath(target)
        if resolved in seen:
            continue
        seen.add(resolved)
        parent = os.path.dirname(resolved)
        if (resolved == root or parent not in projects
                or os.path.basename(resolved) not in names):
            logger.warning("erase: refusing %s — not a session file of %s under %s",
                           resolved, session_id, root)
            refused += 1
            break
        try:
            if os.path.isdir(resolved):
                shutil.rmtree(resolved)
            elif os.path.exists(resolved):
                os.remove(resolved)
                erased = erased or resolved.endswith(".jsonl")
            else:
                continue
        except OSError:
            logger.warning("erase: could not remove %s", resolved, exc_info=True)
            failed += 1
            break
        removed += 1
    return removed, erased, failed, refused


def _every_rule_behind(key: str) -> list[str]:
    """Every recurring rule that could put this task back on the board, named
    once. DELETE's discovery, and a different question from `_rules_behind`'s.

    The tie that matters is WHERE THE RULE'S NEXT RUN LANDS, not which task its
    old runs ended up in — so the predicate is the rule's own session resolving
    to this task's key, read with `_entry_session` exactly as `_collect` files
    entries. That finds a same-session rule even between occurrences, when the
    materialiser has minted nothing and the rule leaves no pending row to read
    a `template_id` off (its next mint would land back on this key, `created`
    newer than the tombstone, and revive the row — `_deleted`).

    What it deliberately does NOT do is follow `template_id` off the task's own
    entries: a `new_task_each_run` series mints every future run into a FRESH
    session, so one of its spent runs being this task ties the series to the
    task's PAST, not its future — deleting that one run's row must not kill the
    live series, nor the pending occurrences that belong to other tasks. Those
    rules keep minting; only this task's own rows go.

    `schedule.cancel` cancels a recurring template together with its pending
    occurrence, so a matched rule dies whole."""
    rules: list[str] = []
    for entry in schedule.list_entries():
        if entry.get("state") != schedule.RECURRING:
            continue
        entry_id = str(entry.get("id") or "")
        if entry_id and entry_id not in rules and _entry_session(entry) == key:
            rules.append(entry_id)
    return rules


def _rules_behind(entries: list[dict]) -> list[str]:
    """The recurring templates this task's still-pending occurrences came from,
    each named once. Only PENDING occurrences count: a template whose runs are
    all spent is not going to produce another one on its own, and cancelling it
    would be this verb reaching past the task it was asked about."""
    rules: list[str] = []
    for entry in entries:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        template_id = str(entry.get("template_id") or "")
        if template_id and template_id not in rules:
            rules.append(template_id)
    return rules


# --------------------------------------------------------------- the queue verbs
# ONE TASK IN PROGRESS PER FOLDER, from the client's side. Three POSTs, and they
# exist because the client cannot derive any of them: who holds a folder is a
# fact the queue manager keeps (`queue_manager.owner`), and a page guessing it
# is how two views end up disagreeing about one task.
#
# ALL THREE CARRY THE D3 X-FUSED HEADER, unlike every other write in this file.
# The reads here are unguarded and so is `POST /api/tasks/read` — it moves a
# badge, it does not run code — but these START WORK: admit spawns a turn or
# stores a message that will spawn one, skip moves a message to the head of the
# line it is about to go out of, and decide releases a parked agent. That is the
# exact pair (schedule / unschedule) the header guard covers next door in
# routers/schedule.py, and a blind cross-origin POST must not reach any of them.
#
# THE BODIES ARE PLAIN DICTS, not pydantic models like the archiving verbs above,
# and for the reason `api_schedule_shot` documents: a required model field is
# validated by FastAPI BEFORE any of our code runs, so a malformed body would be
# answered with an unguarded 422 rather than with the guard's 403. A guarded
# endpoint reads its own body.
#
# AND ALL THREE NOW HAND THE DECISION TO ONE PLACE (PR 2, 2026-09-17). The
# manager below is the single record of who owns a folder and who is behind
# them; these endpoints state an EVENT ("this chat wants to send", "this one
# next", "this card is answered") and read the answer back. Nothing here derives
# a line any more.


# ------------------------------------------------------------- wiring the manager
# `queue_manager` owns the index and none of the machinery: spawning a turn,
# replaying a decision and reading whether a session is running are all things
# this process can do and that module must not import (the scheduler, this
# router and the permission server all reach the manager, so an import edge the
# other way is a cycle — see `queue_manager.set_factory`). The five callables
# below are that machinery, and `_wire_manager` is the hook.


def _queue_spawn(folder: str, task_key: str) -> dict | None:
    """Start the turn one queued task is waiting for — the manager's `spawn`.

    `{"run_id", "session_id"}` for a message that went, None when the task names
    no work any more (the entry was cancelled, or another dispatcher got it),
    and `schedule.SpawnBusy` for a conversation that cannot take it YET — the
    manager leaves a busy item at the head of its line rather than dropping it.

    ONE SLOT PER TASK, SO ONE MESSAGE PER SPAWN: whichever of the task's waiting
    messages is OLDEST. Message order in a conversation is the conversation, and
    a line that held a slot per message would run the second thing typed first
    whenever the first one was held.

    ASKED OF THE STORE AND NOT OF THE KEY, even for a `pending:<entry-id>` key
    that names an entry outright. The named entry is usually the one that goes —
    it is the task's own oldest by construction — but it is not always THERE: a
    queued chat whose first message the user cancelled still has the second one
    behind it, filed under the leader's key, and dispatching the key's own id
    would answer None and drop a message the user is still owed
    (test_a_cancelled_leader_does_not_orphan_its_follower).

    `folder` is not read: the entry carries its own target, and a key that named
    a different tree than the line it stands in would be the index disagreeing
    with the store rather than something to reconcile here."""
    entry_id = _oldest_due_entry(task_key)
    if not entry_id:
        return None
    started = schedule.dispatch_entry(entry_id)
    session_id = str((started or {}).get("session_id") or "")
    if session_id:
        # A TURN JUST BEGAN ON THIS SESSION, and the manager is the one caller
        # that knows it (Akshil's list audit, 2026-09-18). The watcher may still
        # hold this session's `turn_ended` stamp from the previous turn; only a
        # newer mark or a strictly newer registry mtime clears it, and a
        # hand-off inside the same second — or a registry row that never left
        # `busy` — clears neither, so the row read "done" while the next turn
        # ran. The page marks its own sends this way; the queue marks the ones
        # it dispatches.
        try:
            tasks_watch.mark_running(session_id)
        except Exception:  # noqa: BLE001 — a missed mark is the old behaviour
            logger.debug("could not mark %s running", session_id, exc_info=True)
    return started


def _oldest_due_entry(task_key: str) -> str:
    """The id of the earliest message this TASK has waiting to go right now, or
    "" for a task with nothing due.

    `_due_pending`'s three conditions read for one task (pending, due by
    `_queue_at` so a Run now counts, and a folder to wait on), filed by the
    listing's own rule so the key asked about is the key the row was built
    under (`_entry_key`)."""
    entries = schedule.list_entries()
    by_id = _by_entry_id(entries)
    now = time.time()
    best: tuple[float, str] | None = None
    for entry in entries:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        due = _queue_at(entry)
        if not due or due > now:
            continue
        if _entry_key(entry, by_id) != task_key:
            continue
        entry_id = str(entry.get("id") or "")
        if entry_id and (best is None or due < best[0]):
            best = (due, entry_id)
    return best[1] if best else ""


def _queue_deliver(answer: dict) -> None:
    """Replay one held card decision — the manager's `deliver`.

    WHAT WAS HELD IS THE ARGUMENTS, NOT A VERDICT (see `api_queue_decide`), so
    this is one call: `agent._decide` applies every rule it has — the scope
    downgrade, the mode switch, the answer validation, the first-writer-wins
    latch — against the run AS IT NOW IS, which is the whole reason the raw
    arguments were what got stored."""
    agent = project_queue.agent_module()
    raw = (answer or {}).get("raw")
    if agent is None or not isinstance(raw, dict) or not raw:
        logger.debug("queue: nothing to deliver for %r", answer)
        return
    agent._decide(**raw)


def _queue_identity(task) -> tuple[str, str]:
    """`(session, run id)` to ask the status sync about, from either a bare task
    key or the manager's owner/item RECORD.

    A RECORD AND NOT A KEY, because half the index knows a conversation by a
    name the registry has never heard: a `pending:<entry id>` owner is filed
    under the entry that made it, a brand-new chat under the run it started, and
    a status read that could only ask about the label answered "dead" for a turn
    that was very much alive (`queue_manager.reconcile`). The record carries all
    three names; the key form is what a caller with only a label has, and the
    label is tried as BOTH — a session id and a run id are both just directory
    names from here, and asking the wrong one costs a miss, never a wrong yes.

    An `admit:` placeholder names no process at all (it exists precisely because
    there is none yet), so it is never asked about."""
    if isinstance(task, dict):
        label = str(task.get("task") or "")
        session = str(task.get("session_id") or "")
        run_id = str(task.get("run_id") or "")
    else:
        label = str(task or "")
        session = run_id = ""
    if label.startswith(queue_manager.PLACEHOLDER_PREFIX):
        return session, run_id
    if label and not tasks_store.pending_entry(label):
        session = session or label
        run_id = run_id or label
    return session, run_id


def _run_dir_alive(run_id: str) -> bool:
    """Does this run id name a run dir whose process is still up?

    THE TIE-BREAKER THE REGISTRY CANNOT GIVE. A turn that started a second ago
    has no mark and no registry row, but it does have a run dir with a live pid
    in it — and `reconcile` asking "is this owner still there" has to be told
    yes, or the index hands the folder to the next task while the first one is
    still opening its transcript."""
    if not run_id or project_queue.bad_id(run_id):
        return False
    agent = project_queue.agent_module()
    if agent is None:
        return False
    run_dir = os.path.join(str(getattr(agent, "RUNS", "")), run_id)
    if not os.path.isdir(run_dir):
        return False
    return project_queue.run_alive(agent, run_dir)


def _queue_running(task) -> bool:
    """Is a turn open in this task right now — the manager's `running`.

    THE SAME STATUS SYNC THE LISTING READS, and deliberately only its registry
    half: `tasks_watch.is_marked_running` (the sender's own word that a turn
    just started, which covers the seconds before the CLI registers) over
    `live_from_registry` (the process's own status). The transcript-tail
    fallback is not asked — the manager consults this as a TIE-BREAKER on load
    and restart (design.md), where a tail that reads warm because a turn ended
    forty seconds ago would hold a folder for nobody.

    …and the run dir last (`_run_dir_alive`), which is the only channel that
    answers during the seconds between a spawn and its registration."""
    session, run_id = _queue_identity(task)
    if session:
        if tasks_watch.is_marked_running(session):
            return True
        verdict = tasks_watch.live_from_registry(session)
        if verdict and verdict[0]:
            return True
    return _run_dir_alive(run_id)


def _queue_blocked(task) -> bool:
    """Is this task parked on a card nobody has answered — the manager's
    `blocked`.

    The parked scan, read for one task: the bounded walk of the runs tree
    (`project_queue.scan_runs`) and the unanswered-request rule
    (`run_waiting`), which is the same pair the listing's `parked` column
    reads. A parked run holds no folder, which is why the manager wants it.

    MATCHED BY RUN TOO, not only by session: the cards belong to the RUN, and an
    owner the index knows only by its run id (a chat that has not minted a
    session, a `pending:` message the pump started) would otherwise read as "not
    parked" the moment it put a card up — and get its folder taken away while a
    human was looking at the card."""
    session, run_id = _queue_identity(task)
    if not session and not run_id:
        return False
    agent = project_queue.agent_module()
    if agent is None:
        return False
    for run in project_queue.scan_runs(agent):
        if not ((session and session in (run.get("sessions") or ()))
                or (run_id and str(run.get("run_id") or "") == run_id)):
            continue
        if project_queue.run_waiting(agent, run):
            return True
    return False


def _queue_pending_due() -> list[tuple[str, str, str]]:
    """`(folder, task key, entry id)` for every pending message that is waiting
    to go right now — the manager's `pending_due`, and what `reconcile` rebuilds
    its lines from.

    `_due_pending`'s rule over the whole store instead of one task, keyed by the
    listing's filing rule so a rebuilt line names the rows the page is drawing.
    A message with no folder (`queue_key` answers "" for `$HOME`, the filesystem
    root, a relative path) is in no line at all — it queues behind nothing and
    the scheduler dispatches it straight."""
    if not project_queue.enabled():
        return []
    entries = schedule.list_entries()
    by_id = _by_entry_id(entries)
    now = time.time()
    out: list[tuple[str, str, str]] = []
    for entry in entries:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        due = _queue_at(entry)
        if not due or due > now:
            continue
        folder = project_queue.queue_key(str(entry.get("target") or ""))
        entry_id = str(entry.get("id") or "")
        if folder and entry_id:
            out.append((folder, _entry_key(entry, by_id), entry_id))
    return out


def _wire_manager() -> None:
    """Tell `queue_manager` how to build this process's manager.

    A FACTORY AND NOT AN INSTANCE, so importing this router costs nothing: the
    manager reads its index off disk and reconciles it against the scheduler's
    store on construction, and doing that at import time would make every
    `import fused_render.server` — including the ones a test does for one
    unrelated endpoint — touch the state dir. `queue_manager.get()` builds it on
    the first ask, which is the first time anything actually queues.

    Called at import, below. Idempotent: registering the same hook twice is one
    hook, and `reset_for_tests` is the seam a test uses to install its own
    manager in front of it."""
    queue_manager.set_factory(lambda: queue_manager.QueueManager(
        spawn=_queue_spawn,
        deliver=_queue_deliver,
        running=_queue_running,
        blocked=_queue_blocked,
        pending_due=_queue_pending_due,
        notify=tasks_watch.notify,
    ))


_wire_manager()


@router.post("/api/tasks/queue/admit")
def api_queue_admit(body: dict = Body(...),
                    x_fused: str | None = Header(default=None)):
    """May this chat send start a run right now — and if not, queue it.

    ASKED BEFORE THE SEND, NEVER AFTER. A composer that spawned first and queued
    second would have two runs in one folder for as long as the round trip takes,
    which is the single thing this feature exists to prevent. So the client asks,
    and gets one of two answers:

    * `{"run": true}` — go, exactly as before. The folder is free, or the thing
      holding it IS this session (a second message typed into a conversation
      that is already running is the inbox-absorb case the chat has always had,
      and gating it would be this feature refusing a send that touches nothing
      new). A short reservation is taken on the way out: between this answer and
      the CLI registering a session there is nothing on disk saying the folder is
      taken, and a second send landing in that window would be admitted into it.
    * `{"run": false, ...}` — the words are SAFE and nothing spawned. The message
      is stored as an ordinary pending entry due now, in the same store the
      calendar, the queue popover and cancel already work against, so there is no
      second kind of waiting message to keep in step with. The answer says where
      in the line it landed and who is in front, which is what the bubble's chip
      prints.

    **The flag off answers `{"run": true}` and stores nothing**, which is what
    lets the client ask unconditionally and still behave exactly as main does.

    **AND EITHER ANSWER RINGS THE LONG-POLL.** The row changes on both roads —
    `queued` for a stored message, `in_progress` for an admitted one, the latter
    off the reservation this endpoint takes (`_running_now`) — and a page
    waiting on `/api/tasks/changes` should not sit out its own timeout to hear
    about a send it just made.

    The entry is created through the schedule router's own `create_entry`, so a
    queued send carries the model, the effort, the permission mode and the
    attachments the user actually chose. A queued message that differed from the
    one that would have gone had the folder been free would make this a feature
    that changes work rather than one that delays it.

    **A CHAT NEVER OVERTAKES ITSELF.** A session that already has due work
    waiting in the scheduler — an earlier message of its own that queued — is
    answered `run: false` and this message queues behind it, EVEN IF THE FOLDER
    IS FREE. Two reasons, and the second is the load-bearing one: message order
    in a conversation is the conversation, so the second thing typed must not be
    the first thing sent; and a client that spawned a turn into a session the
    scheduler is about to claim for the earlier message would put two `--resume`
    processes on one transcript. So the order flows through one place, and the
    answer carries `behind_own: true` — the folder may be perfectly free and
    there may be nobody ahead to name, and "after your previous message" is what
    the chip says then.

    A `follow_of` body (a second message typed into a chat whose first is still
    queued) is the same rule read through the leader, and an id that names no
    entry is a 400 rather than a silent fresh task — the client is looking at a
    queue that has moved on, and the honest answer is what makes it refetch.

    **`run_id` IS THE NAME A CHAT HAS BEFORE IT HAS A SESSION.** A new chat's
    first message is admitted with `session_id: ""` because Claude Code has not
    minted one yet, and the run that message starts is anonymous for as long as
    nothing has polled it. So the second message — which by then DOES carry a
    session id — asked about a folder held by a process neither id could match,
    and was told `#1 in line · behind a run in this folder`: queued behind
    itself (Akshil, folder qa-folder-b, 2026-09-12). The client passes the run
    it started; the folder's owner carries the run it is; equal run ids are one
    conversation whatever the session says (`queue_manager.is_free`). The other
    half of the same fix learns the run's session from the live registry by pid,
    so the anonymous window is now seconds rather than minutes
    (`project_queue.run_sessions`).

    **`draft_key` IS THE DRAFT THIS SEND SPENT**, and it is how a queued chat
    keeps the name it already had. A session-less composer autosaves under
    `new:<file>` and the listing numbers that key, so the row the reader has been
    watching is TASK-057 before a word of it has gone anywhere. Queueing it used
    to mint a SECOND number for `pending:<entry-id>` and leave the spent key in
    `task_ids.json` for ever — the reader watched TASK-057 become TASK-058 on the
    send, and every later listing paid a scan of the runs tree for a draft
    nothing would settle (`_settle_new_chats`). The client names the key it is
    spending, exactly as the run it would otherwise have started names it
    (`agent._start`'s `meta.json draft_key`), and the entry inherits both the
    number and the delete (`schedule.spend_chat_draft`). Optional, and ignored
    when the folder was free: a send that RUNS spends its draft the way it always
    did, through the run it starts.

    **A WORDLESS SEND IS REFUSED ONLY WHERE IT WOULD HAVE TO BE STORED.** The
    composer lets a user send pictures with no words, and into a free folder
    that is an ordinary send this must not stand in the way of — so the message
    is not looked at until the answer is "queue it". At that point it has to
    become a scheduled entry, and `schedule.create` refuses one with no message
    whatever it is carrying ("message: cannot be empty" — pictures are not words
    and the row would have nothing to say). Mirrored here rather than routed
    around: answering `run: true` to dodge the store would put a second turn in
    a folder another task is holding, which is the one thing this endpoint
    exists to prevent. So it is a 400 with the store's own sentence, and the
    client keeps the words in the composer.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    message = body.get("message")
    message = message if isinstance(message, str) else ""
    session_id = str(body.get("session_id") or "")
    # The run this chat already has in flight, when it has one. A new chat's
    # first message is admitted with NO session (there is none yet), so the run
    # it starts is the only name the conversation has until Claude Code mints
    # one — and without it the second message queued behind the chat's own
    # process (Akshil, 2026-09-12). Validated like every other id that reaches
    # a path: a run id is a directory name under the runs tree.
    run_id = str(body.get("run_id") or "")
    if run_id and project_queue.bad_id(run_id):
        return _error("run_id: not a run id — no leading dot, no separator",
                      status=400)
    if not project_queue.enabled():
        # Nothing is read, nothing is resolved and nothing is stored: with the
        # flag off this endpoint is a constant, and the client's send path is
        # main's byte for byte.
        return {"run": True}

    resolved, refusal = schedule_api.resolve_target(body.get("project"),
                                                    "project")
    if refusal is not None:
        return refusal
    key = project_queue.queue_key(resolved)
    # The store, read once, and ONLY where this send has something to be behind:
    # a brand-new chat's first message names neither a session nor a leader and
    # pays nothing for either question.
    follow_of = str(body.get("follow_of") or "")
    by_id = _by_entry_id() if (session_id or follow_of) else {}
    if follow_of and by_id.get(follow_of) is None:
        return _error(f"follow_of: no scheduled message with id {follow_of!r}",
                      status=400)
    behind_own = _behind_own(session_id, follow_of, by_id)
    # THE TASK THIS SEND BELONGS TO, which is what the manager keys on — the
    # session, else the key its leader is filed under for a message typed behind
    # another, else the run this chat already has in flight. That last one is
    # `run_id`'s whole job here: a new chat's first message is admitted before
    # Claude Code has minted a session, so the run it started is the only name
    # the conversation has, and a second message that could not say "that owner
    # is me" queued behind itself (Akshil, 2026-09-12).
    manager = queue_manager.get()
    chat_key = _admit_key(session_id, follow_of, by_id) or run_id
    # THE NAME THIS SEND OWNS THE FOLDER UNDER, minted here when the send has
    # none of its own (`_owner_token`).
    owner_token = chat_key or _owner_token(body)
    # ONE DECISION, NOT A LOOK AND THEN A WRITE. `is_free` then `started` is two
    # acquisitions of the manager's lock with a gap in between, and two sends
    # into one free folder arriving on two request threads both heard "free" and
    # both spawned. `claim_for_send` is the check and the filing under one lock:
    # `ok` True means the folder is this task's from here, False that somebody
    # else got it and this message queues.
    #
    # Asked at all only once this chat has nothing of its own in the line: an
    # owner filed here would be a folder held for a send that is about to be
    # queued anyway.
    #
    # A folder ALREADY OWNED BY THIS TASK is claimed by it: a second message
    # typed into a conversation that is running is the inbox-absorb case the
    # chat has always had, and the answer is `run: true` so the send goes down
    # the client's ordinary path (`agent._send`) rather than starting a second
    # process.
    claim_token = ""
    if behind_own:
        ok = False
    else:
        ok, _took, claim_token = manager.claim_for_send(key, owner_token, run_id,
                                                         session_id)
    if ok:
        # THE FOLDER IS THIS TASK'S FROM HERE, and that record is the gate's —
        # nothing on the listing reads it. The row turns `in_progress` the way
        # every send's does, flag or no flag: the page marks the session as it
        # sends (`POST /api/tasks/running`, #1163) and that mark rings the poll.
        # Ringing here as well was the same bell twice about a row that had not
        # changed yet.
        resp: dict = {"run": True}
        # THE CLAIM TOKEN RIDES ALONG (2026-09-17, Bugbot PR #1194): the client
        # echoes it back on the run request as `queue_claim`, and
        # `routers/run.py::_folder_busy` consuming it there is the proof this
        # send is the one `claim_for_send` just counted, so the gate looks
        # rather than claiming a second time. `claim_for_send` only fails to
        # mint one when `ok` is False, which never reaches here.
        if claim_token:
            resp["claim"] = claim_token
        if not chat_key:
            # A NAMELESS SEND GETS ITS NAME BACK. The client is untouched in
            # this PR and ignores the field; the run it is about to start
            # replaces the placeholder through the spawn site
            # (`routers/run._file_owner` → `queue_manager.started`), and the
            # token is here so the composer can eventually say "that owner is
            # me" without waiting for a run id.
            resp["owner_token"] = owner_token
        return resp

    if not message.strip():
        return _error("message: cannot be empty — this folder is busy, and a "
                      "send with no words has nothing to queue", status=400)

    try:
        # `schedule._now()` and not this module's clock: the entry's due time is
        # compared against the scheduler's own `now` on every tick, and two
        # clocks over one comparison is how a message due "now" lands a second in
        # the future and waits a whole tick for nothing.
        #
        # `origin="chat"` AND NOT A FIELD OF THE BODY: this endpoint is the one
        # thing a chat's own queued message comes through, so the fact is the
        # endpoint's to state rather than the request's to claim. It is what
        # keeps this message from shutting the composer it was typed into
        # (`_queue_summary`), and a body that could set it would let the New
        # task form silently opt a calendar message out of the block it is
        # supposed to cause.
        entry = schedule_api.create_entry(resolved, dict(body, message=message),
                                          schedule._now(), origin="chat")
    except ValueError as exc:
        return _error(str(exc), status=400)

    # THE DRAFT THIS SEND SPENT, settled BEFORE anything is numbered below —
    # `_task_number` allocates, and the whole point of the rekey inside is that
    # it finds the number already sitting on `pending:<entry-id>` and mints
    # nothing. The schedule router owns the move because it owns the other one
    # exactly like it (a scheduled form's `draft_id`), and one copy is what keeps
    # the two from drifting. Best-effort and silent: see its docstring.
    schedule_api.spend_chat_draft(body.get("draft_key"), entry,
                                  sent=str(body.get("message") or ""))

    # The row this message landed on, read the same way the listing files it:
    # the session it named, else the LEADER's key for a follower, else its own
    # pending key. The entry is added to the index because it is a second old
    # and nothing else has seen it yet.
    task_key = _entry_key(entry, dict(by_id, **{str(entry.get("id") or ""): entry}))
    # AND INTO THE LINE. The store is where the message lives; the manager is
    # where its turn is decided, and a message that was only written would sit
    # there until the next tick happened to notice it.
    #
    # A FOLLOW-UP IS NOT A SECOND SLOT. The entry still carries `follow_of` —
    # the store needs it to file the message on the right row — but the manager
    # keys by TASK, and a message typed into a chat that is already queued has
    # its leader's key, so this `enqueue` finds it already standing there and
    # changes nothing. Which is exactly the design: a follow-up is a message on
    # a task, not another task in the line.
    manager.enqueue(key, task_key, str(entry.get("id") or ""))
    tasks_watch.notify({task_key})
    # ONE COLLECTION FOR BOTH HALVES of the answer, the shape run-now's queued
    # arm already uses: where this message landed in the line and what its task
    # is CALLED are two questions about the same set of tasks, and collecting is
    # a glob over every transcript on the machine.
    tasks = _collect()
    place = _queue_place(task_key, tasks)
    # Placed after the line is derived, never before: `_task_number` fills in
    # the task's project/target/order, and `_queue_lines` is a fact about
    # entries and sessions that must be read the same way the listing reads it.
    number = _task_number(task_key, tasks)
    return {"run": False, "entry": entry, "key": task_key,
            # WHAT THIS CHAT IS NOW CALLED — minted here rather than waited for.
            # See `_task_number`: the chip under a queued bubble names the task,
            # and a brand-new chat's first queued message had no number until
            # the next listing. "" only where the task itself has gone.
            "task_id": number,
            "position": place["position"], "ahead": place["ahead"],
            "ahead_title": place["ahead_title"],
            # The task key behind the name, so the bubble's "behind TASK-041"
            # opens a chat that has no session yet (`pending:<entry>`).
            "ahead_key": place["ahead_key"],
            # WHERE "behind TASK-041" GOES when the chip is clicked — the
            # holder's session and target, `taskHref`'s own pair. Both "" for a
            # holder with no session yet, and the chip then says the words
            # without the link.
            "ahead_session": place["ahead_session"],
            "ahead_target": place["ahead_target"],
            # WHY it queued, where the line alone cannot say: the folder can be
            # free and the position 0 with nobody ahead, and this is still a
            # message waiting on an earlier one of its own.
            "behind_own": behind_own}


def _owner_token(body: dict) -> str:
    """A name for an admission that has none — `admit:<something>`.

    A BRAND-NEW CHAT'S FIRST SEND NAMES NOBODY: there is no session (Claude Code
    mints one inside the spawn) and no run (the spawn has not happened — this
    endpoint is what says it may). So the admission filed no owner at all, the
    folder went on reading free, and a second nameless send arriving in that
    window was admitted straight into it — the exact race `claim` exists to
    settle, walked around because there was nothing to write down.

    THE IDENTITY THE CLIENT ALREADY SENDS, where it sends one. `draft_key` is a
    session-less composer's own key (`new:<file>`) and it is stable across that
    composer's sends, so two messages typed into one new chat claim one owner
    rather than fighting over the folder. Otherwise a uuid, which is a name that
    is at least unique — the placeholder's job is to be SOMEBODY, and it expires
    on its own (`queue_manager.PLACEHOLDER_TTL`) because no process exists yet
    to send the event that would free it."""
    for field in ("draft_key", "pane", "turn", "client_id"):
        value = str(body.get(field) or "").strip()
        if value:
            return queue_manager.PLACEHOLDER_PREFIX + value
    return queue_manager.PLACEHOLDER_PREFIX + uuid.uuid4().hex


def _admit_key(session_id: str, follow_of: str, by_id: dict) -> str:
    """The TASK KEY an incoming chat send belongs to: the session it names, else
    the key its LEADER is filed under for a message typed behind another, and ""
    for the first message of a brand new chat.

    `_entry_key`'s filing rule read from the request side, in one place because
    two things ask it of the same body — whether this chat already has work of
    its own waiting (`_behind_own`) and whether the folder it is sending into is
    already its own (the manager's `is_free`). Two spellings of "which task is
    this" would let those two disagree about one send.

    A leader that names no entry is answered "" rather than guessed at; the
    endpoint has already refused that body with a 400."""
    if session_id:
        return session_id
    if not follow_of:
        return ""
    leader = by_id.get(follow_of)
    return "" if leader is None else _entry_key(leader, by_id)


def _behind_own(session_id: str, follow_of: str, by_id: dict) -> bool:
    """Has this chat already got a message of its own waiting to be claimed?

    The same task key on both sides and nothing cleverer: the incoming send's
    key (its session, else the key its leader is filed under) against the key of
    every PENDING entry that is already due. Due, because a message scheduled
    for next Tuesday is not in front of anything — it is waiting on the clock,
    and queueing behind it would park this send until Tuesday.

    A send with neither a session nor a leader is the first message of a brand
    new chat and can be behind nothing."""
    mine = _admit_key(session_id, follow_of, by_id)
    if not mine:
        return False
    now = time.time()
    for entry in by_id.values():
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        due = _queue_at(entry)
        if not due or due > now:
            continue
        if _entry_key(entry, by_id) == mine:
            return True
    return False


def _queue_place(task_key: str, tasks: dict[str, dict] | None = None) -> dict:
    """Where one task stands right now — `{"position", "ahead_key", "ahead",
    "ahead_title", "ahead_session", "ahead_target"}`, re-derived from scratch,
    or from a collection the caller already holds.

    `ahead_key` rides along for the same reason the row carries it (`_row`'s
    `queue_ahead_key`): a holder that has not minted a session yet can still be
    LINKED to — it is a `pending:<entry>` row with a number — and an answer
    that named it only in words would leave the one surface that has just
    changed the line unable to click through to what is in front of it
    (round-3 review, 2026-09-12).

    The three endpoints below all answer with a place in a line, and all three
    have just CHANGED that line (stored an entry, skipped to the head, held an
    answer). Re-READING it is the only honest way to report it: the manager's
    index is one table and another window may have moved it in the same second,
    and a number computed before the event was handed over would be a promise
    about a queue that no longer exists.

    ONE TASK'S SLOT AND NOT THE WHOLE TABLE (PR 2, 2026-09-17):
    `queue_manager.place()` answers exactly the question this asks, where
    `positions()` would hand back every line on the machine for one row. The
    naming half is unchanged — `_name_ahead` still needs the Tasks collection,
    which is why `tasks` is still read.

    Position 0 with no name is the answer for a task the manager does not place
    — the folder freed between the write and this read, most often — and the
    client prints "runs next" for it rather than "#0 in line".

    Passing `tasks` is for the ONE caller that needs a second Tasks fact in the
    same reply (run-now, which also names who is ahead): the collection is a
    glob over every transcript on the machine, and it is the same collection
    both answers are about."""
    if tasks is None:
        tasks = _collect()
    queue: dict[str, dict] = {}
    manager = queue_manager.peek() if project_queue.enabled() else None
    if manager is not None:
        # `peek` for the same reason `_queue_lines` reads that way: every caller
        # of this has ALREADY fired its event through `get()`, so the manager is
        # there — and the one road that has not (a place asked on a listing) must
        # not build it.
        spot = manager.place(task_key)
        if int((spot or {}).get("position") or 0) > 0:
            queue[task_key] = _queue_row(spot)
    _name_ahead(queue, {}, tasks)
    place = queue.get(task_key) or {}
    return {"position": place.get("position", 0),
            "ahead_key": place.get("ahead_key", ""),
            "ahead": place.get("ahead", ""),
            "ahead_title": place.get("ahead_title", ""),
            # The chip under a queued bubble links to the chat in front, the
            # same way the row does — one derivation, so the two surfaces cannot
            # send a reader to different places.
            "ahead_session": place.get("ahead_session", ""),
            "ahead_target": place.get("ahead_target", "")}


@router.post("/api/tasks/queue/skip")
def api_queue_skip(body: dict = Body(...),
                   x_fused: str | None = Header(default=None)):
    """Move one queued task's due work to the head of its folder's line.

    **IT NEVER INTERRUPTS THE RUN IN FLIGHT** (Akshil, 2026-09-12). Skip is a
    statement about the ORDER of what is waiting, not about what is happening:
    the holder keeps the folder until its turn ends, and this task goes first
    when it frees. So the answer is always position 1 and never "running now" —
    there is no gesture in this app that takes a folder off a live process.

    **`manager.skip` IS THE WHOLE MECHANISM** (PR 2, 2026-09-17): the task moves
    to index 0 of its folder's line — right behind the owner — and is marked
    promoted, which is what lights the ⤒ on the row. Newest press wins, so a
    later skip pushes this one to 2; that is the rule the old stamp ordering was
    trying to express, stated once in the index instead of re-derived from
    `priority_at` on every read.

    `schedule.set_priority` is still written and now means only one thing: the
    calendar page's existing queued display reads the store's flag. NOTHING
    ORDERS BY IT any more. Only the task's DUE work is promoted — a message the
    same task has scheduled for next Tuesday is not in this line and jumping it
    to the head of one would be this verb silently rescheduling work nobody
    asked about.

    REFUSED (400) WHEN THE TASK IS NOT QUEUED, rather than quietly setting a flag
    that does nothing: "skip the queue" on a task that is not in one is a client
    that is looking at a stale row, and the honest answer is what makes it
    refetch. A task holding a HELD ANSWER is already at the head by definition
    (an answer outranks every message in its folder), so it is answered `ok` and
    nothing is written.

    **`{entry_id}` IS AN ALTERNATIVE TO `{key}`, and the chip in the chat sends
    it** (round-2 review, 2026-09-12). A task key is not a constant: a queued
    new chat is `pending:<leader entry>` until its leader's run mints a session,
    and then the whole row REKEYS onto that session. A Skip button holding the
    key it was painted with would, a second after the leader fired, name a row
    that no longer exists — a 404 on the one gesture the user is watching the
    line for. The entry id never moves, so the entry is what the chip names and
    the key is resolved here through the listing's own filing rule
    (`_entry_key`), which is the same answer the row was built under. The
    response is unchanged either way.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    key = str(body.get("key") or "").strip()
    entry_id = str(body.get("entry_id") or "").strip()
    if not key and not entry_id:
        return _error("key or entry_id: required", status=400)
    if not project_queue.enabled():
        return _error("project queue is off", status=409)
    if not key:
        by_id = _by_entry_id()
        entry = by_id.get(entry_id)
        if entry is None:
            return _error(
                f"entry_id: no scheduled message with id {entry_id!r}",
                status=404)
        key = _entry_key(entry, by_id)

    tasks = _collect()
    task = tasks.get(key)
    if task is None:
        return _error(f"no task with key {key!r}", status=404)
    now = time.time()
    manager = queue_manager.get()

    # ONE CALL, AND IT IS THE PRESS. `manager.skip` is the whole mechanism for
    # an answered task too — it moves to the head of the line like anything else
    # and newest press wins — so there is no held-answer arm here any more: a
    # second road to the same answer is a second set of rules to keep in step
    # with the first.
    #
    # Position 0 back is "this task stands in no line", which is the 400 the
    # docstring promises rather than a flag quietly set on nothing — UNLESS the
    # press itself started it. The folder was free, the pump handed it straight
    # over, and a task that is now RUNNING is the best possible outcome of "run
    # this next": answered 200, never refused.
    outcome = manager.skip(key) or {}
    if int(outcome.get("position") or 0) == 0 and not outcome.get("started"):
        return _error("not queued", status=400)

    # THE STORE'S FLAG, FOR THE CALENDAR AND NOTHING ELSE (see the docstring),
    # and only over THE FOLDER THIS TASK IS WAITING IN: a message the same task
    # has due into a DIFFERENT tree stands in a different line, and promoting it
    # here would be this verb silently reordering work nobody asked about. The
    # folder is the one the task's earliest waiting message is for, which is the
    # message the manager would spawn for it (`_queue_spawn`).
    waiting: list[tuple[float, str, str]] = []
    for entry in task["entries"]:
        if str(entry.get("state") or "") != schedule.PENDING:
            continue
        due = _queue_at(entry)
        if not due or due > now:
            continue
        folder = project_queue.queue_key(str(entry.get("target") or ""))
        entry_id = str(entry.get("id") or "")
        if folder and entry_id:
            waiting.append((due, folder, entry_id))
    folder = min(waiting)[1] if waiting else ""
    ids = [entry_id for _due, other, entry_id in waiting if other == folder]
    if ids:
        schedule.set_priority(ids, True)
    tasks_watch.notify({key})
    # WHO IS IN FRONT NOW, the way admit, decide and run-now all answer it
    # (`_queue_place`). The press has just changed this line and the chat has to
    # redraw it: without these the card could only paint the claim ("runs next")
    # and kept saying "behind TASK-041" about whatever was ahead BEFORE the press
    # until the next listing landed (🟡 review, 2026-09-12).
    #
    # RE-READ AFTER THE PRESS, not from the `tasks` in hand: that collection was
    # read before the skip moved the line, so placing against it would report
    # the order this endpoint had just replaced. `position` stays the promise
    # this endpoint makes (see the docstring) and is not taken from the read.
    place = _queue_place(key)
    return {"ok": True, "position": 1,
            "ahead_key": place["ahead_key"], "ahead": place["ahead"],
            "ahead_title": place["ahead_title"],
            "ahead_session": place["ahead_session"],
            "ahead_target": place["ahead_target"]}


def _already_decided(agent, run_dir: str, request_id: str) -> bool:
    """Is this card's answer already on disk?

    THE SECOND TAB (round-3 review, 2026-09-12). `_write_decision` is a
    first-writer-wins latch and `queue_manager.card_answered` is another one,
    but they latch in different places: a card answered in one window and then answered again in a
    stale second window would have its second answer HELD — parked against a
    question that has a verdict, to be replayed into a run that has already
    moved on, and reported to that tab as "Answer queued" when the truth is
    "somebody already answered this". Passing it through instead costs nothing
    and reports the truth: `agent._decide` reads the existing decision and hands
    back the verdict that won, which is what the card should be showing.

    Best-effort by construction — a perm dir that cannot be read answers False,
    which is the ordinary road and where every rule about the run is applied
    anyway."""
    try:
        path = os.path.join(agent._perm_dir(run_dir), request_id + ".res.json")
        return os.path.exists(path)
    except Exception:  # noqa: BLE001 — an unreadable run is the ordinary road
        return False


@router.post("/api/tasks/queue/decide")
def api_queue_decide(body: dict = Body(...),
                     x_fused: str | None = Header(default=None)):
    """Answer a parked run's card — through the queue.

    The same body the chat's own `decide` action takes, plus the `session_id` and
    `project` this needs to find the line. Two answers:

    * `{"held": false, ...}` — the decision went STRAIGHT THROUGH to
      `agent._decide` and the rest of the object is its ordinary result. This is
      what happens with the flag off, when the folder is free (or held by this
      very session), and when the run is already dead — a dead run's answer is
      recorded as `expired` by `_decide` itself, and holding it would park a
      decision for a process that can never read it.
    * `{"held": true, ...}` — the folder is busy with ANOTHER task, so the answer
      is stored in the manager's index and delivered when the folder frees.
      Nothing is written to the run's perm directory yet, which is the point: a
      parked run given its answer now would wake up and start editing a working
      tree another task owns — the exact collision the feature exists to
      prevent, arriving through the one door that is not a message.

    **WHICH OF THE TWO IS THE MANAGER'S ANSWER, NOT THIS ENDPOINT'S** (PR 2,
    2026-09-17). `card_answered` is one call under one lock: it knows who owns
    the folder, so "free, or this very task" and "somebody else has it" are
    decided where the owner is recorded rather than re-derived here against a
    scan that may already have moved. Held also puts the task at index 0,
    promoted — an answered card is the very next thing its folder does, until a
    later Run next says otherwise.

    **WHAT IS HELD IS THE ARGUMENTS, NOT A DECISION PAYLOAD** (deviation from the
    design doc, 2026-09-12, and noted there for the deliverer). The obvious store
    is the dict `_decide` would have written, and it is wrong: building it means
    re-implementing every rule in `_decide` — the narrow-never-widen scope
    downgrade, the mode switch, the keep-planning message, the answer validation
    against the parked request's own questions — in a second place, where they
    would drift. Worse, those rules read the LIVE run (`_alive`, the request
    file), and the answer is being made now and applied later: a scope computed
    against today's card and written after the run moved on would be a grant
    nobody checked. So `payload` is `{"raw": {...the decide arguments...}}` and
    the deliverer calls `agent._decide(**raw)` at delivery time, when every one of
    those rules is evaluated against the run as it then is (`_queue_deliver`).
    The first-writer-wins latch on disk (`_write_decision`) is unchanged.

    Position is 1 in the ordinary case — a held answer goes to the head of its
    folder's line — but it is READ BACK rather than promised: a Run next pressed
    on something else in the same second outranks it (rule 5, 2026-09-16), and
    the number the card prints has to be the one the row is about to print.

    **THE TWO IDS ARE CHECKED BEFORE ANYTHING IS STORED** (round-2 review,
    2026-09-12). Both become a path, and not here: the run id is joined onto
    `agent.RUNS` and the request id is joined with `.res.json` under the perm
    directory — by the pump that delivers the answer, minutes later and with
    nothing left to say where the string came from. `agent._decide` has always
    refused them (`_bad_id`), so the straight-through arm was covered; the HELD
    arm writes them into the index first and lets the rule be applied by whoever
    reads it back. So the rule is applied here, to both arms.

    **A CARD THAT ALREADY HAS A VERDICT IS DELIVERED, NEVER HELD.** See
    `_already_decided`: holding a second answer to a question that is already
    answered parks a decision nothing will ever act on and tells the tab that
    sent it that it is next in line.

    **A RUN WITH NO SESSION IS DELIVERED, NEVER HELD.** A held record is keyed
    by `session_id` — it is how the delivery finds the row, how the chip finds
    the position and how Skip recognises the head of the line — so an empty one
    parks a decision no view can reach and rings `notify(None)`, which wakes
    every poller about nothing. A run with no session cannot be queued behind
    anything anyway, and delivering it now is the honest fallback: the decision
    goes where the user meant it to go.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard

    run_id = str(body.get("run_id") or "")
    request_id = str(body.get("request_id") or "")
    if project_queue.bad_id(run_id) or project_queue.bad_id(request_id):
        return _error("run_id and request_id: required, and neither may be a "
                      "path — no leading dot, no separator", status=400)
    raw = {"run_id": run_id, "request_id": request_id,
           "decision": str(body.get("decision") or ""),
           "scope": str(body.get("scope") or ""),
           "mode": str(body.get("mode") or ""),
           "answers": str(body.get("answers") or ""),
           "note": str(body.get("note") or ""),
           "custom": str(body.get("custom") or "")}

    agent = project_queue.agent_module()
    if agent is None:
        # No agent module, no cards and no runs: there is nothing this could
        # decide and nothing it could hold. The chat's own error, not a 500.
        return _error("the claude agent is not available", status=503)

    session_id = str(body.get("session_id") or "")
    run_dir = os.path.join(str(getattr(agent, "RUNS", "")), run_id)
    # THE THREE REASONS TO DELIVER THAT ARE NOT ABOUT THE FOLDER, asked before
    # the manager is: the flag is off, the run has no session to file an answer
    # under, the process is dead (a dead run's answer is recorded as `expired`
    # by `_decide` itself), or the card already has a verdict from another tab.
    # None of them is a question about who owns a working tree.
    if (not project_queue.enabled()
            or not session_id
            or not project_queue.run_alive(agent, run_dir)
            or _already_decided(agent, run_dir, request_id)):
        return {"held": False, **agent._decide(**raw)}

    # …and the one that is. `card_answered` stores nothing when the folder is
    # free or is this very task's — a card raised by a run that has not
    # published its session yet is still that chat's own run, and holding its
    # answer would park a decision behind the very process it unblocks.
    held = queue_manager.get().card_answered(session_id, run_id, request_id, raw)
    if not held.get("held"):
        return {"held": False, **agent._decide(**raw)}

    tasks_watch.notify({session_id})
    place = _queue_place(session_id)
    return {"held": True,
            # The manager's number where the read cannot see one: the pump may
            # have delivered this answer inside `card_answered` itself, which
            # leaves the task owning its folder and standing in no line.
            "position": place["position"] or int(held.get("position") or 1),
            "ahead": place["ahead"],
            "ahead_title": place["ahead_title"],
            "ahead_key": place["ahead_key"]}
