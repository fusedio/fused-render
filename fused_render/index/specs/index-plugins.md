# Index plugins

> **Status — partial.** This file owns the **plugin contract**: how a second
> (or third-party) index kind is declared, what it is allowed to do, and how a
> user's approval gates it. Implementing modules: `kinds.py` (`Column`,
> `IndexKind`, the registry), `apps_kind.py` (the built-in "apps" kind),
> `manifest.py` (`IndexManifest`, the propose/confirm store),
> `examples/notes_indexer/` (a fully worked third-party reference). A
> registered kind's rows are now extracted by the live walker
> (`scan.py`'s `sink.add()` call sites), compacted into partitions
> (`store._compact_locked`, §6), queryable through the sandboxed
> connection (`guarded_query.py`, §7), rankable through the same
> scoring "files" gets (`query.search_apps_ranked`, §8), and decision #8's
> gate has a real end-to-end surface: a route (`routers/index_manifest.py`,
> §9) plus a status-bar confirmation panel
> (`shell/IndexProposalsDock.tsx`). Not yet built: a router that accepts an
> index identifier beyond the single "files" store, a management page, and
> the ⌘K overlay (Part 3) — see `DECISIONS-index-plugins.md`'s "Remaining
> work" for the exact resume pointers.

## 1. The load-bearing rule: host owns the walk, plugin owns the row

The engine already has one walker, with its own incrementality
(`scan-incremental.md`), FSEvents journal, nice-throttled scheduling, and
cancel-flag plumbing (`scan.md`). A plugin never reimplements any of that,
because it never gets the chance to: `IndexKind` has no `scan()` method at
all. Its entire surface is

```python
extract(path: str, st: os.stat_result) -> dict | None
```

called by the host, once per file the host has ALREADY decided to visit.
`extract` returns a row (a dict keyed by the kind's declared column names) or
`None` to say "not one of mine". It receives the `os.stat_result` the walker
already paid for, so it never needs its own `stat` call for the common case
of a size/mtime-driven shortcut.

This is not a performance shortcut, it is the whole trust boundary
(SPEC-index-plugins.md decision #2, "extract only, user approves root — a
plugin never enumerates paths"): a plugin cannot ask "what exists under this
directory", only "is THIS file — which the host is already showing me —
mine". A plugin that wanted to enumerate paths on its own would have to
reimplement the walker from scratch outside the index engine entirely, which
is a different, unrelated act this contract has no opinion about.

The other consequence: plugin code runs at INDEX time only. A query never
imports or calls a plugin — it is SQL over the parquet rows `extract` already
produced, through the same guarded, sandboxed connection every query goes
through (`guarded_query.py`). A keystroke in the search box never executes a
line of third-party Python.

## 2. `Column` and `IndexKind` (`kinds.py`)

```python
Column(name: str, type: str)   # type in {"string", "int64", "int32", "float64"}

IndexKind(
    name: str,                                  # e.g. "apps", "notes"
    columns: tuple[Column, ...],                 # the row shape, declared up front
    extract: Callable[[str, os.stat_result], dict | None],
    text_column: str,                            # which column ranking treats as fuzzy-matchable text
)
```

The four types are exactly the physical types `store.schemas()` already uses
for `files`/`dirs` (`index-store.md §2`) — closed, not open, so a kind cannot
declare a column the compaction SQL or `guarded_query`'s typed empty-table
stand-ins don't already know how to carry.

`text_column` exists because ranking (`query.md`) needs exactly one
fuzzy-matchable name per row — the same role `nm` plays for the `files` kind
today. A kind with two "name-like" columns picks one; the other is still
indexed and returnable, just not what a fuzzy query matches against.

`IndexKind.pa_schema(pa)` turns the declared columns into a real pyarrow
Schema, in declared order — what `store.Sink` writes shards against for a
kind other than "files" (§6 has the schema-driven compaction that reads it).

`register(kind, *, replace=False)` / `get(name)` / `registered()` form a
tiny, process-local registry. Registering twice under the same name without
`replace=True` raises — a silent overwrite is far more likely a bug than an
intended upgrade, since registration is meant to be a rare, explicit act
(once at import for a built-in kind, once at manifest-confirm time for a
third-party one).

## 3. The built-in "apps" kind (`apps_kind.py`)

The first non-`files` kind, and the proof the contract is usable for
something the `files`/`dirs` schema genuinely cannot answer: whether a
folder IS an app depends on reading a candidate entry page's CONTENT for
`<meta name="fused-app">` (`app_listing.app_entry`), not on anything a
directory listing's name/size/mtime already carries.

`extract(path, st)` returns a row (built from `app_listing.app_dict`, plus
the stable `app_id.app_id`) exactly when `path` is the CANONICAL entry of its
folder — the same folder's OTHER `.html` files (a multi-page app's non-entry
pages) return `None`, so one app contributes exactly one row regardless of
how many pages sit in its folder. Mirrors `git_repos.py`'s "index a fact,
not a live probe" posture and `exported_apps.py`'s degrade-safe one: an
unreadable or racing folder is "not an app", never an exception that takes
the scan down.

`tag` (the Apps hub's own "Folders" facet — `Apps.tsx`'s client-side walk)
is deliberately NOT part of this kind's row shape: `tag` is the first path
segment under a workspace root the Apps hub's OWN walk already knows, and a
global name index has no single such root to derive it against. This is why
the Apps hub keeps its existing client-side walk-and-filter rather than
reading from this index — the resulting duplication is accepted, not a bug
to fix (SPEC-index-plugins.md, the Apps hub decision).

## 4. Third-party manifests (`manifest.py`)

A folder that wants to register its own kind declares a table in its own
`pyproject.toml`, distinct from `[tool.fused-render.app]`
(`background_apps.md`'s daemon/main table) because indexing is a different
kind of opt-in — it runs on every scan, not on demand:

```toml
[tool.fused-render.index]
module = "indexer.py"   # resolved inside the folder, containment-checked
kind = "widgets"        # the IndexKind name this module's register call adds
```

`load_manifest(folder)` parses this defensively — same posture as
`background_apps.load_manifest`: a missing/corrupt `pyproject.toml`, a
`module` that resolves outside the folder (`../`, a symlink), or one that
isn't a file, all read as "no manifest", never an exception. It never
imports `module` — parsing a manifest is side-effect-free introspection,
while importing a third party's code and calling whatever registers its
`IndexKind` is the act decision #8 gates on explicit confirmation.

The propose/confirm store (`propose_index`, `confirm_index`, `refuse_index`,
`pending_folders`, `confirmed_folders`) is the testable backend for
"app proposes, user confirms, never silent": an app calling `propose_index`
on its own folder only lands it in `pending_folders()` — nothing is
imported, nothing runs — until a user-driven `confirm_index` call moves it
to `confirmed_folders()`, the one list a caller is meant to treat as
"safe to import and register". Confirmation is sticky: re-proposing an
already-confirmed folder is a silent no-op, never a demotion back to
pending (which would reprompt for something already settled). The route
and UI that DRIVE `confirm_index`/`refuse_index` from a user click are
§9's `routers/index_manifest.py` and `shell/IndexProposalsDock.tsx`.

## 5. The reference example (`examples/notes_indexer/`)

One small, fully worked third-party folder: a `pyproject.toml` carrying the
exact table §4 describes, and an `indexer.py` with a two-column row shape
(`title`, `word_count`) simple enough to read start to finish in a minute.
`tests/test_index_examples_notes.py` loads it the way a real caller would —
parse its manifest, then import exactly the file the manifest names via
`importlib.util.spec_from_file_location`, never a package-style `import`
(a third party's folder is never on `sys.path`) — proving the manifest
format and the plugin contract work together for someone who has only read
this document. It is a reference, not a feature: nothing surfaces it to end
users as something to discover or enable (decision #9).

## Non-goals

- **Sandboxing a plugin's `extract` call** (a timeout per file, a memory
  cap, a subprocess boundary) — not attempted. The trust boundary this
  contract draws is about WHAT a plugin can see and do (one file, no
  enumeration, index time only), not about running arbitrary Python safely
  in a hostile sense. A confirmed folder's code is trusted to the same
  degree a confirmed background app's code already is.
- **Mounted directories** — out of scope per SPEC-index-plugins.md; nothing
  here changes `MountGuard`'s behavior for any kind.

## 6. Compaction (`store._compact_locked`) is schema-driven

`_compact_locked` no longer hardcodes `path`/`mtime`/`dir`: it asks
`IndexKind.identity_column`/`recency_column` (declared in `kinds.py`, see §2)
for what to dedupe and order by, and derives the containing directory a
registered kind's row belongs to from its identity column rather than
assuming a denormalized `dir` column exists (`store._dir_expr`). "files"
routes through none of this generically — every SQL string it produces for
`cfg.kind == "files"` is the same literal text the store has always run, so
the change is additive rather than a rewrite of the one path 377 existing
tests depend on.

`identity_column` must be a `"string"` column (compaction needs `lower()`
and lexical min/max for the same partition-pruning bounds `query.py` uses for
`files`). `recency_column`, if declared, must be numeric and breaks a dedup
tie in favor of the larger value (`ORDER BY ... DESC`, mirroring `mtime`); a
kind with no natural recency column (the `notes` example) falls back to
ordering by its own identity column — arbitrary, but deterministic, and only
ever exercised by a directory-boundary edge case, not the common path.
`root_size` sums a kind's `size` column only when the kind has one; a kind
without one (again, `notes`) reports `0` rather than a query error. A kind
that declares neither `identity_column` fails `compact()` with a clear
`ValueError` rather than a confusing DuckDB binder error deep in the SQL.

The built-in `apps` kind declares `identity_column="path"`,
`recency_column="updated_at"`. The `notes` example declares
`identity_column="path"` only (no recency column).

## 7. `guarded_query.py`'s views are schema-driven

`_connect`'s `files` view is built against `cfg.kind`'s own row shape
(`store.schemas(pa, cfg.kind)`), not a literal "files" schema — so a second
registered kind's rows are queryable through the same sandboxed connection
"files" has always used. `dirs` still uses the shared, kind-agnostic dirs
schema (schemas() documents it as bookkeeping every index carries alike).

The typed empty-table stand-in used when an index has no partitions yet
(`_empty_stand_in`) is generated from the schema itself — one SQL cast per
column, string→VARCHAR/int64→BIGINT/int32→INTEGER/float64→DOUBLE, the same
mapping `schemas()` documents — rather than written by hand per kind, so a
kind's row shape and its empty stand-in cannot drift apart.

The DuckDB lockdown order (`allowed_directories` →
`enable_external_access=false` → `lock_configuration=true`) is unchanged:
this only touches what the two `CREATE VIEW` statements select, run before
the lockdown as they always were.

## 8. Apps-kind search (`query.search_apps_ranked`)

A registered kind's rows are a flat corpus, not a directory tree, so
`resolve_query`/`search_ranked` stay byte-identical for "files" and a
separate function, `search_apps_ranked(cfg, q, limit, token, ranked,
glob)`, ranks any other registered kind instead of generalizing those two.
It confirms rather than merely assumes the hypothesis this file used to
carry as an open question: `_rank_sql`/`_glob_sql` were already written
against an ABSTRACT `inner` subquery shape (`rel, size, mtime, is_dir,
depth, nm, lrel`), not hardwired to files/dirs, so a second, differently-
shaped `inner` reuses both unchanged.

`search_apps_ranked` reads `IndexKind.identity_column`/`text_column`/
`recency_column` off `cfg.kind`'s own registration (never hardcoded to
"apps"), so any registered kind with an `identity_column` can be searched
this way. `rel`/`lrel` come from the identity column (an absolute path;
unlike "files" there is no root, so `inner` is the kind's whole corpus,
unpruned — no prefix/coverage concept applies). `nm` is the lowercased
text column. `is_dir` is always false and `depth` always 0 — a flat kind
has no hierarchy for either to carry meaning. `mtime` comes from the
recency column when the kind declares one, else NULL; `size` is always
NULL (no kind declares one today). A kind with no `identity_column`
raises `ValueError` — a contract error the caller made, not a data state,
so it surfaces the same way whether or not the index has been scanned yet.

Glob mode inherits `_glob_to_regex`'s existing single-segment semantics
unchanged: a bare `*` does not cross `/`, so a pattern matching across the
identity column's own path segments needs `**`, exactly as it would for a
deeply nested "files" path.

## 9. Confirm/refuse HTTP route + frontend UI (decision #8)

`fused_render/server/routers/index_manifest.py`, mounted in `app.py`
alongside `routers/index.py` (a distinct router, not a replacement — that
one only ever knows about the built-in "files" index this process already
scans). Three routes: `POST /api/index/proposals/propose` (a running app
proposes its OWN folder — takes `html`, the caller's own entry file, and
derives the folder server-side, realpath'd, the same pattern
`routers/background_apps.py` uses for a daemon's folder — never a raw
folder path from the caller); `POST /api/index/proposals/confirm` and
`POST /api/index/proposals/refuse` (both take `folder` directly, safe
despite being a raw path because `manifest.confirm_index`/`refuse_index`
only ever act on a folder already present in the pending/confirmed lists);
`GET /api/index/proposals` (read-only, no guard, lists both). Every
mutating route carries the `X-Fused` header guard every other mutation in
this app carries.

The frontend surface is a fourth, CONDITIONAL status-bar section,
`shell/IndexProposalsDock.tsx` — unlike Models/Activity/Notifications it
draws nothing at all, not even an idle state, while there is nothing
pending: decision #8 is a gate, not a permanent readout. Rows draw through
the shared `NotificationCard` (the same row shape Models/Engines/Jobs/
repo-updates/waiting-tasks/LAN-pairings all use): `navAction` = "Confirm"
(the one act that grants the plugin's declared root), `onDismiss` (✕,
"Refuse") = decline or revoke. Row shaping is a pure
`shell/index-proposals-lib.ts`, the same pure-lib/stateful-Dock split
`repo-updates-lib.ts`/`RepoUpdatesDock.tsx` use — with one simplification
that split doesn't need: no client-side dismiss store, since refusing is
authoritative server state and the next poll of `GET /api/index/proposals`
simply stops reporting a refused folder.

Nothing in either half imports or runs a third party's module — that
stays the caller's own job, gated on `manifest.confirmed_folders()`,
exactly as `manifest.py`'s module docstring (§4) requires.

## See also

- `index-store.md` — the on-disk shape `Sink`/`compact` serve per kind.
- `query.md` — `search_apps_ranked` (§8), which reuses `_rank_sql`/
  `_glob_sql` rather than duplicating the ranking grammar.
- `background_apps.md` — `_folder_for`'s html-to-folder derivation
  pattern, reused unchanged by `routers/index_manifest.py` (§9).
- `scan.md` — the walker a kind's `extract` plugs into.
- `server-api.md` — where a confirm/refuse HTTP surface would live.
