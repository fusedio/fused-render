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
| `<index>/files/part-<gen>-NNNNN.parquet` | file rows, globally sorted by path; one generation per compaction (§4) |
| `<index>/dirs.parquet` | one row per directory: signature + reuse metadata |
| `<index>/partitions.json` | manifest: row count, per-partition path ranges |
| `<index>/fsevents.json` | per-root journal position (`scan-incremental.md §3`) |
| `<index>/ignore_applied.json` | ignore fingerprint the index was built with (`scan-ignore.md §4`) |
| `<index>/config.json` | user config: ignore list + scan roots (`server-api.md §3`) |
| `<index>/scans.json` | last scan start per root — the startup debounce (`server-api.md §2`) |
| `<index>/gitignore/<digest>.json` | one index root's pooled `git check-ignore` verdicts, so a restart does not re-sweep — a SERVER-layer cache that only lodges here, age-bounded by `VERDICT_MAX_AGE_S` and untouched by `POST /api/index/delete` (`server/index_gitignore.py`). Written only for an actual **index root**, which is what bounds this directory: a folder outside every configured scan root gets a pool too, but an in-memory one, because nothing ever reclaims a file here and one per folder ever searched would grow without limit. A `<digest>.json.<pid>.new` is a write in progress; one left by a process that died mid-write is swept on the next save. |
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
| `depth` | int32 | slashes in `path` (absolute, not relative to any root) — the corpus sort key (`query.md §6`) |

**`dirs`** — one row per directory:

| Column | Type | Notes |
|---|---|---|
| `dir` | string | absolute |
| `sig` | string | sha1 of sorted entries (`scan-incremental.md §2`) |
| `n_files` | int32 | files directly inside |
| `total_size` | int64 | bytes of those files |
| `mtime_ns` | int64 | reuse decision input |
| `n_subdirs` | int32 | post-prune count; `-1` = pre-upgrade unknown |
| `depth` | int32 | slashes in `dir`; present on both tables because `search_under` UNIONs them |

`name`, `ext`, `dir` and `depth` are all denormalised out of `path`. That is sound
only because rows are never mutated in place — a scan writes a row once, and a
compaction copies whole partitions and swaps the manifest — so no update path can
leave a derived column disagreeing with its source. `depth` is *stored* rather than
derived at read time because every query opens a fresh in-memory DuckDB over
`read_parquet` (`query.md`): there is no persistent catalog to hold a generated
column and parquet has no computed-column concept. It measures 92 ms vs 148 ms on
300k rows at the real `LIMIT 200_001`, for +0.10% on disk.

Readers tolerate older files: `load_dir_cache` treats a `dirs.parquet` without
`mtime_ns` as no cache at all, and `compact` synthesizes missing `mtime_ns` /
`n_subdirs` / `depth` columns so an old index can still be merged forward.
`search_under` falls back to the slash-count expression for a partition set
written before `depth` existed. Migration is a full rescan.

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
4. **Partition** — `cfg.part_rows` (500 000) rows per
   `part-<generation>-NNNNN.parquet`, row group size 65 536, recording each partition's
   `min`/`max` path and row count. The generation is the previous manifest's plus one.
5. **Directory table** — `old WHERE outside OR kept` `UNION ALL` new dir rows, deduped
   by `dir` keeping the highest `mtime_ns`, sorted by `dir`.
6. **Swap** — `dirs.parquet.new` and `partitions.json.new` land via `os.replace`, the
   manifest **last**; shards are deleted; then partitions older than the previous
   generation are reclaimed.

Three consequences worth knowing:

- **Multi-root indexes work.** The `outside` clause preserves rows for trees other than
  the current root, so scanning `~/Documents` after `~/code` keeps both — while
  `stats` reports only the last root (`query.md §2`).
- **The index stays readable throughout a rescan.** New partitions are written *beside*
  the live ones under a fresh generation number, and the manifest — which is what
  readers follow — is swapped atomically at the end. A query landing mid-compaction
  answers from the last completed generation; a crash leaves that generation intact.
  (OpenIndex `rmtree`d the files dir and renamed the new one in, so a query in that
  window found the partitions its manifest named already deleted.) This is what makes
  "keep serving the index while a rescan runs" a fact rather than a hope.
- **Readers must use the manifest, never a glob.** `partition_files` /
  `query._sources` read exactly what the manifest names. A `files/*.parquet` glob would
  see a half-written generation and would also keep counting the previous one, which is
  deliberately left on disk one compaction longer for a reader that loaded the manifest
  microseconds before the swap.

## Non-goals

- **Queries over these files** — `query.md`.
- **Who decides keep vs. drop** — `scan-incremental.md §4`, `scan-ignore.md §3`.
- **The event log and run directories** — `scan.md §2`, `§3`.

## Open questions

- `partitions.json` records `last_root` only, so an index holding several roots has no
  record of the others.
- Two generations of partitions can exist at once, so a large index transiently costs
  up to twice its size on disk (§4). Reclaiming the older one sooner would need a
  reader lease, which is more machinery than the disk is worth.

## See also

- `scan.md` — produces the shards §3 describes.
- `query.md` — the only reader of §2 and the manifest.
- `scan-incremental.md` — `dirs.parquet` doubles as the reuse cache.
- `platform.md` — the canonical path form every column in §2 stores.
