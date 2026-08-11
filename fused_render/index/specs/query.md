# Query

> **Status — shipped.** This file owns the **read path**: the query actions, the
> search-pattern grammar, and partition pruning. Implementing module: `query.py`
> (`stats`, `lookup`, `pattern_for`, `prune`, `SORTS`). The files being read are
> `index-store.md`; the routes that expose these are `server-api.md`.

## 1. Contract

`query.py` is **read-only** — it never writes to the index. Every action first loads
`partitions.json`; when it is absent the call returns `{empty: true, location, …}` with
empty results rather than raising, because "no index yet" is a state the UI renders,
not an error.

| Function | Params | Returns |
|---|---|---|
| `stats` | `root` | totals + per-extension breakdown + manifest |
| `lookup` | `query`, `limit`, `offset`, `sort` | matching rows + pruning telemetry |

duckdb is imported inside each function, not at module top, so a call on a missing
index stays cheap — and so importing the server's router does not pull duckdb in.

## 2. `stats`

Totals are scoped to **one subtree** — the explicit `root` param, else
`partitions.json`'s `last_root` — not the whole index, which may hold several roots
(`index-store.md §4`). Scoping is `dir = root OR dir LIKE root || '/%'`.

Returns `rows`, `dirs`, `total_size`, `updated`, `last_root`, `location`, `partitions`,
and `types`: per-extension `{ext, n, size}` ordered by size, **top 50** with the
remainder folded into a single `other` bucket. Empty extensions are reported as
`no ext`.

## 3. `lookup` — pattern grammar

`pattern_for` turns a user query into a SQL `ILIKE` pattern plus a pruning prefix:

| Query | Behaviour |
|---|---|
| `app` | substring, anywhere in the path — `%app%` |
| `*.parquet` | `*` is the wildcard → `%%.parquet%` |
| `/Users/me/code` | starts with `/` → **anchored** at the path start |
| `C:/Users/me/code` | a drive prefix anchors too; backslashes are normalized (`platform.md §1`) |
| `~/Documents` | `~` is expanded, and anchors too |

LIKE metacharacters in the literal text (`\`, `%`, `_`) are escaped and the query runs
with `ESCAPE '\'`, so a path containing `_` doesn't silently match any character.
Single quotes are doubled. A trailing `/` is stripped; an empty query matches
everything.

**Sorting** is a fixed allowlist (`SORTS`): `path` ASC, `size` DESC, `mtime` DESC,
`name` ASC — anything else falls back to `mtime`. Only these four strings ever reach
the SQL; `limit` is int-cast and clamped to `MAX_LIMIT` (5 000) and `offset` is int-cast
and floored at 0.

## 4. Partition pruning

Partitions are globally sorted by path and the manifest records each one's `min`/`max`
(`index-store.md §4`), so an **anchored** query needs only partitions whose range can
contain the prefix — `prune` keeps those where `max >= prefix AND min <= prefix + "￿"`.
The range test is exact, not a heuristic, because the ordering is total.

The pruning prefix is the literal lead-in up to the first `*`, and only for anchored
queries; an unanchored substring query can match anywhere and therefore scans every
partition. Each response reports `scanned_partitions` / `of_partitions` plus the
partition filenames.

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

`run_guarded(cfg, sql, limit)` returns `{columns, rows, truncated}` over two views,
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
impossible to bound by inspection, and the caller is a text box.

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

The client's decision table (`apps/explorer/listing/index-corpus.ts`):

| has index | scanning | search reads | indicator |
|---|---|---|---|
| no | — | the live walk | "building index… N files" (optional) |
| yes | yes | the index (last completed generation) | "indexing…" — required |
| yes | no | the index | none |

**A miss is never an error.** No index yet, a first-boot scan still running, a root
outside the scanned roots, a stale index, a failed request — all of them return
`{covered: false, entries: []}` with a 200, and the client falls back to the live walk
silently. They are one condition from a search box's point of view, and none of them is
something the user can act on.

The pruning of §4 applies with the folder prefix, which is what makes an in-folder
search read a slice of the index rather than all of it.

`q` is an **optional** server-side substring filter. The explorer deliberately does not
pass it: its client-side matching is subsequence-based, so pre-narrowing to substrings
server-side would silently drop legitimate matches. It exists for a caller that only
wants the hits.

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

- `stats` reads every partition to group by extension; there is no cached rollup, so it
  costs a full index scan on a large index.

## See also

- `index-store.md` — schemas and the manifest this spec reads.
- `server-api.md` — the routes that expose §2 and §3.
- `platform.md` — path canonical form; anchoring in §3 depends on it.
