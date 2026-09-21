"""Share an app as a public link: `.fused` export → the user's Fused account.

The sibling of the `.fused` download (routers/appfile.py). Where Export hands
the file to the browser, Share hands it to Fused: the same `export_app_file`
builds the file into a temp dir, then the in-interpreter SDK shim
(`_fused_share_app.py`, spawned like `_fused_canvases_list.py`) uploads it,
pushes a one-node canvas named after the app id, makes it public and answers
with the link. Public only — no team or private mode, by decision.

Identity: the canvas is named after the app's stable id (`app_id.py`), so
sharing the same app twice UPDATES the one canvas (same share token, every
link already sent keeps working) instead of minting a second. An app with no
id — one whose export cannot stamp it because the folder sits in a repo with
a remote (`appfile._has_remote`) — is refused with that reason rather than
shared under an invented name.

The local record (`~/.fused-render/shared_apps.json`, keyed by app id) is
what makes `status` disk-only: the button can say "Shared" without a
control-plane call. It is a cache, not the truth — `lookup` asks Fused when
there is no record (shared from another machine), and `publish` adopts an
existing canvas by name either way.

Sign-in is the `fused login` provider canvases.py owns (`_logged_in`,
`/api/canvases/login`); this module only reads it and answers 409 when it is
missing, the same shape `/api/canvases/create` uses, so the dialog can offer
the same sign-in button.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from fastapi import APIRouter, Body, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse

from fused_render import app_id as app_identity
from fused_render import app_listing, appfile
from fused_render.fusedcli import child_env, cli_error, fused_cli, workbench_env

logger = logging.getLogger(__name__)

router = APIRouter()

# Upload + list + create + push + share: five control-plane round trips plus
# the transfer itself. Apps are capped at 512 MB (appfile.MAX_EXPORT_TOTAL_BYTES)
# but a typical one is a few MB; three minutes is generous headroom.
PUBLISH_TIMEOUT = 180.0
LOOKUP_TIMEOUT = 45.0
REMOVE_TIMEOUT = 90.0

STATE_DIR = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
RECORDS_FILE = "shared_apps.json"

_RECORDS_LOCK = threading.Lock()
# One share operation per app at a time: two publishes racing would upload
# twice and push twice against one canvas.
_APP_LOCKS: dict[str, threading.Lock] = {}
_APP_LOCKS_GUARD = threading.Lock()


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _credentials_file() -> str:
    # The `fused login` store canvases.py reads (~/.fused/credentials, same env
    # override). Duplicated rather than imported: the two are private there,
    # and a router module should not lean on another's internals.
    return os.environ.get("FUSED_RENDER_FUSED_CREDENTIALS") or os.path.expanduser(
        "~/.fused/credentials")


def _logged_in() -> bool:
    # Presence-only, like canvases.py: the SDK refreshes an expired-but-
    # refreshable token itself; the shim's 401 is the authority at action time.
    return os.path.isfile(_credentials_file())


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to stay acyclic.
    if x_fused != "1":
        return _error("missing or invalid X-Fused header", 403)
    return None


# -- records -------------------------------------------------------------------


def _records_path() -> str:
    return os.path.join(STATE_DIR, RECORDS_FILE)


def _read_records() -> dict:
    try:
        with open(_records_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_records(records: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _records_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def get_record(app_id: str) -> dict | None:
    with _RECORDS_LOCK:
        rec = _read_records().get(app_id)
    return rec if isinstance(rec, dict) else None


def put_record(app_id: str, record: dict) -> None:
    with _RECORDS_LOCK:
        records = _read_records()
        records[app_id] = record
        _write_records(records)


def drop_record(app_id: str) -> None:
    with _RECORDS_LOCK:
        records = _read_records()
        if app_id in records:
            del records[app_id]
            _write_records(records)


def _app_lock(path: str) -> threading.Lock:
    """One lock per app FOLDER. Keyed on the path rather than the id because a
    first share has no id until `export_app_file` mints one a moment later —
    two overlapping first shares keyed on `aid or path` would take different
    locks and both push the same canvas."""
    key = os.path.normcase(os.path.abspath(path))
    with _APP_LOCKS_GUARD:
        lock = _APP_LOCKS.get(key)
        if lock is None:
            lock = _APP_LOCKS[key] = threading.Lock()
        return lock


# -- identity ------------------------------------------------------------------


def _resolve_app(path: str) -> tuple[str | None, str | None, str | None]:
    """(entry page, app id, refusal) for the folder at `path`.

    The id is read, never minted here: minting is the export's job
    (`export_app_file`), which `publish` runs anyway. `status` only needs to
    know whether an id exists or CAN exist — the one case it cannot is a
    folder inside a repo with a remote, where stamping would dirty a tracked
    checkout, and that is the refusal reported.
    """
    if not path or not os.path.isabs(path) or not os.path.isdir(path):
        return None, None, "path must be an absolute app folder path"
    try:
        entry = app_listing.app_entry(path)
    except OSError as exc:
        return None, None, f"cannot read {path}: {exc}"
    if entry is None:
        return None, None, (f"{os.path.basename(path)} is not a fused app: no page in it "
                            'carries <meta name="fused-app">')
    aid = app_identity.app_id(entry)
    if aid is None and appfile._has_remote(path):
        return entry, None, (
            "This app has no stable id yet, and fused-render will not write one into a "
            "folder tracked by a repository with a remote. Add "
            '<meta name="fused-app-id" content="…"> to its entry page and commit it, '
            "then share again.")
    return entry, aid, None


def _record_for(aid: str | None) -> dict | None:
    # Keyed on the app id, not the folder: a moved or renamed folder still
    # owns its share. The stored `path` is refreshed by publish/lookup, which
    # write anyway — never here, so a status GET touches nothing.
    return get_record(aid) if aid else None


# -- the SDK shim --------------------------------------------------------------


def _shim_command() -> tuple[list[str] | None, JSONResponse | None]:
    cli = fused_cli()
    if cli is None:
        return None, _error(
            "the fused CLI is not available: the fused package is not importable in "
            "the server's environment and no FUSED_RENDER_FUSED_BIN override is set; "
            'run: pip install "fused-render[fused]"')
    if cli.external:
        # There is no interpreter to run the shim in — an external `fused`
        # binary has no Python we can drive. Say so rather than degrade.
        return None, _error(
            "sharing needs the fused package in fused-render's own environment "
            "(FUSED_RENDER_FUSED_BIN points at an external CLI, which cannot be "
            "driven as Python here)")
    shim = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fused_share_app.py")
    return [sys.executable, shim], None


def _run_shim(request: dict, timeout: float) -> tuple[dict | None, JSONResponse | None]:
    command, err = _shim_command()
    if err is not None:
        return None, err
    cli = fused_cli()
    env = child_env(cli)
    env["FUSED_ENV"] = workbench_env()
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(request),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, _error(f"sharing timed out after {int(timeout)}s", 502)
    except OSError as exc:
        return None, _error(f"could not run the share helper: {exc}", 502)
    if proc.returncode != 0:
        message = cli_error(proc.stderr, "sharing failed")
        low = message.lower()
        if "not signed in" in low or "re-authenticate" in low or "refresh your fused credentials" in low:
            # `code` so the dialog can drop to its sign-in view: the
            # credentials FILE exists (status says logged_in) but the token
            # behind it is dead, and only this call can tell the two apart.
            return None, JSONResponse({"error": message, "code": "not_logged_in"},
                                      status_code=401)
        return None, _error(message, 502)
    try:
        out = json.loads(proc.stdout or "{}")
    except ValueError:
        return None, _error("the share helper printed something that is not JSON", 502)
    if not isinstance(out, dict):
        return None, _error("unexpected share helper payload", 502)
    return out, None


def _not_signed_in() -> JSONResponse:
    return JSONResponse(
        {"error": "not signed in to Fused — sign in first", "code": "not_logged_in"},
        status_code=409)


def _creds_stamp() -> float | None:
    try:
        return os.path.getmtime(_credentials_file())
    except OSError:
        return None


# -- routes --------------------------------------------------------------------


@router.get("/api/share/status")
def api_share_status(path: str = ""):
    """Disk only, and read-only: the app's id, whether Fused is signed in, and
    the local share record if there is one. No control-plane call — this runs
    when a dialog opens, and must answer instantly."""
    _entry, aid, refusal = _resolve_app(path)
    return {
        "app_id": aid,
        "can_share": refusal is None,
        "refusal": refusal,
        "cli_found": fused_cli() is not None,
        "logged_in": _logged_in(),
        "creds_stamp": _creds_stamp(),
        "shared": _record_for(aid),
    }


@router.post("/api/share/publish")
def api_share_publish(
    path: str = Form(default=""),
    preview: UploadFile | None = File(default=None),
    x_fused: str | None = Header(default=None),
):
    """Export the folder to a temp `.fused` and publish it. Publishing the same
    app again is an update: same canvas, same link, new file.

    Multipart for the same reason the export POST is: the caller may hand over
    a screenshot to bake in as `preview.png` (D396) — the shared landing page
    shows that picture, so it matters more here than for a download. An
    over-cap capture is dropped, never fatal, exactly as the export route does.

    A plain `def`, deliberately: the export and the shim together block for
    10-30 s, and an `async def` would hold the event loop — every status poll
    and long-poll in the server — for the whole publish. FastAPI runs a sync
    route on its threadpool, like every canvases.py route. The upload is read
    off `UploadFile.file` (a spooled temp file) for the same reason.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    _entry, aid, refusal = _resolve_app(path)
    if refusal is not None:
        return _error(refusal)
    if not _logged_in():
        return _not_signed_in()
    preview_bytes: bytes | None = None
    if preview is not None:
        preview_bytes = preview.file.read(appfile.MAX_PREVIEW_BYTES + 1)
        if not preview_bytes or len(preview_bytes) > appfile.MAX_PREVIEW_BYTES:
            preview_bytes = None

    lock = _app_lock(path)
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "this app is already being shared", "code": "busy"},
                            status_code=409)
    tmp_dir = tempfile.mkdtemp(prefix="fused-share-")
    try:
        name = os.path.basename(os.path.abspath(path))
        out_path = os.path.join(tmp_dir, appfile.default_file_name(path))
        try:
            manifest = appfile.export_app_file(path, out_path, preview_bytes=preview_bytes)
        except appfile.AppFileError as exc:
            return _error(str(exc))
        aid = appfile.app_id_of(manifest) or aid
        if not aid:
            return _error("the export produced no app id, so there is nothing stable to "
                          "name the shared canvas after")
        previous = get_record(aid) or {}
        out, err = _run_shim(
            {"action": "publish", "share_id": aid, "viewer_token": "UDF_Fused_App_File",
             "name": name, "file": out_path, "previous_remote": previous.get("remote"),
             # Apps stay public-only, by decision — no mode control here
             # (share_file.py's publish is where `mode` is a caller choice).
             "mode": "public"},
            PUBLISH_TIMEOUT)
        if err is not None:
            return err
        now = time.time()
        record = {
            "app_id": aid,
            "path": os.path.abspath(path),
            "name": name,
            "url": out.get("url"),
            "canvas_id": out.get("canvas_id"),
            "canvas_name": out.get("canvas_name"),
            "share_token": out.get("share_token"),
            "slug": out.get("slug"),
            "remote": out.get("remote"),
            "workbench_url": out.get("workbench_url"),
            "exported_at": manifest.get("exported_at") if isinstance(manifest, dict) else None,
            "shared_at": previous.get("shared_at") or now,
            "updated_at": now,
        }
        put_record(aid, record)
        return {"ok": True, "shared": record}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        lock.release()


