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

**A draft is the one row whose project can still change**, and it is the one
exception this rule needs. The New task modal numbers what you are typing at the
first keystroke, in whatever folder the form is pointed at THEN — and then the
person changes the folder. Allocate-once let the number ride along, so a project
counting TASK-001…015 showed a TASK-202 borrowed from the project the draft was
started in (Akshil, 2026-09-11). Nothing had been promised: a draft is not a
task yet, nobody has quoted its number in a note, and the number it shows is a
claim about which project it belongs to. So `ensure_ids(…, reproject=True)`
drops the mapping of a draft whose project has moved and allocates afresh in the
project it NOW points at. The old number is SPENT, never released (`_spend`) —
a gap in the old project, which is the same price every delete above pays. The
moment the draft becomes a real task (`rekey` onto `pending:<entry-id>`) the
number is fixed like every other, because from there on it is one the user has
been shown as booked.

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
import urllib.parse
from datetime import datetime, timezone

from fused_render._view_url_codec import canonical_fs_path

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

# What a number nobody holds any more is parked under (`_spend`). Not a task
# key and never read as one: a `(project, n)` pair is allocated exactly once, so
# this can collide with no session id, no `pending:` key and no draft key.
SPENT_PREFIX = "spent:"

# Zero-padded to three, and no further: TASK-1000 is simply four digits wide.
# Padding is for a column of numbers to line up, not a limit.
_TASK_WIDTH = 3
_MSG_WIDTH = 3


def pending_key(entry_id: str) -> str:
    """The task key for a scheduled message that has not run yet."""
    return PENDING_PREFIX + entry_id


def pending_entry(key: str) -> str:
    """`pending_key` read backwards: the entry id inside a `pending:<entry-id>`
    task key, and "" for a key that is a session id.

    The inverse exists because the entry id is the one name a queued task has
    that NEVER MOVES — the key itself rekeys onto the session the moment the
    leader's run mints one (§5) — so every client gesture aimed at a waiting
    chat (open it, skip it, cancel it) has to be able to name the entry rather
    than the row. Spelled here, beside the forward rule, so the prefix is
    written once."""
    key = str(key or "")
    return key[len(PENDING_PREFIX):] if key.startswith(PENDING_PREFIX) else ""


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
    out = {"project": project if isinstance(project, str) else "", "n": n}
    # SPENT rides along, read-only: a rekey whose target already had a number
    # stamps the OLD key this way instead of leaving it looking exactly like a
    # live reservation (see `_apply_rekey`). `task_ids()` is the one place that
    # answer has to reach — a caller deciding whether a `new:<file>` key is
    # still owed a settle pass (routers/tasks.py `_settle_new_chats`) — so it
    # is carried through here rather than filtered out.
    if rec.get("spent"):
        out["spent"] = True
        moved_to = rec.get("moved_to")
        if isinstance(moved_to, str) and moved_to:
            out["moved_to"] = moved_to
    return out


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


def erased(key: str = "") -> set[str]:
    """Every key whose number is a RESERVATION rather than a live task — the
    records `forget_session` stamped when the session behind them was erased.

    Split out rather than carried on `task_ids()` because the two answers are
    read for opposite reasons: `task_ids()` is "what number does this wear, and
    in which project", and every caller of it wants a reserved number to keep
    answering that (the number must never be reissued, whatever became of the
    session). This is the other question — "is there still anything behind it" —
    and exactly one caller asks it: the tasks listing, deciding whether a draft
    bound to a session may still stand in behind it rather than becoming an
    ordinary row of its own (routers/tasks.py `_draft_rows`, review
    2026-09-12).

    `key` narrows it to one lookup; the default answers for the whole store."""
    store = load_state(TASK_IDS_FILE)
    keys = [key] if key else list(store)
    return {k for k in keys
            if isinstance(store.get(k), dict) and store[k].get("erased")}


def _counter(project: str) -> str:
    """The name of the counter a project's numbers come out of: its canonical
    spelling (`canonical_fs_path` — forward slashes on a drive path, unchanged
    on POSIX).

    ONE FOLDER, ONE COUNTER, HOWEVER IT WAS SPELLED. A task's project reaches
    this store by two roads: a transcript's `cwd`, written by Claude Code in the
    OS's own spelling, and a scheduled entry's `target`, which the router ran
    through `os.path.abspath` — and on Windows those two spell the same folder
    with different slashes. Keyed on the raw string, each spelling had a counter
    of its own and a queued chat's row and the row holding its folder were both
    TASK-001 (Windows CI, PR #1124). The record still stores the project as it
    was given; only the counter is looked up by the canonical name, so a store
    written before this rule counts on unchanged.

    A guessed project (the lossy directory-name decode) mints no number at all
    under the queue (`routers/tasks.py::_numbers`), so no counter is keyed on
    the wrong spelling in the first place."""
    return canonical_fs_path(project or "")


