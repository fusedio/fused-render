"""Task numbers and unread marks — the two global stores behind the Tasks page.

A **task is a Claude Code session**, 1:1, and the session store is Claude Code's,
not ours. So neither of the facts this module keeps can live inside a transcript:
a transcript has no room for "this is TASK-003 of ~/Desktop/fused", and it
certainly has no room for "the human has read this message". Both are ours, both
are keyed by session, and both therefore live here.

**Global, deliberately — not branch-scoped.** `storage.home_dir()` nests state
under the checkout's branch so a dev branch cannot fire the baseline install's
schedule. Sessions are the other kind of thing: `~/.claude/projects` is one pool
for the whole machine, so a task numbered from a worktree must keep that number
when the same session is read from main. This mirrors exactly what
`server/routers/claude_sessions.py` does with `session_names.json` /
`triage.json`, and uses the same directory as those, for the same reason.

## `task_ids.json` — allocate once, never renumber

    {"<session-id-or-pending-key>": {"project": "/Users/…/fused", "n": 3}}

Numbers restart at 1 **per project** (a project is a folder — see §2 of the
design; a task on `~/x/foo.py` is a task in `~/x`), and allocation is
"max n seen for this project, plus one", read and written under one lock. That
rule is the whole point: TASK-002 does not become TASK-001 when TASK-001 is
deleted. A number the user has seen, quoted in a note, or typed into a message
must keep meaning the same thing, and a compacting scheme buys tidiness by
breaking that. Gaps are the price and they are the correct price.

A task that exists before its session does (§5: a message scheduled for
tomorrow has nothing to run in yet) is keyed `pending:<entry-id>` and gets its
number then. `rekey` moves that number onto the session id at the first run, so
the row the user has been watching keeps its name instead of being renumbered
the moment it finally does something.

## `read.json` — unread, per message

    {"__initialized_at__": 1755300000.0,
     "<session-id>": {"last_read_at": 1755300000.0,
                      "read_ids": ["MSG-003"], "read_floor": 0}}

**Why an explicit id set and not a watermark.** A watermark ("everything up to
MSG-003 is read") cannot express the thing the design actually asks for: unread
is tracked *per message*, and clicking MSG-003 marks MSG-003 read — not MSG-002,
which the user skipped past and still means to read. A watermark alone would
mark it read as a side effect, and silently losing a notification is the one
failure this feature exists to prevent.

The set's cost is that it grows without bound, so `read_floor` is a watermark
used **only as a compaction floor**: once the set covers a contiguous run from
MSG-001, that run collapses into the floor and leaves the set. Every id at or
below the floor is implicitly read, so the common case (a user who reads a
thread through) stores one integer instead of a thousand strings, and the
sparse case (read 3, skipped 2) keeps the exact truth it needs. `last_read_at`
is the wall clock of the most recent mark; it is not consulted when deciding
whether a message is read — reading it as a floor would reintroduce the bug the
set exists to avoid.

**Marking a WHOLE task read** (the List row's own button) is not a second
mechanism and did not need one. It is `mark_read_many` with every message the
thread holds — one lock and one write for a thread of 89, where clicking through
was 89 of each — and the compaction above is what turns it into the watermark it
should be: a contiguous run from MSG-001 folds into `read_floor` and the id list
comes out empty, which is precisely "everything in this task is read", stored as
one integer. The caller passes the ids; the mark still reaches nowhere on its
own, so the invariant one message carries is the invariant the batch carries.

**Day one.** A store that has never existed would otherwise say every message
ever written is unread — on a real machine that was 1,946 unread across 174 of
192 tasks, a badge on everything and therefore a badge that means nothing.
Unread has to mean "arrived since I started using this", so `initialize` stamps
each existing task's floor at its current message count exactly once, under the
`__initialized_at__` guard. Same mechanism as the compaction floor above, set at
the start rather than accumulated — and per task, not one global clock, so a
task that appears LATER still has its first message land unread.

Nothing here raises for input it cannot read: a missing store, a corrupt store,
a record of the wrong shape, a transcript that vanished mid-walk — each degrades
to "no number/no marks yet", never to a failed listing. Same posture as every
other registry in this package.

No import of anything under `fused_render.server`: `server/app.py` imports the
tasks router, the router imports this module. Keeping that one-way means the
constants below are duplicated from `claude_sessions.py` rather than imported —
the same deliberate local duplication `claude_artifacts.py` makes of the same
two lines.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from datetime import datetime, timezone

try:
    import fcntl  # POSIX only — Windows falls back to no inter-process lock,
    # the same posture as claude_sessions.api_claude_session_triage, whose
    # directory (and locking convention) this shares.
except ImportError:  # pragma: no cover
    fcntl = None

# CLAUDE_CONFIG_DIR wins where set — same rule (and same deliberate local
# duplication) as server/routers/claude_sessions.py and claude_artifacts.py.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

# The same directory claude_sessions.py keeps session_names.json and triage.json
# in, and for the same reason: global, never branch-nested. Derived from the env
# at import like its twin, so overriding either in a test overrides both only if
# both are overridden — tests that read triage must patch that module too.
STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "claude-sessions")

TASK_IDS_FILE = "task_ids.json"
READ_FILE = "read.json"

# What a task with no session yet is keyed by (§5). The entry id is already
# unique and already sorts by due time, so nothing is minted for this.
PENDING_PREFIX = "pending:"

# Zero-padded to three, and no further: TASK-1000 is simply four digits wide.
# Padding is for a column of numbers to line up, not a limit.
_TASK_WIDTH = 3
_MSG_WIDTH = 3


def pending_key(entry_id: str) -> str:
    """The task key for a scheduled message that has not run yet."""
    return PENDING_PREFIX + entry_id


def format_task_id(n: int) -> str:
    return f"TASK-{n:0{_TASK_WIDTH}d}"


def format_message_id(n: int) -> str:
    return f"MSG-{n:0{_MSG_WIDTH}d}"


def message_ids(count: int) -> list[str]:
    """`MSG-001 … MSG-<count>`, oldest first — the ids of a thread of `count`
    messages. Pure, because message ids are *derived*: the Nth message of a
    task in time order IS MSG-N, and storing that would only create something
    that could disagree with the thread."""
    return [format_message_id(n) for n in range(1, max(0, count) + 1)]


def message_number(message_id: str) -> int:
    """`"MSG-012"` -> 12, and 0 for anything that isn't one — an id from a
    future format, a truncated store, a client typo. 0 sorts below every real
    message, so an unreadable id can never be mistaken for a read one."""
    if not isinstance(message_id, str):
        return 0
    text = message_id.strip().upper()
    if not text.startswith("MSG-"):
        return 0
    try:
        return max(0, int(text[4:]))
    except ValueError:
        return 0


# ------------------------------------------------------------------ the files


def load_state(filename: str) -> dict:
    """A json dict from STATE_DIR, or {} — missing/corrupt is not an error.
    Same helper, same posture, as claude_sessions._load_state; duplicated here
    rather than imported so this module keeps its no-server-imports rule."""
    try:
        with open(os.path.join(STATE_DIR, filename), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update(filename: str, mutate):
    """Read-modify-write one store under an exclusive lock; return whatever
    `mutate` returns.

    The lock is a sibling `.lock` file held for the whole read-modify-write, not
    just the write — the same shape as `api_claude_session_triage`. Two shells
    marking different messages read is not exotic (the app runs several windows
    against one server, and FastAPI serves sync routes from a threadpool), and
    without the read inside the lock the second writer would persist a snapshot
    taken before the first one's change and drop it."""
    os.makedirs(STATE_DIR, exist_ok=True)
    path = os.path.join(STATE_DIR, filename)
    with open(path + ".lock", "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = load_state(filename)
        result, changed = mutate(data)
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
    return result


# --------------------------------------------------------------- task numbers


def _record(store: dict, key: str) -> dict | None:
    rec = store.get(key)
    if not isinstance(rec, dict):
        return None
    try:
        n = int(rec.get("n"))
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    project = rec.get("project")
    return {"project": project if isinstance(project, str) else "", "n": n}


def task_ids() -> dict:
    """Every allocated number, as `{key: {"project": str, "n": int}}`, with
    unreadable records dropped."""
    store = load_state(TASK_IDS_FILE)
    out = {}
    for key, rec in store.items():
        clean = _record(store, key)
        if clean is not None:
            out[key] = clean
    return out


def _next_numbers(store: dict) -> dict[str, int]:
    """project -> highest number allocated in it. "Max seen plus one" is the
    allocation rule precisely so a deleted task's number is never handed out
    again: counting live tasks would recycle it."""
    high: dict[str, int] = {}
    for key in list(store):
        rec = _record(store, key)
        if rec is None:
            continue
        if rec["n"] > high.get(rec["project"], 0):
            high[rec["project"]] = rec["n"]
    return high


def ensure_ids(items, rekeys=()) -> dict[str, str]:
    """Numbers for `items`, allocating any that are missing.

    `items` is an iterable of `(key, project, order)`: the task key (a session
    id, or `pending:<entry-id>`), the project folder it belongs to, and a sort
    key — the session's first timestamp — that decides the order new numbers are
    handed out in. Returns `{key: "TASK-nnn"}` for every item.

    `rekeys` is an iterable of `(old, new)` applied first, in the same lock, so
    a session that has just minted its id keeps the number its pending row was
    already showing rather than being renumbered by the allocation below it.

    Idempotent: an item that already has a number is left exactly as it is, so
    calling this on every listing costs one read and no write once the store has
    caught up.
    """
    items = [
        (str(key), str(project or ""), order)
        for key, project, order in items
        if key
    ]
    rekeys = [(str(a), str(b)) for a, b in rekeys if a and b and a != b]

    def mutate(store: dict):
        changed = False
        for old, new in rekeys:
            rec = _record(store, old)
            if rec is None:
                continue
            # The number only MOVES onto a key that has none. Two pending
            # occurrences of one recurring message can chain into the same
            # session; the first transfers, and the second's number is simply
            # SPENT — the record stays put, unread by anything (the pending row
            # is gone the moment its entry has a session), because deleting it
            # would drop the project's high-water mark and hand the same number
            # out again. Releasing a number is the one thing allocate-once
            # forbids.
            if _record(store, new) is not None:
                continue
            store.pop(old, None)
            store[new] = rec
            changed = True

        high = _next_numbers(store)
        missing = [it for it in items if _record(store, it[0]) is None]
        # Sorted by first-timestamp so a backfill numbers a project's history in
        # the order it happened; the key breaks ties so two sessions that start
        # in the same millisecond still number deterministically.
        missing.sort(key=lambda it: (it[2] if it[2] is not None else 0.0, it[0]))
        for key, project, _order in missing:
            n = high.get(project, 0) + 1
            high[project] = n
            store[key] = {"project": project, "n": n}
            changed = True

        out = {}
        for key in [it[0] for it in items] + [new for _old, new in rekeys]:
            rec = _record(store, key)
            if rec is not None:
                out[key] = format_task_id(rec["n"])
        return out, changed

    return _update(TASK_IDS_FILE, mutate)


def rekey(old: str, new: str) -> str:
    """Move a task's number from `old` to `new` — the pending row's key to the
    session id its first run minted (§5). Returns the number `new` ends up with,
    or "" if there was nothing to move and nothing already there.

    Never renumbers: if `new` already holds a number (the second occurrence of a
    recurring message chaining into a session that already ran one), that number
    stands and `old`'s is dropped."""
    return ensure_ids([], rekeys=[(old, new)]).get(new, "")


def task_number(key: str) -> str:
    """The stored number for one key, or "" if it has none yet."""
    rec = _record(load_state(TASK_IDS_FILE), key)
    return format_task_id(rec["n"]) if rec else ""


# ----------------------------------------------------------------- the unread


# The reserved key that says this store has been through its one-time
# initialisation. Never a task key — a task key is a session id or
# `pending:<entry-id>`, and neither can look like this.
INIT_KEY = "__initialized_at__"


def read_state() -> dict:
    return load_state(READ_FILE)


def initialized(state: dict) -> bool:
    return isinstance(state.get(INIT_KEY), (int, float)) and \
        not isinstance(state.get(INIT_KEY), bool)


def initialize(counts, now: float | None = None) -> bool:
    """The day-one baseline: everything that already exists is read. Returns
    whether this call was the one that did it.

    Without this, unread means "exists" rather than "arrived since I started
    using this" — a fresh store lit up 174 of 192 rows on a real machine, which
    is a badge that means nothing because it is on everything.

    `counts` is `(task key, message count)` for everything the listing could
    see, and each key's floor is set to its count: the mechanism is the same
    compaction floor the explicit set already uses, only stamped once at the
    start instead of accumulated. A task discovered LATER gets no floor, so its
    first message is properly unread — which is the whole point, and the reason
    this is a per-task floor rather than a global clock.

    **Once, and only once.** The guard is read inside the lock, so a second run
    (or a second window racing the first) cannot move the baseline forward and
    silently mark unread things read.
    """
    counts = [(str(key), int(count)) for key, count in counts if key]

    def mutate(state: dict):
        if initialized(state):
            return False, False
        stamp = time.time() if now is None else float(now)
        state[INIT_KEY] = stamp
        for key, count in counts:
            if count <= 0:
                continue
            floor, ids = _read_record(state, key)
            state[key] = {"last_read_at": stamp,
                          "read_ids": sorted(
                              (i for i in ids if message_number(i) > count),
                              key=message_number),
                          "read_floor": max(floor, count)}
        return True, True

    return _update(READ_FILE, mutate)


def _read_record(state: dict, key: str) -> tuple[int, set[str]]:
    """(floor, explicit ids) for one task. Anything unreadable reads as "nothing
    read", which is the safe direction: a lost mark shows a notification twice,
    a spurious one hides it forever."""
    rec = state.get(key)
    if not isinstance(rec, dict):
        return 0, set()
    try:
        floor = int(rec.get("read_floor") or 0)
    except (TypeError, ValueError):
        floor = 0
    ids = rec.get("read_ids")
    if not isinstance(ids, list):
        ids = []
    return max(0, floor), {i for i in ids if isinstance(i, str)}


def is_read(state: dict, key: str, message_id: str) -> bool:
    floor, ids = _read_record(state, key)
    return message_id in ids or 0 < message_number(message_id) <= floor


def read_count(state: dict, key: str, total: int) -> int:
    """How many of a thread's first `total` messages are marked read. Counted
    rather than subtracted so a stale id past the end of the thread (a message
    that was read and then the transcript replaced) cannot drive an unread count
    negative."""
    floor, ids = _read_record(state, key)
    counted = min(floor, total)
    for mid in ids:
        n = message_number(mid)
        if counted < n <= total:
            counted += 1
    return counted


def mark_read(key: str, message_id: str, now: float | None = None) -> dict:
    """Mark one message read; return the task's stored record.

    Only that message. The whole reason the record carries a set rather than a
    high-water mark is that reading MSG-003 says nothing about MSG-002 — see the
    module docstring."""
    return mark_read_many(key, [message_id], now=now)


def mark_read_many(key: str, ids_to_mark, now: float | None = None) -> dict:
    """Mark SEVERAL messages read in ONE write; return the task's stored record.

    This is what "mark the whole task read" is made of. The row's own button
    would otherwise be N of these calls — 89 locks, 89 read-modify-writes and 89
    recounts on the one real thread that has 89 messages — so the batch is the
    call and the single-message mark above is the batch of one. There is no
    second mechanism: `mark_read` IS this function, so the two can never drift
    apart in how they compact or what they promise.

    **The invariant is unchanged: only the ids GIVEN are marked.** Nothing newer
    is swept in, which is the one thing this store exists to guarantee (see the
    module docstring) — a whole-task mark is broad because its CALLER passed
    every message, not because the mark itself reaches forward.

    The compaction is where the watermark comes from, and it is the same
    compaction one message has always gone through: a contiguous run up from the
    bottom folds into `read_floor`. So the ordinary whole-task mark — every
    message in the thread has happened — lands as one integer and an empty id
    list, which is exactly "everything in this task is read"; and a thread with
    something still PENDING in the middle of it (the message is not read, so its
    id is not passed) keeps the exact set on the far side of the gap. One code
    path, both truths.
    """
    key = str(key)
    # A number of 0 is "not a message id at all" (message_number's contract), and
    # a store is not the place to record a client's typo as a read message.
    marks = {format_message_id(n) for n in
             (message_number(mid) for mid in ids_to_mark) if n > 0}
    stamp = time.time() if now is None else float(now)

    def mutate(state: dict):
        floor, ids = _read_record(state, key)
        ids |= marks
        # Compaction: a contiguous run from the bottom collapses into the floor,
        # so a thread read end to end costs one integer instead of every id.
        while format_message_id(floor + 1) in ids:
            floor += 1
        ids = {i for i in ids if message_number(i) > floor}
        rec = {"last_read_at": stamp,
               "read_ids": sorted(ids, key=message_number),
               "read_floor": floor}
        state[key] = rec
        return rec, True

    return _update(READ_FILE, mutate)


# ------------------------------------------------------------- transcript head
#
# Only the head, and only the three facts a backfill needs. The full read of a
# transcript belongs to the router (routers/tasks.py) and is cached there; this
# is what makes `backfill()` cheap enough to run at startup on a machine with a
# few thousand sessions.

# path -> (size_at_parse, cwd, first_ts, first_prompt). Same cache shape, and
# the same append-only reasoning, as claude_sessions._HEAD_CACHE.
_HEAD_CACHE: dict[str, tuple[int, str | None, float | None, str]] = {}

_HEAD_CHARS = 256 * 1024
_HEAD_LINES = 2000


def reset_cache() -> None:
    """Forget every cached head. For tests, and for any caller that wants the
    next walk to re-read from disk unconditionally."""
    _HEAD_CACHE.clear()


def epoch(value) -> float | None:
    """A transcript's ISO-8601 timestamp as an epoch float, or None. A stamp
    with no zone is read as UTC — every writer of these records emits UTC, and
    guessing local would shift a session's place in a creation-ordered
    backfill."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def first_text(content) -> str:
    """First text block of a message's content. "" for tool_result-only
    content, which is how a tool result is kept from being read as something
    the human typed (mirrors claude_sessions._first_text)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
            if isinstance(block, dict) and "text" in block:
                return block.get("text", "")
    return ""


# ------------------------------------------------ machinery on a user record
#
# `type: user` records a human did not type, and the ONE policy every reader of
# a transcript's first user message now shares. There were four, they disagreed,
# and the disagreement was visible to the user in two opposite ways at once:
# rows in the Tasks list titled `<live-app-state>` and
# `<command-message>making-a-release</command-message>`, and — worse — real
# messages missing from the app because a reader dropped the whole record on
# sight of a leading tag.
#
# Two sources write these records, and that is the whole reason for the split
# below. Claude Code writes some ON the user's behalf: a finished subagent
# reporting back, a slash command's envelope, the stdout it captured. The
# fused-render Claude page writes others IN FRONT OF what the user typed
# (`composeOutgoing` in templates/claude/template.html: the app-state snapshot,
# then the pane screenshots, then the annotation notes, then the words).
#
# The corpus says the two groups behave OPPOSITELY. Over 219 transcripts / 2519
# user records with text, on one real machine (2026-08-17), counting records
# whose FIRST block is the tag and asking whether any prose survives the strip:
#
#     leading tag                records   carry prose
#     <task-notification>            889   0
#     <command-name>                  77   0      \  one envelope, three blocks,
#     <command-message>               77   0       > written in either order and
#     <command-args>                  66   0      /  sometimes indented
#     <local-command-stdout>          43   0
#     <bash-input>                     5   0      \  one envelope again: input,
#     <bash-stdout>                    5   0       > then its two output halves
#     <bash-stderr>                    5   0      /
#     <live-app-state>                72   72     ← every single one
#     <pane-shot>                      1   1
#
# So DROP is machinery all the way down: a reader that keeps it shows the user
# their own plumbing as the name of their conversation. STRIP is a PREFIX on a
# real message: a reader that drops it deletes the human's words. One session's
# only user record was the app-state block, a pane shot, and "what is this" —
# and "what is this" was gone from the app entirely.
#
# **Putting a tag in the wrong list does one of those two harms.** The test for
# which list a new tag belongs in is the table above: does a record opening with
# it EVER carry words after the block? Never → DROP. Ever → STRIP.
#
# `<user-prompt-submit-hook>` and `<system-reminder>` never LEAD a record in this
# corpus (a reminder is appended to something a human typed, which is a real
# message and stays one). They are kept as DROP because the drop this replaces
# already listed them and a leading one would be a hook's output, not prose.
_MACHINERY_DROP = (
    "task-notification",
    "command-message", "command-name", "command-args",
    "local-command-stdout", "local-command-stderr",
    "bash-input", "bash-stdout", "bash-stderr",
    "user-prompt-submit-hook", "system-reminder",
)

_MACHINERY_STRIP = ("live-app-state", "pane-shot")

_MACHINERY_TAGS = _MACHINERY_DROP + _MACHINERY_STRIP

# One leading `<tag>…</tag>` block. Non-greedy and anchored on the closing tag,
# the same discipline as agent.py's `_APP_STATE_BLOCK` — and anchored at
# position zero too, because only a LEADING block is machinery.
#
# Restricted to the names above rather than a generic `<\w+>` for the same
# reason this code exists: `<div class="card">Order now</div> why does this
# render twice?` is a real question about real markup, and a generic matcher
# would silently eat the half of it that makes it a question.
_LEADING_BLOCK = re.compile(
    r"<(%s)>.*?</\1>\s*" % "|".join(_MACHINERY_TAGS), re.DOTALL)

# The same openers with no close in sight. A transcript caught mid-flush ends
# inside a block, and so does any TRUNCATED copy of one — so a balanced strip
# cannot fire and the record would read as a real message. Everything from a
# machinery opener onwards is machinery whatever follows it, which is exactly
# the second pass template.html's `BLOCK_OPENERS` makes over a cut preview.
_LEADING_OPEN = re.compile(r"<(%s)>" % "|".join(_MACHINERY_TAGS))

# The annotation notes, which have NO TAG at all: `formatAnnotations` writes one
# opening sentence, a paragraph of field notes for the model, and a fenced json
# block. A port of template.html's `stripAnnBlock`, matched at position zero for
# the same reason it is — anything wedged in front turns the strip into a silent
# no-op and leaks raw json as the title.
_ANN_PREAMBLE = "The user annotated "
_ANN_FENCE_OPEN = "\n```json\n"
_ANN_FENCE_CLOSE = "\n```"


def _strip_ann_block(text: str) -> str:
    if not text.startswith(_ANN_PREAMBLE):
        return text
    open_at = text.find(_ANN_FENCE_OPEN)
    if open_at == -1:
        return text
    close_at = text.find(_ANN_FENCE_CLOSE, open_at + len(_ANN_FENCE_OPEN))
    if close_at == -1:
        return text
    return text[close_at + len(_ANN_FENCE_CLOSE):].lstrip("\n")


def strip_machinery(text: str) -> str:
    """`text` with every machine-written PREFIX peeled off — what the human
    actually typed, or "" when they typed no words at all.

    A loop, because one send can carry any combination of the blocks and peeling
    one exposes the next; and a loop rather than a fixed sequence because the
    envelope blocks arrive in more than one order (`/model` writes its name
    first, `/making-a-release` its message first).

    "" is a real answer, not a failure: a send that carried only a screenshot or
    only annotations is something the user DID, and naming it is the client's
    job (`stripBlocks`'s markers). Callers that must not emit an empty message
    check the result; callers deciding whether to drop the record ask
    `is_machinery`, which is a different question.
    """
    out = (text or "").strip()
    while True:
        before = out
        match = _LEADING_BLOCK.match(out)
        if match:
            out = out[match.end():].strip()
        out = _strip_ann_block(out).strip()
        if out == before:
            break
    # An opener still standing has no close in the string — see `_LEADING_OPEN`.
    return "" if _LEADING_OPEN.match(out) else out


def is_machinery(text: str) -> bool:
    """Is this record machinery WHOLE — nothing a human contributed to it?

    Two conditions, and both carry weight. The leading tag has to be a DROP tag:
    a `<live-app-state>` record is a real message with a prefix, so "the strip
    left nothing" there means only that the user sent a picture without words,
    and dropping it would lose the send. And nothing may survive the strip,
    because a DROP tag is only ever the whole record in practice (0 of 1216
    above carried prose) and on the day one does, the words win.
    """
    out = (text or "").strip()
    match = _LEADING_BLOCK.match(out) or _LEADING_OPEN.match(out)
    if match is None or match.group(1) not in _MACHINERY_DROP:
        return False
    return not strip_machinery(out)


_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)


