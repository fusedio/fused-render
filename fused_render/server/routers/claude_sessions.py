"""Claude Code session transcripts, read off disk for the Explorer.

Three endpoints, same on-disk source (~/.claude/projects/<encoded-cwd>/*.jsonl):

* ``GET /api/claude-sessions`` — one row per real project *folder*, for the
  exhaustive folder listing.
* ``GET /api/claude-sessions/home`` — the newest project folders for Home. It
  orders candidates by transcript mtime first, then opens only enough
  transcripts to fill Home's single row.
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

from fused_render import session_liveness, tasks_store
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


HOME_SESSION_LIMIT = 12


def _transcripts_newest_first() -> list[tuple[float, str]]:
    """All transcript paths ordered by mtime without opening their contents.

    Establishing the true newest folders requires seeing every transcript's
    cheap filesystem timestamp. The Home saving is after this pass: JSONL files
    are opened newest-first and parsing stops as soon as the row is full.
    """
    candidates: list[tuple[float, str]] = []
    if not os.path.isdir(PROJECTS_DIR):
        return candidates
    for jsonl_path in glob.iglob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        try:
            candidates.append((os.path.getmtime(jsonl_path), jsonl_path))
        except OSError:
            continue
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates


@router.get("/api/claude-sessions/home")
def api_home_claude_sessions(limit: int = HOME_SESSION_LIMIT):
    """The newest unique, existing Claude project folders needed by Home."""
    limit = max(1, min(limit, HOME_SESSION_LIMIT))
    folders = []
    seen: set[str] = set()
    for mtime, jsonl_path in _transcripts_newest_first():
        cwd = _session_cwd(jsonl_path)
        if not cwd or cwd in seen:
            continue
        # Mark before the probe: repeated sessions for a stale folder cannot
        # become valid during this one request and should not repeat the syscall.
        seen.add(cwd)
        if not os.path.isdir(cwd):
            continue
        folders.append({
            "path": canonical_fs_path(cwd),
            "lastActive": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        })
        if len(folders) >= limit:
            break
    return {"folders": folders}


# --- per-session summaries -------------------------------------------------
#
# The Schedule page polls this every 20s, so nothing here may scale with
# transcript size: the head is parsed once and cached (transcripts are
# append-only, so the head never changes), and liveness comes from a 16KB
# tail read. A multi-MB transcript is never parsed end to end.

STATUSES = ("in_progress", "done", "archived")

# The liveness rule now lives in `fused_render/session_liveness.py`, because the
# SCHEDULER needs the same answer and may not import a router (server/__init__ ->
# app.py -> routers is the cycle). Aliased here rather than renamed at every use
# site so this module — and the Tasks router next door, which reads these names
# off it — keeps reading exactly as it did. See that module for the rule itself.
_HOUSEKEEPING_TYPES = session_liveness.HOUSEKEEPING_TYPES
_RUNNING_WINDOW_SEC = session_liveness.RUNNING_WINDOW_SEC
_STALE_TAIL_SEC = session_liveness.STALE_TAIL_SEC
_TAIL_BYTES = session_liveness.TAIL_BYTES
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


_parse_ts = session_liveness.parse_ts


def _parse_head(path: str) -> tuple[str | None, str | None, str]:
    """(cwd, first timestamp, first user prompt), streaming from the top and
    stopping as soon as all three are known — normally within a few lines.

    The prompt is what a HUMAN typed, which is not the same as the first
    `type: user` record: this reader used to take the record verbatim and named
    the picker's rows `<live-app-state>` — the fused-render Claude page's own
    wire, addressed to the model, quoted back at the user as the name of their
    conversation. `tasks_store` owns that policy for every reader of it now
    (`strip_machinery`), so this surface and the Tasks list cannot disagree about
    what a session is called."""
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
                # `isMeta` (Claude Code's local-command caveat, written FOR the
                # user) and `isSidechain` (a subagent's brief, which can be a
                # whole task description) are both records the user never typed.
                # Neither was skipped here, while the sibling reader in
                # tasks_store skipped one of them — the divergence this file's
                # half of the fix exists to end.
                if (not prompt and obj.get("type") == "user"
                        and not obj.get("isMeta") and not obj.get("isSidechain")):
                    msg = obj.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        # Stripped, and an empty remainder keeps the scan going
                        # to the next user record rather than settling for a
                        # nameless row — see tasks_store.strip_machinery.
                        prompt = tasks_store.strip_machinery(
                            _first_text(msg.get("content")))
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


_tail = session_liveness.tail_activity


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

    # THE IN-PROGRESS LANE IS DERIVED, NOT RECORDED — the same rule as the Inbox
    # this module mirrors (core_apps/sessions/inbox.py). "Something is running in
    # this conversation" is a fact about the present, so it outranks the record:
    # a session filed as done or archived and then resumed belongs in In Progress,
    # and one that finished while nothing was watching drops back to whatever the
    # user filed it as (or Done, untriaged) without anything having to notice.
    #
    # The guard used to cover only the untriaged default, which was the half that
    # was never the problem. The Inbox's `autoFlow` bought the other half by
    # OVERWRITING the record whenever it saw a run — and could only retract that
    # while its own tab stayed open, so every unwitnessed finish left an
    # `in_progress` pin on disk that nothing would ever clear (tasks.py
    # `_pin_holds` is the reap that made the leftovers harmless). autoFlow stopped
    # writing it, so the lane is computed here instead and the user's own pin is
    # still in the file when the run stops.
    if running:
        status = "in_progress"
    else:
        record = triage.get(session_id)
        status = (record.get("status") if isinstance(record, dict) else None) or "done"
        if status not in STATUSES:
            status = "done"  # a hand-edited record costs the pin, not the row

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

    **The write is stamped.** `at` is when this status was chosen, and the Tasks
    router's `_pin_holds` needs it to tell a deliberate `in_progress` — the
    reopen drag — from the ones `autoFlow` writes automatically and cannot take
    back once its page is closed. A pin with no stamp reads as older than
    anything that has happened and is reapable, which is the right answer for
    every automatic one: `autoFlow` sends `{status}` alone, and this is the only
    writer that knows the status came from a person. Stringified because that is
    the shape of the record — `set_triage.py` coerces every field it writes.
    """
    session_id = patch.session_id.strip()
    if not session_id:
        raise HTTPException(status_code=400, detail="missing session id")
    if patch.status not in ("in_progress", "done", "archived"):
        raise HTTPException(status_code=400, detail=f"unknown status {patch.status!r}")
    write_triage(session_id, patch.status)
    return {"ok": True, "session_id": session_id, "status": patch.status}