def _next_numbers(store: dict) -> dict[str, int]:
    """project (canonical, see `_counter`) -> highest number allocated in it.
    "Max seen plus one" is the allocation rule precisely so a deleted task's
    number is never handed out again: counting live tasks would recycle it."""
    high: dict[str, int] = {}
    for key in list(store):
        rec = _record(store, key)
        if rec is None:
            continue
        counter = _counter(rec["project"])
        if rec["n"] > high.get(counter, 0):
            high[counter] = rec["n"]
    return high


def _spend(store: dict, rec: dict) -> None:
    """Park a number nobody holds any more where nothing will hand it out again.

    Releasing a number is the one thing allocate-once forbids, and a record
    simply DELETED would release it: `_next_numbers` reads the high-water mark
    straight off this store, so dropping the highest record in a project hands
    that number to the next task there — the exact renumber `rekey` refuses to
    cause and `forget_session` keeps a reservation to avoid. The reservation
    here is the same idea under a key that is not a task's: the mark stands, the
    number becomes a gap, and nothing joins a row onto it (Akshil, 2026-09-11).
    """
    store[SPENT_PREFIX + "%s#%d" % (rec["project"], rec["n"])] = dict(rec)


def _apply_rekey(store: dict, old: str, new: str) -> tuple[bool, bool]:
    """Move `old`'s number onto `new` in `store`, in place. Returns
    `(moved, changed)`: `moved` is whether the number's OWNER actually changed
    hands; `changed` is whether the store was written at all (stamping a
    no-op spent counts, even though nothing moved).

    The number only MOVES onto a key that has none. Two pending occurrences of
    one recurring message can chain into the same session, or a `new:<file>`
    draft's send can land in a session numbered some other way first (a
    scheduled fire, a resumed session) — either way `new` already has a
    number, and `old`'s is simply SPENT: deleting it would drop the project's
    high-water mark and hand the same number out again, which is the one thing
    allocate-once forbids.

    SPENT IS STAMPED, not left verbatim (bugbot / live repro, 2026-09-15): a
    caller that reads `task_ids()` to find drafts still owed a settle pass
    (`routers/tasks.py::_settle_new_chats`) cannot tell "still live" from
    "already spent" off a bare `{project, n}` record, and re-finding the same
    already-spent key on every listing is what turned one settle into an
    unbounded notify loop. Idempotent: a key already stamped is left alone, so
    the store is written at most once per key that ever lands here — same
    posture as `forget_session`'s reservation stamp.
    """
    rec = _record(store, old)
    if rec is None:
        return False, False
    if _record(store, new) is not None:
        if rec.get("spent"):
            return False, False
        store[old] = {"project": rec["project"], "n": rec["n"],
                      "spent": True, "moved_to": new}
        return False, True
    store.pop(old, None)
    store[new] = rec
    return True, True


def ensure_ids(items, rekeys=(), reproject=False) -> dict[str, str]:
    """Numbers for `items`, allocating any that are missing.

    `items` is an iterable of `(key, project, order)`: the task key (a session
    id, or `pending:<entry-id>`), the project folder it belongs to, and a sort
    key — the session's first timestamp — that decides the order new numbers are
    handed out in. Returns `{key: "TASK-nnn"}` for every item.

    `rekeys` is an iterable of `(old, new)` applied first, in the same lock, so
    a session that has just minted its id keeps the number its pending row was
    already showing rather than being renumbered by the allocation below it.

    `reproject` is the DRAFT rule (see the module docstring): with it on, an
    item whose stored record names a different project than the one passed has
    that record spent and a new number allocated in the project it now names.
    Only the draft listing passes it — `routers/tasks.py::_draft_numbers` — and
    a `pending:` key is skipped even there, because a booked task's number is
    one the user has already been shown as final. Off by default, so every other
    caller keeps the plain allocate-once behaviour.

    Idempotent: an item that already has a number in the project it is passed
    under is left exactly as it is, so calling this on every listing costs one
    read and no write once the store has caught up.
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
            _moved, this_changed = _apply_rekey(store, old, new)
            changed = changed or this_changed

        if reproject:
            for key, project, _order in items:
                # Never a booked row: `pending:<entry-id>` is a scheduled task
                # whose number the user has been watching, and moving THAT is
                # the renumber schedule.py's `replaces` rekey exists to prevent.
                if key.startswith(PENDING_PREFIX):
                    continue
                # A draft whose target has been CLEARED names no project, and
                # "somewhere else" is not somewhere: it keeps the number it has
                # until it points at a folder again, which costs nothing and
                # saves a number every time a half-typed path resolves to a
                # different parent on its way to the real one.
                if not project:
                    continue
                rec = _record(store, key)
                if rec is None or _counter(rec["project"]) == _counter(project):
                    continue
                _spend(store, rec)
                store.pop(key, None)
                changed = True

        high = _next_numbers(store)
        missing = [it for it in items if _record(store, it[0]) is None]
        # Sorted by first-timestamp so a backfill numbers a project's history in
        # the order it happened; the key breaks ties so two sessions that start
        # in the same millisecond still number deterministically.
        missing.sort(key=lambda it: (it[2] if it[2] is not None else 0.0, it[0]))
        for key, project, _order in missing:
            counter = _counter(project)
            n = high.get(counter, 0) + 1
            high[counter] = n
            store[key] = {"project": project, "n": n}
            changed = True

        out = {}
        for key in [it[0] for it in items] + [new for _old, new in rekeys]:
            rec = _record(store, key)
            if rec is not None:
                out[key] = format_task_id(rec["n"])
        return out, changed

    return _update(TASK_IDS_FILE, mutate)


def stored_number(store: dict, key: str) -> str:
    """The number `key` already holds in a `task_ids()` snapshot, or "" — a
    READ, never an allocation. For the caller that must not mint (a task whose
    project is only guessed, `routers/tasks.py::_numbers`) but must still show
    the number a task was given before."""
    rec = _record(store, str(key or ""))
    return format_task_id(rec["n"]) if rec is not None else ""


def rekey(old: str, new: str) -> str:
    """Move a task's number from `old` to `new` — the pending row's key to the
    session id its first run minted (§5). Returns the number `new` ends up with,
    or "" if there was nothing to move and nothing already there.

    Never renumbers: if `new` already holds a number (the second occurrence of a
    recurring message chaining into a session that already ran one), that number
    stands and `old`'s is dropped."""
    return ensure_ids([], rekeys=[(old, new)]).get(new, "")


