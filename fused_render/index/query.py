"""Reading the index — strictly read-only, and strictly parameterized.

Ported from OpenIndex's `query.py` MINUS its `sql` action. Inside a local
trusted page, handing duckdb a user's statement was consistent with runPython
executing arbitrary local Python anyway; behind an HTTP route it is an
arbitrary read/write surface with no allowlist and no read-only flag, so it is
gone rather than guarded. What remains — `stats` and `lookup` — builds SQL only
from escaped literals, an int-cast limit/offset and a fixed sort allowlist.

duckdb is imported inside each function, not at module top: this module is
imported by the server's router, and a call against a missing index should not
pay a duckdb import.

See specs/query.md.
"""
import os
import re

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import norm
from fused_render.index.store import read_manifest

_DRIVE = re.compile(r"^[A-Za-z]:/")

SORTS = {
    "path": "path ASC", "size": "size DESC", "mtime": "mtime DESC",
    "name": "name ASC",
}

# Rows a single lookup may return, whatever the caller asks for.
MAX_LIMIT = 5_000

# Entries an in-folder corpus may return — the same cap /api/fs/walk uses, so
# swapping the corpus source cannot change how much the client holds.
MAX_CORPUS = 200_000

# How old the index may be and still answer an in-folder search. There is no
# watcher (`scan.md`), so this is the honest bound on how wrong the corpus can
# be: past it, the explorer falls back to the live walk. It is a trade, not a
# fact about the data — long enough that the index is actually used during a
# working session, short enough that a morning's edits don't answer an
# afternoon's search.
FRESH_MAX_AGE_S = 3600.0


def _q(s: str) -> str:
    """A SQL string literal's contents (single quotes doubled)."""
    return s.replace("'", "''")


def prune(parts, prefix):
    """Partitions whose [min,max] path range can contain paths starting with
    `prefix` (paths are globally sorted, so the range test is exact, not a
    heuristic)."""
    if not prefix:
        return list(parts)
    hi = prefix + "￿"
    return [p for p in parts
            if p.get("min") is not None and p["max"] >= prefix and p["min"] <= hi]


def pattern_for(q: str):
    """Turn a user query into (like_pattern, prune_prefix).

    `*` is a wildcard; everything else is a literal substring match anywhere
    in the path. A query starting with `/`, a drive letter or `~` is anchored
    at the start, and its literal lead-in (up to the first `*`) prunes
    partitions."""
    q = norm(q)
    anchored = q.startswith("/") or q.startswith("~") or bool(_DRIVE.match(q))
    if q.startswith("~"):
        q = norm(os.path.expanduser(q))
    lit = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pat = lit.replace("*", "%")
    if not anchored:
        pat = "%" + pat
    if not pat.endswith("%"):
        pat += "%"
    prune_prefix = q.split("*", 1)[0] if anchored else ""
    return pat, prune_prefix


def _has_partitions(cfg: IndexConfig) -> bool:
    return os.path.isdir(cfg.files_dir) and any(
        f.endswith(".parquet") for f in os.listdir(cfg.files_dir))


def _sources(cfg: IndexConfig, parts) -> str:
    files = [_q(os.path.join(cfg.files_dir, p["file"])) for p in parts]
    return "read_parquet([" + ",".join(f"'{f}'" for f in files) + "])"


def lookup(cfg: IndexConfig, query: str = "", limit: int = 100, offset: int = 0,
           sort: str = "mtime") -> dict:
    """Files whose path matches `query`, with partition-pruning telemetry.
    An index that was never built answers `{empty: True}` rather than raising —
    "no index yet" is a state the UI renders, not an error."""
    m = read_manifest(cfg)
    if m is None:
        return {"empty": True, "location": cfg.dir, "rows": [], "total": 0,
                "partitions": [], "scanned_partitions": 0, "of_partitions": 0}
    import duckdb

    q = (query or "").strip().rstrip("/")
    pat, prune_prefix = pattern_for(q) if q else ("%", "")
    hit = prune(m["partitions"], prune_prefix)
    base = {"empty": False, "location": cfg.dir,
            "partitions": [p["file"] for p in hit],
            "scanned_partitions": len(hit),
            "of_partitions": len(m["partitions"])}
    if not hit:
        return {**base, "rows": [], "total": 0}
    where = (f"WHERE path ILIKE '{_q(pat)}' ESCAPE '\\'" if q else "WHERE 1=1")
    order = SORTS.get(sort, SORTS["mtime"])
    limit = max(0, min(int(limit), MAX_LIMIT))
    offset = max(0, int(offset))
    src = _sources(cfg, hit)
    con = duckdb.connect()
    total = con.execute(f"SELECT count(*) FROM {src} {where}").fetchone()[0]
    rows = con.execute(
        f"SELECT path, dir, name, ext, size, mtime FROM {src} {where} "
        f"ORDER BY {order} LIMIT {limit} OFFSET {offset}").fetchall()
    cols = ["path", "dir", "name", "ext", "size", "mtime"]
    return {**base, "rows": [dict(zip(cols, r)) for r in rows],
            "total": int(total)}


