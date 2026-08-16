"""Claude Code session transcripts, read off disk for the Explorer.

Two endpoints, same on-disk source (~/.claude/projects/<encoded-cwd>/*.jsonl):

* ``GET /api/claude-sessions`` — one row per real project *folder*, for the
  homepage's "Claude sessions" tab.
* ``GET /api/claude-sessions/summaries`` — one row per *session*, for the
  React shell's Schedule page. Mirrors the bundled sessions inbox app
  (core_apps/sessions/sessions/sessions.py + core_apps/sessions/inbox.py):
  same 45s "running" rule, same housekeeping-aware activity read, same
  session_names.json / triage.json overlays.

GET /api/claude-sessions — Claude Code project folders, for the Explorer
homepage's "Claude sessions" tab.

Scans transcripts at ~/.claude/projects/<encoded-cwd>/*.jsonl (Claude Code's
own on-disk session store) and groups them by the REAL project folder: the
`cwd` field recorded inside each transcript, not the encoded directory name.
That encoding is lossy — Claude Code turns every path separator AND every
literal hyphen in the original path into "-" (core_apps/sessions/sessions.py's
_decode_project_dir has the same caveat, and doesn't trust its own decode for
this reason either) — so a project path containing a hyphen would decode to
garbage. Reading `cwd` back out of the transcript is the only reliable way to
recover the folder.

One row per folder, newest session first. Folders that no longer exist on
disk are dropped rather than listed — the point of this tab is "open it", and
a folder that isn't there can't be opened.
"""
import glob
import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fused_render._view_url_codec import canonical_fs_path

try:
    import fcntl  # POSIX only — Windows falls back to no inter-process lock,
    # the same posture as the Inbox's set_triage.py, whose file this shares.
except ImportError:  # pragma: no cover
    fcntl = None

router = APIRouter()

# CLAUDE_CONFIG_DIR wins where set (same rule as user_skills.py, the claude
# template agent's CLAUDE_DIR, and templates/shared/file_history.py's
# config_dir()) — duplicated locally rather than imported cross-package, same
# posture as those sites.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")

# Mutable session state (custom names, triage) — written by the bundled
# sessions inbox app, read here. Mirrors shell/storage.home_dir()'s
# FUSED_RENDER_HOME override, and deliberately skips branch nesting so the
# state is shared across branches (same posture as community.py and
# core_apps/sessions). The json paths are derived from this inside the
# loaders rather than at import, so overriding STATE_DIR redirects both.
STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "claude-sessions")


def _session_cwd(jsonl_path: str) -> str | None:
    """The transcript's own `cwd`, from whichever line has it first — normally
    the very first line, so this almost always stops after one read rather
    than parsing the whole (possibly multi-MB) transcript."""
    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                cwd = obj.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


@router.get("/api/claude-sessions")
def api_claude_sessions():
    latest: dict[str, float] = {}
    if os.path.isdir(PROJECTS_DIR):
        for jsonl_path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
            cwd = _session_cwd(jsonl_path)
            if not cwd or not os.path.isdir(cwd):
                continue
            try:
                mtime = os.path.getmtime(jsonl_path)
            except OSError:
                continue
            if mtime > latest.get(cwd, 0.0):
                latest[cwd] = mtime
    # Canonicalized on the way out: the frontend's path helpers (basename,
    # FolderStack's joinPath) are forward-slash-only, matching every other
    # fs path the runtime hands them — a raw Windows cwd would break both.
    folders = [
        {
            "path": canonical_fs_path(path),
            "lastActive": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        }
        for path, mtime in latest.items()
    ]
    folders.sort(key=lambda e: e["lastActive"], reverse=True)
    return {"folders": folders}


# --- per-session summaries -------------------------------------------------
#
# The Schedule page polls this every 20s, so nothing here may scale with
# transcript size: the head is parsed once and cached (transcripts are
# append-only, so the head never changes), and liveness comes from a 16KB
# tail read. A multi-MB transcript is never parsed end to end.

STATUSES = ("in_progress", "done", "archived")

# Entry types Claude Code appends to a transcript after the turn is over
# (idle housekeeping: away summaries, turn timing, last-prompt records).
# These bump the file mtime but don't mean a session is active.
_HOUSEKEEPING_TYPES = {"system", "last-prompt", "summary"}

_RUNNING_WINDOW_SEC = 45  # same rule as the inbox UI: fresh activity = running
_STALE_TAIL_SEC = 90      # older than this, the tail can't make it "running"
_TAIL_BYTES = 16384
# Hard caps on the head read so a huge transcript whose first user message
# never arrives (tool-result-only opener, replayed session) still costs O(1).
_HEAD_CHARS = 256 * 1024
_HEAD_LINES = 2000

