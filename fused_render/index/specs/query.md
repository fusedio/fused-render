# Query

> **Status — shipped.** This file owns the **read path**: the query actions and
> partition pruning. Implementing module: `query.py` (`stats`, `prune`). The files
> being read are `index-store.md`; the routes that expose these are `server-api.md`.

## 1. Contract

`query.py` is **read-only** — it never writes to the index. Every action first loads
`partitions.json`; when it is absent the call returns `{empty: true, location, …}` with
empty results rather than raising, because "no index yet" is a state the UI renders,
not an error.

| Function | Params | Returns |
|---|---|---|
| `stats` | `root`, `breakdown`, `token` | totals + manifest; per-extension breakdown when `breakdown` is truthy |
| `search_under` | `root`, `q`, `limit`, `include_dirs`, `token` | the explorer's in-folder corpus (§6) |

duckdb is imported inside each function, not at module top, so a call on a missing
index stays cheap — and so importing the server's router does not pull duckdb in.

`token` (`index/cancel.CancelToken`), when given, is bound to the connection the
moment it exists, checked before each real query, and turns a
`duckdb.InterruptException` this token's own `cancel()` caused into `Cancelled` —
anything else keeps surfacing as itself. `server-api.md`'s `cancellable(request)`
is what supplies one per HTTP request; every route the token reaches
(`stats`, `search`, `query`, `ask`, `rank`) answers an abandoned request with a
quiet 499 rather than finishing work nobody is waiting on.

## 2. `stats`

Totals are scoped to **one subtree** — the explicit `root` param, else
`partitions.json`'s `last_root` — not the whole index, which may hold several roots
(`index-store.md §4`). Scoping is `dir = root OR dir LIKE root || '/%'`.

The **default** response is `rows`, `dirs`, `total_size`, `updated`, `last_root`,
`location`, `partitions` — a `count`/`sum` over the scoped rows, no grouping. `types`
(per-extension `{ext, n, size}` ordered by size, **top 50** with the remainder folded
into a single `other` bucket; empty extensions reported as `no ext`) is computed only
when the caller passes `breakdown=True` — the `GROUP BY ext` pass costs the same table
scan again for a shape most callers never read.

## 3. `resolve_query` and the search grammar

`resolve_query(root, raw, guard=None, blocked_out=None, token=None)` (used by the
ranked route, `server-api.md §7`) is the one place a search box's typed string
becomes `{base, pattern, mode}`. It first runs `raw` through
`expand_whitespace_query` — a **shared** transform (`search_under`, §6, runs the
same one on `q`) that turns whitespace into wildcards without regressing the
motivating "type two words, find the file with both in order" case a naive
`" " -> "*"` substitution would. Follow-up to the original rule (fixes two
disagreements: a trailing space used to be silently trimmed away, and
`icon copy` / `icon*copy` used to disagree on whether they find the same file):

1. **No trimming.** Leading/trailing whitespace is as meaningful as any other
   run — `"*.js "` is not the same query as `"*.js"`.
2. The **one** no-op: a string with **neither** whitespace **nor** `*` anywhere
   (`report`) is returned byte-for-byte unchanged — still substring mode, still
   ranked. A whitespace-free glob like `*.pdf` is **not** a no-op any more (see
   point 4).
3. Collapse every run of whitespace to a single `*` (`hello  world` with two
   spaces resolves identically to `hello world` — `**` means something
   different in this grammar, a cross-directory wildcard, so a whitespace run
   must never produce one; a run bordering a literal `*` the user already typed
   is dropped rather than replaced, for the same reason).
4. On the **final** `/`-separated segment only, wrap each end **independently**:
   prepend `*` unless it already starts with one, append `*` unless it already
   ends with one. This fires even when the segment has no whitespace at all —
   `*.pdf` becomes `*.pdf*`, and `icon*copy` becomes `*icon*copy*`, agreeing
   with `icon copy` -> `*icon*copy*`. Earlier segments get the whitespace
   collapse but no wrap of their own — `~/My Documents/report` becomes
   `~/My*Documents/*report*`, not `~/*My*Documents*/...`.

**Known, accepted consequence** (do not special-case around it): a previously
precise glob like `*.pdf` now also matches `report.pdf.bak` and `notes.pdfx` —
every glob query trades some precision for grammar consistency with the
whitespace rule. Likewise a single space (`"icon "`) now flips a query straight
into glob mode, losing relevance ranking; `_glob_sql` deliberately gains no
scoring to compensate (SPEC-search-space-wildcard.md).

