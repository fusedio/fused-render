"""Share an app as a public link: `.fused` export → the user's Fused account.

The sibling of the `.fused` download (routers/appfile.py). Where Export hands
the file to the browser, Share hands it to Fused: the same `export_app_file`
builds the file into a temp dir, then the in-interpreter SDK shim
(`_fused_share_app.py`, spawned like `_fused_canvases_list.py`) uploads it,
pushes a one-node canvas named after the app id, makes it public and answers
with the link. No visibility control here: public is the one mode asked for.
On a deployment that forbids public canvases the shim falls back on its own
to a team-scoped canvas with a 30-minute session token on the link (the
`temporary` mode share_file.py offers by choice); the record then carries
`mode`, `session_expires` and `public_refused` so the dialog can say the link
expires and why.

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
import re
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
from fused_render.fusedcli import (child_env, cli_error, fused_cli, set_fused_env,
                                   workbench_env)

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
    # The `fused login` store the DEFAULT env reads — the env sharing uploads
    # target (`set_fused_env(..., workbench_env())`). canvases owns the rule:
    # under a custom default it is the settings file's `[auth]` store, not
    # ~/.fused/credentials (D1412). Imported lazily: canvases is the heavier
    # router and this module is imported by share_file at startup.
    from fused_render.canvases import credentials_file

    return credentials_file()


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
    record = get_record(aid) if aid else None
    return with_expiry(record) if record else None


def _parse_expiry(value) -> float | None:
    """`session_expires` as the shim hands it back — an ISO 8601 string in
    the common case, but treated generically since the SDK's own type is not
    pinned in the spec (artifact-sharing.md §3: `.expires_at`)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            import datetime

            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                # A naive ISO string is what the SDK returns in practice — no
                # `Z`, no offset. `.timestamp()` on a naive datetime reads it
                # in the SERVER'S local zone; the SDK means UTC (finding 7).
                # On UTC+2 a fresh 30-minute token read as ~90 minutes
                # expired, so /status flagged a working link "Expired".
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return None
    return None


def is_expired(record: dict) -> bool:
    """A `temporary` share's session token past its `session_expires` — the
    sheet uses this to offer a fresh link without a round trip. Lives here
    (not share_file.py) because share_file imports this module, and an app
    record can be `temporary` too once the deployment refuses public scope."""
    if record.get("mode") != "temporary":
        return False
    expiry = _parse_expiry(record.get("session_expires"))
    if expiry is None:
        return False
    return expiry <= time.time()


def with_expiry(record: dict) -> dict:
    return {**record, "expired": is_expired(record)}


def _adopted_scope(out: dict, existing: dict) -> dict:
    """`url`/`mode` for a record adopted by lookup, from the scope the canvas
    HAS on the account (the shim reports it) — not from the stored record.
    A team canvas is the only kind a refusing deployment ever holds: keep the
    stored session URL when the record is already `temporary` and the token
    is unchanged, else there is no link until Update mints one. A canvas
    flipped public elsewhere drops a stale session URL for the bare one."""
    scope = out.get("access_scope")
    if scope == "team":
        same_token = out.get("share_token") and out.get("share_token") == existing.get("share_token")
        keep = existing.get("mode") == "temporary" and same_token
        return {"mode": "temporary", "url": existing.get("url") if keep else None,
                "public_refused": bool(existing.get("public_refused"))}
    return {"mode": "public", "url": out.get("url") or existing.get("url"),
            "session_token": None, "session_expires": None, "public_refused": False}


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
    set_fused_env(env, workbench_env())
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
    github_copy = refusal is None and _github_copy_repo(path) is not None
    if github_copy:
        refusal = GITHUB_COPY_REFUSAL
    return {
        "app_id": aid,
        "can_share": refusal is None,
        "refusal": refusal,
        # Which refusal, for a sheet that offers the GitHub link instead.
        "refusal_code": "github_copy" if github_copy else None,
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
    if _github_copy_repo(path) is not None:
        # Someone else's code never uploads to Fused (contribution loop):
        # refused here, not only hidden in the sheet.
        return JSONResponse({"error": GITHUB_COPY_REFUSAL, "code": "github_copy"},
                            status_code=409)
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
        # Always asked for as public — no mode control here (share_file.py's
        # publish is where `mode` is a caller choice). The shim downgrades to
        # `temporary` by itself when the deployment refuses public scope; once
        # a record says it did, ask for `temporary` outright rather than pay
        # a doomed flip (and a token rotation that kills the sent link) on
        # every Update.
        public_refused = bool(previous.get("public_refused"))
        out, err = _run_shim(
            {"action": "publish", "share_id": aid, "viewer_token": "UDF_Fused_App_File",
             "name": name, "file": out_path, "previous_remote": previous.get("remote"),
             "mode": "temporary" if public_refused else "public"},
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
            "mode": out.get("mode", "public"),
            "session_token": out.get("session_token"),
            "session_expires": out.get("session_expires"),
            "public_refused": public_refused or bool(out.get("public_refused")),
            "shared_at": previous.get("shared_at") or now,
            "updated_at": now,
        }
        put_record(aid, record)
        return {"ok": True, "shared": with_expiry(record)}
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
        **_adopted_scope(out, existing),
    }
    put_record(aid, record)
    return {"found": True, "app_id": aid, "shared": with_expiry(record)}


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