def rekey_moved(old: str, new: str) -> bool:
    """Like `rekey`, but answers the one thing its callers have never needed:
    did the number actually change hands, or was `new` already numbered (in
    which case nothing about the listing changed and `old` was only stamped
    spent)?

    `_settle_new_chats` needs this to stay idempotent — a settle pass that
    calls `notify()` every time it re-finds an already-settled key turns one
    move into an unbounded loop (bugbot / live repro, 2026-09-15).
    `schedule.spend_chat_draft` needs it for the mirror-image reason: the key it
    is handed usually has no number at all (a session-less composer autosaves
    nothing, so there is no `new:<file>` record to be numbered), and announcing
    a move that did not happen would put a `gone` on every ordinary new-chat
    send. The remaining rekey call sites fire and forget."""
    old, new = str(old or ""), str(new or "")
    if not old or not new or old == new:
        return False
    result = {"moved": False}

    def mutate(store: dict):
        moved, changed = _apply_rekey(store, old, new)
        result["moved"] = moved
        return None, changed

    _update(TASK_IDS_FILE, mutate)
    return result["moved"]


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


# ------------------------------------------------------------ deleted.json
#
#     {"<session-id-or-pending-key>": {"at": 1755300000.0}}
#
# TOMBSTONES, not erasure. Deleting a task removes the ROW — the third fact
# about a task that cannot live in a transcript (after its number and its read
# marks), and it lives here for the same reasons: keyed by session, global,
# never branch-nested. The transcript itself is Claude Code's and is not
# touched (D306: this app does not destroy transcripts); what is stored is the
# user's decision that the row stops being shown, with the WHEN, because the
# when is what makes revival decidable: activity NEWER than the tombstone —
# a fresh message in the conversation, a run scheduled into it afterwards —
# brings the row back rather than running invisibly behind a hidden task,
# which is the same promise the archive cascade makes (`_revived` next door).
# The router owns that comparison (`_deleted` in routers/tasks.py); this
# module only keeps the record, exactly as it does for numbers and reads.
#
# A tombstone whose task never revives is a few bytes forever, and that is the
# correct price — the same "gaps over renumbering" trade task_ids.json makes.

DELETED_FILE = "deleted.json"


def deleted_state() -> dict:
    """The tombstone store, as saved. Missing/corrupt reads as {} — nothing
    deleted — like every other store here: degrading means rows come BACK,
    never that they vanish."""
    return load_state(DELETED_FILE)


def deleted_at(state: dict, key: str) -> float:
    """When this key was deleted, or 0.0 — which every real timestamp beats,
    so an unreadable record can never hide a task."""
    rec = state.get(str(key))
    if not isinstance(rec, dict):
        return 0.0
    try:
        at = float(rec.get("at"))
    except (TypeError, ValueError):
        return 0.0
    return at if at > 0 else 0.0


def mark_deleted(key: str, now: float | None = None) -> None:
    """Tombstone one task key. Idempotent by intent: deleting twice re-stamps
    the WHEN, which is what the second gesture means — hide it as of now,
    including from any activity that revived it in between."""
    stamp = time.time() if now is None else float(now)

    def mutate(state: dict):
        state[str(key)] = {"at": stamp}
        return None, True

    _update(DELETED_FILE, mutate)


