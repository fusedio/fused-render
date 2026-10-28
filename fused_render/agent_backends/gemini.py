"""Google Gemini CLI as a backend. Facts verified against Gemini CLI 0.65.0
(`@google/gemini-cli`, npm) on 2026-10-23, signed out (docs/GEMINI_BACKEND.md
§10 verified):

* driven over ACP (`gemini --acp`, JSON-RPC on stdio — the same protocol
  Copilot speaks, so `claude_agent/gemini_host.py` is `copilot_host.py`'s
  twin) ; never `gemini -p` for a session (that is the recap's one-shot
  only, D16);
* model is a spawn flag (`-m`) AND a live ACP request (`session/set_model`),
  so a model change never respawns; the approval mode is `session/set_mode`
  with Gemini's own ids (`default`, `autoEdit`, `yolo`), also live;
* the session id is Gemini's own (`session/new` mints a uuid4), learned from
  the host's init row as Copilot's is; a resume is `session/load`, which
  replays the history the way Copilot's does;
* no reasoning-effort control of any kind: `efforts` is empty and every
  model row says `reasoning=False`, so the effort pill never shows;
* no `ask_user` tool, no background tasks, no file-history checkpoints the
  chat can read, no Artifact tool, no EnterWorktree tool, no plan card
  (Gemini's own plan mode is experimental and not offered);
* a second `session/prompt` cancels the running one (ACP), so mid-turn
  messages are held by the host until the turn's `result`.

The model list is a seed, not a probe: `session/new` answers with
`models.availableModels` for the signed-in account (the preview ids show
only with preview access), and `gemini_health` stores that list in its
snapshot, which `discovered_models` serves in place of the shortlist below.
`auto` first because it is the CLI's own default. Ids are passed through to
`-m` unvalidated, as the other agents' are.
"""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

AUTO = Model("auto", "Auto (Gemini picks)", reasoning=False)
MODELS = (
    AUTO,
    Model("gemini-3-pro-preview", "Gemini 3 Pro (preview)", reasoning=False),
    Model("gemini-3-flash-preview", "Gemini 3 Flash (preview)", reasoning=False),
    Model("gemini-2.5-pro", "Gemini 2.5 Pro", reasoning=False),
    Model("gemini-2.5-flash", "Gemini 2.5 Flash", reasoning=False),
    Model("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", reasoning=False),
)

#: Gemini CLI has no reasoning-effort flag or request (D7): nothing to offer.
EFFORTS: tuple[str, ...] = ()
#: No plan mode (D5): Gemini's own `plan` approval mode is experimental and
#: there is no plan card to approve. The three offered map onto Gemini's
#: approval modes the host sends with `session/set_mode` — prompt = `default`,
#: acceptEdits = `autoEdit`, auto = `yolo`.
MODES = ("prompt", "acceptEdits", "auto")


def _terminal_resume(cwd: str, session_id: str) -> str:
    cmd = "gemini"
    if session_id:
        cmd += " --resume " + shlex.quote(session_id)
    return f"cd {shlex.quote(cwd)} && {cmd}"


def _health() -> dict:
    from fused_render import gemini_health
    return gemini_health.summary()


def _health_cached():
    from fused_render import gemini_health
    cached = gemini_health.cached()
    return gemini_health._public(cached) if cached else None


def discovered_models(snapshot=None):
    """The account's models as the cached health snapshot lists them
    (`gemini_health.normalize_models` rows off `session/new`'s
    `availableModels`), as `Model`s with `auto` first — or None when nothing
    is cached, so the static `MODELS` stand. Every row says `reasoning=False`:
    the CLI takes no effort for any model."""
    rows = snapshot.get("models") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or not rows:
        from fused_render import gemini_health
        rows = gemini_health.cached_models()
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


GEMINI = Backend(
    id="gemini",
    label="Gemini",
    models=MODELS,
    default_model="auto",
    efforts=EFFORTS,
    modes=MODES,
    caps=Caps(
        plan_mode=False,
        # Both live switches True: `session/set_model` and `session/set_mode`
        # are ACP requests the host sends between turns, so `_send` never
        # answers `{respawn: True}` for Gemini (docs/GEMINI_BACKEND.md §7).
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
    # Seeded from the CLI's own wording (`session/new` signed out: "Gemini API
    # key is missing or not configured." / "Authentication required."; the
    # OAuth client's "invalid_grant" on a revoked token) and the Google API's
    # error vocabulary; widened from real logs (D13). NOT here on purpose:
    # `PERMISSION_DENIED` (Google's word for "this API is not enabled on
    # that project" and for IAM, a signed-in user's problem that Sign in
    # does not fix) and the bare number 401 (a byte count or a port is not
    # a login failure) — "unauthorized" is the word an HTTP 401 carries.
    account_error_markers=(
        "api key is missing", "authentication required", "not logged in",
        "not signed in", "unauthenticated", "invalid_grant", "login required",
        "sign in", "unauthorized",
    ),
    limit_error_markers=(
        "resource_exhausted", "rate limit", "quota", "too many requests", "429",
    ),
    terminal_resume=_terminal_resume,
    health=_health,
    health_cached=_health_cached,
    routes_prefix="/api/gemini",
    pref_key="gemini_enabled",
    discovered_models=discovered_models,
)
