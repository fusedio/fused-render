"""`GET /api/apps/python` — what Python a caller can run beside an app's page.

The discovery half of SPEC §49 (bots calling an app's `.py` directly). The
execution half is NOT here: a caller runs a listed file through the page's own
`POST /api/run` (`{py, html, params}`, routers/run.py), so bot and page share
one runner, one envelope and one 60 s bound by construction. This route only
answers the question the page never had to ask — which files exist and what
`main()` takes — and answers it from the files' text alone (`pyinspect`), never
by importing them.

Read-only, but guarded with `X-Fused` all the same: the listing names every
file in a folder the caller already knows the path of, so the guard costs a
same-origin page nothing and keeps a foreign page from fishing for folder
contents through a blind GET's side channels.
"""
from __future__ import annotations

import asyncio
import os
import sys

from fastapi import APIRouter, Header

from fused_render import background_apps, pyinspect
from fused_render.executor import DEFAULT_TIMEOUT
from fused_render.server import engine_host
from fused_render.server.common import _error, _require_fused

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

router = APIRouter()


def _folder_for(html, dir_) -> str | None:
    """The app folder: `dir` as given, else the folder of `html` (the page's
    path, the same key every other `/api/apps/*` route takes). realpath'd so
    the `running` flag below keys off the same folder identity
    `background_apps.engine_id_for` uses (D509)."""
    if isinstance(dir_, str) and dir_:
        return os.path.realpath(os.path.abspath(dir_))
    if isinstance(html, str) and html:
        return os.path.realpath(os.path.dirname(os.path.abspath(html)))
    return None


def _entry_page(folder: str) -> str | None:
    """The folder's single entry page, for callers that found the folder by
    `dir` and need an `html` to hand `/api/run`."""
    p = os.path.join(folder, "index.html")
    return p if os.path.isfile(p) else None


def _curated_tools(folder: str) -> list[dict]:
    """The `[[tool]]` tables of `<folder>/mcp.toml`, trimmed to what a caller
    needs to recognise a file it is about to call as one the author already
    curated (openfused `spec/serve/app-mcp.md` §2). Invalid or absent → []."""
    path = os.path.join(folder, "mcp.toml")
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, ValueError):
        return []
    out = []
    for entry in raw.get("tool", []) or []:
        if not isinstance(entry, dict):
            continue
        out.append({
            "name": str(entry.get("name", "")),
            "description": str(entry.get("description", "")),
            "file": str(entry.get("file", "")),
            "entrypoint": str(entry.get("entrypoint", "main")),
            "curated": True,
        })
    return out


def _background(folder: str) -> dict | None:
    """`{kind: "main"|"daemon", file, running}` for a folder that declares a
    background app (SPEC §46), else None. Reported so a caller knows the
    folder's real work lives in a resident process it cannot reach through
    `/api/run`; calling that process is out of scope here (§49)."""
    manifest = background_apps.load_manifest(folder)
    if manifest is None:
        return None
    child = engine_host.current(background_apps.engine_id_for(folder))
    return {
        "kind": "main" if manifest.main else "daemon",
        "file": os.path.basename(manifest.main or manifest.daemon),
        "running": child is not None and engine_host._alive(child),
    }


@router.get("/api/apps/python")
async def api_apps_python(html: str = "", dir: str = "",
                          x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    folder = _folder_for(html, dir)
    if folder is None:
        return _error("query must include 'html' (the app's page) or 'dir' (the app folder)")
    if not os.path.isdir(folder):
        return _error("no such folder: %s" % folder, status=404)

    def _build():
        return {
            "app": folder,
            "html": html if html and os.path.isfile(html) else _entry_page(folder),
            "entrypoint": pyinspect.ENTRYPOINT,
            "timeout_s": int(DEFAULT_TIMEOUT),
            "files": pyinspect.folder_report(folder),
            "tools": _curated_tools(folder),
            "background": _background(folder),
        }

    return await asyncio.to_thread(_build)
