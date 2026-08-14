# File index — spec registry

> **Status — index (shipped).** This file owns the **capability index**: which spec
> owns which concept, and how the pieces fit. It owns no behavioural contract of its
> own — every rule lives in the spec named below.

`fused_render/index/` builds a **local parquet index of filesystem metadata**
(path, name, ext, size, mtime) for a directory tree and queries it with duckdb.
It is a port of the OpenIndex fused-render app into the server; these specs are
that app's specs, updated to the ported reality.

## Capabilities

- **Scan lifecycle** — start a detached scan, watch it via an append-only event log,
  cancel it, resume watching after a reload, reclaim old run dirs (`scan.md`).
- **Incremental reuse** — skip directories that haven't changed, using per-directory
  mtime signatures and (on macOS) the FSEvents journal (`scan-incremental.md`).
- **Ignore list + mount guard** — folders never walked and never indexed, the
  structural refusal of every fused-render home, and the same-filesystem rule that
  refuses every mount nobody named (`scan-ignore.md`, `scan.md §6`).
- **Index store** — on-disk layout, parquet schemas, the partition manifest, and
  compaction (`index-store.md`).
- **Query** — index totals by extension, path lookup with partition pruning, and the
  explorer's in-folder corpus (`query.md`).
- **Server API** — the `/api/index/*` routes and the startup scan scheduler
  (`server-api.md`).
- **Platform support** — the canonical path form every other spec assumes, plus what
  differs on macOS / Linux / Windows (`platform.md`).

## Architecture at a glance

```
routers/index.py ──> runner.py   start / status / cancel / list   ← control plane only
                 │       └─ spawns: python -m fused_render.index.worker <run_dir>  (detached)
                 │                     └─ scan.py: shards ─> store.compact ─> <index dir>
                 └──> query.py   stats / lookup / search_under     ← reads via duckdb
```

The route never blocks on a scan: a full home scan takes minutes, so `start` returns a
`run_id` immediately and the client polls `status`. See `scan.md §1`.

## What changed in the port from OpenIndex

| Concern | OpenIndex | Here |
|---|---|---|
| Configuration | module globals frozen at import (`INDEX_DIR`, `IGNORE`, `NPROC`) | `IndexConfig`, resolved per call, carried to workers in `spec.json` (`config.py`) |
| Store location | `~/.fused-render/cache/OpenIndex*`, relocatable via a location file | `storage.home_dir()/index` — FUSED_RENDER_HOME + branch nesting; no relocation action |
| Worker spawn | `Popen([python, __file__, "--worker", run_dir])` | `python -m fused_render.index.worker <run_dir>` (survives py2app bundling) |
| Run dirs | system temp dir, never cleaned | `<index>/runs`, pruned (`scan.md §2`) |
| Mount safety | an ignore-list entry | the entry, a structural `MountGuard` over every fused-render home, and a walk confined to the scan root's filesystem (`scan-ignore.md §7`, `scan.md §6`) |
| `sql` action | arbitrary duckdb from the page | removed (`query.md §5`) |
| Compaction swap | `rmtree` + `rename` of the files dir (unreadable mid-swap) | generation-numbered partitions, manifest swapped last (`index-store.md §4`) |
| Ignore storage | its own JSON beside the index | `<index>/config.json`, with the scan roots (`server-api.md §3`) |

## Conventions

- **Numbered sections.** Cite as `scan.md §3`.
- **Status markers.** `(SHIPPED)` is the default and is left implicit; anything not
  yet built is marked `(TARGET)` inline.
