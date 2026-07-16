"""GET/PUT/POST /api/recents — recently opened files at ~/.fused-render/recents.json.

The sidebar's "Recents" section: the last files opened in the shell, each with
the last params they carried (the entry url is updated live while the file is
open — see frontend lib/recents.ts). Files only: directory navigation and
sentinel routes (`_panel`, `_prefs`, any `_`-prefixed view) are never recorded.

File shape:

    {"collapsed": false, "entries": [{"url": "/view/...?...", "openedAt": iso}]}

Entries are newest-first, deduped by target fs path, capped at ENTRY_CAP (a
buffer so the UI's top 3 survive missing-file filtering). URLs are stored
verbatim including the query string — same "URL is the whole state" posture as
bookmarks (D20). `collapsed` lives in the data file itself, matching D44's
persisted folder collapse. Entries whose file has since been deleted are
hidden from the GET response but never deleted from disk — the file may come
back (a mount reconnect, an undeleted trash item).
"""
import os
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()

VIEW_PREFIX = "/view/"

# Cap well above the UI's 3 visible rows so enough valid entries remain after
# missing-file filtering.
ENTRY_CAP = 20


def _require_fused(x_fused: str | None) -> JSONResponse | None:
    # Same D3 guard as server._require_fused, duplicated to keep shell↛server
    # acyclic (see shell/bookmarks.py).
    if x_fused != "1":
        return JSONResponse({"error": "missing X-Fused header"}, status_code=403)
    return None


def _path() -> str:
    return os.path.join(storage.home_dir(), "recents.json")


def _decoded_fs_path(url: str) -> str | None:
    """Decode a shell url to the absolute fs path it names, or None when it
    cannot name a file at all: non-/view/ urls and sentinel routes (any
    `_`-prefixed top-level pathname — `_panel`, `_prefs`, `_templates`, ...).

    Deliberately does NOT check disk: this is the dedupe identity, and two
    entries for the same path must dedupe even while the file is deleted
    (existence matters only for GET filtering and for accepting a new record —
    see _file_path_from_url).

    Mirrors bookmarks._fs_path_from_url but is stricter: only under /view/
    (recording never happens in embed panes)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    path = parts.path
    if not path.startswith(VIEW_PREFIX):
        return None
    segments = [unquote(s) for s in path[len(VIEW_PREFIX):].split("/") if s]
    # Any `_`-prefixed top-level pathname is a shell sentinel (`_panel`,
    # `_prefs`, `_templates`, `_listing`, ...) — the whole namespace is
    # shell-owned, so reject the prefix rather than enumerating names.
    if not segments or (len(segments) == 1 and segments[0].startswith("_")):
        return None
    joined = "/".join(segments)
    # Mirror the frontend's rootedFsPath (lib/router.ts): keep a Windows
    # drive-letter path absolute as-is; prepend "/" only for POSIX paths.
    if len(joined) == 2 and joined[0].isalpha() and joined[1] == ":":
        fs_path = joined + "/"
    elif len(joined) >= 3 and joined[0].isalpha() and joined[1] == ":" and joined[2] == "/":
        fs_path = joined
    else:
        fs_path = "/" + joined
    return fs_path


def _file_path_from_url(url: str) -> str | None:
    """`_decoded_fs_path`, additionally requiring the path to be an existing
    FILE right now (recents record files only, D22-style basename rows) —
    the gate for accepting a record and for the GET response filter."""
    fs_path = _decoded_fs_path(url)
    if fs_path is None or not os.path.isfile(fs_path):
        return None
    return fs_path


def _read() -> dict:
    data = storage.read_json(_path())
    if not isinstance(data, dict):
        return {"collapsed": False, "entries": []}
    entries = data.get("entries")
    return {
        "collapsed": data.get("collapsed") is True,
        "entries": [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else [],
    }


@router.get("/api/recents")
def get_recents():
    """The collapsed flag + entries whose target file still exists. Missing
    files are filtered from the response, NOT deleted from disk (read-only
    GET, like bookmarks; the file may reappear)."""
    data = _read()
    entries = [
        e for e in data["entries"]
        if isinstance(e.get("url"), str) and _file_path_from_url(e["url"]) is not None
    ]
    return {"collapsed": data["collapsed"], "entries": entries}


@router.post("/api/recents/open")
def post_recent_open(
    payload: dict = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    url = payload.get("url")
    if not isinstance(url, str):
        return JSONResponse({"error": "url required"}, status_code=400)
    fs_path = _file_path_from_url(url)
    if fs_path is None:
        # Directory / sentinel / missing-file url -> benign no-op, same
        # posture as POST /api/bookmarks/history for non-file urls.
        return {"recorded": False}
    data = _read()
    # Dedupe by DECODED target fs path — existence-blind, so a dead entry for
    # the same path (file deleted and recreated since) is replaced rather than
    # left wasting a cap slot beside the fresh one.
    kept = [
        e for e in data["entries"]
        if not (isinstance(e.get("url"), str) and _decoded_fs_path(e["url"]) == fs_path)
    ]
    entry = {"url": url, "openedAt": datetime.now(timezone.utc).isoformat()}
    data["entries"] = [entry, *kept][:ENTRY_CAP]
    storage.write_json(_path(), data)
    return {"recorded": True}


@router.put("/api/recents/collapsed")
def put_recents_collapsed(
    payload: dict = Body(...), x_fused: str | None = Header(default=None)
):
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    collapsed = payload.get("collapsed")
    if not isinstance(collapsed, bool):
        return JSONResponse({"error": "'collapsed' must be a boolean"}, status_code=400)
    data = _read()
    data["collapsed"] = collapsed
    storage.write_json(_path(), data)
    return {"collapsed": collapsed}
