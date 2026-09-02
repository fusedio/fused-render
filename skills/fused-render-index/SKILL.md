---
name: fused-render-index
description: Use when a page or .py needs file search, disk-usage/file-type breakdowns, or SQL over the filesystem — fused.fileIndex instead of walking the fs.
---

# The file index

fused-render keeps a parquet index of the filesystem (one row per file, one per dir). Read it instead of `os.walk`/`find`/per-row stats. Three ways in:

| Need | Path |
|---|---|
| Interactive UI reads (search box, tiles, table) | `fused.fileIndex.*` (JS) |
| Bulk/analytical Python (aggregate, join, pandas) | duckdb over the parquet directly |
| Index management (scan, roots/ignore, repos) | raw `fetch` + `X-Fused: 1` header on POSTs |

## Readiness rule (the one that matters)

Zero rows means BOTH "no matches" and "no index yet". Rendering the second as the first is the silent lie this design exists to prevent. Both JS calls resolve with `ready: {indexed, scanning, stale, reason}` (`reason`: `no-index` / `outdated` / `not-covered` / null); the Python reader returns `(None, None)` for an empty store. `indexed: false` → "building / not indexed", never "no results". `stale` never means "matches the fs" — an index is always slightly behind.

## JS: `fused.fileIndex`

Two methods, that's the whole bridge:

- `search({root, q, limit})` — one folder's corpus, `/api/fs/walk` entry shape + `covered, fresh, age_s, truncated`. Per-keystroke cheap. `covered: false` is a 200 and a state, not an error.
- `query({sql, limit})` — ONE read-only SQL statement over views `files` and `dirs` → `{columns, rows (positional arrays), truncated}`. limit default 200, cap 5000. Totals, breakdowns, path matches — all of it is `query`.

Facts:

- **No bind parameters** — quote user strings by doubling `'` yourself. Guard (`fused_render/index/guarded_query.py`): one statement, read-only types only, two views only, 10 s wall clock, duckdb errors surface verbatim as 400 (show them).
- Index calls are **not superseded like `runPython`'s**: the slower earlier keystroke's reply can land last. Guard your own renders (below).
- A page of rows ≠ match count — ask `count(*)` separately to page.
- Not on the bridge (raw fetch, POSTs need `X-Fused: 1`): `/api/index/scan`, `/api/index/cancel`, `/api/index/config`, `/api/index/status`, `/api/index/stats`, `/api/index/lookup`, `/api/index/runs`, `/api/index/delete`, `/api/index/ask` (spends AI credits — button, not debounce), `GET /api/git-repos`. Shapes: `fused_render/server/routers/` + `fused_render/static/runtime.js` comments. `/api/index/status`'s `error` field is data (last scan's failure), not a throw.

### The canonical shape (per-keystroke)

```js
let generation = 0;
async function drawRows() {
  const mine = ++generation;
  const out = await fused.fileIndex.query({ sql });
  if (mine !== generation) return;   // a newer keystroke owns the table
  if (!out.ready.indexed) { showBuilding(out.ready); return; }
  render(out.rows);                  // positional arrays, per out.columns
}
```

Schemas (`fused_render/index/store.py` `schemas()`): `files(path, dir, name, ext, size, mtime, depth)`, `dirs(dir, sig, n_files, total_size, mtime_ns, n_subdirs, depth)`. Paths absolute POSIX; `ext` lowercase, no dot; `mtime` epoch seconds, `dirs.mtime_ns` nanoseconds.

## Python: read the parquet

App venvs cannot `import fused_render` — copy `reader.py` (beside this SKILL.md, tested in CI) into the app and declare `duckdb` in its `pyproject.toml`. Store: `~/.fused-render/index/` — `partitions.json` (THE manifest), `files/part-*.parquet`, `dirs.parquet`. Everything else is writer-only; readers never take `store_lock`, never write.

- **THE trap: never glob `files/*.parquet`.** Old generations stay on disk beside new ones — a glob double-counts (~2x) silently, and only on machines scanned twice. Open exactly the files `partitions.json` names.
- Use the relation API (`con.read_parquet(list).create_view("files")`) — `CREATE VIEW ... read_parquet(?)` refuses prepared params.
- No manifest / zero partitions / no dirs.parquet → return "not indexed" to the page, don't raise.
- Dev worktrees are branch-scoped stores (`FUSED_RENDER_HOME` + `branches/<sanitized ref>/`, `fused_render/_branch.py`). Pass the `location` that `/api/index/stats` reports rather than resolving the path yourself.

Deeper reference: `fused_render/index/query.py` (and the sanitize rule in `fused_render/_branch.py`).

## Deriving entity kinds

Precedent: git repos = `dirs` rows named `.git` (leaf dirs are recorded, not descended) — zero stats vs ~71k per request. Before probing the fs per row, ask: did the scan already record the marker? BUT read `fused_render/server/routers/git_repos.py` before copying: `junk_path` + `MountGuard` parent-screening and the applied-ignore signature are Python-only — a page-side SELECT gets both kinds of zero wrong. For repos, just `GET /api/git-repos` (`{repos, indexed, scanning, stale, reason}`). A new kind needing those screens belongs in a server route, not a page.

## Pitfalls

- Globbing partitions; rendering `[]` without checking `ready.indexed`; treating `covered: false` as error.
- No generation guard on per-keystroke queries.
- Unescaped `'` in interpolated SQL.
- `import fused_render` from an app `.py`; forgetting `duckdb` in the app's pyproject (dep list is complete — see `fused-render-authoring`).
- POST without `X-Fused: 1` → 403.
- Looking for `fused.index.*` or stats/lookup/scan on the bridge — surface is `search` + `query`, period.
- `/api/index/ask` per keystroke.

Related: authoring the page → `fused-render-authoring`; daemon reading the index → `fused-render-background-apps`.
