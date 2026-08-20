"""Routes behind the ``.fused`` single-file app export/open (SPEC §43, D384-D388).

Export is a GET download (the card menu navigates to it, so the browser's own
download UI handles the file) of a zip built into a per-request temp dir and
deleted after the response — read-only against the app folder, nothing
persisted server-side, same unguarded-GET posture as the template-pack export.

Open is ONE hop with no gate (D388 removed D385's confirm page, owner call):
``GET /openfused?file=`` — what the shared view-URL codec routes a
double-clicked or explorer-clicked ``.fused`` to — extracts the payload
(hardened, content-addressed, read-only; see ``appfile.open_app_file``) and
302-redirects straight to the entry page's chrome-free embed URL. The GET
mutates only the app's own cache dir, idempotently — the same posture as
GET /render recording an open.
"""

from __future__ import annotations

import html
import os
import shutil
import tempfile
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.background import BackgroundTask

from fused_render import appfile
from fused_render._view_url_codec import embed_url_path

router = APIRouter()


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.get("/api/appfile/export")
def api_appfile_export(path: str = ""):
    """Build and download ``<app name>.fused`` for the app folder at ``path``.

    A GET, deliberately: the trigger is browser navigation from the card menu,
    which cannot set headers, and the operation is read-only against the
    folder (the zip lands in a temp dir removed once the response is sent).
    """
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute app folder path")
    tmp_dir = tempfile.mkdtemp(prefix="fused-appfile-export-")
    file_name = appfile.default_file_name(path)
    out_path = os.path.join(tmp_dir, file_name)
    try:
        appfile.export_app_file(path, out_path)
    except appfile.AppFileError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _error(str(exc))
    return FileResponse(
        out_path,
        media_type="application/octet-stream",
        # RFC 5987 filename*: the app folder's name may be non-ASCII.
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(file_name)
        },
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


@router.get("/openfused")
def openfused(file: str = ""):
    """Open the ``.fused`` file at ``file``: extract (or re-use the extract)
    and redirect to the entry page's embed URL. No confirm gate (D388).

    Errors render as a minimal same-tab HTML page rather than JSON — this URL
    is reached by OS double-click navigation, where a JSON body reads as a
    broken download."""
    if not file or not os.path.isabs(file):
        return _openfused_error("missing or relative ?file= parameter")
    try:
        result = appfile.open_app_file(file)
    except appfile.AppFileError as exc:
        return _openfused_error(str(exc))
    return RedirectResponse(embed_url_path(result["entry"]), status_code=302)


def _openfused_error(message: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'>"
        "<body style=\"font:15px/1.5 -apple-system,sans-serif;padding:40px\">"
        "<h1 style='font-size:18px'>Could not open app</h1>"
        f"<pre style='white-space:pre-wrap'>{html.escape(message)}</pre></body>",
        status_code=400,
    )
