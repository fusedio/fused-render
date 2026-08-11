"""POST /api/search/files — system-wide file search from a validated filter spec.

The AI search on the explorer homepage translates a natural-language query into
a small filter spec CLIENT-side (one /api/ai call); this endpoint is the
execution half, and the app's own DuckDB/parquet index (`fused_render/index`) is
the ONE engine that answers it. The index holds every fact the spec filters on —
name, ext, size, mtime — so the whole spec becomes one SQL query over the `files`
and `dirs` views and rows come back straight from parquet: no `stat`, no walk, no
subprocess, and therefore no syscall a wedged mount could swallow.

There is deliberately NO fallback. Spotlight (`mdfind`) and the bounded home walk
both used to sit behind this endpoint, and both are gone: two engines that answer
the same query differently make the search box's results depend on which one ran,
and the walk in particular re-statted the filesystem on every keystroke-driven
query. The consequences are owned rather than papered over:

  * the index covers the CONFIGURED ROOTS (home by default), so this search is
    no longer whole-disk. A path outside the roots is not findable here.
  * a query that finds nothing returns an empty result — an honest miss, not a
    cue to consult something wider.
  * no index yet, or an index that cannot be read, is an ERROR (503 / 502) with
    a message a search box can show. Reporting it as "no matches" would blame
    the user's files for the app's state.
  * `created_after` / `created_before` are REFUSED: the index stores `mtime` and
    no birth time, and no engine is left that could stat for one. Only a stale
    client can still send them; quietly searching by modification date instead
    would answer a different question than the one asked.

Freshness is deliberately not a gate — a scan in flight keeps serving its last
completed generation, exactly as `query.FRESH_MAX_AGE_S` is informational for the
explorer's in-folder corpus.

SECURITY: the spec arrives from the client but ORIGINATES from a model, so every
field is re-validated here as if hostile, and the SQL is assembled only from
validated values — numbers cast in Python, name terms escaped with
`like_literal`, extensions re-checked against a closed charset at the point of
interpolation. An empty spec is rejected rather than becoming a
match-everything query.
"""

import logging
import os
import re
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Body
from fastapi.concurrency import run_in_threadpool

from fused_render.index.config import load_config
from fused_render.index.query import dirs_src, files_src
from fused_render.index.store import like_literal, read_manifest
from fused_render.server.common import _error
from fused_render.server.gitignore import _IgnoreOracle
from fused_render.server.walk import WALK_IGNORE_DIRS

logger = logging.getLogger(__name__)
router = APIRouter()

# Rows a search may return. A broad query can match tens of thousands of index
# rows; the client shows ~60, so 400 leaves plenty of ranking headroom.
SEARCH_MAX_RESULTS = 400

_EXT_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789")

# Date-range spec fields: (key, is_range_end). Modified only — see the module
# docstring on created_*.
_DATE_FIELDS = (("modified_after", False), ("modified_before", True))
# Creation-date fields a stale client may still send. Named so they can be
# refused explicitly instead of ignored.
_REFUSED_DATE_FIELDS = ("created_after", "created_before")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class IndexUnavailable(RuntimeError):
    """There is no index to search — no manifest, or nothing in it this spec
    could be matched against. Distinct from a query FAILURE so the endpoint can
    say "not ready yet" (503) rather than "broken" (502)."""


def _day_bound_epoch(date_str: str, end: bool) -> float:
    """Epoch seconds of the LOCAL midnight starting `date_str` — or, for a
    range end, the midnight AFTER it, so 'before 2026-08-05' includes all of
    the 5th (dates are inclusive on both sides)."""
    d = date.fromisoformat(date_str)
    if end:
        d = d + timedelta(days=1)
    return datetime(d.year, d.month, d.day).astimezone().timestamp()


def _parse_spec(body: dict):
    """Coerce the request body into a clean spec dict, or raise ValueError.

    Fields the engine does not implement are IGNORED (`path_hints` is
    client-side ranking only), with one exception: the creation-date fields are
    refused outright, because dropping a date filter silently would answer a
    different question than the caller asked."""

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

    # Backslash and double-quote are stripped rather than escaped: neither is
    # meaningful to a substring match on a file name, and removing them keeps
    # every term trivially safe to embed in whatever the engine builds.
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

    for key in _REFUSED_DATE_FIELDS:
        if body.get(key) is not None:
            raise ValueError(
                f"'{key}' is not supported: the file index records modification "
                f"time only. Use 'modified_after'/'modified_before'.")
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
    for key, _end in _DATE_FIELDS:
        spec[key] = date_str(key)
    return spec


