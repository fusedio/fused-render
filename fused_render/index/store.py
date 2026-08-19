"""The on-disk index: parquet schemas, the shard sink a scan worker writes
into, the per-directory reuse cache, and the duckdb compaction that merges
shards with the previous index into path-sorted partitions.

Ported from OpenIndex's `runner.py` (`_schemas`, `_Sink`, `_load_dir_cache`,
`_compact`) with the module globals replaced by an explicit `IndexConfig`.
See specs/index-store.md.

pyarrow and duckdb are passed in / imported inside functions rather than at
module top: this module is imported by the server (routers/index.py), and a
`stats` call on a missing index should not pay a duckdb import.
"""
import contextlib
import json
import os
import time

from fused_render.index.config import IndexConfig
from fused_render.index.ignore import ignored_for_index


# How often the Windows lock re-tries. Short enough to hand the lock over
# promptly, long enough that a minutes-long compaction costs a trivial number
# of syscalls to wait out.
NT_LOCK_POLL_S = 0.05

# Cores the background compaction's DuckDB may use. It runs inside a scan
# worker, against an interactive `/api/index/rank` that gets no cap at all
# (query.py) — a merge on every core starved the query for seconds, and the
# user is not waiting on the merge. A quarter of the machine, never more than
# this many.
MAX_COMPACTION_THREADS = 4


