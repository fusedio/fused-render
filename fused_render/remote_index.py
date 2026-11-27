"""Search and folder listings over S3 bucket indexes.

Each index the signed-in account can read (`remote_buckets.indexes()`) has its
own store on disk, written by `remote_buckets` and read here. This module
never fetches and never lists a bucket: it only answers from a store.

**Sibling store, same format.** A store is the main index's own format (the
7-column files table in partitions, the dirs table, partitions.json), so
`query.search_ranked` and the folder queries below read it unchanged. Nothing
in `fused_render/index/` knows these stores exist.

**Pseudo root.** `search_ranked` normalises its root with `os.path.abspath`,
which an `s3://` spelling does not survive. So inside a store an object key
`k` of bucket `b` is spelled `<root>/k`, where `<root>` is `/b` normalised by
the same expression the query code applies (`Index.root`). The root never
touches the disk. `s3://b/k` is produced only at this module's boundary.

**Coverage.** `_coverage_reason` answers "uncovered" unless dirs.parquet has
a row equal to the root, so every store has a dirs row for the root and for
every ancestor folder of every key.

pyarrow and duckdb belong to the optional `index` extra and are imported
lazily; without them the readers answer not-ready. Nothing here gates on the
canvases flag: callers do.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from fused_render import extras
from fused_render.remote_access import canonical_url

logger = logging.getLogger(__name__)

#: One `list_dir` page.
PAGE_SIZE = 1000


@dataclass(frozen=True)
class Index:
    """One bucket index and where its store lives. `prefix` is "" for a whole
    bucket, else ends in "/"; keys in the store are whole bucket keys."""
    id: str
    bucket: str
    prefix: str
    is_public: bool
    dir: str
    root: str


def pseudo_root(bucket: str) -> str:
    """`/<bucket>` exactly as `query.search_ranked` normalises a root:
    `norm(os.path.abspath(...))` then trailing slashes stripped. Never a
    bare root, so its `_root_or_bare` step is a no-op here."""
    from fused_render.index.ignore import norm

    return norm(os.path.abspath(os.sep + bucket)).rstrip("/")


def cfg_for(ix: Index):
    from fused_render.index.config import IndexConfig

    return IndexConfig(dir=ix.dir)


def _deps_missing() -> bool:
    return not extras.available("index")


def open_store(ix: Index):
    """(cfg, root, manifest) for a ready store, else None. The manifest is
    read ONCE per call and only the partitions it names are read, the way
    query.py does, so a call racing a sync answers from one complete
    generation (the writer keeps the previous generation's files on disk)."""
    if _deps_missing():
        return None
    from fused_render.index.store import read_manifest

    cfg = cfg_for(ix)
    m = read_manifest(cfg)
    if m is None or not os.path.exists(cfg.dirs_parquet):
        return None
    return cfg, ix.root, m


# --- status / refresh: remote_buckets owns both -------------------------------


def status() -> dict:
    from fused_render import remote_buckets

    return remote_buckets.status()


def start_background(reason: str) -> bool:
    from fused_render import remote_buckets

    remote_buckets.start(reason)
    return True


def startup_check() -> bool:
    return start_background("startup")


def retry_if_needed() -> None:
    """Cheap, idempotent, never raises: the routes that read an index call it
    on every request so a session that only searches still gets one."""
    try:
        from fused_render import remote_buckets

        remote_buckets.nudge()
    except Exception:  # noqa: BLE001
        logger.exception("remote index: nudge failed")


# --- readers ------------------------------------------------------------------


def rank(q: str, limit: int, ranked: bool, token=None, under: tuple | None = None) -> dict:
    """Top `limit` keys matching `q`, as `{ready, hits, total, truncated}`
    with hits `{url, key, is_dir, size, mtime}` (a folder's key and url end
    in "/", size and mtime None). Raises `index.cancel.Cancelled` when
    `token` is cancelled mid-query.

    `under=(index, prefix)` scopes the search to that folder's subtree — the
    S3 folder view, which is the explorer's own Listing, so the query means
    exactly what it means in a local folder (`resolve_query`: globs,
    whitespace wildcards, a leading "/"), and the answer carries its `mode`
    and `pattern` for highlighting.

    Without `under` every index the account can read is searched, and the
    query is address-aware: a bucket's name (or part of it) offers that
    bucket as a hit, `s3://` lists the buckets, and `<bucket>/<text>` or
    `s3://<bucket>/<text>` searches `<text>` inside that bucket only. Each
    index is ranked by `search_ranked` on its own and the answers are
    interleaved, best of each first."""
    empty = {"ready": False, "hits": [], "total": 0, "truncated": False}
    if under is not None:
        ix, prefix = under
        opened = open_store(ix)
        if opened is None:
            return empty
        return _rank_scoped(ix.bucket, opened[0], opened[1], q, limit, ranked, token, prefix)
    from fused_render import remote_buckets
    from fused_render.index.query import search_ranked

    stores = [(ix, o) for ix in remote_buckets.indexes() if (o := open_store(ix)) is not None]
    if not stores:
        return empty
    text = (q or "").strip()
    low = text.lower()
    scheme = "s3://"
    listing = False  # `s3://` (or a start of it) typed alone: list the buckets
    if low.startswith(scheme):
        text = text[len(scheme):]
        low = text.lower()
        listing = not low
    elif len(low) >= 3 and scheme.startswith(low):
        listing, text, low = True, "", ""
    buckets = list(dict.fromkeys(ix.bucket for ix, _ in stores))
    hits = []
    scoped = next((b for b in buckets if low.startswith(b + "/")), None)
    if scoped is not None:
        text = text[len(scoped) + 1:]
        stores = [(ix, o) for ix, o in stores if ix.bucket == scoped]
    else:
        for b in buckets:
            if listing or (low and low in b):
                hits.append({"url": canonical_url(b, ""), "key": "", "is_dir": True,
                             "size": None, "mtime": None})
    hits = hits[:max(0, limit)]
    if not text.strip() or listing:
        return {"ready": True, "hits": hits, "total": len(hits), "truncated": False}
    room = max(0, limit - len(hits))
    answers, truncated = [], False
    for ix, (cfg, root, _m) in stores:
        out = search_ranked(cfg, root, q=text, limit=room, include_dirs=True,
                            token=token, ranked=ranked)
        if out.get("covered"):
            answers.append([_rank_hit(ix.bucket, h) for h in out["hits"]])
            truncated = truncated or bool(out.get("truncated"))
    seen = {h["url"] for h in hits}
    for i in range(max((len(a) for a in answers), default=0)):
        for a in answers:
            if i < len(a) and a[i]["url"] not in seen and len(hits) < limit:
                seen.add(a[i]["url"])
                hits.append(a[i])
    return {"ready": True, "hits": hits, "total": len(hits),
            "truncated": truncated or sum(map(len, answers)) > room}


def _rank_scoped(bucket: str, cfg, root: str, q: str, limit: int, ranked: bool, token,
                 prefix: str) -> dict:
    """`rank` within folder `prefix` — see `rank`."""
    from fused_render.index.query import resolve_query, search_ranked

    if prefix and not prefix.endswith("/"):
        prefix += "/"
    folder = _dir_path(root, prefix)
    resolved = resolve_query(folder, q or "", token=token)
    base, pattern, mode = resolved["base"], resolved["pattern"], resolved["mode"]
    nothing = {"ready": True, "hits": [], "total": 0, "truncated": False,
               "mode": mode, "pattern": pattern}
    if not pattern.strip():
        return nothing
    # A query that walks out of the folder (`~/…`, `..`, an absolute path on
    # this machine) has left the bucket: nothing in this store answers it.
    # The explorer never sends one live — it waits for Enter and opens that
    # folder instead (useListingSearch's commit gate).
    if base != folder and not base.startswith(folder + "/"):
        return nothing
    out = search_ranked(cfg, base, q=pattern, limit=max(0, limit), include_dirs=True,
                        token=token, ranked=ranked, glob=(mode == "glob"))
    if not out.get("covered"):
        # The store is open, so "not covered" is a folder the bucket does not
        # have (every real folder has a dirs row): ready, nothing in it.
        return nothing
    sub = prefix + (base[len(folder) + 1:] + "/" if base != folder else "")
    hits = [_rank_hit(bucket, h, sub) for h in out["hits"]]
    return {"ready": True, "hits": hits, "total": len(hits),
            "truncated": bool(out.get("truncated")), "mode": mode, "pattern": pattern}


def _rank_hit(bucket: str, h: dict, prefix: str = "") -> dict:
    """One `search_ranked` hit (its `rel` relative to folder `prefix`) as a
    bucket hit. A folder's key ends in "/"; its synthesised dirs-row mtime is
    not a real object time, so size and mtime are both None."""
    key = prefix + h["rel"]
    if h.get("is_dir"):
        key += "/"
        return {"url": canonical_url(bucket, key), "key": key,
                "is_dir": True, "size": None, "mtime": None}
    return {"url": canonical_url(bucket, key), "key": key,
            "is_dir": False, "size": h["size"], "mtime": h["mtime"]}


def _sq(s: str) -> str:
    """A SQL string literal's contents (single quotes doubled)."""
    return s.replace("'", "''")


def _dir_path(root: str, prefix: str) -> str:
    """The store's spelling of folder `prefix` ("" = bucket root, otherwise
    ending in "/"). Only the ONE trailing slash is dropped, so a key folder
    with an empty segment (`a//`) keeps its own spelling."""
    if not prefix:
        return root
    if not prefix.endswith("/"):
        prefix += "/"
    return root + "/" + prefix[:-1]


def _connect():
    import duckdb

    from fused_render.index.store import search_threads

    con = duckdb.connect()
    con.execute(f"SET threads TO {search_threads()}")
    return con


def has_dir(ix: Index, prefix: str) -> bool | None:
    """Whether folder `prefix` ("" = bucket root, else ending in "/") exists
    in the index; None when the store is not ready. Every ancestor folder of
    every key has a dirs row (see `_feed`), so this is one exact lookup."""
    opened = open_store(ix)
    if opened is None:
        return None
    cfg, root, _ = opened
    if not prefix:
        return True
    from fused_render.index.query import dirs_src

    con = _connect()
    try:
        d = _dir_path(root, prefix)
        return con.execute(f"SELECT count(*) FROM {dirs_src(cfg)} "
                           f"WHERE dir = '{_sq(d)}'").fetchone()[0] > 0
    finally:
        con.close()


def has_file(ix: Index, key: str) -> bool | None:
    """Whether an object with exactly `key` is in the index; None when the
    store is not ready. One lookup over the partitions whose path range can
    hold it (`query.prune`). A key may be both a file and a folder prefix
    (`x.zip` and `x.zip/...`): this and `has_dir` answer independently."""
    opened = open_store(ix)
    if opened is None:
        return None
    if not key or key.endswith("/"):
        return False
    cfg, root, m = opened
    from fused_render.index.query import files_src, prune

    path = root + "/" + key
    parts = prune(m.get("partitions") or [], path)
    if not parts:
        return False
    con = _connect()
    try:
        return con.execute(f"SELECT count(*) FROM {files_src(cfg, parts)} "
                           f"WHERE path = '{_sq(path)}'").fetchone()[0] > 0
    finally:
        con.close()


_CURSOR_RE = re.compile(r"o(\d{1,12})")


def _parse_cursor(cursor) -> int:
    if cursor in (None, ""):
        return 0
    m = _CURSOR_RE.fullmatch(cursor) if isinstance(cursor, str) else None
    if m is None:
        raise ValueError("invalid cursor")
    return int(m.group(1))


def list_dir(ix: Index, prefix: str, cursor: str | None = None,
             limit: int = PAGE_SIZE) -> dict | None:
    """One level of folder `prefix` ("" = bucket root, else ending in "/"):
    `{entries: [{key, name, is_dir, size, mtime}], cursor}`, child folders
    first then files, each by name (byte order), `limit` (at most
    `PAGE_SIZE`) per page. None when the store is not ready. A folder's key
    ends in "/"; its `size`, `n_files` and `mtime` are its whole subtree's
    (bytes, file count, newest file), summed from the files table at query
    time (`_subtree_totals`); a file's `n_files` is None. `cursor` is opaque (an
    offset, advanced by the entries returned, so pages of different sizes
    chain without gaps); a malformed one raises ValueError. A missing
    folder lists as empty — ask `has_dir` first.

    Child folders come from dirs.parquet (`depth` = one more than the
    folder's, inside the folder's byte range, so row-group statistics skip
    the rest); files from the manifest's partitions pruned to the folder's
    path range (`query.prune`) and filtered on `dir` exactly."""
    offset = _parse_cursor(cursor)
    page = max(1, min(int(limit), PAGE_SIZE))
    opened = open_store(ix)
    if opened is None:
        return None
    cfg, root, m = opened
    from fused_render.index.query import dirs_src, files_src, prune

    d = _dir_path(root, prefix)
    lo = d + "/"
    hi = d + "0"  # '0' is the code point right after '/': [lo, hi) = inside d
    depth = d.count("/") + 1
    cut = len(lo) + 1  # SQL substr is 1-based
    branches = [
        f"SELECT 0 AS grp, substr(dir, {cut}) AS name, CAST(NULL AS BIGINT) AS size, "
        f"CAST(NULL AS DOUBLE) AS mtime FROM {dirs_src(cfg)} "
        f"WHERE dir >= '{_sq(lo)}' AND dir < '{_sq(hi)}' AND depth = {depth}"]
    parts = prune(m.get("partitions") or [], lo)
    if parts:
        branches.append(
            f"SELECT 1 AS grp, name, size, mtime FROM {files_src(cfg, parts)} "
            f"WHERE path >= '{_sq(lo)}' AND path < '{_sq(hi)}' AND dir = '{_sq(d)}'")
    con = _connect()
    try:
        rows = con.execute(
            "SELECT grp, name, size, mtime FROM (" + " UNION ALL ".join(branches)
            + f") ORDER BY grp, name LIMIT {page + 1} OFFSET {offset}").fetchall()
        totals = (_subtree_totals(con, cfg, parts, d)
                  if any(r[0] == 0 for r in rows[:page]) else {})
    finally:
        con.close()
    key_base = d[len(root) + 1:] + "/" if d != root else ""
    entries = []
    for grp, name, size, mtime in rows[:page]:
        is_dir = grp == 0
        if is_dir:
            # A folder with no file anywhere below it (only empty subfolders) has no row: zero.
            size, n_files, mtime = totals.get(name, (0, 0, None))
        else:
            n_files = None
        entries.append({"key": key_base + name + ("/" if is_dir else ""),
                        "name": name, "is_dir": is_dir,
                        "size": int(size or 0), "n_files": n_files,
                        "mtime": (None if mtime is None else float(mtime)) if is_dir
                        else float(mtime or 0.0)})
    nxt = f"o{offset + page}" if len(rows) > page else None
    return {"entries": entries, "cursor": nxt}


def _subtree_totals(con, cfg, parts, d: str) -> dict:
    """`{child folder name: (bytes, files, newest mtime)}` for every folder
    directly under `d`, each over its WHOLE subtree. dirs.parquet's own
    `total_size`/`n_files` count only a folder's direct files, so these are
    summed from the files table: one range scan over `d`'s partitions,
    grouped by the first path segment below `d`. The bucket root scans every
    row (~260k in 2026-11): well under a second in DuckDB."""
    if not parts:
        return {}
    from fused_render.index.query import files_src

    lo, hi = d + "/", d + "0"
    cut = len(lo) + 1  # SQL substr is 1-based
    rows = con.execute(
        f"SELECT split_part(substr(path, {cut}), '/', 1) AS child, sum(size), count(*), max(mtime) "
        f"FROM {files_src(cfg, parts)} "
        f"WHERE path >= '{_sq(lo)}' AND path < '{_sq(hi)}' AND dir <> '{_sq(d)}' "
        "GROUP BY child").fetchall()
    return {r[0]: (int(r[1] or 0), int(r[2] or 0), r[3]) for r in rows}


def summary(ix: Index, prefix: str, top: int = 10) -> dict | None:
    """How big folder `prefix` ("" = bucket root, else ending in "/") is, from
    the index alone: `{files, bytes, newest, children, extensions,
    index_fetched_at}`. `children` is the `top` largest entries directly
    under it (folders by subtree, files by size: `{key, is_dir, bytes,
    files, newest}`); `extensions` the `top` extensions by bytes (`{ext,
    files, bytes}`, "" for none). `newest` is an epoch time or None. None
    when the store is not ready; a missing folder sums to zero files — ask
    `has_dir` first."""
    opened = open_store(ix)
    if opened is None:
        return None
    cfg, root, m = opened
    from fused_render.index.query import files_src, prune

    top = max(1, min(int(top), 100))
    d = _dir_path(root, prefix)
    lo, hi = d + "/", d + "0"
    key_base = d[len(root) + 1:] + "/" if d != root else ""
    parts = prune(m.get("partitions") or [], lo)
    out = {"files": 0, "bytes": 0, "newest": None, "children": [], "extensions": [],
           "index_fetched_at": _synced_at(ix)}
    if not parts:
        return out
    src = files_src(cfg, parts)
    where = f"path >= '{_sq(lo)}' AND path < '{_sq(hi)}'"
    cut = len(lo) + 1
    con = _connect()
    try:
        files, total, newest = con.execute(
            f"SELECT count(*), sum(size), max(mtime) FROM {src} WHERE {where}").fetchone()
        children = con.execute(
            f"SELECT split_part(substr(path, {cut}), '/', 1) AS child, dir <> '{_sq(d)}' AS is_dir, "
            f"sum(size) AS b, count(*), max(mtime) FROM {src} WHERE {where} "
            f"GROUP BY child, is_dir ORDER BY b DESC, child LIMIT {top}").fetchall()
        exts = con.execute(
            f"SELECT coalesce(ext, '') AS e, count(*), sum(size) AS b FROM {src} WHERE {where} "
            f"GROUP BY e ORDER BY b DESC, e LIMIT {top}").fetchall()
    finally:
        con.close()
    out.update(
        files=int(files or 0), bytes=int(total or 0),
        newest=None if newest is None else float(newest),
        children=[{"key": key_base + c + ("/" if is_dir else ""), "is_dir": bool(is_dir),
                   "bytes": int(b or 0), "files": int(n or 0),
                   "newest": None if t is None else float(t)}
                  for c, is_dir, b, n, t in children],
        extensions=[{"ext": e, "files": int(n or 0), "bytes": int(b or 0)} for e, n, b in exts])
    return out


def _synced_at(ix: Index):
    from fused_render.shell import storage

    s = storage.read_json(os.path.join(ix.dir, "state.json"))
    return s.get("synced_at") if isinstance(s, dict) else None
