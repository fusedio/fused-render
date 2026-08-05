"""POST /api/search/files — system-wide file search from a validated filter spec.

The AI search on the explorer homepage translates a natural-language query
into a small filter spec CLIENT-side (one /api/ai call); this endpoint is the
execution half. On macOS it queries Spotlight (`mdfind`) — the only engine
with a whole-disk index, so "video downloaded today" can find ~/Downloads
without walking the filesystem. Elsewhere it falls back to the bounded
home-dir walk (_walk_bfs), which is honest about its narrower coverage via
`engine` in the response.

SECURITY: the spec arrives from the client but ORIGINATES from a model, so
every field is re-validated here as if hostile. The mdfind query string is
assembled only from validated values — extensions/kind against closed
charsets, numbers coerced, and name terms stripped of the two characters
(backslash, double-quote) that could break out of an mdfind string literal.
mdfind runs argv-style (no shell), and an empty spec is rejected rather than
becoming a match-everything query.
"""

import os
import subprocess
import sys
import time

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool

from fused_render.server.common import _error
from fused_render.server.walk import _WALK_TRUNCATED, _walk_bfs

router = APIRouter()

# Result and runtime bounds. Spotlight can return tens of thousands of paths
# for a broad query; the client shows ~60, so 400 leaves plenty of ranking
# headroom without stat()ing the world.
SEARCH_MAX_RESULTS = 400
SEARCH_MDFIND_TIMEOUT_S = 15.0
# Fallback walk caps: the home dir is bigger than a workspace, so the walk
# uses the same entry cap as /api/fs/walk but a shallower depth — deep hits
# are unlikely search targets and shallow-first coverage matters more.
SEARCH_WALK_MAX_ENTRIES = 200_000
SEARCH_WALK_MAX_DEPTH = 12

_EXT_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789")


def _parse_spec(body: dict):
    """Coerce the request body into a clean spec dict, or an error string."""

    def strings(key, limit, clean):
        raw = body.get(key)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError(f"'{key}' must be an array of strings")
        out = []
        for v in raw:
            if not isinstance(v, str):
                raise ValueError(f"'{key}' must be an array of strings")
            v = clean(v)
            if v:
                out.append(v)
        return out[:limit]

    def pos_num(key):
        v = body.get(key)
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError(f"'{key}' must be a positive number or null")
        return float(v)

    # An mdfind string literal is "..."; only backslash and the quote itself
    # can escape it, so stripping those two makes any term safe to embed.
    def clean_term(v):
        return v.replace("\\", "").replace('"', "").strip()

    def clean_ext(v):
        v = v.strip().lstrip(".").lower()
        return v if 0 < len(v) <= 12 and set(v) <= _EXT_ALLOWED else ""

    kind = body.get("kind", "any")
    if kind not in ("file", "dir", "any"):
        raise ValueError("'kind' must be 'file', 'dir', or 'any'")
    return {
        "name_terms": strings("name_terms", 8, clean_term),
        "extensions": strings("extensions", 8, clean_ext),
        "kind": kind,
        "modified_within_days": pos_num("modified_within_days"),
        "min_size_bytes": pos_num("min_size_bytes"),
        "max_size_bytes": pos_num("max_size_bytes"),
    }


def _mdfind_query(spec) -> str | None:
    """The Spotlight query for a spec, or None when the spec has no
    constraints at all (a match-everything query is never intended)."""
    pieces = []
    if spec["name_terms"]:
        # cd = case- and diacritic-insensitive. OR across terms: the model
        # lists synonyms, and requiring all of them would over-filter.
        terms = " || ".join(
            f'kMDItemFSName = "*{t}*"cd' for t in spec["name_terms"]
        )
        pieces.append(f"({terms})")
    if spec["extensions"]:
        exts = " || ".join(
            f'kMDItemFSName = "*.{e}"cd' for e in spec["extensions"]
        )
        pieces.append(f"({exts})")
    if spec["kind"] == "dir":
        pieces.append('kMDItemContentType == "public.folder"')
    elif spec["kind"] == "file":
        pieces.append('kMDItemContentType != "public.folder"')
    if spec["modified_within_days"] is not None:
        secs = int(spec["modified_within_days"] * 86400)
        pieces.append(f"kMDItemFSContentChangeDate >= $time.now(-{secs})")
    if spec["min_size_bytes"] is not None:
        pieces.append(f"kMDItemFSSize >= {int(spec['min_size_bytes'])}")
    if spec["max_size_bytes"] is not None:
        pieces.append(f"kMDItemFSSize <= {int(spec['max_size_bytes'])}")
    # A kind-only query ("folders") still matches half the disk; require at
    # least one NARROWING constraint beyond kind.
    narrowing = [p for p in pieces if not p.startswith("kMDItemContentType")]
    if not narrowing:
        return None
    return " && ".join(pieces)


