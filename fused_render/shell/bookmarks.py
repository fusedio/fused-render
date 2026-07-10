"""GET/PUT /api/bookmarks — the bookmark tree at ~/.fused-render/bookmarks.json.

Server-side successor to the old localStorage store (DECISIONS D21 anticipated
this move as a "trivial export"). Whole-file, last-write-wins: the tree is tiny
(a handful of items, one level of folders) so there is no partial-update API —
the shell PUTs the entire tree on each mutation.

The GET `exists` flag is load-bearing: it is false only until the file is first
written, letting the shell run its one-time localStorage import exactly once
(a user who later deletes every bookmark leaves an existing `[]` file, so the
old localStorage data is never re-imported). See frontend lib/bookmarks.ts.
"""
import json
import os
import time
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()

_VIEW_PREFIXES = ("/view/", "/embed/")
_SENTINELS = ("_panel", "_tab")


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused (a custom header forces a CORS
    # preflight that fails cross-origin, blocking a blind foreign PUT).
    # Duplicated deliberately: shell/ must not import server (no server<->shell
    # cycle — the router is imported the other way in create_app).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "bookmarks.json")


def _dedupe_names(items: list) -> bool:
    """One-time migration (D97): make bookmark names globally unique,
    case-insensitive, across top-level bookmarks and folder children (folder
    names are a separate namespace and are left alone). The oldest bookmark by
    created_at keeps its name; each newer duplicate gets the first `-1`, `-2`,
    ... suffix that collides with nothing already in the tree. Idempotent —
    returns True only when something was renamed."""
    bookmarks = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "folder":
            bookmarks.extend(c for c in item.get("children") or [] if isinstance(c, dict))
        else:
            bookmarks.append(item)
    named = [b for b in bookmarks if isinstance(b.get("name"), str)]
    # Suffix candidates must dodge EVERY current name (including a pre-existing
    # literal "x-1"), not just the ones processed so far.
    taken = {b["name"].lower() for b in named}
    seen = set()
    changed = False
    for b in sorted(named, key=lambda b: b.get("created_at") or 0):
        key = b["name"].lower()
        if key not in seen:
            seen.add(key)  # first (oldest) holder keeps the name
            continue
        n = 1
        while f"{b['name']}-{n}".lower() in taken:
            n += 1
        b["name"] = f"{b['name']}-{n}"
        taken.add(b["name"].lower())
        changed = True
    return changed


@router.get("/api/bookmarks")
def get_bookmarks():
    data = storage.read_json(_path())
    # Absent or corrupt (not a list) -> report not-yet-written so the shell may
    # import from localStorage; a valid file (even []) reports exists=true.
    if not isinstance(data, list):
        return {"exists": False, "bookmarks": []}
    # Pre-D97 files may hold duplicate names; migrate once (write only when
    # something actually changed — the normal GET stays read-only).
    if _dedupe_names(data):
        storage.write_json(_path(), data)
    return {"exists": True, "bookmarks": data}


@router.put("/api/bookmarks")
def put_bookmarks(
    bookmarks: list = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    storage.write_json(_path(), bookmarks)
    return {"ok": True, "count": len(bookmarks)}


# --------------------------------------------------------- .bookmark file export
#
# "Save to disk" (SB-8, D98): the frontend computes the whole file —
# destination dir (the deepest common ancestor of the bookmark's targets),
# `<name>.bookmark` filename and the format-v1 JSON content
# (lib/bookmark-file.ts, next to the `_layout` codec it reuses) — and this
# endpoint only validates and writes. Overwrite is allowed by design: the
# name is globally unique (D97), so an existing file is a stale snapshot of
# the same bookmark and a re-save refreshes it.


@router.post("/api/bookmarks/export")
def export_bookmark(
    payload: dict = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    dir_ = payload.get("dir")
    filename = payload.get("filename")
    content = payload.get("content")
    if not (isinstance(dir_, str) and isinstance(filename, str) and isinstance(content, str)):
        return JSONResponse({"error": "dir, filename and content required"}, status_code=400)
    if not os.path.isabs(dir_) or not os.path.isdir(dir_):
        return JSONResponse({"error": "dir must be an existing absolute directory"}, status_code=400)
    # Bare `<stem>.bookmark` only — no separators, no traversal, non-empty stem.
    stem = filename[: -len(".bookmark")]
    if (
        not filename.endswith(".bookmark")
        or not stem
        or stem in (".", "..")
        or "/" in filename
        or "\\" in filename
        or filename != os.path.basename(filename)
    ):
        return JSONResponse({"error": "filename must be a bare <name>.bookmark"}, status_code=400)
    # Defense against a garbage body reaching disk: the content must at least
    # be a JSON object with an integer format version (bool is not a version).
    try:
        doc = json.loads(content)
    except ValueError:
        doc = None
    version = doc.get("version") if isinstance(doc, dict) else None
    if not isinstance(version, int) or isinstance(version, bool):
        return JSONResponse({"error": "content must be .bookmark JSON with an int version"}, status_code=400)
    path = os.path.join(dir_, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": path}


# ------------------------------------------------------ bookmark history sidecar
#
# A bookmark create/url-update is mirrored into the target file's `<file>.json`
# sidecar under "bookmarkHistory" — the same file the claude chat template keeps
# next to each target (templates/claude/agent.py). This is the file's permanent
# record of every bookmark ever saved for it; delete is a no-op (history stays).


def _fs_path_from_url(url: str) -> str | None:
    """Resolve a bookmark shell url to the absolute filesystem path it targets,
    or None when it does not name a real single file/dir on disk.

    Mirrors frontend router.fsPathFromLocation: strip the /view/ or /embed/
    prefix and query, decode each path segment. Sentinel layout/tab urls
    (`_panel` / `_tab`) and anything that does not exist on disk resolve to
    None (they get no sidecar)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = parts.path
    for prefix in _VIEW_PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix):]
            break
    else:
        return None
    segments = [unquote(s) for s in rest.split("/") if s]
    # Sentinels are the exact top-level pathnames `/view/_panel` and `/view/_tab`
    # (App.tsx matches the whole pathname) — a real file/dir named `_panel`/`_tab`
    # nested deeper is NOT a sentinel and gets a sidecar like any other path.
    if not segments or (len(segments) == 1 and segments[0] in _SENTINELS):
        return None
    joined = "/".join(segments)
    # Mirror the frontend's rootedFsPath (lib/router.ts): a Windows drive-letter
    # path (`C:/...`) is already absolute and keeps its form, a bare drive (`C:`)
    # gets a trailing slash, and every POSIX path gets the leading `/`. Prepending
    # `/` unconditionally would corrupt `C:/...` into `/C:/...` and miss on disk.
    if len(joined) == 2 and joined[0].isalpha() and joined[1] == ":":
        fs_path = joined + "/"
    elif len(joined) >= 3 and joined[0].isalpha() and joined[1] == ":" and joined[2] == "/":
        fs_path = joined
    else:
        fs_path = "/" + joined
    # Only a path that actually exists gets a sidecar (a file OR a directory
    # listing — both are bookmarkable). Missing paths / sentinels no-op.
    if not os.path.exists(fs_path):
        return None
    return fs_path


def _sidecar_path(fs_path: str) -> str:
    return fs_path + ".json"


def _record_history(fs_path: str, entry: dict) -> None:
    """Upsert `entry` (keyed by its `id`) into the sidecar's bookmarkHistory
    array, preserving `claudeSessions` and every other key. Best-effort: never
    raise into the request handler (bookkeeping must not break bookmarking).

    `entry["created_at"]` is ms epoch (matches the bookmark record / Date.now);
    `recorded_at`/`updated_at` are server `time.time()` SECONDS (matches
    agent.py's created_at/last_used). Different units in one file, by design —
    do not "unify" them."""
    sidecar = _sidecar_path(fs_path)
    data = storage.read_json(sidecar)
    if not isinstance(data, dict):
        data = {}
    # Keep agent.py's _load_sidecar guard happy so a claude turn round-trips
    # bookmarkHistory instead of dropping it (see spec-2 defense-in-depth).
    data.setdefault("claudeSessions", [])
    history = data.get("bookmarkHistory")
    if not isinstance(history, list):
        history = []
    now = time.time()
    for existing in history:
        if isinstance(existing, dict) and existing.get("id") == entry["id"]:
            existing.update({k: v for k, v in entry.items() if v is not None})
            existing["updated_at"] = now
            break
    else:
        history.append({
            **{k: v for k, v in entry.items() if v is not None},
            "recorded_at": now,
            "updated_at": now,
        })
    data["bookmarkHistory"] = history
    storage.write_json(sidecar, data)


@router.post("/api/bookmarks/history")
def post_bookmark_history(
    payload: dict = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    bid = payload.get("id")
    url = payload.get("url")
    if not isinstance(bid, str) or not isinstance(url, str):
        return JSONResponse({"error": "id and url required"}, status_code=400)
    fs_path = _fs_path_from_url(url)
    if fs_path is None:
        # Sentinel / directory-that-vanished / non-file url -> nothing to record.
        return {"recorded": False}
    # Store only the portable query string, NOT the incoming url: the sidecar
    # lives next to fs_path, so the target file is implicit (it is the sidecar's
    # owner). Persisting the absolute /view/<abs-path> url would break every
    # history entry the moment the file + its sidecar are moved together. The
    # search reconstructs the bookmark relative to whatever file owns the
    # sidecar. Empty search ("") is a bare bookmark of the file itself.
    entry = {
        "id": bid,
        "name": payload.get("name"),
        "search": urlsplit(url).query,
        "created_at": payload.get("created_at"),
        "icon": payload.get("icon"),
    }
    try:
        _record_history(fs_path, entry)
    except OSError:
        # Unwritable dir, etc. — never break the bookmark itself.
        return {"recorded": False}
    return {"recorded": True}
