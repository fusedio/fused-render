---
name: fused-render-index
description: Use when page or .py needs file search, disk-usage/file-type breakdowns, or SQL over filesystem — fused.fileIndex, not fs walks.
---

# The file index

fused-render keeps parquet index of filesystem (one row per file, one per dir). Read it, don't `os.walk`/`find`/per-row stat. Three ways in:

| Need | Path |
|---|---|
| Interactive UI reads (search box, tiles, table) | `fused.fileIndex.*` (JS) |
| Bulk/analytical Python (aggregate, join, pandas) | duckdb over parquet directly |
| Index management (scan, roots/ignore, repos) | raw `fetch` + `X-Fused: 1` on POSTs |

## Readiness rule (the one that matters)

Zero rows means BOTH "no matches" and "no index yet". Rendering second as first = silent lie this design exists to prevent. Both JS calls resolve with `ready: {indexed, scanning, stale, reason}` (`reason`: `no-index` / `outdated` / `not-covered` / null); Python reader returns `(None, None)` for empty store. `indexed: false` → "building / not indexed", never "no results". `stale` never means "matches fs" — index always slightly behind.

## JS: `fused.fileIndex`

Two methods, whole bridge:

- `search({root, q, limit})` — one folder's corpus, `/api/fs/walk` entry shape + `covered, fresh, age_s, truncated`. Per-keystroke cheap. `covered: false` = 200 + a state, not error.
- `query({sql, limit})` — ONE read-only SQL statement over views `files`, `dirs` → `{columns, rows (positional arrays), truncated}`. limit default 200, cap 5000. Totals, breakdowns, path matches — all `query`.

Facts:

- **No bind parameters** — quote user strings yourself, double `'`. Guard (`fused_render/index/guarded_query.py`): one statement, read-only types, two views only, 10 s wall clock, duckdb errors surface verbatim as 400 (show them).
- Failures reject Error with `.message` (duckdb/server text), `.type`, `.status` — render `.message`.
- Index calls **not superseded like `runPython`'s**: slower earlier keystroke's reply can land last. Guard own renders (below).
- Page of rows ≠ match count — ask `count(*)` separately to page.
- Not on bridge (raw fetch, POSTs need `X-Fused: 1`): `/api/index/scan`, `/api/index/scan-folder`, `/api/index/cancel`, `/api/index/rank`, `/api/index/config`, `/api/index/status`, `/api/index/stats`, `/api/index/lookup`, `/api/index/runs`, `/api/index/delete`, `/api/index/ask` (spends AI credits — button, not debounce), `GET /api/git-repos`. Shapes: `fused_render/server/routers/` + `fused_render/static/runtime.js` comments. `/api/index/status`'s `error` field = data (last scan's failure), not throw.
- Store location fixed: `home_dir()/index` — `/api/index/config` edits roots/ignore, cannot relocate store (only `FUSED_RENDER_HOME` at server start moves it).

### The canonical shape (per-keystroke)

```js
let generation = 0;
async function drawRows() {
  const mine = ++generation;
  const out = await fused.fileIndex.query({ sql });
  if (mine !== generation) return;   // newer keystroke owns table
  if (!out.ready.indexed) { showBuilding(out.ready); return; }
  render(out.rows);                  // positional arrays, per out.columns
}
```

Schemas (`fused_render/index/store.py` `schemas()`): `files(path, dir, name, ext, size, mtime, depth)`, `dirs(dir, sig, n_files, total_size, mtime_ns, n_subdirs, depth)`. Paths absolute POSIX; `ext` lowercase, no dot; `mtime` epoch seconds, `dirs.mtime_ns` nanoseconds.

## Python: read the parquet

App venvs cannot `import fused_render` — copy `reader.py` (beside this SKILL.md, tested in CI) into app, declare `duckdb` in its `pyproject.toml`. Store: `~/.fused-render/index/` — `partitions.json` (THE manifest), `files/part-*.parquet`, `dirs.parquet`. Rest is writer-only; readers never take `store_lock`, never write.

- **THE trap: never glob `files/*.parquet`.** Old generations stay on disk beside new — glob double-counts (~2x) silently, only on machines scanned twice. Open exactly what `partitions.json` names.
- Relation API (`con.read_parquet(list).create_view("files")`) — `CREATE VIEW ... read_parquet(?)` refuses prepared params.
- No manifest / zero partitions / no dirs.parquet → return "not indexed" to page, don't raise.
- Dev worktrees = branch-scoped stores (`FUSED_RENDER_HOME` + `branches/<sanitized ref>/`, `fused_render/_branch.py`). Pass `location` that `/api/index/stats` reports; don't resolve path yourself.

Deeper reference: `fused_render/index/query.py` (+ sanitize rule in `fused_render/_branch.py`).

## Deriving entity kinds

Precedent: git repos = `dirs` rows named `.git` (leaf dirs recorded, not descended) — zero stats vs ~71k per request. Before probing fs per row, ask: did scan already record the marker? BUT read `fused_render/server/routers/git_repos.py` before copying: `junk_path` + `MountGuard` parent-screening + applied-ignore signature are Python-only — page-side SELECT gets both kinds of zero wrong. For repos just `GET /api/git-repos` (`{repos, indexed, scanning, stale, reason}`). New kind needing those screens → server route, not page.

## Pitfalls

- Globbing partitions; rendering `[]` without checking `ready.indexed`; treating `covered: false` as error.
- No generation guard on per-keystroke queries.
- Unescaped `'` in interpolated SQL.
- `import fused_render` from app `.py`; forgetting `duckdb` in app pyproject (dep list complete — `fused-render-authoring`).
- POST without `X-Fused: 1` → 403.
- Looking for `fused.index.*` or stats/lookup/scan on bridge — surface = `search` + `query`, period.
- `/api/index/ask` per keystroke.

Related: page authoring → `fused-render-authoring`; daemon reading index → `fused-render-background-apps`.
