"""Reading the index — strictly read-only, and strictly parameterized.

Ported from OpenIndex's `query.py` MINUS its `sql` action, which was an
arbitrary read/write surface with no allowlist and no read-only flag. User SQL
lives in `guarded_query.py` instead, where the confinement is the whole point of
the module (specs/query.md §5); nothing here takes a caller's statement.
`stats` builds SQL only from escaped literals, same as every other reader here.

duckdb is imported inside each function, not at module top: this module is
imported by the server's router, and a call against a missing index should not
pay a duckdb import.

See specs/query.md.
"""
import logging
import os
import re

from fused_render.index.cancel import CancelToken, Cancelled
from fused_render.index.config import IndexConfig
from fused_render.index.ignore import (
    MountGuard,
    is_inside_leaf_dir,
    is_leaf_dir,
    norm,
)
from fused_render.index.store import (
    depth_expr,
    like_literal,
    parquet_src,
    read_manifest,
    search_threads,
)

logger = logging.getLogger(__name__)

# A bare drive letter ("C:") is what rstrip("/") leaves a Windows drive root
# ("C:/") reduced to — the same way rstrip("/") leaves a POSIX root ("/")
# reduced to "". Every root normalization below restores the trailing "/" for
# the POSIX case (the existing `or "/"`); this is the matching restoration for
# the Windows one, without which a scan root that IS a whole drive would
# resolve here to "C:" while canonical_root() (index/runner.py) — what every
# stored dirs.parquet row is actually keyed under — resolves the identical
# input to "C:/", and the two would never compare equal.
_BARE_DRIVE = re.compile(r"^[A-Za-z]:$")

# A typed string starting "C:\" or "C:/" is unambiguously a Windows absolute
# path — there is no POSIX-style "leading slash means depth-1 anchor at the
# box root" reading for it to be confused with (that ambiguity is specific to
# a bare "/", which a drive letter never looks like). `resolve_query` below
# treats a match the same way it treats "~": always walked as an escape from
# the box's own root, never falling back to `root` the way the bare-"/" branch
# can.
_DRIVE_ABS = re.compile(r"^[A-Za-z]:[\\/]")


def _root_or_bare(stripped: str) -> str:
    """`stripped` (already rstripped of "/") restored to its canonical bare-
    root spelling if it collapsed to one — "" -> "/", "C:" -> "C:/" — else
    unchanged."""
    return stripped + "/" if not stripped or _BARE_DRIVE.match(stripped) else stripped

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


def files_src(cfg: IndexConfig, parts) -> str:
    """A duckdb source over exactly the partitions the MANIFEST names.

    Never a `files/*.parquet` glob: a compaction writes the next generation
    into the same directory (index-store.md §4), so a glob would read a
    half-written set — and would keep counting the previous generation's rows,
    which are deliberately left on disk for readers still holding the old
    manifest."""
    files = [_q(os.path.join(cfg.files_dir, p["file"])) for p in parts]
    return "read_parquet([" + ",".join(f"'{f}'" for f in files) + "])"


def _src_cols(con, src: str) -> set:
    """The column names the parquet behind `src` carries.

    DESCRIBE reads footers only, so a single call costs no rows. `_name_col`
    and `_depth_col` both used to run their OWN `DESCRIBE` against the same
    `src` (one footer read each) — this collapses that into one lookup per
    caller, since both questions are answered by the same column list.
    (A `cache: dict` parameter used to let a caller share that one lookup
    ACROSS sources too, for a caller asking about the same source more than
    once in one call — D707. Dropped: `search_ranked`'s files branch was the
    only caller that ever passed one, and D708 removed the dirs branch's own
    `_src_cols`/`_depth_col` call entirely — the cache's second ask never
    happened. Re-add it if a caller that actually hits the same source twice
    shows up; keeping it unused made the "second ask is a dict lookup" claim
    describe no live call.)

    Deciding per SOURCE rather than per file is exact: every partition a
    manifest names was written by one compaction, so a generation's schema is
    uniform (and DuckDB would refuse a mixed-schema read_parquet list
    anyway)."""
    return {r[0] for r in con.execute(
        f"DESCRIBE SELECT * FROM {src} LIMIT 0").fetchall()}


_SRC_COLS_CACHE_MAX = 16
_src_cols_cache: dict = {}


def _cached_src_cols(con, src: str, cache_key: tuple) -> set:
    """Cache-wrapped `_src_cols`, keyed on `cache_key` — callers pass
    `(cfg.dir, manifest["generation"], "files"|"dirs")`.

    Every partition a manifest generation names was written by one
    compaction and shares one schema (see `_src_cols`'s own docstring), so
    it is safe to skip the DESCRIBE entirely once ANY caller has already
    paid for it this generation — not merely once per call, which
    `_src_cols`'s own inline `cache` parameter used to do (D707/D708) before
    being dropped as dead code. This is SPEC-index-search-wedge.md item 5:
    every keystroke against an unchanged index used to pay one ~8ms DESCRIBE
    purely to learn a column set that cannot have changed since the last
    one.

    `cfg.dir` is the store's stable identity (`IndexConfig.dir`); the
    generation increments on compaction (`store.py`'s `compact`), so a new
    generation invalidates itself automatically — no explicit eviction on
    compaction is needed. Bounded to `_SRC_COLS_CACHE_MAX` entries, oldest
    inserted evicted first, so a long-lived process juggling many index
    stores/generations cannot grow this without limit.

    Two caveats review finding I raised, both real but neither worth more
    machinery than this (D880 has the fuller reasoning):

    - `dirs.parquet` (the `"dirs"` half of `cache_key`) is a single file
      overwritten in place by every compaction (`store.py`'s COPY to
      `dirs_parquet + ".new"` then `os.replace`), unlike `files/*.parquet`
      which are named per generation and never overwritten. The swap lands
      before the manifest naming the new generation is written, so a reader
      that already loaded the OLD manifest and asks for that generation's
      dirs schema for the first time during the swap's race window could, in
      principle, DESCRIBE the new generation's bytes under the old
      generation's cache key. This is harmless for what this cache actually
      answers (a column SET) because compaction never changes dirs.parquet's
      schema between generations — only its rows — so every generation's
      "dirs" entry holds the same value regardless of which generation's
      bytes were actually behind the read. It would stop being harmless only
      if a future change made the dirs schema itself vary by generation, at
      which point this cache key stops being sound and would need to key on
      something that actually is generation-stable content, not merely a
      generation number.
    - `delete_store` (`store.py`) removes the manifest along with the rest of
      the store, so the next compaction's `generation` starts back at 1 —
      the same key a PRE-delete generation 1 could have used earlier in this
      same process's life. `delete_store` calls `forget_src_cols_for(cfg.dir)`
      below to evict every cache entry for that store before returning, so a
      rebuilt store's first real generation-1 lookup can never be shadowed
      by a stale pre-delete entry."""
    cached = _src_cols_cache.get(cache_key)
    if cached is not None:
        return cached
    cols = _src_cols(con, src)
    _src_cols_cache[cache_key] = cols
    if len(_src_cols_cache) > _SRC_COLS_CACHE_MAX:
        _src_cols_cache.pop(next(iter(_src_cols_cache)))
    return cols


def forget_src_cols_for(cfg_dir: str) -> None:
    """Evict every `_cached_src_cols` entry for `cfg_dir` (an `IndexConfig.dir`).

    Review finding I: `store.py`'s `delete_store` removes the manifest, so
    the NEXT compaction's `generation` starts back at 1 — the same
    `(cfg_dir, 1, "files"|"dirs")` key a pre-delete generation 1 could
    already have populated earlier in this same process's life, which would
    otherwise shadow the rebuilt store's real schema. `delete_store` calls
    this before returning so a store's cache entries never outlive the store
    itself. Cheap and rare (an interactive delete, not a per-query path):
    a linear scan of a cache bounded to `_SRC_COLS_CACHE_MAX` entries."""
    for key in [k for k in _src_cols_cache if k[0] == cfg_dir]:
        _src_cols_cache.pop(key, None)


def _name_col(cols: set) -> str:
    """`lower(name)` when `cols` (from `_src_cols`) carries a `name` column,
    else the regex extracted from `path`.

    Same pattern as `_depth_col` just below, and for the same reason: without
    this, `search_ranked`'s candidate subquery ran a regex per row over every
    indexed file (~571k) on every keystroke, purely to recover a value the
    files parquet already stores (store.py's schema — `name` is denormalised
    out of `path` at scan time, scan.py:147's `e.name`, unlowered and
    including the extension). `lower(name)` is therefore byte-for-byte the
    same string `regexp_extract(lower(path), '[^/]*$')` computes, just without
    paying for the regex. Only the files table HAS a `name` column — dirs has
    none (store.py's dir schema) — so this is for the files branch only; an
    index predating the column keeps answering via the fallback."""
    return ("lower(name)" if "name" in cols
            else "regexp_extract(lower(path), '[^/]*$')")


def _depth_col(cols: set, path_col: str) -> str:
    """`depth` when `cols` (from `_src_cols`) carries it, else the slash-count
    expression over `path_col`. An index predating the column keeps answering;
    migrating it is a full rescan."""
    return "depth" if "depth" in cols else depth_expr(path_col)


