"""Reading the index — strictly read-only, and strictly parameterized.

Ported from OpenIndex's `query.py` MINUS its `sql` action, which was an
arbitrary read/write surface with no allowlist and no read-only flag. User SQL
lives in `guarded_query.py` instead, where the confinement is the whole point of
the module (specs/query.md §5); nothing here takes a caller's statement.
`stats` and `lookup` build SQL only from escaped literals, an int-cast
limit/offset and a fixed sort allowlist.

duckdb is imported inside each function, not at module top: this module is
imported by the server's router, and a call against a missing index should not
pay a duckdb import.

See specs/query.md.
"""
import logging
import os
import re

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import is_inside_leaf_dir, is_leaf_dir, norm
from fused_render.index.rank import query_wants_hidden as _wants_hidden
from fused_render.index.rank import rank_entries
from fused_render.index.store import (
    depth_expr,
    like_literal,
    parquet_src,
    read_manifest,
)

logger = logging.getLogger(__name__)

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


def files_src(cfg: IndexConfig, parts) -> str:
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
    src = files_src(cfg, hit)
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
            f"FROM {files_src(cfg, m['partitions'])} "
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


def _root_is_covered(con, cfg: IndexConfig, root: str) -> bool:
    """Has the scan actually visited this exact directory?

    That exactness is what keeps a partial index honest: a root whose parent
    was scanned but which was itself pruned (ignored, or below a cancelled
    run's frontier) has no row.

    A package directory is the exception: the scan records it as ONE opaque
    row and never lists it (scan.scan_dir_once), so its dirs row means "this
    is a leaf", not "we know what is inside". The explorer can still navigate
    into a .app, and the live walk answers that (it only refuses to descend
    leaf CHILDREN, not a leaf it was pointed at) — so hand it over, exactly as
    for any other uncovered folder, instead of reporting an empty corpus as
    complete.

    The test is is_inside_leaf_dir as well, not just the root's own final
    component: any index written before the leaf rule still holds real dirs
    rows for paths INSIDE a package, and answering `/x/Foo.app/Contents` from
    that partial set while `/x/Foo.app` one level up goes to the walk is the
    two-interchangeable-sources-disagree bug in miniature."""
    return _coverage_reason(con, cfg, root) == ""


def _coverage_reason(con, cfg: IndexConfig, root: str) -> str:
    """Why the index cannot answer for `root`, or "" when it can.

    Two misses, and the caller has to tell them apart because only one of them
    is fixable: an `uncovered` folder becomes covered the moment something
    scans it, while a `package` never will — the scan records it as one opaque
    row by design (see `_root_is_covered`), so a client that asked for a scan
    and waited would wait for ever."""
    if is_leaf_dir(root) or is_inside_leaf_dir(root):
        return "package"
    covered = con.execute(f"SELECT count(*) FROM {dirs_src(cfg)} "
                          f"WHERE dir = '{_q(root)}'").fetchone()[0] > 0
    return "" if covered else "uncovered"


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
    covered = _root_is_covered(con, cfg, root)
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
        fsrc = files_src(cfg, hit)
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


# Rows stage A hands to the Python ranker. Measured basis (571k rows under
# /Users/<me>): the subsequence regex over every row costs 31-143 ms depending
# on the query, while scoring in Python costs ~4 us a row — 3,576 candidates
# scored in 14 ms, 176,505 in 186 ms. So SQL narrows and coarse-orders over
# everything, and Python does the real fuzzy scoring on a bounded slice. This
# is the bound.
RANK_CANDIDATE_CAP = 2_000

# Ranked hits a search answers with. The client renders a list; nobody scrolls
# past a couple of hundred fuzzy matches, and the whole point of ranking here
# is that the tail is the part nobody needed.
RANK_LIMIT = 200

# Whatever a caller asks for, they get at most this. `search_ranked` is not a
# corpus endpoint — `search_under` is, and it has its own MAX_CORPUS.
MAX_RANK_LIMIT = 2_000

# RE2 metachars, escaped one at a time. Not `re.escape`: that escapes a space
# as `\ `, which RE2 rejects as an unknown escape, and this pattern is executed
# by duckdb, not by Python.
_RE2_META = set("\\.^$|()[]{}*+?")


