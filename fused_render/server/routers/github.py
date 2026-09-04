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
from fastapi import APIRouter, Body, Header
from fastapi.concurrency import run_in_threadpool

from fused_render import github_setup
from fused_render.server.common import _error, _require_fused

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


# --- installing `gh` without a package manager -------------------------------
#
# Mirrors claude_health.py's `/api/claude/install` pair almost verbatim: a
# guarded POST to start (it downloads and writes an executable into the
# user's home, the last thing a blind cross-origin POST may be allowed to
# start) and an unguarded GET to poll (a read of module state, no spawn).


@router.post("/api/github/install")
async def api_github_install(x_fused: str | None = Header(default=None)):
    """Download and install `gh` into ~/.fused-render/bin, in the background.

    The X-Fused guard is not a formality here: this writes an executable into
    the user's home, same as `/api/claude/install`.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    try:
        return await run_in_threadpool(github_setup.install_start)
    except github_setup.InstallError as e:
        # A refusal with a sentence the strip can show as-is.
        return _error(str(e), status=409)


@router.get("/api/github/install")
async def api_github_install_status():
    """The current install record. A read — no guard, no spawn."""
    return github_setup.install_status()


# --- signing in through `gh`'s own browser flow -------------------------------
#
# Mirrors claude_health.py's `/api/claude/login` trio almost verbatim: a
# guarded POST to start (it spawns a process AND opens a browser window on the
# user's desktop), an unguarded GET to poll (a read of module state, no
# spawn), and a guarded POST to cancel (it signals a process). See
# fused_render/github_login.py for why this waits on a person rather than
# being a third `/api/github/install` action.
#
# The signup detour needs no endpoint of its own: opening github.com/signup is
# a plain browser tab the template opens directly, and the status poll above
# is what notices the user came back signed in.


@router.post("/api/github/login")
async def api_github_login(x_fused: str | None = Header(default=None)):
    """Start a browser sign-in, and return the opening record.

    The X-Fused guard, like its neighbours: this spawns a process AND opens a
    browser window on the user's desktop, which is not something a blind
    cross-origin POST may do.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import github_login

    try:
        return await run_in_threadpool(github_login.start)
    except github_login.LoginError as e:
        # A refusal with a sentence the strip shows as-is — "a sign-in is
        # already waiting in your browser" is the whole value of the 409,
        # since the window the user needs is already open behind the app.
        return _error(str(e), status=409)


@router.get("/api/github/login")
async def api_github_login_status():
    """The current sign-in record. A read — no guard, no spawn."""
    from fused_render import github_login

    return github_login.status()


@router.post("/api/github/login/cancel")
async def api_github_login_cancel(x_fused: str | None = Header(default=None)):
    """Stop a sign-in the user gave up on. Idempotent, and guarded because it
    signals a process."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render import github_login

    return await run_in_threadpool(github_login.cancel)


# --- creating the repo and pushing -------------------------------------------
#
# Mirrors the install pair immediately above: a guarded POST to start (it
# creates a repository on the user's github.com account AND pushes their
# code to it — nothing a blind cross-origin POST may ever trigger) and an
# unguarded GET to poll (a read of module state, no spawn). See
# `github_setup.py`'s "creating the repo and pushing" section for why this
# is its own record rather than a third `/api/github/install` action.
#
# `root` arrives in the POST body, from the page, exactly like
# `/api/git-upstream`'s POST does — and like that endpoint, it is resolved
# and containment-checked (`github_setup._resolve_repo_root`) before
# anything is done with it, not trusted outright just because the request
# carried the X-Fused header.


@router.post("/api/github/publish")
async def api_github_publish(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Start `gh repo create --source --push` for one repository.

    The X-Fused guard, like its neighbours: this creates a repository on the
    user's own github.com account and pushes to it, which is not something a
    blind cross-origin POST may do.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    root = str(body.get("root") or "")
    name = str(body.get("name") or "")
    visibility = str(body.get("visibility") or "")
    try:
        return await run_in_threadpool(github_setup.publish_start, root, name, visibility)
    except github_setup.PublishError as e:
        # A refusal with a sentence the modal can show as-is — "this
        # repository already has a remote" is the whole value of the 409,
        # same as the install endpoint's own refusal.
        return _error(str(e), status=409)


@router.get("/api/github/publish")
async def api_github_publish_status():
    """The current publish record. A read — no guard, no spawn."""
    return github_setup.publish_status()
