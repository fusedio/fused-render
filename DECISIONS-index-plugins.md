# Decisions — index plugins / app-name index / global search

## What landed

- **The plugin contract (`fused_render/index/kinds.py`).** Two frozen
  dataclasses, `Column` and `IndexKind`, plus a module-level registry
  (`register`/`get`/`registered`). `Column.type` is restricted to the four
  physical types `store.schemas()` already uses (`string`, `int64`,
  `int32`, `float64`), so a third-party kind cannot declare a column the
  compaction SQL or `guarded_query`'s typed empty-table stand-ins don't
  know how to carry. `extract(path, st)` is the entire plugin surface: one
  file, the `os.stat` result the host walker already has, return a row-dict
  or `None`. `IndexKind` has no `scan()` method at all — "host owns the
  walk, plugin owns the row" is enforced by the contract not offering a
  walk method, not by a runtime check. `register()` rejects a duplicate
  name unless `replace=True` is passed explicitly.
- **Identity and recency.** `IndexKind` carries `identity_column` (must
  name a declared `"string"` column) and `recency_column` (numeric,
  requires `identity_column`), used by compaction's dedup/tie-break, and
  `identity_is_dir` (default `False`) for a kind whose identity value names
  a directory rather than a file — the built-in "apps" kind sets this,
  since an app's identity is its own folder, not a file inside it.
  `store._dir_expr(identity_col, identity_is_dir)` returns the identity
  column unchanged for a dir-identity kind instead of stripping a trailing
  path segment from it, which would otherwise silently walk an app's
  identity up to its *parent* directory and drop the row on the next
  incremental rescan of that folder.
- **Kind-aware config (`fused_render/index/config.py`).** `IndexConfig.kind`
  (default `"files"`); `index_dir(kind)`/`load_config(dir, kind)` both take
  it. `index_dir("files")` still resolves to the original, unmigrated
  `home_dir()/index` path; every other kind nests under it as
  `home_dir()/index/<kind>`.
- **The built-in "apps" kind (`fused_render/index/apps_kind.py`).**
  `extract(path, st)` returns a row exactly when `path` is the canonical
  entry of its folder (`app_listing.app_entry`); any other `.html` in the
  same folder returns `None`, so one app contributes exactly one row. Built
  from `app_listing.app_dict`, with `tag` dropped from the row shape
  (`app_dict` requires it for the Apps hub's own "Folders" facet, which a
  root-less name index has no equivalent of). `identity_column="path"`,
  `identity_is_dir=True`, `recency_column="updated_at"` (the app entry
  page's own mtime).
- **`apps_kind.register_builtin(replace=True)` is called from two places
  that must each see it independently**: at import time of
  `routers/index.py` (so a running server, or a test that imports the
  router directly without going through `create_app()`, always has "apps"
  registered), and at the top of `fused_render/index/worker.py` (the
  detached `python -m fused_render.index.worker` scan subprocess never
  imports the router — it only imports `scan.py`/`worker.py` — so without
  its own registration call, a worker process that spawned to scan the
  "apps" kind would raise `KeyError` inside `_kind_obj(cfg)` before the
  scan's own `try` even starts, and die with no `run_end` event at all).
- **Third-party manifests (`fused_render/index/manifest.py`).**
  `IndexManifest(folder, module, kind)`, parsed from a folder's own
  `[tool.fused-render.index]` table (separate from `background_apps`'s
  `[tool.fused-render.app]`, since indexing opts in every scan rather than
  on demand). `load_manifest` never imports the declared `module` — parsing
  is side-effect-free; importing and registering a third party's
  `IndexKind` only happens once a user has confirmed it. Propose/confirm
  state is a JSON file (`<home>/index_proposals.json`, `pending`/
  `confirmed` lists, realpath-normalized); confirmation is sticky — an
  already-confirmed folder proposing again is a silent no-op, never a
  demotion back to pending.
- **A worked reference example (`fused_render/index/examples/notes_indexer/`).**
  A standalone folder (its own `pyproject.toml` + `indexer.py`, not part of
  the `fused_render` package) indexing markdown files by heading and word
  count — the thing `specs/index-plugins.md` points a third-party author at.
- **Schema/store generalization (`fused_render/index/store.py`).**
  `schemas(pa, kind)` and `Sink(..., kind)`: `"files"` keeps its original,
  byte-identical schema and row-append path; any other kind asks its
  registered `IndexKind` for its schema and appends dict rows (the shape
  `extract` returns) by column name. `Sink.add`'s generic branch reads a
  row's declared columns with `.get(name)` rather than `[name]`, so a
  plugin's row dict that omits a declared key (a partial or degraded
  extraction) writes `None` for that column instead of raising `KeyError`
  and taking the whole scan down — the same "a plugin must never take the
  host's scan down" rule the `extract()` error handling below follows.
  `_compact_locked` derives its identity/recency ordering and containing-
  directory expression from the registered kind rather than hardcoding
  `path`/`mtime`/`dir`, so a non-"files" kind's rows dedupe and compact
  through the same code path "files" always has.
- **Schema-driven sandbox views (`fused_render/index/guarded_query.py`).**
  The `files`/`dirs` views' typed empty stand-in (used when an index has no
  partitions yet) is built from any registered kind's schema rather than a
  literal "files"-shaped SQL string, so an empty non-"files" index still
  reports its own columns through `DESCRIBE` instead of the wrong ones. The
  DuckDB lockdown order (`allowed_directories` →
  `enable_external_access=false` → `lock_configuration=true`) is unchanged.
