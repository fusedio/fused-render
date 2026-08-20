"""Routes behind the ``.fused`` single-file app export/open (SPEC §43, D384-D386).

Export is a GET download (the card menu navigates to it, so the browser's own
download UI handles the file) of a zip built into a per-request temp dir and
deleted after the response — read-only against the app folder, nothing
persisted server-side, same unguarded-GET posture as the template-pack export.

Open is split across the same trust boundary as the deep-link clone (D110):
``GET /openfused`` serves the static confirm page (no I/O), ``GET
/api/appfile/info`` is the page's read-only preview (manifest only, nothing
extracted), and only the X-Fused-guarded ``POST /api/appfile/open`` extracts
and answers with the embed URL to land on.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, Body, Header
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from fused_render import appfile
from fused_render._view_url_codec import embed_url_path

router = APIRouter()

_OPENFUSED_PAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static", "openfused.html"
)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated like deeplink.py's:
    # a router module must not import the app factory that includes it.
    if x_fused != "1":
        return _error("missing or invalid X-Fused header", status=403)
    return None


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
def openfused_page(file: str = ""):
    # The confirm page is self-contained (no shell, no external assets): it
    # reads ?file= client-side, previews via GET /api/appfile/info, and only
    # its explicit Open button fires the guarded POST. Serving it does no I/O.
    return FileResponse(_OPENFUSED_PAGE)


@router.get("/api/appfile/info")
def api_appfile_info(file: str = ""):
    """Read-only preview for the confirm page: the manifest, the file size,
    and where the extract would land. Nothing is extracted."""
    if not file or not os.path.isabs(file):
        return _error("file must be an absolute .fused file path")
    try:
        manifest = appfile.read_manifest(file)
        size = os.path.getsize(file)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    except OSError as exc:
        return _error(f"cannot read {file}: {exc}")
    return {
        "name": manifest.get("name"),
        "entry": manifest.get("entry"),
        "size": size,
        "dest_root": appfile.appfiles_root(),
    }


@router.post("/api/appfile/open")
def api_appfile_open(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    file = str(body.get("file") or "")
    if not file or not os.path.isabs(file):
        return _error("file must be an absolute .fused file path")
    try:
        result = appfile.open_app_file(file)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    # No explicit hub registration here: rendering the marker-carrying entry
    # records the open (D301), which for a folder outside the workspace IS the
    # registration (registered_apps.record_open) — deliberate, D386.
    return {**result, "view": embed_url_path(result["entry"])}