# -------------------------------------------------- session_settings.json
#
#     {"<session-id>": {"model": "haiku", "effort": "low", "at": 1755300000.0}}
#
# WHICH CLAUDE A CONVERSATION RUNS WITH, and how hard it thinks — the fourth
# fact about a task that cannot live in a transcript, after its number, its read
# marks and its tombstone, and it lives here for the same reasons: keyed by
# session, global, never branch-nested.
#
# WHY OURS AND NOT THE TRANSCRIPT'S. The composer used to DETECT both by reading
# Claude Code's transcripts: the model off `message.model`, which every
# assistant row carries, and the effort off a top-level `effort` key, which
# Claude Code writes only sometimes. A field the writer does not reliably write
# is a field that reads as missing, and a missing field used to be filled in
# from the newest OTHER chat in the same folder — so a task set up with
# haiku/low opened on some neighbour's max (Akshil, 2026-09-18: "made a task
# with haiku/low; peek first showed fable/max"). Detection also cannot answer
# at all in the seconds between "this conversation has an id" and "this
# conversation has a transcript", which is exactly when a new task's peek is
# first read.
#
# So the app records what it launched a run with, and what the reader picked,
# at the moment it knows — `agent._start` and `_send` for every spawn and every
# send, `POST /api/tasks/settings` for every pill pick — and every surface reads
# THAT. Transcript scanning stays as the legacy fallback for conversations that
# predate this store.
#
# PER FIELD, and a missing one stays missing. `record` writes only what it was
# given, so a pick that names the effort cannot wipe a model recorded at spawn.
# Nothing here ever answers about a DIFFERENT session: a field this store has
# no value for reads as "", the caller's own constant default speaks, and the
# reader is never told about a conversation they did not ask about.
#
# A record for a session whose transcript is later erased goes with it
# (`forget_session`) — there is no conversation left for it to be about.

SETTINGS_FILE = "session_settings.json"


def settings_state() -> dict:
    """The per-session model/effort store, as saved. Missing/corrupt reads as
    {} — no record anywhere, so every chat falls back to detection, which is
    precisely how the app behaved before this file existed."""
    return load_state(SETTINGS_FILE)


def session_settings(state: dict, session_id: str) -> tuple[str, str]:
    """`(model, effort)` recorded for one session, "" for each field this store
    has no answer for.

    Strings only, and no vocabulary check here: the store keeps what the app
    launched with, and the two readers that turn it into a selected pill
    (`agent._defaults`, the composer's own `pick`) each validate against the
    list THEY offer. A value this module rejected would be a value the CLI
    really ran with that the app then denies knowing."""
    rec = state.get(str(session_id or ""))
    if not isinstance(rec, dict):
        return "", ""
    return (str(rec.get("model") or ""), str(rec.get("effort") or ""))


def record_settings(session_id: str, model: str = "", effort: str = "",
                    now: float | None = None) -> dict:
    """Record what this conversation runs with; return the stored record.

    ONLY THE FIELDS GIVEN. An empty `model` means "I am not saying anything
    about the model", not "the model is nothing" — a pill pick names one field,
    a spawn names both, and neither may erase what the other knew. That is the
    same invariant `mark_read_many` keeps for the ids it was handed.

    Writes nothing for an empty session id: a conversation with no identity has
    nothing to key a record on, and a `""` key would be a record every future
    id-less caller overwrote in turn. The task entry's own setting is what
    speaks for that window (`routers/tasks.py::_run_settings`)."""
    session_id = str(session_id or "").strip()
    model = str(model or "").strip()
    effort = str(effort or "").strip()
    if not session_id or not (model or effort):
        return {}
    stamp = time.time() if now is None else float(now)

    def mutate(state: dict):
        rec = state.get(session_id)
        rec = dict(rec) if isinstance(rec, dict) else {}
        if model:
            rec["model"] = model
        if effort:
            rec["effort"] = effort
        rec["at"] = stamp
        state[session_id] = rec
        return rec, True

    return _update(SETTINGS_FILE, mutate)


