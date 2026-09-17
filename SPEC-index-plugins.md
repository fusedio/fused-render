# Multi-index plugin system, an app-name index, and global ⌘K search

> **Status — building.** This is the agreed scope from a completed
> `scope-requirements` round. Every decision below was made by the user; none
> of it is open for re-litigation. Where this spec is silent, follow the
> existing patterns in `fused_render/index/` and its `specs/` directory.

## Context

The file indexer is one index, hardwired to one store and one row schema. Its
engine is already de-globalized — `IndexConfig.dir` is a plain field, so a
second store is *spellable* today as `load_config(dir=other)` — but nothing
above it is: the HTTP API has no index identifier at all, every route calls a
bare `load_config()`, and the row schema is hardcoded in three places.

Three things are being built, **in this order**. Each is a usable increment;
commit them as separate logical units.

1. **A multi-index engine** — many indexes keyed by `(root, kind)`, with a
   plugin contract that a third-party app can register against.
2. **An app-name index** over `~/Fused` — the first non-file index, and the
   proof the contract carries its own weight.
3. **A global ⌘K search overlay** — one field, results from every index,
   grouped by source.

## The decisions (all eleven, as given)

| # | Decision |
|---|----------|
| 1 | Scope is all three parts, **sequenced** in the order above. |
| 2 | Plugin trust: **extract only, user approves root**. A plugin never enumerates paths. |
| 3 | Results are **grouped by source**. No cross-source score calibration. |
| 4 | **No migration.** The index store is rebuilt from scratch. |
| 5 | ⌘K opens search. The shortcuts overlay is **removed entirely** — surface and listing both. |
| 6 | Ship two kinds — **apps and files** — plus one app-authored example indexer. |
| 7 | Mounted directories are **out of scope entirely**. |
| 8 | Adding an index: **the app proposes, the user confirms**. Never silent. |
| 9 | The example indexer is a **reference implementation**: documented, exercised in tests, not pitched to end users as something to install and use. |
| 10 | Acceptance is **both** the search surface and the contract, **weighted to the contract** — a third party being able to author an indexer from the docs is what decides whether this was worth doing. |
| 11 | The search overlay is **in-app**, not a system-wide OS hotkey. |

## Part 1 — The multi-index engine

### The plugin model: host owns the walk, plugin owns the row

This is the load-bearing design decision and the reason decision #2 reads the
way it does. A plugin does **not** implement `scan()`. The host walks — with
its existing incrementality, its `dirs.parquet` directory signatures, its
FSEvents fast path, its `SCAN_NICE_INCREMENT = 10` and background I/O policy,
its cancel-flag file — and hands the plugin one file at a time. The plugin
returns **a row, or nothing**.

Three consequences, all of them the point:

- A plugin author gets incrementality, throttling and cancellation for free,
  without understanding any of it.
- A plugin cannot wander off its approved root, because it never chooses what
  to visit. This is what makes "extract only, user approves root" enforceable
  rather than advisory.
- **Plugins contribute rows, not results.** Plugin code runs at index time
  only. Query time is SQL plus a declarative column projection, so no
  user Python ever runs on a keystroke.

### What must generalize

Three sites hardcode the row schema. They are the real cost of this part:

- `fused_render/index/store.py` — `Sink`, and the `_compact_locked` SQL at
  roughly `store.py:521`.
- `fused_render/index/query.py` (~1426 lines) — all of it: `resolve_query`,
  `search_ranked`, `_rank_sql`, `files_src` / `dirs_src`, `search_under`,
  `stats`.
- `fused_render/index/guarded_query.py:111` `_connect` — hardcodes
  `CREATE VIEW files AS ...` / `CREATE VIEW dirs AS ...`.

The current schema, from `fused_render/index/specs/index-store.md` §2:

- `files`: `path, dir, name, ext, size:int64, mtime:float64, depth:int32`
- `dirs`: `dir, sig, n_files:int32, total_size:int64, mtime_ns:int64, n_subdirs:int32, depth:int32`

### What already generalizes — do not rewrite it

These are schema-agnostic today. Extend their addressing to carry an index
identity; do not restructure them.

- `fused_render/index/runner.py` — `canonical_root`, `active_run`,
  `start(cfg, root, full=False)`, `read_events`, `derive_state`, `status`,
  `cancel`, `list_runs`, `prune_runs`, `last_scan`.
- `fused_render/index/worker.py:124` — the single dispatch point
  (`run_scan(argv[0])`) that becomes plugin-aware.
- `fused_render/index/cancel.py`.

### Confinement must survive

In `guarded_query._connect`, the DuckDB lockdown order is load-bearing and
must not be reordered or weakened:

```
SET allowed_directories=[<index dir>]
SET enable_external_access=false
SET lock_configuration=true
```

The confinement logic is content-agnostic and generalizes cleanly. Only the
two hardcoded view names do not.

### The API gains an index identifier

Every route in `fused_render/server/routers/index.py` calls a bare
`load_config()` today: `POST /api/index/scan` (:1473), `scan-folder` (:1513),
`cancel` (:1639), `GET status` (:1651), `stats` (:1707), `search` (:1753),
`rank` (:1799), `POST query` (:2062), `ask` (:2135), `GET/POST config`
(:2204/:2212), `POST delete` (:2268).

`fused_render/static/runtime.js` (~4937–5127) exposes `fused.fileIndex` with
`search({root, q, limit})` and `query({sql, limit})`. This is a public contract
user pages are already written against — extend it, keep those two working.

### The management page

Message one of the request was: *"I want to make this more modular and
accessible using a new page."* Build it. It is also where decision #8's
confirmation lives — an app proposing an index surfaces here for the user to
approve or refuse, and approving is what grants a root.

