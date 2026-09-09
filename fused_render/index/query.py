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

from fused_render.index.cancel import Cancelled
from fused_render.index.config import IndexConfig
from fused_render.index.ignore import is_inside_leaf_dir, is_leaf_dir, norm
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
    keeps the endpoint useful for a caller that only wants the hits.

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
                f"{_depth_col(_src_cols(con, fsrc), 'path')} AS depth FROM {fsrc} "
                f"WHERE path LIKE '{prefix_like}%' ESCAPE '\\'{like}")
        if include_dirs:
            dsrc = dirs_src(cfg)
            dlike = f" AND dir ILIKE '%{qlit}%' ESCAPE '\\'" if qlit else ""
            branches.append(
                f"SELECT dir AS path, CAST(NULL AS BIGINT) AS size, "
                f"nullif(mtime_ns, 0) / 1e9 AS mtime, true AS is_dir, "
                f"{_depth_col(_src_cols(con, dsrc), 'dir')} AS depth FROM {dsrc} "
                f"WHERE dir LIKE '{prefix_like}%' ESCAPE '\\'{dlike}")
        entries, truncated = [], False
        if branches:
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

# Chars that open a new "segment" in a path/name; a match right after one of
# these reads as the start of a word and scores higher. Mirrors
# frontend/src/platform/lib/fuzzy.ts's SEPARATORS exactly — change one, change
# both, then regenerate tests/fixtures/rank-parity.json.
_SEGMENT_SEPARATORS = ["/", ".", "-", "_", " "]

# A deep, vague match used to out-score a shallow one on raw `score` alone —
# score accumulates over the matched window, and DEPTH_PENALTY offsets a long
# ancestor chain's extra segment-start bonuses. Mirrors fuzzy.ts's DEPTH_PENALTY
# / SHALLOW_FREE. See the ORDER BY comment on `_rank_sql` for how these fold in.
_DEPTH_PENALTY = 4
_SHALLOW_FREE = 3


