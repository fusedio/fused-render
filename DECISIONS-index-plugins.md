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

## Unit 3 — `fused_render/index/apps_kind.py`

**Design**: `extract(path, st)` returns a row exactly when `path` is the
CANONICAL entry of its folder (`app_listing.app_entry(folder)`, compared by
`os.path.abspath` equality) — any other `.html` in the same folder (a
multi-page app's non-entry pages) returns `None`, so one app contributes
exactly one row. Built from `app_listing.app_dict` (the same shape
`GET /api/apps` serves) plus `app_id.app_id` for the stable identity.

**Decision made here, not pre-planned**: dropped `tag` from the row shape
entirely rather than trying to derive it. `app_dict` requires a `tag`
positionally (the Apps hub's "Folders" facet — first path segment under
ITS OWN workspace-root walk), but `extract(path, st)` has no root context
to derive an equivalent from, and nothing about a global name index needs
it. Call `app_dict(..., "", ...)` and `row.pop("tag", None)` before
returning. Documented at length in both the module docstring and
`specs/index-plugins.md §3`, so a future reader doesn't mistake the
omission for an oversight.

**Tests**: `tests/test_index_apps_kind.py`, 8 tests (TDD). Covers:
non-html rejection, no-marker rejection, non-entry-sibling rejection,
successful extraction with id/title, missing-id degrade, never-raises on
an unreadable/vanished folder (using a bare `os.stat_result` rather than a
real stat, since the point is the folder disappearing between visit and
extract), column-shape/row-key parity, and registry registration.

## Unit 4 — `fused_render/index/manifest.py`

**Design**: `IndexManifest(folder, module, kind)`, parsed from a folder's
own `[tool.fused-render.index]` table — a SEPARATE table from
`background_apps`'s `[tool.fused-render.app]`, because indexing is a
different opt-in shape (runs every scan, not on demand). `load_manifest`
mirrors `background_apps.load_manifest`'s exact defensive posture and
containment-check style almost line for line (tomllib/tomli fallback,
`(OSError, TOMLDecodeError)` swallow, realpath containment guard, file-not-
directory check). Deliberately never imports the declared `module` —
parsing is side-effect-free; importing and registering a third party's
`IndexKind` is the act decision #8 gates on confirmation, so it stays the
caller's job, done only against `confirmed_folders()`.

Propose/confirm store: a JSON file (`<home>/index_proposals.json`) with
`pending`/`confirmed` lists, realpath-normalized, mirroring
`background_apps`'s autostart-store conventions. `propose_index` returns
False outright for a folder with no valid manifest. Confirmation is
STICKY — re-proposing an already-confirmed folder is a silent no-op, never
a demotion back to pending — because the alternative (an app calling
propose again on every startup silently un-confirming itself) would defeat
the entire point of decision #8's "never silent, never re-prompt fatigue"
posture.

**Deferred**: the actual HTTP route(s) that let a running app call
`propose_index` and a user click call `confirm_index`/`refuse_index`, and
the frontend confirmation UI. This module is the tested backend only.

**Tests**: `tests/test_index_manifest.py`, 15 tests (TDD). Covers manifest
parsing (missing pyproject, corrupt toml, missing table, missing
module/kind, path escaping via `../`, a directory passed as module) and
the full propose/confirm/refuse lifecycle including the sticky-confirmation
case. Store path is isolated per test via an autouse fixture monkeypatching
`manifest._store_path`, so tests never touch a real home dir.

## Unit 5 — `fused_render/index/examples/notes_indexer/`

**Design**: the one app-authored EXAMPLE indexer decisions #6/#9 call for
— a real, standalone folder (its own `pyproject.toml` + `indexer.py`, NOT
part of the `fused_render` package: no `__init__.py`, nothing imports it
by package path) indexing markdown files by first-`# `-heading title and
word count. Deliberately trivial row shape, chosen to be checkable by eye
in a small fixture.

**Test approach decision**: `tests/test_index_examples_notes.py` loads the
example the way a REAL third-party folder would be loaded — parse its
manifest via `manifest.load_manifest`, then `importlib.util.spec_from_
file_location` on exactly the `module` path the manifest names — never a
package-style `from fused_render.index.examples.notes_indexer import
indexer`. This is the test that most directly exercises the acceptance
criterion ("a third party being able to author an indexer from the docs")
end to end: manifest format, parsing, and loading all proven together, not
just each function tested in isolation.

Written test-after rather than test-first (an acknowledged, deliberate
deviation from strict TDD for this one unit): the example's whole point is
to BE the readable reference, so it was written in one pass to read
cleanly top to bottom, then verified with a full test pass immediately
after — a case judged to be about documentation-shaped code rather than
behavior under specification, where TDD's fail-first step would have
added no signal (there was no ambiguity about intended behavior to
pin down before writing it). Flagging this explicitly per "self-review
every diff" rather than silently skipping the discipline.

## Unit 6 — `specs/index-plugins.md`

New house-style spec doc: contract (§1-2), built-in apps kind (§3),
third-party manifest (§4), reference example (§5), Non-goals, and an
explicit Open Questions section enumerating every piece of wiring this
session did NOT reach (scan.py's walker call sites, store.py's schema/
compaction generality, guarded_query.py's per-kind views, the confirm/
refuse HTTP route). Linked from `specs/overview.md`'s capability list.

## Session outcome: PARTIAL-DONE

**What's built, tested (58+ narrow tests, all green), and committed as 7
separate logical units**: the full plugin contract (`kinds.py`), a
kind-aware `IndexConfig`/`index_dir()`/`load_config()` (`config.py`), the
built-in "apps" kind (`apps_kind.py`), the third-party manifest +
propose/confirm registry (`manifest.py`), one fully worked reference
example indexer (`examples/notes_indexer/`), and a house-style spec doc
(`specs/index-plugins.md`, linked from `overview.md`).

**What remains, in priority order, with exact resume pointers**:

1. **`scan.py` wiring** (Part 1, stretch — not reached): call a registered
   kind's `extract` from the walker's `sink.add()` sites (lines ~236, ~304/
   311, ~500/525, ~633 per the earlier read of this file) so a registered
   kind actually gets indexed by a live scan, not just declared/testable in
   isolation. This is the single highest-value next step — nothing above
   is provably wired into a real scan yet.
2. **`store.py` schema/compaction generality** (Part 1, stretch — not
   reached, HIGH RISK): `schemas(pa)`/`Sink.__init__` need to consult
   `IndexKind.pa_schema` for a non-"files" `cfg.kind`, and `_compact_locked`
   (~store.py:521, ~170 lines of SQL literally naming `path, dir, name,
   ext, size, mtime, depth`) needs to become column-list-driven. Budget
   this as its own multi-turn unit with its own TDD pass against
   `test_index_store.py`; do not attempt it inside a unit doing anything
   else, given how easily a slip here could silently corrupt the "files"
   compaction path the 377-test baseline depends on.
3. **`guarded_query.py` per-kind views** (Part 1, stretch — not reached):
   `_connect`'s `CREATE VIEW files/dirs AS...` and `_EMPTY_FILES`/
   `_EMPTY_DIRS` need a kind-driven equivalent. MUST preserve the exact
   lockdown order (`allowed_directories` → `enable_external_access=false`
   → `lock_configuration=true`) — do not reorder these three `SET`s.
4. **Apps-kind search** (Part 2 tail — not reached): per the architecture
   insight in the "Open questions carried forward" section below,
   `resolve_query`/`search_ranked` should stay byte-identical for "files";
   a NEW function shapes an apps-kind `inner` subquery (its `name` mapped
   to `rel`/`nm`) and calls the same `_rank_sql`/`_glob_sql` primitives.
   Not started — needs its own read of `query.py`'s exact `_rank_sql`/
   `_glob_sql` signatures before writing, and its own test file (likely
   `tests/test_index_apps_search.py` or an extension of
   `test_index_query.py`).
5. **Confirm/refuse HTTP route + frontend UI** (Part 2 tail — not
   reached): `manifest.propose_index`/`confirm_index`/`refuse_index` have
   no caller yet. A route under `routers/index.py` (or a new small router)
   plus a confirmation surface in the frontend (a toast/dialog, not a
   silent auto-enable) is the remaining half of decision #8.
6. **Router generalization** (Part 1/2 tail — not reached):
   `routers/index.py`'s per-route bare `load_config()` calls need to
   accept an index identifier (root, kind) rather than assuming the single
   "files" store.
7. **Management page** (deferred, not started): folding a second index's
   status into, or coexisting with, `frontend/src/shell/Indexing.tsx`.
8. **Part 3 in its entirety** (not started): delete the shortcuts overlay
   (surface + `frontend/src/platform/lib/shortcuts.ts:116`'s listing
   entry — DELETE, do not relocate to `?`), build the in-app ⌘K overlay,
   grouped by source with no cross-source score calibration, file search
   reusing `resolve_query`/`search_ranked` verbatim, app search reusing
   whatever function item 4 above produces, per-keystroke cancellation via
   the existing `CancelToken`/HTTP 499 machinery. None of this has been
   touched — no frontend files read or edited this session beyond the
   pointers already in SPEC-index-plugins.md.

**Why stopped here rather than pushing into item 1 or 2**: the remaining
items are large, cross-cutting, and — especially items 2 and 8 — each
individually comparable in size to everything built so far. Attempting
`store.py`'s compaction generalization or the frontend overlay without a
full fresh turn budget for its own TDD cycle, self-review, and narrow
test pass risked exactly the outcome the orchestrator's instructions
warn against: a half-finished, uncommitted, or under-tested change to a
load-bearing, previously-377-tests-green file. Every unit actually
attempted this session was carried to a fully green, narrowly-tested,
committed state; none was left mid-edit.

## Open questions carried forward (not yet resolved)

- `config.py`'s `to_dict()` omits `roots` even though `from_dict()`'s
  known-keys set includes it — needs checking against
  `test_to_dict_from_dict_round_trip_carries_the_store_location` before
  editing `to_dict()`/`from_dict()` for the new `kind` field, so the `kind`
  field doesn't repeat the same asymmetry by accident.
- **Resolved by Unit 11**: `query.py`'s `_rank_sql`/`_glob_sql` were indeed
  already reusable against an abstract "inner" subquery shape (`rel, size,
  mtime, is_dir, nm, lrel, depth`) rather than being hardwired to the
  files/dirs schema. `search_apps_ranked` keeps `resolve_query`/
  `search_ranked` byte-identical for "files" and shapes its own `inner` for
  any registered kind with an `identity_column`, calling the same
  `_rank_sql`/`_glob_sql` primitives unchanged.

## Unit 7 — `schemas()`/`Sink` generalization (store.py)

**Design**: `schemas(pa, kind="files")` — `kind == "files"` returns the
exact literal tuple it always has (never routed through the registry, even
if something registered a kind literally named "files" — the check is on
the string, not a lookup). Any other kind asks `kinds.get(kind).pa_schema
(pa)`. The dirs schema is returned unchanged regardless of kind: directory-
reuse bookkeeping is the host's own, not plugin data, so every index shares
one dirs table shape.

`Sink` now takes `kind="files"` and branches its row-append in `add`: for
"files" it keeps the ORIGINAL fixed six-tuple unpacking
(`fr[0]`..`fr[5]`) verbatim — byte-identical, not merely
behaviorally-equivalent code. For any other kind, `frows` are dicts (the
shape `IndexKind.extract` returns) appended generically by walking
`self.file_schema.names`. The row-count flush check and the "any rows
pending" check in `_flush_files` no longer name `"path"` directly (a
non-files kind may not have that column) — both use
`self.file_schema.names[0]` instead, which works for every kind since
every column gets exactly one append per frow.

