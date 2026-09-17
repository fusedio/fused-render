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
import secrets
import shutil
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, Header, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from fused_render import appfile, jobs
from fused_render._view_url_codec import canonical_fs_path, embed_url_path
from fused_render.server.index_touch import note_index_mutation

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
    which cannot set headers. The file lands in a temp dir removed once the
    response is sent. The one write to the folder itself is the app's FIRST
    export stamping ``<meta name="fused-app-id">`` into its entry page
    (`app_id.py`) — an identity the app then keeps for life, so the same
    folder exported again produces a file with the same ``app_id``.
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
    always our own fetch, never bare browser navigation.

    An OVER-CAP capture is DROPPED here rather than raised: the browser-side
    contract is that every capture failure exports plain (appShot.ts), and a
    screenshot that came out too big is a capture failure — losing the
    thumbnail is the cost, losing the export is not. `export_app_file` keeps
    its strict answer for a caller that passes a preview deliberately; this
    route, which knows its bytes came off a best-effort screen grab, screens
    the size first. Read one byte past the cap so "exactly at the cap" ships.
    A non-PNG still raises: `canvas.toBlob(…, "image/png")` cannot produce one,
    so those bytes are a client bug worth reporting, not a failed grab.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute app folder path")
    preview_bytes: bytes | None = None
    if preview is not None:
        preview_bytes = await preview.read(appfile.MAX_PREVIEW_BYTES + 1)
        if not preview_bytes or len(preview_bytes) > appfile.MAX_PREVIEW_BYTES:
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


def _export_destination_dir() -> str:
    """The platform Downloads folder, created if this is its first use.

    No existing helper in the codebase resolves a per-platform user
    directory outside the app's own home (`storage.home_dir()` is a
    different, sandboxed thing) — `~/Downloads` and
    `%USERPROFILE%\\Downloads` both come out of `os.path.expanduser("~")`,
    which resolves correctly on both POSIX and Windows.
    """
    d = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(d, exist_ok=True)
    return d


def _export_file_name(path: str, name: str) -> str:
    """The ``.fused`` file's own name: the caller's display name when one is
    given, the app folder's own basename otherwise.

    A caller building a versioned export (`AppPage.tsx`/`EntryActionsMenu.tsx`
    compute `${name}-${versionLabel}`) does so specifically so a v7 snapshot
    export never lands beside a live export under the same ambiguous name —
    `os.path.basename` strips any directory component a request body could
    otherwise smuggle in, so this can only ever choose a bare filename inside
    `dest_dir`, never escape it.
    """
    base = os.path.basename((name or "").strip())
    if not base:
        return appfile.default_file_name(path)
    return base if base.lower().endswith(".fused") else base + ".fused"


def _unique_export_path(dest_dir: str, file_name: str) -> str:
    """The first name in `dest_dir` that does not already exist — `App.fused`,
    then `App (2).fused`, `App (3).fused`, ... `export_app_file` itself
    refuses to overwrite, so a repeat export must be handed a free name
    rather than relying on that refusal, which would just fail the second
    export outright instead of producing a sibling copy."""
    stem, ext = os.path.splitext(file_name)
    candidate = os.path.join(dest_dir, file_name)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(dest_dir, f"{stem} ({n}){ext}")
        n += 1
    return candidate


@router.post("/api/appfile/export/save")
async def api_appfile_export_to_disk(
    path: str = Form(default=""),
    name: str = Form(default=""),
    preview: UploadFile | None = File(default=None),
    x_fused: str | None = Header(default=None),
):
    """Write ``<name>.fused`` straight to the platform Downloads folder and
    report its real path, instead of handing the browser a blob it saves
    wherever the user's download settings land it. ``name`` is the caller's
    own display name for the file (a version export's own
    ``${name}-${versionLabel}``, see `_export_file_name`); it falls back to
    the app folder's own basename when blank.

    This is what makes the export immediately searchable: writing through
    the browser leaves the real destination unknown to the server, so the
    exported file sits outside the index until the next scan happens to
    cover it (SPEC's ~110s-unsearchable bug). Writing here means the path is
    known the instant the file exists, so `note_index_mutation` can queue the
    exported file itself for a rescan synchronously, on the same request — no
    freshness gate involved at all. It is handed the FILE, not `dest_dir`:
    `note_index_mutation` scans the PARENT of whatever path it is given
    (`index_touch._folder_of`), so passing the folder itself would queue a
    scan of the folder's own parent instead of Downloads.

    Same optional-preview shape as `api_appfile_export_with_preview`: an
    over-cap or non-PNG capture is dropped rather than raised, since it comes
    off a best-effort screen grab and a failed grab must cost the thumbnail,
    not the export.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute app folder path")
    preview_bytes: bytes | None = None
    if preview is not None:
        preview_bytes = await preview.read(appfile.MAX_PREVIEW_BYTES + 1)
        if not preview_bytes or len(preview_bytes) > appfile.MAX_PREVIEW_BYTES:
            preview_bytes = None
    dest_dir = _export_destination_dir()
    file_name = _export_file_name(path, name)
    out_path = _unique_export_path(dest_dir, file_name)
    try:
        appfile.export_app_file(path, out_path, preview_bytes=preview_bytes)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    note_index_mutation(out_path)
    real_path = canonical_fs_path(out_path)
    jobs.upsert(
        {
            "id": f"{jobs.SERVER_ID_PREFIX}appfile-export:{secrets.token_hex(4)}",
            "title": f"Exported {os.path.basename(out_path)}",
            "state": "done",
            "kind": "task",
            # The client raises its own two-action notification ("Reveal
            # folder" / "Open file") on this same export, so this row must
            # not ALSO pop a card — `popupJobs` already drops a `done` job
            # whose stored tier is `silent`.
            "tier": "silent",
        },
        page=real_path,
        origin="Export",
        server=True,
    )
    return JSONResponse({"path": real_path})


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
