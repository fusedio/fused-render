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

## 5. No user SQL

OpenIndex exposed a third action that ran the caller's duckdb statement against views
over the index — deliberately unrestricted: no allowlist, no read-only flag, able to
attach files, write, or read anything the user's account can. That was consistent with
a trusted local page where `runPython` already executes arbitrary local Python.

Behind an HTTP route it is not the same surface, so **it is not ported**. Any future
AI-query feature must compile to a guarded, read-only view rather than reintroducing
it. `tests/test_index_query.py` asserts the module has no `sql` attribute, so it cannot
come back by accident.

## 6. `search_under` — the explorer's in-folder corpus

The explorer's in-folder search used to re-walk the tree live on every search
session. `search_under(cfg, root, q, limit)` answers the same corpus from the index:
entries in **exactly** the shape `/api/fs/walk` streams — `rel` (posix, relative to
`root`), `is_dir`, `size`, `mtime` — so the client's fuzzy scoring, throttles and paging
are indifferent to which source produced them. Files come from the partitions, folders
from `dirs.parquet`; the corpus is capped at `MAX_CORPUS` (200 000), the same cap the
walk uses, and flags `truncated` the same way.

Two flags travel with it:

- **`covered`** — the scan visited *this exact directory* (a `dirs.parquet` row for
  `root`), not merely some ancestor. A folder that was pruned, ignored, or left below a
  cancelled run's frontier therefore reports honestly instead of answering with a
  partial corpus. **This is the gate**: covered means the index answers, uncovered means
  the live walk does.
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