`Sink.add`'s second positional parameter is renamed from `kind` (the scan
status, "u"/"s") to `status`, since `self.kind` (the index kind) now
exists on the same object and the old name was ambiguous. Every caller
passes it positionally, so this is not a breaking rename.

**Tests**: extended `tests/test_index_store.py` (TDD: written, watched
fail on `TypeError`/`KeyError`, then made to pass) — `schemas(pa)` and
`schemas(pa, "files")` produce identical schemas, a registered kind's
schema matches its declared columns, a kind-aware `Sink` writes dict rows
by column name, and the default `Sink` stays byte-identical to before
kinds existed.

## Unit 8 — wiring `IndexKind.extract` into the walker (scan.py)

**Design**: `scan_dir_once` gained a `kind_obj=None` parameter — the
registered `IndexKind` for the scan's `cfg.kind`, resolved ONCE per run/
child by the new `_kind_obj(cfg)` helper (`None` for "files"), never
looked up per file. With `kind_obj=None` the file-row branch is completely
unchanged: the fixed tuple is still built inline, never routed through
`extract` — files stays byte-identical. With a `kind_obj`, each file's
`os.stat` result (already read for the signature) is hand to
`kind_obj.extract(path, st)`: a returned dict becomes the file's row, and
`None` means "not one of mine" — but the file still counts toward the
directory's signature and total size, since that bookkeeping belongs to
the host, not the plugin. This is "host owns the walk, plugin owns the
row" made concrete: a plugin is shown exactly the files the host was
already going to visit and gets no say over which ones those are.

