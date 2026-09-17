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

## Remaining work (exact resume pointers)

This section was corrected by the following builder session (see prior
paragraph in git history for what it replaced): the version before that
said Part 2 was "not started" and listed `config.py`'s `kind` field as
pending, both stale by the time that session ended. Units 7-8 above (this
session) closed the "declarable but never indexed" gap for the file-write
half of the pipeline. This is the reconciled list, in priority order.

1. **`store.py` compaction generality** (Part 1, HIGH RISK — not done,
   the single biggest remaining risk in the whole spec): `_compact_locked`
   (~store.py:452-600+) hardcodes `path, dir, name, ext, size, mtime,
   depth` throughout — the `QUALIFY row_number() ... ORDER BY mtime DESC`
   dedup (assumes a recency column named `mtime`), the per-partition
   `min(path)/max(path)/min(lower(path))/max(lower(path))` pruning bounds
   (assumes `path` is both the identity column AND lexically sortable for
   pruning), and the backfill-missing-`depth`-column fallback. A non-files
   kind may have no natural "mtime" (recency) column at all, and its
   identity column may not be named "path". This needs its own dedicated
   TDD pass against `test_index_store.py`'s compaction tests, thought
   through as its own design question (what generalizes across a "dedup
   key" and an "order key" that isn't always literally `path`/`mtime`)
   rather than attempted inside a unit doing anything else. Until this
   lands, a non-"files" kind can be scanned (Unit 8) but not compacted —
   `run_scan` for such a kind will fail at the `compact()` call.
2. **`guarded_query.py` per-kind views** (Part 1 — not done): `_connect`'s
   `CREATE VIEW files/dirs AS...` and `_EMPTY_FILES`/`_EMPTY_DIRS` need a
   kind-driven equivalent. The DuckDB lockdown order
   (`allowed_directories` → `enable_external_access=false` →
   `lock_configuration=true`) MUST NOT change relative order.
3. **Apps-kind search** (Part 2 tail — not done): the working hypothesis
   under "Open questions carried forward" below — `resolve_query`/
   `search_ranked` stay byte-identical for "files"; a new function shapes
   an apps-kind `inner` subquery and calls the same `_rank_sql`/
   `_glob_sql` primitives — needs verifying before relying on it.
4. **Confirm/refuse HTTP route + frontend UI** (Part 2 tail — not done):
   `manifest.propose_index`/`confirm_index`/`refuse_index` have no
   caller yet. Decision #8 ("never silent") needs a route plus a
   confirmation surface in the frontend.
5. **Router generalization** (Part 1/2 tail — not done):
   `routers/index.py`'s per-route bare `load_config()` calls need to
   accept an index identifier rather than assuming the single "files"
   store. The spec lists every call site with line numbers; change them
   in lockstep, and keep `fused.fileIndex.search`/`.query` in
   `static/runtime.js` working.
6. **Management page** (not started): `apps/ai_models` is the precedent
   for a prefix-routed built-in page (sidebar entry + lazy import in
   `App.tsx`) — build this feature's equivalent, and decide whether
   `frontend/src/shell/Indexing.tsx` folds into it or stays as a second,
   non-disagreeing source of truth.
7. **Part 3 in its entirety** (not started): delete the shortcuts overlay
   (surface + `frontend/src/platform/lib/shortcuts.ts:116`'s listing
   entry — delete, do not relocate to `?`), build the in-app ⌘K overlay,
   grouped by source with no cross-source score calibration, file search
   reusing `resolve_query`/`search_ranked` verbatim, app search reusing
   whatever item 3 above produces, per-keystroke cancellation via the
   existing `CancelToken`/HTTP 499 machinery.

**Already built and committed, for the avoidance of doubt**: the full
plugin contract (`kinds.py`), kind-aware `IndexConfig`/`index_dir()`/
`load_config()` (`config.py`), the built-in "apps" kind (`apps_kind.py`),
the third-party manifest + propose/confirm registry (`manifest.py`), one
fully worked reference example indexer (`examples/notes_indexer/`), the
house-style spec doc (`specs/index-plugins.md`, linked from
`overview.md`), kind-aware `schemas()`/`Sink` (store.py), and
`IndexKind.extract` wired into every walker call site (scan.py) — a
registered kind's rows are extracted and shard-written by a live scan,
though not yet compactable (item 1 above).