# Path segments that mark a hit as machine-managed junk or hidden data. The
# index's own ignore rules are a user-editable name list that says nothing about
# hidden files, so hits are screened with the same standards the explorer's walk
# enforces during traversal: WALK_IGNORE_DIRS segments and dot-segments never
# surface (matching /api/fs/walk's default).
def _junk_path(path: str) -> bool:
    # Both separators: the index stores posix paths (index/ignore.norm) whatever
    # the platform, and one screening standard has to cover both spellings.
    for seg in re.split(r"[/\\]", path):
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


# ------------------------------------------------------------ the index engine

# Spec fields that NARROW a query. `kind` is deliberately absent: "folders"
# still matches half the disk, so it is not enough on its own.
_NARROWING_KEYS = (
    "modified_within_days", "modified_after", "modified_before",
    "min_size_bytes", "max_size_bytes",
)


def _has_narrowing(spec) -> bool:
    return bool(spec["name_terms"] or spec["extensions"]) or any(
        spec[k] is not None for k in _NARROWING_KEYS)


def _dir_narrowing(spec) -> bool:
    """Whether anything in the spec narrows DIRECTORIES.

    Extension and size are file facts a dirs row cannot answer, so an
    extensions-only spec ("find my mp4s") narrows the files view and nothing
    else — and a dirs branch with no predicate left would return every folder in
    the index, spending the result cap on rows that answer nothing."""
    return bool(spec["name_terms"]) or any(
        spec[k] is not None for k in
        ("modified_within_days", "modified_after", "modified_before"))


def _index_where(spec, *, path_expr, name_expr, mtime_expr, now_s,
                 ext_expr=None, size_expr=None) -> str:
    """The WHERE clause for one index view. `ext_expr`/`size_expr` are None for
    the dirs view: extension and size are FILE facts, and the dirs table has no
    size column at all.

    Every value reaching SQL here is either a number cast in Python or a name
    term escaped with `like_literal` (quotes doubled, LIKE metachars escaped) —
    the model-originated half of the spec never lands in the statement raw. See
    the module docstring's SECURITY note."""
    # Dot segments never surface (parity with _junk_path and the explorer's
    # walk). Applied in SQL as well as in _junk_path so the result cap is not
    # spent on rows from ~/.cache that would be dropped a moment later;
    # _junk_path stays the single standard, this is only a budget prefilter.
    pieces = [f"{path_expr} NOT LIKE '%/.%'"]
    if spec["name_terms"]:
        # Substring, case-insensitive, OR'd across terms: the model lists
        # synonyms, and requiring all of them would over-filter. Recall matters
        # more than precision — the client re-ranks these hits fuzzily.
        pieces.append("(" + " OR ".join(
            f"{name_expr} ILIKE '%{like_literal(t)}%' ESCAPE '\\'"
            for t in spec["name_terms"]) + ")")
    if ext_expr is not None and spec["extensions"]:
        # clean_ext restricts these to [a-z0-9]{1,12}; re-checked here so the
        # literal is provably quote-free however this spec was built.
        exts = [e for e in spec["extensions"] if set(e) <= _EXT_ALLOWED]
        if exts:
            pieces.append(f"lower({ext_expr}) IN ("
                          + ",".join(f"'{e}'" for e in exts) + ")")
    if spec["modified_within_days"] is not None:
        cutoff = now_s - spec["modified_within_days"] * 86400
        pieces.append(f"{mtime_expr} >= {float(cutoff)!r}")
    # Local-day bounds, inclusive on both ends: `before 2026-08-05` runs to the
    # midnight AFTER the 5th.
    if spec["modified_after"] is not None:
        pieces.append(f"{mtime_expr} >= "
                      f"{_day_bound_epoch(spec['modified_after'], False)!r}")
    if spec["modified_before"] is not None:
        pieces.append(f"{mtime_expr} < "
                      f"{_day_bound_epoch(spec['modified_before'], True)!r}")
    if size_expr is not None:
        if spec["min_size_bytes"] is not None:
            pieces.append(f"{size_expr} >= {int(spec['min_size_bytes'])}")
        if spec["max_size_bytes"] is not None:
            pieces.append(f"{size_expr} <= {int(spec['max_size_bytes'])}")
    return " AND ".join(pieces)