`mode` is `"glob"` the moment the expanded string contains a `*` anywhere — which
now includes every whitespace-containing query AND every glob query, whitespace
or not — else `"substring"`. `?` and `[`/`]` are left as literal characters always
(people put them in filenames far more often than they mean them as patterns), so
their presence never flips the mode. There is deliberately **no escape hatch** for
a literal space in a filename — quoting is out of scope (SPEC-search-space-
wildcard.md).

Base resolution (peeling a leading `~`, an absolute path, a Windows drive letter,
or a `..`-containing relative query off into `base`, with whatever is left as
`pattern`) runs on the ALREADY-expanded string, so a leading folder path is
subject to the same whitespace-as-wildcard rule as the trailing name — "apply
everywhere" is a deliberate scope choice, not an oversight. The walk that does
this (`_walk_from`) stops at the first `*`-segment, so a wildcard anywhere in an
early segment ends the walk there and folds the rest into `pattern` rather than
trying to resolve a glob against the filesystem.

`search_ranked` (`server-api.md §7`) never scores a `mode == "glob"` result —
every hit is an equal full-pattern match with no ranking signal to compute, so
results are returned in the same `depth ASC, rel ASC` order `search_under`
already used for its own unranked branch. This is an accepted consequence, not a
gap: SPEC-search-space-wildcard.md explicitly keeps scoring out of glob mode.

## 4. Partition pruning

Partitions are globally sorted by path and the manifest records each one's `min`/`max`
(`index-store.md §4`), so a caller that can name a literal path prefix — a folder root
(§6), an anchored path — needs only partitions whose range can contain it: `prune`
keeps those where `max >= prefix AND min <= prefix + "￿"`. The range test is exact,
not a heuristic, because the ordering is total. A caller with no prefix to offer (an
unanchored substring, or the empty prefix) scans every partition. Each response
reports `scanned_partitions` / `of_partitions` plus the partition filenames.

## 5. Guarded user SQL

> Implementing module: `guarded_query.py` (`run_guarded`, `MAX_LIMIT`, `TIMEOUT_S`);
> routes in `server-api.md §1`. Deliberately NOT in `query.py`, and nothing here is
> named `sql`: `tests/test_index_query.py` still asserts `query.py` has no `sql`
> attribute, so the unrestricted action cannot come back by accident.

OpenIndex exposed a third action that ran the caller's duckdb statement against views
over the index, unrestricted: no allowlist, no read-only flag, able to attach files,
write, or read anything the user's account can. That was consistent with a trusted local
page where `runPython` already executes arbitrary local Python — and behind an HTTP route
it was not the same surface, so it was dropped rather than guarded.

It is now back, **guarded**. The bar is set by what the app already is: `/api/run`
executes arbitrary local Python for the same caller, so confined read-only SQL adds no
capability. What the guard buys is that a mistyped — or model-written — statement cannot
write to the index or read a file outside it.

`run_guarded(cfg, sql, limit, token)` returns `{columns, rows, truncated}` over two views,
`files` and `dirs`, whose columns are exactly the stored schemas (`index-store.md §2`).
Two independent guards, and **both** are necessary:

**The statement-type gate**, before anything executes. `duckdb.extract_statements` must
yield exactly one statement, of type `SELECT`, `CALL` or `EXPLAIN`. This is
load-bearing, not defence in depth: behind a fully locked configuration an in-memory
`INSERT` / `CREATE TABLE` / `DELETE` **still succeeds**, so the only place a write can be
refused is before it runs. It also cannot be "SELECT only" — duckdb parses
`PRAGMA database_list` and `DESCRIBE …` as `StatementType.SELECT`, so PRAGMA, DESCRIBE
and (via `CALL`) the table-function pragmas are reachable regardless; they are read-only
and are therefore admitted *explicitly*, rather than smuggled in by a gate that claims
to admit only SELECT. Everything else — INSERT, UPDATE, DELETE, CREATE, DROP, ALTER,
`COPY … TO`, ATTACH, INSTALL/LOAD, SET/RESET — is refused, as is a batch.

**The DuckDB lockdown**, once per connection, in this order:

1. `SET allowed_directories=[<index dir>]`
2. `SET enable_external_access=false`
3. `SET lock_configuration=true`