def forget_session(session_id: str) -> dict:
    """Erase what these stores keep about one session — the erase gesture's
    share of `POST /api/tasks/erase`, where the transcript itself goes too.

    `read.json`'s record GOES: it is per-message read marks for messages that
    no longer exist, and there is no thread left for them to be about.

    `session_settings.json`'s record GOES for the same reason: it says which
    model a conversation runs with, and the conversation is gone. Left behind,
    it would be the one thing that outlived the erase and re-seeded a new chat
    that happened to be handed the same id.

    `task_ids.json`'s record STAYS, deliberately, and this is the one decision
    in here worth arguing. Allocation is "max n seen for this project, plus
    one" (`_next_numbers`) read straight off this store, so the record IS the
    ledger: removing it would hand TASK-007 to the next task somebody starts,
    and a number the user has quoted in a note or a message must keep meaning
    the same thing forever. So the mapping is left in place as a RESERVATION
    and only stamped `erased` — gaps over renumbering, exactly the trade the
    module docstring makes for deletes. `erased()` is the one reader of the
    stamp — the listing asks it whether a draft still bound to this session may
    go on wearing its number — and it is legible in the file besides, so a human
    reading the store can tell a reserved number from a live one.

    Returns `{"read": bool, "settings": bool, "number": bool}` — whether each
    store changed."""
    def forget_read(state: dict):
        if session_id not in state:
            return False, False
        state.pop(session_id, None)
        return True, True

    def forget_settings(state: dict):
        if session_id not in state:
            return False, False
        state.pop(session_id, None)
        return True, True

    def reserve_number(store: dict):
        rec = store.get(session_id)
        if not isinstance(rec, dict) or rec.get("erased"):
            return False, False
        rec["erased"] = True
        store[session_id] = rec
        return True, True

    return {"read": _update(READ_FILE, forget_read),
            "settings": _update(SETTINGS_FILE, forget_settings),
            "number": _update(TASK_IDS_FILE, reserve_number)}


# ------------------------------------------------------------- transcript head
#
# Only the head, and only the three facts a backfill needs. The full read of a
# transcript belongs to the router (routers/tasks.py) and is cached there; this
# is what makes `backfill()` cheap enough to run at startup on a machine with a
# few thousand sessions.

# path -> (size_at_parse, cwd, first_ts, first_prompt, pane_file). Same cache
# shape, and the same append-only reasoning, as claude_sessions._HEAD_CACHE.
_HEAD_CACHE: dict[str, tuple[int, str | None, float | None, str, str, bool]] = {}

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

_MACHINERY_STRIP = ("live-app-state", "pane-shot", "annotations")

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

# The annotation notes as they are written TODAY: an `<annotations>` block of
# markdown stanzas, one per pin. It needs no strip code of its own — the tag is
# in `_MACHINERY_STRIP` above and `_LEADING_BLOCK` peels it like the other two —
# but `ann_notes` still has to read the user's words back out of it, and prose
# has no `content` key to ask for. Hence the shape rules below, which are the
# ones `formatAnnotations` writes and nothing else:
#
#   * stanzas are separated by a blank line, and the writer collapses any blank
#     line INSIDE a note so that boundary is unambiguous (the composer commits
#     on Enter and takes a newline on Shift+Enter, so a note really can hold
#     one);
#   * the FIRST paragraph is the block's own preamble, and it is the only one
#     that does not open with `**A** — ` (every stanza opens with its label);
#   * inside a stanza the first line is that heading, the no-badge caveat and
#     the no-words placeholder are OURS, and what is left is what the user said.
#
# The two machine lines are matched EXACTLY rather than as "any wholly-italic
# line": a note that is one emphasised word is a note, and dropping it as prose
# of ours loses the row's whole name.
#
# Anything that does not match is answered "" rather than guessed at — a row
# named with a fragment of our own preamble is worse than one named by its id.
_ANN_TAG = "annotations"
_ANN_BLOCK = re.compile(r"<%s>(.*?)</%s>" % (_ANN_TAG, _ANN_TAG), re.DOTALL)
_ANN_STANZA_HEAD = re.compile(r"^\*\*(.+?)\*\* — ")
_ANN_NO_WORDS = "_(no words for this spot)_"
_ANN_OFFSCREEN = re.compile(r"^_no badge on the overview: .+_$", re.DOTALL)

# The annotation notes as they were written BEFORE that tag existed: one opening
# sentence, a paragraph of field notes for the model, and a fenced json payload.
# No tag at all, so it is recognised by its prose at position ZERO — which is
# exactly the fragility the tag was added to end. Sessions already on disk carry
# this shape forever, so both readers stay (the same permanent obligation
# `pane_shot`'s bare-object form carries).
_ANN_PREAMBLE = "The user annotated "
_ANN_FENCE_OPEN = "\n```json\n"
_ANN_FENCE_CLOSE = "\n```"


def _ann_notes_md(block: str) -> str:
    """The user's words from one `<annotations>` block's stanzas, joined.

    Flattened with spaces, unlike the page's own reader (which rejoins with
    newlines): this builds a row TITLE, and a title is one line."""
    notes = []
    for para in re.split(r"\n\s*\n", block.strip()):
        lines = [ln.strip() for ln in para.strip().splitlines() if ln.strip()]
        if not lines or not _ANN_STANZA_HEAD.match(lines[0]):
            continue            # the preamble paragraph, or something we did not write
        said = [ln for ln in lines[1:]
                if ln != _ANN_NO_WORDS and not _ANN_OFFSCREEN.match(ln)]
        if said:
            notes.append(" ".join(said))
    return " · ".join(notes)


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


