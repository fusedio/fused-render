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
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool

from fused_render._view_url_codec import canonical_fs_path
from fused_render.server.common import _error
from fused_render.server.gitignore import _IgnoreOracle
from fused_render.server.walk import _WALK_TRUNCATED, _walk_bfs, WALK_IGNORE_DIRS

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

# Date-range spec fields: (key, mdfind attribute, is_range_end). Modified uses
# the fs content-change date, created the fs creation date (birthtime).
_DATE_FIELDS = (
    ("modified_after", "kMDItemFSContentChangeDate", False),
    ("modified_before", "kMDItemFSContentChangeDate", True),
    ("created_after", "kMDItemFSCreationDate", False),
    ("created_before", "kMDItemFSCreationDate", True),
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_bound_epoch(date_str: str, end: bool) -> float:
    """Epoch seconds of the LOCAL midnight starting `date_str` — or, for a
    range end, the midnight AFTER it, so 'before 2026-08-05' includes all of
    the 5th (dates are inclusive on both sides)."""
    d = date.fromisoformat(date_str)
    if end:
        d = d + timedelta(days=1)
    return datetime(d.year, d.month, d.day).astimezone().timestamp()


def _time_iso(epoch: float) -> str:
    """The mdfind $time.iso(...) literal for an epoch, in local offset form
    (probed: mdfind accepts 2026-08-05T00:00:00+05:30)."""
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


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

    def date_str(key):
        v = body.get(key)
        if v is None:
            return None
        if not isinstance(v, str) or not _DATE_RE.match(v):
            raise ValueError(f"'{key}' must be a YYYY-MM-DD date or null")
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"'{key}' is not a real date")
        return v

    kind = body.get("kind", "any")
    if kind not in ("file", "dir", "any"):
        raise ValueError("'kind' must be 'file', 'dir', or 'any'")
    spec = {
        "name_terms": strings("name_terms", 8, clean_term),
        "extensions": strings("extensions", 8, clean_ext),
        "kind": kind,
        # Legacy field, still honored: older clients (and a model told about
        # both shapes) may send it. Ranges are the primary form.
        "modified_within_days": pos_num("modified_within_days"),
        "min_size_bytes": pos_num("min_size_bytes"),
        "max_size_bytes": pos_num("max_size_bytes"),
    }
    for key, _attr, _end in _DATE_FIELDS:
        spec[key] = date_str(key)
    return spec


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
    for key, attr, end in _DATE_FIELDS:
        if spec[key] is not None:
            epoch = _day_bound_epoch(spec[key], end)
            op = "<" if end else ">="
            pieces.append(f"{attr} {op} $time.iso({_time_iso(epoch)})")
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


# Path segments that mark a hit as machine-managed junk or hidden data.
# Spotlight has no gitignore/hidden notion, so its hits are re-screened with
# the same standards the walk enforces during traversal: WALK_IGNORE_DIRS
# segments and dot-segments never surface (matching /api/fs/walk's default).
def _junk_path(path: str) -> bool:
    for seg in path.split(os.sep):
        if seg in WALK_IGNORE_DIRS:
            return True
        if seg.startswith(".") and seg not in (".", ".."):
            return True
    return False


def _nearest_repo(dirpath: str, memo: dict) -> str | None:
    """The closest ancestor (including `dirpath`) containing a `.git` marker,
    or None. Pure filesystem probes with memoization — a `git rev-parse` per
    unique hit directory would cost a subprocess each."""
    probe = dirpath
    chain = []
    result = None
    while True:
        if probe in memo:
            result = memo[probe]
            break
        chain.append(probe)
        if os.path.exists(os.path.join(probe, ".git")):
            result = probe
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    for p in chain:
        memo[p] = result
    return result


