"""GET /api/github/status — is the GitHub CLI usable on this machine.

Mirrors fused_render/server/routers/claude_health.py's own top two endpoints
almost verbatim: the read is unguarded because a browser already blocks a
foreign page from reading our response, and the refresh POST carries the D3
X-Fused guard because it is a POST that spawns subprocesses — not something a
blind cross-origin form may kick off, even though what it writes is only a
cache.

Its own endpoint rather than a field on /api/config, for the same reason
claude_health earned one: the facts here are backed by process spawns behind
a disk cache, and bolting them onto a payload read on every page load would
put a version probe on a hot path to pay for a "Publish to GitHub" surface
that only renders while it matters.
"""
from fastapi import APIRouter, Header
from fastapi.concurrency import run_in_threadpool

from fused_render import github_setup
from fused_render.server.common import _require_fused

router = APIRouter()


@router.get("/api/github/status")
async def api_github_status():
    """The cached snapshot. A read — no X-Fused guard.

    In a threadpool because a cold cache measures inline (a `--version` spawn
    and a `gh auth status` spawn), and blocking the event loop would stall
    every other request in the app — including the ones the page fires beside
    this one. A warm cache is a single small file read and returns
    immediately.
    """
    return await run_in_threadpool(github_setup.summary)


@router.post("/api/github/status/refresh")
async def api_github_status_refresh(x_fused: str | None = Header(default=None)):
    """Re-probe now, ignoring the cache — the "Check again" action.

    Carries the D3 X-Fused guard: it is a POST that spawns subprocesses, which
    is not something a blind cross-origin form may kick off, even though what
    it writes is only a cache.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    return await run_in_threadpool(github_setup.summary_refreshed)