Threaded through every `sink.add()`/`scan_dir_once` call site named in the
spec: the pool child's `_scan_subtree` and `_scan_dirs_threaded` (via a new
`_CHILD["kind_obj"]` entry set in `_child_init`), `run_scan`'s top-of-tree
walk loop and the huge-subtree split loop, and `_run_fsevents` (new trailing
`kind_obj=None` parameter). Both `Sink(...)` constructions in `scan.py` now
pass `kind=cfg.kind`.

**What this does NOT yet make possible**: a full end-to-end scan of a
non-"files" kind still fails at `compact()` — `_compact_locked` (see Unit
9's open item below) is unmodified and hardcodes the files/dirs column
list throughout its dedup, ordering and partition-bounds SQL. What IS true
now: a registered kind's rows are actually extracted and written to shard
parquet files during a live scan, which was previously impossible (a kind
was declarable and unit-testable in isolation, but never actually invoked
by the walker). Verifying a full non-files scan end to end needs Unit 9.

**Tests**: extended `tests/test_index_scan.py` (TDD) — `kind_obj=None`
byte-identical to before the parameter existed, a registered kind's
`extract` replaces the row for matching files, a kind that declines every
file yields zero rows while directory totals stay correct. Two pre-existing
tests that hand-seed `scan_mod._CHILD` via monkeypatch
(`test_threaded_scan_never_drops_entries_from_a_slow_worker`) needed a
`kind_obj` entry added and their `fake_scan_dir_once` stub's signature
extended to accept the new trailing parameter — not a design change, just
keeping a hand-built double in sync with the real signature.

**Verified NOT a regression**: `test_index_scan_on_demand.py` has 5
pre-existing failures (`_ask(...)["started"] is True` assertions) present
identically before ANY of this session's changes (checked via a
temporary, dropped `git stash`) — unrelated to this work, most likely an
environment/router-level issue, not touched by this session.