def stats(cfg: IndexConfig, root: str = "") -> dict:
    """Totals + per-extension breakdown for ONE subtree — the explicit `root`,
    else the manifest's `last_root`. An index may hold several roots, so a
    whole-index total would be a number nobody asked for.

    Known cost (inherited): this reads every partition to group by extension;
    there is no cached rollup."""
    m = read_manifest(cfg)
    if m is None:
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}
    import duckdb

    con = duckdb.connect()
    root = norm(os.path.expanduser(root.strip())) if root.strip() else ""
    root = (root or m.get("last_root") or "").rstrip("/") or "/"
    pfx = (_q(root) + "/") if root != "/" else "/"
    inside = f"(dir = '{_q(root)}' OR dir LIKE '{pfx}%')"
    n_rows, total_size, n_dirs = 0, 0, 0
    types = []
    if os.path.exists(cfg.dirs_parquet):
        n_dirs = con.execute(
            f"SELECT count(*) FROM read_parquet('{_q(cfg.dirs_parquet)}') "
            f"WHERE {inside}").fetchone()[0]
    if _has_partitions(cfg):
        by_ext = con.execute(
            f"SELECT coalesce(nullif(ext, ''), 'no ext') e, count(*) n, "
            f"coalesce(sum(size), 0) s "
            f"FROM read_parquet('{_q(cfg.files_dir)}/*.parquet') "
            f"WHERE {inside} "
            f"GROUP BY 1 ORDER BY s DESC").fetchall()
        n_rows = sum(r[1] for r in by_ext)
        total_size = sum(r[2] for r in by_ext)
        top, rest = by_ext[:50], by_ext[50:]
        types = [{"ext": e, "n": int(n), "size": int(sz)} for e, n, sz in top]
        if rest:
            types.append({"ext": "other", "n": int(sum(r[1] for r in rest)),
                          "size": int(sum(r[2] for r in rest))})
    return {"empty": False, "location": cfg.dir, "rows": int(n_rows),
            "dirs": int(n_dirs), "total_size": int(total_size),
            "updated": m.get("updated"), "last_root": root,
            "partitions": m["partitions"], "types": types}


def search_under(cfg: IndexConfig, root: str, q: str = "", limit: int = MAX_CORPUS,
                 include_dirs: bool = True) -> dict:
    """The explorer's in-folder corpus for `root`, from the index.

    Returns entries in exactly the shape /api/fs/walk streams — `rel` (posix,
    relative to `root`), `is_dir`, `size`, `mtime` — so the client's fuzzy
    scoring, throttles and paging are untouched by where the corpus came from.

    `covered` says the index has actually visited this root; `fresh` says the
    last compaction is recent enough to answer with (FRESH_MAX_AGE_S). Both
    are false for a never-built index, and the caller treats every false the
    same way: fall back to the live walk, silently. "No index yet", "not
    covered" and "a scan is running" are one condition to a search box.

    `q` is an OPTIONAL server-side substring filter. The explorer does not use
    it — it wants the whole corpus, so client-side fuzzy matching stays
    subsequence-based rather than being pre-narrowed to substrings — but it
    keeps the endpoint useful for a caller that only wants the hits.
    """
    root = norm(os.path.abspath(os.path.expanduser((root or "").strip()))).rstrip("/")
    m = read_manifest(cfg)
    empty = {"covered": False, "fresh": False, "updated": None, "age_s": None,
             "root": root, "entries": [], "truncated": False, "total": 0,
             "scanned_partitions": 0,
             "of_partitions": len(((m or {}).get("partitions")) or [])}
    if m is None or not root or not os.path.exists(cfg.dirs_parquet):
        return empty
    import time

    import duckdb

    updated = m.get("updated")
    age = (time.time() - updated) if isinstance(updated, (int, float)) else None
    fresh = age is not None and age <= FRESH_MAX_AGE_S
    con = duckdb.connect()
    # Coverage is "the scan visited this exact directory", which is what keeps
    # a partial index honest: a root whose parent was scanned but which was
    # itself pruned (ignored, or below a cancelled run's frontier) has no row.
    covered = con.execute(
        f"SELECT count(*) FROM read_parquet('{_q(cfg.dirs_parquet)}') "
        f"WHERE dir = '{_q(root)}'").fetchone()[0] > 0
    if not covered:
        return {**empty, "updated": updated, "age_s": age}
    prefix = (root + "/") if root != "/" else "/"
    limit = max(0, min(int(limit), MAX_CORPUS))
    hit = prune(m["partitions"], prefix)
    like = f" AND path ILIKE '%{_q(q.strip())}%'" if q and q.strip() else ""
    entries, truncated = [], False
    if hit and limit:
        # One row past the cap, so "there was more" is known without a count.
        rows = con.execute(
            f"SELECT path, size, mtime FROM {_sources(cfg, hit)} "
            f"WHERE path LIKE '{_q(prefix)}%'{like} "
            f"ORDER BY path LIMIT {limit + 1}").fetchall()
        for path, size, mtime in rows[:limit]:
            entries.append({"rel": path[len(prefix):], "is_dir": False,
                            "size": int(size) if size is not None else None,
                            "mtime": float(mtime) if mtime is not None else None})
        truncated = len(rows) > limit
    if include_dirs and len(entries) < limit:
        dlike = f" AND dir ILIKE '%{_q(q.strip())}%'" if q and q.strip() else ""
        room = limit - len(entries)
        drows = con.execute(
            f"SELECT dir, mtime_ns FROM read_parquet('{_q(cfg.dirs_parquet)}') "
            f"WHERE dir LIKE '{_q(prefix)}%'{dlike} "
            f"ORDER BY dir LIMIT {room + 1}").fetchall()
        for d, mtime_ns in drows[:room]:
            entries.append({"rel": d[len(prefix):], "is_dir": True, "size": None,
                            "mtime": (mtime_ns / 1e9) if mtime_ns else None})
        truncated = truncated or len(drows) > room
    return {"covered": True, "fresh": fresh, "updated": updated, "age_s": age,
            "root": root, "scanned_partitions": len(hit),
            "of_partitions": len(m["partitions"]), "entries": entries,
            "truncated": truncated, "total": len(entries)}