- **A plugin's `extract()` raising must not end the scan.** In
  `fused_render/index/scan.py`, the call to `kind_obj.extract(path, st)`
  is wrapped in its own `try/except Exception` (distinct from the walker's
  existing `except OSError` around the rest of the per-file work), logging
  and treating the file as contributing no row on any exception. Before
  this, only `OSError` was caught; any other exception a plugin's
  `extract` raised propagated out of the walker and killed the entire run,
  and routing it through the `OSError` handler alone would also have
  skipped the file's signature/size bookkeeping that must still happen
  regardless of what the plugin did with the row.
- **A flat-kind ranker (`search_apps_ranked` in `fused_render/index/query.py`).**
  `_rank_sql`/`_glob_sql` already took an abstract `inner` subquery
  (`rel, size, mtime, is_dir, depth, nm, lrel`) rather than the files/dirs
  schema directly, so `search_apps_ranked` shapes a different `inner` from
  any registered kind's `identity_column`/`text_column`/`recency_column`
  and reuses both primitives unchanged. `resolve_query`/`search_ranked`
  stay byte-identical for `"files"`. Every return shape — the two early
  returns (no `identity_column`, unbuilt index) and the successful result —
  carries `covered: True`, so a caller (the HTTP route, and in turn the
  global search overlay) can tell a real, empty answer apart from "this
  kind cannot be searched yet" without a separate flag.
- **Route generalization (`fused_render/server/routers/index.py`).** Every
  route that used to assume the single "files" store now resolves a `kind`
  parameter (via `_kind_param`, defaulting to `"files"`) and loads that
  kind's own `IndexConfig`. `/api/index/rank` branches: `"files"` keeps its
  existing `_rank_worker` behavior exactly; any other kind runs
  `_rank_flat_kind_worker`, built on `search_apps_ranked`, with no `root`
  required (a flat kind has no navigable folder for a client to name — only
  its own fixed default root, `fused_dir()` for "apps" via
  `_default_root(kind)`). `/api/index/search` degrades to the same zero-row
  "not covered" shape a never-scanned index returns, rather than raising,
  for a kind whose schema doesn't have the columns `search_under` needs.
  `/api/index/stats` and `/api/index/ask` are left as accepted, documented
  gaps for a non-"files" kind (their SQL and system prompt are still
  files/dirs-shaped) since neither sits on the global search overlay's
  path. `GET /api/index/kinds` lists `["files", *kinds.registered()]`.
  `run_startup_scan`/`run_startup_warm` remain "files"-only; a non-"files"
  kind is only ever scanned on demand.
- **`GET /index` is a real route (`fused_render/server/routers/shell.py`).**
  The management page needs its own `@router.get("/index")`, alongside the
  existing route, so refreshing or bookmarking `/index` serves the page
  instead of 404ing.
- **The index management page (`frontend/src/shell/IndexManager.tsx`,
  `/index`).** Lists every kind from `/api/index/kinds` with live status
  and Scan/Full scan/Delete — the one place a non-"files" kind is visible
  or manageable at all. `IndexProposalsDock.tsx`'s `useIndexProposals` poll
  hook is exported and shared between the status-bar chip and this page's
  `ProposalsSection`, so there is exactly one piece of client state for
  "what is pending," not two that could disagree. `Indexing.tsx`
  (Preferences > Indexing) stays where it is — its roots/ignore editors,
  FDA prompt, and SQL/AI-ask console remain "files"-specific and reachable
  by their existing bookmarked URL; the new page links out to it rather
  than duplicating it.