def _subseq_regex(q: str) -> str:
    """`q` as a subsequence pattern: `abc` -> `a.*b.*c`, ready for a duckdb
    string literal (single quotes doubled; backslashes are literal in a
    standard SQL string, so they need no doubling)."""
    return ".*".join(("\\" + c if c in _RE2_META else c).replace("'", "''")
                     for c in q)


# root -> (index generation, ignore-root rels). The `.gitignore` rows under a
# root change only when the index does, and the query behind them is a scan of
# the path column — a few tens of ms that a keystroke must not pay. Keyed on the
# manifest's `updated`, so a completed scan re-discovers exactly once. Tiny and
# bounded by the number of roots ever searched in one process.
_ORACLE_RELS: dict = {}


def _ignore_roots(con, cfg: IndexConfig, parts, root: str, prefix: str,
                  updated) -> list:
    """Dirs under `root` that hold a `.gitignore`, as rels ('' = the root).

    The server's gitignore filter needs these, and it cannot get them from a
    ranked payload: stage A drops every dot-leading rel unless the query asks
    for hidden entries, so `.gitignore` is essentially never a candidate, and a
    filter that discovers its oracles from the rows it is given then decides
    nothing at all (server/index_gitignore.filter_corpus). The index knows
    where they are, so it says.

    Not the same question as "which oracle decides this row" — that stays in
    the filter. This is only the discovery half, moved to the one place that
    can see the whole tree cheaply."""
    key = (cfg.dir, root)
    cached = _ORACLE_RELS.get(key)
    if cached is not None and cached[0] == updated:
        return cached[1]
    rels = []
    if parts:
        rel_from = len(prefix) + 1
        rows = con.execute(
            f"SELECT DISTINCT substr(path, {rel_from}) AS rel "
            f"FROM {files_src(cfg, parts)} "
            f"WHERE path LIKE '{like_literal(root)}/%' ESCAPE '\\' "
            f"AND path LIKE '%/.gitignore' ESCAPE '\\'").fetchall()
        for (rel,) in rows:
            rels.append(rel[: -len("/.gitignore")] if "/" in rel else "")
    _ORACLE_RELS[key] = (updated, rels)
    return rels


