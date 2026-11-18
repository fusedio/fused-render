"""Cursor CLI (`cursor-agent`, also installed as `agent`) as a backend. Facts
verified against Cursor CLI 2026.11.13 signed out, plus Cursor's ACP docs
(https://cursor.com/docs/cli/acp.md):

* driven over ACP (`cursor-agent acp`, JSON-RPC on stdio — the protocol
  Copilot, Gemini and OpenCode speak, so `claude_agent/cursor_host.py` is
  `gemini_host.py`'s twin); never `cursor-agent -p` for a session (that is
  the recap's one-shot only);
* the model is a spawn flag (`--model`), so a model change respawns; the
  approval mode is app-side (Cursor's own ACP modes `agent`/`plan`/`ask` are
  not approval modes): the host answers each `session/request_permission`
  from the mode, so a mode switch is live and host-side;
* the session id is Cursor's own (`session/new` mints it), learned from the
  host's init row as Gemini's is; a resume is `session/load`;
* no reasoning-effort control: `efforts` is empty and every model row says
  `reasoning=False`, so the effort pill never shows;
* no `ask_user` bridge (`cursor/ask_question` is answered "skipped"), no
  background tasks, no file-history checkpoints the chat can read, no
  Artifact tool, no EnterWorktree tool, no plan card (`cursor/create_plan`
  is rejected).

The model list is a seed, not a probe: `cursor-agent models` needs a login,
and `cursor_health` stores the account's list in its snapshot, which
`discovered_models` serves in place of the shortlist below. `auto` first
because it is the CLI's own default. Ids are passed through to `--model`
unvalidated, as the other agents' are (`auto` is the absence of the flag).
"""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

AUTO = Model("auto", "Auto (Cursor picks)", reasoning=False)
MODELS = (
    AUTO,
    Model("gpt-5", "GPT-5", reasoning=False),
    Model("sonnet-4.5", "Claude Sonnet 4.5", reasoning=False),
    Model("sonnet-4.5-thinking", "Claude Sonnet 4.5 (thinking)", reasoning=False),
    Model("composer-1", "Composer 1", reasoning=False),
)

#: Cursor CLI has no reasoning-effort flag or request: nothing to offer.
EFFORTS: tuple[str, ...] = ()
#: No plan mode: Cursor's own `plan` ACP mode is never switched to and there
#: is no plan card to approve. The three offered are the app's own approval
#: modes, applied by the host to each permission request — prompt = every
#: request becomes a card, acceptEdits = edits are allowed and the rest asked,
#: auto = everything is allowed.
MODES = ("prompt", "acceptEdits", "auto")


def _terminal_resume(cwd: str, session_id: str) -> str:
    cmd = "cursor-agent"
    if session_id:
        cmd += " --resume " + shlex.quote(session_id)
    return f"cd {shlex.quote(cwd)} && {cmd}"


def _health() -> dict:
    from fused_render import cursor_health
    return cursor_health.summary()


def _health_cached():
    from fused_render import cursor_health
    cached = cursor_health.cached()
    return cursor_health._public(cached) if cached else None


def discovered_models(snapshot=None):
    """The account's models as the cached health snapshot lists them
    (`cursor_health.normalize_models` rows off `cursor-agent models`), as
    `Model`s with `auto` first — or None when nothing is cached, so the
    static `MODELS` stand. Every row says `reasoning=False`: the CLI takes no
    effort for any model."""
    rows = snapshot.get("models") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or not rows:
        from fused_render import cursor_health
        rows = cursor_health.cached_models()
    if not rows:
        return None
    out = [AUTO]
    seen = {AUTO.id}
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(Model(mid, str(row.get("label") or mid), reasoning=False,
                         efforts=()))
    return tuple(out) if len(out) > 1 else None


CURSOR = Backend(
    id="cursor",
    label="Cursor",
    models=MODELS,
    default_model="auto",
    efforts=EFFORTS,
    modes=MODES,
    caps=Caps(
        plan_mode=False,
        # The model is `--model` on the spawn line, so a change respawns
        # (`_send`); the mode is the host's own answer policy, switched
        # between turns without a respawn.
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
    # Seeded from the CLI's own wording (`session/new` signed out:
    # "Authentication required. Please run 'agent login' first ...") and the
    # usual API vocabulary; widened from real logs.
    account_error_markers=(
        "authentication required", "not logged in", "not signed in",
        "unauthenticated", "login required", "agent login", "invalid api key",
        "unauthorized",
    ),
    limit_error_markers=(
        "rate limit", "usage limit", "out of usage", "quota",
        "too many requests", "429",
    ),
    terminal_resume=_terminal_resume,
    health=_health,
    health_cached=_health_cached,
    routes_prefix="/api/cursor",
    pref_key="cursor_enabled",
    discovered_models=discovered_models,
)
