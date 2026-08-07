"""GET /api/claude-sessions — Claude Code project folders, for the Explorer
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

from fastapi import APIRouter

router = APIRouter()

# CLAUDE_CONFIG_DIR wins where set (same rule as user_skills.py, the claude/
# claude_split agents' CLAUDE_DIR, and templates/shared/file_history.py's
# config_dir()) — duplicated locally rather than imported cross-package, same
# posture as those sites.
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")


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
    folders = [
        {"path": path, "lastActive": datetime.fromtimestamp(mtime, timezone.utc).isoformat()}
        for path, mtime in latest.items()
    ]
    folders.sort(key=lambda e: e["lastActive"], reverse=True)
    return {"folders": folders}
