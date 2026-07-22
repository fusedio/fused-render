"""GET/PUT /api/bookmarks — the bookmark tree at ~/.fused-render/bookmarks.json.

Whole-file, last-write-wins: the tree is tiny (a handful of items, folders
nesting to arbitrary depth — D121, revising D44's one-level rule) so there is
no partial-update API — the shell PUTs the entire tree on each mutation.

The GET `exists` flag distinguishes an absent/corrupt file from a valid
(possibly empty) one — a user who deletes every bookmark leaves an existing
`[]` file, which still reports `exists=true`. See frontend lib/bookmarks.ts.

GET also reports `missing`: bookmark ids whose target is confirmed gone (see
the "missing-file flag" section below) — display-only, recomputed every GET,
never persisted or round-tripped through PUT.
"""
import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from fused_render.shell import mounts, pathops, storage

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


# Bookmark name -> filename stem, mirroring sanitizeBookmarkStem in the
# frontend (lib/bookmarks.ts): path separators, the colon and control chars
# become "-". The char class must stay in sync with the TS regex.
_STEM_UNSAFE = re.compile(r"[/\\:\x00-\x1f\x7f]")


def _name_key(name: str) -> str:
    """D97 uniqueness comparison key: the sanitized `.bookmark` filename stem,
    lowercased. Keying on the stem (not the raw name) means two names that
    would sanitize to the same filename (`a/b` vs `a:b`) count as duplicates,
    so export files can never silently overwrite each other."""
    return _STEM_UNSAFE.sub("-", name).strip().lower()


def _dedupe_names(items: list) -> bool:
    """One-time migration (D97): make bookmark names globally unique by
    sanitized-stem key (case-insensitive), across top-level bookmarks and
    folder children (folder names are a separate namespace and are left
    alone). The oldest bookmark by created_at keeps its name; each newer
    duplicate gets the first `-1`, `-2`, ... suffix whose key collides with
    nothing already in the tree ("-" and digits survive sanitization, so
    suffixed keys stay distinct). Idempotent — returns True only when
    something was renamed. Folders nest to arbitrary depth (D121) — the walk
    recurses so a bookmark in a grandchild folder shares the same namespace."""
    bookmarks = []

    def collect(entries: list) -> None:
        for item in entries:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "folder":
                children = item.get("children")
                if isinstance(children, list):
                    collect(children)
            else:
                bookmarks.append(item)

    collect(items)
    named = [b for b in bookmarks if isinstance(b.get("name"), str)]
    # Suffix candidates must dodge EVERY current name (including a pre-existing
    # literal "x-1"), not just the ones processed so far.
    taken = {_name_key(b["name"]) for b in named}
    seen = set()
    changed = False
    for b in sorted(named, key=lambda b: b.get("created_at") or 0):
        key = _name_key(b["name"])
        if key not in seen:
            seen.add(key)  # first (oldest) holder keeps the name
            continue
        n = 1
        while _name_key(f"{b['name']}-{n}") in taken:
            n += 1
        b["name"] = f"{b['name']}-{n}"
        taken.add(_name_key(b["name"]))
        changed = True
    return changed


def _sanitize_tree(items: list) -> bool:
    """Drop entries the sidebar cannot render (D121: folders nest to arbitrary
    depth, revising D44). An entry — at the top level or inside any folder's
    `children` — is kept iff it is a dict AND either a bookmark (string `url`)
    or a folder (`type == "folder"` with a list `children`, sanitized
    recursively). Anything else (urlless plain dict, folder with non-list
    children, non-dict) would render as a bookmark row and crash reading its
    missing `.url`, blanking the whole shell — the same failure at every
    level, so the top level gets the same rule rather than D44's leniency.
    Mutates in place; returns True only when something was removed (same
    write-only-on-change posture as _dedupe_names). No cycle guard: the tree
    always comes from json.load, which cannot produce reference cycles."""
    changed = False
    kept = []
    for item in items:
        if not isinstance(item, dict):
            changed = True
            continue
        if item.get("type") == "folder":
            children = item.get("children")
            if not isinstance(children, list):
                changed = True
                continue
            if _sanitize_tree(children):
                changed = True
        elif not isinstance(item.get("url"), str):
            changed = True
            continue
        kept.append(item)
    items[:] = kept
    return changed