- **A manual `refresh()` must collapse onto the mount effect's own poll,
  not fork a second one.** `useIndexProposals`'s `poll` clears its own
  previously-scheduled `setTimeout` at the top of every call, before
  scheduling its next one. Without this, a manual `refresh()` call (fired
  by `onConfirm`/`onRefuse`) started an additional, uncancelled polling
  chain on top of whatever the mount effect had already scheduled — one
  extra live chain per confirm/refuse, forever, doubling again if the same
  hook was also mounted on the management page.
- **The ⌘K global search overlay (`frontend/src/shell/GlobalSearchOverlay.tsx`).**
  One in-app dialog (not an OS-wide hotkey), opened by the same Mod+K
  chord the deleted shortcuts overlay used. Results are grouped by source
  — "Files" and "Apps" — and each group is ranked entirely within itself:
  there is no merged list and no cross-source score calibration. The files
  group calls `indexRank` (the same `/api/index/rank` →
  `resolve_query`/`search_ranked` path the in-folder home search already
  uses); the apps group calls `indexRankKind("apps", …)`
  (`/api/index/rank?kind=apps` → `search_apps_ranked`). Both groups only
  render hits when the response's `covered` is true — an uncovered or
  not-yet-built kind renders as no results for that group, not an error.
  Per-keystroke `AbortController` cancellation, not a debounce, for each
  group independently. A file hit opens via `navigate(base + "/" +
  hit.rel, { isDir })`; an app hit opens its own management page via
  `navigateUrl(appPageUrl(hit.rel))` — running or launching an app from a
  search result is out of scope, matching the spec.
- **A hit is only labelled `"~/…"` when its base really is the home
  directory.** `displayText(hit, base, home)` rebases to `"~/" + hit.rel`
  when `base === home`, not merely when `base` is truthy. `/api/index/rank`'s
  `base` is whatever root a query actually resolved against — which can be
  a mount or any other configured root that is not the home directory — so
  the earlier truthiness check mislabeled every hit under such a root as
  if it lived under the user's home.
- **Chassis reuse, not a second dialog implementation.** The overlay renders
  inside the shared `Modal` (focus trap, Esc, backdrop-close, animation),
  the same choice the shortcuts overlay had already made; it adds only the
  overlay-lock hold (`acquireOverlay`/`releaseOverlay`) and a Mod+K-toggles-
  closed listener on top, matching that overlay's own precedent.
- **The shortcuts overlay is deleted outright, not relocated.**
  `platform/ui/ShortcutsOverlay.tsx`, `platform/lib/shortcuts.ts` (including
  its own Mod+K entry), and `styles/shortcuts.css` are removed; `shell.css`'s
  import of that stylesheet is replaced with the new overlay's own
  `styles/global-search.css`. Mod+K is not moved to another chord — it
  simply means search now.

## Non-goals, explicitly

- No cross-source score calibration in the global search overlay, ever —
  a file's score is never compared against an app's.
- The overlay never runs an app or fires a command from a result; an app
  hit only ever navigates to that app's own management page.
- `/api/index/stats` and `/api/index/ask` are not generalized to a
  non-"files" kind's schema — calling either against one raises, by
  design, rather than silently answering against the wrong columns.
- Startup scans remain "files"-only; no other kind is auto-scanned at boot.

## Tests

`tests/test_index_kinds.py`, `tests/test_index_config.py`,
`tests/test_index_apps_kind.py`, `tests/test_index_manifest.py`,
`tests/test_index_examples_notes.py`, `tests/test_index_store.py`,
`tests/test_index_scan.py`, `tests/test_index_guarded_query.py`,
`tests/test_index_apps_search.py`, `tests/test_index_kind_routes.py`,
`tests/test_index_manifest_api.py`, `tests/test_index_worker_e2e.py`
(a real `python -m fused_render.index.worker` subprocess scan, an
incremental rescan, and both read back through `/api/index/rank`) on the
backend; `IndexProposalsDock.test.tsx`, `index-proposals-lib.test.ts`,
`GlobalSearchOverlay.test.tsx`, `StatusBar.test.tsx`,
`exclusiveSection.test.tsx` on the frontend.
