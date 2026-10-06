"""GET/PUT/POST /api/recents — recently opened files at ~/.fused-render/recents.json.

The sidebar's "Recents" section: the last files opened in the shell, each with
the last params they carried (the entry url is updated live while the file is
open — see frontend lib/recents.ts). Files only: directory navigation and
sentinel routes (`_panel`, `_prefs`, any `_`-prefixed view) are never recorded.
App ENTRY pages (workspace or registered) aren't either — rendering one IS
the open and GET /render records it (D301), see _is_app_entry.

File shape:

    {"collapsed": false, "entries": [
        {"url": "/view/...?...", "openedAt": iso, "title": "optional page title"}
    ]}

Entries are newest-first, deduped by target fs path, capped at ENTRY_CAP (a
buffer so the UI's top 3 survive missing-file filtering). URLs are stored
verbatim including the query string — same "URL is the whole state" posture as
bookmarks (D20). `collapsed` lives in the data file itself, matching D44's
persisted folder collapse. Entries whose file has since been deleted are
hidden from the GET response but never deleted from disk — the file may come
back (an undeleted trash item).
"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import storage

router = APIRouter()

# Current prefix first; the bare "/view/" accepts entries written before the
# /explorer namespace rename (they open through the client's legacy rewrite).
VIEW_PREFIXES = ("/explorer/view/", "/view/")

# Cap well above the UI's 3 visible rows so enough valid entries remain after
# missing-file filtering.
ENTRY_CAP = 20

# Total wall-clock budget for ALL of a GET /api/recents' existence checks. The
# checks fan out concurrently (on _CHECK_POOL below) and share this one deadline
# — so the endpoint stays bounded no matter how many entries sit on a slow or hung
# mount. An entry whose check outlives the budget is KEPT, not dropped (fail
# open): a possibly-dead sidebar row beats a multi-second stall. Sized like the
# fs/events watch ticker's per-stat _STAT_TIMEOUT_S (server.py, 4.0s) but tighter
# — this is the whole sidebar section, not one background poll.
CHECK_BUDGET_S = 1.5

# Existence checks run on a DEDICATED pool, not asyncio's default loop executor:
# a check that outlives CHECK_BUDGET_S keeps running in its thread (a blocking
# os.stat/rc call can't be cancelled mid-flight), and parking those on the
# shared default executor makes the request wait for them at loop teardown.
# ENTRY_CAP-wide so every entry's check can run at once (true fan-out); slots
# free themselves as the local isfile / bounded rc_mtime_for calls return.
_CHECK_POOL = ThreadPoolExecutor(
    max_workers=ENTRY_CAP, thread_name_prefix="recents-exists"
)


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
    prefix = next((p for p in VIEW_PREFIXES if path.startswith(p)), None)
    if prefix is None:
        return None
    segments = [unquote(s) for s in path[len(prefix):].split("/") if s]
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
    if fs_path is None:
        return None
    return fs_path if _local_exists(fs_path) else None


def _is_app_entry(fs_path: str) -> bool:
    """Whether `fs_path` is an app's ENTRY page — a workspace app's or a
    registered (linked) one's. Those never appear in the FILE recents: the
    open is already recorded by GET /render when it serves the marker-carrying
    page (D301 — server/routers/render.py feeds the app recents store and the
    registry), and the same open landing in both sidebar lists is the
    duplication this filters. Applied at record time AND as a GET filter
    (entries recorded before this rule, hide-not-delete like the missing-file
    filter). Fails toward False — an indeterminate answer must record/keep
    the row, never hide a file from both lists."""
    from fused_render import app_listing, registered_apps
    from fused_render.shell.seed import fused_dir

    try:
        return (app_listing.is_workspace_app_entry(fs_path, fused_dir())
                or registered_apps.is_registered_app_entry(fs_path))
    except Exception:
        return False


def _local_exists(fs_path: str) -> bool:
    """Whether `fs_path` is an existing FILE (recents are files-only, D22)."""
    try:
        return os.path.isfile(fs_path)
    except OSError:
        return False


async def _keep_entry(url: str) -> bool:
    """Whether a recents entry should appear in the GET response: True to keep,
    False to filter (file confirmed gone). Runs the existence check off the
    event loop. Only a check
    that COMPLETES False filters; anything indeterminate keeps (fail open)."""
    fs_path = _decoded_fs_path(url)
    if fs_path is None:
        return False  # not a file-naming url (sentinel / non-/view/) -> filtered
    check = _local_exists
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_CHECK_POOL, check, fs_path)
    except Exception:
        return True  # fail open on any unexpected error


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
async def get_recents():
    """The collapsed flag + entries whose target file still exists. Missing
    files are filtered from the response, NOT deleted from disk (read-only
    GET, like bookmarks; the file may reappear).

    Existence checks fan out concurrently under a single CHECK_BUDGET_S deadline
    and are bounded. An entry whose check outlives the budget is KEPT (fail
    open) — a possibly-dead row beats a stalled sidebar."""
    data = _read()
    raw = [e for e in data["entries"] if isinstance(e.get("url"), str)]
    # App entry pages are hidden (never deleted) — they live in the app
    # recents instead. Synchronous, unlike the existence fan-out: the check
    # only ever touches the LOCAL workspace (see _is_app_entry).
    raw = [e for e in raw
           if (p := _decoded_fs_path(e["url"])) is None or not _is_app_entry(p)]
    keeps = [True] * len(raw)  # default keep (fail open on timeout/cancel)
    if raw:
        tasks = [asyncio.ensure_future(_keep_entry(e["url"])) for e in raw]
        done, pending = await asyncio.wait(tasks, timeout=CHECK_BUDGET_S)
        for i, t in enumerate(tasks):
            if t in done:
                # _keep_entry never raises, but stay defensive: keep on error.
                keeps[i] = t.result() if not t.cancelled() else True
        for t in pending:
            t.cancel()  # deadline hit; underlying thread finishes on its own
    entries = [e for e, keep in zip(raw, keeps) if keep]
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
        # Directory / sentinel / missing-file url -> benign no-op: a recent is
        # a FILE row, so a url with no file behind it has nothing to record.
        return {"recorded": False}
    # The rendered page's own <title> (frontend lib/recents.ts), preferred
    # over the file's basename by the sidebar row — same posture as bookmark
    # naming. Optional: absent until the preview iframe reports one.
    title_raw = payload.get("title")
    title = title_raw.strip() if isinstance(title_raw, str) and title_raw.strip() else None
    if _is_app_entry(fs_path):
        # An app's entry page belongs to the APP recents, not this store.
        # No forwarding needed: rendering the page IS the open, and GET
        # /render already recorded it (D301) — recording here too would list
        # every app in both sidebar sections.
        return {"recorded": False}
    data = _read()
    # Dedupe by DECODED target fs path — existence-blind, so a dead entry for
    # the same path (file deleted and recreated since) is replaced rather than
    # left wasting a cap slot beside the fresh one. A title-less record (the
    # synchronous open-record always fires before the async title arrives;
    # some modes never report one at all) must not erase a title learned on a
    # PREVIOUS open — carry the old one forward when this record doesn't
    # supply a fresher one, so a known title only ever improves, never regresses.
    existing_title = None
    kept = []
    for e in data["entries"]:
        if isinstance(e.get("url"), str) and _decoded_fs_path(e["url"]) == fs_path:
            t = e.get("title")
            if existing_title is None and isinstance(t, str) and t:
                existing_title = t
            continue
        kept.append(e)
    entry = {"url": url, "openedAt": datetime.now(timezone.utc).isoformat()}
    if title is not None:
        entry["title"] = title
    elif existing_title is not None:
        entry["title"] = existing_title
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