def compaction_threads() -> int:
    return max(1, min(MAX_COMPACTION_THREADS, (os.cpu_count() or 4) // 4))


def background_connect():
    """An in-memory DuckDB for the scan side, capped to `compaction_threads`."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET threads TO {compaction_threads()}")
    return con


def _acquire_nt(msvcrt, fileno: int) -> None:
    """Block until the byte is ours, polling the NON-blocking mode.

    NOT msvcrt.LK_LOCK: that one retries for about ten seconds and then
    raises, and this lock is held for the length of a whole DuckDB merge — so
    a second root's compaction (or a Delete Index) would fail instead of
    waiting. Polling LK_NBLCK with no deadline gives flock's semantics."""
    while True:
        try:
            msvcrt.locking(fileno, msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            time.sleep(NT_LOCK_POLL_S)


@contextlib.contextmanager
def store_lock(cfg: IndexConfig):
    """Mutual exclusion for store WRITERS (compactions, delete), blocking.

    Two concurrent compactions — two configured roots scanned at startup, or a
    config-save rescan racing the startup scan — would otherwise both read the
    same manifest generation, write identically-named partition files, and
    lose whichever manifest lands first (its rows are absent from the other's
    merge). Everything from the manifest read to the manifest write must
    happen inside this lock. Readers never take it: they follow the manifest,
    and generations make that safe (see compact).

    An OS lock (flock / msvcrt), not a lockfile-existence protocol: it is held
    by an open fd, so a crashed worker releases it with its process instead of
    wedging every future scan."""
    os.makedirs(cfg.dir, exist_ok=True)
    f = open(os.path.join(cfg.dir, ".store.lock"), "a")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            _acquire_nt(msvcrt, f.fileno())
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def _sql(s: str) -> str:
    """A SQL string literal's contents (single quotes doubled)."""
    return s.replace("'", "''")


def shard_files(shards_dir: str, pattern: str) -> list:
    """Shard files matching `pattern`, listed rather than globbed downstream.

    `glob.escape` on the DIRECTORY half only: the pattern's own `*` has to
    stay a wildcard, while the store path is the user's and may contain the
    very metacharacters the pattern language uses."""
    import glob as globmod
    return sorted(globmod.glob(os.path.join(globmod.escape(shards_dir), pattern)))


def parquet_src(paths):
    """`read_parquet` over an EXPLICIT file list, or None for no files.

    Never a glob string built from a real path: DuckDB's glob has no escape
    (checked on 1.5.5 — a `[` in the directory silently matches nothing), and
    an unescaped path would also close the SQL literal on an apostrophe."""
    if not paths:
        return None
    return "read_parquet([" + ",".join(f"'{_sql(p)}'" for p in paths) + "])"


def like_literal(s: str) -> str:
    """`s` as a LITERAL inside a LIKE pattern: quotes doubled for the SQL
    string, and LIKE's own metachars (`\\`, `%`, `_`) escaped so an `_` in a
    folder name cannot match a lookalike sibling (`/x/proj_a` matching
    `/x/proj-a`). The pattern MUST be used with `ESCAPE '\\'`."""
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
             .replace("'", "''"))


def schemas(pa):
    """The single definition of both tables (specs/index-store.md §2).

    `name`, `ext`, `dir` and `depth` are all DENORMALISED out of `path`. That is
    correct only because rows are never mutated in place: a scan writes a row
    once, and a compaction copies whole partitions and swaps the manifest, so
    there is no update path that could leave a derived column disagreeing with
    the path it was derived from. `depth` joins that existing family; it is not
    a new anomaly.

    It is a STORED int32 rather than a derived or generated column because
    there is nowhere to put a generated one: every query opens a fresh
    in-memory duckdb over `read_parquet` (query.py), so no catalog survives
    between calls, and parquet has no computed-column concept. Measured on 300k
    rows at the real LIMIT 200_001: 92.2ms stored vs 147.6ms for the
    slash-counting expression, for +0.10% on disk.

    `depth` is the ABSOLUTE slash count of the full path, not a count relative
    to any search root — a stored column cannot know which root will read it.
    Both tables carry it because query.search_under UNIONs them and a UNION
    needs uniform columns. Appended last in both schemas: the compaction's
    `UNION ALL` of old partitions with new shards is positional."""
    file_schema = pa.schema([
        ("path", pa.string()), ("dir", pa.string()), ("name", pa.string()),
        ("ext", pa.string()), ("size", pa.int64()), ("mtime", pa.float64()),
        ("depth", pa.int32()),
    ])
    dir_schema = pa.schema([
        ("dir", pa.string()), ("sig", pa.string()),
        ("n_files", pa.int32()), ("total_size", pa.int64()),
        ("mtime_ns", pa.int64()), ("n_subdirs", pa.int32()),
        ("depth", pa.int32()),
    ])
    return file_schema, dir_schema


# The slash-count expression a pre-`depth` partition falls back to, so an index
# already on disk keeps reading instead of hard-failing (the same additive
# evolution load_dir_cache and _compact_locked already do for mtime_ns /
# n_subdirs). Migration is a full rescan.
def depth_expr(col: str) -> str:
    return f"CAST(length({col}) - length(replace({col}, '/', '')) AS INTEGER)"


class Sink:
    """Accumulates scan results and writes shard-*/_dirs-*/_keep-* parquets
    into `shards_dir`. `tag` keeps filenames unique across processes."""

    def __init__(self, shards_dir, tag, pa, pq, shard_rows):
        self.shards_dir, self.tag, self.pa, self.pq = shards_dir, tag, pa, pq
        self.shard_rows = shard_rows
        self.file_schema, self.dir_schema = schemas(pa)
        self.rows = {k: [] for k in self.file_schema.names}
        self.dir_rows = {k: [] for k in self.dir_schema.names}
        self.keep = []
        self.seq = 0
        self.dirs = self.files = self.reused = self.udirs = 0

    def add(self, d, kind, payload):
        self.dirs += 1
        if kind == "u":
            self.keep.append(d)
            self.reused += payload
            self.udirs += 1
            return
        sig, frows, dtotal, mtime_ns, n_subdirs = payload
        r = self.rows
        for fr in frows:
            r["path"].append(fr[0]); r["dir"].append(fr[1])
            r["name"].append(fr[2]); r["ext"].append(fr[3])
            r["size"].append(fr[4]); r["mtime"].append(fr[5])
            r["depth"].append(fr[0].count("/"))
        self.files += len(frows)
        dr = self.dir_rows
        dr["dir"].append(d); dr["sig"].append(sig)
        dr["n_files"].append(len(frows)); dr["total_size"].append(dtotal)
        dr["mtime_ns"].append(mtime_ns)
        dr["n_subdirs"].append(n_subdirs)
        dr["depth"].append(d.count("/"))
        if len(r["path"]) >= self.shard_rows:
            self._flush_files()

    def _flush_files(self):
        if self.rows["path"]:
            t = self.pa.table(self.rows, schema=self.file_schema)
            self.pq.write_table(t, os.path.join(
                self.shards_dir, f"shard-{self.tag}-{self.seq:05d}.parquet"))
            for k in self.rows:
                self.rows[k].clear()
        self.seq += 1

    def close(self):
        """Force-write pending rows plus this batch's dir/keep tables."""
        self._flush_files()
        pa, pq = self.pa, self.pq
        pq.write_table(pa.table(self.dir_rows, schema=self.dir_schema),
                       os.path.join(self.shards_dir,
                                    f"_dirs-{self.tag}-{self.seq:05d}.parquet"))
        pq.write_table(pa.table({"dir": pa.array(self.keep, type=pa.string())}),
                       os.path.join(self.shards_dir,
                                    f"_keep-{self.tag}-{self.seq:05d}.parquet"))
        for k in self.dir_rows:
            self.dir_rows[k].clear()
        self.keep = []


def load_dir_cache(cfg: IndexConfig, root: str, pq) -> dict:
    """dir -> (mtime_ns, n_files, n_subdirs) for cached dirs under `root`,
    from dirs.parquet. Returns {} when there is no usable cache (missing file
    or a pre-mtime index). Ignored subtrees are filtered out here, which is
    what makes a newly-ignored folder self-purging (specs/scan-ignore.md §3).

    Self-purging is why this filter is also the sharpest edge in the whole ignore
    story: a row missing from this cache is a row the next compaction drops. So it
    asks `ignored_for_index`, the one predicate all three gates share, rather than
    the raw rules — otherwise a LEAF dir the user's ignore list names (a `.git`,
    for every config saved before it left the default list) is written by a full
    rescan and then deleted by the very next incremental one."""
    if not os.path.exists(cfg.dirs_parquet):
        return {}
    names = pq.read_schema(cfg.dirs_parquet).names
    if "mtime_ns" not in names:
        return {}
    cols = ["dir", "n_files", "mtime_ns"] + (
        ["n_subdirs"] if "n_subdirs" in names else [])
    t = pq.read_table(cfg.dirs_parquet, columns=cols)
    subs = (t.column("n_subdirs").to_pylist() if "n_subdirs" in names
            else [-1] * t.num_rows)
    rules = cfg.rules
    prefix = root.rstrip("/") + "/"
    cache = {}
    for d, m, n, ns in zip(t.column("dir").to_pylist(),
                           t.column("mtime_ns").to_pylist(),
                           t.column("n_files").to_pylist(), subs):
        if ((d == root or d.startswith(prefix))
                and not ignored_for_index(rules, d, tree=True)):
            cache[d] = (m, n, ns)
    return cache


def read_manifest(cfg: IndexConfig):
    """partitions.json, or None when no scan has ever compacted."""
    try:
        with open(cfg.partitions_json) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def applied_ignore_sig(cfg: IndexConfig, root: str = ""):
    """The ignore fingerprint `root`'s slice of the index was built with
    (None if unknown — an index predating the feature, safe incrementally).

    PER ROOT, because each root reconciles a rules change on its own scan: a
    single global sig was stamped by whichever root full-rescanned first,
    after which every other root's next scan looked already-reconciled and
    ran incrementally against a cache built under the old rules — a parent
    whose only child was ignored is cached as a leaf (n_subdirs == 0), so a
    re-included tree under it stayed permanently missing.

    Without `root` this answers whether ANY recorded root differs from the
    current rules (the router's needs_rescan bit): the current sig if all
    recorded roots match, else the first differing sig."""
    try:
        with open(cfg.applied_ignore_json) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    roots = data.get("roots")
    if not isinstance(roots, dict):
        # pre-per-root file: one global sig, which is exact for the single
        # root it was written by and the safe answer for any other
        return data.get("sig")
    if root:
        # `legacy_sig` is that same pre-per-root global sig, carried through
        # the migration for the roots it covered but that have not been
        # stamped individually yet. Answering None for them would claim they
        # were never built under any rules, and the router reads that as
        # up-to-date — so a rules edit would skip their reconciling rescan.
        return roots.get(root, data.get("legacy_sig"))
    if not roots:
        return None
    sigs = set(roots.values())
    current = cfg.rules.sig()
    return current if sigs == {current} else next(
        s for s in roots.values() if s != current)


def save_applied_ignore(cfg: IndexConfig, root: str) -> None:
    """Record the rules `root`'s slice of the index was built under. Written
    only after a SUCCESSFUL compaction — a crashed run must not claim its
    rules applied. Read-modify-write under the store lock: two roots' workers
    finishing together must not drop each other's entry."""
    with store_lock(cfg):
        try:
            with open(cfg.applied_ignore_json) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        roots = data.get("roots")
        legacy = data.get("legacy_sig")
        if not isinstance(roots, dict):
            # Migrating the pre-per-root file. Its single global sig described
            # EVERY root, so it has to survive as the fallback for the ones
            # this stamp does not name — dropping it would leave them
            # answering None, i.e. indistinguishable from up-to-date.
            roots, legacy = {}, data.get("sig")
        roots[root] = cfg.rules.sig()
        out = {"roots": roots, "patterns": list(cfg.rules.patterns),
               "updated": time.time()}
        if legacy is not None:
            out["legacy_sig"] = legacy
        tmp = cfg.applied_ignore_json + ".new"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, cfg.applied_ignore_json)


def partition_files(cfg: IndexConfig, manifest=None):
    """Absolute paths of the partitions the manifest names, in order.

    The manifest — never a glob of the files dir — is what the index IS. That
    is what lets a compaction write a new generation of partitions beside the
    live ones: a reader following the manifest cannot see the half-written set,
    and a stray parquet cannot become index rows."""
    m = read_manifest(cfg) if manifest is None else manifest
    if not m:
        return []
    return [os.path.join(cfg.files_dir, p["file"]) for p in m.get("partitions") or []]


def compact(cfg: IndexConfig, root, shards_dir, pa, pq, emit=None,
            cancel_flag=None):
    """Merge new shards with the existing index, keeping old rows for dirs
    outside `root` and for unchanged dirs inside it, sort by path, and write
    size-bounded partition files + manifest. Skips the rewrite entirely when
    an incremental scan found nothing changed.

    A scan is a background job the user did not ask to wait for, so the index
    stays READABLE throughout: new partitions are written under a fresh
    generation number alongside the old ones and the manifest is swapped last,
    atomically. A query running mid-compaction therefore answers from the last
    completed generation instead of failing — and a crash leaves that
    generation intact rather than an index that has been rmtree'd and not yet
    renamed (OpenIndex's "atomic-ish" swap, which this replaces).

    The whole merge runs under `store_lock`, manifest read included, so a
    concurrent compaction (another root, a config-save rescan) serializes
    behind this one and merges on top of ITS output instead of on the stale
    generation both of them read.

    `cancel_flag` is re-checked after the lock is acquired: a Delete Index
    pressed while this run was walking cancels the run and takes the lock, so
    a compaction that was about to rebuild the store the user just emptied
    aborts here (returns None) instead of quietly undoing the delete.

    `emit` is the worker's event writer (or None when compaction is driven
    directly, e.g. by a test)."""
    with store_lock(cfg):
        if cancel_flag is not None and os.path.exists(cancel_flag):
            return None
        return _compact_locked(cfg, root, shards_dir, pa, pq, emit)


def _compact_locked(cfg: IndexConfig, root, shards_dir, pa, pq, emit=None):
    import shutil

    def phase(msg):
        if emit is not None:
            emit(type="phase", msg=msg)

    files_dir = cfg.files_dir
    dirs_parquet = cfg.dirs_parquet
    old_manifest = read_manifest(cfg) or {}
    generation = int(old_manifest.get("generation") or 0) + 1
    con = background_connect()
    rootp = root.rstrip("/") or "/"
    prefix_like = (like_literal(rootp) + "/") if rootp != "/" else "/"
    outside = (f"(dir <> '{_sql(rootp)}' "
               f"AND dir NOT LIKE '{prefix_like}%' ESCAPE '\\')")

    # Every path below reaches SQL through parquet_src/_sql, never as a raw
    # f-string: the store lives under the user's home, so a quote or a glob
    # metacharacter in it is ordinary, not exotic.
    tmp_new_dirs = parquet_src(shard_files(shards_dir, "_dirs-*.parquet"))
    tmp_keep = parquet_src(shard_files(shards_dir, "_keep-*.parquet"))
    shard_src = parquet_src(shard_files(shards_dir, "shard-*.parquet"))
    has_shards = shard_src is not None
    old_files = [p for p in partition_files(cfg, old_manifest) if os.path.exists(p)]
    has_old = bool(old_files)
    old_src = parquet_src(old_files)
    dirs_src = parquet_src([dirs_parquet] if os.path.exists(dirs_parquet) else [])

    n_new_dirs = (con.execute(
        f"SELECT count(*) FROM {tmp_new_dirs}").fetchone()[0]
        if tmp_new_dirs else 0)
    # A closed Sink always writes a _keep table, but an empty shards dir (no
    # Sink ran) leaves nothing to match: keep nothing rather than fail.
    kept = (f"dir IN (SELECT dir FROM {tmp_keep})" if tmp_keep else "false")

    # dirs diff counts vs the previous index
    changed, added, removed = 0, 0, 0
    old_dirs_src = None
    if dirs_src and tmp_new_dirs:
        cols = pq.read_schema(dirs_parquet).names
        mt = "mtime_ns" if "mtime_ns" in cols else "CAST(0 AS BIGINT) AS mtime_ns"
        ns = ("n_subdirs" if "n_subdirs" in cols
              else "CAST(-1 AS INTEGER) AS n_subdirs")
        dp = ("depth" if "depth" in cols
              else f"{depth_expr('dir')} AS depth")
        old_dirs_src = (f"SELECT dir, sig, n_files, total_size, {mt}, {ns}, {dp} "
                        f"FROM {dirs_src}")
        old_in = f"SELECT dir, sig FROM ({old_dirs_src}) WHERE NOT {outside}"
        changed = con.execute(
            f"SELECT count(*) FROM ({old_in}) o JOIN {tmp_new_dirs} n "
            f"USING (dir) WHERE o.sig <> n.sig").fetchone()[0]
        added = con.execute(
            f"SELECT count(*) FROM {tmp_new_dirs} n "
            f"WHERE n.dir NOT IN (SELECT dir FROM ({old_in}) o)").fetchone()[0]
        removed = con.execute(
            f"SELECT count(*) FROM ({old_in}) o "
            f"WHERE o.dir NOT IN (SELECT dir FROM {tmp_new_dirs}) "
            f"AND NOT o.{kept}").fetchone()[0]
    else:
        added = n_new_dirs

    def root_totals(src):
        """files / folders / bytes under the scan root, from the index.

        dirs.parquet is re-resolved per call rather than reused from above:
        this runs both before and after the file is replaced."""
        rf, rs = (con.execute(
            f"SELECT count(*), coalesce(sum(size),0) FROM {src} "
            f"WHERE NOT {outside}").fetchone() if src else (0, 0))
        dirs_now = parquet_src(
            [dirs_parquet] if os.path.exists(dirs_parquet) else [])
        rd = con.execute(
            f"SELECT count(*) FROM {dirs_now} "
            f"WHERE NOT {outside}").fetchone()[0] if dirs_now else 0
        return {"root_files": int(rf), "root_size": int(rs),
                "root_dirs": int(rd)}

    # nothing changed anywhere -> keep the existing index untouched
    if not has_shards and n_new_dirs == 0 and removed == 0 and has_old:
        shutil.rmtree(shards_dir, ignore_errors=True)
        meta = dict(old_manifest) or {"rows": 0, "partitions": []}
        meta.update(updated=time.time(), last_root=root)
        _write_manifest(cfg, meta)
        return {"rows": meta.get("rows", 0),
                "partitions": len(meta.get("partitions", [])),
                "changed_dirs": 0, "added_dirs": 0, "removed_dirs": 0,
                "skipped_rewrite": True, **root_totals(old_src)}

    phase("writing index")
    os.makedirs(files_dir, exist_ok=True)

    srcs = []
    if has_old:
        # Column list spelled out, not `SELECT *`: a partition written before
        # `depth` existed has one fewer column than the shards it is unioned
        # with, and a positional UNION ALL would fail outright rather than
        # merge. Backfill it from the path instead (see depth_expr).
        fcols = pq.read_schema(old_files[0]).names
        fdp = "depth" if "depth" in fcols else f"{depth_expr('path')} AS depth"
        srcs.append(f"SELECT path, dir, name, ext, size, mtime, {fdp} "
                    f"FROM {old_src} WHERE {outside} OR {kept}")
    if has_shards:
        srcs.append(f"SELECT * FROM {shard_src}")
    src = " UNION ALL ".join(srcs) or None

    parts = []
    total_rows = 0
    if src:
        con.execute(
            f"CREATE TEMP TABLE merged AS SELECT * FROM ({src}) "
            f"QUALIFY row_number() OVER (PARTITION BY path ORDER BY mtime DESC) = 1 "
            f"ORDER BY path")
        total_rows = con.execute("SELECT count(*) FROM merged").fetchone()[0]
        n_parts = max(1, -(-total_rows // cfg.part_rows))
        for i in range(n_parts):
            fp = os.path.join(files_dir, f"part-{generation:06d}-{i:05d}.parquet")
            con.execute(
                f"COPY (SELECT * FROM merged LIMIT {cfg.part_rows} "
                f"OFFSET {i * cfg.part_rows}) "
                f"TO '{_sql(fp)}' (FORMAT PARQUET, ROW_GROUP_SIZE 65536)")
            # The folded bounds are their own aggregate, not lower() of the
            # byte-wise ones: the two orders disagree, so a partition can
            # hold a folded-smaller path than its byte-wise minimum. Pruning
            # for the ILIKE match needs the real folded range (query.prune).
            lo, hi, lo_f, hi_f, n = con.execute(
                f"SELECT min(path), max(path), "
                f"min(lower(path)), max(lower(path)), count(*) "
                f"FROM {parquet_src([fp])}").fetchone()
            parts.append({"file": os.path.basename(fp), "min": lo, "max": hi,
                          "min_lower": lo_f, "max_lower": hi_f, "rows": n})

    # dirs.parquet: old rows outside root or unchanged inside it + new rows
    phase("writing signatures")
    dirs_out = _sql(dirs_parquet + ".new")
    if old_dirs_src and tmp_new_dirs:
        con.execute(
            f"COPY (SELECT * FROM ({old_dirs_src}) WHERE {outside} OR {kept} "
            f"UNION ALL SELECT * FROM {tmp_new_dirs} "
            f"QUALIFY row_number() OVER (PARTITION BY dir ORDER BY mtime_ns DESC) = 1 "
            f"ORDER BY dir) "
            f"TO '{dirs_out}' (FORMAT PARQUET)")
    elif tmp_new_dirs:
        con.execute(
            f"COPY (SELECT * FROM {tmp_new_dirs} ORDER BY dir) "
            f"TO '{dirs_out}' (FORMAT PARQUET)")
    elif old_dirs_src:
        con.execute(
            f"COPY (SELECT * FROM ({old_dirs_src}) WHERE {outside} OR {kept} "
            f"ORDER BY dir) TO '{dirs_out}' (FORMAT PARQUET)")

    # The swap. Both replacements are atomic, and the manifest goes LAST: until
    # that line lands, every reader is answering from the previous generation,
    # whose files are all still on disk (specs/index-store.md §4).
    os.makedirs(cfg.dir, exist_ok=True)
    # Absent only when there was neither a dirs shard nor a previous
    # dirs.parquet to carry forward — nothing to swap in.
    if os.path.exists(dirs_parquet + ".new"):
        os.replace(dirs_parquet + ".new", dirs_parquet)
    _write_manifest(cfg, {"updated": time.time(), "last_root": root,
                          "rows": total_rows, "partitions": parts,
                          "generation": generation})
    shutil.rmtree(shards_dir, ignore_errors=True)
    _reclaim_partitions(cfg, keep=[p["file"] for p in parts]
                        + [p["file"] for p in old_manifest.get("partitions") or []])

    new_src = ("read_parquet([" + ",".join(
        f"'{_sql(os.path.join(files_dir, p['file']))}'" for p in parts) + "])"
        if parts else None)
    return {"rows": total_rows, "partitions": len(parts),
            "changed_dirs": changed, "added_dirs": added,
            "removed_dirs": removed, "skipped_rewrite": False,
            **root_totals(new_src)}


def _reclaim_partitions(cfg: IndexConfig, keep) -> None:
    """Delete partition files outside `keep` — the new generation plus the one
    before it. The previous generation is spared because a reader that loaded
    the manifest microseconds before the swap is still holding those filenames;
    the compaction after this one reclaims them, by which time no reader can
    still be on them."""
    keep = set(keep)
    try:
        names = os.listdir(cfg.files_dir)
    except OSError:
        return
    for name in names:
        if name.endswith(".parquet") and name not in keep:
            try:
                os.unlink(os.path.join(cfg.files_dir, name))
            except OSError:
                pass  # a concurrent reader holds it on Windows: next pass gets it


def delete_store(cfg: IndexConfig) -> None:
    """Remove the index itself — partitions, dir signatures, manifest, the
    FSEvents positions, the applied-rules fingerprint, and the last-scan
    record — leaving the config and the run directories in place.

    Run dirs stay because a scan in flight polls its `cancel` flag from one:
    deleting them would remove the only way to stop a worker that is about to
    compact a fresh index into the store the user just emptied. The last-scan
    record goes so the next startup rescans immediately instead of debouncing
    against a scan whose output no longer exists.

    Missing files are not an error: "delete" on an empty store is a no-op that
    succeeds, which is what makes the button safe to press twice.

    Runs under `store_lock`: a worker already inside its compaction finishes
    first and THEN gets deleted, and one that was still walking finds its
    cancel flag when it reaches the lock (see compact) — either way the store
    stays deleted."""
    import shutil

    with store_lock(cfg):
        shutil.rmtree(cfg.files_dir, ignore_errors=True)
        for path in (cfg.dirs_parquet, cfg.partitions_json, cfg.fsevents_json,
                     cfg.applied_ignore_json, cfg.scans_json):
            try:
                os.unlink(path)
            except OSError:
                pass


def _write_manifest(cfg: IndexConfig, meta: dict) -> None:
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.partitions_json + ".new", "w") as f:
        json.dump(meta, f)
    os.replace(cfg.partitions_json + ".new", cfg.partitions_json)