def stats(cfg: IndexConfig, root: str = "", breakdown: bool = False,
         token=None) -> dict:
    """Totals for ONE subtree — the explicit `root`, else the manifest's
    `last_root`. An index may hold several roots, so a whole-index total
    would be a number nobody asked for.

    The default is a `count(*)`/`sum(size)` over partitions PRUNED to the
    root's range (`prune`, query.md §4) — no per-extension grouping. Pass
    `breakdown=True` for `types`: the same partitions, but a `GROUP BY ext`
    pass over them, which most callers never read and shouldn't pay for.

    `token` (index/cancel.CancelToken), when given, is bound to the connection
    the moment it exists and checked before each query — same contract
    `search_ranked` documents at length. `breakdown=True` is the only pass
    here expensive enough for cancellation to matter in practice, but the
    check costs nothing on the cheap path either."""
    m = read_manifest(cfg)
    if m is None:
        return {"empty": True, "location": cfg.dir, "rows": 0, "dirs": 0,
                "total_size": 0, "types": [], "partitions": []}
    import duckdb

    con = duckdb.connect()
    # Capped like every other interactive index read (search_threads' docstring
    # in store.py): a bare connect() defaults to one thread per core, and this
    # is a keystroke away, not a background job.
    con.execute(f"SET threads TO {search_threads()}")
    if token is not None:
        token.bind(con)
    try:
        root = norm(os.path.expanduser(root.strip())) if root.strip() else ""
        root = _root_or_bare((root or m.get("last_root") or "").rstrip("/"))
        # `root` already ends in "/" for a bare root (POSIX "/", or a Windows
        # drive root "C:/" via _root_or_bare above) — appending another "/"
        # unconditionally, as a plain `root != "/"` check used to, would double
        # it on the drive-root case and match nothing.
        prefix = root if root.endswith("/") else root + "/"
        pfx = like_literal(prefix)
        inside = (f"(dir = '{_q(root)}' "
                  f"OR dir LIKE '{pfx}%' ESCAPE '\\')")
        hit = prune(m["partitions"], prefix)
        n_rows, total_size, n_dirs = 0, 0, 0
        types = []
        if os.path.exists(cfg.dirs_parquet):
            if token is not None:
                token.check()
            n_dirs = con.execute(
                f"SELECT count(*) FROM {dirs_src(cfg)} "
                f"WHERE {inside}").fetchone()[0]
        if hit and breakdown:
            if token is not None:
                token.check()
            by_ext = con.execute(
                f"SELECT coalesce(nullif(ext, ''), 'no ext') e, count(*) n, "
                f"coalesce(sum(size), 0) s "
                f"FROM {files_src(cfg, hit)} "
                f"WHERE {inside} "
                f"GROUP BY 1 ORDER BY s DESC").fetchall()
            n_rows = sum(r[1] for r in by_ext)
            total_size = sum(r[2] for r in by_ext)
            top, rest = by_ext[:50], by_ext[50:]
            types = [{"ext": e, "n": int(n), "size": int(sz)} for e, n, sz in top]
            if rest:
                types.append({"ext": "other", "n": int(sum(r[1] for r in rest)),
                              "size": int(sum(r[2] for r in rest))})
        elif hit:
            if token is not None:
                token.check()
            n_rows, total_size = con.execute(
                f"SELECT count(*), coalesce(sum(size), 0) "
                f"FROM {files_src(cfg, hit)} WHERE {inside}").fetchone()
        return {"empty": False, "location": cfg.dir, "rows": int(n_rows),
                "dirs": int(n_dirs), "total_size": int(total_size),
                "updated": m.get("updated"), "last_root": root,
                "partitions": m["partitions"], "types": types}
    except duckdb.InterruptException:
        # Same attribution rule search_ranked's identical handler documents:
        # only an interrupt THIS token caused becomes Cancelled.
        if token is not None and token.cancelled:
            raise Cancelled() from None
        raise
    finally:
        # `unbind` BEFORE `close` — see search_ranked's identical `finally`
        # for why the ordering matters (a disconnect-triggered `cancel()`
        # racing the connection closing).
        if token is not None:
            token.unbind()
        con.close()


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
                 include_dirs: bool = True, token=None) -> dict:
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
    keeps the endpoint useful for a caller that only wants the hits, and it
    is what `fused.fileIndex.search` (the public JS bridge, runtime.js) is
    backed by.

    `q` is run through `expand_whitespace_query` (SPEC-search-space-
    wildcard.md §3) exactly like `resolve_query`'s `raw` — one shared
    transform, not two copies of the whitespace rule. `q` is a no-op only
    when it has NEITHER whitespace NOR a `*` anywhere (the common single-
    word case): the filter stays the plain `ILIKE '%q%'` it always was.
    Whitespace OR a literal `*` flips the filter to the same glob-to-regex
    matching `resolve_query`'s glob mode uses (`_glob_to_regex`), confined
    to any depth under `root` — this function never walks a base off `q`
    the way `resolve_query` does, so there is no "/"-in-`raw` distinction to
    make here; the whole expanded pattern always searches any depth, the
    same as an unadorned glob with no "/" gets `resolve_query`'s own
    implicit `**/` prefix. This does NOT make `search_under` a general glob
    engine with base-walking or an implicit-depth distinction — it is still
    the same `ILIKE`-or-`regexp_matches` binary choice it always was, just
    keyed on `"*" in expanded` (mirroring `resolve_query`'s own `is_glob`
    check) rather than "did whitespace fire", now that `expand_whitespace_
    query` always wraps a whitespace-free `*`-containing `q` too (see
    `expand_whitespace_query`'s docstring; this is the same accepted
    precision-glob consequence, not a `search_under`-specific choice — see
    DECISIONS.md).

    A WHITESPACE-ONLY `q` (code review finding 3) is not the same thing as
    `q` never having been passed: an absent/empty `q` is this function's own
    documented "no filter, whole corpus" contract, but a `q` that is
    non-empty yet resolves via `expand_whitespace_query` to `""` (there is
    no literal character anywhere in an all-whitespace string to search on)
    means the caller typed something and it resolved to nothing — the same
    resolved-empty state `search_ranked`'s `if not qs: return {hits: []}`
    guard already treats as zero hits, not "everything." Answers with zero
    entries in this case, never the unfiltered corpus.

    `token`, when given, follows `search_ranked`'s contract exactly: bound to
    the connection as soon as it exists, checked before the one real query,
    and an interrupt this token caused re-raises as `Cancelled`.
    """
    # `or "/"` because rstrip eats the filesystem root down to the empty
    # string, which the guard below then reads as "no root given" — so a
    # search of "/" answered `covered: false` every time. Everything past
    # here already special-cases "/" (see `prefix`); only this line did not.
    root = _root_or_bare(
        norm(os.path.abspath(os.path.expanduser((root or "").strip()))).rstrip("/"))
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
    # Capped like every other interactive index read (search_threads' docstring
    # in store.py): a bare connect() defaults to one thread per core, and this
    # is a keystroke away, not a background job.
    con.execute(f"SET threads TO {search_threads()}")
    if token is not None:
        token.bind(con)
    try:
        covered = _root_is_covered(con, cfg, root)
        if not covered:
            return {**empty, "updated": updated, "age_s": age}
        # See stats()'s identical fix above: root already ends in "/" for
        # any bare root (POSIX or a Windows drive), not only "/" itself.
        prefix = root if root.endswith("/") else root + "/"
        prefix_like = like_literal(prefix)
        limit = max(0, min(int(limit), MAX_CORPUS))
        hit = prune(m["partitions"], prefix)
        q_trimmed = q.strip() if q else ""
        expanded = expand_whitespace_query(q) if q else ""
        wildcard_regex = None
        if "*" in expanded:
            # Whitespace or a literal `*` put a wildcard in the expanded
            # query — mirrors `resolve_query`'s own `is_glob = "*" in raw`
            # check, not "did the transform change anything" (a whitespace-
            # free `*.pdf` is no longer a no-op either — see
            # `expand_whitespace_query`'s docstring — so that used-to-work
            # equality check would now also fire for it, but checking `"*"
            # in expanded` directly is the same decision `resolve_query`
            # makes and doesn't depend on that being true). Match via the
            # same glob-to-regex translation `resolve_query`'s glob mode
            # uses, confined to the REL portion of the path (post-`prefix`)
            # so a `/`-containing expansion lines up with the same relative
            # structure `rel` already reflects, not the absolute path on
            # disk. Always any-depth (see docstring) — mirrors the implicit
            # `**/` prefix an unadorned `resolve_query` glob with no "/"
            # gets.
            wildcard_regex = _q(_glob_to_regex(("**/" + expanded).lower()))
        qlit = like_literal(q_trimmed) if q_trimmed and wildcard_regex is None else ""
        # Code review finding 3: `q` truthy but `expanded` empty means `q`
        # was WHITESPACE-ONLY — `expand_whitespace_query`'s own contract
        # (test_index_query.py, A2) already resolves that to `""` because
        # there is no literal character anywhere in an all-whitespace string
        # to search on, the SAME resolved-empty state `search_ranked`'s own
        # `if not qs: return {hits: []}` guard treats as zero hits. This is
        # NOT the same as `q` never having been passed at all (this
        # function's own documented "no filter, whole corpus" contract,
        # `q=""`/`q=None`) — conflating the two let a whitespace-only query
        # fall through every filter guard below (`q_trimmed` and `qlit` are
        # ALSO empty for whitespace) and answer with the unfiltered corpus.
        no_match = bool(q) and not expanded
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
        prefix_chars = len(prefix)
        branches = []
        if hit:
            fsrc = files_src(cfg, hit)
            if wildcard_regex is not None:
                like = (f" AND regexp_matches(lower(substr(path, {prefix_chars + 1})), "
                        f"'{wildcard_regex}')")
            else:
                like = f" AND path ILIKE '%{qlit}%' ESCAPE '\\'" if qlit else ""
            branches.append(
                f"SELECT path, size, mtime, false AS is_dir, "
                f"{_depth_col(_cached_src_cols(con, fsrc, (cfg.dir, m.get('generation'), 'files')), 'path')} AS depth FROM {fsrc} "
                f"WHERE path LIKE '{prefix_like}%' ESCAPE '\\'{like}")
        if include_dirs:
            dsrc = dirs_src(cfg)
            if wildcard_regex is not None:
                dlike = (f" AND regexp_matches(lower(substr(dir, {prefix_chars + 1})), "
                         f"'{wildcard_regex}')")
            else:
                dlike = f" AND dir ILIKE '%{qlit}%' ESCAPE '\\'" if qlit else ""
            branches.append(
                f"SELECT dir AS path, CAST(NULL AS BIGINT) AS size, "
                f"nullif(mtime_ns, 0) / 1e9 AS mtime, true AS is_dir, "
                f"{_depth_col(_cached_src_cols(con, dsrc, (cfg.dir, m.get('generation'), 'dirs')), 'dir')} AS depth FROM {dsrc} "
                f"WHERE dir LIKE '{prefix_like}%' ESCAPE '\\'{dlike}")
        entries, truncated = [], False
        if branches and not no_match:
            if token is not None:
                token.check()
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
    except duckdb.InterruptException:
        if token is not None and token.cancelled:
            raise Cancelled() from None
        raise
    finally:
        # `unbind` BEFORE `close` — see search_ranked's identical `finally`
        # for why the ordering matters (a disconnect-triggered `cancel()`
        # racing the connection closing).
        if token is not None:
            token.unbind()
        con.close()


# Ranked hits a search answers with. The client renders a list; nobody scrolls
# past a couple of hundred fuzzy matches, and the whole point of ranking here
# is that the tail is the part nobody needed.
RANK_LIMIT = 200

# Whatever a caller asks for, they get at most this. `search_ranked` is not a
# corpus endpoint — `search_under` is, and it has its own MAX_CORPUS.
MAX_RANK_LIMIT = 2_000

# A glob's own ceiling, wider than a ranked substring answer's: every glob hit
# is an equal match with no tail to trim, so the client renders and select-alls
# the whole fetched set (never a top-N of it) — the only limit left is how many
# rows a scrollable list and a select-all should ever hold at once.
MAX_GLOB_RANK_LIMIT = 5_000

# Position-free, basename-first ranking (DECISIONS.md, search-architecture-
# review.md §6/§10.1/§10.3). The scorer used to be a scalar sum
# (`n + 3*(n-1) + 5*segment_starts - DEPTH_PENALTY*... + name_bonus +
# _TAIL_BONUS`) built around `p0 = strpos(lrel, q) - 1` — the FIRST
# occurrence of the query in the full path. That was the generator of a whole
# bug class: `LIKE`/`regexp_matches` matches ANY occurrence, so every term
# reading `p0` (the tail bonus, the segment-start count, the tier-2/3
# boundary, and the glob path's chained `strpos` walk) could be graded
# against a DIFFERENT occurrence than the one the filter actually matched on
# — confirmed by probe (query `js`: `lib/app.js` scored 35, `js/lib/app.js`
# and `json/script.js` both scored 10, purely because the first "js" in
# their paths sits in a directory segment). `_TAIL_BONUS` (commit
# 3622523ad) was the sharpest instance: sized equal to the prefix bonus, it
# tied a true exact-basename match against a mere tail match at the same
# depth, which `lower(rel) ASC` then resolved alphabetically instead of by
# match quality (`config.json` vs `app-config`, both scoring 51).
#
# The replacement drops position entirely. Every signal below is a boolean
# predicate over `nm` (the already-lowercased basename) and the query's
# literal run(s), built once in `_name_predicate_sql` and shared by both
# `_rank_sql` (one literal: the whole substring query) and `_glob_sql`
# (`_glob_literal_runs`'s literal pieces) so the two modes cannot
# independently drift the way `_rank_sql` and the deleted `_glob_score_sql`
# did. `ORDER BY` on the resulting vector (`_lex_order_and_score`) replaces
# the scalar sum — SQL does field-separated, VS-Code/Zed-style ranking
# natively via `ORDER BY col1 DESC, col2 DESC, ...` without needing a single
# number at all. `score` is still computed and returned (compatibility: see
# `search_ranked`'s docstring) as a WEIGHTED SUM of the same predicates for
# display/debugging only — the actual order comes from the `ORDER BY` vector,
# not from sorting by this number, so a caller must not assume `score DESC`
# reproduces the result order.
#
# `depth` and `length(nm)` remain as late, purely positional (not
# position-in-string — position-in-CORPUS) tie-breaks: a shallower path and a
# shorter filename are both real, position-free signals (Zoekt ranks on
# filename length; a shallow match is more likely to be what the user meant
# than a deep one with an identical name-quality signature), not first-
# occurrence bonuses, so they carry no trace of the bug class above.
#
# No segment-start / camelCase-hump bonus survives this rewrite. The old one
# read the ORIGINAL-case `rel` at the matched window's positions
# (`p0..p0+n`) — inherently the same "which occurrence" question this
# rewrite exists to eliminate, and `nm` (the column every predicate here is
# built from) is stored already-lowercased, so recovering case information
# for a hump test would need a new original-case basename column. Left out
# rather than reintroduced as another positional read; see this round's
# report for the explicit call-out.


def _walk_from(start: str, rest: str, guard: "MountGuard | None" = None,
                token: "CancelToken | None" = None) -> tuple:
    """Consume `rest`'s "/"-joined segments onto `start`, one directory at a
    time, stopping at the first segment that contains a `*` or the first one
    that does not exist as a directory. Returns `(base, pattern, advanced)`:
    `base` is how far the walk got, `pattern` is whatever segments were left
    over (rejoined with "/"), and `advanced` says whether even the FIRST
    segment was consumed — the leading-slash disambiguation below is exactly
    that question, asked once.

    A folder that hasn't been created yet therefore widens the search instead
    of failing it: the walk simply stops one segment early and folds the
    missing name into the pattern, which the caller matches at whatever base
    it did reach.

    `guard`, when given, is consulted BEFORE `os.path.isdir` on every
    candidate (SPEC-index-search-wedge.md item 1): this walk runs directly
    against the user's raw typed string, segment by segment, and a candidate
    that lands under a wedged NFS/rclone mount would otherwise park this
    thread on `os.path.isdir` forever — the exact failure this repo already
    knows that class of mount can cause (`MountGuard`'s own docstring).

    The check is `guard.blocks()`, deliberately NOT `guard.blocks_root()`:
    `blocks()` is pure string comparison against `MountGuard`'s own
    (once-resolved-at-construction) roots, no syscall at all, whereas
    `blocks_root()` falls through to `mounts.is_mount_backed`, which pays an
    `os.path.realpath` — itself a readlink/lstat per path component — on
    every miss. Calling that per SEGMENT, per KEYSTROKE, on the hot path
    item 5 exists to shave milliseconds off would add back exactly the class
    of blocking syscall this item exists to remove, just relocated from
    `isdir` into the guard. `blocks()` only covers fused's OWN rclone
    mounts/home tree — a wedge in an arbitrary system mount outside that
    tree (an external SMB share, iCloud) cannot be detected here without a
    syscall, and this walk deliberately does not pay one; item 2's bounded
    permit (`ABANDON_S`, routers/index.py) is the backstop for that case,
    not this guard.

    A blocked candidate is treated exactly like one that failed `isdir` (the
    same `break`), never raised: the walk just stops one segment early, the
    same as a folder that doesn't exist yet. The blocked candidate's path is
    also returned (the 4th tuple element, `None` when nothing was blocked)
    so a caller can still answer "this typed path is mount-backed" from a
    string alone, even though `base` itself lands short of it (see
    `resolve_query`'s `blocked_out` parameter, SPEC-index-search-wedge.md
    item C / D-number TBD). `guard=None` (every existing caller) preserves
    today's behaviour unchanged.

    `token`, when given, is checked (`token.check()`, the same
    cooperative-cancellation contract `search_ranked`/`stats`/`search_under`
    already use) once BETWEEN each segment, before that segment's
    `os.path.isdir` call — never during one. `os.path.isdir` itself is an
    ordinary blocking syscall with no way to interrupt it once it has
    started (no threads-within-threads, no signal tricks: this repo already
    accepts that constraint for duckdb's `con.interrupt()`, and a raw
    `isdir` has no equivalent escape hatch at all). So a cancelled token
    only ever stops the walk BEFORE the next segment's `isdir`, bounding the
    damage a wedged mount can do to at most one slow segment instead of the
    remaining N — it cannot make an already-in-flight `isdir` on a hung
    mount return any faster. `token=None` (every existing caller) preserves
    today's behaviour unchanged."""
    base = norm(start).rstrip("/") or "/"
    if not rest:
        return base, "", False, None
    segs = rest.split("/")
    i = 0
    blocked = None
    while i < len(segs) and "*" not in segs[i]:
        if token is not None:
            token.check()
        candidate = base + "/" + segs[i] if base != "/" else "/" + segs[i]
        if guard is not None and guard.blocks(candidate):
            blocked = candidate
            break
        if not os.path.isdir(candidate):
            break
        base = candidate
        i += 1
    return base, "/".join(segs[i:]), i > 0, blocked


_WS_RUN = re.compile(r"\s+")


def expand_whitespace_query(raw: str) -> str:
    """SPEC-search-space-wildcard.md §1: the shared transform both
    `resolve_query` and `search_under` run the raw typed string through
    before anything else — this is what makes a multi-word search find
    `hello-world.txt`/`hello_world.py`/etc for `hello world` without a naive
    `" " -> "*"` substitution regressing the motivating case (see the
    spec's "Anti-goal" section: a plain substitution anchors BOTH ends of
    the filename and loses `hello world.txt` itself).

    Pure string manipulation — no filesystem access — so both callers can
    run it before doing anything path-shaped with the result, and it is one
    place, not two, that has to agree with the spec's grammar.

    0. **Whitespace-only has nothing to search for.** A string that is
       ALL whitespace (`"   "`, including empty after `.strip()`) resolves
       to `""`, the same as an actually-empty query — not to `"*"`. Every
       other rule below treats whitespace as meaningful, but a run of
       spaces with no literal character anywhere is not "a query that
       matches everything typed with a stray wildcard", it is a query with
       nothing in it. Code review finding: the old rule let `"   "`
       collapse into a bare `"*"`, which rule 4 then both-ends-wrapped into
       still just `"*"` — `resolve_query` turned that into `{pattern:
       "**/*", mode: "glob"}`, and `search_ranked`'s `if not qs: return
       {hits: []}` guard (keyed off the RESOLVED pattern, not the raw typed
       string) no longer caught it, so a few spacebar presses answered with
       an arbitrary slab of the corpus. Returning `""` here keeps that
       existing empty-query guard doing its job instead of teaching it a
       second condition.
    1. **No trimming**, otherwise. Leading and trailing whitespace next to
       an actual literal are meaningful — `"icon "` (a trailing space the
       user just typed) is NOT the same query as `"icon"`, and must not
       collapse back to it. (Reversed from an earlier version of this rule,
       which trimmed first specifically to avoid a stray wildcard from a
       trailing space mid-typing; the owner decided a trailing space
       should count.)
    2. A no-op ONLY when `raw` has neither whitespace nor a `*` anywhere —
       this is the one case rule 4 below would have nothing to do (no
       whitespace to collapse, and both ends of the final segment already
       "have" no wildcard to add or skip). A bare word like `report` stays
       byte-for-byte unchanged, still substring mode, still ranked. Once
       either whitespace or `*` is present — even a single trailing space,
       even a whitespace-free `*.pdf` — rule 4's wrap always runs.
    3. Collapse every run of whitespace to `**` — **not** a single `*`
       (reversed by the trailing-space follow-up's own design decision,
       DECISIONS.md worktree-search-trailing-space): a typed space is a
       WIDENING operation, never a narrowing one, so the wildcard it
       inserts has to be the cross-directory token, the same one `**`
       already means everywhere else in this grammar — a single-segment
       `*` cannot cross a `/`, so `src ` (a trailing space) would have
       NARROWED `src`'s matches (losing `srcdir/file.txt`) instead of
       widening them, which is backwards for what typing a space is
       supposed to do. Two spaces still resolve identically to one
       (`hello  world` == `hello world`) — collapsing to `**` doesn't
       change that, it only changes WHICH wildcard the collapse produces.
       This must never STACK a second wildcard directly beside a `*` the
       user already typed adjacent to that whitespace: when the run
       borders a literal `*` on either side, that star already does the
       whitespace run's job, so the run is dropped (replaced with nothing)
       rather than replaced with `**`. Dropping the run can still merge two
       ADJACENT user-typed single stars into a `**`-looking run by simple
       concatenation (`"* *"` -> `"**"`) — accepted, not invented: two
       single-segment wildcards separated only by whitespace are already
       maximally broad on their own, and letting them merge into one
       cross-directory token changes nothing they can match.
    4. On the FINAL `/`-separated segment only: prepend `**` unless it
       already starts with `*`; append a single `*` unless it already ends
       with `*` OR the segment contains a user-typed `*` ANYWHERE in it.
       The two ends are NOT symmetric, on purpose. The leading `**` is what
       turns a typed fragment into "contains, anywhere below this point"
       from the start — it is what fixes the middle case, the same reason
       step 3 inserts `**` — and it is UNCONDITIONAL apart from its own
       "already starts with `*`" guard: an earlier segment's `*` (a
       directory wildcard, `src/*/index`) says nothing about the filename,
       so it never suppresses this segment's own leading wrap.

       The TRAILING wildcard is different: it is what makes an unanchored
       fragment ALSO match a longer name/extension in the SAME folder
       (`report` -> matches `report.pdf.bak` too), and it is now the one
       place a user-typed `*` gets read as an opt-in signal, not just a
       literal. REVERSED from an earlier version of this rule (DECISIONS.md
       worktree-search-trailing-space, "reverse the icon*copy equivalence"):
       a `*` typed ANYWHERE in the final segment — not only at its very
       end — now suppresses the trailing append, because a mid-segment `*`
       is the user asking for precision (`*.parquet` should mean "ends with
       .parquet", not "ends with .parquet and then anything else"). This
       means `icon*copy` no longer behaves like `icon copy`: `icon copy`
       still trailing-wraps (no user `*` anywhere) and matches `icon
       copy.png`, but `icon*copy` is now anchored at the end and does NOT.
       `icon*copy*` (a user-typed trailing `*`) still gets no append either
       way and reproduces the pre-reversal behaviour — the capability is
       opt-in, not gone. `*.pdf` keeps its leading `*` and now ALSO keeps no
       trailing wrap (it already carries a `*`), so it means exactly "ends
       with .pdf" and stops matching `notes.pdfx`/`report.pdf.bak`. This
       check is scoped to the FINAL segment's PRE-collapse text only — an
       earlier segment's `*` (a directory-position wildcard) never reads as
       this signal; `src/*/index` still trailing-wraps `index` into
       `**index*` because that `*` lives in an earlier segment. `report*`
       (user's own trailing `*`) already satisfies "ends with `*`" so gains
       only the leading `**`. Earlier segments get the whitespace collapse
       (step 3) but no wrap of their own (`~/My Documents/report` ->
       `~/My**Documents/**report*`, not `~/**My**Documents**/...`).

    Anti-goal, unchanged from the original version of this rule: a literal
    `" " -> "*"` substitution regresses the motivating case (`hello world`
    would become the both-ends-anchored `hello*world`, which does not match
    `hello world.txt` itself). The implied wrap in step 4 is what avoids
    that; do not drop it — but note the trailing half of that wrap is a
    single `*`, not `**` (see above): `hello world` still matches
    `hello world.txt` because the trailing `*` stays within the same
    segment as `world`, which is all that example needs. Whitespace never
    counts as a user-typed `*` for the new step-4 suppression, so this is
    unaffected by the reversal above.

    Known, accepted consequences (confirmed with the user, not bugs): a
    whitespace-free glob still gains a leading `**` (`*.pdf` is not the
    no-op it looks like — see rule 2), but a glob that ALREADY has a `*`
    anywhere in its final segment no longer gains the trailing wrap that an
    earlier version of this rule gave it — that is the reversal itself, not
    a residual gap. The leading wrap deliberately does NOT also match across
    a `/` on the trailing side (`x.pdf/inner/deep.bin`) — the trailing
    wildcard is same-segment-confined, unlike the leading one, and this is
    unchanged by the reversal. Step 3 still inserts `**` for whitespace
    runs, so a multi-word query still widens across directories BETWEEN its
    words the moment it has whitespace (`src file` reaches
    `src/nested/file.txt`) — that is the whole point of the trailing-space
    follow-up (`src ` must match `srcdir/file.txt`, which step 3's
    `**`-for-a-whitespace-run still gives it: the trailing space itself
    collapses to `**`, not the step-4 append) — but the tail past the last
    typed fragment's own end no longer over-widens past the containing
    folder. There is no escape hatch for the leading side; see
    DECISIONS.md.

    A note on what this function no longer guarantees: an earlier version
    asserted "the output never contains `**` unless the input already did"
    — that invariant does not survive step 3/4 now deliberately inserting
    `**` themselves. What DOES still hold: this function never manufactures
    a run of three-or-more consecutive `*` characters that the input didn't
    already have — every insertion it makes is exactly a two-character
    token, and the "already starts/ends with `*`" checks in step 4 (and the
    "already borders a `*`" check in step 3) exist specifically so an
    insertion is never placed directly beside one of the function's OWN
    prior insertions or directly beside a spot that already satisfies the
    check. See `test_expand_whitespace_query_never_invents_a_run_of_three_
    or_more_stars`.

    `frontend/src/apps/explorer/lib/home-search.ts`'s `expandWhitespaceQuery`
    mirrors this function and claims byte-equivalence with it (code review
    finding: that claim used to be untrue for one input). This function's
    `\\s` (Python's, via `re`) does NOT treat U+FEFF (a leading BOM) as
    whitespace — it is Unicode category Cf (format) — but JS's native `\\s`/
    `String.trim()` does; the TS mirror now special-cases that one character
    (`NON_BOM_WS`, a `[^\\S\\uFEFF]` class) so a BOM-prefixed literal takes
    this function's same no-op path on both sides rather than resolving to
    a different mode client-side. See `test_expand_whitespace_query_does_
    not_treat_a_bom_as_whitespace` (this file's tests) and the equivalent
    test in home-search.test.ts."""
    raw = raw or ""
    if raw.strip() == "":
        return ""
    if not _WS_RUN.search(raw) and "*" not in raw:
        return raw
    segments = raw.split("/")
    last = len(segments) - 1
    # Captured BEFORE whitespace collapse: whitespace collapse only ever
    # inserts `**` (a two-char token), it never introduces a bare `*`, so a
    # single `*` found here is always one the user typed themselves, never
    # one this function invented (see rule 4 below).
    final_has_user_star = "*" in segments[last]

    def _collapse_ws(segment: str) -> str:
        def repl(m: "re.Match[str]") -> str:
            start, end = m.span()
            already_starred = (
                (start > 0 and segment[start - 1] == "*")
                or (end < len(segment) and segment[end] == "*")
            )
            return "" if already_starred else "**"
        return _WS_RUN.sub(repl, segment)

    segments = [_collapse_ws(s) for s in segments]
    final = segments[last]
    if not final.startswith("*"):
        final = "**" + final
    # Trailing wrap is suppressed the moment the FINAL segment carries a
    # user-typed `*` anywhere in it (not just at the end) — a mid-segment
    # `*` is the user opting into precision (see DECISIONS.md, reversal of
    # the `icon*copy` == `icon copy` equivalence), so this end is no longer
    # auto-widened just because it doesn't already end with `*`. Scope is
    # the FINAL segment only: a `*` in an EARLIER segment (`src/*/index`)
    # is a directory wildcard and says nothing about the filename, so it
    # must not suppress this segment's own trailing wrap.
    if not (final.endswith("*") or final_has_user_star):
        final = final + "*"
    segments[last] = final
    return "/".join(segments)


def resolve_query(root: str, raw: str, guard: "MountGuard | None" = None,
                   blocked_out: "list | None" = None,
                   token: "CancelToken | None" = None) -> dict:
    """The one place a search box's typed string becomes a `(base, pattern,
    mode)` triple. `root` is the box's own root (home sends the home dir, the
    explorer sends the open folder); `raw` is the string exactly as typed,
    unstripped of anything meaningful — with one exception, below: a LEADING
    run of whitespace in front of a path-escape (`~`, a leading `/`, a
    Windows drive letter, a `..` segment) is stripped before that escape is
    detected, since it carries no meaning for that grammar and would
    otherwise be folded into a `**` token that hides the very prefix being
    looked for. A leading run in front of anything else, and any TRAILING
    run anywhere, stay exactly as typed.

    `guard`, when given, is threaded straight through to every `_walk_from`
    call below — see its docstring. `guard=None` (the default; every test in
    this module) preserves today's unguarded behaviour; the server caller
    (`routers/index.py`'s `_rank_body`) passes a real `MountGuard`.

    `token`, when given, is threaded straight through to every `_walk_from`
    call below the same way — see its docstring for the cancellation
    contract and its honest limit (bounds damage to one in-flight segment,
    cannot interrupt it). `token=None` (the default; every test in this
    module) preserves today's behaviour; the server caller
    (`routers/index.py`'s `_rank_body`) passes the request's real
    `CancelToken`.

    `blocked_out`, when given, is a list this function APPENDS to (never
    replaces) with the candidate path `_walk_from` refused, whenever a walk
    actually blocked one — i.e. `resolve_query`'s return dict shape never
    changes, so every existing caller and every `out == {...}` test in this
    module stays exactly as it was; the extra information is opt-in, via a
    side channel, for the one caller (`routers/index.py`'s `_rank_body`)
    that needs it. This is what lets a typed path like
    `~/.fused-render/branches/x/mounts/bucket/foo` still answer `reason:
    "mount"` (SPEC-index-search-wedge.md item C): the walk stops at the last
    unblocked ancestor, so `base` itself is never under the blocked tree and
    a check against `base` alone would miss it — but the blocked candidate
    is still a plain string, known without any further syscall, so a caller
    can decide "mount" from it directly.

    `raw` is run through `expand_whitespace_query` FIRST, before any of the
    base-splitting below even sees it (SPEC-search-space-wildcard.md) — a
    query with whitespace in it (`hello world`) comes out the other side
    with wildcards already inserted (`**hello**world*`), so everything
    past this point treats it exactly like a query the user typed with `*`
    in it directly. Only a query with NEITHER whitespace nor a `*` anywhere
    is untouched (`report` stays `report`); a whitespace-only query is `""`
    (nothing to search for, not "match everything"); a whitespace-free glob
    like `*.pdf` is NOT untouched any more — it comes out `*.pdf*` — see
    `expand_whitespace_query`'s own docstring for why, including why its
    leading inserted wildcard is the cross-directory `**` token while its
    trailing one is a single segment-confined `*`.

    `mode` is "glob" the moment `raw` (after that expansion) contains a `*`
    anywhere, else "substring" — `?` and `[`/`]` are left as literal
    characters on purpose (spec: people put them in filenames far more often
    than they mean them as patterns), so their presence never flips the
    mode.

    Base resolution: a query starting with `~` or `/` can escape the box's
    own root entirely; a bare relative query with a `..` segment anywhere in
    it escapes upward from the box's root the same way, walked with the same
    `_walk_from`; anything else inherits the root unchanged. `~` expands to
    the home directory and then walks forward (`_walk_from`); a leading `/`
    is genuinely ambiguous and gets its own rule:

    `/*.csv` has to mean "depth 1 under the box root", and `/etc/*/x.conf`
    has to mean the absolute path — both start with `/`. The fix mirrors git,
    where a leading slash is relative to the level the pattern is written at:
    strip the slash and try walking it as an absolute path (`_walk_from("/",
    ...)`); if that walk could not even consume its first segment (no
    filesystem directory backs it, or the first segment is itself a glob),
    it was never an absolute path to begin with, and the leading `/` is read
    instead as the depth-1 anchor it looks like — base stays the box root,
    pattern is `raw` with only the leading slash stripped.

    A Windows drive-letter path (`C:\\` or `C:/`) has no such ambiguity to
    resolve — nothing else starts that way — so it always walks as an escape,
    the same as `~`: `_walk_from` runs from the drive root (`"C:/"`) over the
    rest of the string with backslashes folded to `/` first (the drive
    letter's own separator is native; the walk only ever splits on `/`), and
    whatever it reaches is the base regardless of `_walk_from`'s `advanced`
    flag — there is no bare-root fallback to `root` for this branch the way
    there is for a bare leading `/`.

    The implicit `**/` prefix — a glob with no `/` anywhere searches any
    depth — is decided from `raw` BEFORE any of the base-splitting above, not
    from the leftover pattern. Deciding it after would silently anchor every
    query that escapes to a base: `~/a/b/*.c` types no slash into ITS pattern
    either (`*.c`, once `~/a/b` is peeled off as the base), but `raw` itself
    has three, so no prefix is added and the match stays anchored at
    `~/a/b`'s own depth 1 — exactly the point of the original request this
    rule exists for. Deciding it from the post-split pattern instead would
    have widened that one, and every query like it, to any depth."""
    def _note(blocked):
        if blocked is not None and blocked_out is not None:
            blocked_out.append(blocked)

    raw = raw or ""
    # A LEADING run of whitespace carries no meaning for the path-escape
    # grammar (`~`, a leading `/`, a Windows drive letter, a `..` segment) —
    # unlike a TRAILING run, which must keep counting (that is the bug this
    # whole round's earlier fix was for). Left alone, `expand_whitespace_
    # query` below folds a leading space into a `**` token glued onto the
    # very prefix (`~/`, `/`, `C:`, `..`) the checks further down look for —
    # `" ~/Documents"` becomes `"**~/**Documents**"`, which starts with `*`,
    # not `~`, so the escape is silently missed and the query falls back to
    # a box-relative search that (almost always) matches nothing. Detected
    # and stripped here, BEFORE expansion, only when what is left actually
    # looks like one of those forms — a leading space on a plain query
    # (`" icon"`) is untouched and keeps flipping into glob mode exactly as
    # any other whitespace does (`expand_whitespace_query`'s own grammar);
    # only the path-escape forms are special-cased.
    raw_lstripped = raw.lstrip()
    if raw_lstripped != raw and (
            raw_lstripped == "~"
            or raw_lstripped.startswith("~/")
            or raw_lstripped.startswith("/")
            or _DRIVE_ABS.match(raw_lstripped)
            or any(seg == ".." for seg in raw_lstripped.split("/"))):
        raw = raw_lstripped
    # The implicit `**/` decision below is defined to read the RAW typed
    # string (before any of this function's own mutations) for a `/` —
    # captured here, before the drive-path backslash normalization that
    # follows, so an all-backslash Windows path (no `/` at all, as typed)
    # still widens to any depth exactly as it always has, rather than
    # picking up a "/" this function itself inserted.
    raw_had_slash = "/" in raw
    if _DRIVE_ABS.match(raw.strip()):
        # A Windows drive path's own separator is "\", never "/" — normalize
        # it to "/" BEFORE `expand_whitespace_query` runs, so that function's
        # own "/"-segment split (and its "wrap only the FINAL segment" rule)
        # actually sees the path's real segments (code review finding: doing
        # this AFTER expansion left the whole path as one opaque segment,
        # `raw`'s leading `C:\` got swallowed into the wrap's own `*` prefix,
        # and `_DRIVE_ABS.match(raw)` below stopped matching at all — a
        # drive path with a space, e.g. `C:\My Files\rep`, silently stopped
        # resolving as an absolute path). `_DRIVE_ABS` only tests the drive
        # PREFIX (`[A-Za-z]:[\\/]`), so this is safe to do unconditionally
        # once that prefix is confirmed present — nothing past it is a POSIX
        # path where a literal backslash would be a legal filename character
        # this might mangle.
        raw = raw.replace("\\", "/")
    raw = expand_whitespace_query(raw)
    is_glob = "*" in raw
    if raw == "~" or raw.startswith("~/"):
        home = norm(os.path.expanduser("~"))
        rest = raw[2:] if raw.startswith("~/") else ""
        base, pattern, _, blocked = _walk_from(home, rest, guard=guard, token=token)
        _note(blocked)
    elif _DRIVE_ABS.match(raw):
        drive_root = raw[:2] + "/"
        rest = raw[3:].replace("\\", "/")
        base, pattern, _, blocked = _walk_from(drive_root, rest, guard=guard, token=token)
        _note(blocked)
        # A bare drive letter with nothing after it (`rest == ""`) leaves
        # `_walk_from` at its own bare-root collapse, `"C:"` — the same
        # bare spelling `_root_or_bare` exists to restore to `"C:/"`
        # (this module's own `_BARE_DRIVE` comment), so the canonical form
        # matches what `canonical_root()` (index/runner.py) actually stores
        # this drive under.
        base = _root_or_bare(base.rstrip("/"))
    elif raw.startswith("/"):
        rest = raw[1:]
        abs_base, abs_pattern, advanced, blocked = _walk_from("/", rest, guard=guard, token=token)
        _note(blocked)
        if advanced:
            base, pattern = abs_base, abs_pattern
        else:
            base, pattern = root, rest
    elif any(seg == ".." for seg in raw.split("/")):
        # A bare query with a `..` segment walks out of `root` the same way
        # `~` and a leading `/` already do: `_walk_from` advances one
        # directory at a time via `os.path.isdir`, which resolves a literal
        # `..` component exactly as the filesystem would, so it already
        # climbs correctly without any extra case for it — the walk simply
        # never manufactures a segment that isn't real. `os.path.normpath`
        # afterward is cosmetic (it turns "root/.." into the clean parent
        # path instead of leaving the literal ".." embedded in `base`); it
        # is also what keeps a run of `..` past the filesystem root pinned
        # at that root instead of growing an ever-longer trail of ".." that
        # still, harmlessly, means the same directory.
        walked_base, pattern, _, blocked = _walk_from(root, raw, guard=guard, token=token)
        _note(blocked)
        base = norm(os.path.normpath(walked_base)).rstrip("/") or "/"
    else:
        base, pattern = root, raw
    if is_glob and not raw_had_slash:
        pattern = "**/" + pattern
    return {"base": base, "pattern": pattern,
            "mode": "glob" if is_glob else "substring"}


# Turns a glob pattern into a regex full-matched against `lrel` (both already
# lowercased by the caller) — never DuckDB's own `GLOB` operator, whose `*`
# crosses `/`, which is the one thing this translation depends on not
# happening. `**/ ` collapses to zero-or-more whole segments so it can match
# nothing (`**/*.csv` reaching a root-level file), `**` alone spans
# separators unrestricted (mid-pattern, not just as a whole segment), and a
# lone `*` is confined to one segment. `?` and `[`/`]` are NOT wildcards here
# (spec: people put them in filenames far more often than they mean them as
# patterns) — like every other character, they fall through to the literal
# branch and get regex-escaped.
def _glob_to_regex(pattern: str) -> str:
    out = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]*/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif pattern[i] == "*":
            out.append(r"[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "^" + "".join(out) + "$"


def query_wants_hidden(raw_query: str) -> bool:
    """A dot-leading query segment is explicit intent to SEE hidden entries.

    That makes ".py" work as an extension search without a second pass, and
    "env" deliberately not surface ".env". Moved here from the now-deleted
    index/rank.py, which this module was its only importer of."""
    q = raw_query.strip()
    return q.startswith(".") or "/." in q


# `query.py` used to import this as `_wants_hidden` from `index/rank.py`.
_wants_hidden = query_wants_hidden


def is_hidden_rel(rel: str) -> bool:
    """An entry is hidden when any path segment is dot-leading. Moved here
    from the now-deleted index/rank.py."""
    return rel.startswith(".") or "/." in rel


def _name_predicate_sql(nm_col: str, literals: list) -> dict:
    """Three position-free boolean predicates over `nm_col` (always `"nm"` in
    practice — the already-lowercased basename column both `inner` branches
    produce) built from `literals`, an ORDERED list of raw (un-escaped)
    literal strings: `_rank_sql` passes the single-element `[qs]` (the whole
    substring query IS one literal run), `_glob_sql` passes
    `_glob_literal_runs(pattern)`'s multi-element output. This is the ONE
    place either caller reads a literal against `nm` at all — sharing it is
    what keeps the two modes from drifting the way `_rank_sql` and the
    deleted `_glob_score_sql` used to (each re-implementing "name bonus"
    separately, kept equal only by a test).

    - `prefix`: `nm` starts with the FIRST literal run.
    - `suffix`: `nm` ends with the LAST literal run.
    - `contains`: every literal run occurs in `nm`, IN ORDER, with anything
      (including nothing) between them — `nm LIKE '%lit0%lit1%...%litN%'`.
      For a single literal this is exactly "the query is a substring of the
      basename" (the old tier-1 test). For multiple literals this is what
      replaces testing the WHOLE compiled regex against `nm`
      (`query.py:1471` before this round) — the old test could never be true
      for a path-shaped pattern like `**/src/*.ts`, because `_glob_to_regex`
      compiles `**/ ` and `**` into `/`-crossing wildcards that a slash-free
      `nm` can never satisfy, so every such glob was tier-3 for every hit
      (search-architecture-review.md §10.3). This predicate does not care
      what KIND of wildcard separated the literals in the original pattern —
      it only asks whether `nm` alone could satisfy an in-order,
      anything-between reading of them — so it comes back true exactly when
      the pattern's literal content is fully explainable by the basename,
      independent of how much of the pattern was directory-crossing syntax.
      **`_glob_sql` no longer passes every literal run in the WHOLE pattern
      here** — see `_final_segment_pattern`'s docstring for why only the
      literals that can legally live in the final path segment are passed,
      which is what actually fixes the `**/src/*.ts` defect the paragraph
      above describes (a `contains` test built from `["src/", ".ts"]` could
      never be true against a slash-free `nm` either; one built from `[".ts"]`
      alone can).
    - `boundary`: `nm` contains the FIRST literal run immediately preceded by
      either the start of the string or a non-alphanumeric character —
      `regexp_matches(nm, '(^|[^a-z0-9])' || <literal, regex-escaped>)`. A
      match sitting right after a separator (`zz_config.py`'s "config",
      right after `_`) is a stronger signal than the identical literal
      buried mid-word (`aaaconfig.py`'s "config", right after another
      letter) — the two otherwise tie on every column above AND on
      `depth`/`length(nm)` whenever the candidates happen to share both, and
      fall through to `lower(rel) ASC`, which orders by SPELLING, not
      quality (reproduced: `aaaconfig.py` beat `zz_config.py` for query
      "config" purely because `'a' < 'z'`). This is position-free (a boolean
      existence test, like every other column here) and reads no case
      information — `nm` is already lowercased, so this is NOT the dropped
      camelCase hump bonus (that needed ORIGINAL case to detect a hump;
      finding a separator needs none). The literal is regex-escaped
      (`re.escape`, not `like_literal`'s LIKE-metachar escaping) because it
      is embedded in a `regexp_matches` pattern, not a `LIKE` one — an
      unescaped `.` or `(` in the query would otherwise be read as "any
      character" or "start a group" instead of the literal character a user
      typed.

      Before testing for a separator, any leading non-alphanumeric run is
      stripped off the literal itself (code review finding 6) — without
      this, a literal that already STARTS with punctuation (an extension
      glob's final-segment literal, e.g. `.pdf` or `.ts` from `*.pdf`/
      `*.ts`) made the predicate structurally dead: the regex demanded a
      separator immediately before the literal, i.e. before its own leading
      `.`, which for an ordinary file is the character right before the
      extension's dot (`report.pdf`'s `t`) — any alnum basename character,
      never a separator, so this was false for essentially every real
      extension match. Stripping the leading punctuation before building the
      regex moves the separator test to right before the literal's
      alphanumeric CORE (`pdf`, not `.pdf`) — and the stripped-off `.` itself
      already satisfies "non-alphanumeric," so an ordinary extension match
      (`report.pdf`) now correctly reads as boundary-true. A literal with no
      leading punctuation at all (`config`) is unaffected — there is nothing
      to strip, so this is the exact same regex as before for every
      non-extension case, including the `zz_config.py`/`aaaconfig.py` case
      directly above.

    None of these read a position (no `strpos`, no `p0`): `LIKE`/
    `regexp_matches` answer "does this pattern exist anywhere" without
    exposing WHERE, which is exactly the occurrence-independence the
    position-free redesign needs.

    `literals=[]` (a literal-free glob, e.g. `**/*`) returns four `"false"`
    literals rather than raising or dividing by anything — `_glob_sql`
    never calls this for that case (it takes the fully-unscored branch
    instead), but the function stays total rather than assuming its own
    caller's discipline.

    A fifth, non-SQL entry, `"suffix_before_prefix"`, is a plain Python bool:
    true when the LAST literal run starts with a non-alphanumeric extension
    marker (`.`) — an extension-shaped query (`.js`, or `*.js` -> `.js` as
    the final-segment literal `_glob_sql` passes). Reported defect: searching
    `.js` returned dotfiles like `.jshintrc` ABOVE the real `script.js`,
    because `.jshintrc` satisfies the basename PREFIX predicate
    (`nm LIKE '.js%'`) while `script.js` only satisfies the basename SUFFIX
    predicate (`nm LIKE '%.js'`), and prefix outranks suffix in the
    unmodified vector — an accident of the dotfile's OWN leading dot, not a
    better match. For a dot-leading literal the user means "this extension",
    i.e. a suffix, so `_lex_order_and_score` swaps prefix and suffix (in both
    `order_by` and `score`) when this flag is set. Keyed on the LAST element
    (the one `suffix` itself is built from, `literals[-1]`) rather than the
    first, so both callers get it right: `_rank_sql` passes the single-
    element `[qs]` (first == last), and `_glob_sql` passes the final
    segment's literal runs (`*.js` -> final segment `.js*`... -> `['.js']`,
    still first == last for a single-extension pattern, but keyed on the
    element `suffix` actually reads, not an assumption that they always
    coincide)."""
    if not literals:
        return {"prefix": "false", "suffix": "false", "contains": "false",
                "boundary": "false", "suffix_before_prefix": False}
    first_like = like_literal(literals[0])
    last_like = like_literal(literals[-1])
    chain_like = "%".join(like_literal(lit) for lit in literals)
    # `boundary`'s literal is the first run with its own leading
    # non-alphanumeric characters stripped (code review finding 6): a literal
    # that already starts with punctuation (an extension glob's ".pdf"/".ts")
    # would otherwise need a SEPARATE separator before that punctuation too,
    # which an ordinary basename never has (the character before an
    # extension's dot is a plain alnum basename character) — see this
    # function's own docstring on `boundary`. Falls back to the untouched
    # literal when stripping would leave nothing (a literal made ENTIRELY of
    # punctuation), since an empty core would make the regex's separator
    # clause match almost anywhere.
    _boundary_core = re.sub(r"^[^A-Za-z0-9]+", "", literals[0]) or literals[0]
    first_re = _q(re.escape(_boundary_core))
    return {
        "prefix": f"{nm_col} LIKE lower('{first_like}') || '%' ESCAPE '\\'",
        "suffix": f"{nm_col} LIKE '%' || lower('{last_like}') ESCAPE '\\'",
        "contains": (f"{nm_col} LIKE '%' || lower('{chain_like}') || '%' "
                     f"ESCAPE '\\'"),
        "boundary": (f"regexp_matches({nm_col}, "
                     f"'(^|[^a-z0-9])' || lower('{first_re}'))"),
        "suffix_before_prefix": literals[-1].startswith("."),
    }


# Strictly less than the smallest gap between adjacent `score` levels.
# There are five levels, not four: 0 (no predicate holds), then
# 100/250/500/1000 (contains/suffix/prefix/nm_exact). The binding gap is
# between 0 and 100 (100), NOT between two of the four named weights (the
# next-smallest gap, 250-100, is 150) — the worst case this cap has to
# survive is a row with ONLY `contains` true (score contribution 100) against
# a row with NOTHING true (score contribution 0). 99 is strictly less than
# that 100, so `- LEAST(depth, _SCORE_DEPTH_CAP)` can never push a row with
# SOME predicate true below a row with NONE true, at any depth — see
# `_lex_order_and_score`'s docstring (Finding 4).
_SCORE_DEPTH_CAP = 99

# At most this many rows sharing one basename (`nm`) survive into a single
# response. Reported defect: once a run of same-named files (fifteen
# `.jshintrc`, one machine-generated tree's worth) tied on every predicate
# column above, the remaining tie-breaks (`depth`, `length(nm)`, `rel`)
# CLUSTERED identical basenames together instead of spreading results across
# distinct names — one such tree could fill the entire visible response with
# copies of the same filename. Applied via a `QUALIFY row_number() OVER
# (PARTITION BY nm ORDER BY <the branch's own order_by>) <= _MAX_PER_BASENAME`
# clause (`_qualify_basename_cap`, below) in EVERY branch that emits an
# `ORDER BY ... LIMIT` (`_rank_sql`'s both branches, `_glob_sql`'s both
# branches) — placed BEFORE that `ORDER BY ... LIMIT` in the same statement,
# since DuckDB evaluates `QUALIFY` ahead of the outer `ORDER BY`/`LIMIT`, so
# capping happens before the row count is decided rather than after (capping
# AFTER `LIMIT` would silently return fewer rows than the caller's `limit`
# asked for, since the trimmed rows have already left the result set).
_MAX_PER_BASENAME = 3


def _qualify_basename_cap(order_by: str) -> str:
    """The shared `QUALIFY` fragment both `_rank_sql` and `_glob_sql` splice
    into their `ORDER BY ... LIMIT` tail. `order_by` MUST be the exact same
    vector the statement's own `ORDER BY` uses — not merely a similar one —
    so the `_MAX_PER_BASENAME` rows this keeps per `nm` are the best `nm`
    has to offer BY THE STATEMENT'S OWN ORDERING, not an arbitrary subset a
    differently-ordered window would have kept instead. Partitions on `nm`,
    the same lowercased basename column every predicate in
    `_name_predicate_sql`/`_lex_order_and_score` already reads."""
    return (f"QUALIFY row_number() OVER "
            f"(PARTITION BY nm ORDER BY {order_by}) <= {_MAX_PER_BASENAME} ")


def _lex_order_and_score(nm_exact: str, preds: dict) -> tuple:
    """The shared tail of both `_rank_sql` and `_glob_sql`: given `nm_exact`
    (a mode-specific SQL boolean — `nm = lower(q)` for a substring query,
    `regexp_matches(nm, regex)` for a glob, since only the CALLER knows how
    to test "does the whole pattern hold within the basename alone") and
    `preds` (`_name_predicate_sql`'s output), returns `(order_by, score,
    tier)`.

    `order_by` is the position-free, basename-first lexicographic vector
    search-architecture-review.md §6 recommends: exact name, then basename
    prefix, then basename suffix, then "the query is satisfiable within the
    basename alone", then a word/segment-BOUNDARY test (`preds['boundary']`
    — a match right after a separator outranks the identical literal buried
    mid-word; see `_name_predicate_sql`'s docstring on why this is still
    position-free and does not resurrect the dropped camelCase hump), THEN
    `depth`/`length(nm)`/`rel` as pure tie-breaks. This is what actually
    decides the row order — `ORDER BY` reads it as a vector comparison,
    column by column, which is exactly the field-separated ranking VS
    Code/Zed do structurally (their power-of-two score bands ARE this same
    lexicographic order, just encoded as one integer instead of left as a
    column list).

    `score` is a WEIGHTED SUM of the same four leading predicates (NOT
    `boundary` — it stays a coarse four-term summary, and `boundary` only
    ever breaks a tie the sum already reports as equal, so adding a fifth,
    smaller-weighted term would buy the display value nothing a reader
    couldn't already get from `tier` alone), returned for compatibility
    (`search_ranked` still emits it on every hit — existing tests and probes
    read it) and for human debugging, but it is NOT what decides the order:
    two hits can tie on `score` while the vector still orders them (a
    `boundary`/`depth`/`length(nm)`/`rel` tie-break the scalar sum cannot
    see), so nothing downstream may assume `ORDER BY score DESC` reproduces
    this function's actual order. The weights (1000/500/250/100) are spaced
    so each level dominates every combination of the levels below it.

    `- depth` is CAPPED at `_SCORE_DEPTH_CAP` (99), not subtracted
    unbounded, so `score` cannot invert the real order at depth the way an
    unbounded subtraction did (reproduced: a `contains`-only match at depth
    601 scored `100 - 601 = -501`, BELOW a same-tier-0 ancestor-only match
    at depth 2 scoring `0 - 2 = -2`, even though the real `ORDER BY` vector
    — which `tier`/`contains` alone already separates — ranks the basename
    match first; a debug field that inverts the true order at depth is worse
    than no debug field). 99 is smaller than every gap between adjacent
    score LEVELS — there are five, not four: 0 (nothing matched), then
    100/250/500/1000; the binding gap is between 0 and 100 (100), not
    between two of the four named weights — so a row with SOME predicate
    true (worst case: `contains` only, weight 100, any depth) always
    outscores a row with NONE true (best case: depth 0, score 0) —
    `100 - 99 = 1 > 0`. This bounds `score`'s depth-driven error, it does not
    make `score` monotonic with the full vector in general (`boundary` and
    `length(nm)` still are not reflected in it at all) — `score` remains a
    coarse display value, documented as such, not a second ranking
    mechanism.

    `tier` collapses to two values, not the old three: 1 when `contains`
    holds (the match is fully explainable within the basename — subsumes the
    old tier 1, and folds the old "straddles the directory/basename
    boundary" tier 2 into "ancestor-only" here, since `contains` cannot
    distinguish a straddle from a pure-ancestor hit without reading a
    position), else 3. `_rank_sql`'s docstring below and DECISIONS.md record
    this as a deliberate simplification, not an oversight: `tier` is no
    longer a primary sort key at all (the four DESC columns ahead of it in
    `order_by` already separate name matches from ancestor-only ones more
    finely than tier alone ever did), so the three-way split it used to make
    has nothing left to earn its keep for — it is kept as a coarse,
    wire-compatible summary field, not a ranking mechanism.

    `preds["suffix_before_prefix"]` (see `_name_predicate_sql`'s docstring)
    swaps `prefix` and `suffix` in BOTH `order_by` and `score` for an
    extension-shaped literal (`.js`, ...): the predicate that would otherwise
    rank second (`suffix`) takes the FIRST slot and its weight (500), and the
    one that would otherwise rank first (`prefix`) takes the second slot and
    its weight (250) — every other column (`contains`, `boundary`,
    `depth`/`length(nm)`/`rel`) keeps its exact position, and the weight
    SCALE (1000/500/250/100, `_SCORE_DEPTH_CAP`) is untouched, so `score`
    stays coherent with whichever order `order_by` actually emits rather than
    contradicting it."""
    tier = f"CASE WHEN ({preds['contains']}) THEN 1 ELSE 3 END"
    if preds.get("suffix_before_prefix"):
        hi_pred, lo_pred = preds["suffix"], preds["prefix"]
    else:
        hi_pred, lo_pred = preds["prefix"], preds["suffix"]
    # DuckDB has no BOOLEAN*INTEGER overload (unlike Python's `True == 1`) —
    # each predicate is cast to INTEGER before it can be weighted and summed.
    score = (f"1000 * CAST({nm_exact} AS INTEGER) "
             f"+ 500 * CAST({hi_pred} AS INTEGER) "
             f"+ 250 * CAST({lo_pred} AS INTEGER) "
             f"+ 100 * CAST({preds['contains']} AS INTEGER) "
             f"- LEAST(depth, {_SCORE_DEPTH_CAP})")
    order_by = (f"({nm_exact}) DESC, ({hi_pred}) DESC, "
                f"({lo_pred}) DESC, ({preds['contains']}) DESC, "
                f"({preds['boundary']}) DESC, "
                f"depth ASC, length(nm) ASC, lower(rel) ASC, rel ASC")
    return order_by, score, tier


def _rank_sql(inner: str, hidden: str, ql: str, qq: str, qs: str, limit: int,
              ranked: bool = True) -> str:
    """The whole rank query: substring filter, scoring, and ORDER BY ... LIMIT,
    all in SQL — no candidate cap, no Python-side pass.

    `ranked=False` (the owner's unranked-results preference, D720) keeps the
    exact same `WHERE lrel LIKE ...` substring filter and hidden-file handling
    below, but drops the entire scoring apparatus — no predicate columns, no
    `score`, no `tier` — computed nowhere, not computed-then-discarded. It
    orders `depth ASC, rel ASC` instead: `depth` here is `rel_depth`, the
    same ROOT-RELATIVE depth the ranked branch computes in `inner` (see
    `search_ranked`'s docstring on `rel_depth` for why the parquet's own
    stored absolute `depth` column would be the wrong one — the ranked branch
    had a real bug from exactly that mix-up before it was fixed). This
    mirrors `search_under`'s own `ORDER BY depth, path` above — the ordering
    the owner's user confirmed is usable. `depth, rel` is a TOTAL order as
    long as `rel` is unique, which it is: a file and a directory cannot share
    a path on a real filesystem, and both parquet stores are keyed on that
    same path uniqueness — the ranked branch's own final tie-break (`rel
    ASC`, after the lexicographic predicate vector/depth/length(nm)/
    lower(rel)) already leans on this identical fact. No `lower(rel)` ahead
    of `rel` here — unlike the ranked branch, which needs it as an
    intermediate tie-break before `rel ASC` because ties can survive the
    predicate vector/depth/length(nm) — the unranked branch's ORDER BY has
    only two keys and `rel` alone already makes it total, so a `lower(rel)`
    in front would reintroduce a case-insensitive ordering question for no
    benefit (see D712 on the ranked branch's own `lower(rel)`/`rel` split for
    the class of bug that guards against, which does not apply to a two-key
    order that is already total).

    `inner` is the UNION ALL of the files/dirs branches (each already carries
    `rel`, `size`, `mtime`, `is_dir`, `depth` — RELATIVE to the search root,
    `search_ranked`'s `rel_depth` — and `nm`, the lowercased basename,
    `_name_col`'s doing) plus `lrel` (`lower(rel)`). `ql` is the ORIGINAL-case
    `qs` as a LIKE literal (metachars escaped, use with ESCAPE '\\'); `qq` is
    the same original-case string as a plain SQL string literal (quotes
    doubled only) for the `nm = lower(qq)` exact-match test, which is not
    LIKE and must not see LIKE's escapes. Every comparison against `ql`/`qq`
    below wraps them in SQL's own `lower(...)` rather than lowering in Python
    first — see the paragraph below for why. `qs` is the original-case query
    string itself, passed through to `_name_predicate_sql` as the single
    literal run a substring query is.

    Ported (then substantially rewritten — see the position-free redesign
    note above this function) from the deleted index/rank.py's `fuzzy_match`
    substring branch, `_is_segment_start`, `_name_tier` and `_sort_key` — see
    that module's own history for the fuzzy-subsequence half this replaces,
    and search-architecture-review.md §6/§10.1/§10.3 for why the position-
    based scoring this function used to compute (`p0 = strpos(lrel, qq) - 1`,
    `segment_starts`, `tail_bonus`) was replaced rather than kept.

    The query is lowercased IN SQL, with the same `lower()` call that already
    produces `lrel` — not in Python (`qs.lower()`) before being embedded as a
    literal. The two used to disagree: Python's `str.lower()` and DuckDB's
    `lower()` don't always fold the same character the same way — U+0130
    ('İ') folds to TWO Python characters ('i' + a combining dot, U+0307) but
    to plain 'i' in DuckDB — so a query lowered in Python could never appear
    as a substring of a `rel` DuckDB lowered, even on an exact-letter match a
    user would expect to work. Deferring both sides' lowering to the SAME
    function makes them agree by construction, whichever way `lower()`
    happens to fold any given character; `rank.py` made the Python-side
    assumption alone (a `KNOWN, deliberate` divergence noted in its own
    docstring, about `lower()` not preserving character OFFSETS — a related
    but different Unicode wrinkle from this one, about the two sides FOLDING
    a character differently in the first place), which this rewrite no
    longer needs to inherit now that both sides are one implementation.

    Per matched row, `_name_predicate_sql("nm", [qs])` builds the three
    position-free basename predicates (`prefix`/`suffix`/`contains` — for a
    single-literal substring query, `contains` is exactly "the query occurs
    somewhere in the basename", the old tier-1 test) and `_lex_order_and_score`
    combines them with `nm = lower(qq)` (the exact-basename test) into the
    final `order_by`/`score`/`tier`. See both functions' own docstrings for
    the full column set and why `score` is display-only.

    Final order comes from `_lex_order_and_score`'s `order_by` — see its
    docstring for the full vector and the trailing `rel ASC`'s role as what
    makes this a TOTAL order (a pair equal under every earlier column,
    including `lower(rel)` — two rels differing only in case, e.g.
    `notes/Alpha.txt` vs `notes/alpha.txt` — was otherwise still an
    unresolved tie, and DuckDB's multi-threaded top-N is free to resolve an
    unresolved tie arbitrarily, so the pair could silently swap order between
    two runs of the identical query and shift keyboard selection out from
    under a user who hadn't typed anything). `rel` (byte/ASCII comparison,
    not `lower(rel)`) breaks that tie for free — it costs nothing beyond a
    column DuckDB already has in hand — and always resolves it the same way.
    This makes the SQL side deterministic ON ITS OWN, but not necessarily
    identical to `frontend/src/platform/lib/fuzzy.ts`'s tie-break: the JS
    ranker's is `Intl.Collator(sensitivity: "base")`, which is locale-aware
    and does not always agree with a plain ASCII byte comparison on which of
    a case-only pair sorts first. That divergence is pre-existing and is
    unaffected by `rel ASC` here — it only fixes SQL's OWN run-to-run
    stability, not cross-language agreement."""
    if not ranked:
        # No predicate columns, no `score`, no `tier` — the scoring apparatus
        # below is never built for this branch, not built and then left out
        # of the SELECT list.
        unranked_order = "depth ASC, rel ASC"
        return (
            f"SELECT rel, size, mtime, is_dir, depth FROM ({inner}) "
            f"WHERE lrel LIKE '%' || lower('{ql}') || '%' ESCAPE '\\'{hidden} "
            f"{_qualify_basename_cap(unranked_order)}"
            f"ORDER BY {unranked_order} "
            f"LIMIT {limit}")
    preds = _name_predicate_sql("nm", [qs])
    order_by, score, tier = _lex_order_and_score(f"nm = lower('{qq}')", preds)
    return (
        f"SELECT rel, size, mtime, is_dir, depth, ({score}) AS score, "
        f"({tier}) AS tier FROM ({inner}) "
        f"WHERE lrel LIKE '%' || lower('{ql}') || '%' ESCAPE '\\'{hidden} "
        f"{_qualify_basename_cap(order_by)}"
        f"ORDER BY {order_by} "
        f"LIMIT {limit}")


def _glob_literal_runs(pattern: str) -> list:
    """`pattern`'s literal pieces, in order — everything `_glob_to_regex`
    would emit as `re.escape(...)` text rather than a wildcard, split at
    every wildcard token. Walks `pattern` with the EXACT SAME three-way
    tokenizer `_glob_to_regex` uses (`**/ ` as one three-character token,
    then bare `**`, then a lone `*`) rather than a simpler `re.split(r"\\*+",
    pattern)` — the naive split gets `"**/*"` wrong: it would report a
    literal `"/"` run between the two star groups, but `**/ ` is a SINGLE
    wildcard token that can match ZERO segments (`_glob_to_regex`'s
    `(?:[^/]*/)*`), so `"**/*"` can match `"a.txt"` at the root with no
    literal `/` anywhere in the matched string at all — a pattern that is
    ALL wildcard tokens (`"**/*"`, what a bare `*` query resolves to) must
    come back `[]`, not `["/"]`.

    `"**/**icon**copy**"` -> `["icon", "copy"]`: the leading `"**/"` and
    `"**"` are both wildcard tokens, contributing nothing.

    This is the ONLY thing a glob hit has to score against: a glob match has
    no single contiguous "substring position" the way a `_rank_sql` hit does
    (that hit's own `qs` never contains a wildcard), so `_glob_sql` passes
    this list straight to `_name_predicate_sql` (the same basename predicate
    builder `_rank_sql` uses) rather than locating each run's position
    separately. An empty return means there is nothing to test against `nm`
    at all; `_glob_sql` treats that identically to `ranked=False`.

    `search_ranked` calls this on `_final_segment_pattern(qs)`, NOT on `qs`
    itself — see that function's docstring for why: the whole pattern's
    literal runs can include directory-segment text (`"src/"` from
    `**/src/*.ts`) that can never appear in `nm`, a slash-free basename, so
    scoring off them left every path-shaped glob stuck at tier 3 regardless
    of match quality (search-architecture-review.md §10.3, fixed this
    round)."""
    out = []
    cur = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            step = 3
        elif pattern.startswith("**", i):
            step = 2
        elif pattern[i] == "*":
            step = 1
        else:
            cur.append(pattern[i])
            i += 1
            continue
        if cur:
            out.append("".join(cur))
            cur = []
        i += step
    if cur:
        out.append("".join(cur))
    return out


def _final_segment_pattern(pattern: str) -> str:
    """The sub-pattern of `pattern` after its LAST directory-crossing
    boundary — what `_glob_sql` tests against `nm` (a slash-free basename)
    instead of the whole pattern (search-architecture-review.md §10.3, the
    tier defect this closes).

    `_glob_literal_runs(pattern)` used to be called on the WHOLE pattern and
    handed straight to `_name_predicate_sql`, which builds `contains` etc.
    against `nm`. For a path-shaped pattern like `**/src/*.ts`, that walks
    away with literal runs `["src/", ".ts"]` — `"src/"` is a directory-
    segment literal, and `nm LIKE '%src/%.ts%'` can never be true against a
    basename that by definition has no `/` in it, so `contains` (and every
    predicate built on it) was false for EVERY hit of a path-shaped glob no
    matter how good the basename match was, and tier was stuck at 3
    uniformly. This function's return value is what `search_ranked` now
    feeds `_glob_literal_runs` (and a matching `_glob_to_regex` call for
    `nm_exact`) instead of the raw pattern, so only the final segment's own
    literal content — the part that could ever appear in `nm` — is tested.

    Only two things count as a hard directory-crossing boundary: a literal
    `/` character, and the three-character `**/ ` wildcard token (it
    resolves to `(?:[^/]*/)*`, an entire zero-or-more chain of `dir/`
    segments — crossing it, min zero times, still lands past the last `/` a
    matched string could have). A BARE `**` token (two characters, no
    trailing slash) is NOT a boundary: it resolves to `.*`, which can span
    a `/` OR match nothing WITHIN a single segment, and treating it as a
    hard cut would wrongly truncate already-correct single-segment patterns
    like `**icon**copy**` (no directory literal anywhere in it) down to
    `"copy**"`, losing the `icon` literal `_glob_literal_runs` correctly
    extracts from it today. Walking with the SAME three-way tokenizer
    `_glob_to_regex`/`_glob_literal_runs` use (rather than `pattern.rsplit
    ("/", 1)`, which cannot tell a literal `/` from a `**/ ` token's own
    `/`) keeps all three functions agreeing on where the wildcard tokens
    are.

    `"**/src/*.ts"` -> `"*.ts"` (the leading `**/ ` is the last boundary).
    `"**/src/*.ts**"` -> `"*.ts**"` (trailing `**` is not a boundary itself,
    but nothing after the `**/ ` crosses one either, so it stays attached).
    `"**/**icon**copy**"` -> `"**icon**copy**"` (identical to today's
    behavior on this pattern — no directory literal, no regression: the
    leading `**/ ` is the only boundary, everything after it is one
    segment, and `_glob_literal_runs` extracts the same `["icon", "copy"]`
    either way).
    `"**/alpha/*"` -> `"*"` (zero literal runs post-split: an ancestor-only
    glob has nothing to test against `nm` and correctly stays tier 3 —
    `test_glob_final_segment_tier_ancestor_only_stays_tier_3`)."""
    i, n = 0, len(pattern)
    boundary = 0
    while i < n:
        if pattern.startswith("**/", i):
            i += 3
            boundary = i
        elif pattern.startswith("**", i):
            i += 2
        elif pattern[i] == "*":
            i += 1
        else:
            if pattern[i] == "/":
                boundary = i + 1
            i += 1
    return pattern[boundary:]


def _glob_sql(inner: str, regex: str, hidden: str, limit: int,
              literals: list = (), nm_regex: str = None,
              score: bool = None) -> str:
    """Glob mode's whole query: a full-match regex filter, plus — when
    `literals` is non-empty — the scoring `_name_predicate_sql`/
    `_lex_order_and_score` build from those literal runs, the SAME two
    functions `_rank_sql` uses. There is no glob-specific scoring code left
    at all (the deleted `_glob_score_sql` — a `strpos`-chained, per-run
    position walk — was the OTHER half of the first-occurrence bug class
    this round eliminates, and duplicated `_rank_sql`'s own per-match terms
    by hand, which is exactly the drift risk sharing the helpers removes).

    `literals=()` (the default) is what `search_ranked` passes in TWO
    distinct situations, and — absent an explicit `score` override — this
    function treats them identically because they need the identical
    answer: the caller's own `ranked=False` preference (D720, same contract
    `_rank_sql`'s unranked branch has: no predicate columns, no `score`, no
    `tier` computed at all, not computed then discarded), and a glob
    pattern whose FINAL SEGMENT has no literal run to score
    (`_final_segment_pattern`'s docstring — `**/*`, what a bare `*`
    resolves to) — there is nothing `_name_predicate_sql` could test
    (`literals=[]` degenerates to "false" for all four predicates, which
    would rank every row identically rather than being an honest "nothing
    to rank here"). Both land on the SAME order `_rank_sql`'s own unranked
    branch uses and for the same reason: `depth ASC, lower(rel) ASC, rel
    ASC` — `rel` alone (after `lower(rel)`) is what makes a case-only-
    differing pair (`notes/Alpha.txt` vs `notes/alpha.txt`) resolve the
    same way on every run instead of however DuckDB's multi-threaded top-N
    happens to land.

    `score` (default `None`, meaning "follow `literals`": score iff
    `literals` is non-empty) exists for exactly ONE case where those two
    situations above are NOT the same: a pattern whose WHOLE literal
    content is non-empty but whose FINAL segment's is empty, e.g.
    `**/alpha/*` (final segment: a bare `*`, no literal at all — "alpha" is
    purely a directory-ancestor requirement, never tested against `nm`).
    Before `_final_segment_pattern` existed, `_glob_literal_runs` ran on the
    WHOLE pattern for such cases too and returned `["alpha/"]` — non-empty
    — so this went through the SCORED branch with `contains` forced false
    (a directory literal can never match a slash-free `nm`), landing on a
    real, computed tier 3, not the placeholder `0` the unscored branch
    reports. `search_ranked` preserves that by passing `literals=[]` (the
    correct, final-segment-only content — there is genuinely nothing to
    test against `nm`) together with `score=True` (there WAS literal
    content somewhere in the whole pattern, so this is an honest "ancestor-
    only, computed tier 3", not "nothing to rank at all") —
    `test_glob_final_segment_tier_ancestor_only_stays_tier_3`
    (`tests/test_index_rank.py`) pins this. `nm_exact` is `"false"` in this
    state (there is no `nm_regex` to test — `literals` being empty means
    `_name_predicate_sql` already returns `"false"` for every OTHER
    predicate too, so forcing `nm_exact` false alongside them keeps the row
    an honest, uniform "nothing matched in the name" rather than raising on
    a missing `nm_regex`).

    With `literals`, `_name_predicate_sql("nm", literals)` builds the same
    three position-free predicates `_rank_sql` uses — for glob mode,
    `contains` (the in-order, anything-between chain of every literal run
    against `nm`) is what fixes the tier defect this round targets
    (search-architecture-review.md §10.3): the OLD tier test ran the WHOLE
    compiled regex against `nm`, which can never match a path-shaped pattern
    like `**/src/*.ts` (the leading `**/ ` compiles to a `/`-crossing
    wildcard `nm`, a slash-free basename, can never satisfy), so every such
    glob was tier-3 for every hit regardless of how good the basename match
    was. `contains` only asks whether `nm` alone can explain the pattern's
    LITERAL content, independent of what kind of wildcard separated the
    literals originally, so it comes back true exactly when it should.

    `nm_exact` for glob mode is NOT plain `regexp_matches(nm, regex)`: for any
    pattern with a wildcard either side of a literal (`**icon**`, what a bare
    `icon` query resolves to), the compiled regex is `.*icon.*` — full-
    matching that against `nm` is true whenever `icon` occurs ANYWHERE in
    `nm`, which is exactly `contains`'s question, not "exact". Using it
    directly as `nm_exact` double-credits every `contains` hit with the
    +1000 exact-match weight too (caught empirically: `icon.png` scored 1599
    against substring mode's 599 for the equivalent `icon` query, before this
    was corrected). The fix adds a length constraint: `regexp_matches(nm,
    nm_regex) AND length(nm) = sum(len(lit) for lit in literals)` — a glob
    match is "exact" only when EVERY character of `nm` is accounted for by
    the pattern's literal content, i.e. every wildcard in the pattern matched
    ZERO characters, which is the only way a wildcard pattern can mean the
    same thing `_rank_sql`'s `nm = lower(qq)` means for a plain string.
    `test_glob_single_literal_run_score_matches_rank_sql_substring_score`
    (`tests/test_index_rank.py`) is the empirical check that this expression
    and `_rank_sql`'s agree on the boolean VALUE (not text) for a
    single-literal-run pattern, which is the only case the two modes'
    `nm_exact` expressions are asked to agree on at all.

    `nm_regex` is `regex`'s counterpart for the FINAL SEGMENT only —
    `_glob_to_regex(_final_segment_pattern(pattern))`, compiled and passed
    in by `search_ranked` alongside `literals` (which is likewise
    `_glob_literal_runs` run on the final segment, not the whole pattern —
    see `_final_segment_pattern`'s docstring for why: `regex`/`literals`
    built from the WHOLE pattern can never match `nm`, a slash-free
    basename, whenever the pattern crosses a directory boundary). `nm_regex`
    defaults to `None` and is only read when `literals` is non-empty (the
    caller's own contract: an empty `literals` list means there is nothing
    to test against `nm` at all, so `nm_regex` is never even reached).
    Enforced, not merely documented (code review finding 5): a non-empty
    `literals` with `nm_regex` left at its default asserts rather than
    silently interpolating the Python string `"None"` into the generated SQL
    as `regexp_matches(nm, 'None')` — a query that runs without error and
    just never matches anything, which is a much harder bug to notice than a
    loud failure at the call site that broke the pairing.
    `regex` itself remains the WHOLE, unmodified pattern's regex — it is
    still what `lrel` is filtered against in `WHERE`, since the file has to
    match the pattern in full, directory parts included; only the SCORING
    predicates (`contains`, `nm_exact`, `boundary`, ...) narrow to the final
    segment, because those are the only things ever tested against `nm`.

    `regex` is the already-lowercased, already-SQL-escaped pattern from
    `_glob_to_regex`; it is matched against `lrel`, never `rel` — glob mode
    is case-insensitive like every other mode here.

    `hidden` is the exact same fragment `_rank_sql` is handed (`""` or
    `" AND NOT (lrel LIKE '.%' OR lrel LIKE '%/.%')"`, from
    `query_wants_hidden` against the same `qs` both branches share) — code
    review finding: a plain multi-word query (`hello world`) now resolves to
    glob mode (SPEC-search-space-wildcard.md's whitespace-as-wildcard rule),
    and without this it surfaced dotfiles a single-word substring query for
    the same text would have hidden, purely because of which mode the query
    happened to land in. `query_wants_hidden` already reads intent off `qs`
    itself (a dot-leading query, `.env`/`*/.git`, still opts back in), so
    applying it here needs no separate rule — the same one both modes share."""
    if score is None:
        score = bool(literals)
    assert not (literals and nm_regex is None), (
        "_glob_sql: literals is non-empty but nm_regex was not supplied — "
        "every caller must pass a compiled nm_regex whenever literals is "
        "truthy (see this function's own docstring), or the missing regex "
        "silently becomes the Python string 'None' inside the generated SQL "
        "rather than raising")
    if not score:
        unscored_order = "depth ASC, lower(rel) ASC, rel ASC"
        return (
            f"SELECT rel, size, mtime, is_dir, depth FROM ({inner}) "
            f"WHERE regexp_matches(lrel, '{regex}'){hidden} "
            f"{_qualify_basename_cap(unscored_order)}"
            f"ORDER BY {unscored_order} "
            f"LIMIT {limit}")
    preds = _name_predicate_sql("nm", literals)
    if literals:
        total_len = sum(len(lit) for lit in literals)
        nm_exact = (f"(regexp_matches(nm, '{nm_regex}') "
                    f"AND length(nm) = {total_len})")
    else:
        # `score=True` with empty `literals`: the whole pattern had literal
        # content somewhere (else `search_ranked` would not have set
        # `score=True` at all), but none of it survived confining to the
        # final segment — an ancestor-only glob like `**/alpha/*`. There is
        # no `nm_regex` to test; `nm_exact` stays false alongside every
        # other predicate (`_name_predicate_sql([])`'s own "false" answer).
        nm_exact = "false"
    order_by, score_expr, tier = _lex_order_and_score(nm_exact, preds)
    return (
        f"SELECT rel, size, mtime, is_dir, depth, ({score_expr}) AS score, "
        f"({tier}) AS tier FROM ({inner}) "
        f"WHERE regexp_matches(lrel, '{regex}'){hidden} "
        f"{_qualify_basename_cap(order_by)}"
        f"ORDER BY {order_by} "
        f"LIMIT {limit}")


def search_ranked(cfg: IndexConfig, root: str, q: str = "",
                  limit: int = RANK_LIMIT, include_dirs: bool = True,
                  token=None, ranked: bool = True, glob: bool = False) -> dict:
    """Search `root` for `q` — filtered, scored and ordered ENTIRELY in SQL,
    top `limit` returned. No candidate rows cross into Python.

    The home page used to fetch the whole corpus and rank it in the browser:
    19.8 MB and 164k rows on one keystroke, and silently capped at MAX_CORPUS,
    so ~71% of a 571k-file home could not be found AT ALL. This answers a few
    KB from the whole index instead.

    Index-backed search is substring-only: `_rank_sql`'s `WHERE lrel LIKE
    '%q%'` is the entire candidate filter, and its score/tier expressions are
    a straight SQL port of the deleted index/rank.py's substring branch
    (`fuzzy_match`'s `sub != -1` case), `_is_segment_start`, `_name_tier` and
    `_sort_key`. The fuzzy subsequence escalation `rank.py` used to fall back
    to when a substring pass came up short is GONE — an owner-accepted feature
    loss, not an oversight: `indexstore` no longer matches `index/specs/
    index-store.md` on an indexed folder. `frontend/src/platform/lib/fuzzy.ts`
    keeps its subsequence pass unchanged; it ranks the live streamed walk for
    folders no scan will ever cover (a mount, a package, an ignored folder),
    which this function never touches.

    Files and directories still compete in ONE query, for the reason
    `search_under` explains at length: two queries served files first and
    folder search died on any tree big enough to fill the budget.

    `ORDER BY ... LIMIT {limit + 1}` is pushed into the same query as the
    filter and the scoring — the real win this change preserves from the
    two-stage version it replaces (which capped a 2,000-row Python-scored
    candidate set) is that duckdb now does the ordering and the cut in the
    SAME statement, so there is no per-request cap to tune, no Python
    scoring loop, and no risk of the cap silently dropping a row the full
    ranking would have preferred. **Not free, and not claimed to be**:
    scoring runs for every matched row before the top-N cut, not only the
    rows that survive it, so a broad query pays for scoring rows it will
    then discard. Measured on a 300k-file synthetic index, `q="e"` (353k
    matching rows) took ~100ms for the full statement here, ~53ms with
    scoring stripped out, and ~43ms for the old stage-A candidate-gathering
    shape alone — scoring roughly DOUBLES the per-keystroke cost for a query
    this broad. The trade is still the right one (no candidate cap means no
    row is ever dropped before ranking gets to see it, which is the actual
    correctness property that matters), but it is a trade with a real cost on
    a broad query, not a strictly cheaper replacement for the two-stage
    shape.

    `longest_run` does not appear in `_rank_sql`'s ORDER BY: every surviving
    row is a substring hit, so `longest_run = len(q)` for every one of them,
    and a sort key that is constant across every row drops out. It, and
    `score`/`tier`/`depth`, are still on every hit THIS function returns (this
    module's own tests pin `_rank_sql`'s scoring correctness off them) but no
    longer reach the wire: nothing downstream re-sorts a server-answered row —
    `listing/ranked-hits.ts` returns hits "in the order [the server] returned
    them" — so `server/routers/index.py`'s `api_index_rank` strips them before
    responding, the same way it already strips `positions`. See DECISIONS.md.

    Coverage semantics are `search_under`'s exactly: an uncovered root, a
    missing index or a package directory answers `covered: false` with no hits
    — never an error, because "no index yet", "not covered" and "a scan is
    running" are one condition to a search box.

    `ranked=False` (D720) is the owner's unranked-search preference: same
    filter, same hidden-file handling, `depth ASC, rel ASC` order instead of
    a score — see `_rank_sql`'s docstring on that branch for the ordering
    guarantee and why it's total. Threaded straight into `_rank_sql`; every
    other piece of this function (coverage, root/prefix resolution, LIMIT,
    truncation, cancellation) is unchanged by it.

    `glob=True` is a different FILTER altogether, not a variant of `ranked`:
    `q` is taken as a glob pattern (already resolved to a `(base, pattern)`
    pair by `resolve_query` — this function never re-derives one), translated
    by `_glob_to_regex` and full-matched against `lrel` in `_glob_sql`. Hidden
    entries are still dropped by the same `hidden` fragment `_rank_sql` uses
    (an explicit dot-leading query segment still opts back in — see
    `_glob_sql`'s docstring on the code-review finding this closed).

    `ranked` is NOT ignored when `glob` is set (an earlier version of this
    docstring said it was — stale the moment glob mode grew its own scoring):
    `_glob_literal_runs(_final_segment_pattern(qs))` splits the FINAL
    SEGMENT of the resolved pattern (`_final_segment_pattern`'s docstring on
    why — not the whole pattern, whose directory-segment literals can never
    match `nm`) on its `*` runs into literal pieces, and `_glob_sql` scores
    them with the SAME shared `_name_predicate_sql`/`_lex_order_and_score`
    helpers `_rank_sql` uses — no separate glob-scoring formula survives this
    round (the deleted `_glob_score_sql` — a `strpos`-chained per-run
    position walk, plus an interior-only wildcard-swallow penalty — was
    itself half the first-occurrence bug class this round's redesign
    eliminates; see `_glob_sql`'s docstring). `tier` is likewise built from
    the shared `contains` predicate (`_lex_order_and_score`'s docstring on
    the 3-value to 2-value collapse) rather than the fixed `0` placeholder
    every glob hit used to carry, and — as with `_rank_sql` — is no longer a
    primary sort key on its own; the lexicographic predicate vector is
    (which now also includes `boundary`, a word/segment-start bonus — see
    `_name_predicate_sql`'s and `_lex_order_and_score`'s docstrings).
    `ranked=False`, and a pattern with NO literal content anywhere at all
    (not merely in its final segment), e.g. the bare `**/*` a lone `*`
    resolves to, both fall back to the SAME unscored `depth ASC, lower(rel)
    ASC, rel ASC` order `_rank_sql`'s own unranked branch uses (no tier
    there either, same discipline as the rest of the scoring apparatus: not
    computed, not computed-then-discarded). An ANCESTOR-ONLY pattern like
    `**/alpha/*` is different from that: "alpha" IS literal content, just
    not any that survives confining to the final segment (a bare `*`) —
    `_glob_sql`'s `score` parameter keeps this case on the SCORED path with
    `contains` forced false, so it gets a real, computed tier 3 (not the
    placeholder `0` a truly literal-free pattern gets) — the same answer
    this case got before `_final_segment_pattern` existed, when
    `_glob_literal_runs` ran on the whole pattern and returned `["alpha/"]`.

    `token` (index/cancel.CancelToken), when given, is bound to the duckdb
    connection the moment it exists and checked immediately before and after
    the one SQL statement that does the filtering, scoring AND ordering.
    Binding is to the whole CONNECTION, so a `duckdb.InterruptException` can
    land in any query run on it — `_coverage_reason`'s included, which
    executes on this same connection first. One try/except around the whole
    bound region (not one per query site) re-raises as `Cancelled` an
    interrupt this token caused; one it did NOT cause (a real duckdb error, or
    another caller's timeout on a connection this function does not own — it
    never shares one) keeps surfacing as itself. **CORRECTS D703**: that entry
    describes a Python-only cancellation gap INSIDE `rank_entries`'s scoring
    loop — pure Python holding the GIL, with no duckdb call in flight for
    `con.interrupt()` to reach. `rank_entries` no longer exists; scoring is
    now inside the one SQL statement `con.interrupt()` already reaches, so
    that gap is gone with it, not merely unaddressed. `search_ranked` cannot
    make the abandoned THREAD return on its own (`asyncio.to_thread` has no
    such power); this is what makes the QUERY inside it return quickly
    instead, which is what the caller is actually waiting on.
    """
    root = _root_or_bare(
        norm(os.path.abspath(os.path.expanduser((root or "").strip()))).rstrip("/"))
    m = read_manifest(cfg)
    # `reason` is the miss's cause, and it is what the in-folder search box
    # switches on: a package or a mount-backed folder goes to the live walk, an
    # uncovered one is scanned on demand. Decided here rather than in the
    # client, so there is one copy of the rule. The mount half is the server
    # layer's to add (MountGuard); this package/uncovered half is the index's.
    #
    # `fresh`/`age_s`/`updated`/`root` are `search_under`'s fields, not this
    # function's: `search_under`'s copy is load-bearing (FRESH_MAX_AGE_S drives
    # the in-folder corpus box's "indexing…" caveat), but nothing reads them on
    # this path — the frontend's `IndexRankResult` never declared `fresh`/
    # `age_s`/`updated`, and `_rank_reason` (server/routers/index.py) only ever
    # reads `covered`/`reason` off this function's return. Carrying them here
    # was copy-paste from `search_under` directly above. Removed, not just
    # left unread — see DECISIONS.md.
    empty = {"covered": False, "hits": [], "truncated": False, "total": 0,
             "scanned_partitions": 0,
             "reason": "package" if (is_leaf_dir(root)
                                     or is_inside_leaf_dir(root)) else "uncovered",
             "of_partitions": len(((m or {}).get("partitions")) or [])}
    if m is None or not root or not os.path.exists(cfg.dirs_parquet):
        return empty
    import time

    import duckdb

    con = duckdb.connect()
    # Capped like every other interactive index read (search_threads' docstring
    # in store.py): a bare connect() defaults to one thread per core, and this
    # is the query a single keystroke pays for.
    con.execute(f"SET threads TO {search_threads()}")
    # Bound the instant the connection exists: a token cancelled before this
    # point still has to stop it (CancelToken.bind's own docstring), and every
    # `return`/`raise` from here on must close it — hence the try/finally
    # wrapping the entire rest of the function, replacing what used to be a
    # connection leaked on any early return or raised exception.
    if token is not None:
        token.bind(con)
    try:
        miss = _coverage_reason(con, cfg, root)
        if miss:
            return {**empty, "reason": miss}
        # See stats()'s identical fix above: root already ends in "/" for
        # any bare root (POSIX or a Windows drive), not only "/" itself.
        prefix = root if root.endswith("/") else root + "/"
        prefix_like = like_literal(prefix)
        limit = max(0, min(int(limit), MAX_GLOB_RANK_LIMIT if glob else MAX_RANK_LIMIT))
        hit = prune(m["partitions"], prefix)
        base = {"covered": True, "reason": "", "scanned_partitions": len(hit),
                "of_partitions": len(m["partitions"])}
        qs = (q or "").strip()
        if not qs:
            # Nothing typed is not "everything": the empty query has no ranking to
            # apply, and answering with an arbitrary 200 files would be noise.
            return {**base, "hits": [], "truncated": False, "total": 0}

        # `substr` from the prefix's length, so every comparison below is against
        # the REL — exactly the string stage B scores. Filtering on the full path
        # would let the root's own spelling admit rows no fuzzy match will keep.
        rel_from = len(prefix) + 1
        # `depth` here is RELATIVE to `root` (1 + how many "/" are in `rel`) —
        # the deleted index/rank.py's `_depth_of(rel)`, not the files/dirs
        # parquet's own stored `depth` column, which is the ABSOLUTE path's
        # depth and answers a different question (used by `search_under`'s
        # breadth-first corpus ordering, and by the coarse candidate-cap
        # ordering this single-pass query no longer has). Mixing the two up
        # here would make `alpha.txt` directly under a search root score as if
        # it were nested two levels deep.
        rel_depth = f"({depth_expr('rel')} + 1)"
        branches = []
        # `_src_cols` (one `DESCRIBE`) is the ONLY column question this
        # function asks: the dirs branch has no `name` column to reuse
        # (D708) and derives its basename by regex instead, so only the
        # files branch, for `_name_col`, ever calls this.
        if hit:
            fsrc = files_src(cfg, hit)
            fcols = _cached_src_cols(con, fsrc, (cfg.dir, m.get("generation"), "files"))
            branches.append(
                f"SELECT substr(path, {rel_from}) AS rel, size, mtime, "
                f"false AS is_dir, {_name_col(fcols)} AS nm "
                f"FROM {fsrc} WHERE path LIKE '{prefix_like}%' ESCAPE '\\'")
        if include_dirs:
            dsrc = dirs_src(cfg)
            # No stored `name` column here to reuse — the dirs schema has none
            # (store.py:165-171) — so the dirs branch keeps deriving its
            # basename by regex. Far fewer directories than files, so this is
            # still the bulk of the win over regexing every row.
            branches.append(
                f"SELECT substr(dir, {rel_from}) AS rel, CAST(NULL AS BIGINT) AS size, "
                f"nullif(mtime_ns, 0) / 1e9 AS mtime, true AS is_dir, "
                f"regexp_extract(lower(substr(dir, {rel_from})), '[^/]*$') AS nm "
                f"FROM {dsrc} WHERE dir LIKE '{prefix_like}%' ESCAPE '\\'")
        if not branches:
            return {**base, "hits": [], "truncated": False, "total": 0}

        # `nm` comes from each branch (the files branch reuses the stored
        # `name` column instead of a regex — see `_name_col`), so this adds
        # `lrel` and the root-RELATIVE `depth` (see `rel_depth` above; this is
        # computed once here rather than duplicated into every branch).
        inner = (f"SELECT *, lower(rel) AS lrel, {rel_depth} AS depth FROM ("
                 + " UNION ALL ".join(branches) + ")")

        if token is not None:
            token.check()
        t0 = time.monotonic()
        n = len(qs)
        # Hidden entries are dropped HERE, in the same query that filters
        # and scores/matches — `query_wants_hidden`/`is_hidden_rel` (this
        # module, moved from the now-deleted index/rank.py) are the
        # definitions; this mirrors them. Shared by both branches below
        # (code review finding: `_glob_sql` used to skip this filter
        # entirely, so a plain multi-word query — glob mode, since
        # SPEC-search-space-wildcard.md's whitespace-as-wildcard rule —
        # surfaced dotfiles the equivalent single-word substring query
        # would have hidden).
        hidden = ("" if _wants_hidden(qs)
                  else " AND NOT (lrel LIKE '.%' OR lrel LIKE '%/.%')")
        if glob:
            # `qs` is already the resolved pattern (`resolve_query`'s
            # `pattern`, base already peeled off) — lowered here, the same
            # side the corpus is lowered on (`lrel`), so the two always fold
            # through the same `lower()`.
            regex = _q(_glob_to_regex(qs.lower()))
            # `ranked=False` gets no literals at all — not computed then
            # ignored, the same discipline `_rank_sql`'s own unranked branch
            # follows (`_glob_sql`'s docstring on why an empty `literals`
            # answers both that case and a literal-free pattern identically).
            # Literals (and the regex `nm_exact` reads) come from the FINAL
            # SEGMENT only, not the whole pattern — `_final_segment_pattern`'s
            # docstring on why the whole pattern's literal runs can include
            # directory-segment text that can never match `nm`, a slash-free
            # basename (search-architecture-review.md §10.3).
            final_pattern = _final_segment_pattern(qs)
            literals = _glob_literal_runs(final_pattern) if ranked else []
            nm_regex = (_q(_glob_to_regex(final_pattern.lower()))
                        if literals else None)
            # An ancestor-only pattern (`**/alpha/*`: literal content
            # ("alpha") exists in the WHOLE pattern but not in its final
            # segment, a bare `*`) still gets a real, computed tier 3, not
            # the unscored placeholder `0` — `_glob_sql`'s docstring on why
            # `score` is passed explicitly rather than left to follow
            # `literals` (which is correctly `[]` here either way).
            score = bool(_glob_literal_runs(qs)) if ranked else False
            sql = _glob_sql(inner, regex, hidden, limit + 1,
                             literals=literals, nm_regex=nm_regex, score=score)
        else:
            # NOT `.lower()`'d here — `_rank_sql` lowers `ql`/`qq` itself,
            # with the same `lower()` call that produces `lrel`, so the query
            # and the rel it's compared against always fold through one
            # implementation (see `_rank_sql`'s docstring on why lowering both
            # sides separately can disagree).
            ql = like_literal(qs)
            qq = _q(qs)
            sql = _rank_sql(inner, hidden, ql, qq, qs, limit + 1, ranked=ranked)
        # One row past `limit` so "there was more" is known without a count —
        # same trick `search_under` uses for its own LIMIT.
        rows = con.execute(sql).fetchall()
        if token is not None:
            token.check()
        logger.debug("index rank: %r under %s: %d row(s) in %.1fms",
                    qs, root, len(rows), (time.monotonic() - t0) * 1000)
        truncated = len(rows) > limit
        if glob and score:
            # `_glob_sql` returned 6th and 7th columns (`score`, `tier`) for
            # this branch — the `score` BOOLEAN passed into `_glob_sql`
            # (not `literals`'s own emptiness any more: an ancestor-only
            # pattern like `**/alpha/*` has empty final-segment `literals`
            # but still gets a real, computed tier via `score=True` — see
            # `_glob_sql`'s docstring on why the two are no longer the same
            # condition) is exactly when it did. `tier` is a real,
            # generalized value now (1 = pattern satisfiable within the
            # basename, 3 = ancestor-only), not the fixed `0` placeholder
            # every glob hit used to carry — the HTTP layer still strips all
            # four scoring fields before the wire regardless of which branch
            # produced them.
            hits = [{"rel": rel, "is_dir": bool(is_dir),
                     "size": int(size) if size is not None else None,
                     "mtime": float(mtime) if mtime is not None else None,
                     "score": int(score_val), "longest_run": 0,
                     "tier": int(tier),
                     "depth": int(depth)}
                    for rel, size, mtime, is_dir, depth, score_val, tier
                    in rows[:limit]]
        elif glob:
            # `score` false: `_glob_sql` took its unscored branch (either
            # `ranked=False`, or a pattern with no literal content anywhere,
            # not merely in its final segment — see its docstring), so
            # `rows` has no `score` column to unpack.
            hits = [{"rel": rel, "is_dir": bool(is_dir),
                     "size": int(size) if size is not None else None,
                     "mtime": float(mtime) if mtime is not None else None,
                     "score": 0, "longest_run": 0, "tier": 0,
                     "depth": int(depth)}
                    for rel, size, mtime, is_dir, depth in rows[:limit]]
        elif ranked:
            # `score`/`tier`/`depth`/`longest_run` stay on EVERY hit dict this
            # function returns, even though nothing on the client's index-
            # answered path reads them any more (see this docstring above, and
            # DECISIONS.md) — this function's own test suite (test_index_rank.py)
            # pins `_rank_sql`'s scoring correctness (the depth penalty, both
            # name bonuses, the tier boundaries, the camelCase segment-start
            # case) directly off these fields, and that is the only place they
            # still earn their keep. The HTTP layer (server/routers/index.py's
            # `api_index_rank`) is where they actually stop reaching the wire —
            # same pattern already used there to drop `positions`.
            hits = [{"rel": rel, "is_dir": bool(is_dir),
                     "size": int(size) if size is not None else None,
                     "mtime": float(mtime) if mtime is not None else None,
                     "score": int(score), "longest_run": n, "tier": int(tier),
                     "depth": int(depth)}
                    for rel, size, mtime, is_dir, depth, score, tier
                    in rows[:limit]]
        else:
            # Unranked (D720): `_rank_sql` emits no score/tier — every hit
            # still carries those keys as fixed constants (0/0/len(q)) so
            # existing callers/tests keying off them unconditionally don't
            # KeyError; the HTTP layer strips all four before the wire either
            # way (`_WIRE_DROP`, server/routers/index.py).
            hits = [{"rel": rel, "is_dir": bool(is_dir),
                     "size": int(size) if size is not None else None,
                     "mtime": float(mtime) if mtime is not None else None,
                     "score": 0, "longest_run": n, "tier": 0,
                     "depth": int(depth)}
                    for rel, size, mtime, is_dir, depth in rows[:limit]]
        return {**base, "hits": hits, "truncated": truncated,
                "total": len(hits)}
    except duckdb.InterruptException:
        # `token.bind(con)` above binds the WHOLE connection, not just
        # pass_over's own SELECT — con.interrupt() can land in any statement
        # run on it, including `_coverage_reason`'s query above, which runs on
        # this same bound connection before pass_over ever does. Handled once
        # here for the entire bound region rather than wrapping each query
        # site separately.
        #
        # An interrupt with NO token, or one this token did not cause, is a
        # real error (someone else's timeout, a genuine duckdb abort) and
        # must keep surfacing as one — only attribute it to cancellation when
        # this token says it actually happened.
        if token is not None and token.cancelled:
            raise Cancelled() from None
        raise
    finally:
        # `unbind` BEFORE `close`, not after: the disconnect watcher
        # (server/routers/index.py's `_watch_disconnect`) can call
        # `token.cancel()` from another thread at any time, including in the
        # window right here between the query finishing and the connection
        # closing. Closing first would let that `cancel()` call `interrupt()`
        # on an already-closed connection (`duckdb.ConnectionException`, on a
        # task nothing then retrieves the result of — an unhandled-exception
        # warning on a perfectly normal disconnect). Same ordering
        # `guarded_query.py` already uses for its own connection-owning
        # `threading.Timer` (`timer.cancel()` before `close()`).
        if token is not None:
            token.unbind()
        con.close()
