"""Is a Claude Code session mid-turn RIGHT NOW — read off its transcript.

One question, asked from three places that had no way to share it:

* the sessions router (`server/routers/claude_sessions.py`) paints a `running`
  badge in the Inbox and the Board,
* the Tasks router (`server/routers/tasks.py`) uses the same answer to decide
  whether a thread's newest message still has a turn open,
* **the scheduler** (`fused_render/schedule.py`) must not resume a conversation
  the user is typing into, because two `claude --resume S` processes append to
  one transcript and the transcript is the session.

The third one is why this module exists rather than the rule staying where it
was. `schedule.py` may not import anything under `fused_render.server` — the
router imports the schedule, and `server/__init__` -> `app.py` -> routers would
close the loop. The other way out was to duplicate the rule locally, the way
`claude_artifacts.py` duplicates `CLAUDE_DIR`; that is right for a two-line
constant and wrong here, because the rule is not a constant. It is a tail read,
a housekeeping-record filter, a turn_duration special case and two windows, and
a scheduler that disagreed with the badge by even one of those would defer a
message the page says is safe (or, worse, send one it says is not). One copy,
one answer.

**The rule, stated once.** A transcript's mtime alone lies: Claude Code appends
housekeeping records (away summaries, turn timing, last-prompt) after the turn
is over, so a file touched two seconds ago may belong to a session that has been
idle for an hour. So the last 16KB is read and walked BACKWARDS to the newest
record that is not housekeeping; that record's timestamp is the activity. A
`turn_duration` record newer than any real message is the explicit "the turn
just ended" marker and reports 0.0, which no window can make live. Activity
inside 45 seconds is running. A file nothing has touched in 90 seconds skips
the read entirely — it is stale either way, and the read would only be deciding
what kind of stale.

Nothing here raises. An unreadable transcript, a vanished file, a truncated
line: every one of them answers "not running", because the two callers that
matter both degrade the same way — a badge that stays dark, and a scheduled
message that goes rather than waits for a turn nobody can see.
"""
from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone

# CLAUDE_CONFIG_DIR wins where set — the same rule (and the same deliberate
# local copy) as claude_artifacts.py, tasks_store.py and the claude template's
# agent. Module-level so a test can monkeypatch PROJECTS_DIR at a tmp dir,
# which is how every other transcript reader here is tested.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

# Entry types Claude Code appends to a transcript after the turn is over (idle
# housekeeping: away summaries, turn timing, last-prompt records). These bump
# the file mtime but don't mean a session is active.
HOUSEKEEPING_TYPES = {"system", "last-prompt", "summary"}

RUNNING_WINDOW_SEC = 45  # same rule as the inbox UI: fresh activity = running
STALE_TAIL_SEC = 90      # older than this, the tail can't make it "running"
TAIL_BYTES = 16384


def parse_ts(ts) -> datetime | None:
    """Transcript timestamp -> aware UTC datetime, or None. Naive values are
    read as UTC so output doesn't depend on the server's local zone."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def tail_activity(path: str, mtime: float) -> tuple[float, datetime | None]:
    """(activity timestamp, last real activity time) from a 16KB tail read.

    Same rule as core_apps/sessions' `_activity_mtime`: housekeeping appends
    bump the file mtime but aren't activity, and a `turn_duration` entry newer
    than any real message means the turn just ended — the session is idle right
    now, so it reports 0.0 rather than letting the 45s window keep the badge
    lit.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return mtime, None
    lines = [ln for ln in chunk.split("\n") if ln.strip()]
    if size > TAIL_BYTES and lines:
        lines = lines[1:]  # drop the partial first line from mid-file seek
    activity: float | None = None
    last: datetime | None = None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            if activity is None:
                activity = mtime  # partial last line: a write is in flight
            continue
        if obj.get("type") in HOUSEKEEPING_TYPES:
            if activity is None and obj.get("subtype") == "turn_duration":
                activity = 0.0
            continue
        ts = obj.get("timestamp")
        if not ts:
            continue
        dt = parse_ts(ts)
        if dt is None:
            if activity is None:
                activity = mtime
            continue
        last = dt
        if activity is None:
            activity = dt.timestamp()
        break
    # nothing but housekeeping in the tail — not real activity
    return (0.0 if activity is None else activity), last


def transcript_running(path: str, now: float) -> tuple[bool, float]:
    """(is this transcript's session mid-turn, when it was last active).

    The whole rule in one call, including the stale fast path. A path that is
    empty, missing, or unreadable answers `(False, 0.0)` — a caller deciding
    whether to defer a message must never be stopped by a file it cannot read.
    """
    if not path:
        return False, 0.0
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False, 0.0
    if now - mtime > STALE_TAIL_SEC:
        return False, mtime
    activity, last = tail_activity(path, mtime)
    running = (now - activity) < RUNNING_WINDOW_SEC
    return running, (last.timestamp() if last is not None else mtime)


def transcript_path(session_id: str, projects_dir: str | None = None) -> str:
    """Where a session id's transcript lives, or "" if there is not one.

    A session's transcript is `<projects>/<encoded cwd>/<session id>.jsonl`, and
    the caller that needs this (the scheduler) knows the id and not the cwd — the
    encoded directory is derived from a working directory it was never told. So
    the id is globbed for across every project bucket. The id is used as a glob
    PATTERN, so a caller passing something with a `*` or a `/` in it could reach
    outside the tree; ids are uuids, and anything that is not one is refused here
    rather than trusted to be harmless.
    """
    if not session_id or not isinstance(session_id, str):
        return ""
    if not all(ch.isalnum() or ch in "-_" for ch in session_id):
        return ""
    root = projects_dir or PROJECTS_DIR
    matches = glob.glob(os.path.join(root, "*", session_id + ".jsonl"))
    return matches[0] if matches else ""


def session_running(session_id: str, now: float,
                    projects_dir: str | None = None) -> bool:
    """Is SOMETHING mid-turn in this session right now — whoever started it.

    The scheduler's question, and the reason the answer had to leave the router.
    "Whoever started it" is the whole content of it: the schedule store knows
    about the sends IT has in flight and nothing at all about the user typing
    into the same conversation, and a transcript does not record which process
    is appending to it — which is exactly why it is the right thing to ask.
    """
    path = transcript_path(session_id, projects_dir)
    if not path:
        return False
    try:
        running, _last = transcript_running(path, now)
    except Exception:  # noqa: BLE001 — never stop a send over an unreadable file
        return False
    return running