## Unit 9 — `_compact_locked` generalized to a schema-driven merge (store.py)

**Design**: the highest-risk item on the prior list, done as its own
dedicated TDD unit exactly as flagged. `_compact_locked` no longer hardcodes
`path`/`mtime`/`dir`. Three new pieces:

- `IndexKind` (`kinds.py`) gains `identity_column: Optional[str] = None` and
  `recency_column: Optional[str] = None`. `identity_column` must name a
  declared `"string"` column (compaction's `lower()`/lexical min-max pruning
  bounds need a string); `recency_column`, if given, must be numeric and
  requires `identity_column` to also be set (recency only resolves a tie
  *within* an identity partition — declaring one without the other is a
  contract error, not silently ignored). Both default to `None`: a test
  double that only exercises `schemas()`/`Sink` need not declare either.
- `store._dedup_keys(cfg)` returns `(identity_column, recency_expr)`.
  `cfg.kind == "files"` is the literal, unchanged `("path", "mtime")` pair —
  never routed through the kinds registry, mirroring `schemas()`'s existing
  "files is never looked up" posture. Any other kind asks its registered
  `IndexKind`; a kind with no `recency_column` falls back to ordering by its
  own identity column (arbitrary, but deterministic — duplicates are a
  directory-boundary edge case the dedup QUALIFY guards against, not the
  ordinary path for any kind). A kind with no `identity_column` at all raises
  a clear `ValueError` at `compact()` rather than a confusing DuckDB binder
  error three calls deep.
