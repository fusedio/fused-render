"""Change detection for the Tasks page — WHICH sessions moved, and a number
that says something did.

The Tasks listing (server/routers/tasks.py) is already cheap to rebuild: every
transcript read is incremental and skipped outright when the file size has not
moved. What it never had was a *signal*. The page asked every 20 seconds, and a
session started (or resumed) in a terminal outside the app surfaced up to a poll
later. This module is that signal.

Claude Code writes two things this can watch, both verified on a real
machine (2026-08-27):

* ``~/.claude/sessions/<pid>.json`` — one file per RUNNING ``claude`` process:
  ``sessionId``, ``cwd``, ``status`` (busy / shell / waiting / idle),
  ``updatedAt``. Rewritten on every status change and deleted when the process
  exits. A resumed two-week-old session gets a file under its OLD session id.
* the transcripts themselves — but only the ones the registry says are live
  are watched here (a couple of dozen files, not the machine's whole history).
  A session nobody is running cannot grow.

`~/.claude/history.jsonl` is NOT one of them, though it was for a round: a
chat sent from this app runs ``claude -p``, and ``-p`` never appends to it.

Stat-poll on a daemon thread, once a second, rather than FSEvents/inotify:
cross-platform, no ctypes, no dropped-event semantics to reason about, and
~25 ``stat`` calls per second is nothing. A real filesystem stream can replace
``_loop`` later behind the same two exports — ``generation()`` and ``wait()`` —
without the router or the page noticing.

What no file can say in time is that a turn has JUST started. A ``claude -p``
run writes its registry row two to four seconds after the process starts, so
the listing called every one of this app's own turns "done" for its first
seconds — and a short turn for the whole of it (Akshil, 2026-09-15). The send
is the earliest signal there is, and the page that made it says so directly:
``mark_running`` (``POST /api/tasks/running``). It is a short-fused FLOOR under
the liveness reading, not a status of its own — the registry takes over the
moment it appears, and the mark expires by itself either way.

Everything here degrades to "no news": an unreadable directory, a half-written
registry file, a vanished transcript all produce no keys and no exception. The
20-second full listing is still there underneath and remains the truth.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import threading
import time

from fused_render import session_liveness, tasks_store

logger = logging.getLogger(__name__)

# CLAUDE_CONFIG_DIR wins where set — same rule, same deliberate local copy, as
# session_liveness.py and tasks_store.py. Module-level so tests can point them
# at a tmp dir.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
SESSIONS_DIR = os.path.join(CLAUDE_DIR, "sessions")

TICK_SEC = 1.0
# How long a long-poll may block. Below the 30s most proxies and the
# TestClient's default patience, above the page's own 20s full pass so the two
# do not line up.
MAX_WAIT_SEC = 25.0
# How many generations of "what changed" are remembered. A client further
# behind than this gets `None` from wait() and does a full reload.
RING = 200
# Registry statuses that mean a turn is open. `waiting` is Claude waiting on
# the user (a permission prompt, a question) — nothing is running, and the
# transcript-tail rule says the same; `idle` and a missing status are not live.
RUNNING_STATUSES = frozenset({"busy", "shell"})

_cond = threading.Condition()
_generation = 0
_changed: collections.deque = collections.deque(maxlen=RING)  # (gen, frozenset)
_registry: dict[str, dict] = {}   # session_id -> parsed sessions/<pid>.json
# session_id -> epoch when its registry row went away (process exited or died).
# A departed session is KNOWN idle: without this, a `claude -p` that ran for
# four seconds paints a running badge for the 45s tail window after it exits.
_departed: dict[str, float] = {}
_primed = False
_sess_mtimes: dict[str, tuple] = {}   # sessions/<pid>.json -> (mtime_ns, size)
_sess_sids: dict[str, str] = {}       # sessions/<pid>.json -> session_id
_tr_paths: dict[str, str] = {}        # session_id -> transcript path
_tr_sizes: dict[str, int] = {}        # session_id -> size
# session_id -> the live "a turn just started here" MARK, a record:
#
#   {"until": <epoch when it runs out>,   see `mark_running`
#    "at":    <epoch when it was made>,   the send's own moment
#    "turn":  <the client `turn` it carried, or None>,
#    "text":  <the words that were sent, "" when the caller said none>,
#    "file":  <the target path they were sent about, "" for none>}
#
# It used to be the expiry float alone. The three fields beside it are what
# makes the mark a LISTING fact rather than a spinner: the Tasks row can show
# the words the moment they are sent (`routers/tasks.py _row`), and a send into
# a session with no transcript yet can be a ROW at all (`_collect`) — placed in
# the right project, because the send says which file it was about. All of it
# dies with the mark; see `_expire_marks`.
_marks: dict[str, dict] = {}
# session_id -> the client `turn` its CURRENT mark was set with, or absent if
# that mark was set with `turn=None`. `mark_idle`'s side of the running/idle
# race (bugbot #1163, round two): `mark_running` already refuses a `turn` that
# is not newer than the last `mark_idle` saw, but nothing stopped the reverse
# — a `mark_idle` for an OLDER turn arriving after a NEWER turn's
# `mark_running` already landed, retiring a mark that has nothing to do with
# it. Compared against an incoming `mark_idle`'s `turn`; cleared whenever the
# mark it names is (a fresh `mark_running`, an expiry, a stand-down).
_mark_turns: dict[str, float] = {}
# How long a mark stands on its own. Long enough to cover the two to four
# seconds a `claude -p` takes to write its registry row (measured), short
# enough that a send whose run died on the spot — a bad model id, a refused
# permission — is not left spinning for a noticeable time.
MARK_TTL_SEC = 15.0
# How much of a sent message a mark remembers. A listing row draws ONE LINE of
# it, and the mark is in memory for fifteen seconds — so this is a ceiling on
# what a page can park in this process, not a display rule. Generous enough
# that no realistic first line is cut, small enough that a thousand marks is
# still kilobytes.
MARK_TEXT_MAX = 2000
# session_id -> was its registry row seen `busy`/`shell` (RUNNING_STATUSES)
# while ITS CURRENT mark was alive? A mark this never happened for has nothing
# to do with a registry row that goes idle or departs — that row belongs to
# whatever turn came before the send, not this one (bugbot #1163's flicker
# wore the opposite shape: `_verdict_outvotes_live` discounting a genuinely
# fresh turn's OWN row). Cleared the moment the mark itself is: a new
# `mark_running` on the same session starts this over, `_expire_marks` drops
# it with the mark it timed out on, and a stood-down mark takes it along too.
_mark_busy_seen: set[str] = set()
# session_id -> the newest client `turn` a `mark_idle` call carried. Running
# and idle are two independent POSTs (`run-controller.ts` `noteTurnRunning` /
# `noteTurnIdle`), and nothing serializes their arrival at this process — a
# short turn's idle can reach the server before its own running does. Without
# this, that late `mark_running` reads as a FRESH send and clears the
# stand-down `mark_idle` just made, leaving the row `in_progress` for the rest
# of `MARK_TTL_SEC` (bugbot #1163). `mark_running` refuses a `turn` that is not
# strictly newer than what is recorded here, on the theory that a running mark
# can never be true information about a turn a caller has already told us
# ended. Client-side awaiting closes the ordinary case; this is the net under
# it, and under the one running ping that is never awaited (`resumeAttach`'s
# untracked seat `0` — see `noteSessionId`).
#
# Nothing ever pops an entry outright — a session can always send one more
# `mark_running` to race against — so `_expire_marks` drops any entry older
# than `_LAST_IDLE_TURN_TTL_SEC` on every tick; otherwise this would keep one
# float per session ever seen for the server's whole lifetime (bugbot
# #4019069906).
_last_idle_turn: dict[str, float] = {}
# `turn` is the client's `Date.now()` — epoch milliseconds — and client and
# server share a clock: this is one local desktop app. A few multiples of
# `MARK_TTL_SEC`, comfortably longer than the running/idle race this guards,
# short enough the dict cannot grow across the server's whole lifetime.
_LAST_IDLE_TURN_TTL_SEC = MARK_TTL_SEC * 4
# run_dir -> the `perm/` directory's (mtime_ns, size) as of the last tick. See
# `_read_permission_cards`.
_perm_stamps: dict[str, tuple] = {}
# How many run dirs (newest first) the card watch stats per tick. The listing's
# own window is the same number (`routers/tasks.py _PARKED_SCAN_LIMIT`), and it
# has to be: a run the listing will not look at cannot become a parked ROW, so
# watching further back would ring about news no page could draw.
PERM_SCAN_LIMIT = 120
_started = False


# ------------------------------------------------------------------ the reads

def generation() -> int:
    with _cond:
        return _generation


def registry_row(session_id: str) -> dict | None:
    """The live-registry record for a session, or None if no `claude` process
    currently holds it."""
    if not session_id:
        return None
    with _cond:
        row = _registry.get(session_id)
        return dict(row) if row else None


def session_for_pid(pid) -> str:
    """The session id the `claude` process `pid` is holding, or `""`.

    THE REGISTRY READ BACKWARDS, and the reason it exists is a run that has not
    said who it is yet. A run dir carries the CLI's own pid from the instant the
    session host spawns it (`run_dir/pid`), but the session id only lands in the
    run dir once something polls the run — a chat whose first message was sent
    and then left alone can go a long time without one, and for all that time
    the project queue can say only "a process is starting in that folder", not
    WHICH conversation it is. Claude Code itself answers that in
    `~/.claude/sessions/<pid>.json` from the moment the CLI comes up, and this
    loop already parses every one of those rows once a second: the map is keyed
    by session with the pid INSIDE the row, so the reverse lookup is a scan of a
    map holding one entry per live `claude` on the machine.

    The file is read directly when the map has nothing to say — the watcher may
    not have ticked yet on a server seconds old, and it is the same file the
    tick would have read. The pid is checked for life on that path because a
    crashed CLI leaves its row behind; the map's rows are pruned by the tick
    itself and need no second check.
    """
    pid_s = str(pid or "").strip()
    if not pid_s.isdigit():
        return ""
    with _cond:
        for sid, row in _registry.items():
            if str(row.get("pid") or "") == pid_s:
                return sid
    try:
        with open(os.path.join(SESSIONS_DIR, pid_s + ".json"),
                  encoding="utf-8") as fh:
            row = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(row, dict) or str(row.get("pid") or "") != pid_s:
        return ""
    sid = row.get("sessionId")
    # `_pid_alive` wants the int — it trusts anything it cannot read as a pid,
    # which is the right default for the tick (the file is the authority there)
    # and the wrong one here, where an unprobed pid would name a crashed
    # session the map has already pruned.
    if not isinstance(sid, str) or not sid or not _pid_alive(int(pid_s)):
        return ""
    return sid


def live_from_registry(session_id: str,
                       transcript_mtime: float | None = None) -> tuple[bool, float] | None:
    """(running, last_active) as the registry tells it, or None to say "no
    opinion" — no process ever held the session here, or the record carries no
    status. The transcript-tail rule (session_liveness) is the fallback for None.

    A session whose process has GONE is an opinion too: not running, whatever
    the tail's timestamps say — unless the transcript was written after the
    departure, which means something unregistered is appending and the tail
    rule should decide."""
    row = registry_row(session_id)
    if not row:
        with _cond:
            gone_at = _departed.get(session_id)
        if gone_at is None:
            return None
        if transcript_mtime is not None and transcript_mtime > gone_at:
            return None
        return False, 0.0
    status = row.get("status")
    if not isinstance(status, str) or not status:
        return None
    updated = row.get("updatedAt")
    active = float(updated) / 1000.0 if isinstance(updated, (int, float)) else 0.0
    return status in RUNNING_STATUSES, active


def is_marked_running(session_id: str) -> bool:
    """Is there a live "a turn just started here" mark on this session?

    A floor under the liveness reading (`routers/tasks.py _live`), never a
    status: a registry that says `busy` is saying the same thing louder, and one
    that says `idle` is not yet entitled to be believed — the row it would flip
    to done belongs to a turn whose process has not finished announcing itself.
    """
    if not session_id:
        return False
    with _cond:
        mark = _marks.get(session_id)
    return mark is not None and mark["until"] > time.time()


def sent_marks() -> dict[str, dict]:
    """Every LIVE sent mark: ``session_id -> {"at", "text", "file"}``.

    The listing's read of this module (`routers/tasks.py` `_collect`/`_row`).
    `is_marked_running` answers the yes/no the liveness rule wants; this answers
    the other half — WHAT was sent, and WHERE — which is what lets a row show
    the words in the same poll that turns its ring on, and lets a send into a
    session with no transcript on disk yet be a row at all.

    A SNAPSHOT, and only of marks that have not run out: the caller is building
    one listing and must not see a mark expire halfway down it. Expired entries
    are left for `_expire_marks` to drop and ANNOUNCE — reaping them here would
    retire a row with no generation bump behind it, and the page would go on
    drawing a send that is over until something else moved.

    `until` and `turn` are deliberately not in the answer: the first is this
    module's own fuse and the second is the client's race token. Neither is a
    fact about the message, and a listing that read them would be deriving
    liveness a second way.
    """
    now = time.time()
    with _cond:
        return {sid: {"at": mark["at"], "text": mark["text"],
                      "file": mark["file"]}
                for sid, mark in _marks.items() if mark["until"] > now}


def wait(since: int, timeout: float = MAX_WAIT_SEC) -> tuple[int, frozenset | None]:
    """Block until the generation passes `since`, or `timeout` elapses.

    Returns ``(generation, keys)``. ``keys`` is the union of every task key that
    changed in generations ``since+1 .. generation`` — empty when the wait
    timed out with nothing new, and **None** when `since` is older than the
    ring remembers (the caller should reload everything).

    A negative `since` is a handshake — "where are we?" — answered at once with
    the current generation and no keys, so a client that has its own listing
    (the Claude page's Recent chats) can start watching without first paying
    for GET /api/tasks."""
    deadline = time.monotonic() + max(0.0, min(timeout, MAX_WAIT_SEC))
    with _cond:
        if since < 0:
            return _generation, frozenset()
        if since > _generation:
            # The client is ahead of us: this process restarted (or was
            # hot-reloaded) and counts from zero again. Its rows may be stale
            # in ways the ring cannot name — reload, don't wait (bugbot #892).
            return _generation, None
        while _generation <= since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _generation, frozenset()
            _cond.wait(remaining)
        if _changed and _changed[0][0] > since + 1:
            return _generation, None
        keys: set[str] = set()
        for gen, changed in _changed:
            if gen > since:
                keys |= changed
        return _generation, frozenset(keys)


# ----------------------------------------------------------------- the writes

def _bump(keys: set[str]) -> None:
    global _generation
    with _cond:
        _generation += 1
        _changed.append((_generation, frozenset(keys)))
        _cond.notify_all()


def notify(keys: set[str] | None = None) -> None:
    """Announce a change from outside the watcher — the read/archive/delete
    endpoints call this so the page they were called from (and every other
    window) sees the row flip without waiting for a tick."""
    _bump(set(keys or ()))


def mark_running(session_id: str, ttl_sec: float = MARK_TTL_SEC,
                 turn: float | None = None, text: str = "",
                 file: str = "") -> None:
    """Say that a turn just started on this session, and announce it.

    The client calls this the moment it sends (`POST /api/tasks/running`),
    which is earlier than anything Claude Code writes — see the module note. The
    bump is what makes the change-poll wake: without it the ring would be right
    and still a listing behind.

    Re-marking an already-marked session just moves the expiry, so a client that
    says it twice costs one extra bump and nothing else. A NEW mark is a new
    turn, so any stand-down bookkeeping the last one left behind — a
    corroborating `busy` sighting — goes with it; nothing about how the
    previous turn ended is entitled to an opinion about this one
    (test_an_older_turns_row_departing_does_not_retire_a_fresh_mark).

    `turn` (the client's `Date.now()` at send) is compared against
    `_last_idle_turn`: a value that is not strictly newer than the last
    `mark_idle` this session saw is a running POST that lost the race to its
    OWN turn's idle POST (running and idle are independent fetches; nothing
    orders their arrival here) — a stale echo, not a new send, and it is
    dropped whole: no mark, no bump, no touching `_mark_busy_seen`.
    `turn=None` (a caller with nothing to compare, or a test) always proceeds,
    exactly as if `_last_idle_turn` had nothing on file for it.

    Records `turn` in `_mark_turns` — the floor `mark_idle` measures a LATER
    idle call against, so a follow-up turn's mark cannot be retired by an
    idle that names the turn before it.

    `text` is the words that were sent and `file` the target they were sent
    about. Both optional, both purely DESCRIPTIVE — nothing about liveness
    reads them — and both kept only for as long as the mark is (`sent_marks`,
    `_expire_marks`). They are what the Tasks row draws while the transcript is
    still being written, and what places a brand-new chat's placeholder row in
    the right project. `text` is capped at `MARK_TEXT_MAX`; a caller with
    nothing to say passes neither and gets the mark this always was."""
    if not session_id:
        return
    now = time.time()
    with _cond:
        if turn is not None:
            last_idle_turn = _last_idle_turn.get(session_id)
            if last_idle_turn is not None and turn <= last_idle_turn:
                return
        _marks[session_id] = {
            "until": now + max(0.0, ttl_sec),
            "at": now,
            "turn": turn,
            "text": str(text or "")[:MARK_TEXT_MAX],
            "file": str(file or ""),
        }
        if turn is not None:
            _mark_turns[session_id] = turn
        else:
            _mark_turns.pop(session_id, None)
        _mark_busy_seen.discard(session_id)
    _bump({session_id})


def mark_idle(session_id: str, turn: float | None = None) -> None:
    """Say that a turn just ENDED on this session, and announce it.

    The other half of `mark_running` (`POST /api/tasks/idle`): the page that
    sent a turn is also the first to know it landed — the final result, a
    stop, an error — which is sooner than a registry row disappearing and far
    sooner than the mark's own TTL. Retiring the mark here is what makes a
    three-second turn read `done` in about a second instead of wearing a
    running ring for the rest of `MARK_TTL_SEC`; the TTL remains the safety
    net for a page that never gets to call this (closed tab, lost network).

    Everything the mark carried goes with it — the words, the target, the
    corroborating sighting, the client turn — because all of it described THAT
    send and none of it outlives the send (`_marks`).

    Idempotent and announced whether or not a mark was actually standing —
    the caller is reporting a fact about the TURN, not asking whether the
    watcher had an opinion, and a duplicate or late call must cost one bump
    and nothing else, the same contract `mark_running` keeps.

    Records `turn` in `_last_idle_turn` (keeping the newer of the two, in case
    a stale idle call ever arrives out of order itself) so a `mark_running`
    that shows up afterward claiming that turn or an earlier one is recognized
    as the late half of the turn THIS call already closed, not a new one —
    see `mark_running`.

    `turn` is ALSO compared against `_mark_turns`, the reverse of that same
    race: `noteTurnIdle` awaits its seat's `mark_running` POST before firing,
    which delays the stand-down rather than ordering it, so a FOLLOW-UP turn's
    `mark_running` can still land first. Without this check, this call would
    retire that newer mark — a stale idle standing down a turn that has not
    happened yet — and the row would read `done` until the registry (or that
    turn's own eventual `mark_idle`) caught up. A `turn` that is older than the
    mark currently standing is dropped whole for `_marks`/`_mark_busy_seen`
    (the live mark is left exactly as it was); it still updates
    `_last_idle_turn` when it is the newer value there, so a `mark_running`
    later claiming that same stale turn is refused by `mark_running`'s own
    check, and it does not bump — nothing observable changed."""
    if not session_id:
        return
    with _cond:
        if turn is not None:
            mark_turn = _mark_turns.get(session_id)
            if mark_turn is not None and turn < mark_turn:
                prev = _last_idle_turn.get(session_id)
                if prev is None or turn > prev:
                    _last_idle_turn[session_id] = turn
                return
        _marks.pop(session_id, None)
        _mark_turns.pop(session_id, None)
        _mark_busy_seen.discard(session_id)
        if turn is not None:
            prev = _last_idle_turn.get(session_id)
            if prev is None or turn > prev:
                _last_idle_turn[session_id] = turn
    _bump({session_id})


def _expire_marks(now: float) -> set[str]:
    """Session ids whose mark has just run out — CHANGED KEYS, because they are.

    A mark going away is the moment a row stops being running on our say-so, and
    no byte on disk marks it. Announced once: the id is dropped here, so the
    next tick has nothing left to expire. The words and the target it carried go
    with it — which is also what retires a PLACEHOLDER row built out of nothing
    else (`routers/tasks.py` `_collect`): a send whose run died on the spot and
    wrote no transcript leaves no row behind, because nothing happened.

    Also evicts any `_last_idle_turn` entry old enough
    (`_LAST_IDLE_TURN_TTL_SEC`) that nothing still racing against it could
    plausibly arrive — that dict has no other way to shrink (bugbot
    #4019069906): nothing pops an entry outright, since a session can always
    send one more `mark_running` to compare against."""
    with _cond:
        gone = {sid for sid, mark in _marks.items() if mark["until"] <= now}
        for sid in gone:
            del _marks[sid]
            _mark_turns.pop(sid, None)
            _mark_busy_seen.discard(sid)
        stale_cutoff = (now * 1000.0) - (_LAST_IDLE_TURN_TTL_SEC * 1000.0)
        stale = [sid for sid, turn in _last_idle_turn.items() if turn < stale_cutoff]
        for sid in stale:
            del _last_idle_turn[sid]
    return gone


def _note_registry_status(sid: str, status: object) -> None:
    """A registry sighting for `sid` — a fresh reparse, or a departure passing
    `status=None`. Corroborates an active mark when the status is a running
    one (`busy`/`shell`); retires the mark when it is not, but ONLY if a
    running status was already seen for it while THIS mark was alive.

    That qualifier is the whole fix (bugbot #1163's flicker, the opposite
    shape of this one): a registry row that was already `idle`, or gone,
    before the mark existed belongs to the turn before this send and has
    nothing to say about it — only a row this mark can point to and say "that
    was me, and now it isn't" is allowed to stand it down early. Without it,
    `test_an_older_turns_row_departing_does_not_retire_a_fresh_mark` would see
    a brand-new mark wiped out by the PREVIOUS turn's process finally being
    reaped."""
    running = isinstance(status, str) and status in RUNNING_STATUSES
    with _cond:
        if running:
            if sid in _marks:
                _mark_busy_seen.add(sid)
            return
        if sid in _mark_busy_seen:
            _mark_busy_seen.discard(sid)
            _marks.pop(sid, None)
            _mark_turns.pop(sid, None)


# --------------------------------------------------------------- one tick

def _pid_alive(pid) -> bool:
    """Is there a process with this pid? Asked once a second per live session,
    so it must be a syscall, not a subprocess — and on Windows it must not be
    `os.kill(pid, 0)`, which there is TerminateProcess, not a probe (bugbot,
    PR #892; the same trap envinstall._pid_alive documents)."""
    if not isinstance(pid, int) or pid <= 0:
        return True  # no pid to check: trust the file
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but not ours (EPERM), or a platform without kill
    return True


def _pid_alive_windows(pid: int) -> bool:
    """OpenProcess + GetExitCodeProcess: STILL_ACTIVE means alive. Any failure
    to ask answers True — a probe that cannot run must not un-badge a session."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # no such process (or one we may not even look at)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — a broken probe is "no opinion", not "dead"
        return True


def _wake_schedule() -> None:
    """Tell the scheduler a session may just have freed.

    THIS IS THE "wake, not wait" half of the queue. This loop already stats the
    live registry once a second, so it learns that a run stopped — status left
    RUNNING_STATUSES, the row departed, the pid died — long before the
    scheduler's own 30-second timer would. Without the ring the next queued
    task starts up to half a minute after the one in front of it finished,
    which reads as a queue that is not moving.

    A HINT, never a mechanism: `schedule.wake` only shortens the wait, every
    rule about what fires stays in `schedule.tick`, and a ring that finds
    nothing costs one early pass. Imported inside the function because the
    scheduler reaches this module the other way round (through
    `project_queue`), so a module-level import would close the cycle.

    NOT gated on the project-queue flag (Akshil, 2026-09-16): the queue is the
    one-task-per-folder RULE, and this is a sync improvement — the scheduler's
    own per-session hold (a follow-up waiting for the turn in front of it) ends
    on the same event, and before this ring it waited out the 30-second poll
    with the flag off too."""
    try:
        from fused_render import schedule

        schedule.wake()
    except Exception:  # noqa: BLE001 — a watcher must outlive any one bad ring
        pass


def _read_registry() -> set[str]:
    """Reconcile `sessions/*.json` with `_registry`; return the session ids
    whose record appeared, changed, or went away.

    Rings the scheduler once (`_wake_schedule`) when any session STOPPED
    running in this pass — the row went away, the pid died, or the status left
    RUNNING_STATUSES. That transition is what the project queue is waiting for,
    and this is the loop that sees it first."""
    keys: set[str] = set()
    stopped = False
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        names = []
    seen: set[str] = set()
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESSIONS_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        # mtime AND size: a rewrite inside the filesystem's timestamp
        # granularity still changes what it says.
        mtime = (st.st_mtime_ns, st.st_size)
        seen.add(path)
        if _sess_mtimes.get(path) == mtime:
            # Unchanged file — but a crash or a kill leaves the file behind
            # untouched, so the pid is asked every tick, not only on rewrite
            # (bugbot, PR #892). One kill(pid, 0) per live session.
            sid = _sess_sids.get(path)
            if sid:
                with _cond:
                    pid = (_registry.get(sid) or {}).get("pid")
                if not _pid_alive(pid):
                    _sess_sids.pop(path, None)
                    with _cond:
                        _registry.pop(sid, None)
                        _departed[sid] = time.time()
                    _tr_paths.pop(sid, None)
                    _tr_sizes.pop(sid, None)
                    _note_registry_status(sid, None)
                    keys.add(sid)
                    stopped = True
            continue
        _sess_mtimes[path] = mtime
        try:
            with open(path, "r", encoding="utf-8") as f:
                row = json.load(f)
        except (OSError, ValueError):
            continue  # half-written: the next rewrite bumps mtime again
        if not isinstance(row, dict):
            continue
        sid = row.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        old_sid = _sess_sids.get(path)
        with _cond:
            was = (_registry.get(sid) or {}).get("status")
        if not _pid_alive(row.get("pid")):
            # A crashed claude leaves its file behind; a dead pid is not a
            # live session, and must not paint a running badge forever. The
            # mtime stays recorded so the file is not re-read every tick.
            if old_sid:
                _sess_sids.pop(path, None)
                with _cond:
                    _registry.pop(old_sid, None)
                    _departed[old_sid] = time.time()
                _note_registry_status(old_sid, None)
                keys.add(old_sid)
                stopped = True
            continue
        if old_sid and old_sid != sid:
            _registry.pop(old_sid, None)
            _note_registry_status(old_sid, None)
            keys.add(old_sid)
            stopped = True
        _sess_sids[path] = sid
        with _cond:
            _registry[sid] = row
            _departed.pop(sid, None)
        _note_registry_status(sid, row.get("status"))
        keys.add(sid)
        # busy/shell -> anything else: the turn ended, and whatever was queued
        # behind that folder can go now. `waiting` counts as stopped on purpose
        # — a run parked on a card holds nothing (project_queue.holders).
        if was in RUNNING_STATUSES and row.get("status") not in RUNNING_STATUSES:
            stopped = True
    for path in list(_sess_mtimes):
        if path in seen:
            continue
        _sess_mtimes.pop(path, None)
        sid = _sess_sids.pop(path, None)
        if sid:
            with _cond:
                _registry.pop(sid, None)
                _departed[sid] = time.time()
            _tr_paths.pop(sid, None)
            _tr_sizes.pop(sid, None)
            _note_registry_status(sid, None)
            keys.add(sid)
            stopped = True
    if stopped:
        _wake_schedule()
    # Corroboration is invited every tick, not only when the file itself
    # changed: a mark set against an ALREADY-busy row that never rewrites
    # again must still count as seen once, or its eventual departure would
    # read as the untouched-turn-before case
    # (test_an_older_turns_row_departing_does_not_retire_a_fresh_mark) instead
    # of what it actually is — a row this mark can rightly be stood down by.
    with _cond:
        marked_sids = list(_marks)
    for sid in marked_sids:
        with _cond:
            row = _registry.get(sid)
        if row is not None:
            _note_registry_status(sid, row.get("status"))
    return keys


def _read_live_transcripts() -> set[str]:
    """Session ids whose transcript grew — checked only for sessions a running
    `claude` holds, which is the only kind that can grow."""
    keys: set[str] = set()
    with _cond:
        registered = sorted(_registry)
    for sid in registered:
        path = _tr_paths.get(sid)
        if not path or not os.path.exists(path):
            path = session_liveness.transcript_path(sid, tasks_store.PROJECTS_DIR)
            if not path:
                continue
            _tr_paths[sid] = path
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        last = _tr_sizes.get(sid)
        _tr_sizes[sid] = size
        if last is None:
            # First sight of this transcript. News if it was born under a
            # session we are already watching (the first turn just landed); a
            # baseline otherwise.
            if _primed:
                keys.add(sid)
            continue
        if size != last:
            keys.add(sid)
    return keys


def _read_permission_cards() -> set[str]:
    """Session ids whose live run just RAISED or ANSWERED a permission card.

    THE ONE NEEDS-ATTENTION FACT NOTHING ELSE ANNOUNCES. `_status` reads
    `needs_attention` off `_parked_runs()` — a scan of the runs tree for cards
    with no decision — so the lane is correct on every listing and up to a full
    poll late in arriving, which for the one status that means "a person has to
    come and do something" is the worst possible latency.

    It has to be watched from HERE, and that is not a style choice. The card is
    WRITTEN by `permission_server.py`, an MCP server the CLI spawns, and it is
    ANSWERED by `agent._decide`, which runs in the executor's subprocess
    (`executor._run_python` — the claude agent is deliberately not on the
    in-process allowlist). Neither is this process, neither may import
    `fused_render` at all (SPEC PY-15 / D166), so neither can call `notify`.
    What both DO is write a file into the run's `perm/` directory, and this
    process can see that for the price of one `stat`.

    So: the newest `PERM_SCAN_LIMIT` run dirs, one stat each, and a session id
    only for the handful whose directory actually moved. A run's FIRST sighting
    is a baseline and announces nothing — `_start` creates `perm/` empty before
    the CLI can raise anything, so the baseline always lands first, and treating
    a new run dir as news would ring on every spawn for a card that does not
    exist.

    Best-effort throughout, like every other read in this module: no agent
    module, no runs tree, an unreadable meta — all mean "no news", never a dead
    watcher.
    """
    try:
        from fused_render.server.routers import tasks as tasks_router

        agent = tasks_router._agent_module()
        if agent is None:
            return set()
        runs = agent.RUNS
        perm_dir_of = agent._perm_dir
        names = sorted(os.listdir(runs), reverse=True)[:PERM_SCAN_LIMIT]
    except Exception:  # noqa: BLE001 — a watcher must outlive a bad scan
        return set()
    keys: set[str] = set()
    seen: set[str] = set()
    for name in names:
        run_dir = os.path.join(runs, name)
        seen.add(run_dir)
        try:
            st = os.stat(perm_dir_of(run_dir))
        except OSError:
            continue  # no card directory: this run has never been carded
        stamp = (st.st_mtime_ns, st.st_size)
        last = _perm_stamps.get(run_dir)
        _perm_stamps[run_dir] = stamp
        if last is None or last == stamp:
            continue  # baseline, or nothing moved
        try:
            with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
            if not isinstance(meta, dict):
                continue
            keys |= {sid for sid in tasks_router._run_sessions(agent, run_dir, meta)
                     if sid}
        except Exception:  # noqa: BLE001 — one unreadable run, not a dead tick
            logger.debug("could not name the session behind a permission card "
                         "in %s", run_dir, exc_info=True)
    for run_dir in [d for d in _perm_stamps if d not in seen]:
        # Out of the window the listing itself looks at, so nothing it does can
        # be a row any more. Dropped rather than remembered for ever — this dict
        # would otherwise keep one tuple per run dir the machine has ever made.
        del _perm_stamps[run_dir]
    return keys


def tick() -> set[str]:
    """One pass over the registry, the transcripts it names, the permission
    cards this app's own runs have raised or had answered, and the marks that
    have run out. Bumps the generation if anything moved and returns the
    affected task keys. The first call is a baseline and announces nothing —
    the page's first full listing already has it all."""
    global _primed
    keys = _read_registry()
    keys |= _read_live_transcripts()
    keys |= _read_permission_cards()
    # LAST, so a mark whose registry row arrived in the same tick is retired
    # against a listing that already knows better. The row does not flicker
    # either way — `_live` reads `busy` over a mark — but the announcement
    # belongs after the fact that replaces it.
    keys |= _expire_marks(time.time())
    if not _primed:
        _primed = True
        return set()
    if keys:
        _bump(keys)
    return keys


# ------------------------------------------------------------------ the loop

def _loop() -> None:
    while True:
        try:
            tick()
        except Exception:  # noqa: BLE001 — a watcher must outlive any one bad tick
            pass
        time.sleep(TICK_SEC)


def start() -> None:
    """Start the watcher thread, once per process. From the app's startup
    event, never from create_app — tests build apps without lifespan and must
    not spawn a thread that reads the developer's real ~/.claude."""
    global _started
    if _started:
        return
    _started = True
    try:
        tick()  # prime synchronously so the first request has the registry
    except Exception:  # noqa: BLE001
        pass
    threading.Thread(target=_loop, daemon=True, name="fused-tasks-watch").start()


def reset() -> None:
    """Forget everything. For tests."""
    global _generation, _primed
    with _cond:
        _generation = 0
        _changed.clear()
        _registry.clear()
        _departed.clear()
        _marks.clear()
        _mark_turns.clear()
        _mark_busy_seen.clear()
        _last_idle_turn.clear()
    _primed = False
    _sess_mtimes.clear()
    _sess_sids.clear()
    _tr_paths.clear()
    _tr_sizes.clear()
    _perm_stamps.clear()
