"""GET /api/claude-artifacts — every Artifact published from this machine's
Claude Code sessions, for the Explorer homepage's artifacts index.

A publish leaves no index behind, only a `frame-link` record inside the session
transcript it happened in, so the listing is recovered by scanning
~/.claude/projects/<encoded-cwd>/*.jsonl and joining those records to the
Artifact tool calls that carry the author's description and favicon. All of that
— the scan, the dedupe by hosted url across sessions, and what one listed
artifact IS — lives in `fused_render/claude_artifacts.py` rather than in this
handler, for the same reason the apps walk lives in `app_listing.py`: those are
the rules worth testing and reusing directly, and a route is not the place for
them.

Read-only and unguarded, like the neighbouring /api/claude-sessions: it reads
transcripts and touches nothing.
"""
from fastapi import APIRouter

from fused_render import claude_artifacts
from fused_render._view_url_codec import canonical_fs_path

router = APIRouter()


@router.get("/api/claude-artifacts")
def api_claude_artifacts(cwd: str | None = None):
    # `cwd` scopes the listing to sessions run in one directory — the claude
    # template's "artifacts for this file/folder" section. Compared in the
    # shell's canonical form, the same form the entries themselves carry.
    artifacts = claude_artifacts.list_artifacts()
    if cwd is not None:
        want = canonical_fs_path(cwd)
        artifacts = [a for a in artifacts if a["cwd"] and canonical_fs_path(a["cwd"]) == want]
    return {"artifacts": artifacts}