# ------------------------------------------------------ missing-file flag (GET)
#
# GET /api/bookmarks additionally reports which bookmarks' targets are
# confirmed gone (a "missing" id list) — display-only, computed fresh on every
# GET and never written into bookmarks.json or round-tripped through PUT (the
# whole-tree PUT contract, D75, stays exactly the tree the frontend sent).
# Existence checks fan out concurrently under one wall-clock budget and are
# mount-safe (pathops.exists), mirroring recents.py's CHECK_BUDGET_S/_CHECK_POOL
# — the tree is "a handful of items" (module intro) but one could still sit on
# a hung/slow mount, and this endpoint must stay bounded regardless. A check
# that outlives the budget is NOT flagged (fail open): a possibly-stale row
# beats a stalled sidebar. Unlike recents (files-only, D22), a bookmark may
# target a directory listing too, so this uses pathops.exists, not is_file.

_MISSING_CHECK_BUDGET_S = 1.5
_MISSING_CHECK_POOL = ThreadPoolExecutor(
    max_workers=16, thread_name_prefix="bookmarks-exists"
)


def _flatten_bookmarks(items: list) -> list:
    """All bookmark leaves (not folders), at every depth, in tree order — the
    set the missing-file check fans its probes over. Folders nest to arbitrary
    depth (D121); mirrors _dedupe_names' own collect() walk."""
    out = []

    def walk(entries: list) -> None:
        for item in entries:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "folder":
                children = item.get("children")
                if isinstance(children, list):
                    walk(children)
            else:
                out.append(item)

    walk(items)
    return out


def _bookmark_missing(url: str) -> bool:
    """Whether a bookmark's target is CONFIRMED gone: only when its url decodes
    to a real fs path (_decode_fs_path) AND a mount-safe probe proves absence. A
    url naming no fs target at all (sentinel, unparseable) is never flagged —
    there's nothing to confirm missing, so it reads as present."""
    fs_path = _decode_fs_path(url)
    if fs_path is None:
        return False
    return not pathops.exists(fs_path)


async def _compute_missing(leaves: list) -> list:
    """Ids of `leaves` (from _flatten_bookmarks) whose target is confirmed
    missing, checked concurrently on a dedicated pool under one budget (see
    module note above) — mirrors recents._keep_entry's fan-out/fail-open shape.

    Walks `candidates`/`tasks` in lockstep by INDEX (mirrors recents.get_recents'
    `enumerate(tasks)` pairing), not by iterating the `done` set directly: a
    `set` of Task objects has no defined iteration order, which would make the
    returned id list nondeterministic between requests."""
    candidates = [
        b for b in leaves
        if isinstance(b.get("id"), str) and isinstance(b.get("url"), str)
    ]
    if not candidates:
        return []
    loop = asyncio.get_running_loop()

    async def check(url: str) -> bool:
        try:
            return await loop.run_in_executor(_MISSING_CHECK_POOL, _bookmark_missing, url)
        except Exception:
            return False  # unexpected error -> fail open, don't flag

    tasks = [asyncio.ensure_future(check(b["url"])) for b in candidates]
    done, pending = await asyncio.wait(tasks, timeout=_MISSING_CHECK_BUDGET_S)
    missing_ids = []
    for b, t in zip(candidates, tasks):
        # check() never raises, but stay defensive: skip on error/cancellation.
        if t in done and not t.cancelled() and t.result():
            missing_ids.append(b["id"])
    for t in pending:
        t.cancel()  # deadline hit; underlying thread finishes on its own
    return missing_ids


@router.get("/api/bookmarks")
async def get_bookmarks():
    data = storage.read_json(_path())
    # Absent or corrupt (not a list) -> report not-yet-written; a valid file
    # (even []) reports exists=true.
    if not isinstance(data, list):
        return {"exists": False, "bookmarks": [], "missing": []}
    # Order matters: sanitize before dedupe, so a dropped garbage entry never
    # claims a name that a real bookmark would then get suffixed around.
    changed = _sanitize_tree(data)
    # Pre-D97 files may hold duplicate names; migrate once (write only when
    # something actually changed — the normal GET stays read-only).
    if _dedupe_names(data):
        changed = True
    if changed:
        storage.write_json(_path(), data)
    missing = await _compute_missing(_flatten_bookmarks(data))
    return {"exists": True, "bookmarks": data, "missing": missing}


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


