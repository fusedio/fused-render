"""Scroll-to-message: `?msg=<transcript uuid>` lands on ONE turn.

The Tasks list links a message, not just a session: its rows carry the uuid of
the transcript record the server read the prompt out of. The chat can only
match that anchor if `_history` hands the uuid back on every restored user
turn, so the backend half is pinned here. The page half (reading `msg`,
scrolling to it) lives in the native chat under frontend/src/apps/claude.
"""

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT = os.path.join(_ROOT, "fused_render", "claude_agent", "agent.py")


def test_a_restored_user_turn_carries_its_transcript_uuid():
    with open(_AGENT, encoding="utf-8") as f:
        agent = f.read()
    assert '"uuid": str(row.get("uuid") or "")' in agent
