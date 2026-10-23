"""GitHub Copilot CLI as a backend. Facts verified against Copilot CLI
1.0.98 on 2026-10-16 and 1.0.102 on 2026-10-22 (docs/COPILOT_BACKEND.md
§verified):

* driven over ACP (`copilot --acp`, JSON-RPC on stdio) by
  `claude_agent/copilot_host.py`; never `copilot -p` for a session;
* model and reasoning effort are spawn-time only (ACP exposes no model
  option), so a model change respawns and `session/load`s;
* 1.0.102 fails `session/new` when `--reasoning-effort` is given with
  `auto` ("Reasoning effort is not supported when using the "auto" model")
  or with either Claude model ("does not support reasoning effort
  configuration"); the GPT and Gemini ids accept every level, and 1.0.98
  accepted all 42 model × effort pairs. ACP has no `models.list` to ask, so
  `Model.reasoning` records it and `agent._copilot_argv` drops the flag;
* `allow_all` and the session mode CAN be switched live
  (`session/set_config_option`, `session/set_mode`);
* no `ask_user` tool, no background tasks, no file-history checkpoints the
  chat can read, no Artifact tool, no EnterWorktree tool;
* a second `session/prompt` cancels the running one, so mid-turn messages
  are held by the host until the turn's `result`.

The static model list below is the FIRST-FRAME shortlist, not the truth:
which ids `--model` takes is per account (the plan, an org's model policy)
and moves with every release. ACP does not list models, but the CLI's
headless SDK server does (`models.list`, verified 2026-10-23 on 1.0.103),
and `copilot_health` asks it on every measure and stores the answer in the
health snapshot as `models` — id, display name, whether `--reasoning-effort`
applies and which levels, the picker category and billing multiplier.
`discovered_models` turns that into `Model`s with `auto` kept first (the
server never lists the virtual `auto`), the catalog serves them in place of
`MODELS`, and `agent._copilot_argv` reads the same snapshot to decide
whether the effort flag rides. With nothing cached (a fresh install, a
signed-out CLI) the shortlist shows, and ids are still passed through to
`--model` unvalidated, as Claude's are.
"""
from __future__ import annotations

import shlex

from fused_render.agent_backends.base import Backend, Caps, Model

AUTO = Model("auto", "Auto (Copilot picks)", reasoning=False)
MODELS = (
    AUTO,
    Model("gpt-5.4", "GPT-5.4"),
    Model("gpt-5.4-mini", "GPT-5.4 mini"),
    Model("claude-sonnet-4.5", "Claude Sonnet 4.5 (via Copilot)", reasoning=False),
    Model("claude-opus-4.5", "Claude Opus 4.5 (via Copilot)", reasoning=False),
    Model("gemini-3-pro", "Gemini 3 Pro (via Copilot)"),
)
#: The ids `_copilot_argv` must not pair with `--reasoning-effort`. A COPY
#: lives in agent.py (`_COPILOT_NO_EFFORT_MODELS`), pinned equal by a test.
NO_EFFORT_MODELS = tuple(m.id for m in MODELS if not m.reasoning)

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


def discovered_models(snapshot=None):
    """The account's models as the cached health snapshot lists them, as
    `Model`s with `auto` first — or None when nothing is cached, so the
    static `MODELS` stand. `snapshot` is the catalog's already-read health
    row; without one the raw cache file is read (`copilot_health.
    cached_models`, which skips the freshness gates on purpose: the plan's
    list does not go stale with the PATH). Rows the snapshot stores are
    `copilot_health.normalize_models`'s; anything malformed is skipped."""
    rows = snapshot.get("models") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list) or not rows:
        from fused_render import copilot_health
        rows = copilot_health.cached_models()
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
        efforts = row.get("efforts")
        efforts = tuple(str(e) for e in efforts if e in EFFORTS) \
            if isinstance(efforts, list) else None
        reasoning = bool(efforts) if efforts is not None else bool(row.get("reasoning", True))
        multiplier = row.get("multiplier")
        seen.add(mid)
        out.append(Model(
            mid, str(row.get("label") or mid),
            reasoning=reasoning,
            efforts=efforts if reasoning else (),
            default_effort=str(row.get("default_effort") or ""),
            category=str(row.get("category") or "") or None,
            vendor=str(row.get("vendor") or "") or None,
            multiplier=float(multiplier) if isinstance(multiplier, (int, float))
            and not isinstance(multiplier, bool) else None,
        ))
    return tuple(out) if len(out) > 1 else None


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
    discovered_models=discovered_models,
)
