"""Change detection for the Tasks page — WHICH sessions moved, and a number
that says something did.

The Tasks listing (server/routers/tasks.py) is already cheap to rebuild: every
transcript read is incremental and skipped outright when the file size has not
moved. What it never had was a *signal*. The page asked every 20 seconds, and a
session started (or resumed) in a terminal outside the app surfaced up to a poll
later. This module is that signal.

Claude Code writes three things this can watch, all verified on a real
machine (2026-08-27):

* ``~/.claude/sessions/<pid>.json`` — one file per RUNNING ``claude`` process:
  ``sessionId``, ``cwd``, ``status`` (busy / shell / waiting / idle),
  ``updatedAt``. Appears before the transcript exists, is rewritten on every
  status change, and is deleted when the process exits. A resumed two-week-old
  session gets a file under its OLD session id.
* ``~/.claude/history.jsonl`` — one line appended per user prompt, across every
  project, carrying ``sessionId``. Lands the same second the prompt is sent;
  the transcript's assistant append follows seconds later.
* the transcripts themselves — but only the ones the registry says are live
  are watched here (a couple of dozen files, not the machine's whole history).
  A session nobody is running cannot grow.

Stat-poll on a daemon thread, once a second, rather than FSEvents/inotify:
cross-platform, no ctypes, no dropped-event semantics to reason about, and
~25 ``stat`` calls per second is nothing. A real filesystem stream can replace
``_loop`` later behind the same two exports — ``generation()`` and ``wait()`` —
without the router or the page noticing.

Everything here degrades to "no news": an unreadable directory, a half-written
registry file, a vanished transcript all produce no keys and no exception. The
20-second full listing is still there underneath and remains the truth.
"""
from __future__ import annotations

import collections
import json
import os
import threading
import time

from fused_render import session_liveness, tasks_store

# CLAUDE_CONFIG_DIR wins where set — same rule, same deliberate local copy, as
# session_liveness.py and tasks_store.py. Module-level so tests can point them
# at a tmp dir.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
HISTORY_PATH = os.path.join(CLAUDE_DIR, "history.jsonl")
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
_hist_size = -1
_sess_mtimes: dict[str, float] = {}   # sessions/<pid>.json -> mtime
_sess_sids: dict[str, str] = {}       # sessions/<pid>.json -> session_id
_tr_paths: dict[str, str] = {}        # session_id -> transcript path
_tr_sizes: dict[str, int] = {}        # session_id -> size
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


def wait(since: int, timeout: float = MAX_WAIT_SEC) -> tuple[int, frozenset | None]:
    """Block until the generation passes `since`, or `timeout` elapses.

    Returns ``(generation, keys)``. ``keys`` is the union of every task key that
    changed in generations ``since+1 .. generation`` — empty when the wait
    timed out with nothing new, and **None** when `since` is older than the
    ring remembers (the caller should reload everything)."""
    deadline = time.monotonic() + max(0.0, min(timeout, MAX_WAIT_SEC))
    with _cond:
        while _generation <= since:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _generation, frozenset()
            _cond.wait(remaining)
        if since < 0 or (_changed and _changed[0][0] > since + 1):
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


# --------------------------------------------------------------- one tick

def _read_history_tail() -> set[str]:
    """Session ids named by history lines appended since the last tick."""
    global _hist_size
    try:
        size = os.path.getsize(HISTORY_PATH)
    except OSError:
        # No history yet (a fresh ~/.claude): the file's first line, when it
        # comes, is news — so the baseline is "read from byte 0", not "unseen".
        _hist_size = 0
        return set()
    if _hist_size < 0 or size < _hist_size:
        # First sight, or the file was rotated/rewritten: baseline, no news.
        _hist_size = size
        return set()
    if size == _hist_size:
        return set()
    keys: set[str] = set()
    try:
        with open(HISTORY_PATH, "rb") as f:
            f.seek(_hist_size)
            chunk = f.read(size - _hist_size)
    except OSError:
        return set()
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return set()  # a line still being written: read it whole next tick
    _hist_size += cut + 1
    for line in chunk[:cut + 1].decode("utf-8", "replace").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        sid = obj.get("sessionId") if isinstance(obj, dict) else None
        if isinstance(sid, str) and sid:
            keys.add(sid)
    return keys


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return True  # no pid to check: trust the file
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but not ours (EPERM), or a platform without kill
    return True


def _read_registry() -> set[str]:
    """Reconcile `sessions/*.json` with `_registry`; return the session ids
    whose record appeared, changed, or went away."""
    keys: set[str] = set()
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
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        seen.add(path)
        if _sess_mtimes.get(path) == mtime:
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
        if not _pid_alive(row.get("pid")):
            # A crashed claude leaves its file behind; a dead pid is not a
            # live session, and must not paint a running badge forever. The
            # mtime stays recorded so the file is not re-read every tick.
            if old_sid:
                _sess_sids.pop(path, None)
                with _cond:
                    _registry.pop(old_sid, None)
                    _departed[old_sid] = time.time()
                keys.add(old_sid)
            continue
        if old_sid and old_sid != sid:
            _registry.pop(old_sid, None)
            keys.add(old_sid)
        _sess_sids[path] = sid
        with _cond:
            _registry[sid] = row
            _departed.pop(sid, None)
        keys.add(sid)
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
            keys.add(sid)
    return keys


def _read_live_transcripts() -> set[str]:
    """Session ids whose transcript grew — checked only for sessions a running
    `claude` holds, which is the only kind that can grow."""
    keys: set[str] = set()
    with _cond:
        sids = list(_registry)
    for sid in sids:
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
            # session we are already watching (the first prompt just landed);
            # a baseline otherwise.
            if _primed:
                keys.add(sid)
            continue
        if size != last:
            keys.add(sid)
    return keys


def tick() -> set[str]:
    """One pass over the three signals. Bumps the generation if anything moved
    and returns the affected task keys. The first call is a baseline and
    announces nothing — the page's first full listing already has it all."""
    global _primed
    keys = _read_history_tail()
    keys |= _read_registry()
    keys |= _read_live_transcripts()
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
    global _generation, _primed, _hist_size
    with _cond:
        _generation = 0
        _changed.clear()
        _registry.clear()
        _departed.clear()
    _primed = False
    _hist_size = -1
    _sess_mtimes.clear()
    _sess_sids.clear()
    _tr_paths.clear()
    _tr_sizes.clear()
