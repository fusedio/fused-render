# Decisions — index plugins / app-name index / global search

Living log of decisions taken, dead ends ruled out, and anything the spec
got wrong, kept up to date as the build proceeds (per the orchestrator's
instructions — not a postmortem written at the end).

## Scope for this session

Full completion of all three spec parts — a fully schema-generic
scan/store/query/router stack, the complete apps-kind index with a
manifest-driven propose/confirm HTTP flow and frontend, AND the ⌘K overlay
replacing the shortcuts surface — is not realistically achievable in one
session at the codebase's existing rigor (dense docstrings, TDD per site,
self-review per diff, narrow test runs only). Scoped to the highest-value,
lowest-risk slice of Part 1 first, built as separate committed units, with
the rest left as clearly pointed-to follow-on work rather than attempted
and left half-done.

Order of units:
1. `kinds.py` — the plugin contract + registry (this unit).
2. `config.py` — `kind`-aware `IndexConfig`/`index_dir()`.
3. (stretch) `store.py` schema/Sink generalization, kept byte-identical
   for the "files" kind.
4. (stretch) `guarded_query.py` view-creation generalization.
5. Part 2: `apps_kind.py`, `manifest.py`, one example indexer.
6. Part 3: ⌘K overlay + shortcuts-overlay deletion.

Whatever is not reached gets an exact resume pointer in this file rather
than a half-written attempt.

## Unit 1 — `fused_render/index/kinds.py`

**Design**: two frozen dataclasses (`Column`, `IndexKind`) plus a
module-level registry (`register`/`get`/`registered`). `Column.type` is
restricted to a closed set (`string`, `int64`, `int32`, `float64`) — the
four physical types `store.schemas()` already uses for `files`/`dirs` — so
a third-party kind cannot declare a column the compaction SQL or
`guarded_query`'s typed empty-table stand-ins don't know how to carry.
`IndexKind.__post_init__` rejects duplicate column names and a
`text_column` not among the declared columns (query.py's ranking needs
exactly one fuzzy-matchable text column per kind, mirroring the `nm` field
`search_ranked` already builds for files).

`extract(path, st)` is the entire plugin surface: one file, the `os.stat`
result the host walker already has, return a row-dict or `None`. No
`scan()` method exists on `IndexKind` at all — "host owns the walk, plugin
owns the row" is enforced by the contract simply not offering a walk
method, not by a runtime check.

`register()` defaults to rejecting a duplicate name (raises `ValueError`)
and requires `replace=True` to overwrite deliberately — registering is a
rare, explicit act (once at import for a built-in kind, once at
manifest-confirm for a third-party one), so silent overwrite is far more
likely a bug than an intended upgrade path.

**Not yet decided / explicitly deferred**: how a THIRD-PARTY kind's
`extract` gets invoked safely — sandboxing, exception containment,
timeout-per-file — is Part 2's `manifest.py` concern, not this module's.
This module only defines the shape of a registered kind; it does not yet
wire anything into `scan.py`'s walker. That wiring (the `sink.add()` call
sites in `scan.py` at roughly lines 236, 304/311, 500/525, 633) is
deferred — see "Remaining work" below.

**Tests**: `tests/test_index_kinds.py`, 9 tests, TDD (watched fail on
`ModuleNotFoundError`, then implemented, then watched pass). Covers:
invalid column type, duplicate column names, text_column not in columns,
`pa_schema()` output, register/get round trip, duplicate-name rejection,
`replace=True` overwrite, `get()` on an unknown name, `registered()`
sortedness.

## Unit 2 — `fused_render/index/config.py`

**Design**: `IndexConfig.kind: str = "files"` added; `index_dir(kind="files")`
and `load_config(dir=None, kind="files")` both take the new parameter.
`index_dir("files")` resolves to the byte-identical `home_dir()/index` path
it always has (zero migration) — every other kind nests under it as
`home_dir()/index/<kind>`. `to_dict()`/`from_dict()` both carry `kind` now.