def _index_rows(cfg, spec, limit, *, parts=None, dirs=False):
    """The spec as ONE query over the requested index views, newest first.

    Both views are named through the MANIFEST (query.files_src / dirs_src),
    never a glob of the files dir: a compaction leaves the previous generation's
    partitions on disk for readers still holding the old manifest
    (index-store.md §4), so a glob would return every row twice."""
    import duckdb

    now_s = time.time()
    branches = []
    if parts:
        fsrc = files_src(cfg, parts)
        branches.append(
            f"SELECT path, size, mtime, false AS is_dir FROM {fsrc} WHERE "
            + _index_where(spec, path_expr="path", name_expr="name",
                           mtime_expr="mtime", ext_expr="ext",
                           size_expr="size", now_s=now_s))
    if dirs:
        dsrc = dirs_src(cfg)
        # A dirs row carries no name column, so the final path component is the
        # name; mtime_ns of 0 means "unknown" and becomes NULL, which fails
        # every mtime comparison rather than passing it.
        dmtime = "nullif(mtime_ns, 0) / 1e9"
        branches.append(
            f"SELECT dir AS path, CAST(NULL AS BIGINT) AS size, "
            f"{dmtime} AS mtime, true AS is_dir FROM {dsrc} WHERE "
            + _index_where(spec, path_expr="dir",
                           name_expr="regexp_extract(dir, '[^/]*$')",
                           mtime_expr=dmtime, now_s=now_s))
    # One row past the cap, so "there was more" is known without a count.
    # Newest first: the client re-ranks every hit anyway, so ordering only
    # decides WHICH rows survive the cap, and recency is the best sample.
    return duckdb.connect().execute(
        " UNION ALL ".join(branches)
        + f" ORDER BY mtime DESC LIMIT {int(limit)}").fetchall()


def _search_index(spec, cfg=None):
    """The spec executed against the SQL index — the only engine.

    Rows are returned straight from parquet, deliberately NOT re-statted: that
    is the point of this engine (zero filesystem syscalls, so a search can never
    touch a mount). The cost is that a file deleted since the last scan can
    still appear, the same staleness the index-backed in-folder search accepts.

    Raises, rather than quietly answering something narrower:
      * `ValueError` — the spec narrows nothing (a match-everything query is
        never intended), or it asks only for directories while narrowing only
        file facts (see _dir_narrowing).
      * `IndexUnavailable` — no manifest, or no view this `kind` could be
        answered from. "Not ready", not "no matches".
      * `RuntimeError` — the index is there but could not be read.
    """
    if not _has_narrowing(spec):
        raise ValueError("spec has no narrowing constraints")
    cfg = load_config() if cfg is None else cfg
    manifest = read_manifest(cfg)
    if manifest is None:
        raise IndexUnavailable(
            "the file index has not been built yet — search works once the "
            "first scan finishes")
    parts = (manifest.get("partitions") or []) if spec["kind"] != "dir" else []
    dirs = spec["kind"] in ("dir", "any") and os.path.exists(cfg.dirs_parquet)
    if dirs and not _dir_narrowing(spec):
        # kind "any" simply drops the dirs half; a folder-only search with
        # nothing a folder can be matched by has no answer to give, and there is
        # no wider engine to pass it to.
        if spec["kind"] == "dir":
            raise ValueError(
                "searching for folders needs a name or a date — extension and "
                "size only narrow files")
        dirs = False
    if not parts and not dirs:
        raise IndexUnavailable(
            "the file index holds nothing this search could match yet — it may "
            "still be scanning")
    cap = SEARCH_MAX_RESULTS
    try:
        rows = _index_rows(cfg, spec, cap + 1, parts=parts, dirs=dirs)
    except Exception as e:  # noqa: BLE001 - duckdb's exception tree, flattened
        logger.exception("the index search query failed")
        raise RuntimeError(
            f"the file index could not be searched: {type(e).__name__}") from e
    truncated = len(rows) > cap
    entries = [
        {"path": path, "is_dir": bool(is_dir),
         "size": None if is_dir or size is None else int(size),
         "mtime": None if mtime is None else float(mtime)}
        for path, size, mtime, is_dir in rows[:cap]
        if not _junk_path(path)
    ]
    # The index knows nothing about git (its ignore rules are name patterns), so
    # gitignored hits are screened here — the same oracle the explorer's walk
    # uses, and the same reason server/index_gitignore.py filters the in-folder
    # corpus: a build directory's 100k generated files must not flood a search.
    return {"entries": _drop_gitignored(entries), "truncated": truncated,
            # Constant now that the index is the only engine. Kept in the
            # response because old clients read it, and because a support
            # question about a surprising result starts with "what answered it".
            "engine": "index"}


@router.post("/api/search/files")
async def api_search_files(body: dict = Body(...)):
    try:
        spec = _parse_spec(body)
    except ValueError as e:
        return _error(str(e))
    # duckdb blocks, so it runs off the event loop. Every failure is REPORTED:
    # there is no second engine to degrade to, and a search box that says
    # "no matches" when the index is missing would be lying about the disk.
    try:
        result = await run_in_threadpool(_search_index, spec)
    except ValueError as e:
        return _error(str(e))
    except IndexUnavailable as e:
        return _error(str(e), status=503)
    except RuntimeError as e:
        return _error(str(e), status=502)
    return {"ok": True, **result}