def _stat_entry(path):
    """The response entry for one hit, or None when it can't be statted
    (Spotlight's index can be ahead of the filesystem)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    is_dir = os.path.isdir(path)
    return {
        "path": path,
        "is_dir": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
    }


def _search_mdfind(spec):
    query = _mdfind_query(spec)
    if query is None:
        raise ValueError("spec has no narrowing constraints")
    try:
        proc = subprocess.run(
            ["mdfind", "-0", query],
            capture_output=True,
            timeout=SEARCH_MDFIND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Spotlight search timed out")
    except FileNotFoundError:
        raise RuntimeError("mdfind not available")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"mdfind failed: {err or proc.returncode}")
    paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    truncated = len(paths) > SEARCH_MAX_RESULTS
    entries = []
    for p in paths[:SEARCH_MAX_RESULTS]:
        e = _stat_entry(p)
        if e is not None:
            entries.append(e)
    return {"entries": entries, "truncated": truncated, "engine": "spotlight"}


def _match_walk_entry(entry, spec, now_s):
    """The fallback walk applies the spec's HARD filters server-side; name
    terms stay client-side (the client ranks fuzzily either way)."""
    if spec["kind"] == "file" and entry["is_dir"]:
        return False
    if spec["kind"] == "dir" and not entry["is_dir"]:
        return False
    if not entry["is_dir"] and spec["extensions"]:
        ext = entry["rel"].rsplit(".", 1)
        if len(ext) != 2 or ext[1].lower() not in spec["extensions"]:
            return False
    if spec["modified_within_days"] is not None:
        cutoff = now_s - spec["modified_within_days"] * 86400
        if entry["mtime"] is None or entry["mtime"] < cutoff:
            return False
    if not entry["is_dir"]:
        size = entry["size"] or 0
        if spec["min_size_bytes"] is not None and size < spec["min_size_bytes"]:
            return False
        if spec["max_size_bytes"] is not None and size > spec["max_size_bytes"]:
            return False
    return True


def _search_walk_home(spec):
    home = os.path.expanduser("~")
    now_s = time.time()
    entries = []
    truncated = False
    walker = _walk_bfs(
        home,
        False,
        max_entries=SEARCH_WALK_MAX_ENTRIES,
        max_depth=SEARCH_WALK_MAX_DEPTH,
    )
    for entry in walker:
        if entry is _WALK_TRUNCATED:
            truncated = True
            continue
        if not _match_walk_entry(entry, spec, now_s):
            continue
        entries.append(
            {
                "path": os.path.join(home, entry["rel"].replace("/", os.sep)),
                "is_dir": entry["is_dir"],
                "size": entry["size"],
                "mtime": entry["mtime"],
            }
        )
        if len(entries) >= SEARCH_MAX_RESULTS:
            truncated = True
            break
    return {"entries": entries, "truncated": truncated, "engine": "walk"}


@router.post("/api/search/files")
async def api_search_files(body: dict = Body(...)):
    try:
        spec = _parse_spec(body)
    except ValueError as e:
        return _error(str(e))
    # Both engines block (subprocess / disk walk); keep the event loop free.
    try:
        if sys.platform == "darwin":
            result = await run_in_threadpool(_search_mdfind, spec)
        else:
            result = await run_in_threadpool(_search_walk_home, spec)
    except ValueError as e:
        return _error(str(e))
    except RuntimeError as e:
        return _error(str(e), status=502)
    return {"ok": True, **result}