- `store._dir_expr(identity_col)` derives "this row's containing directory"
  as a SQL expression (`regexp_replace(col, '/[^/]*$', '')`) for any kind
  whose row has no denormalized `dir` column of its own (every non-"files"
  kind today: the host walker never attaches one at scan time — see
  scan.py's `kind_obj.extract` call sites). Only meaningful when
  `identity_column` names an absolute path, true of both `apps` and `notes`.
  "files" never calls this — it already has a real `dir` column — so
  `dir_expr == "dir"` for that kind exactly, and every SQL string built from
  it (`rows_outside`, `rows_kept`) is byte-identical text to what
  `_compact_locked` has always produced for "files". The DIRS bookkeeping
  table needed NO changes at all: `schemas()` already documents that table
  as kind-agnostic, so the pre-existing `outside`/`kept` (used only against
  it) are untouched; only the FILES-shaped table's filtering needed the new
  `rows_outside`/`rows_kept` pair.

`root_totals()` sums a kind's `size` column only when the kind's schema
declares one (`"size" in file_schema.names`) — `apps`/`notes` have none, and
report `root_size: 0` rather than a DuckDB binder error on a nonexistent
column. The old-rows backfill-missing-`depth`-column accommodation stays
"files"-only, spelled out explicitly as such: that backfill exists only
because pre-`depth` "files" partitions are still on disk today, and decision
#4 ("no migration, rebuilt from scratch") means no other kind carries that
history to accommodate.

`apps_kind.py`'s `KIND` now declares `identity_column="path"`,
`recency_column="updated_at"` (a genuine recency signal — the app entry
page's own mtime, mirroring `mtime`'s role for "files"). The `notes` example
declares `identity_column="path"` only — it has no natural recency column,
so its dedup falls back to path ordering.

**Tests**: extended `tests/test_index_kinds.py` (7 new tests: default-None,
identity-not-in-columns, non-string identity, recency-not-in-columns,
non-numeric recency, recency-without-identity, valid pair) and
`tests/test_index_store.py` (5 new tests: dedup by identity+recency picks
the higher-recency row, `root_size` is 0 for a sizeless kind, a registered
kind's rows outside the scan root survive a second root's compaction
(exercising `_dir_expr` directly), a kind with no `recency_column` still
dedupes (to exactly one survivor, not asserting which), and a kind with no
`identity_column` raises at `compact()`) — all TDD (watched fail on
`BinderException`/`TypeError`/no-raise, then made to pass).

**Verified**: `test_index_query test_index_store test_index_api
test_apps_api test_index_rank test_search test_index_kinds
test_index_config test_index_apps_kind test_index_manifest
test_index_examples_notes test_index_scan` → 484 passed (the 386-strong
6-file baseline plus the new tests above and every other unit's suite,
none regressed).

## Unit 10 — `guarded_query.py`'s views made schema-driven

**Design**: `_connect`'s `files` view was hardcoded to the "files" schema in
exactly one place — the typed empty stand-in used when an index has no
partitions yet (`_EMPTY_FILES`/`_EMPTY_DIRS`, literal SQL strings). The real
partition-backed case already worked for any kind: `parquet_src` reads
whatever columns are actually on disk, and `compact()` (Unit 9) already
writes a registered kind's own columns. So the only gap was the empty case.

Replaced the two literal strings with `_empty_stand_in(pa, arrow_schema)`,
which builds `SELECT CAST(NULL AS <sql type>) AS <col>, ... WHERE false`
from any pyarrow Schema — the same string/int64/int32/float64 → VARCHAR/
BIGINT/INTEGER/DOUBLE mapping `schemas()` already documents. `_connect` now
calls `schemas(pa, cfg.kind)` (previously always implicitly "files") and
builds both views' empty stand-ins from the returned schemas. `dirs` still
uses the shared, kind-agnostic dir schema — dirs bookkeeping was never
"files"-specific to begin with, so nothing there needed to change.

The DuckDB lockdown order (`allowed_directories` →
`enable_external_access=false` → `lock_configuration=true`) is untouched:
this only changes what the two `CREATE VIEW` statements select, which all
runs before the lockdown exactly as before.

**Tests**: extended `tests/test_index_guarded_query.py` with a registered
non-"files" kind (identity + recency columns, mirroring the apps kind) and
three new tests — a built index's `files` view answers with that kind's own
columns (passed immediately: proof `parquet_src`'s path already worked), an
EMPTY index of that kind still reports its own columns via `DESCRIBE` (the
one that was red — `AssertionError` on the "files" column names — until
`_empty_stand_in` landed), and `dirs`' schema is unaffected by the kind.

**Verified**: `test_index_guarded_query.py` alone (42 passed) and the full
prior regression set plus this file plus `test_index_rank_concurrency.py`
(which spies on `guarded_query._connect`'s connection) → 544 passed, 1
skipped, nothing regressed.

## Unit 11 — `search_apps_ranked` (query.py)

**Design**: confirmed the "Open questions carried forward" hypothesis by
implementation rather than inspection alone — `_rank_sql`/`_glob_sql` take
an ABSTRACT `inner` subquery (`rel, size, mtime, is_dir, depth, nm, lrel`),
never touching files/dirs column names directly, so a second function
shapes a different `inner` and reuses both unchanged. `search_ranked`/
`resolve_query` are untouched — byte-identical, exactly as the spec's
"file search MUST reuse resolve_query/search_ranked unchanged" requires.

`search_apps_ranked(cfg, q, limit, token, ranked, glob)` reads
`kinds.get(cfg.kind)`'s `identity_column`/`text_column`/`recency_column` at
call time rather than hardcoding "apps"'s names, so any registered kind
with a declared `identity_column` can reuse it unchanged. A kind with no
`identity_column` raises `ValueError` — checked BEFORE the manifest lookup,
since a kind with no identity column is a contract error the caller made,
not a data state, and must surface identically whether or not anything has
been scanned yet. Column mapping onto `inner`: `rel`/`lrel` from the
identity column (an absolute path, no root to be relative to — unlike
"files" there is no root/prefix/coverage concept, so `inner` is the kind's
whole corpus, unpruned), `nm` from the lowercased text column, `is_dir`
always false, `depth` always 0 (no hierarchy to penalize by — the same
"no natural signal" fallback `_dedup_keys` already takes), `mtime` from the
recency column when declared else NULL, `size` always NULL (no kind
declares one today).

**Glob-mode gotcha (test correction, not a function bug)**: a bare `*` in
`_glob_to_regex` becomes `[^/]*` and never crosses `/` — only `**` does.
Since a flat kind's `rel` is a full, multi-segment absolute path (not
root-relative the way a "files" `rel` can be shallow), a glob pattern
matching across the identity column's own path segments needs `**`, the
same as it would for a deeply nested "files" path. The test originally
used a bare `*one*`, which cannot match `/apps/one/index.html` (`one` sits
in a middle segment); corrected to `**one**` rather than special-casing
`search_apps_ranked`'s glob column mapping, since the existing semantics
are consistent with how "files" glob search already treats depth.

**Tests**: new `tests/test_index_apps_search.py`, 11 tests (TDD: written
first, confirmed red — `ImportError` before the function existed, then two
genuine logic failures on the first real implementation attempt described
below — then made to pass). Covers: substring-match tiering, exact-vs-
prefix scoring, recency-as-mtime, no-recency-column reports no mtime,
every hit `is_dir: False`, empty query, unbuilt index, unranked ordering by
identity column, glob-mode matching, a kind with no `identity_column`
raises, and the row-cap-plus-truncation-flag contract.

**Errors and fixes**: first implementation attempt checked the manifest
(and returned an empty result) BEFORE resolving `kind_obj`/validating
`identity_column`, so a kind with no `identity_column` and no manifest yet
short-circuited to the empty-result branch instead of raising — reordered
so the identity-column check runs first. The glob-mode test failure above
was a test correction, not a code fix — no line of `search_apps_ranked`,
`_rank_sql`, or `_glob_sql` changed for it.

**Verified**: `test_index_apps_search.py` alone (11 passed) and the full
prior regression set (`test_index_query test_index_store test_index_api
test_apps_api test_index_rank test_search test_index_kinds
test_index_config test_index_apps_kind test_index_manifest
test_index_examples_notes test_index_scan test_index_rank_concurrency
test_index_guarded_query test_index_apps_search`) → 555 passed, 1 skipped,
nothing regressed.

## Unit 12 — confirm/refuse HTTP route + frontend UI (decision #8)

**Design (backend)**: new router `fused_render/server/routers/index_manifest.py`,
mounted in `app.py` alongside (not replacing) `routers/index.py`. `propose`
takes `html` — the calling page's own entry file — and derives the folder
server-side exactly as `routers/background_apps.py`'s endpoints derive a
daemon's folder (`_folder_for`, realpath'd): never a raw folder path from
the caller, so no new path-typed API surface is introduced. `confirm`/
`refuse` accept `folder` directly, which stays safe despite being a raw
path because `manifest.confirm_index`/`refuse_index` only ever act on a
folder already present in the pending/confirmed lists — a caller cannot
use either to reach a folder that was never legitimately proposed first.
Every mutating route carries the `X-Fused` guard (`_require_fused`); the
read (`GET /api/index/proposals`) does not, matching every other GET in
this router family. `_entry(folder)` degrades to `{"kind": None}` rather
than erroring when a proposed folder's manifest has since gone missing or
turned invalid, mirroring `load_manifest`'s own never-raises posture.

**Design (frontend)**: a fourth, CONDITIONAL status-bar section
(`shell/IndexProposalsDock.tsx`), added to `StatusBar.tsx`'s
`models`/`activity`/`repoUpdates` slots as `indexProposals` and to
`exclusiveSection.ts`'s `SECTION_ORDER` as `"index-proposals"` (rightmost —
see that file's own updated doc comment for why it does not compete with
the three always-present sections). Unlike those three, it draws NOTHING
at all — not even an idle state — while there are no pending proposals:
decision #8 is a gate that should not occupy a permanent slot in the bar
when it has nothing to gate.

