"""Claude Code as a backend — the description of what shipped before there
was a second backend. Every flag here is True because the chat was built
around this CLI; the lists mirror the constants the chat and prefs already
hold (`VALID_DEFAULT_MODELS`, `_EFFORT_LEVELS`, `PERMISSION_MODES`), which
remain the enforcement points for the Claude path."""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

MODELS = (
    Model("fable", "Fable"),
    Model("opus", "Opus"),
    Model("sonnet", "Sonnet"),
    Model("haiku", "Haiku"),
)

EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODES = ("plan", "prompt", "acceptEdits", "auto")


def _terminal_resume(cwd: str, session_id: str) -> str:
    cmd = "claude"
    if session_id:
        cmd += " --resume " + shlex.quote(session_id)
    return f"cd {shlex.quote(cwd)} && {cmd}"


def _health() -> dict:
    from fused_render import claude_health
    return claude_health.summary()


def _health_cached():
    from fused_render import claude_health
    cached = claude_health.cached()
    return claude_health._public(cached) if cached else None


CLAUDE = Backend(
    id="claude",
    label="Claude",
    models=MODELS,
    default_model="sonnet",
    efforts=EFFORTS,
    modes=MODES,
    caps=Caps(
        plan_mode=True,
        live_model_switch=True,
        live_mode_switch=True,
        snapshots=True,
        artifacts=True,
        worktrees=True,
        bg_tasks=True,
        ask_user=True,
        terminal_mirror=True,
        mid_turn_messages=True,
    ),
    account_error_markers=(
        "invalid api key", "/login", "oauth token", "not logged in",
        "authentication_error",
    ),
    limit_error_markers=("usage limit", "session limit"),
    terminal_resume=_terminal_resume,
    health=_health,
    health_cached=_health_cached,
    routes_prefix="/api/claude",
    pref_key="claude_enabled",
)