def ann_notes(text: str) -> str:
    """The words the user typed INSIDE their annotations, for a send that
    carried no free text at all — or "" when there are none.

    THE THIRD COPY of an annotation rule (`template.html`'s `stripAnnBlock`,
    `agent.py`'s `_ann_notes`, this) and pinned to the second one over the
    shared corpus by `test_claude_sessions_merged.py`, for the reason D166
    forces on every one of these: a template may not import `fused_render`, and
    four readers that stopped agreeing about machinery is the bug this whole
    family of functions was written to end.

    Deliberately NOT part of `strip_machinery`, which answers a different
    question — "what did the human type in the composer" — and is asked by
    `is_machinery` to decide whether a record is worth keeping at all. This is a
    SECOND source, consulted only where an empty answer costs a row its name:
    since annotations became sendable with an empty message, the notes on the
    pins ARE what the user wrote, and no reader was showing them.
    """
    out = (text or "").strip()
    # TODAY'S shape first, and searched rather than anchored: the tag made this
    # block position-independent, so it is found wherever the send put it — no
    # peeling loop needed to expose it, unlike the untagged form below.
    found = _ANN_BLOCK.search(out)
    if found:
        return _ann_notes_md(found.group(1))
    while True:
        match = _LEADING_BLOCK.match(out)
        if not match:
            break
        out = out[match.end():].strip()
    if not out.startswith(_ANN_PREAMBLE):
        return ""
    open_at = out.find(_ANN_FENCE_OPEN)
    if open_at == -1:
        return ""
    close_at = out.find(_ANN_FENCE_CLOSE, open_at + len(_ANN_FENCE_OPEN))
    if close_at == -1:
        return ""
    try:
        pins = json.loads(out[open_at + len(_ANN_FENCE_OPEN):close_at])
    except ValueError:
        return ""
    if not isinstance(pins, list):
        return ""
    notes = []
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        note = pin.get("content")
        if isinstance(note, str) and note.strip():
            notes.append(note.strip())
    return " · ".join(notes)


# ---------------------------------------------------------- the wordless send
#
# A send can carry no typed words AT ALL and still be something the user did:
# annotations with nothing written on them, or a screenshot on its own. The
# client already has a vocabulary for exactly this — `stripBlocks` in
# `frontend/src/apps/claude/protocol/wire.ts` substitutes one MARKER per block
# kind for the bubble's text — and until 2026-09-18 every Python reader answered
# such a record "" and DROPPED it, which cost the whole chat its rows on the
# Tasks page (both real sends in one reported annotation session) and, via
# `tasks.py::_status`, its status too: status is derived from the messages, so no
# messages meant nothing to derive from and the run read `done` while it ran.
#
# THE WORDS ARE THE CLIENT'S, NOT OURS. A fourth spelling of "screenshot" would
# be a fourth thing to keep in step, so these are `MARKER_VIEW`/`MARKER_IMG`/
# `MARKER_FILE`/`MARKER_ANN` — pinned to the page's own copy by
# `tests/test_tasks_store.py` (D146: the duplicated rule gets a test, not a
# comment).
#
# WITHOUT THE SIGIL. Every client marker opens with U+2063 INVISIBLE SEPARATOR
# because the page needs to tell its own substitute text apart from a reader who
# genuinely typed "files"; that sigil is a private token of the page's display
# layer and is never put on the wire. What a bubble SHOWS is `markerWord`'s
# output — the bare word — and a listing row shows the same.
MARKER_ANN = "annotations"
MARKER_VIEW = "pane screenshot"
MARKER_IMG = "images"
MARKER_FILE = "files"
#: `MARKER_JOIN` — a send that carried two kinds is named for both.
MARKER_JOIN = " + "

_PANE_SHOT_TAG = "pane-shot"
_PANE_SHOT_BLOCK = re.compile(
    r"<%s>(.*?)</%s>" % (_PANE_SHOT_TAG, _PANE_SHOT_TAG), re.DOTALL)


def _pane_shot_kinds(text: str) -> list[str]:
    """The `kind` of every entry in the `<pane-shot>` block, in order.

    The payload is the block's LAST line — a caption paragraph for the model
    comes first, and `paneShotIn` reads it exactly this way. Both the array form
    and the bare-object form parse, because a session on disk carries whichever
    shape the page wrote that year. Anything that does not parse answers `[]`,
    which is the same answer as "no `kind` field anywhere" and falls the right
    way on its own: a block we cannot read is a picture of the pane.
    """
    found = _PANE_SHOT_BLOCK.search(text or "")
    if not found:
        return []
    lines = [ln for ln in found.group(1).strip().splitlines() if ln.strip()]
    if not lines:
        return []
    try:
        payload = json.loads(lines[-1])
    except ValueError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    return [entry.get("kind") for entry in payload
            if isinstance(entry, dict) and isinstance(entry.get("kind"), str)]


