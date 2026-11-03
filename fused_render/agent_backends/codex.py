"""OpenAI Codex CLI as a backend. Facts verified against Codex CLI 0.163.0
(`@openai/codex`, npm) on 2026-10-21, signed in with a ChatGPT Go account
(docs/CODEX_BACKEND.md §10 verified):

* driven over `codex app-server` (JSON-RPC 2.0, newline-delimited, on
  stdio — Codex's own "app-server protocol v2", the one its VS Code
  extension speaks) by `claude_agent/codex_host.py`; never `codex exec` for
  a session (that is the recap's one-shot only, D16);
* model, reasoning effort, approval policy and sandbox are all PER TURN
  (`turn/start` carries the four), so neither a model nor a mode change
  respawns — the host stores the new value and the next turn uses it;
* the session id is Codex's own thread id (`thread/start` mints a
  uuid7-shaped one), learned from the host's init row as Copilot's is;
* no `ask_user` tool, no background tasks, no file-history checkpoints the
  chat can read, no Artifact tool, no EnterWorktree tool, no plan card;
* Codex has `turn/steer`, but v1 does not use it: the host holds a mid-turn
  message in `inbox/` until the turn's `result`, as Copilot's host does.

The model list is a seed, not a probe: `model/list` is dynamic and
plan-dependent (a Go plan listed three), so the catalog names the ids the
build machine's account offered, `gpt-6-luna` first because it is that
plan's default. Ids are passed through to `turn/start` unvalidated, as
Claude's and Copilot's are, so a user may type one this list has not caught
up with.
"""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

MODELS = (
    Model("gpt-6-luna", "GPT-6 Luna"),
    Model("gpt-6.1-sol", "GPT-6.1 Sol"),
    Model("gpt-5.6-terra", "GPT-5.6 Terra"),
    Model("gpt-5.6-luna", "GPT-5.6 Luna"),
)

EFFORTS = ("low", "medium", "high", "xhigh", "max")
#: No plan mode (D5): Codex has no plan card to approve. The three that are
#: offered map to (approvalPolicy, sandbox) pairs the host sends per turn —
#: prompt = untrusted + read-only, acceptEdits = untrusted + workspace-write,
#: auto = never + workspace-write (Codex's own `--full-auto`).
MODES = ("prompt", "acceptEdits", "auto")


def _terminal_resume(cwd: str, session_id: str) -> str:
    cmd = "codex"
    if session_id:
        cmd += " resume " + shlex.quote(session_id)
    return f"cd {shlex.quote(cwd)} && {cmd}"


def _health() -> dict:
    from fused_render import codex_health
    return codex_health.summary()


def _health_cached():
    from fused_render import codex_health
    cached = codex_health.cached()
    return codex_health._public(cached) if cached else None


CODEX = Backend(
    id="codex",
    label="Codex",
    models=MODELS,
    default_model="gpt-6-luna",
    efforts=EFFORTS,
    modes=MODES,
    caps=Caps(
        plan_mode=False,
        # Both live switches True: model, effort, approval policy and sandbox
        # ride every `turn/start`, so `_send` never answers `{respawn: True}`
        # for Codex (docs/CODEX_BACKEND.md §7, invariant 15).
        live_model_switch=True,
        live_mode_switch=True,
        snapshots=False,
        artifacts=False,
        worktrees=False,
        bg_tasks=False,
        ask_user=False,
        terminal_mirror=False,
        mid_turn_messages=False,
    ),
    # Seeded from the CLI's own wording (`account/read` answers
    # `requiresOpenaiAuth`; the CLI tells a signed-out user to run
    # `codex login`) and the OpenAI API's error vocabulary; widened from real
    # logs (D13).
    account_error_markers=(
        "not logged in", "requiresopenaiauth", "codex login", "unauthorized",
        "authentication", "login required", "sign in",
    ),
    limit_error_markers=(
        "rate limit", "usage limit", "quota", "insufficient_quota",
        "spend control", "too many requests", "429",
    ),
    terminal_resume=_terminal_resume,
    health=_health,
    health_cached=_health_cached,
    routes_prefix="/api/codex",
    pref_key="codex_enabled",
)
