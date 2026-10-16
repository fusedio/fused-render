"""`GET /api/agent-backends` — the agent catalog every picker reads.

One payload (docs/COPILOT_BACKEND.md D11): the ENABLED backends in order,
the default, and every backend's description under `all` so the Preferences
page can draw a switch for one that is off. Each row carries the models,
efforts, modes and caps the frontend used to keep hand-synced copies of, plus
the backend's health snapshot (`claude_health.summary()` and its Copilot
twin) — which may spawn a probe on a cold cache, hence the threadpool.

A read, so no X-Fused guard, like `/api/claude/health` beside it.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from fused_render.agent_backends import registry

router = APIRouter()


@router.get("/api/agent-backends")
async def api_agent_backends():
    return await run_in_threadpool(registry.catalog)