# -- the GitHub link -----------------------------------------------------------

# A hosted page that installs an app straight from its GitHub repo:
# `?repo=<owner>/<name>`. Nothing is uploaded — the link points at what is
# already pushed, so it is only offered when the folder IS the repo and every
# change in it is on the remote.
GITHUB_LINK_BASE = "https://udf.ai/fc_1XxeHZKmYjTdzf14pLsSnY/fused_app_github.html"

_GITHUB_REMOTE = re.compile(
    r"^(?:git@github\.com:|(?:https?|ssh)://(?:[^@/]+@)?(?:www\.)?github\.com/)"
    r"([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", re.I)


def _github_repo(remote_url: str) -> str | None:
    """`owner/name` for a GitHub remote URL (https or ssh), or None."""
    m = _GITHUB_REMOTE.match((remote_url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _origin_default_branch(gu, root: str) -> str | None:
    """The remote's default branch from local refs only, or None.

    `refs/remotes/origin/HEAD` when a clone recorded it — not
    `git_upstream._default_branch`, whose fallback (`remote set-head --auto`)
    asks the network. A `git init` + push never records that symref, so then
    `origin/main`, then `origin/master` — the names GitHub defaults to."""
    head = gu._run(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    short = gu._out(head) if gu._ok(head) else ""
    if "/" in short:
        return short.split("/", 1)[1]
    for name in ("main", "master"):
        if gu._ok(gu._run(root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{name}")):
            return name
    return None


def _github_copy_repo(path: str, *, nested: bool = True) -> str | None:
    """The AUTHOR's `owner/name` when the folder at `path` is — or, with
    `nested`, sits anywhere inside — a GitHub-link copy (contribution loop
    on, contrib record kind "github"), else None: the loop off, no record,
    any other kind, or the store unreadable (then today's behaviour
    answers). Nested matters for the upload refusal: an app folder inside
    Bob's copy of Alice's repo is still Alice's code. One store read for the
    whole walk. The record lives outside the folder, so no file in the repo
    can make a copy look like the user's own."""
    try:
        from fused_render.contrib import flags, store

        if not flags.sources():
            return None
        records = store.all() or {}
        here = store.key(path)
        while True:
            record = records.get(here)
            if (isinstance(record, dict) and record.get("kind") == "github"
                    and record.get("repo") and os.path.isdir(here)):
                return record["repo"]
            parent = os.path.dirname(here)
            if not nested or parent == here:
                return None
            here = parent
    except Exception:  # noqa: BLE001 — never worse than today's checks
        logger.warning("contrib: reading the install source failed for %s", path, exc_info=True)
    return None


#: Why a GitHub-link copy is never uploaded to Fused storage: it is someone
#: else's code, and its public way to be shared is the author's own link.
GITHUB_COPY_REFUSAL = "This app came from someone else's GitHub — share its GitHub link instead."


def _contributor_link(path: str, copy_status: dict | None = None) -> dict | None:
    """The author's link for a GitHub-link copy — the copy itself, never a
    folder nested in one (that link would install the whole repo) — or None
    (`_github_copy_repo`). `changes` counts what the link does NOT carry:
    the engine's local count of this copy's own edits, plus one for
    uncommitted edits (no network) — from `copy_status` when the caller
    already has it (the app page's read does), else one `engine.status`."""
    repo = _github_copy_repo(path, nested=False)
    if repo is None:
        return None
    try:
        if copy_status is None:
            from fused_render.contrib import engine

            copy_status = engine.status(path)
        changes = (int((copy_status or {}).get("changes") or 0)
                   + (1 if (copy_status or {}).get("dirty") else 0))
    except Exception:  # noqa: BLE001 — the count is a hint, the link stands
        logger.warning("contrib: change count failed for %s", path, exc_info=True)
        changes = 0
    return {"ok": True, "url": f"{GITHUB_LINK_BASE}?repo={repo}", "repo": repo,
            "reason": None, "code": "contributor", "changes": changes}


def github_link_state(path: str, *, copy_status: dict | None = None) -> dict:
    """Whether the folder at `path` can be shared as a GitHub link, and why not.

    Three conditions, checked in order, the first failure being the `reason`:
    the git root IS the app folder (an app inside the shared `local` repo, or
    any larger repo, would link to the whole repo), `origin` is a GitHub
    remote, and nothing is unpushed — a clean tree (untracked files count:
    they are code the link would not carry) and no commit on HEAD missing
    from the remote's default branch, which is what the link installs — never
    this branch's own upstream: a pushed feature branch is not what the link
    would install.

    NO NETWORK CALL — "pushed" is as of the last fetch, the same local-refs
    trade `app_doctor._pushed_pending` makes; this runs when the dialog opens.

    `code` names the failed condition in a word (`not_repo`, `nested`,
    `no_remote`, `not_github`, `dirty`, `unpushed`, `no_default`; None when
    ok) for a surface that must not show `reason`'s git words — the
    contribution loop's share sheet maps it to its own sentence
    (docs/CONTRIB_LOOP.md §15). `copy_status`: the engine's CopyStatus for
    this folder when the caller already computed it, so a contributor's link
    does not run `engine.status` a second time.

    A CONTRIBUTOR'S COPY shares the AUTHOR'S app (`_contributor_link`): with
    the contribution loop on, a copy recorded as installed from someone
    else's GitHub link answers that repo's link, `code: "contributor"`, and
    how many of its own edits the link does NOT carry — whatever its local
    tree holds, since Bob's unpublished edits were never what he is sharing.
    Own apps and remixes keep the checks below.
    """
    from fused_render import git_upstream as gu

    def no(code: str, reason: str, repo: str | None = None) -> dict:
        return {"ok": False, "url": None, "repo": repo, "reason": reason, "code": code}

    if not path or not os.path.isabs(path) or not os.path.isdir(path):
        return no("not_repo", "path must be an absolute app folder path")
    contributor = _contributor_link(path, copy_status)
    if contributor is not None:
        return contributor
    root = gu.repo_root(path)
    if root is None:
        return no("not_repo", "This app is not a git repository.")
    if os.path.realpath(path) != root:
        return no("nested", "This app sits inside a larger git repository — "
                  "the link needs the app folder to be the repository root.")
    remote = gu._run(root, "remote", "get-url", "origin")
    if not gu._ok(remote):
        return no("no_remote", "This app has no remote repository — push it to GitHub first.")
    repo = _github_repo(gu._out(remote))
    if repo is None:
        return no("not_github", "The app's remote is not on GitHub.")
    if not gu._is_clean(root):
        return no("dirty", "This app has changes that are not committed and pushed.", repo)
    default = _origin_default_branch(gu, root)
    if default is None:
        return no("no_default",
                  "Can't tell the repository's default branch — push it to GitHub first.", repo)
    count = gu._run(root, "rev-list", "--count", f"origin/{default}..HEAD")
    try:
        ahead = int(gu._out(count)) if gu._ok(count) else 1
    except ValueError:
        ahead = 1
    if ahead > 0:
        return no("unpushed", f"This app has commits not pushed to {default} yet.", repo)
    return {"ok": True, "url": f"{GITHUB_LINK_BASE}?repo={repo}", "repo": repo,
            "reason": None, "code": None}


@router.get("/api/share/github")
def api_share_github(path: str = ""):
    """Read-only, local git only: the GitHub install link for this app, or the
    one sentence saying why it is not offered (github_link_state)."""
    return github_link_state(path)
