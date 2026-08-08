"""runPython target: list Claude Code sessions merged with triage state.

Reuses the session-scanning logic from ./sessions/sessions.py and
overlays triage.json (status / project / note per session). Sessions without
a triage record are "inbox" — the unmanaged pile.
"""
import datetime
import json
import os
import sys

_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))
sys.path.insert(0, os.path.join(_HERE, "sessions"))

import sessions as _sessions  # noqa: E402

# Mutable state lives in the user-data dir, never next to the scripts — the
# shipped copy is mounted read-only (see ensure_builtin_mounts). Mirrors
# shell/storage.home_dir()'s FUSED_RENDER_HOME override.
_STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "claude-sessions")

TRIAGE_FILE = os.path.join(_STATE_DIR, "triage.json")

STATUSES = ["in_progress", "done", "archived"]


def _load_triage() -> dict:
    try:
        with open(TRIAGE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


HOME = os.path.expanduser("~")


def _folder_project(path: str) -> str:
    """Project label = the session's folder, shortened with ~ for home."""
    if path == HOME:
        return "~"
    if path.startswith(HOME + "/"):
        return "~/" + path[len(HOME) + 1:]
    return path


# same rule as the UI: fresh activity mtime within 45s = running now
def _is_running(mtime_iso: str) -> bool:
    if not mtime_iso:
        return False
    try:
        mtime = datetime.datetime.fromisoformat(mtime_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - mtime).total_seconds() < 45


def main(limit: int = 500) -> dict:
    base = _sessions.main(limit=limit)
    triage = _load_triage()

    sessions = []
    status_counts = {s: 0 for s in STATUSES}
    unread_count = 0
    project_counts = {}
    tag_counts = {}
    for s in base["sessions"]:
        t = triage.get(s["sessionId"], {})
        # untriaged: running sessions are in progress, stopped ones are done —
        # a session that finished while the page wasn't watching shouldn't sit
        # in "In Progress" forever
        default = "in_progress" if _is_running(s.get("mtime")) else "done"
        status = t.get("status") or default
        if status not in STATUSES:
            status = default
        s["status"] = status
        s["read"] = bool(t.get("read"))
        # the Inbox holds unread work that still matters — archived is filed away
        if not s["read"] and status != "archived":
            unread_count += 1
        s["triageProject"] = _folder_project(s["cwd"] or s["project"])
        s["note"] = t.get("note") or ""
        # tags stored as a comma-separated string in triage.json
        tags = [x.strip() for x in (t.get("tags") or "").split(",") if x.strip()]
        s["tags"] = tags
        for tag in tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        status_counts[status] += 1
        project_counts[s["triageProject"]] = project_counts.get(s["triageProject"], 0) + 1
        sessions.append(s)

    return {
        "sessions": sessions,
        # Passed straight through: the page's status poll needs every id the
        # scan SAW, not just the ones it could summarize, or an unsummarizable
        # transcript makes each poll trigger another full rescan (see
        # sessions.py's all_ids).
        "allSessionIds": base.get("allSessionIds", []),
        "statuses": STATUSES,
        "statusCounts": status_counts,
        "unreadCount": unread_count,
        "projectCounts": project_counts,
        "tagCounts": tag_counts,
    }


try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
