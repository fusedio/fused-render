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
import json
import os
import time

from fused_render.index.config import IndexConfig


def _sql(s: str) -> str:
    """A SQL string literal's contents (single quotes doubled)."""
    return s.replace("'", "''")


def like_literal(s: str) -> str:
    """`s` as a LITERAL inside a LIKE pattern: quotes doubled for the SQL
    string, and LIKE's own metachars (`\\`, `%`, `_`) escaped so an `_` in a
    folder name cannot match a lookalike sibling (`/x/proj_a` matching
    `/x/proj-a`). The pattern MUST be used with `ESCAPE '\\'`."""
    return (s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
             .replace("'", "''"))


def schemas(pa):
    """The single definition of both tables (specs/index-store.md §2)."""
    file_schema = pa.schema([
        ("path", pa.string()), ("dir", pa.string()), ("name", pa.string()),
        ("ext", pa.string()), ("size", pa.int64()), ("mtime", pa.float64()),
    ])
    dir_schema = pa.schema([
        ("dir", pa.string()), ("sig", pa.string()),
        ("n_files", pa.int32()), ("total_size", pa.int64()),
        ("mtime_ns", pa.int64()), ("n_subdirs", pa.int32()),
    ])
    return file_schema, dir_schema


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
        self.files += len(frows)
        dr = self.dir_rows
        dr["dir"].append(d); dr["sig"].append(sig)
        dr["n_files"].append(len(frows)); dr["total_size"].append(dtotal)
        dr["mtime_ns"].append(mtime_ns)
        dr["n_subdirs"].append(n_subdirs)
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
    what makes a newly-ignored folder self-purging (specs/scan-ignore.md §3)."""
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
        if (d == root or d.startswith(prefix)) and not rules.is_ignored_tree(d):
            cache[d] = (m, n, ns)
    return cache


def read_manifest(cfg: IndexConfig):
    """partitions.json, or None when no scan has ever compacted."""
    try:
        with open(cfg.partitions_json) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def applied_ignore_sig(cfg: IndexConfig):
    """The ignore fingerprint the CURRENT index was built with (None if
    unknown — an index predating the feature, which is safe incrementally)."""
    try:
        with open(cfg.applied_ignore_json) as f:
            return json.load(f).get("sig")
    except (OSError, ValueError):
        return None


def save_applied_ignore(cfg: IndexConfig) -> None:
    """Record the rules this index was built under. Written only after a
    SUCCESSFUL compaction — a crashed run must not claim its rules applied."""
    os.makedirs(cfg.dir, exist_ok=True)
    tmp = cfg.applied_ignore_json + ".new"
    with open(tmp, "w") as f:
        json.dump({"sig": cfg.rules.sig(), "patterns": list(cfg.ignore),
                   "updated": time.time()}, f)
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


def compact(cfg: IndexConfig, root, shards_dir, pa, pq, emit=None):
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

    `emit` is the worker's event writer (or None when compaction is driven
    directly, e.g. by a test)."""
    import duckdb
    import shutil

    def phase(msg):
        if emit is not None:
            emit(type="phase", msg=msg)

    files_dir = cfg.files_dir
    dirs_parquet = cfg.dirs_parquet
    old_manifest = read_manifest(cfg) or {}
    generation = int(old_manifest.get("generation") or 0) + 1
    con = duckdb.connect()
    rootp = root.rstrip("/") or "/"
    prefix_like = (like_literal(rootp) + "/") if rootp != "/" else "/"
    outside = (f"(dir <> '{_sql(rootp)}' "
               f"AND dir NOT LIKE '{prefix_like}%' ESCAPE '\\')")

    import glob as globmod

    tmp_new_dirs = os.path.join(shards_dir, "_dirs-*.parquet")
    tmp_keep = os.path.join(shards_dir, "_keep-*.parquet")
    has_shards = bool(globmod.glob(os.path.join(shards_dir, "shard-*.parquet")))
    old_files = [p for p in partition_files(cfg, old_manifest) if os.path.exists(p)]
    has_old = bool(old_files)
    old_src = ("read_parquet([" + ",".join(f"'{_sql(p)}'" for p in old_files) + "])"
               if has_old else None)

    n_new_dirs = con.execute(
        f"SELECT count(*) FROM read_parquet('{tmp_new_dirs}')").fetchone()[0]
    kept = f"dir IN (SELECT dir FROM read_parquet('{tmp_keep}'))"

    # dirs diff counts vs the previous index
    changed, added, removed = 0, 0, 0
    old_dirs_src = None
    if os.path.exists(dirs_parquet):
        cols = pq.read_schema(dirs_parquet).names
        mt = "mtime_ns" if "mtime_ns" in cols else "CAST(0 AS BIGINT) AS mtime_ns"
        ns = ("n_subdirs" if "n_subdirs" in cols
              else "CAST(-1 AS INTEGER) AS n_subdirs")
        old_dirs_src = (f"SELECT dir, sig, n_files, total_size, {mt}, {ns} "
                        f"FROM read_parquet('{dirs_parquet}')")
        old_in = f"SELECT dir, sig FROM ({old_dirs_src}) WHERE NOT {outside}"
        changed = con.execute(
            f"SELECT count(*) FROM ({old_in}) o JOIN read_parquet('{tmp_new_dirs}') n "
            f"USING (dir) WHERE o.sig <> n.sig").fetchone()[0]
        added = con.execute(
            f"SELECT count(*) FROM read_parquet('{tmp_new_dirs}') n "
            f"WHERE n.dir NOT IN (SELECT dir FROM ({old_in}) o)").fetchone()[0]
        removed = con.execute(
            f"SELECT count(*) FROM ({old_in}) o "
            f"WHERE o.dir NOT IN (SELECT dir FROM read_parquet('{tmp_new_dirs}')) "
            f"AND NOT o.{kept}").fetchone()[0]
    else:
        added = n_new_dirs

    def root_totals(src):
        """files / folders / bytes under the scan root, from the index."""
        rf, rs = (con.execute(
            f"SELECT count(*), coalesce(sum(size),0) FROM {src} "
            f"WHERE NOT {outside}").fetchone() if src else (0, 0))
        rd = con.execute(
            f"SELECT count(*) FROM read_parquet('{dirs_parquet}') "
            f"WHERE NOT {outside}").fetchone()[0]
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
        srcs.append(f"SELECT * FROM {old_src} WHERE {outside} OR {kept}")
    if has_shards:
        srcs.append(f"SELECT * FROM read_parquet('{shards_dir}/shard-*.parquet')")
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
                f"TO '{fp}' (FORMAT PARQUET, ROW_GROUP_SIZE 65536)")
            lo, hi, n = con.execute(
                f"SELECT min(path), max(path), count(*) "
                f"FROM read_parquet('{fp}')").fetchone()
            parts.append({"file": os.path.basename(fp), "min": lo, "max": hi,
                          "rows": n})

    # dirs.parquet: old rows outside root or unchanged inside it + new rows
    phase("writing signatures")
    if old_dirs_src:
        con.execute(
            f"COPY (SELECT * FROM ({old_dirs_src}) WHERE {outside} OR {kept} "
            f"UNION ALL SELECT * FROM read_parquet('{tmp_new_dirs}') "
            f"QUALIFY row_number() OVER (PARTITION BY dir ORDER BY mtime_ns DESC) = 1 "
            f"ORDER BY dir) "
            f"TO '{dirs_parquet}.new' (FORMAT PARQUET)")
    else:
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{tmp_new_dirs}') ORDER BY dir) "
            f"TO '{dirs_parquet}.new' (FORMAT PARQUET)")

    # The swap. Both replacements are atomic, and the manifest goes LAST: until
    # that line lands, every reader is answering from the previous generation,
    # whose files are all still on disk (specs/index-store.md §4).
    os.makedirs(cfg.dir, exist_ok=True)
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
    succeeds, which is what makes the button safe to press twice."""
    import shutil

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
