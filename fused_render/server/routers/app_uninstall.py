"""Uninstall an app from every list that shows it — one door, three kinds:

* a **workspace folder** (`app/<slug>`, anything under `~/Fused` but the
  showcase clone) — the user's choice on the confirm:
  - *delete app data too* (the default): the whole folder goes to the OS
    Trash (the undo), or is hard-deleted behind the client's second confirm
    when the OS has no bin for it (501);
  - *keep app data*: the app's files and its `.git` are removed outright
    and `<app>/.fused/` is left where it is (data, cache, meta — minus the
    install stamp, which describes files that are gone). The folder is a
    husk the install page recognises: installing the app again lands in it
    and the app finds its data where it left it.
* **linked** — a folder outside the workspace: we never delete what we did
  not create; the registry entry goes, nothing else.
* **showcase** — a subfolder of the showcase clone: deleting it dirties the
  repo and the next sync restores it, so the path is hidden (`app_hidden`).

Every kind then archives the folder's tasks (the desk's own gesture, which
cancels pending scheduled runs), prunes the stores (`app_state_remove`) and
drops the hub snapshot so the card is gone on the next fetch.

`preflight` is the confirm dialog's data: what the user is about to lose.
"""
from __future__ import annotations

import logging
import os
import shutil

from fastapi import APIRouter, Body, Header, Query
from fastapi.responses import JSONResponse

from fused_render import app_fused_dir, app_hidden, app_state_remove
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers import apps as apps_router
from fused_render.server.routers import current_apps as desk_router

logger = logging.getLogger(__name__)

router = APIRouter()

KIND_FOLDER = "folder"
KIND_LINKED = "linked"
KIND_SHOWCASE = "showcase"

# Workspace tag folders that are shelves apps sit on, never an app themselves.
_TAG_SHELVES = frozenset({"app", "local", "showcase", "bots", "sandbox"})


def kind_for(path: str) -> str:
    from fused_render.shell.seed import fused_dir

    p = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(fused_dir()))
    if p == root or not p.startswith(root.rstrip(os.sep) + os.sep):
        return KIND_LINKED
    first = os.path.relpath(p, root).split(os.sep, 1)[0]
    return KIND_SHOWCASE if first == "showcase" else KIND_FOLDER


def _refuse_shelf(path: str) -> JSONResponse | None:
    """The workspace root and its tag folders, by name, whether or not they
    exist: a missing root would class as `linked` and prune every store row
    under it."""
    from fused_render.shell.seed import fused_dir

    ap = os.path.abspath(path)
    root = os.path.abspath(fused_dir())
    if os.path.normcase(ap) == os.path.normcase(root) or (
            os.path.normcase(os.path.dirname(ap)) == os.path.normcase(root)
            and os.path.basename(ap).lower() in _TAG_SHELVES):
        return _error("that is the workspace or one of its folders, not an app", status=400)
    return None


def _refuse_non_app(path: str) -> JSONResponse | None:
    """Only an APP folder: one whose page declares `fused-app`. The endpoint
    is reachable by any X-Fused caller; "uninstall" promises app semantics
    and nothing wider."""
    from fused_render import app_listing

    try:
        if not os.path.isdir(path) or app_listing.app_entry(path) is None:
            return _error("not an app folder (no page declares fused-app)", status=400)
    except OSError:
        return _error("cannot read that folder", status=400)
    return None


def _dir_size(path: str) -> tuple[int, int]:
    total = files = 0
    for dirpath, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(dirpath, n)).st_size
                files += 1
            except OSError:
                continue
    return total, files


def _uncommitted(path: str) -> int | None:
    """Changed paths git reports for the app, or None when no repo owns it."""
    from fused_render import app_git

    scope = app_git._repo_scope(os.path.abspath(path))
    if scope is None:
        return None
    repo, spec = scope
    try:
        out = app_git._git(repo, "status", "--porcelain", "--", app_git._pathspec(spec))
    except Exception:  # noqa: BLE001 — a hung or missing git is "unknown"
        return None
    if out.returncode != 0:
        return None
    return sum(1 for line in out.stdout.splitlines() if line.strip())


def _bots_using(path: str) -> list[str]:
    from fused_render.bots import store as bots_store

    root = os.path.abspath(path)
    names = []
    for b in bots_store.builds_read():
        d = b.get("dir")
        if isinstance(d, str) and d and app_state_remove._is_under(d, root):
            names.append(str(b.get("name") or os.path.basename(d)))
    return names


@router.get("/api/apps/uninstall/preflight")
def api_uninstall_preflight(path: str = Query(...)):
    """What uninstalling `path` would cost — the confirm dialog's numbers."""
    from fused_render.server import fs_mutate

    if not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    refused = _refuse_shelf(path)
    if refused is not None:
        return refused
    exists = os.path.isdir(path)
    if exists:
        refused = _refuse_non_app(path)
        if refused is not None:
            return refused
    kind = kind_for(path)
    data_bytes = data_files = 0
    if exists:
        data_bytes, data_files = _dir_size(app_fused_dir.dot_fused(path))
    folder = app_state_remove.canonical(path)
    return {
        "path": folder,
        "name": os.path.basename(path.rstrip("/\\")),
        "exists": exists,
        "kind": kind,
        "trash_supported": fs_mutate._trash_supported(),
        "data_bytes": data_bytes,
        "data_files": data_files,
        "uncommitted": _uncommitted(path) if exists and kind == KIND_FOLDER else None,
        "tasks": len(desk_router._folder_tasks(folder)),
        "bots": _bots_using(path),
    }


