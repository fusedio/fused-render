# Index store

> **Status — shipped.** This file owns the **on-disk index**: where it lives, its
> parquet schemas, the partition manifest, and the shard → partition compaction.
> Implementing modules: `config.py` (`IndexConfig`), `store.py` (`schemas`, `Sink`,
> `compact`, `read_manifest`). Reading it is `query.md`; producing the shards is
> `scan.md`.

## 1. Location

Everything hangs off `IndexConfig.dir`, which defaults to
`storage.home_dir()/index` — so FUSED_RENDER_HOME redirection and per-branch
nesting come for free, and no test can write into a real home.

| Path | Contents |
|---|---|
| `<index>/files/part-NNNNN.parquet` | file rows, globally sorted by path |
| `<index>/dirs.parquet` | one row per directory: signature + reuse metadata |
| `<index>/partitions.json` | manifest: row count, per-partition path ranges |
| `<index>/fsevents.json` | per-root journal position (`scan-incremental.md §3`) |
| `<index>/ignore_applied.json` | ignore fingerprint the index was built with (`scan-ignore.md §4`) |
| `<index>/config.json` | user config: ignore list + scan roots (`server-api.md §3`) |
| `<index>/scans.json` | last scan start per root — the startup debounce (`server-api.md §2`) |
| `<index>/runs/<run_id>/` | per-run scratch: spec, event log, shards (`scan.md §2`) |

OpenIndex had a relocatable index folder (`OpenIndex.location` + a `move_index`
action). That is **not** ported: the home dir is already the app's one
redirection point, and a second, index-only relocation channel would be a
second answer to "where is my data".

## 2. Schemas

`schemas(pa)` is the single definition of both tables.

**`files`** — one row per file (never per directory):

| Column | Type | Notes |
|---|---|---|
| `path` | string | absolute, canonical form — forward slashes (`platform.md §1`); the sort key and dedupe key |
| `dir` | string | parent directory, the join key to `dirs` |
| `name` | string | basename |
| `ext` | string | lowercased, leading `.` stripped; `""` when none |
| `size` | int64 | bytes |
| `mtime` | float64 | epoch seconds |

**`dirs`** — one row per directory:

| Column | Type | Notes |
|---|---|---|
| `dir` | string | absolute |
| `sig` | string | sha1 of sorted entries (`scan-incremental.md §2`) |
| `n_files` | int32 | files directly inside |
| `total_size` | int64 | bytes of those files |
| `mtime_ns` | int64 | reuse decision input |
| `n_subdirs` | int32 | post-prune count; `-1` = pre-upgrade unknown |

Readers tolerate older files: `load_dir_cache` treats a `dirs.parquet` without
`mtime_ns` as no cache at all, and `compact` synthesizes missing `mtime_ns` /
`n_subdirs` columns so an old index can still be merged forward.

## 3. Shards (worker output)

`Sink` accumulates rows in a pool worker and writes three shard kinds into
`<run_dir>/shards/`, tagged per process so filenames never collide:

- **`shard-<tag>-NNNNN.parquet`** — file rows, flushed every `cfg.shard_rows` (200 000).
- **`_dirs-<tag>-NNNNN.parquet`** — directory rows for directories actually scanned.
- **`_keep-<tag>-NNNNN.parquet`** — just `dir`: directories whose existing rows should
  be carried forward untouched.

`keep` is the load-bearing idea: a directory in `_keep` keeps its old file rows without
the worker restating a single file. A directory in **neither** `_dirs` nor `_keep` is
dropped from the index — which is how deletions and newly-ignored folders leave
(`scan-ignore.md §3`).

## 4. Compaction

`compact` merges shards with the previous index using duckdb. Let
`outside` = rows whose `dir` is not the scan root or under it, and `kept` = rows whose
`dir` is in `_keep`.

1. **Diff counts** vs the previous `dirs.parquet` — `changed` (same dir, different
   `sig`), `added`, `removed` (in the old index, not rescanned, not kept). Reported in
   the run summary.
2. **Skip-rewrite fast path** — no shards, no new dir rows, nothing removed, and an
   existing index → the index is left byte-identical; only `partitions.json`'s
   `updated`/`last_root` are refreshed and the summary is flagged `skipped_rewrite`.
3. **Merge** — `old rows WHERE outside OR kept` `UNION ALL` new shard rows, deduped by
   `QUALIFY row_number() OVER (PARTITION BY path ORDER BY mtime DESC) = 1`, then
   `ORDER BY path`.
4. **Partition** — `cfg.part_rows` (500 000) rows per `part-NNNNN.parquet`, row group
   size 65 536, recording each partition's `min`/`max` path and row count.
5. **Directory table** — `old WHERE outside OR kept` `UNION ALL` new dir rows, deduped
   by `dir` keeping the highest `mtime_ns`, sorted by `dir`.
6. **Swap** — `files.new` replaces `files` by `rmtree` + `rename`; `dirs.parquet.new`
   and `partitions.json.new` land via `os.replace`; shards are deleted.

Two consequences worth knowing:

- **Multi-root indexes work.** The `outside` clause preserves rows for trees other than
  the current root, so scanning `~/Documents` after `~/code` keeps both — while
  `stats` reports only the last root (`query.md §2`).
- **The swap is atomic-ish, not atomic.** `files` is removed and renamed as two steps,
  so a crash in that window can leave the index missing. Each individual file swap is
  atomic.

## Non-goals

- **Queries over these files** — `query.md`.
- **Who decides keep vs. drop** — `scan-incremental.md §4`, `scan-ignore.md §3`.
- **The event log and run directories** — `scan.md §2`, `§3`.

## Open questions

- The `files` directory swap is not crash-atomic (§4).
- `partitions.json` records `last_root` only, so an index holding several roots has no
  record of the others.

## See also

- `scan.md` — produces the shards §3 describes.
- `query.md` — the only reader of §2 and the manifest.
- `scan-incremental.md` — `dirs.parquet` doubles as the reuse cache.
- `platform.md` — the canonical path form every column in §2 stores.