def _walk_from(start: str, rest: str) -> tuple:
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
    it did reach."""
    base = norm(start).rstrip("/") or "/"
    if not rest:
        return base, "", False
    segs = rest.split("/")
    i = 0
    while i < len(segs) and "*" not in segs[i]:
        candidate = base + "/" + segs[i] if base != "/" else "/" + segs[i]
        if not os.path.isdir(candidate):
            break
        base = candidate
        i += 1
    return base, "/".join(segs[i:]), i > 0


def resolve_query(root: str, raw: str) -> dict:
    """The one place a search box's typed string becomes a `(base, pattern,
    mode)` triple. `root` is the box's own root (home sends the home dir, the
    explorer sends the open folder); `raw` is the string exactly as typed,
    unstripped of anything meaningful.

    `mode` is "glob" the moment `raw` contains a `*` anywhere, else
    "substring" — `?` and `[`/`]` are left as literal characters on purpose
    (spec: people put them in filenames far more often than they mean them as
    patterns), so their presence never flips the mode.

    Base resolution: a query starting with `~` or `/` can escape the box's
    own root entirely; anything else inherits it unchanged. `~` expands to
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

    The implicit `**/` prefix — a glob with no `/` anywhere searches any
    depth — is decided from `raw` BEFORE any of the base-splitting above, not
    from the leftover pattern. Deciding it after would silently anchor every
    query that escapes to a base: `~/a/b/*.c` types no slash into ITS pattern
    either (`*.c`, once `~/a/b` is peeled off as the base), but `raw` itself
    has three, so no prefix is added and the match stays anchored at
    `~/a/b`'s own depth 1 — exactly the point of the original request this
    rule exists for. Deciding it from the post-split pattern instead would
    have widened that one, and every query like it, to any depth."""
    raw = raw or ""
    is_glob = "*" in raw
    if raw == "~" or raw.startswith("~/"):
        home = norm(os.path.expanduser("~"))
        rest = raw[2:] if raw.startswith("~/") else ""
        base, pattern, _ = _walk_from(home, rest)
    elif raw.startswith("/"):
        rest = raw[1:]
        abs_base, abs_pattern, advanced = _walk_from("/", rest)
        if advanced:
            base, pattern = abs_base, abs_pattern
        else:
            base, pattern = root, rest
    else:
        base, pattern = root, raw
    if is_glob and "/" not in raw:
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


def _rank_sql(inner: str, hidden: str, ql: str, qq: str, n: int, limit: int,
              ranked: bool = True) -> str:
    """The whole rank query: substring filter, scoring, and ORDER BY ... LIMIT,
    all in SQL — no candidate cap, no Python-side pass.

    `ranked=False` (the owner's unranked-results preference, D720) keeps the
    exact same `WHERE lrel LIKE ...` substring filter and hidden-file handling
    below, but drops the entire scoring apparatus — no `p0`/`strpos`, no
    `segment_starts`, no `name_bonus`, no `score`, no `tier` — computed
    nowhere, not computed-then-discarded. It orders `depth ASC, rel ASC`
    instead: `depth` here is `rel_depth`, the same ROOT-RELATIVE depth the
    ranked branch computes in `inner` (see `search_ranked`'s docstring on
    `rel_depth` for why the parquet's own stored absolute `depth` column would
    be the wrong one — the ranked branch had a real bug from exactly that mix-
    up before it was fixed). This mirrors `search_under`'s own `ORDER BY
    depth, path` above — the ordering the owner's user confirmed is usable.
    `depth, rel` is a TOTAL order as long as `rel` is unique, which it is: a
    file and a directory cannot share a path on a real filesystem, and both
    parquet stores are keyed on that same path uniqueness — the ranked
    branch's own final tie-break (`rel ASC`, after tier/score/depth/
    lower(rel)) already leans on this identical fact. No `lower(rel)` ahead of
    `rel` here — unlike the ranked branch, which needs it as an intermediate
    tie-break before `rel ASC` because ties can survive tier/score/depth —
    the unranked branch's ORDER BY has only two keys and `rel` alone already
    makes it total, so a `lower(rel)` in front would reintroduce a case-
    insensitive ordering question for no benefit (see D712 on the ranked
    branch's own `lower(rel)`/`rel` split for the class of bug that guards
    against, which does not apply to a two-key order that is already total).

    `inner` is the UNION ALL of the files/dirs branches (each already carries
    `rel`, `size`, `mtime`, `is_dir`, `depth` — RELATIVE to the search root,
    `search_ranked`'s `rel_depth` — and `nm`, the lowercased basename,
    `_name_col`'s doing) plus `lrel` (`lower(rel)`). `ql` is the ORIGINAL-case
    `qs` as a LIKE literal (metachars escaped, use with ESCAPE '\\'); `qq` is
    the same original-case string as a plain SQL string literal (quotes
    doubled only) for `strpos`/`=` comparisons, which are not LIKE and must
    not see LIKE's escapes. Every comparison against `ql`/`qq` below wraps
    them in SQL's own `lower(...)` rather than lowering in Python first — see
    the paragraph below for why. `n` is `len(qs)` — the constant `longest_run`
    every surviving row shares now that the fuzzy/subsequence pass is gone
    (see this module's docstring on `search_ranked` for why that constant
    safely drops out of the ORDER BY).

    Ported line for line from the deleted index/rank.py's `fuzzy_match`
    substring branch, `_is_segment_start`, `_name_tier` and `_sort_key` — see
    that module's own history for the fuzzy-subsequence half this replaces.

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

    Per matched row, at the substring's start position `p0` (`strpos(lrel,
    qq) - 1`, 0-indexed):

    - `score = n + 3*(n-1) + 5*segment_starts - DEPTH_PENALTY*max(0, depth -
      SHALLOW_FREE) + name_bonus` — `n + 3*(n-1)` is rank.py's "+1 per char,
      +3 for the whole run being consecutive" collapsed algebraically (a
      substring match IS one run), `segment_starts` is how many of the `n`
      matched positions land on a segment start (computed against the
      ORIGINAL-case `rel`, not `lrel`, so the camelCase hump test survives
      lowercasing — same reason rank.py's `_is_segment_start` does), and
      `name_bonus` is +100 for an exact basename match, +25 for a basename
      prefix, else 0.
    - `tier` is 1 when `qs` is a substring of the basename, 3 when the match
      ends before the basename starts (an ancestor-only hit), else 2.

    `segment_starts` is a `list_filter` over `range(p0, p0+n)` — the lambda
    only evaluates for the (small) matched window of each already-substring-
    filtered row, not over every row in the corpus. `substr(rel, i, 1)` (1-
    indexed) is therefore the ORIGINAL-case character at 0-indexed `i - 1` —
    the "previous" character for the segment-start test at 0-indexed `i`;
    `substr(rel, i + 1, 1)` is the character AT `i`. `c BETWEEN 'A' AND 'Z'`
    is the ASCII-uppercase test (a plain byte comparison, matching rank.py's
    `.isupper() and .isascii()` pair — and `regexp_matches(c, '^[A-Z]$')`,
    which this was rewritten from: RE2's `[A-Z]` is byte/ASCII by default
    too, so the two are equivalent, `BETWEEN` just doesn't pay for spinning
    up the regex engine to answer a single-byte-range question).

    Final order is `tier ASC, score DESC, depth ASC, lower(rel) ASC, rel ASC` —
    `longest_run` does not appear because every surviving row shares it. The
    trailing `rel ASC` is what makes this a TOTAL order: a pair equal under
    every column before it (tier, score, depth) AND under `lower(rel)` — two
    rels differing only in case, e.g. `notes/Alpha.txt` vs `notes/alpha.txt`
    — was otherwise still an unresolved tie, and DuckDB's multi-threaded
    top-N is free to resolve an unresolved tie arbitrarily, so the pair could
    silently swap order between two runs of the identical query and shift
    keyboard selection out from under a user who hadn't typed anything.
    `rel` (byte/ASCII comparison, not `lower(rel)`) breaks that tie for free
    — it costs nothing beyond a column DuckDB already has in hand — and
    always resolves it the same way. This makes the SQL side deterministic
    ON ITS OWN, but not necessarily identical to `frontend/src/platform/lib/
    fuzzy.ts`'s tie-break: the JS ranker's is `Intl.Collator(sensitivity:
    "base")`, which is locale-aware and does not always agree with a plain
    ASCII byte comparison on which of a case-only pair sorts first. That
    divergence is pre-existing (`tests/test_index_rank.py`'s
    `_group_case_only_ties` helper exists because of it, not because of
    this) and is unaffected by adding `rel ASC` here — it only fixes SQL's
    OWN run-to-run stability, not cross-language agreement."""
    if not ranked:
        # No `p0`/`strpos`, no `segment_starts`, no `name_bonus`, no `score`,
        # no `tier` — the scoring apparatus below is never built for this
        # branch, not built and then left out of the SELECT list.
        return (
            f"SELECT rel, size, mtime, is_dir, depth FROM ({inner}) "
            f"WHERE lrel LIKE '%' || lower('{ql}') || '%' ESCAPE '\\'{hidden} "
            f"ORDER BY depth ASC, rel ASC "
            f"LIMIT {limit}")
    # `BETWEEN 'A' AND 'Z'`, not `regexp_matches(c, '^[A-Z]$')`: same ASCII-
    # uppercase test (a single-byte comparison DuckDB can do without spinning
    # up its regex engine), measured ~15-20% faster and byte-for-byte
    # equivalent for this predicate — this only ever compares a length-1
    # string against the two ASCII bytes 'A'/'Z', which is exactly what
    # `^[A-Z]$` matched and nothing more.
    segment_starts = (
        f"len(list_filter(range(p0, p0 + {n}), i -> "
        f"i = 0 OR list_contains({_SEGMENT_SEPARATORS!r}, substr(rel, i, 1)) "
        f"OR (substr(rel, i + 1, 1) BETWEEN 'A' AND 'Z' "
        f"AND NOT (substr(rel, i, 1) BETWEEN 'A' AND 'Z'))))"
    )
    name_bonus = (f"CASE WHEN nm = lower('{qq}') THEN 100 "
                  f"WHEN nm LIKE lower('{ql}') || '%' ESCAPE '\\' THEN 25 ELSE 0 END")
    tier = (f"CASE WHEN strpos(nm, lower('{qq}')) > 0 THEN 1 "
            f"WHEN p0 + {n} - 1 < length(rel) - length(nm) THEN 3 "
            f"ELSE 2 END")
    score = (f"{n} + 3 * ({n} - 1) + 5 * {segment_starts} "
             f"- {_DEPTH_PENALTY} * greatest(0, depth - {_SHALLOW_FREE}) "
             f"+ {name_bonus}")
    return (
        f"WITH matched AS ("
        f"SELECT *, strpos(lrel, lower('{qq}')) - 1 AS p0 FROM ({inner}) "
        f"WHERE lrel LIKE '%' || lower('{ql}') || '%' ESCAPE '\\'{hidden}) "
        f"SELECT rel, size, mtime, is_dir, depth, ({score}) AS score, "
        f"({tier}) AS tier FROM matched "
        f"ORDER BY tier ASC, score DESC, depth ASC, lower(rel) ASC, rel ASC "
        f"LIMIT {limit}")


def _glob_sql(inner: str, regex: str, limit: int) -> str:
    """Glob mode's whole query: a full-match regex filter, no scoring at all
    — not `_rank_sql`'s apparatus with the scoring columns dropped, `p0`/
    `strpos`/`segment_starts`/`name_bonus` are never computed in the first
    place, because a glob hit has no "substring position" for them to score.
    Ordered `depth ASC, lower(rel) ASC, rel ASC` — the same total order
    `_rank_sql`'s own unranked branch uses and for the same reason: `rel`
    alone (after `lower(rel)`) is what makes a case-only-differing pair
    (`notes/Alpha.txt` vs `notes/alpha.txt`) resolve the same way on every
    run instead of however DuckDB's multi-threaded top-N happens to land.

    `regex` is the already-lowercased, already-SQL-escaped pattern from
    `_glob_to_regex`; it is matched against `lrel`, never `rel` — glob mode
    is case-insensitive like every other mode here. Hidden entries are NOT
    filtered: `query_wants_hidden` governs substring mode only, and an
    explicit pattern here matches whatever the regex matches, dotfiles
    included."""
    return (
        f"SELECT rel, size, mtime, is_dir, depth FROM ({inner}) "
        f"WHERE regexp_matches(lrel, '{regex}') "
        f"ORDER BY depth ASC, lower(rel) ASC, rel ASC "
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

    `glob=True` is a different filter altogether, not a variant of `ranked`:
    `q` is taken as a glob pattern (already resolved to a `(base, pattern)`
    pair by `resolve_query` — this function never re-derives one), translated
    by `_glob_to_regex` and full-matched against `lrel` in `_glob_sql`. No
    scoring, no hidden-entry filter (an explicit pattern matches whatever it
    matches), `depth ASC, lower(rel) ASC, rel ASC` order. `ranked` is ignored
    when `glob` is set.

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
        limit = max(0, min(int(limit), MAX_RANK_LIMIT))
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
            fcols = _src_cols(con, fsrc)
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
        if glob:
            # `qs` is already the resolved pattern (`resolve_query`'s
            # `pattern`, base already peeled off) — lowered here, the same
            # side the corpus is lowered on (`lrel`), so the two always fold
            # through the same `lower()`.
            regex = _q(_glob_to_regex(qs.lower()))
            sql = _glob_sql(inner, regex, limit + 1)
        else:
            # NOT `.lower()`'d here — `_rank_sql` lowers `ql`/`qq` itself,
            # with the same `lower()` call that produces `lrel`, so the query
            # and the rel it's compared against always fold through one
            # implementation (see `_rank_sql`'s docstring on why that used to
            # diverge).
            ql = like_literal(qs)
            qq = _q(qs)
            # Hidden entries are dropped HERE, in the same query that filters
            # and scores — `query_wants_hidden`/`is_hidden_rel` (this module,
            # moved from the now-deleted index/rank.py) are the definitions;
            # this mirrors them.
            hidden = ("" if _wants_hidden(qs)
                      else " AND NOT (lrel LIKE '.%' OR lrel LIKE '%/.%')")
            sql = _rank_sql(inner, hidden, ql, qq, n, limit + 1, ranked=ranked)
        # One row past `limit` so "there was more" is known without a count —
        # same trick `search_under` uses for its own LIMIT.
        rows = con.execute(sql).fetchall()
        if token is not None:
            token.check()
        logger.debug("index rank: %r under %s: %d row(s) in %.1fms",
                    qs, root, len(rows), (time.monotonic() - t0) * 1000)
        truncated = len(rows) > limit
        if glob:
            # No score/tier at all — a glob hit has no substring position to
            # score. Every hit still carries the same keys as the other two
            # branches (0-valued) so nothing downstream has to special-case
            # a missing key; the HTTP layer strips all four before the wire
            # regardless of which branch produced them.
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