Row shaping lives in a pure `shell/index-proposals-lib.ts`
(`proposalFolderName`/`proposalRows`), the same pure-lib/stateful-Dock
split `repo-updates-lib.ts`/`RepoUpdatesDock.tsx` and `jobs.ts`/
`DownloadManager.tsx` already use. Unlike repo-updates rows, no
client-side dismiss store is needed: refusing is authoritative SERVER
state (`manifest.refuse_index` drops the folder from both lists), so the
next poll of `GET /api/index/proposals` simply stops reporting it.

Rows draw through the shared `NotificationCard` (six panels already share
this shape): `navAction` = "Confirm" (grants the root — the one act
decision #8 gates), `onDismiss` (✕, tooltip "Refuse") = "Refuse" (declines
or revokes). `IndexProposalsDock`'s own poll (`useIndexProposals`) mirrors
`RepoUpdatesDock.tsx`'s `useRepoUpdates` — a `generation` counter discards
stale responses, a failed poll leaves the last snapshot standing, a
`disposed` ref stops the chain on unmount — at the same `POLL_MS = 6000`
cadence as the other quick-status chips. Split into a pure
`IndexProposalsCardView` (props-in, testable without polling) and the
stateful default export, the same split `ModelsCardView`/`ModelsDock` use.

**TDD ordering, flagged explicitly (mirrors Unit 5's precedent)**:
`index-proposals-lib.test.ts` was written AFTER `index-proposals-lib.ts`'s
implementation — a deliberate deviation for one small, obviously-pure
row-shaping module. Everything else this unit touched
(`test_index_manifest_api.py`, `IndexProposalsDock.test.tsx`, the new
`StatusBar.test.tsx` case) was written first and confirmed red for the
right reason before being made to pass.

**Tests**: backend `tests/test_index_manifest_api.py`, 10 tests (confirmed
red first — 404s, the routes did not exist yet — then green): the
`X-Fused` guard on every mutating route, missing-body validation, propose
without a valid manifest reports `{"ok": false}` rather than erroring,
propose lands a valid manifest in pending, confirm on an unknown folder
reports not-ok, confirm moves pending → confirmed, refuse removes a
pending proposal, refuse revokes an already-confirmed one. Frontend
`index-proposals-lib.test.ts` (8 tests) and `IndexProposalsDock.test.tsx`
(5 tests: empty renders nothing, a pending row's chip/panel content, the
Confirm/Refuse buttons fire with the row's own folder, a busy row disables
both and relabels Confirm), plus one new `StatusBar.test.tsx` case for the
fourth slot.

**Verified**: `test_index_manifest_api.py` alone (10 passed) plus the
16-file/649-test backend regression set (nothing regressed; one confirmed
pre-existing, unrelated flake in `test_apps_api.py` investigated and ruled
out — passes standalone). Frontend: `bun test` on
`IndexProposalsDock.test.tsx`, `index-proposals-lib.test.ts`,
`StatusBar.test.tsx`, `exclusiveSection.test.tsx` (22 passed, 0 failed);
`node scripts/check-boundaries.mjs` (823 files, OK); `tsc --noEmit` clean.

## Remaining work (exact resume pointers)

Units 7-12 above have closed Part 1 in full, the apps-kind search gap, AND
Part 2's confirm/refuse surface: a registered kind's rows are extracted,
shard-written, compacted, queryable through the sandbox, rankable through
the same scoring "files" gets, AND a third-party proposal now has a real
HTTP route plus a user-facing Confirm/Refuse panel. This is the reconciled
list, in priority order.

1. **Router generalization** (Part 1/2 tail — not done):
   `routers/index.py`'s per-route bare `load_config()` calls need to
   accept an index identifier rather than assuming the single "files"
   store. The spec lists every call site with line numbers; change them
   in lockstep, and keep `fused.fileIndex.search`/`.query` in
   `static/runtime.js` working.
2. **Management page** (not started): `apps/ai_models` is the precedent
   for a prefix-routed built-in page (sidebar entry + lazy import in
   `App.tsx`) — build this feature's equivalent, and decide whether
   `frontend/src/shell/Indexing.tsx` folds into it or stays as a second,
   non-disagreeing source of truth.
3. **Part 3 in its entirety** (not started): delete the shortcuts overlay
   (surface + `frontend/src/platform/lib/shortcuts.ts:116`'s listing
   entry — delete, do not relocate to `?`), build the in-app ⌘K overlay,
   grouped by source with no cross-source score calibration, file search
   reusing `resolve_query`/`search_ranked` verbatim, app search reusing
   `search_apps_ranked`, per-keystroke cancellation via the existing
   `CancelToken`/HTTP 499 machinery.

**Already built and committed, for the avoidance of doubt**: the full
plugin contract (`kinds.py`, now with `identity_column`/`recency_column`),
kind-aware `IndexConfig`/`index_dir()`/`load_config()` (`config.py`), the
built-in "apps" kind (`apps_kind.py`), the third-party manifest +
propose/confirm registry (`manifest.py`), one fully worked reference
example indexer (`examples/notes_indexer/`), the house-style spec doc
(`specs/index-plugins.md`, linked from `overview.md`), kind-aware
`schemas()`/`Sink` (store.py), `IndexKind.extract` wired into every walker
call site (scan.py), a schema-driven `_compact_locked` (store.py),
schema-driven views in `guarded_query.py`, `search_apps_ranked`
(query.py), the `routers/index_manifest.py` HTTP surface over
propose/confirm/refuse, and `shell/IndexProposalsDock.tsx`'s status-bar
confirmation panel — a registered kind's rows are extracted, shard-written,
compacted into queryable partitions, readable through the sandboxed
connection, rankable through the same `_rank_sql`/`_glob_sql` scoring
"files" has always had, AND a third-party proposal now has a real
grant/decline surface end to end. What is NOT yet true: no router accepts
an index identifier beyond the single "files" store, there is no
management page, and Part 3 (the ⌘K overlay, the shortcuts-overlay
deletion) has not been started.

## Unit 13: router generalization (item 1 above) — DONE

`routers/index.py`'s twelve listed call sites (scan, scan-folder, cancel,
status, stats, search, rank, query, ask, config GET/POST, delete) all now
resolve a `kind` query/body parameter through a new `_kind_param(raw)`
helper — `raw` if it is a non-empty string, else `"files"` — and load that
kind's own `IndexConfig` via `load_config(kind=kind)` instead of a bare
`load_config()`. An unregistered kind name is a 400 naming what IS
registered. `"files"` is always accepted despite never being in
`kinds.registered()` — it predates the plugin registry entirely
(`config.index_dir`/`store.schemas` special-case it by name) — so
`_kind_param` checks for it by name rather than requiring a "files"
`IndexKind` to exist just to satisfy this check.