def search_ranked(cfg: IndexConfig, root: str, q: str = "",
                  limit: int = RANK_LIMIT, include_dirs: bool = True,
                  cap: int = RANK_CANDIDATE_CAP,
                  gitignore_filter=None) -> dict:
    """Search `root` for `q` — filtered AND ranked here, top `limit` returned.

    The home page used to fetch the whole corpus and rank it in the browser:
    19.8 MB and 164k rows on one keystroke, and silently capped at MAX_CORPUS,
    so ~71% of a 571k-file home could not be found AT ALL. This answers a few
    KB from the whole index instead.

    Two stages, because neither alone is affordable:

    - **A, in SQL, over every row under `root`.** A candidate filter on the rel
      (the same relation `fuzzy_match` tests, so a candidate here is a possible
      hit there) plus a coarse `tier` — name-exact / name-prefix /
      name-contains / rel-contains / subsequence-only — and `ORDER BY tier,
      depth, rel LIMIT cap`. Files and directories compete in ONE query, for
      the reason `search_under` explains at length: two queries served files
      first and folder search died on any tree big enough to fill the budget.

      Two passes, cheapest first, and the second is often skipped:

        1. substring — `lower(rel) LIKE '%q%'`;
        2. subsequence — `regexp_matches(lower(rel), 'a.*b.*c')`.

      Measured over 571k rows: `render` is 30,319 rows / 51 ms as a substring
      against 176,505 / 143 ms as a subsequence; `readme.md` 3,056 / 41 ms
      against 11,766 / 45 ms. So pass 2 runs only when pass 1 cannot fill the
      returned `limit`, which is LOSSLESS rather than approximate:
      `fuzzy_match`'s substring branch sets `longest_run = len(q)`, the maximum
      the subsequence branch can never reach (a contiguous run of the whole
      query would have taken the substring branch), and `rank_compare` orders
      on `longest_run` first. Every substring hit therefore outranks every
      subsequence-only hit, so once pass 1 fills the cut, pass 2 can only append
      rows below the cut that nobody will ever see. `escalated` reports which
      happened.

      The check is made AFTER ranking and gitignore-filtering pass 1, not on
      the raw SQL row count, so no safety margin is needed or used: the number
      compared against `limit` is the number of rows the user would actually
      get. A margin over the raw count would be the guess this avoids —
      gitignore can drop any fraction of a pass, and guessing high spends the
      143 ms it was trying to save.

      Deliberately NOT taken: a depth cap on pass 1. `depth` is already a
      tie-break in the ORDER BY, so shallow-first is delivered without a
      cutoff, and a hard limit would hide a deep exact match and cost a second
      round trip precisely when the first answer was wrong.
    - **B, in Python.** `rank_entries` (index/rank.py) over those ≤`cap` rows —
      the real fuzzy scoring, in parity with the browser's ranker — then the
      gitignore filter, THEN the cut to `limit`. That order is not incidental:
      filtering after the cut is what makes today's corpus report `truncated`
      while holding fewer rows than it claims.

    KNOWN, and logged rather than hidden: stage A's cap can in principle drop a
    row stage B would have ranked first. It is unlikely because the two agree
    on what is coarsely good — a name-substring hit outranks a fuzzy-only one
    in `rank_compare` too, and tier 1-3 rows are emitted before any tier 5 one
    — but it is not impossible, so a cap that bites is a debug line in the log and
    `truncated: true` in the response. Silent truncation is what this removes,
    not what it reintroduces.

    `gitignore_filter(root, entries, oracle_rels) -> entries` is how the server
    layer hands in `index_gitignore.filter_corpus`; the index package does not
    import the server. `oracle_rels` is this function's half of that job — the
    dirs holding a `.gitignore`, read out of the INDEX (`_ignore_roots`),
    because a filter that discovers them from a 200-row ranked payload finds
    none and therefore filters nothing. Omitted, nothing is filtered.

    Coverage semantics are `search_under`'s exactly: an uncovered root, a
    missing index or a package directory answers `covered: false` with no hits
    — never an error, because "no index yet", "not covered" and "a scan is
    running" are one condition to a search box.
    """
    root = norm(os.path.abspath(os.path.expanduser((root or "").strip()))).rstrip("/") or "/"
    m = read_manifest(cfg)
    # `reason` is the miss's cause, and it is what the in-folder search box
    # switches on: a package or a mount-backed folder goes to the live walk, an
    # uncovered one is scanned on demand. Decided here rather than in the
    # client, so there is one copy of the rule. The mount half is the server
    # layer's to add (MountGuard); this package/uncovered half is the index's.
    empty = {"covered": False, "fresh": False, "updated": None, "age_s": None,
             "root": root, "hits": [], "truncated": False, "total": 0,
             "escalated": False, "scanned_partitions": 0,
             "reason": "package" if (is_leaf_dir(root)
                                     or is_inside_leaf_dir(root)) else "uncovered",
             "of_partitions": len(((m or {}).get("partitions")) or [])}
    if m is None or not root or not os.path.exists(cfg.dirs_parquet):
        return empty
    import time

    import duckdb

    updated = m.get("updated")
    age = (time.time() - updated) if isinstance(updated, (int, float)) else None
    fresh = age is not None and age <= FRESH_MAX_AGE_S
    con = duckdb.connect()
    miss = _coverage_reason(con, cfg, root)
    if miss:
        return {**empty, "reason": miss, "updated": updated, "age_s": age}
    prefix = (root + "/") if root != "/" else "/"
    prefix_like = (like_literal(root) + "/") if root != "/" else "/"
    limit = max(0, min(int(limit), MAX_RANK_LIMIT))
    cap = max(1, int(cap))
    hit = prune(m["partitions"], prefix)
    base = {"covered": True, "fresh": fresh, "updated": updated, "age_s": age,
            "root": root, "reason": "", "scanned_partitions": len(hit),
            "of_partitions": len(m["partitions"])}
    qs = (q or "").strip()
    if not qs:
        # Nothing typed is not "everything": the empty query has no ranking to
        # apply, and answering with an arbitrary 200 files would be noise.
        return {**base, "hits": [], "truncated": False, "total": 0,
                "escalated": False}

    # `substr` from the prefix's length, so every comparison below is against
    # the REL — exactly the string stage B scores. Filtering on the full path
    # would let the root's own spelling admit rows no fuzzy match will keep.
    rel_from = len(prefix) + 1
    branches = []
    if hit:
        fsrc = files_src(cfg, hit)
        branches.append(
            f"SELECT substr(path, {rel_from}) AS rel, size, mtime, "
            f"false AS is_dir, {_depth_col(con, fsrc, 'path')} AS depth "
            f"FROM {fsrc} WHERE path LIKE '{prefix_like}%' ESCAPE '\\'")
    if include_dirs:
        dsrc = dirs_src(cfg)
        branches.append(
            f"SELECT substr(dir, {rel_from}) AS rel, CAST(NULL AS BIGINT) AS size, "
            f"nullif(mtime_ns, 0) / 1e9 AS mtime, true AS is_dir, "
            f"{_depth_col(con, dsrc, 'dir')} AS depth "
            f"FROM {dsrc} WHERE dir LIKE '{prefix_like}%' ESCAPE '\\'")
    if not branches:
        return {**base, "hits": [], "truncated": False, "total": 0,
                "escalated": False}

    ql = like_literal(qs.lower())
    qq = _q(qs.lower())
    # The coarse tier. Deliberately cruder than `rank_entries`' — it exists to
    # decide which `cap` rows are worth scoring properly, not to order the
    # answer, and every extra SQL expression here is paid on all 571k rows.
    tier = (f"CASE WHEN nm = '{qq}' THEN 1 "
            f"WHEN nm LIKE '{ql}%' ESCAPE '\\' THEN 2 "
            f"WHEN nm LIKE '%{ql}%' ESCAPE '\\' THEN 3 "
            f"WHEN lrel LIKE '%{ql}%' ESCAPE '\\' THEN 4 "
            f"ELSE 5 END")
    # Hidden entries are dropped HERE as well as in the ranker, so a query that
    # does not want them cannot spend the candidate cap on them (rank.py's
    # query_wants_hidden / is_hidden_rel are the definitions; this mirrors them).
    hidden = ("" if _wants_hidden(qs)
              else " AND NOT (lrel LIKE '.%' OR lrel LIKE '%/.%')")
    inner = (f"SELECT *, lower(rel) AS lrel, "
             f"regexp_extract(lower(rel), '[^/]*$') AS nm FROM ("
             + " UNION ALL ".join(branches) + ")")

    def pass_over(predicate: str):
        """One candidate pass: `predicate` over every row under the root, coarse
        tier order, one row past the cap so "the cap bit" needs no count."""
        rows = con.execute(
            f"SELECT rel, size, mtime, is_dir FROM ({inner}) "
            f"WHERE {predicate}{hidden} "
            f"ORDER BY {tier}, depth, rel LIMIT {cap + 1}").fetchall()
        if len(rows) > cap:
            # DEBUG, not WARNING: this fires on every keystroke of any broad
            # query — "a" and "e" alone exceed the cap on the substring pass —
            # and a line that appears whenever the app is working normally
            # trains everyone to ignore the log. The response says the same
            # thing where it can be acted on (`truncated: true`), so nothing is
            # silent; it is just not shouted.
            logger.debug(
                "index rank: the candidate cap (%d) bit for %r under %s — the "
                "ranked answer is drawn from stage A's coarse top rows only",
                cap, qs, root)
        entries = [{"rel": rel, "is_dir": bool(is_dir),
                    "size": int(size) if size is not None else None,
                    "mtime": float(mtime) if mtime is not None else None}
                   for rel, size, mtime, is_dir in rows[:cap]]
        ranked = rank_entries(qs, entries)
        if gitignore_filter is not None:
            ranked = gitignore_filter(
                root, ranked,
                _ignore_roots(con, cfg, hit, root, prefix, updated))
        return ranked, len(rows) > cap

    # The LADDER (see the docstring): the cheap substring pass first, and the
    # regex only when the cheap one cannot fill the cut. `capped` also stops the
    # escalation — a pass that filled the candidate cap already handed stage B
    # more coarsely-better rows than it can return, so widening the filter can
    # only push MORE of them out of the cap.
    ranked, capped = pass_over(f"lrel LIKE '%{ql}%' ESCAPE '\\'")
    escalated = False
    if not capped and len(ranked) < limit:
        escalated = True
        ranked, capped = pass_over(
            f"regexp_matches(lrel, '{_subseq_regex(qs.lower())}')")
    return {**base, "hits": ranked[:limit],
            "truncated": capped or len(ranked) > limit,
            "total": len(ranked[:limit]), "escalated": escalated}