def _ann_block_present(text: str) -> bool:
    """Did this send carry annotations at all — words on them or not?

    `ann_notes` answers the narrower question (what the user WROTE on the pins)
    and its "" covers two different sends: one with no annotations, and one whose
    pins nobody typed a note on. Only the second is a wordless annotation send,
    and only this tells them apart. Both shapes, for `ann_notes`' reason: the
    tagged block is found wherever it sits, the legacy preamble only at position
    zero once the other leading blocks are peeled off.
    """
    out = (text or "").strip()
    if _ANN_BLOCK.search(out):
        return True
    while True:
        match = _LEADING_BLOCK.match(out)
        if not match:
            break
        out = out[match.end():].strip()
    return out.startswith(_ANN_PREAMBLE) and _ANN_FENCE_OPEN in out


def carried_words(text: str) -> str:
    """What a send with NO typed words carried, named the way the chat names it
    — "pane screenshot", "annotations", "images", "files", or two joined by
    `" + "` — or "" for a send that carried none of them.

    `stripBlocks`' marker branch, mirrored decision for decision (wire.ts:450):
    annotations first, then the pictures, and the pictures' word depends on what
    they ARE. A `kind` of "pane" or "overview" is a picture of this app taken at
    send time; "image" is a picture the user brought in from somewhere else and
    "file" is not a picture at all. So an all-"image" block is `images`, a block
    that is all brought-in but not all pictures is `files`, and anything with a
    screenshot of the pane in it — including a block too old or too broken to
    carry `kind` — is `pane screenshot`. That last default is the page's too,
    and for the same reason: `kind` postdates the pane shot, so its absence IS
    the pane case.

    Asked LAST, after the words and after the notes on the pins: a marker is a
    label for a send that said nothing, and a send that said something is named
    by what it said. See `user_words`.
    """
    carried = []
    if _ann_block_present(text):
        carried.append(MARKER_ANN)
    if _PANE_SHOT_BLOCK.search(text or ""):
        kinds = _pane_shot_kinds(text)
        brought = bool(kinds) and all(k in ("image", "file") for k in kinds)
        if not brought:
            carried.append(MARKER_VIEW)
        elif all(k == "image" for k in kinds):
            carried.append(MARKER_IMG)
        else:
            carried.append(MARKER_FILE)
    return MARKER_JOIN.join(carried)


def user_words(text: str) -> str:
    """The words to SHOW for one send, in the one order every reader wants them:
    what the human typed, else the notes they wrote inside their annotations,
    else the client's own name for what the send carried. "" only for a record
    that carried nothing a reader could name.

    ONE RULE, SPELLED ONCE, for the three readers that had drifted: this module's
    own `_parse_head`, `claude_sessions._parse_head` and `tasks.py::_prompt`. The
    first two already took the second step (`strip_machinery(raw) or
    ann_notes(raw)`); none of them took the third, so a screenshot sent with no
    words was dropped by all three.

    A reader that must not put a MARKER where a real message would do asks the
    three steps in this order but at its own precedence — the two head readers
    keep scanning for words before settling for a marker, because a row titled
    "pane screenshot" while the words that could name it sit two records further
    down is the bug this fallback exists to fix, told from the other side.
    """
    return strip_machinery(text) or ann_notes(text) or carried_words(text)


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


# ------------------------------------------------ pane file on a user record
#
# The `<live-app-state>` block the Claude page prepends to a send carries the
# pane's own URL (`"url": "/render?path=<file>"`), and that URL is the only
# durable record of WHICH FILE a chat was opened on: the transcript's `cwd` is
# always a folder (agent.py:_workdir resolves a file target to its directory
# before Claude Code ever sees it), and the per-file sidecar is on its way out
# (D335). Reading the path back out of the block is what lets "open this task"
# land on the file the user was actually looking at instead of its folder. A
# chat with no block ever — a folder chat, a terminal session — has no pane,
# and for those the folder IS the right answer, so "" here is not a failure.
_APP_STATE_LEAD = re.compile(r"<live-app-state>(.*?)</live-app-state>",
                             re.DOTALL)


def pane_file(text: str) -> str:
    """The file the LEADING `<live-app-state>` block says the pane was on,
    or "". Anchored like every machinery matcher here (`_LEADING_BLOCK`),
    because only a leading block is machinery — the tag further in may be
    something a human typed. The state is prose followed by one JSON object,
    so the object is cut from first `{` to last `}` and parsed properly
    rather than regexed; a title containing `"url":` must not win.

    Three answers, in order of honesty. `entry` is the state's own name for
    the document the pane is about (template.html `appEntry`) and wins
    outright. The url is the fallback for older blocks — and there the
    `_file` param must beat `path`, because a templated preview's url is
    `/render?path=<template>&_file=<file>`: `path` names OUR template, which
    exists on disk and would sail through the caller's isfile check as the
    target of somebody's chat about their own parquet file."""
    match = _APP_STATE_LEAD.match((text or "").lstrip())
    if not match:
        return ""
    blob = match.group(1)
    start, end = blob.find("{"), blob.rfind("}")
    if start == -1 or end <= start:
        return ""
    try:
        state = json.loads(blob[start:end + 1])
    except ValueError:
        return ""
    if not isinstance(state, dict):
        return ""
    entry = state.get("entry")
    if isinstance(entry, str) and entry:
        return entry
    url = state.get("url")
    if not isinstance(url, str):
        return ""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    for key in ("_file", "path"):
        values = query.get(key)
        if values and values[0]:
            return values[0]
    return ""