# path -> (size_at_parse, cwd, first_ts, first_prompt)
_HEAD_CACHE: dict[str, tuple[int, str | None, str | None, str]] = {}


def _load_state(filename: str) -> dict:
    """A json dict from STATE_DIR, or {} — missing/corrupt is not an error."""
    try:
        with open(os.path.join(STATE_DIR, filename), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _decode_project_dir(dirname: str) -> str:
    # Claude Code encodes cwd paths as dir names like "-Users-sina-Desktop-foo".
    # Lossy (literal hyphens encode as "-" too), which is why it's only the
    # fallback for a transcript that never recorded its own cwd.
    if dirname.startswith("-"):
        return "/" + dirname[1:].replace("-", "/")
    return dirname


def _first_text(content) -> str:
    """First text block of a message's content (mirrors sessions.py). Returns
    "" for tool_result-only content, which is how tool results are skipped
    when picking a session title."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
            if isinstance(block, dict) and "text" in block:
                return block.get("text", "")
    return ""


# Claude Code writes its own one-line title for a session into the transcript
# as a top-level record:
#
#   {"type":"ai-title","aiTitle":"Build flight details analyzer","sessionId":"…"}
#
# Verified against a real transcript, where it appears 242 times — once per
# turn, because the title tracks the conversation as it evolves. So the LAST
# such record is the title, and the first one is what the session looked like
# before it was about anything. Nothing summarises a session for us anywhere
# else in this app; this is that fact, already written down.
AI_TITLE_TYPE = "ai-title"
# Substring screen for a raw line, applied before json.loads by callers that
# stream a whole transcript. A filter only — a false positive costs one parse.
AI_TITLE_HINT = "ai-title"


def ai_title(record) -> str:
    """One transcript record's `aiTitle`, or "" if it isn't one of those.

    Record-level rather than file-level on purpose: the callers that want this
    (the Tasks listing) are already streaming the transcript for other reasons,
    and a second pass over a multi-MB file to re-find a field they just walked
    past is exactly the cost those endpoints are written to avoid. **Last one
    wins** — see the note above; a caller keeping the most recent non-empty
    answer gets the current title."""
    if not isinstance(record, dict) or record.get("type") != AI_TITLE_TYPE:
        return ""
    title = record.get("aiTitle")
    return title.strip() if isinstance(title, str) else ""


def _parse_ts(ts) -> datetime | None:
    """Transcript timestamp -> aware UTC datetime, or None. Naive values are
    read as UTC so output doesn't depend on the server's local zone."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _parse_head(path: str) -> tuple[str | None, str | None, str]:
    """(cwd, first timestamp, first user prompt), streaming from the top and
    stopping as soon as all three are known — normally within a few lines."""
    cwd: str | None = None
    first_ts: str | None = None
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
                if cwd is None:
                    val = obj.get("cwd")
                    if isinstance(val, str) and val:
                        cwd = val
                if first_ts is None:
                    ts = obj.get("timestamp")
                    if isinstance(ts, str) and ts:
                        first_ts = ts
                if not prompt and obj.get("type") == "user":
                    msg = obj.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        text = _first_text(msg.get("content")).strip()
                        if text:
                            prompt = text
                if cwd is not None and first_ts is not None and prompt:
                    break
    except OSError:
        return None, None, ""
    return cwd, first_ts, prompt


def _head(path: str, size: int) -> tuple[str | None, str | None, str]:
    """_parse_head, cached per path. Transcripts are append-only, so a head
    that was fully resolved stays valid however much the file grows; an
    incomplete one is retried once the file has more to offer, and a file
    that shrank was replaced and is re-read from scratch."""
    cached = _HEAD_CACHE.get(path)
    if cached is not None:
        cached_size, cwd, first_ts, prompt = cached
        complete = bool(prompt) and first_ts is not None and cwd is not None
        if cached_size == size or (size > cached_size and complete):
            if size != cached_size:
                # Record the size we just saw, not the one we last parsed at,
                # so the entry always describes the file's current extent and
                # a later shrink is still recognized as a different file.
                _HEAD_CACHE[path] = (size, cwd, first_ts, prompt)
            return cwd, first_ts, prompt
    if len(_HEAD_CACHE) > 20000:  # unbounded only if the user has 20k sessions
        _HEAD_CACHE.clear()
    cwd, first_ts, prompt = _parse_head(path)
    _HEAD_CACHE[path] = (size, cwd, first_ts, prompt)
    return cwd, first_ts, prompt


def _tail(path: str, mtime: float) -> tuple[float, datetime | None]:
    """(activity timestamp, last real activity time) from a 16KB tail read.

    Same rule as core_apps/sessions' _activity_mtime: housekeeping appends
    bump the file mtime but aren't activity, and a turn_duration entry newer
    than any real message means the turn just ended — the session is idle
    right now, so it reports 0.0 rather than letting the 45s window keep the
    badge lit.
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return mtime, None
    lines = [ln for ln in chunk.split("\n") if ln.strip()]
    if size > _TAIL_BYTES and lines:
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
        if obj.get("type") in _HOUSEKEEPING_TYPES:
            if activity is None and obj.get("subtype") == "turn_duration":
                activity = 0.0
            continue
        ts = obj.get("timestamp")
        if not ts:
            continue
        dt = _parse_ts(ts)
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


def _summarize(path: str, now: float, names: dict, triage: dict) -> dict | None:
    session_id = os.path.splitext(os.path.basename(path))[0]
    try:
        stat = os.stat(path)
    except OSError:
        return None

    cwd, first_ts, prompt = _head(path, stat.st_size)
    started = _parse_ts(first_ts)
    if started is None:
        return None  # no timestamps at all: not a session we can place in time

    activity, last = _tail(path, stat.st_mtime)
    # Too old for the tail to matter (matches _activity_mtime's fast path):
    # stale either way, so this only skips deciding what kind of stale.
    if now - stat.st_mtime > _STALE_TAIL_SEC:
        activity = stat.st_mtime
    running = (now - activity) < _RUNNING_WINDOW_SEC
    last_active = last or datetime.fromtimestamp(stat.st_mtime, timezone.utc)

    # Untriaged: running sessions are in progress, stopped ones are done — a
    # session that finished while nothing was watching shouldn't sit in
    # "in_progress" forever.
    default = "in_progress" if running else "done"
    record = triage.get(session_id)
    status = (record.get("status") if isinstance(record, dict) else None) or default
    if status not in STATUSES:
        status = default

    custom = names.get(session_id)
    if isinstance(custom, str) and custom.strip():
        name = custom
    elif prompt:
        name = prompt[:140]
    else:
        name = "(no user message)"

    return {
        "session_id": session_id,
        "name": name,
        # Canonicalized like /api/claude-sessions: the frontend's path helpers
        # are forward-slash-only and a raw Windows cwd would break them.
        "cwd": canonical_fs_path(cwd or _decode_project_dir(
            os.path.basename(os.path.dirname(path)))),
        "started_at": started.isoformat(),
        "last_active": last_active.astimezone(timezone.utc).isoformat(),
        "running": running,
        "status": status,
    }


@router.get("/api/claude-sessions/summaries")
def api_claude_session_summaries():
    names = _load_state("session_names.json")
    triage = _load_state("triage.json")
    now = datetime.now(timezone.utc).timestamp()
    sessions = []
    if os.path.isdir(PROJECTS_DIR):
        for jsonl_path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
            row = _summarize(jsonl_path, now, names, triage)
            if row is not None:
                sessions.append(row)
    # All last_active values are UTC ISO with the same offset spelling, so a
    # string sort is a time sort.
    sessions.sort(key=lambda s: s["last_active"], reverse=True)
    return {"sessions": sessions}


class TriagePatch(BaseModel):
    session_id: str
    status: str


@router.post("/api/claude-sessions/triage")
def api_claude_session_triage(patch: TriagePatch):
    """Set a session's triage status — the write half of the Inbox's own
    set_triage.py (core_apps/sessions), duplicated here because the shell's
    Board drags cards between the same three columns the Inbox uses. Same
    file, same locking, same merge semantics: only `status` changes, and the
    record's other keys (note, tags, read) survive untouched.
    """
    session_id = patch.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="missing session id")
    if patch.status not in ("in_progress", "done", "archived"):
        raise HTTPException(status_code=400, detail=f"unknown status {patch.status!r}")

    os.makedirs(STATE_DIR, exist_ok=True)
    triage_path = os.path.join(STATE_DIR, "triage.json")
    lock_path = triage_path + ".lock"
    with open(lock_path, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        triage = _load_state("triage.json")
        rec = triage.get(session_id)
        if not isinstance(rec, dict):
            rec = {}
        rec["status"] = patch.status
        triage[session_id] = rec
        with open(triage_path, "w", encoding="utf-8") as f:
            json.dump(triage, f, indent=2, ensure_ascii=False)
    return {"ok": True, "session_id": session_id, "status": patch.status}