**`apps_kind.register_builtin()` is now actually called** (at import time
of `routers/index.py`, `replace=True`). It was defined and tested in
isolation since unit 10-ish but never invoked anywhere a running server or
an app.py-driven test would reach — the built-in "apps" kind was
unreachable from any request path. Import time (not `app.py`'s
`create_app`) so a test that imports the router module directly, without
going through `create_app()` (several existing test files do exactly
this), still sees "apps" registered.

**`/api/index/rank` branches on kind.** For `"files"` it is byte-for-byte
the previous behavior (`_rank_worker`: `resolve_query` then
`search_ranked`, `_rank_reason`'s mount/package/uncovered/scanning
classification, `root` required and non-empty). For any other kind it runs
a new `_rank_flat_kind_worker`, built on `search_apps_ranked` — the fully
generic ranker unit 11 built, which already reads a kind's
identity/text/recency columns off the registry. `root` is NOT required
for a non-"files" kind (the route only 400s on a missing root when
`kind == "files"`): a flat kind has no navigable folder for a client to
name, only its own fixed default root (`_default_root`, below). The three
fields `_rank_body` computes for the files tree — `base`/`mode`/`pattern`
— are answered with fixed stand-ins (`""`, `"substring"`, `q` itself) so
the response shape (and the `_WIRE_DROP` trimming `api_index_rank` already
does) stays identical across kinds; `reason` is always `""` — a flat
kind's default root is the one workspace it always scans, never "mount" /
"uncovered" / "scanning".