def slash_command(text: str) -> str:
    """The command a `<command-name>` envelope records — "/making-a-release",
    "/clear", "/model" — or "" for anything that is not one.

    Searched rather than anchored, unlike everything above: the envelope's
    blocks arrive in either order on a real machine, and the command is the same
    fact whichever of them leads.

    This exists for `tasks.py _title`. A session whose only user records are a
    slash command has no prose to be named from, and the command the user typed
    is both true and useful where a blank row is neither.
    """
    match = _COMMAND_NAME.search(text or "")
    return match.group(1).strip() if match else ""


def _parse_head(path: str) -> tuple[str | None, float | None, str]:
    cwd: str | None = None
    first_ts: float | None = None
    prompt = ""
    chars = 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                chars += len(line)
                count += 1
                if chars > _HEAD_CHARS or count > _HEAD_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if cwd is None:
                    val = obj.get("cwd")
                    if isinstance(val, str) and val:
                        cwd = val
                if first_ts is None:
                    first_ts = epoch(obj.get("timestamp"))
                # `isMeta` is the caveat Claude Code writes FOR the user;
                # `isSidechain` is a prompt written for a SUBAGENT, which the
                # user never typed and which can be a whole task brief. Both
                # skipped — templates/claude/agent.py's sibling reader has
                # always skipped both, and this one having only half the pair
                # was how a subagent's brief came to name a task.
                if (not prompt and obj.get("type") == "user"
                        and not obj.get("isMeta") and not obj.get("isSidechain")):
                    msg = obj.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        # STRIPPED, not raw: the fused-render Claude page
                        # prepends its own blocks to what the user typed, and
                        # the raw text is how rows came to be titled
                        # `<live-app-state>`. An empty remainder is not an
                        # answer either — the loop simply carries on to the next
                        # user record, because a blank title while the message
                        # that could have named the row sits two lines further
                        # down is the same bug from the other side.
                        prompt = strip_machinery(first_text(msg.get("content")))
                if cwd is not None and first_ts is not None and prompt:
                    break
    except OSError:
        return None, None, ""
    return cwd, first_ts, prompt