def _drop_gitignored(entries):
    """Filter out entries a containing repo's own gitignore rules ignore,
    batched through one check-ignore co-process per repo (_IgnoreOracle)."""
    repo_memo: dict = {}
    by_repo: dict = {}
    for i, e in enumerate(entries):
        repo = _nearest_repo(os.path.dirname(e["path"]), repo_memo)
        if repo is not None:
            by_repo.setdefault(repo, []).append(i)
    dropped = set()
    for repo, indices in by_repo.items():
        oracle = _IgnoreOracle(repo)
        try:
            rels = [os.path.relpath(entries[i]["path"], repo).replace(os.sep, "/")
                    for i in indices]
            ignored = oracle.ignored(rels)
            for i, rel in zip(indices, rels):
                if rel in ignored:
                    dropped.add(i)
        finally:
            oracle.close()
    return [e for i, e in enumerate(entries) if i not in dropped]


def _search_mdfind(spec):
    query = _mdfind_query(spec)
    if query is None:
        raise ValueError("spec has no narrowing constraints")
    # mdfind has been seen to die with SIGSEGV depending on the launch
    # context; a signal death (negative returncode) gets one retry before
    # the caller falls back to the walk engine.
    for attempt in (1, 2):
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
        if proc.returncode >= 0 or attempt == 2:
            break
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"mdfind failed: {err or proc.returncode}")
    paths = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    kept = [p for p in paths if not _junk_path(p)]
    truncated = len(kept) > SEARCH_MAX_RESULTS
    entries = []
    for p in kept[:SEARCH_MAX_RESULTS]:
        e = _stat_entry(p)
        if e is not None:
            entries.append(e)
    entries = _drop_gitignored(entries)
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
    if spec["modified_after"] is not None:
        if entry["mtime"] is None or entry["mtime"] < _day_bound_epoch(spec["modified_after"], False):
            return False
    if spec["modified_before"] is not None:
        if entry["mtime"] is None or entry["mtime"] >= _day_bound_epoch(spec["modified_before"], True):
            return False
    if not entry["is_dir"]:
        size = entry["size"] or 0
        if spec["min_size_bytes"] is not None and size < spec["min_size_bytes"]:
            return False
        if spec["max_size_bytes"] is not None and size > spec["max_size_bytes"]:
            return False
    return True


def _created_epoch(path: str) -> float | None:
    """Best-effort creation time: st_birthtime where the OS has one (macOS,
    some BSDs), st_ctime on Windows (creation there), else None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return birth
    return st.st_ctime if sys.platform == "win32" else None


def _match_created_range(path: str, spec) -> bool:
    """Created-range check for the walk engine (walk entries carry no
    creation time, so it costs a stat — only paid when the spec asks). On a
    platform with no creation time the filter is skipped rather than
    silently emptying every search."""
    if spec["created_after"] is None and spec["created_before"] is None:
        return True
    created = _created_epoch(path)
    if created is None:
        return True
    if spec["created_after"] is not None:
        if created < _day_bound_epoch(spec["created_after"], False):
            return False
    if spec["created_before"] is not None:
        if created >= _day_bound_epoch(spec["created_before"], True):
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
        abs_path = os.path.join(home, entry["rel"].replace("/", os.sep))
        if not _match_created_range(abs_path, spec):
            continue
        entries.append(
            {
                # Canonicalized (forward slashes) for the response: the
                # client strips `home` with a "/" join and its path helpers
                # are forward-slash-only, matching every other fs path the
                # runtime hands them. `abs_path` above stays native for the
                # stat call.
                "path": canonical_fs_path(abs_path),
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
    # A Spotlight failure (crash, timeout, missing mdfind) degrades to the
    # home-walk engine rather than a 502 — narrower coverage, labeled via
    # `engine`, beats a dead search box.
    try:
        if sys.platform == "darwin":
            try:
                result = await run_in_threadpool(_search_mdfind, spec)
            except RuntimeError:
                result = await run_in_threadpool(_search_walk_home, spec)
        else:
            result = await run_in_threadpool(_search_walk_home, spec)
    except ValueError as e:
        return _error(str(e))
    except RuntimeError as e:
        return _error(str(e), status=502)
    return {"ok": True, **result}
