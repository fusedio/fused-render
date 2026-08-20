"""Routes behind the ``.fused`` single-file app export/open (SPEC §43, D385-D389).

Export is a GET download (the card menu navigates to it, so the browser's own
download UI handles the file) of a zip built into a per-request temp dir and
deleted after the response — read-only against the app folder, nothing
persisted server-side, same unguarded-GET posture as the template-pack export.

Open has no user-facing route at all (D390, which removed D389's /openfused
redirect hop): a ``.fused`` renders at its own ``/explorer/view|embed/<path>``
URL through the ``fusedapp`` preview template (``templates/fusedapp``), and
that template calls the internal X-Fused-guarded ``POST /api/appfile/open``
here — extract-or-reuse (hardened, content-addressed, read-only; see
``appfile.open_app_file``) — then iframes the entry page's embed URL it
answers with. No gate anywhere (D389's owner call stands).

Clone (D397) is the way OUT of the read-only artifact: GET reports where the
file would land in the workspace and whether it is already there, POST does the
copy. The pair backs one button in the preview header, which flips between
"Clone" and "Go to local version" on the GET's ``cloned``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, Header, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from fused_render import appfile
from fused_render._view_url_codec import embed_url_path

router = APIRouter()


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


@router.post("/api/appfile/export")
async def api_appfile_export_with_preview(
    path: str = Form(default=""),
    preview: UploadFile | None = File(default=None),
    x_fused: str | None = Header(default=None),
):
    """The card's export path (D396): same download as the GET, plus an
    optional caller-captured screenshot that becomes the payload's
    ``preview.png`` when the folder has no authored one. A POST because it
    carries a body; X-Fused-guarded because — unlike the GET — its caller is
    always our own fetch, never bare browser navigation."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute app folder path")
    preview_bytes: bytes | None = None
    if preview is not None:
        preview_bytes = await preview.read(appfile.MAX_PREVIEW_BYTES + 1)
        if not preview_bytes:
            preview_bytes = None
    tmp_dir = tempfile.mkdtemp(prefix="fused-appfile-export-")
    file_name = appfile.default_file_name(path)
    out_path = os.path.join(tmp_dir, file_name)
    try:
        appfile.export_app_file(path, out_path, preview_bytes=preview_bytes)
    except appfile.AppFileError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return _error(str(exc))
    return FileResponse(
        out_path,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(file_name)
        },
        background=BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True),
    )


@router.get("/api/appfile/preview")
def api_appfile_preview(path: str = ""):
    """The ``preview.png`` inside the ``.fused`` at ``path``, as bytes — the
    exported card's thumbnail (D396). Read-only single-member zip read, no
    extraction (a grid of thumbnails must never populate the extract cache).
    404 when the file ships without one, so the card's ordinary onError
    fallback shows the empty thumb."""
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute .fused file path")
    try:
        raw = appfile.read_preview(path)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    if raw is None:
        return _error("this app file has no preview image", status=404)
    return Response(raw, media_type="image/png",
                    headers={"Cache-Control": "no-cache"})


@router.get("/api/appfile/clone")
def api_appfile_clone_state(path: str = ""):
    """Where the ``.fused`` at ``path`` would clone to, and whether it already
    has: ``{name, slug, path, cloned}`` (D397). The preview header's Clone
    button reads this on mount to pick its label — "Clone" or "Go to local
    version" — so it must stay a cheap read-only probe: one bounded manifest
    read and one isdir, no extraction. Unguarded like the preview GET; it
    reports a path and touches nothing."""
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute .fused file path")
    try:
        return appfile.clone_target(path)
    except appfile.AppFileError as exc:
        return _error(str(exc))


@router.post("/api/appfile/clone")
def api_appfile_clone(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Copy the ``.fused`` at ``file`` into ``<workspace>/local/<slug>`` as an
    editable app folder and answer where it landed (D397). ``cloned: true``
    means the folder was ALREADY there and nothing was copied — the caller
    navigates to it either way, so a second Clone is a no-op that opens the
    existing copy rather than an error or a second folder.

    X-Fused-guarded, unlike the GET beside it: this one writes."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    file = str(body.get("file") or "")
    if not file or not os.path.isabs(file):
        return _error("file must be an absolute .fused file path")
    try:
        return appfile.clone_app_file(file)
    except appfile.AppFileError as exc:
        return _error(str(exc))


@router.post("/api/appfile/open")
def api_appfile_open(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Extract the ``.fused`` at ``file`` (or re-use its cached extract) and
    answer the entry page's embed URL. The one caller is the ``fusedapp``
    preview template — there is no user-facing open route (D390)."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    file = str(body.get("file") or "")
    if not file or not os.path.isabs(file):
        return _error("file must be an absolute .fused file path")
    # Preview contract (D396): a card thumbnail / listing peek may RE-USE an
    # existing extract to live-render the app, but must never extract fresh
    # (reuse_only) and must never count as an open (no recency below).
    preview = body.get("preview") is True
    try:
        result = appfile.open_app_file(file, reuse_only=preview)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    # The open IS the recency signal for the .fused file itself (D396): this
    # is the one moment the SOURCE path is known (rendering the extracted
    # entry only knows the cache dir, which registered_apps now refuses).
    # Best-effort — a failed write must not fail the open. Previews never
    # record: a card thumbnail counting as an open would reshuffle the very
    # recency order the grid is sorted by (the D301 rule, held server-side
    # because the flag is what the template's preview branch sends).
    if not preview:
        try:
            from fused_render import exported_apps

            exported_apps.record_open(file)
        except Exception:  # noqa: BLE001 - recency is telemetry, not the answer
            pass
    return {**result, "view": embed_url_path(result["entry"])}