def head(path: str, size: int | None = None) -> tuple[str | None, float | None, str]:
    """(cwd, first timestamp, first user prompt) for one transcript, cached per
    path. Transcripts are append-only, so a head that resolved fully stays
    valid however much the file grows; an incomplete one is retried once the
    file has more to offer, and a file that shrank was replaced."""
    if size is None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None, None, ""
    cached = _HEAD_CACHE.get(path)
    if cached is not None:
        cached_size, cwd, first_ts, prompt = cached
        complete = bool(prompt) and first_ts is not None and cwd is not None
        if cached_size == size or (size > cached_size and complete):
            if size != cached_size:
                _HEAD_CACHE[path] = (size, cwd, first_ts, prompt)
            return cwd, first_ts, prompt
    if len(_HEAD_CACHE) > 20000:  # unbounded only if the user has 20k sessions
        _HEAD_CACHE.clear()
    cwd, first_ts, prompt = _parse_head(path)
    _HEAD_CACHE[path] = (size, cwd, first_ts, prompt)
    return cwd, first_ts, prompt


def project_of(cwd: str) -> str:
    """The project a cwd belongs to: itself. A project is a FOLDER (§2) and a
    transcript's cwd is always one — `agent.py:_workdir` resolves a file target
    to its directory before Claude Code ever sees it — so this exists to name
    the rule, and to normalise the trailing slash a hand-edited store might
    carry."""
    cwd = (cwd or "").strip()
    if len(cwd) > 1:
        cwd = cwd.rstrip("/\\") or cwd[0]
    return cwd


def transcripts(projects_dir: str | None = None) -> list[str]:
    """Every session transcript on this machine, in a stable order."""
    root = projects_dir or PROJECTS_DIR
    try:
        return sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))
    except OSError:  # pragma: no cover — glob is forgiving, the dir may vanish
        return []


def backfill(projects_dir: str | None = None) -> dict[str, str]:
    """Give every existing session a task number, oldest first within each
    project. Returns `{session_id: "TASK-nnn"}` for everything it saw.

    Idempotent by construction — `ensure_ids` only ever allocates for a key that
    has none — so this is safe to run at every startup rather than being a
    migration that has to remember whether it ran. It costs one head parse per
    transcript, cached against file size, so the second run is nearly free.

    A transcript with no readable cwd is skipped rather than filed under "": it
    would otherwise pool with every other unreadable session in one nameless
    project and take numbers there.
    """
    items = []
    for path in transcripts(projects_dir):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue  # vanished mid-walk: costs that one session, not the walk
        cwd, first_ts, _prompt = head(path, size)
        if not cwd:
            continue
        session_id = os.path.splitext(os.path.basename(path))[0]
        items.append((session_id, project_of(cwd), first_ts))
    return ensure_ids(items)
