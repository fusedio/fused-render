"""Cursor CLI (`cursor-agent`) as a backend. Facts read from the CLI's own
bundle, release 2026.11.19-f005e9d (docs/CURSOR_BACKEND.md §10):

* driven over ACP (`cursor-agent acp`, JSON-RPC on stdio — the same protocol
  Copilot, Gemini and OpenCode speak, so `claude_agent/cursor_host.py` is
  `gemini_host.py`'s twin); never `cursor-agent -p` for a session (that is
  the recap's one-shot only, D16);
* the model is a live ACP request (`session/set_model`), so a model change
  never respawns. The host declares `parameterizedModelPicker` in its client
  capabilities, so model ids are Cursor's BASE model names (`auto`,
  `gpt-5.5`, …) rather than its exploded variant strings;
* Cursor's ACP modes are `agent` / `plan` / `ask` — not approval modes. The
  fused approval modes are the HOST's policy for answering
  `session/request_permission` (D5), so a mode switch is live too;
* the session id is Cursor's own (`session/new` mints a uuid4), learned from
  the host's init row as Gemini's is; a resume is `session/load`, which
  replays the history before it answers;
* no reasoning-effort control: the thinking level is a per-model parameter
  Cursor picks a default for, so `efforts` is empty and every model row says
  `reasoning=False`;
* no `ask_user` tool, no background tasks, no checkpoints the chat can read,
  no Artifact tool, no EnterWorktree tool, no plan card in v1;
* a second `session/prompt` cancels the running one, so mid-turn messages
  are held by the host until the turn's `result`.

The model list is a seed, not a probe: `cursor_health` asks the CLI for the
account's models (`cursor/list_available_models`, an ACP extension method)
and stores them in its snapshot, which `discovered_models` serves in place
of the one-row seed below. `auto` first because it is Cursor's own default.

`auto` is fused's name, not a wire id: the CLI's Auto is a listed row
(`auto-smart`, named "Auto", observed on 2026.11.19-f005e9d) and
`session/set_model auto` is refused, so the host resolves `auto` to that row
(`cursor_host._Host._wire_model`). A discovered list that carries Cursor's own
Auto row serves it first, in place of the seed, and defaults to it.
"""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

AUTO = Model("auto", "Auto (Cursor picks)", reasoning=False)
MODELS = (AUTO,)

#: No effort control (D7): the thinking level is a model parameter.
EFFORTS: tuple[str, ...] = ()
#: No plan mode in v1 (D5). The three offered are the host's answer policy
#: for permission cards: prompt = forward every card, acceptEdits = allow
#: edit/delete cards itself, auto = allow every card itself.
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
    (`cursor_health.normalize_models` rows), as `Model`s with `auto` first —
    or None when nothing is cached, so the static `MODELS` stand."""
    rows = snapshot.get("models") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or not rows:
        from fused_render import cursor_health
        rows = cursor_health.cached_models()
    if not rows:
        return None
    # Cursor's own Auto row, when the account lists one, stands in for the
    # seed (one "Auto", not two) and goes first.
    own = next((r for r in rows if isinstance(r, dict) and _is_auto(r)), None)
    if own is not None:
        rows = [own] + [r for r in rows if r is not own]
        out, seen = [], set()
    else:
        out, seen = [AUTO], {AUTO.id}
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append(Model(mid, str(row.get("label") or mid), reasoning=False,
                         efforts=()))
    return tuple(out) if len(out) > 1 or own is not None else None


def _is_auto(row: dict) -> bool:
    """Cursor's own Auto row: an id that is or starts with `auto`, or the
    label "Auto" (the host's `_wire_model` rule)."""
    mid = str(row.get("id") or "").strip()
    return mid.startswith("auto") or str(row.get("label") or "").strip().lower() == "auto"


CURSOR = Backend(
    id="cursor",
    label="Cursor",
    models=MODELS,
    default_model="auto",
    efforts=EFFORTS,
    modes=MODES,
    caps=Caps(
        plan_mode=False,
        # Both live: `session/set_model` is an ACP request, and the approval
        # mode is the host's own answer policy (docs/CURSOR_BACKEND.md D5).
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
    # The CLI's own wording: "Authentication required. Please run 'agent
    # login' first…" (authRequired on session/new, observed: the CLI names
    # its `agent` alias whichever name it was run by), "Not
    # logged in" (`status`), "The provided API key is invalid." (an API key
    # in CURSOR_API_KEY that the backend refused).
    account_error_markers=(
        "authentication required", "not logged in", "not signed in",
        "api key is invalid", "unauthenticated", "unauthorized",
        "login required", "agent login",
    ),
    limit_error_markers=(
        "rate limit", "usage limit", "quota", "too many requests", "429",
        "resource_exhausted",
    ),
    terminal_resume=_terminal_resume,
    health=_health,
    health_cached=_health_cached,
    routes_prefix="/api/cursor",
    pref_key="cursor_enabled",
    discovered_models=discovered_models,
    # The discovered list starts with an Auto row (Cursor's own, or the
    # seed when the account lists none): the default is that row.
    default_from_discovered=True,
)
