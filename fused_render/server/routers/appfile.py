"""Routes behind the ``.fused`` single-file app export/install (SPEC §43).

Export is a GET download (the card menu navigates to it, so the browser's own
download UI handles the file) of a container built into a per-request temp
dir and deleted after the response — read-only against the app folder,
nothing persisted server-side, same unguarded-GET posture as the
template-pack export.

Opening has no user-facing route (D390): a ``.fused`` renders at its own
``/explorer/view|embed/<path>`` URL through the ``fusedapp`` template, which
is the INSTALL PAGE (AF-14, owner 2026-10-13): nothing in a file runs until
it is installed. The page reads ``GET /api/appfile/install`` (what installing
would do: new / update / diverged / same / downgrade, the README, the member
list — read-only, never an extraction), ``/api/appfile/icon`` and
``/api/appfile/preview`` for its pictures, and its one button POSTs the
X-Fused ``/api/appfile/install``, which unpacks the app into
``<workspace>/app/<slug>`` — the one working copy — or updates the copy
already there (``appfile.install_app_file``). A state that loses the user's
work (``needs_force``) answers 409 until the client sends ``force``.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import tempfile
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, Header, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask

from fused_render import appfetch, appfile, jobs
from fused_render._view_url_codec import canonical_fs_path, embed_url_path, view_url_path
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
    file_name = appfile.default_file_name(path)

    def _build() -> tuple[str, str]:
        # mkdtemp + the zip of the whole app folder + the error-path rmtree are
        # all blocking disk work: off the event loop.
        tmp_dir = tempfile.mkdtemp(prefix="fused-appfile-export-")
        out_path = os.path.join(tmp_dir, file_name)
        try:
            appfile.export_app_file(path, out_path, preview_bytes=preview_bytes)
        except BaseException:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise
        return tmp_dir, out_path

    try:
        tmp_dir, out_path = await asyncio.to_thread(_build)
    except appfile.AppFileError as exc:
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
    different, sandboxed thing) — `~/Downloads` comes out of
    `os.path.expanduser("~")`.
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
    real_path = canonical_fs_path(out_path)
    note_index_mutation(real_path)
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


@router.get("/api/appfile/icon")
def api_appfile_icon(path: str = ""):
    """The payload's ``icon.svg``/``icon.png`` as bytes — the install page's
    mark. Bounded single-member read, no extraction; 404 without one."""
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute .fused file path")
    try:
        icon = appfile.read_icon(path)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    if icon is None:
        return _error("this app file has no icon", status=404)
    mime, raw = icon
    return Response(raw, media_type=mime, headers={"Cache-Control": "no-cache"})


@router.get("/api/appfile/install")
def api_appfile_install_state(path: str = "", light: str = ""):
    """What installing the ``.fused`` at ``path`` would do (AF-14): the
    install page's data — ``state`` (new / update / diverged / same /
    downgrade / unknown / clash), where the copy is or would be, the file's
    README and member list. Read-only: one bounded manifest read, the
    workspace walk for the app id, and the hash of the installed copy's
    stamped files — skipped under ``light=1`` (a card thumbnail, D396).
    Unguarded like the preview GET; it reports and touches nothing."""
    if not path or not os.path.isabs(path):
        return _error("path must be an absolute .fused file path")
    try:
        return appfile.install_target(path, light=light == "1")
    except appfile.AppFileError as exc:
        return _error(str(exc))


@router.post("/api/appfile/install")
def api_appfile_install(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Install the ``.fused`` at ``file`` into ``<workspace>/app/<slug>``, or
    update the copy already there (``appfile.install_app_file``). A state
    that would lose the user's work — the copy diverged, the file is older
    or the same version, no install record — answers 409 with the fresh
    probe under ``target`` until the client sends ``force: true`` (the
    page's second click); a slug held by a different app answers 400.
    Answers the probe shape after the write plus ``action``, ``entry`` and
    the entry page's view and embed URLs for the page to open.

    X-Fused-guarded, unlike the GET beside it: this one writes."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    file = str(body.get("file") or "")
    if not file or not os.path.isabs(file):
        return _error("file must be an absolute .fused file path")
    force = body.get("force") is True
    try:
        target = appfile.install_target(file)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    if target["needs_force"] and not force:
        return JSONResponse({"error": f"install state {target['state']!r} needs confirmation",
                             "target": target}, status_code=409)
    try:
        result = appfile.install_app_file(file, force=force)
    except appfile.InstallNeedsConfirm as exc:
        # A warn state that appeared between the probe above and the write
        # (a copy claimed the folder meanwhile): the same 409 as the probe's.
        return JSONResponse({"error": str(exc), "target": exc.target}, status_code=409)
    except appfile.AppFileError as exc:
        return _error(str(exc))
    # The paged hub's snapshot is at most 20 s behind the disk; the one folder
    # it must never be behind on is the one just installed (the create path
    # and #1576's uninstall reset it the same way).
    from fused_render.server.routers import apps as apps_router

    apps_router._reset_snapshot()
    entry = result["entry"]
    return {**result, "entry_view": view_url_path(entry), "entry_embed": embed_url_path(entry)}


@router.post("/api/appfile/fetch")
def api_appfile_fetch(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Download the ``.fused`` at the http(s) ``url`` (DL-8), or build one
    from the single app in the public GitHub repo at ``git`` (DL-9), into the
    managed downloads dir and answer ``{file}`` — the saved absolute path,
    which the caller then opens like any other ``.fused``: on its install
    page. The one caller is the shell's ``FetchAppFileBoot``, fed by a
    ``fused-render://open?url=`` / ``open?git=`` link.

    X-Fused-guarded: it writes (a file into the downloads dir, nothing
    else). There is no confirm before the DOWNLOAD (the deep-link click is
    the gesture); the install page is the confirm before anything runs. The
    recorded cost is that a web page which can navigate the browser to this
    origin with ``?_fetch_appfile=`` / ``?_fetch_appgit=`` gets a remote
    ``.fused`` downloaded without a prompt."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    repo = str(body.get("git") or "").strip()
    url = str(body.get("url") or "").strip()
    try:
        if repo:
            file = appfetch.download_github_app(repo)
        elif appfetch.is_url(url):
            file = appfetch.download_app_file(url)
        else:
            return _error("url must be an http:// or https:// link to a .fused file")
    except appfetch.FetchError as exc:
        return _error(str(exc))
    return JSONResponse({"file": file.replace(os.sep, "/")})