**Bug found and fixed in passing**: `save_config()` called
`load_config(cfg.dir)` with no `kind`, which would have silently reset
`cfg.kind` back to `"files"` on every save for any non-files config. Fixed
to `load_config(cfg.dir, kind=cfg.kind)`. Covered by
`test_save_config_preserves_a_non_files_kind`.

**Left alone deliberately**: `to_dict()`'s pre-existing omission of `roots`
(present in `from_dict`'s known-keys set but never written by `to_dict`) is
untouched — it predates this work, is not part of the kind generalization,
and fixing incidental pre-existing bugs outside the task's scope risks
masking a behavior something else already depends on. Flagged here in case
a future unit needs `roots` to survive a worker round-trip, at which point
it should be fixed with its own test and its own commit.

**Tests**: extended `tests/test_index_config.py` with 7 new tests (TDD:
written, watched fail with `TypeError`/`AttributeError`, then made to
pass). Full suite plus `test_index_store`, `test_index_api`,
`test_index_query`, `test_index_rank`, `test_search` re-run clean (317
passed) to confirm no regression from the `index_dir()` signature change.

## Open questions carried forward (not yet resolved)

- `config.py`'s `to_dict()` omits `roots` even though `from_dict()`'s
  known-keys set includes it — needs checking against
  `test_to_dict_from_dict_round_trip_carries_the_store_location` before
  editing `to_dict()`/`from_dict()` for the new `kind` field, so the `kind`
  field doesn't repeat the same asymmetry by accident.
- `query.py`'s `_rank_sql`/`_glob_sql` appear to already be reusable
  against an abstract "inner" subquery shape (`rel, size, mtime, is_dir,
  nm, lrel, depth`) rather than being hardwired to the files/dirs schema.
  Working hypothesis (not yet implemented or tested): keep
  `resolve_query`/`search_ranked` byte-identical for "files" (satisfying
  the spec's "file search MUST reuse resolve_query/search_ranked
  unchanged"), and give the apps-kind search its own function that shapes
  its own `inner` and calls the same `_rank_sql`/`_glob_sql` primitives —
  satisfying "query.py generalizes" without a full rewrite of a
  1427-line file. Needs verification once Part 2 starts.

## Remaining work (exact resume pointers)

- `config.py`: add `kind: str = "files"` field; generalize
  `index_dir()` to accept a `kind` parameter, `kind == "files"` must
  produce the CURRENT unchanged path (zero migration risk); extend
  `tests/test_index_config.py` via TDD.
- `store.py`: generalize `schemas(pa)` and `Sink.__init__` to consult
  `kinds.get(kind).pa_schema(pa)` instead of the hardcoded files/dirs
  tuple, keeping "files"/"dirs" byte-identical. `_compact_locked`'s SQL
  (~store.py:521) embeds literal column names and needs to become
  schema-driven — this is the highest-risk remaining piece, budget
  accordingly.
- `guarded_query.py`: `_connect`'s `CREATE VIEW files AS...`/`CREATE VIEW
  dirs AS...` and the `_EMPTY_FILES`/`_EMPTY_DIRS` stand-ins need a
  kind-driven equivalent for a second index to be queryable through the
  same sandbox. The DuckDB lockdown order (`allowed_directories` →
  `enable_external_access=false` → `lock_configuration=true`) MUST NOT
  change relative order when this is touched.
- `scan.py`: the actual wiring of `IndexKind.extract` into the live
  walker's `sink.add()` call sites (lines ~236, ~304/311, ~500/525, ~633)
  is not done. This is what makes a registered kind actually get indexed,
  as opposed to merely declarable.
- Part 2 (`apps_kind.py`, `manifest.py`, example indexer under
  `index/examples/`) not started.
- Part 3 (⌘K overlay, shortcuts-overlay deletion at
  `frontend/src/platform/lib/shortcuts.ts:116`) not started.
- Router-level generalization of `routers/index.py`'s per-route
  `load_config()` calls to accept an index identifier: not started.
- Management-page frontend work (coexisting with or folding into
  `frontend/src/shell/Indexing.tsx`): not started.