# ------------------------------------------------------- .bookmark file open
#
# Finder double-click (SB-9, D99): the app maps a `.bookmark` document to the
# `/view/_bookmark?file=<abs path>` sentinel, whose frontend calls this to
# read the file, then resolves the relative paths inside it against `dir` and
# redirects — mirror of the export split above: the `_layout` grammar's only
# parser lives in the shell codec, so the server stays a dumb validated reader.
# Read-only GET, unguarded like GET /api/bookmarks.


@router.get("/api/bookmark-file")
def get_bookmark_file(path: str):
    if not os.path.isabs(path):
        return JSONResponse({"error": "path must be absolute"}, status_code=400)
    if not path.lower().endswith(".bookmark"):
        return JSONResponse({"error": "path must end in .bookmark"}, status_code=400)
    if not os.path.isfile(path):
        return JSONResponse({"error": f"no such file: {path}"}, status_code=404)
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return JSONResponse({"error": "file is not valid .bookmark JSON"}, status_code=400)
    version = doc.get("version") if isinstance(doc, dict) else None
    if version != 1 or isinstance(version, bool):
        # Forward-compat: a v2 file from a newer build gets a clear refusal,
        # not a garbled redirect.
        return JSONResponse(
            {"error": f"unsupported .bookmark version: {version!r} (this build reads version 1)"},
            status_code=400,
        )
    return {"dir": os.path.dirname(path), "bookmark": doc}


# ------------------------------------------------------ bookmark history sidecar
#
# A bookmark create/url-update is mirrored into the target file's `<file>.json`
# sidecar under "bookmarkHistory" — the same file the claude chat template keeps
# next to each target (templates/claude/agent.py). This is the file's permanent
# record of every bookmark ever saved for it; delete is a no-op (history stays).


def _decode_fs_path(url: str) -> str | None:
    """Decode a bookmark shell url to the absolute filesystem path it targets,
    WITHOUT checking disk — the decode-only half shared by _fs_path_from_url
    (history sidecar, below) and the missing-file flag (_bookmark_missing,
    above).

    Mirrors frontend router.fsPathFromLocation: strip the /view/ or /embed/
    prefix and query, decode each path segment. Sentinel layout/tab urls
    (`_panel` / `_tab`) resolve to None (nothing to check)."""
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
    # nested deeper is NOT a sentinel and gets checked like any other path.
    if not segments or (len(segments) == 1 and segments[0] in _SENTINELS):
        return None
    joined = "/".join(segments)
    # Mirror the frontend's rootedFsPath (lib/router.ts): a Windows drive-letter
    # path (`C:/...`) is already absolute and keeps its form, a bare drive (`C:`)
    # gets a trailing slash, and every POSIX path gets the leading `/`. Prepending
    # `/` unconditionally would corrupt `C:/...` into `/C:/...` and miss on disk.
    if len(joined) == 2 and joined[0].isalpha() and joined[1] == ":":
        return joined + "/"
    if len(joined) >= 3 and joined[0].isalpha() and joined[1] == ":" and joined[2] == "/":
        return joined
    return "/" + joined


def _fs_path_from_url(url: str) -> str | None:
    """`_decode_fs_path`, additionally requiring the path to actually exist on
    disk right now (a file OR a directory listing — both are bookmarkable).
    Only a path that exists gets a sidecar; missing paths / sentinels no-op."""
    fs_path = _decode_fs_path(url)
    if fs_path is None or not os.path.exists(fs_path):
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
    # The bookmark itself already lives in the global tree (bookmarks.json);
    # this sidecar mirror can't be written when fs_path is inside a read-only
    # remote mount (CacheMode=full would 403-loop the doomed PutObject — the
    # sidecar-write incident). Skip the mirror; the bookmark still works.
    if mounts.mount_read_only(fs_path):
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