All three, in that order. `allowed_directories` **alone confines nothing** — it is a
carve-out from `enable_external_access=false`, not a restriction — and without the lock a
statement simply widens the allowlist again. Afterwards the lazy `read_parquet` views
still resolve (they are inside the allowed directory) and every path outside it is a
permission error, whichever function reaches for it.

There is deliberately **no function blocklist**. Around 40 of ~2 900 built-ins touch the
filesystem and that ratio moves every release, so a list would be stale on the next
upgrade while the confinement covers the ones nobody enumerated.

The views are **lazy `read_parquet`, not materialized tables** — measured at 300k rows,
6.8 ms for the view against 14.9 ms to copy the rows in first; the parquet is already
the columnar format DuckDB wants. Their file list comes from the manifest via
`store.partition_files`, never a glob: the store leaves the previous generation on disk
for readers still holding the old manifest (`index-store.md §4`), so a glob would read
two generations and silently double every count. An index with no partitions yet gets
typed empty stand-ins, so a query written against a built index fails on nothing but its
own logic.

Two limits: the row cap is pushed into the SQL as an outer `LIMIT limit + 1` (so a
whole-index query is never materialized just to be trimmed), clamped server-side to
`MAX_LIMIT` whatever the client asks, and one row past the cap is what sets `truncated`.
A PRAGMA is not a subquery-able expression, so a wrap that fails to parse falls back to
the bare statement and a fetch cap — those answer in tens of rows by nature. And
`TIMEOUT_S` (10 s) arms a `con.interrupt()`: a cross join is trivial to type and
impossible to bound by inspection, and the caller is a text box. `token`, when
given, arms a second `con.interrupt()` on the same connection — whichever fires
first wins, and only an interrupt this token's own `cancel()` caused comes back
as `Cancelled`; a real `TIMEOUT_S` timeout keeps surfacing as
`duckdb.InterruptException`, mapped to the caller's usual 400.

**Natural language** (`POST /api/index/ask`) is a thin hop on top: the question goes to
the existing AI relay with a system prompt carrying the two schemas and the units, the
reply is stripped of code fencing, and the result runs through `run_guarded` unchanged.
Nothing trusts the model — the prompt asking for a SELECT is a hint, the gate is the
boundary — and the compiled statement is returned to the caller whatever happens to it,
including when it is refused, because a wrong answer with the SQL visible is debuggable
and a bare error is not.

## 6. `search_under` — the explorer's in-folder corpus

The explorer's in-folder search used to re-walk the tree live on every search
session. `search_under(cfg, root, q, limit)` answers the same corpus from the index:
entries in **exactly** the shape `/api/fs/walk` streams — `rel` (posix, relative to
`root`), `is_dir`, `size`, `mtime` — so the client's fuzzy scoring, throttles and paging
are indifferent to which source produced them. Files come from the partitions, folders
from `dirs.parquet`; the corpus is capped at `MAX_CORPUS` (200 000), the same cap the
walk uses, and flags `truncated` the same way.

The two sources are **one** query — a `UNION ALL` under a single
`ORDER BY depth, path LIMIT limit + 1`, so files and folders compete for the same
budget by depth and the capped corpus keeps the breadth-first character of the walk
it replaces. Serving files first and giving folders the leftover meant that on any
tree big enough to truncate there was no leftover, and folder search was dead rather
than degraded. The named cost of the fix: folders now spend budget files used to have,
so a very large tree carries slightly fewer files.

Two flags travel with it:

- **`covered`** — the scan visited *this exact directory* (a `dirs.parquet` row for
  `root`), not merely some ancestor. A folder that was pruned, ignored, or left below a
  cancelled run's frontier therefore reports honestly instead of answering with a
  partial corpus. **This is the gate**: covered means the index answers, uncovered means
  the live walk does. A package directory (`scan-ignore.md §3`) is the one row that does
  *not* count: it is recorded as an opaque leaf, so its row means "this is a leaf", not
  "we know what is inside", and a search rooted at one hands over to the walk. So does a
  search rooted *inside* one: an index written before that rule holds real dirs rows for
  package internals, and answering from that partial set while the folder one level up is
  answered by the walk is the same disagreement the shared constant exists to prevent.
