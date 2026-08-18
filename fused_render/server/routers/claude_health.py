"""GET /api/claude/health — is Claude Code usable on this machine.

Its own endpoint rather than a field on /api/config, deliberately: /api/config is
read on every page load and by the status-banner poll, and the facts here are
backed by process spawns behind a disk cache. Bolting them onto that payload
would put a version probe on the hot path to pay for a strip that only renders
while something is wrong.

It is also NOT `/api/claude-config/status`, which stays what it is: one
`isdir(~/.claude)` answering "is there config to edit". That was always an honest
gate for the Preferences tab and was never a claim about whether Claude Code
works — this endpoint is that claim.
"""
import logging

from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool

from fused_render import claude_health
from fused_render.server.common import _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/claude/health")
async def api_claude_health():
    """The cached snapshot. A read — no X-Fused guard.

    In a threadpool because a cold cache measures inline (a `--version` spawn,
    and possibly the login-shell probe), and blocking the event loop would stall
    every other request in the app — including the ones the page fires beside
    this one. A warm cache is a single small file read and returns immediately.
    """
    return await run_in_threadpool(claude_health.summary)


@router.post("/api/claude/health/refresh")
async def api_claude_health_refresh(x_fused: str | None = Header(default=None)):
    """Re-probe now, ignoring the cache — the "Check again" action.

    Carries the D3 X-Fused guard: it is a POST that spawns subprocesses (and on
    POSIX the user's login shell), which is not something a blind cross-origin
    form may kick off, even though what it writes is only a cache.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return await run_in_threadpool(claude_health.summary_refreshed)