class _StripFailed(Exception):
    """A keep-data uninstall could not remove one of the app's entries. The
    message names it; the folder is left as it stands."""


def _strip_app_files(path: str) -> int:
    """Keep-data uninstall: every top-level entry but `.fused/` goes — the
    app's files and its `.git`. The install stamp inside `.fused/` becomes a
    HUSK stamp (`appfile.write_husk`): no files, but the app's id, so the
    next install of this app finds the folder wherever it sits and another
    app sharing the slug is refused the data. Hard delete: the app is
    reinstallable from its file, and the Trash would hold a folder-shaped
    pile of loose files.

    All or nothing in effect: the entry page's own top-level entry goes
    LAST, and the first failure (a locked `.venv` is the common one) stops
    the strip and raises — the folder still declares an app, so the hub
    still lists it and the user can retry; no husk stamp is written over a
    folder that is not a husk, and the stores are not pruned for an app
    that is still there. Answers how many entries were removed."""
    from fused_render import app_id as app_identity
    from fused_render import app_listing, appfile

    try:
        entry = app_listing.app_entry(path)
    except OSError:
        entry = None
    app_id = app_identity.app_id(entry) if entry else None
    entry_top = None
    if entry:
        rel = os.path.relpath(os.path.abspath(entry), os.path.abspath(path))
        entry_top = rel.split(os.sep, 1)[0]
    names = [n for n in os.listdir(path) if n != app_fused_dir.DIRNAME]
    names.sort(key=lambda n: (n == entry_top, n))
    removed = 0
    for name in names:
        p = os.path.join(path, name)
        try:
            if os.path.islink(p) or not os.path.isdir(p):
                os.remove(p)
            else:
                _lift_read_only(p)
                shutil.rmtree(p)
        except OSError as exc:
            raise _StripFailed(
                f"could not remove {p} ({exc.strerror or exc}); the app's remaining files "
                "are still there — close whatever holds it and try again") from exc
        removed += 1
    try:
        appfile.write_husk(path, app_id, os.path.basename(path.rstrip("/\\")))
    except OSError:
        logger.warning("could not write the husk stamp at %s", path, exc_info=True)
    return removed


def _lift_read_only(root: str) -> None:
    for dirpath, _dirs, names in os.walk(root):
        for n in names:
            try:
                os.chmod(os.path.join(dirpath, n), 0o644)
            except OSError:
                continue


@router.post("/api/apps/uninstall")
def api_uninstall(body: dict = Body(...), x_fused: str | None = Header(default=None)):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    from fused_render.server import fs_mutate

    path = body.get("path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return _error("'path' must be an absolute filesystem path")
    refused = _refuse_shelf(path)
    if refused is not None:
        return refused
    exists = os.path.isdir(path)
    if exists:
        refused = _refuse_non_app(path)
        if refused is not None:
            return refused
    kind = kind_for(path)
    keep_data = body.get("keep_data") is True
    mode = body.get("mode") or "trash"
    if mode not in ("trash", "delete"):
        return _error(f"mode {mode!r} is not trash or delete")
    folder = app_state_remove.canonical(path)

    result: dict = {"ok": True, "path": folder, "kind": kind, "keep_data": keep_data,
                    "removed_files": False, "data_kept_at": None}
    if kind == KIND_FOLDER and exists:
        if keep_data:
            try:
                result["removed_entries"] = _strip_app_files(path)
            except _StripFailed as exc:
                # Nothing below runs: the folder still declares an app.
                return _error(str(exc), status=500)
            result["removed_files"] = True
            result["data_kept_at"] = app_fused_dir.dot_fused(path).replace(os.sep, "/")
            fs_mutate._note_index_mutation({"deleted": path}, path)
            # A `local/` app's files left the shared repo: record it there as
            # a whole-folder delete does, so the repo is not dirty for good.
            fs_mutate._record_app_removal(path)
        else:
            out = fs_mutate._fs_delete({"path": path, "trash": mode == "trash", "recursive": True}, x_fused)
            if isinstance(out, JSONResponse):
                # Nothing was removed, so nothing below runs either: a folder
                # still on disk is still an app. A 501 (no bin) tells the
                # client to offer the hard delete behind its own confirm.
                if mode == "trash" and out.status_code == 501:
                    return JSONResponse({"error": "trash unsupported"}, status_code=501)
                return out
            fs_mutate._note_index_mutation(out, path)
            result["removed_files"] = True
            if out.get("trashed_to"):
                result["trashed_to"] = out["trashed_to"]
    elif kind == KIND_SHOWCASE:
        app_hidden.hide(path)
    result.update(desk_router._archive_folder(folder))
    result["pruned"] = app_state_remove.prune_stores(path)
    apps_router._reset_snapshot()
    logger.info("uninstalled %s (%s, keep_data=%s)", folder, kind, keep_data)
    return result
