"""GitHub Copilot CLI as a backend. Facts verified against Copilot CLI
1.0.98 on 2026-10-16 (docs/COPILOT_BACKEND.md §verified):

* driven over ACP (`copilot --acp`, JSON-RPC on stdio) by
  `claude_agent/copilot_host.py`; never `copilot -p` for a session;
* model and reasoning effort are spawn-time only (ACP exposes no model
  option), so a model change respawns and `session/load`s;
* `allow_all` and the session mode CAN be switched live
  (`session/set_config_option`, `session/set_mode`);
* no `ask_user` tool, no background tasks, no file-history checkpoints the
  chat can read, no Artifact tool, no EnterWorktree tool;
* a second `session/prompt` cancels the running one, so mid-turn messages
  are held by the host until the turn's `result`.

The model list is a catalog, not a probe: ACP does not list models and the
`-p` stream only names the auto-routing candidates. `auto` lets Copilot
pick; the named ids are the ones its own `/model` picker offered on the
build machine. Ids are passed through to `--model` unvalidated, as Claude's
are, so a user may type one this list has not caught up with.
"""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

MODELS = (
    Model("auto", "Auto (Copilot picks)"),
    Model("gpt-5.4", "GPT-5.4"),
    Model("gpt-5.4-mini", "GPT-5.4 mini"),
    Model("claude-sonnet-4.5", "Claude Sonnet 4.5 (via Copilot)"),
    Model("claude-opus-4.5", "Claude Opus 4.5 (via Copilot)"),
    Model("gemini-3-pro", "Gemini 3 Pro (via Copilot)"),
)

EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: No plan mode (D12/Q2): ACP has a plan mode but no card to approve a plan.
MODES = ("prompt", "acceptEdits", "auto")


def _terminal_resume(cwd: str, session_id: str) -> str:
    cmd = "copilot"
    if session_id:
        cmd += " --resume=" + shlex.quote(session_id)
    return f"cd {shlex.quote(cwd)} && {cmd}"


def _health() -> dict:
    from fused_render import copilot_health
    return copilot_health.summary()


def _health_cached():
    from fused_render import copilot_health
    cached = copilot_health.cached()
    return copilot_health._public(cached) if cached else None


COPILOT = Backend(
    id="copilot",
    label="Copilot",
    models=MODELS,
    default_model="auto",
    efforts=EFFORTS,
    modes=MODES,
    caps=Caps(
        plan_mode=False,
        live_model_switch=False,
        live_mode_switch=True,
        snapshots=False,
        artifacts=False,
        worktrees=False,
        bg_tasks=False,
        ask_user=False,
        terminal_mirror=False,
        mid_turn_messages=False,
    ),
    # Seeded from the CLI's own wording; widened from real logs (D13).
    account_error_markers=(
        "not logged in", "auth_required", "authentication required",
        "copilot login", "no copilot access", "not authorized",
    ),
    limit_error_markers=(
        "ai credits", "premium request", "rate limit", "quota",
    ),
    terminal_resume=_terminal_resume,
    health=_health,
    health_cached=_health_cached,
    routes_prefix="/api/copilot",
    pref_key="copilot_enabled",
)
