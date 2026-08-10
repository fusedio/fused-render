"""POST /api/claude-config/{module} + GET /api/claude-config/status — the
backend for the Preferences page's native "Claude config" tab.

ONE dispatch endpoint rather than ~40 hand-written routes, because the thing
being exposed is already a dispatch table: each module in
`fused_render/claude_config/` owns one feature and answers a `main(action=..., …)`
call. Those signatures were the app's contract when the feature ran as an html+py
app through `runPython` (one subprocess per click); keeping them means the port
is a transport change and nothing else, and the modules stay readable side by
side with the canonical example app they came from.

The allowlist is EXPLICIT — a dict of the eleven callables, not
`importlib.import_module(f"fused_render.claude_config.{module}")`. The module
name arrives in the URL from a browser, and dynamic import over an
attacker-influenced name is how a dispatch endpoint turns into "call `main` on
any importable module in the process". An unknown name is a 404, which is also
the honest status: there is no such resource.

The body is forwarded as `**kwargs`, so the HTTP shape is
`{"action": "...", …}` — exactly the params the module documents. Kwargs are
bound against the real signature BEFORE the call, so a caller's mistake
("unexpected keyword argument") is a 400 while a TypeError raised from inside
the feature's own logic stays a 500; conflating the two would report our bugs as
the client's.

Everything runs in a threadpool: every module blocks somewhere real (git
subprocesses, `claude` CLI invocations, `mdfind`, and the catalog refresh's HTTP
fetch), and blocking the event loop would stall every other request in the app,
including the ones this page fires in parallel.

Mutating POSTs carry the same D3 X-Fused header guard as /api/fs/*: `claude_md`
deletes files, `preferences`/`plugins`/`marketplaces` rewrite settings.json, and
`profiles` checks out git branches — all of which a blind cross-origin POST
could otherwise fire. The frontend's postJson helper already sends the header.
"""
import inspect
import logging
import os

from fastapi import APIRouter, Body, Header
from fastapi.concurrency import run_in_threadpool

from fused_render.claude_config import (
    claude_md,
    git_ops,
    lib,
    marketplaces,
    mcp,
    memory,
    plugins,
    preferences,
    profiles,
    refresh_catalog,
    skills,
    statusline,
)
from fused_render.server.common import _error, _require_fused

logger = logging.getLogger(__name__)

router = APIRouter()

# name in the URL -> the feature module's dispatch entry point. Adding a feature
# = one row here plus the module; there is no other registration.
MODULES = {
    "claude_md": claude_md.main,
    "git_ops": git_ops.main,
    "marketplaces": marketplaces.main,
    "mcp": mcp.main,
    "memory": memory.main,
    "plugins": plugins.main,
    "preferences": preferences.main,
    "profiles": profiles.main,
    "refresh_catalog": refresh_catalog.main,
    "skills": skills.main,
    "statusline": statusline.main,
}


@router.get("/api/claude-config/status")
def api_claude_config_status():
    """Whether this machine has a Claude Code config dir at all.

    Replaces the old `claude_config_mount_ready` flag on /api/config: the tab
    used to be gated on a builtin :archive: mount being attached, which said
    nothing about whether the user had Claude Code installed and everything
    about whether rclone had finished. The honest gate is "is there config to
    edit", and it is one isdir() — so it stays its own cheap endpoint rather
    than growing /api/config, which every page load reads.

    Read through `lib` rather than imported by value: the constant is resolved
    from the env at import time, so a by-value copy here would be a second thing
    to keep in sync (and a second thing to patch in tests).
    """
    return {"available": os.path.isdir(lib.CLAUDE_DIR)}


@router.post("/api/claude-config/{module}")
async def api_claude_config(
    module: str,
    body: dict = Body(default={}),
    x_fused: str | None = Header(default=None),
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    main = MODULES.get(module)
    if main is None:
        return _error(f"unknown claude-config module: {module}", status=404)
    try:
        inspect.signature(main).bind(**body)
    except TypeError as e:
        return _error(f"{module}: {e}")
    try:
        return await run_in_threadpool(main, **body)
    except Exception as e:  # noqa: BLE001
        # The message only reaches the browser — a traceback in a JSON body tells
        # a page nothing it can act on and leaks absolute paths into whatever the
        # browser logs. It is logged in full here instead, because catching the
        # exception is what takes it away from the server's default handler.
        logger.exception("claude-config %s(%s) failed", module, ", ".join(body))
        return _error(f"{module}: {type(e).__name__}: {e}", status=500)