`apps/ai_models` is the precedent for a prefix-routed built-in page: sidebar
entry plus a lazy import in `App.tsx`. Follow it.

The existing Preferences > Indexing panel (`frontend/src/shell/Indexing.tsx`,
524 lines — enable toggle, roots/ignore editors, manual scan/full-scan/delete,
FDA prompt, read-only SQL + AI "ask" console) is the file index's controls.
Decide whether it folds into the new page or stays; if it stays, it must not
end up as a second, disagreeing source of truth.

## Part 2 — The app-name index over `~/Fused`

### Why this needs an index at all

The files index stores **metadata only**. App-ness is not metadata: a folder is
an app because a direct-child `.html` carries `<meta name="fused-app">`
(D301), which requires reading file *content* — `app_listing.has_fused_meta`
does it within a 4 KiB head-read budget. That content read is exactly what an
extractor is for, and it is why this cannot be answered the way
`git_repos.py` and `exported_apps.py` answer theirs.

Study both of those first. They are the house pattern for "derived index
fact", and they establish the failure posture you must copy:
`fused_render/exported_apps.py` ("the index cannot answer" degrades to **zero
rows silently, never a 502") and
`fused_render/server/routers/git_repos.py` ("repo-ness is an INDEX FACT, not a
filesystem probe" — it replaced a version doing ~71k stats per request).

### The row

`app_listing.app_dict()` already produces the shape: `name, tag, path`
(realpath), `entry, entry_html, preview_image, category, icon, updated_at`.
Carry the stable id from `fused_render/app_id.py` —
`<meta name="fused-app-id" content="my-app-test-82de2580">`, minted once on
first export, surviving rename/move/edit. **The id is never a path**: it is
matched, not joined.

### Manifest

Registration goes in the folder's own `pyproject.toml`, parsed the way
`fused_render/background_apps.py:90-184` `load_manifest` parses
`[tool.fused-render.app]`. Note that section enforces **exactly one of**
`daemon` / `main` (`has_daemon == has_main` → reject), which is precisely why
an indexer-only app needs its **own table**, not a new key in `app`. Match its
posture: any parse failure returns `None` **silently**.

### The Apps hub is left alone

`frontend/src/apps/builder/Apps.tsx:285-297` keeps its current client-side
walk-and-filter. This means the app list is derived two ways. The user was
shown this duplication explicitly and accepted it for the smaller blast
radius. **Do not "fix" it.**

## Part 3 — Global ⌘K search

- One overlay, in-app, opened with ⌘K.
- **Delete the shortcuts overlay entirely** — the surface and its listing,
  including the `{ group: "View", keys: [MOD_LABEL, "K"], label: "Show this
  shortcut list" }` entry at `frontend/src/platform/lib/shortcuts.ts:116`.
  Per the repo's standing rule, delete stranded code rather than leaving it
  inert; do not relocate the overlay to `?`.
- Results **grouped by source**, each group ranked within itself. Scores from
  different sources are not comparable and must never be merged into one
  ordering.
- The file-search path **must** reuse `resolve_query` / `search_ranked`. See
  `DECISIONS-one-search-language.md` and `DECISIONS-one-field-search.md`: one
  search grammar, one field. Do not introduce a second matcher.
- `query.py` is mirrored line-for-line against
  `frontend/src/platform/lib/fuzzy.ts`. If you touch ranking, keep them in
  lockstep.
- Per-keystroke cancellation already exists (`CancelToken` / `cancellable`,
  HTTP 499). Use it.

## Constraints

- Results grouped by source; **no cross-source score calibration**.
- File search reuses `resolve_query` / `search_ranked` — one search language.
- App-supplied indexers **never enumerate paths themselves**.
- The Apps hub keeps its current behavior; the resulting duplication is accepted.
- DuckDB lockdown order in `guarded_query._connect` is preserved exactly.
- `fused.fileIndex.search` / `.query` keep working.
- Index failure degrades to zero rows, never an error page.

## Out of scope

- Mounted directories and `~/.fused-render/mount/*`. `MountGuard` blocks every
  fused-render home whole (`blocks` by string compare, `blocks_root` by
  realpath via `mounts.is_mount_backed`) and `runner.start` refuses such a root
  outright. Per `fused_render/index/specs/scan-ignore.md` §7, a kernel
  `scandir`/`stat` on an rclone NFS mount "can **wedge the mount
  permanently**". Leave all of it exactly as it is.
- A system-wide OS-level hotkey.
- Running actions or app commands from results.
- Image-embedding indexes (`~/Pictures`). The `(root, kind)` contract must not
  *preclude* one later, but none ships here.
- Any migration of the existing index store — it is rebuilt from scratch.

## Verification

Python: `/home/iamsdas/Work/fused-render/.venv/bin/python -m pytest <paths> -q`
(the worktree has no venv of its own; that interpreter resolves imports to the
worktree because cwd wins).

Relevant existing suites — keep them green, extend them rather than replacing:
`tests/test_index_query.py`, `test_index_store.py`, `test_index_api.py`,
`test_index_rank.py`, `test_index_search.py`, `test_index_runner.py`,
`test_index_guarded_query.py`, `test_index_mount_safe.py`,
`test_apps_api.py`, `test_exported_apps.py`, `test_background_apps.py`,
`test_search.py`, `test_search_index.py`, `tests/test_index_config.py`.

Frontend: `cd frontend && bun install && bun run build` — **`bun`, never
`npm`**, regardless of what any file in the repo says. The Python suite needs
`fused_render/static/shell-dist/` to exist, so build the shell before running
API tests.

Baseline at branch point: 377 passed across
`test_index_query test_index_store test_index_api test_apps_api test_index_rank test_search`.

**Keep the inner loop scoped.** Run the narrowest tests covering each change.
The full suite is the orchestrator's job, once, at the end — do not run it.