def _parse_head(path: str) -> tuple[str | None, float | None, str, str, bool]:
    cwd: str | None = None
    first_ts: float | None = None
    prompt = ""
    # The FIRST wordless send's marker ("pane screenshot"), held back as a last
    # resort — see the `carried` note in the loop below.
    carried = ""
    pane = ""
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
                        raw = first_text(msg.get("content"))
                        # The pane file rides the same records the prompt is
                        # searched in, and stops with it: the first send from
                        # a pane carries both the block and the words, so by
                        # the time the prompt resolves the pane has had its
                        # chance. Scanning on after that would trade a bounded
                        # head read for a full one on every pane-less chat.
                        if not pane:
                            pane = pane_file(raw)
                        # STRIPPED, not raw: the fused-render Claude page
                        # prepends its own blocks to what the user typed, and
                        # the raw text is how rows came to be titled
                        # `<live-app-state>`. An empty remainder is not an
                        # answer either — the loop simply carries on to the next
                        # user record, because a blank title while the message
                        # that could have named the row sits two lines further
                        # down is the same bug from the other side.
                        # The pins are the FALLBACK: a send with both words and
                        # annotations is named by the words. A send with only
                        # annotations is named by the notes on them, which is
                        # the only text in the record a human wrote (`ann_notes`).
                        prompt = strip_machinery(raw) or ann_notes(raw)
                        # …and, if this send said nothing anywhere, WHAT IT
                        # CARRIED — kept aside rather than taken, because the
                        # loop's whole point is that words two records further
                        # down name the row better than the block on record one
                        # does. Only a head that found no words at all settles
                        # for this (`carried_words`), and only the first one,
                        # which is the send the row would be named after.
                        if not prompt and not carried:
                            carried = carried_words(raw)
                if cwd is not None and first_ts is not None and prompt:
                    break
    except OSError:
        return None, None, "", "", False
    # THE FIFTH VALUE IS "IS THIS PROMPT SETTLED" (Bugbot, PR #1213). A marker is
    # what the head shows when nothing in it has said anything YET — and a
    # transcript is append-only, so the words can still arrive. Handed back as an
    # ordinary answer it let `head`'s cache call the read COMPLETE and keep "pane
    # screenshot" as the row's title for the life of the process, over every
    # later word the reader typed. The two are told apart here; the cache decides
    # what to do about it.
    return cwd, first_ts, prompt or carried, pane, bool(prompt)


def head(path: str, size: int | None = None,
         ) -> tuple[str | None, float | None, str, str]:
    """(cwd, first timestamp, first user prompt, pane file) for one
    transcript, cached per path. Transcripts are append-only, so a head that
    resolved fully stays valid however much the file grows; an incomplete one
    is retried once the file has more to offer, and a file that shrank was
    replaced. The pane file is deliberately absent from the completeness
    test: a chat with no `<live-app-state>` block has none to find, and
    re-reading it on every append to keep looking would never pay for
    itself.

    A MARKER IS NOT A SETTLED PROMPT (Bugbot, PR #1213). "pane screenshot" is
    what the head shows for a chat whose sends so far carried no words at all —
    and the very next append can carry some. Counting it complete froze it as the
    row's title for the life of the process: the reader typed, the transcript
    grew, and the listing went on calling their chat "pane screenshot". So a
    marker-only head stays INCOMPLETE and is re-read on the next append, exactly
    like a head that found nothing. The marker is still shown meanwhile; it is
    just not banked."""
    if size is None:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None, None, "", ""
    cached = _HEAD_CACHE.get(path)
    if cached is not None:
        cached_size, cwd, first_ts, prompt, pane, settled = cached
        complete = settled and first_ts is not None and cwd is not None
        if cached_size == size or (size > cached_size and complete):
            if size != cached_size:
                _HEAD_CACHE[path] = (size, cwd, first_ts, prompt, pane, settled)
            return cwd, first_ts, prompt, pane
    if len(_HEAD_CACHE) > 20000:  # unbounded only if the user has 20k sessions
        _HEAD_CACHE.clear()
    cwd, first_ts, prompt, pane, settled = _parse_head(path)
    _HEAD_CACHE[path] = (size, cwd, first_ts, prompt, pane, settled)
    return cwd, first_ts, prompt, pane


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
        cwd, first_ts, _prompt, _pane = head(path, size)
        if not cwd:
            continue
        session_id = os.path.splitext(os.path.basename(path))[0]
        items.append((session_id, project_of(cwd), first_ts))
    return ensure_ids(items)