**`scan_roots`'s home-directory fallback is now per-kind**, via a new
`_default_root(kind)`: `"~"` for `"files"` (unchanged), `fused_dir()` (the
`~/Fused` app workspace, `shell/seed.py`) for anything else — the natural
default scan root for the "apps" kind, mirroring what `GET /api/apps`
itself walks.

**Deliberately NOT generalized, and documented in the code as such**:
- `/api/index/stats` and `/api/index/search` thread `kind` through to load
  the right store, but `query.stats`/`query.search_under` themselves stay
  hardcoded to the `files`/`dirs` schema — a flat kind's rows have no
  directory tree for a breakdown or a "descendants of this folder" query
  to describe. A non-"files" kind loads correctly; calling stats/search
  against one will raise inside those functions (an accepted gap, not a
  silent one).
- `/api/index/ask` threads `kind` through to the guarded SQL execution,
  but `_ASK_SYSTEM_PROMPT` stays hardcoded describing the files/dirs
  schema — asking a natural-language question against a non-"files" kind
  compiles SQL the model was never told the real column names for. Also
  an accepted, documented gap.
- `run_startup_scan`/`run_startup_warm`/`_startup_runs` were NOT widened to
  auto-scan "apps" (or any other kind) at boot — `tests/test_index_api.py`
  pins `_startup_runs` with exact dict-equality against the "files"-only
  shape, and the spec's own scope is "generalize the routes", not "change
  what scans automatically at startup". An "apps" scan is only ever
  triggered on demand, via `POST /api/index/scan {"kind": "apps"}`
  (the eventual management page's job to call).

**Test-authoring note (bit us once, worth writing down)**: several
existing tests call route functions directly as plain Python calls,
bypassing FastAPI's request handling entirely — a `Query(default="")`-
declared parameter then arrives as the `Query(...)` FieldInfo object
itself, not the string `""` it describes. `_kind_param` treats anything
that is not a real non-empty string (including that FieldInfo object) as
"absent", specifically to keep those tests working unchanged. One
existing test (`test_index_jobs.py`'s
`test_api_index_scan_wakes_the_bridge_on_a_started_run`) stubbed
`load_config` with a zero-argument lambda; updated to accept `kind="files"`
since every route now calls `load_config(kind=...)`.

**New test file**: `tests/test_index_kind_routes.py` — an unregistered
kind is a 400 (parametrized across config/scan/status/rank/delete); the
default (no `kind`) config answers for the original unmigrated
`home/index` directory; `kind=apps` config answers for `home/index/apps`;
deleting one kind never touches a sibling kind's store; `/api/index/rank`
for `kind=apps` ranks through `search_apps_ranked` (built against the
real, fully-shaped built-in "apps" `IndexKind`, not a throwaway test
double) and does not require `root`; `scan_roots` defaults "apps" to a
monkeypatched workspace root and still defaults "files" to home.

**Verified**: `tests/test_index_kind_routes.py` (12 passed) plus the
directly-affected existing suites — `test_index_api.py`,
`test_index_apps_search.py`, `test_index_apps_kind.py`,
`test_index_config.py`, `test_index_jobs.py`, `test_git_repos_api.py`,
`test_index_fda_gate.py`, `test_app_lifespan.py` (240 passed total, one
existing test updated for the new `load_config(kind=...)` call shape).
`tests/test_index_runtime.py` (the `fused.fileIndex` JS-bridge node
harness) still 18/18 after the `runtime.js` `opts.kind` passthrough.
`tests/test_index_scan_on_demand.py` still shows exactly the same 5
pre-existing, unrelated failures verified at the top of this branch (7
passed, 5 failed — unchanged). `ast.parse`/`node --check` both clean.

**Resume pointer**: item 1 above is done. Next: item 2 (the management
page) and item 3 (Part 3 — the ⌘K overlay and the shortcuts-overlay
deletion), in that order, per the top-level task ordering.
