# Index plugins

> **Status — partial.** This file owns the **plugin contract**: how a second
> (or third-party) index kind is declared, what it is allowed to do, and how a
> user's approval gates it. Implementing modules: `kinds.py` (`Column`,
> `IndexKind`, the registry), `apps_kind.py` (the built-in "apps" kind),
> `manifest.py` (`IndexManifest`, the propose/confirm store),
> `examples/notes_indexer/` (a fully worked third-party reference). Wiring a
> registered kind into the LIVE walker (`scan.py`'s `sink.add()` call sites)
> and into the query/router layers for a kind other than "files" is not yet
> built — see `Open questions` below and `DECISIONS-index-plugins.md`.

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
Schema, in declared order — what `store.Sink` would write shards against for
a kind other than "files" once that wiring exists (see Open questions).

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
pending (which would reprompt for something already settled). The actual
HTTP route and frontend confirmation UI that DRIVE `confirm_index`/
`refuse_index` from a user click are not built yet — see Open questions.

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

## Open questions

- `scan.py`'s walker does not yet call any registered kind's `extract` at
  its `sink.add()` sites — a registered kind (built-in or third-party) is
  fully declarable and independently testable, but nothing yet makes it
  actually get indexed by a live scan.
- `store.py`'s `schemas(pa)`/`Sink.__init__` and the ~170-line
  `_compact_locked` merge SQL (`index-store.md §4`) still hardcode the
  `files`/`dirs` shape; generalizing them to consult `IndexKind.pa_schema`
  for a non-"files" `IndexConfig.kind` is the highest-risk remaining piece
  and is not done.
- `guarded_query.py`'s `_connect` (`CREATE VIEW files/dirs AS...`) and its
  `_EMPTY_FILES`/`_EMPTY_DIRS` stand-ins are not yet kind-driven, so a
  second index cannot be queried through the sandbox yet. The DuckDB
  lockdown order (`allowed_directories` → `enable_external_access=false` →
  `lock_configuration=true`) must not change relative order when this is
  built.
- The HTTP route(s) and frontend surface that actually call
  `manifest.propose_index`/`confirm_index`/`refuse_index` from a running
  app and a user click do not exist yet.

## See also

- `index-store.md` — the on-disk shape a generalized `Sink`/`compact` would
  need to serve per kind.
- `query.md` — where a second kind's search would need its own
  `resolve_query`-equivalent, reusing `_rank_sql`/`_glob_sql` rather than
  duplicating the ranking grammar (see `DECISIONS-index-plugins.md`'s open
  question on this).
- `scan.md` — the walker a kind's `extract` plugs into, once wired.
- `server-api.md` — where a confirm/refuse HTTP surface would live.