@router.post("/api/share/lookup")
def api_share_lookup(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Ask Fused whether a canvas for this app already exists — the case with
    no local record (shared from another machine). A hit is adopted into the
    records so the next `status` is disk-only again."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    _entry, aid, refusal = _resolve_app(path)
    if refusal is not None:
        return _error(refusal)
    if aid is None:
        return {"found": False, "app_id": None}
    if not _logged_in():
        return _not_signed_in()
    name = os.path.basename(os.path.abspath(path))
    out, err = _run_shim({"action": "lookup", "share_id": aid, "name": name}, LOOKUP_TIMEOUT)
    if err is not None:
        return err
    if not out.get("found"):
        return {"found": False, "app_id": aid}
    now = time.time()
    existing = get_record(aid) or {}
    record = {
        **existing,
        "app_id": aid,
        "path": os.path.abspath(path),
        "name": name,
        "url": out.get("url") or existing.get("url"),
        "canvas_id": out.get("canvas_id"),
        "canvas_name": out.get("canvas_name"),
        "share_token": out.get("share_token"),
        "slug": out.get("slug") or existing.get("slug"),
        "workbench_url": out.get("workbench_url") or existing.get("workbench_url"),
        "shared_at": existing.get("shared_at") or now,
        "updated_at": existing.get("updated_at") or now,
        # Adopted, not published from here: the canvas may carry an older
        # file, so the dialog offers Update rather than claiming it is current.
        "adopted": True,
    }
    put_record(aid, record)
    return {"found": True, "app_id": aid, "shared": record}


@router.post("/api/share/remove")
def api_share_remove(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    """Delete the canvas and the uploaded copies. Every link handed out stops
    working — the dialog confirms before calling this."""
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    path = body.get("path") if isinstance(body.get("path"), str) else ""
    _entry, aid, refusal = _resolve_app(path)
    if refusal is not None:
        return _error(refusal)
    if aid is None:
        return _error("this app has never been shared")
    if not _logged_in():
        return _not_signed_in()
    lock = _app_lock(path)
    if not lock.acquire(blocking=False):
        return JSONResponse({"error": "this app is already being shared", "code": "busy"},
                            status_code=409)
    try:
        rec = get_record(aid) or {}
        out, err = _run_shim(
            {"action": "remove", "share_id": aid, "canvas_id": rec.get("canvas_id")},
            REMOVE_TIMEOUT)
        if err is not None:
            return err
        drop_record(aid)
        return {"ok": True, "deleted_canvas": bool(out.get("deleted_canvas"))}
    finally:
        lock.release()