- **`fresh`** — the last compaction is within `FRESH_MAX_AGE_S` (1 h). Reported, not
  enforced. Age does not decide anything because the index is rescanned at every
  startup, a rescan keeps serving its last completed generation
  (`index-store.md §4`), and the search box says "indexing…" while one runs. An
  instant, mostly-right answer under a visible caveat beats re-walking the tree; a
  folder the index never reached has no answer at all, and that is what falls back.

**A miss is never an error.** No index yet, a first-boot scan still running, a root
outside the scanned roots, a stale index, a failed request — all of them return
`{covered: false, entries: []}` with a 200. None of them is something the user can act
on, so none of them is reported as a fault.

They are not one ACTION, though, and the ranked route next door
(`server-api.md §7.1`) is where that distinction lives: `covered: false` there carries a
`reason`, and the in-folder search scans an uncovered folder on demand, polls a folder
being scanned, and reserves the live streamed walk for the folders no scan can ever
cover. This route keeps the older, coarser contract because it is the public
`fused.fileIndex.search` bridge, not because the app still decides that way.

The pruning of §4 applies with the folder prefix, which is what makes an in-folder
search read a slice of the index rather than all of it.

`q` is an **optional** server-side substring filter. The explorer deliberately does not
pass it: its client-side matching is subsequence-based, so pre-narrowing to substrings
server-side would silently drop legitimate matches. It exists for a caller that only
wants the hits — including `fused.fileIndex.search`, the public JS bridge this
function backs.

`q` is run through the same `expand_whitespace_query` (§3) `resolve_query` runs `raw`
through — one shared implementation, not two copies of the whitespace rule, so the
public API gets the identical grammar the two search boxes do. `q` is a no-op — the
filter stays the plain `ILIKE` substring test it always was — only when it has
**neither** whitespace **nor** `*` anywhere. Either one flips the filter to the same
glob-to-regex matching `resolve_query`'s glob mode uses (keyed on `"*" in expanded`,
mirroring `resolve_query`'s own `is_glob` check), confined to any depth under
`root` — this function never walks a base off `q` the way `resolve_query` does, so the
whole expanded pattern always searches any depth. A bare `*`-containing `q` (`*.pdf`)
is therefore no longer a literal-character `ILIKE` match either — same accepted
precision-glob consequence as §3.

## 7. The source helpers are public — `files_src` / `dirs_src`

`files_src(cfg, partitions)` and `dirs_src(cfg)` are the only sanctioned way to name the
two views in SQL, and they are public because a **second reader** now uses them: the AI
file search's index engine (`server/routers/search.py`, `POST /api/search/files`), which
compiles its validated filter spec into one query over these views and returns rows
without statting anything. It is that endpoint's **only** engine — Spotlight and the
bounded home walk that used to sit behind it are gone — so the index's coverage is now
the search's coverage: the configured roots, home by default, and a missing index is
reported to the user as an error instead of as an empty disk. Those filters and refusals
belong to that module; what belongs here is the invariant every reader must obey: the
views are named from the **manifest**, never from a glob of the files dir, because a
compaction leaves the previous generation's partitions on disk for readers still holding
the old manifest (`index-store.md §4`) and a glob would count both generations.

It also inherits §6's lesson the hard way: two views under ONE ordered budget starve one
of them. There it was files served before folders; there it was a folder losing a shared
recency cap to the files inside it, which match the same name term and are newer. Any
reader of both views owes them **separate budgets or an order that cannot starve either
kind** — folders in that engine get a reserved share of the result cap and are ordered
shallowest-first, since a dirs row's `mtime_ns` may be 0 (unknown) and would sort last.

The schema is what decides which spec fields a search can offer at all: the files table
carries `mtime` and **no birth time**, so there is no creation-date filter anywhere in
the feature — the endpoint refuses one rather than answering it with modification time.

## Non-goals

- **Writing or repairing the index** — `scan.md` / `index-store.md`.
- **Excluding build folders from results** — they are never indexed (`scan-ignore.md`).
- **Full-text search inside file contents** — the index stores metadata only. A
  path-sorted metadata parquet is the wrong shape for it; SQLite FTS5 as a sibling
  store would be the cheap path if it is ever wanted.

## Open questions

- `stats` with `breakdown=True` still groups by extension over every pruned partition;
  there is no cached rollup, so a wide root costs a full scan of its slice of the index.

## See also

- `index-store.md` — schemas and the manifest this spec reads.
- `server-api.md` — the routes that expose §2, §5 and §6.
- `platform.md` — path canonical form; anchoring in §6 depends on it.