def write_triage(session_id: str, status: str) -> None:
    """The write itself, without the HTTP around it — so a second router can
    file a session away without going back out through its own server. The
    Tasks router's archive verb is the caller: archiving a TASK is one gesture
    that both cancels its scheduled work and files its session, and both halves
    have to be the same write the Inbox makes or the two views would keep two
    different truths about the same session."""
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
        rec["status"] = status
        rec["at"] = str(datetime.now(timezone.utc).timestamp())
        triage[session_id] = rec
        with open(triage_path, "w", encoding="utf-8") as f:
            json.dump(triage, f, indent=2, ensure_ascii=False)


def clear_triage(session_id: str) -> bool:
    """Take the filing back — drop `status` (and its stamp) from one session's
    record, keeping everything else in it. True when there was one to drop.

    TWO CALLERS in the Tasks router, and they are the two ways out of Archive:

    * a task that was archived DOES SOMETHING NEW — a message typed into an
      archived conversation has to actually un-file it rather than be shown out
      of its lane for one poll. See `_revived` there for which activity counts,
      and why a run already in flight when the filing happened does not.
    * somebody drags the card out of the Archive lane (`api_task_unarchive`).
      Which lane they dropped it on says nothing — the task lands wherever it
      derives to — so that gesture has nothing to pass here either.

    Both are the same one-line change to the same record, which is why it lives
    here rather than in either caller.

    The record itself is NOT deleted — a note, a tag or a read mark on that
    session is somebody else's data and outlives the status the Board put on it.
    Same file, same lock, same merge semantics as the write above."""
    os.makedirs(STATE_DIR, exist_ok=True)
    triage_path = os.path.join(STATE_DIR, "triage.json")
    lock_path = triage_path + ".lock"
    with open(lock_path, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        triage = _load_state("triage.json")
        rec = triage.get(session_id)
        if not isinstance(rec, dict) or "status" not in rec:
            return False
        rec.pop("status", None)
        rec.pop("at", None)
        triage[session_id] = rec
        with open(triage_path, "w", encoding="utf-8") as f:
            json.dump(triage, f, indent=2, ensure_ascii=False)
    return True
