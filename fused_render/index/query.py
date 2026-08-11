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
from fused_render.index.ignore import is_inside_leaf_dir, is_leaf_dir, norm
from fused_render.index.store import (
    depth_expr,
    like_literal,
    parquet_src,
    read_manifest,
)

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

# The age past which `fresh` goes false on an in-folder search. INFORMATIONAL:
# nothing refuses a corpus for being stale. `covered` is the whole gate, on
# purpose (frontend index-corpus.ts) — a rescan keeps serving its last
# completed generation and the search box says "indexing…" meanwhile, so an
# instant mostly-right answer with a visible caveat beats re-walking the tree.
# Changes made THROUGH the app are the exception and do drop the folder to a
# live walk, since offering the pre-rename name back to the user who just
# renamed it is not a trade (frontend lib/index-freshness.ts).
#
# There is no watcher (`scan.md`) — opening a folder whose own mtime is ahead of
# the index rescans its root (`scan-incremental.md §5`), but a change deeper than
# the folder being viewed does not move that mtime — so this is still the honest
# bound on how wrong an unflagged corpus can be. It is a trade, not a
# fact about the data — long enough that the index is actually used during a
# working session, short enough that a morning's edits don't answer an
# afternoon's search.
FRESH_MAX_AGE_S = 3600.0


def _q(s: str) -> str:
    """A SQL string literal's contents (single quotes doubled)."""
    return s.replace("'", "''")


def dirs_src(cfg: IndexConfig) -> str:
    """dirs.parquet as an explicit one-file list, never a glob string — the
    store path is the user's, and DuckDB's glob has no escape for a `[` in
    it (store.parquet_src)."""
    return parquet_src([cfg.dirs_parquet])


def prune(parts, prefix):
    """Partitions whose path range can contain paths starting with `prefix`.

    Folded, because the match this gates is ILIKE: comparing the prefix
    byte-wise ruled out every /Users/... partition for a query typed
    /users/..., so the anchored query found nothing while the unanchored one
    found it. The folded bounds are a SEPARATE aggregate written at
    compaction, not lower(min)/lower(max): byte order and case-folded order
    disagree, so a partition can hold a folded-smaller path than its
    byte-wise minimum.

    A partition written before those bounds existed keeps the old byte-wise
    test — the status quo for data already on disk, not a new hole. Every
    compaction rewrites the manifest, so the first scan after an upgrade
    gives every partition bounds."""
    if not prefix:
        return list(parts)
    hi = prefix + "￿"
    lo_f, hi_f = prefix.lower(), hi.lower()
    out = []
    for p in parts:
        if p.get("min") is None:
            continue
        if p["max"] >= prefix and p["min"] <= hi:
            out.append(p)  # byte-exact hit, whatever the folded bounds say
        elif p.get("min_lower") is not None and (
                p["max_lower"] >= lo_f and p["min_lower"] <= hi_f):
            out.append(p)
    return out


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


def _sources(cfg: IndexConfig, parts) -> str:
    """A duckdb source over exactly the partitions the MANIFEST names.

    Never a `files/*.parquet` glob: a compaction writes the next generation
    into the same directory (index-store.md §4), so a glob would read a
    half-written set — and would keep counting the previous generation's rows,
    which are deliberately left on disk for readers still holding the old
    manifest."""
    files = [_q(os.path.join(cfg.files_dir, p["file"])) for p in parts]
    return "read_parquet([" + ",".join(f"'{f}'" for f in files) + "])"


