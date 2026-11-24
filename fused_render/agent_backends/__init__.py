"""Agent backends for Tasks and Sessions: Claude Code, GitHub Copilot CLI,
OpenAI Codex CLI, Google Gemini CLI, OpenCode and Cursor CLI.

    from fused_render.agent_backends import registry
    registry.get("copilot").caps.plan_mode     # False
    registry.enabled_ids()                     # ["claude"] by default
    registry.catalog()                         # GET /api/agent-backends

Roots (where transcripts and registries live) are in
`templates/shared/agent_roots.py`, stdlib-only, because the chat agent and
the session hosts load it by path. Design: docs/COPILOT_BACKEND.md (the
shared seam), docs/CODEX_BACKEND.md (the third backend) and
docs/GEMINI_BACKEND.md (the fourth), docs/OPENCODE_BACKEND.md (the fifth)
and docs/CURSOR_BACKEND.md (the sixth).
"""
from fused_render.agent_backends import registry  # noqa: F401
from fused_render.agent_backends.base import Backend, Caps, Model  # noqa: F401
