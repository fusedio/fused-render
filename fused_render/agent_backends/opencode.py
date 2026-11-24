"""OpenCode (`opencode-ai`, npm) as a backend. Facts verified against
OpenCode 1.19.5 on 2026-11-07 (docs/OPENCODE_BACKEND.md §10 verified):

* driven over ACP (`opencode acp`, JSON-RPC on stdio — the wire Copilot and
  Gemini speak, so `claude_agent/opencode_host.py` is `gemini_host.py`'s
  twin); never `opencode run` for a session (the recap's one-shot only, D16);
* the model rides the injected config (`OPENCODE_CONFIG_CONTENT`'s `model`)
  at spawn AND `session/set_model` live, so a model change never respawns;
  the approval policy is the config's `permission` map, fixed at spawn, so a
  MODE change is a respawn (`caps.live_mode_switch = False`, D5);
* the session id is OpenCode's own (`session/new` mints `ses_<id>`), learned
  from the host's init row as Copilot's is; a resume is `session/load`;
* ids are `provider/model` throughout (D7) — fused's `Model.id` IS that
  string; `fused/<catalog id>` names a local model served by the gateway;
* no reasoning-effort control of any kind: `efforts` is empty and every
  model row says `reasoning=False`;
* no `ask_user` tool, no background tasks, no checkpoints, no Artifact tool,
  no EnterWorktree tool, no plan card (OpenCode's `plan` agent is a
  different concept and is not offered);
* a second `session/prompt` cancels the running one (ACP), so mid-turn
  messages are held by the host until the turn's `result`.

The model list is a seed, not a probe (D11a): the health probe's
`session/new` answers `configOptions[id=model].options` for every provider
models.dev knows, which `opencode_health` filters down to the connected
providers plus `fused/…` and stores in its snapshot; `discovered_models`
serves that list in place of the shortlist below.
"""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

MODELS = (
    Model("anthropic/claude-sonnet-5-5", "Claude Sonnet 5.5", reasoning=False),
    Model("openai/gpt-5.5", "GPT-5.5", reasoning=False),
)
DEFAULT_MODEL = "anthropic/claude-sonnet-5-5"

#: OpenCode has no reasoning-effort request (D7): nothing to offer.
EFFORTS: tuple[str, ...] = ()
#: No plan mode (D5). The three offered map onto the `permission` block of
#: the injected config (`opencode_config.PERMISSIONS`): prompt = every edit
#: and shell command asks, acceptEdits = edits run and shell asks, auto =
#: nothing asks.
MODES = ("prompt", "acceptEdits", "auto")


def _terminal_resume(cwd: str, session_id: str) -> str:
    cmd = "opencode"
    if session_id:
        cmd += " --session " + shlex.quote(session_id)
    return f"cd {shlex.quote(cwd)} && {cmd}"


def _health() -> dict:
    from fused_render import opencode_health
    return opencode_health.summary()


def _health_cached():
    from fused_render import opencode_health
    cached = opencode_health.cached()
    return opencode_health._public(cached) if cached else None


def _cached_rows():
    try:
        from fused_render import opencode_health
    except ImportError:
        return None
    getter = getattr(opencode_health, "cached_models", None)
    return getter() if callable(getter) else None


def _local_rows():
    """The text models INSTALLED on this machine, as the gateway serves them
    (`catalog.local_agent_models` through the stdlib `opencode_config`
    shim — the same list the injected `fused` provider names, D20), as
    `fused/<catalog id>` rows. First in the picker: on a machine that has
    one, coding on it is the point of this backend. Never raises."""
    try:
        from fused_render.agent_backends import opencode_config
        rows = opencode_config.local_models()
    except Exception:  # noqa: BLE001 - a catalog read is never a 500
        return []
    out = []
    for row in rows or ():
        mid = str(row.get("id") or "").strip() if isinstance(row, dict) else ""
        if not mid:
            continue
        label = str(row.get("label") or mid)
        if not row.get("tools", True):
            label += " · no tools"
        out.append(Model("fused/" + mid, label, reasoning=False, efforts=(),
                         category="local", vendor="fused"))
    return out


def discovered_models(snapshot=None):
    """The usable models: every LOCAL model installed here (`_local_rows`,
    first), then the cloud rows the cached health snapshot lists (off the
    probe's `configOptions[id=model].options`, filtered per D11a; the
    probe's own `fused/…` echoes are dropped in favour of the live local
    list), else the static cloud seeds. None only when there is nothing
    local and nothing cached, so the static `MODELS` stand. Every row says
    `reasoning=False`: OpenCode takes no effort for any model."""
    local = _local_rows()
    rows = snapshot.get("models") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or not rows:
        rows = _cached_rows()
    cloud = []
    seen = set()
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen or mid.startswith("fused/"):
            continue
        seen.add(mid)
        cloud.append(Model(mid, str(row.get("label") or mid), reasoning=False,
                           efforts=()))
    if not local and not cloud:
        return None
    if not cloud:
        cloud = list(MODELS)
    return tuple(local + cloud)


OPENCODE = Backend(
    id="opencode",
    label="OpenCode",
    models=MODELS,
    default_model=DEFAULT_MODEL,
    efforts=EFFORTS,
    modes=MODES,
    caps=Caps(
        plan_mode=False,
        # `session/set_model` is an ACP request the host sends between turns;
        # the mode is the `permission` config fixed at spawn, so a mode
        # change answers `{respawn: True}` (docs/OPENCODE_BACKEND.md D5).
        live_model_switch=True,
        live_mode_switch=False,
        snapshots=False,
        artifacts=False,
        worktrees=False,
        bg_tasks=False,
        ask_user=False,
        terminal_mirror=False,
        mid_turn_messages=False,
    ),
    # D13: a provider that lacks its credential fails the `session/prompt`,
    # never the handshake, with the provider SDK's own wording; these are the
    # common ones plus the host's own `not signed in` text.
    account_error_markers=(
        "no api key", "not authenticated", "unauthorized", "invalid api key",
        "provider not found", "model not found", "not signed in",
    ),
    limit_error_markers=(
        "rate limit", "quota", "too many requests", "429", "overloaded",
    ),
    terminal_resume=_terminal_resume,
    health=_health,
    health_cached=_health_cached,
    routes_prefix="/api/opencode",
    pref_key="opencode_enabled",
    discovered_models=discovered_models,
    default_from_discovered=True,
)