def _depth_col(con, src: str, path_col: str) -> str:
    """`depth` when the parquet behind `src` carries it, else the slash-count
    expression over `path_col`.

    DESCRIBE reads footers only, so this costs no rows. Deciding per SOURCE
    rather than per file is exact: every partition a manifest names was written
    by one compaction, so a generation's schema is uniform (and DuckDB would
    refuse a mixed-schema read_parquet list anyway). An index predating the
    column keeps answering; migrating it is a full rescan."""
    cols = {r[0] for r in con.execute(
        f"DESCRIBE SELECT * FROM {src} LIMIT 0").fetchall()}
    return "depth" if "depth" in cols else depth_expr(path_col)


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
    pfx = (like_literal(root) + "/") if root != "/" else "/"
    inside = (f"(dir = '{_q(root)}' "
              f"OR dir LIKE '{pfx}%' ESCAPE '\\')")
    n_rows, total_size, n_dirs = 0, 0, 0
    types = []
    if os.path.exists(cfg.dirs_parquet):
        n_dirs = con.execute(
            f"SELECT count(*) FROM {dirs_src(cfg)} "
            f"WHERE {inside}").fetchone()[0]
    if m.get("partitions"):
        by_ext = con.execute(
            f"SELECT coalesce(nullif(ext, ''), 'no ext') e, count(*) n, "
            f"coalesce(sum(size), 0) s "
            f"FROM {_sources(cfg, m['partitions'])} "
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
    # `or "/"` because rstrip eats the filesystem root down to the empty
    # string, which the guard below then reads as "no root given" — so a
    # search of "/" answered `covered: false` every time. Everything past
    # here already special-cases "/" (see `prefix`); only this line did not.
    root = norm(os.path.abspath(os.path.expanduser((root or "").strip()))).rstrip("/") or "/"
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
    #
    # A package directory is the exception: the scan records it as ONE opaque
    # row and never lists it (scan.scan_dir_once), so its dirs row means "this
    # is a leaf", not "we know what is inside". The explorer can still navigate
    # into a .app, and the live walk answers that (it only refuses to descend
    # leaf CHILDREN, not a leaf it was pointed at) — so hand it over, exactly as
    # for any other uncovered folder, instead of reporting an empty corpus as
    # complete.
    # The test is is_inside_leaf_dir as well, not just the root's own final
    # component: any index written before the leaf rule still holds real dirs
    # rows for paths INSIDE a package, and answering `/x/Foo.app/Contents` from
    # that partial set while `/x/Foo.app` one level up goes to the walk is the
    # two-interchangeable-sources-disagree bug in miniature.
    inside_pkg = is_leaf_dir(root) or is_inside_leaf_dir(root)
    covered = not inside_pkg and con.execute(
        f"SELECT count(*) FROM {dirs_src(cfg)} "
        f"WHERE dir = '{_q(root)}'").fetchone()[0] > 0
    if not covered:
        return {**empty, "updated": updated, "age_s": age}
    prefix = (root + "/") if root != "/" else "/"
    prefix_like = (like_literal(root) + "/") if root != "/" else "/"
    limit = max(0, min(int(limit), MAX_CORPUS))
    hit = prune(m["partitions"], prefix)
    qlit = like_literal(q.strip()) if q and q.strip() else ""
    # Files and directories compete in ONE depth-ordered query, not two.
    #
    # Two queries meant the files branch was served first and directories got
    # only `limit - len(files)` rows — so on any tree big enough to truncate the
    # corpus, `room` was 0 and folder search was DEAD, not degraded: a query
    # naming a folder returned the files inside it and never the folder. The
    # live walk never had this bug because BFS interleaves both kinds.
    #
    # Shallow entries first (smaller `depth`), path order within a depth: when
    # the cap bites on a >limit tree, the capped corpus keeps the same
    # breadth-first character as the walk it replaces — plain ORDER BY path
    # would starve everything after the first deep subtree.
    #
    # The trade: directories now spend part of the budget files used to have,
    # so a very large tree carries slightly fewer files. A corpus with no
    # folders in it at all is strictly worse.
    branches = []
    if hit:
        fsrc = _sources(cfg, hit)
        like = f" AND path ILIKE '%{qlit}%' ESCAPE '\\'" if qlit else ""
        branches.append(
            f"SELECT path, size, mtime, false AS is_dir, "
            f"{_depth_col(con, fsrc, 'path')} AS depth FROM {fsrc} "
            f"WHERE path LIKE '{prefix_like}%' ESCAPE '\\'{like}")
    if include_dirs:
        dsrc = dirs_src(cfg)
        dlike = f" AND dir ILIKE '%{qlit}%' ESCAPE '\\'" if qlit else ""
        branches.append(
            f"SELECT dir AS path, CAST(NULL AS BIGINT) AS size, "
            f"nullif(mtime_ns, 0) / 1e9 AS mtime, true AS is_dir, "
            f"{_depth_col(con, dsrc, 'dir')} AS depth FROM {dsrc} "
            f"WHERE dir LIKE '{prefix_like}%' ESCAPE '\\'{dlike}")
    entries, truncated = [], False
    if branches:
        # One row past the cap, so "there was more" is known without a count.
        rows = con.execute(
            " UNION ALL ".join(branches)
            + f" ORDER BY depth, path LIMIT {limit + 1}").fetchall()
        for path, size, mtime, is_dir, _depth in rows[:limit]:
            entries.append({"rel": path[len(prefix):], "is_dir": bool(is_dir),
                            "size": int(size) if size is not None else None,
                            "mtime": float(mtime) if mtime is not None else None})
        truncated = len(rows) > limit
    return {"covered": True, "fresh": fresh, "updated": updated, "age_s": age,
            "root": root, "scanned_partitions": len(hit),
            "of_partitions": len(m["partitions"]), "entries": entries,
            "truncated": truncated, "total": len(entries)}
