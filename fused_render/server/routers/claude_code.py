"""The Claude Code related-parts API — a thin HTTP skin over `claude_sessions`.

Three read-only GETs, so any UI (the shell's history view today, anything else
tomorrow) can pivot between the pieces of a session's work:

* ``GET /api/claude/sessions`` — the consulted sessions, newest first.
* ``GET /api/claude/sessions/{session_id}/files`` — transcript → every file it
  touched (source code included — see `session_files`' ``viewable`` hint).
* ``GET /api/claude/related?path=…`` — file → the sessions that touched it AND
  its checkpointed versions: the git-history-like payload.

All of the logic lives in ``fused_render/claude_sessions.py`` — these handlers
only translate "unknown" into a 404 and a bad path into a 400, so the module
stays usable without a server at all (the decoupling is the point, same as
`app_listing` vs the apps router).

Deliberately NOT gated on the ``discover_claude_sessions`` preference: that
switch is about the AMBIENT listing — transcripts being walked on every Home
render without being asked. These endpoints run only on an explicit request
about a specific file or session, which is the user asking. No X-Fused guard
for the same reason the other read endpoints carry none (server/common.py):
reads are already safe cross-origin.
"""
import os

from fastapi import APIRouter

from fused_render import claude_sessions
from fused_render.server.common import _error

router = APIRouter()


@router.get("/api/claude/sessions")
def api_claude_sessions():
    return {"sessions": claude_sessions.list_sessions()}


@router.get("/api/claude/sessions/{session_id}/files")
def api_claude_session_files(session_id: str):
    # The id is matched against the enumerated transcript window inside
    # session_files — never joined into a path — so a crafted value can only
    # ever land here, as a 404.
    data = claude_sessions.session_files(session_id)
    if data is None:
        return _error("unknown session (not among the transcripts consulted)",
                      status=404)
    return data


@router.get("/api/claude/related")
def api_claude_related(path: str = ""):
    expanded = os.path.expanduser(path)
    if not path or not os.path.isabs(expanded):
        return _error("'path' must be an absolute path")
    return claude_sessions.related(expanded)
