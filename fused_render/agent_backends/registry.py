"""The one answer to "which agents exist, which are on, which is the default".

Read by: `GET /api/agent-backends` (the catalog every picker renders),
`prefs` (validation of the two switches and `default_backend`),
`routers/tasks` + `schedule` (the 400 gate on a disabled backend),
`routers/claude_agent` (the same gate in front of `agent.main`), and the
Claude-only surfaces that follow `claude_enabled` (recap, health warm-up).

`agent.py` does NOT import this (stdlib-only, loaded by path). It receives
`backend` as a request field and enforces the gate through the ONE check in
`_start` that reads `prefs.json` directly via `agent_roots` — see
docs/COPILOT_BACKEND.md §gate.
"""
from __future__ import annotations

from typing import Optional

from fused_render.agent_backends.base import Backend
from fused_render.agent_backends.claude import CLAUDE
from fused_render.agent_backends.copilot import COPILOT

BACKENDS: dict[str, Backend] = {CLAUDE.id: CLAUDE, COPILOT.id: COPILOT}
ORDER = (CLAUDE.id, COPILOT.id)
DEFAULT = CLAUDE.id


def get(backend_id: Optional[str]) -> Backend:
    """The backend for an id; an unknown or empty id reads as Claude (every
    pre-existing record has no field)."""
    return BACKENDS.get(backend_id or "", CLAUDE)


def is_known(backend_id: Optional[str]) -> bool:
    return (backend_id or DEFAULT) in BACKENDS


def enabled_ids() -> list[str]:
    """In ORDER. Never empty: prefs refuses the PUT that would clear the last
    one, and a hand-edited prefs.json that clears both reads as Claude on."""
    from fused_render.shell import prefs
    ids = [b for b in ORDER if prefs.backend_enabled(b)]
    return ids or [DEFAULT]


def is_enabled(backend_id: Optional[str]) -> bool:
    return (backend_id or DEFAULT) in enabled_ids()


def default_id() -> str:
    """`prefs.default_backend()` when it names an enabled backend, else the
    first enabled one."""
    from fused_render.shell import prefs
    wanted = prefs.default_backend()
    ids = enabled_ids()
    return wanted if wanted in ids else ids[0]


def disabled_error(backend_id: str) -> dict:
    """The 400 body every spawn entry answers with (D17)."""
    label = get(backend_id).label
    return {"error": f"{label} is turned off in Preferences",
            "reason": "backend_disabled", "backend": backend_id}


def foreign_model_error(backend_id: str, model: str) -> Optional[dict]:
    """The 400 body for a model that belongs to ANOTHER agent's catalog, or
    None. The server half of the pickers' `isForeignModel`
    (platform/lib/agent-backends.ts): a model on this agent's own list, or on
    nobody's list, passes through unvalidated (a user may type an id the
    catalog has not caught up with); only a model another agent offers is
    refused, because that is a leak from a form or a page that defaulted to
    the wrong agent, and it fails only when the turn spawns (`copilot --model
    opus`: 'Model "opus" from --model flag is not available.')."""
    model = str(model or "").strip()
    own = get(backend_id)
    if not model or model in own.model_ids():
        return None
    for b in BACKENDS.values():
        if b.id != own.id and model in b.model_ids():
            return {"error": f"{model!r} is a {b.label} model, not one {own.label} runs",
                    "reason": "unsupported", "backend": own.id}
    return None


def catalog(with_health: bool = True) -> dict:
    """`GET /api/agent-backends` payload: enabled backends in ORDER, the
    default, and every backend's description under `all` (so the Preferences
    page can render a switch for a disabled one). Health is ALWAYS the cached
    snapshot, never a probe: this payload sits in front of every picker and a
    cold Copilot cache is an ACP spawn plus `gh auth token` plus up to three
    login-shell probes (~15 s worst case). The entry points warm both caches
    at startup (`*_health.warm_in_background`), and a surface that needs a
    fresh answer asks that backend's own `/health` route, which measures.
    `null` here means "not measured yet", and the UI renders it as unknown."""
    ids = enabled_ids()
    out = []
    everything = []
    for bid in ORDER:
        b = BACKENDS[bid]
        health = None
        if with_health and b.health_cached is not None:
            try:
                health = b.health_cached()
            except Exception:  # noqa: BLE001 - a probe failure is "unknown", not a 500
                health = None
        row = b.to_catalog(enabled=bid in ids, health=health)
        everything.append(row)
        if bid in ids:
            out.append(row)
    return {"backends": out, "default": default_id(), "all": everything}
