# Server API

> **Status — shipped.** This file owns the **HTTP surface** over the index and the
> **startup scan scheduler**. Implementing module:
> `fused_render/server/routers/index.py`. The engine behind it is `scan.md` (writes)
> and `query.md` (reads); the store is `index-store.md`.

## 1. Routes

Mutating routes carry the standard `X-Fused: 1` guard (a POST can be fired blind
cross-origin; a custom header forces a preflight that fails there). Reads are
unguarded like every other read endpoint and none of them can write.

| Route | Guard | Purpose |
|---|---|---|
| `POST /api/index/scan` `{root?, full?}` | X-Fused | start a detached scan; `{run_id, root}`. No `root` means the first configured root (§3). A non-directory or mount-backed root is a 400. |
| `POST /api/index/cancel` `{run_id}` | X-Fused | touch the run's cancel flag |
| `GET /api/index/status?run_id=&since=` | — | scan state (§2) |
| `GET /api/index/runs` | — | the 20 most recent runs with their folded state |
| `GET /api/index/stats?root=` | — | totals + per-extension breakdown (`query.md §2`) |
| `GET /api/index/lookup?q=&limit=&offset=&sort=` | — | path lookup (`query.md §3`) |
| `GET /api/index/search?root=&q=&limit=&fmt=` | — | the explorer's in-folder corpus (`query.md §6`); `fmt=columns` for the compact encoding (§6) |
| `GET /api/index/rank?root=&q=&limit=` | — | the home page's search: filtered AND ranked server-side, top `limit` hits (§7) |
| `POST /api/index/query` `{sql, limit?}` | X-Fused | run ONE read-only statement over `files`/`dirs`; `{columns, rows, truncated}` (`query.md §5`) |
| `POST /api/index/ask` `{prompt, limit?}` | X-Fused | compile a question to SQL through the AI relay, run it under the same guard, echo the `sql` (`query.md §5`) |
| `GET /api/index/config` · `POST /api/index/config` | X-Fused on write | scan roots + ignore list (§3) |
| `POST /api/index/delete` | X-Fused | drop the whole store (§5) |

Every route resolves its `IndexConfig` from disk **per request**. That is what
de-globalizing the engine bought: an ignore-list edit applies to the next scan with no
restart, and a test's redirected home is honoured by the same process that served the
previous test.

`query` and `ask` are the two routes that are **guarded despite being reads**. They
write nothing, but they execute a statement the caller (or a model) wrote, and that is
not something a foreign page should be able to fire blind — so they are POST-only and
carry the header. Both map every refusal to a 400 with the message verbatim, including
duckdb's own ("no such column" is the caller's typo, not a server fault), and `ask`
returns the compiled `sql` even when the guard refuses it. What makes them safe is
`query.md §5`, not the header.

Both also run their statement **off the event loop**: a guarded query is duckdb plus
disk, bounded only by `TIMEOUT_S` (10 s), and a handler that blocks that long freezes
every other request the app is making — including the status polling the same panel
does. `query` gets that for free by being a plain `def` handler (FastAPI threadpools
those); `ask` must be `async` for the AI relay hop, so it asks for a threadpool
explicitly.

## 2. Status, and the first-boot window

`GET /api/index/status` answers flat, render-ready state: `running`, `phase`, `dirs`,
`files`, `reused`, `current`, `summary`, `cancelled`, `error`, plus `run_id`, `root`.

Four fields carry the UI's decision, and they are deliberately independent:

| Field | Meaning |
|---|---|
| `scanning` | a run is in flight |
| `has_index` | a compaction has completed at least once |
| `files_indexed` | rows in the last COMPLETED index (vs `files`, this run's progress) |
| `last_completed_at` | when that compaction finished |

`scanning` does NOT mean "stop using the index": a rescan keeps serving its last
completed generation (`index-store.md §4`), so `scanning && has_index` means *say
"indexing…"*, not *fall back*. The full table is `query.md §6`.

**Without a `run_id` it reports the most recent run.** That is the case a page that just
loaded is actually in: it has no run id, but the startup scan may well be in flight, and
the UI needs to be able to say "building index… N files" rather than pretending nothing
is happening. With a `run_id` it additionally returns `events[since:]` and a `cursor`,
so a poller fetches only what it has not seen (`scan.md §3`).

**The first scan is not a blocking state.** Until a compaction has completed, the
explorer's in-folder search falls back to the live walk. "No index yet", "this root is
not covered yet" and "the request failed" are the *same* condition to the search seam:
fall back silently, never surface an error. From the second scan onward the index keeps
answering while the rescan runs, under the "indexing…" caveat (`query.md §6`).

## 3. Configuration

`<index>/config.json` holds the two user-editable knobs:

- **`roots`** — what the startup scheduler scans. Empty (the default) means **the
  user's home directory**. Home rather than the project root because a whole-home scan
  with the default ignore rules costs seconds, and an index covering one project
  answers almost none of the searches the explorer wants to ask. `start_dir` is
  deliberately NOT a fallback: an index that followed whichever folder the app was
  opened on would give different answers per window.
- **`ignore`** — the prune list (`scan-ignore.md`). `GET` also returns `defaults` so a
  UI can offer "restore defaults".

Both are edited from **Preferences > Indexing** (`frontend/src/shell/Indexing.tsx`),
which also carries the manual actions of §5.

**A write reconciles.** The engine fingerprints the rules an index was BUILT under
(`scan-ignore.md §4`); while the saved rules differ, the store still holds rows for
folders the user just excluded and still lacks the ones they just re-included. So a
write that changes the fingerprint starts a scan itself and reports
`{needs_rescan, rescan_run_id}`, rather than leaving the index disagreeing with its own
rules until the next boot. With no index yet there is nothing to reconcile and nothing
is started.

## 5. Deleting the index

`POST /api/index/delete` removes the partitions, the directory signatures, the manifest,
the FSEvents positions, the applied-rules fingerprint and the last-scan record — leaving
`config.json` and the run directories.

- **Run dirs stay** because a scan in flight polls its `cancel` flag from one. Any
  running scan is cancelled *first*: a worker that survived the delete would compact its
  shards into the store moments later and quietly undo it.
- **The last-scan record goes**, so the next startup rescans immediately instead of
  debouncing against a scan whose output no longer exists.
- **Deleting is not destructive** in any user-visible sense: the index is derived data,
  and search silently reverts to the live walk. Deleting an already-empty store succeeds,
  which is what makes the button safe to press twice.

## 4. The startup scheduler

`create_app` registers one startup hook. It runs off the event loop and can never
delay serving: the scan itself is a detached worker, so the hook is a `Popen` per root.

1. **Reclaim** run directories beyond the newest `KEEP_RUNS` (`scan.md §2`).
2. For each root: skip it if it does not exist, or if a scan **started** within
   `SCAN_DEBOUNCE_S` (15 min). The debounce reads the start time, not the finish time,
   so a scan still running suppresses the next one just as firmly.
3. Otherwise start an incremental scan. Later boots are cheap by construction — the
   directory-mtime rule (`scan-incremental.md §2`) turns an unchanged tree into one
   stat per directory, and on macOS the FSEvents journal skips even that.

Nothing here raises: a bad root is logged and the remaining roots still run, and a
failure to read the config aborts the scheduling rather than the server.

**In tests** the hook is patched out wholesale (`tests/conftest.py`
`_no_startup_index_scan`) — a suite that ran it would spawn a worker over the
developer's real home. The scheduler's own tests call `run_startup_scan()` directly.

### 4.1 The startup warm

The same hook then calls `startup_warm()`, which spawns one detached daemon thread
running `run_startup_warm()`: `search_under` + `filter_corpus` over `expanduser("~")`,
and then `search_ranked` with a representative query over the same root — exactly the
two requests the explorer makes (the in-folder corpus and the home page's ranked
search), under exactly the same pool key. Warming only the corpus would leave the
ranked path's own duckdb plan cold, and that is the one a keystroke waits on.

Everything that path caches is **per process** and starts empty: the gitignore verdict
pool has no verdicts until some request sweeps `git check-ignore` over the whole corpus,
and `duckdb` is not imported until the first query. Measured on a 164k-entry home,
that made the first search of a fresh process ~2.2 s against ~0.8 s for the next one,
all of it billed to a keystroke. The warm moves it to idle.

- A **thread**, not `asyncio.to_thread`: a startup hook must complete before the app
  serves, and this is seconds of work.
- A **mount-backed** home is skipped by `MountGuard.blocks_root`, the same check
  `runner.start` makes, before anything touches the path: the index refuses to scan
  mounts, so the warm could only answer `covered: false` after aiming kernel I/O at a
  mount. The guarantee is about the mount, not about cost — a path under the mounts dir
  matches on `abspath` alone, while a local home falls through to `is_mount_backed` and
  pays two `realpath`s, neither of them on the mount.
- **One bounded wait on a first boot.** Usually the index is already there, the search
  answers `covered: true`, and the warm is those two calls and nothing else. When it
  answers `covered: false` — first-ever boot, nothing to sweep — the warm waits for the
  one scan `run_startup_scan` just spawned for that root (recorded in `_startup_runs`)
  and then searches again. It is not a scheduler: one named run, polled every
  `WARM_WAIT_POLL_S` (0.5 s, the worker's heartbeat cadence) with a hard
  `WARM_WAIT_DEADLINE_S` ceiling (6 min — just past `runner.ABANDONED_RUN_S`, so a dead
  worker is reported not-running before the ceiling is reached), on a daemon thread that
  cannot hold the process open. The search and the sweep then run **whatever ended the
  wait** — a finished scan, a dead worker, a pruned run dir, the ceiling — because how a
  scan ended does not tell you whether the index covers the root, and an uncovered one
  costs one cheap `covered: false`. Giving up is logged once.
  A root whose scan was **debounce-skipped** has no entry and is not waited on: no new
  run is coming and its index is already on disk. The persisted verdict pool
  (`server/index_gitignore.py`) is what covers restarts.
- The warm and the **first keystroke overlap by design** — the warm runs for ~2.2 s and
  the user is typing inside it — so `server/index_gitignore.py` coordinates them: the
  second caller to want verdicts for the same base waits on the sweep already in flight
  (`_inflight`, `SWEEP_WAIT_MAX_S`) and reads the pool it produced, instead of shelling
  out to `git check-ignore` over the same 200k entries a second time.
- It **never raises**: nobody joins the thread.

Patched out in tests by the same fixture, and its own tests call `run_startup_warm()`
directly.

## 6. The compact corpus — `fmt=columns`

`GET /api/index/search` answers with one object per entry
(`{rel, is_dir, size, mtime}`), which is the shape `/api/fs/walk` streams and the
shape every existing caller — including the JS bridge `fused.fileIndex.search`
(`static/runtime.js`) — reads. That shape stays the default, and an unrecognized
`fmt` falls back to it rather than 400ing: a format nobody asked for must not be able
to break a page someone wrote.

`fmt=columns` re-cuts the same corpus as index-aligned parallel arrays —
`rels`, `dirs` (0/1), `sizes`, `mtimes`, plus `fmt: "columns"` — and leaves every other
field (`covered`, `fresh`, `truncated`, `total`, `updated`, `age_s`, …) exactly as it is.
`size`/`mtime` stay nullable: a directory legitimately has neither. The explorer asks
for it and decodes back to the walk's entry shape at the API-client boundary
(`platform/lib/api.ts`), so the corpus consumers never learn the wire format.

Why, measured on the 164k-entry home corpus this route exists for (25.7 MB, fetched in
one shot on the user's **first keystroke**):

| Body | Bytes |
|---|---|
| `entries` objects | 27.7 MB |
| `fmt=columns` | 21.1 MB |
| `fmt=columns`, gzip level 1 | 5.0 MB |

The compact response is therefore also **gzipped** — level 1, and only when
`Accept-Encoding` says the caller can take it. Level 1 costs 0.06 s, less than the JSON
encoding itself (0.07 s); the higher levels spend seconds to shave a few percent off a
body that is read once. It is done per-route rather than with a GZip middleware because
this app also streams the live walk and serves file bytes raw, and compressing those on
a **local** server is CPU spent against loopback for nothing — this one response is the
outlier.

Rejected: front-coding the rels (they arrive depth-then-path ordered, so neighbours
share long prefixes). It takes the body to 12.4 MB, but costs 0.45 s of Python per
corpus — more of the first search's budget than it saves, and gzip finds most of the
same redundancy for an eighth of the time.

## 7. The ranked search — `GET /api/index/rank`

`GET /api/index/rank?root=&q=&limit=` answers `{ok, covered, fresh, updated, age_s,
root, hits, total, truncated, escalated, scanned_partitions, of_partitions}`, where each
hit is `{rel, is_dir, size, mtime, score, longest_run, tier, depth}` and `limit`
defaults to 200 (hard cap `MAX_RANK_LIMIT`, 2,000). Plain JSON, a few KB — no columnar
encoding and no gzip special-casing, because that machinery (§6) exists for a 20 MB
corpus and this is not one. A miss is a 200 with `covered: false` and no hits, exactly
as for the corpus.

**Why it exists.** §6's corpus is the whole ranking set shipped to the browser: 19.8 MB
raw / 5.4 MB gzipped for 164,405 rows on a home directory whose index actually held
571,429 files. `MAX_CORPUS` (200,000, depth-ordered) plus the gitignore filter is what
cut it down, so ~71% of that home was unfindable from the home search while the
response reported `truncated: true` and the client ranked what it got. This route
filters and ranks over the WHOLE index and returns the part anybody reads.

**Two stages** (`query.md`, `search_ranked`), because neither is affordable alone: SQL
narrows and coarse-orders over every row under the root; Python's ranker
(`index/rank.py`) scores a bounded slice of at most `RANK_CANDIDATE_CAP` (2,000) rows.
Scoring every subsequence candidate in Python is the 186 ms case that shape avoids.

Stage A is itself a **ladder**, cheapest pass first:

| pass | filter | `render` over 571k rows | `readme.md` |
|---|---|---|---|
| 1 | `lower(rel) LIKE '%q%'` | 30,319 rows / 51 ms | 3,056 / 41 ms |
| 2 | `regexp_matches(lower(rel), 'a.*b.*c')` | 176,505 / 143 ms | 11,766 / 45 ms |

Pass 2 runs only when pass 1 cannot fill the returned `limit` after ranking and
gitignore filtering, and `escalated` says which happened. That is **lossless, not an
approximation**: `fuzzyMatch`'s substring branch sets `longestRun = len(q)`, the maximum
the subsequence branch can never reach, and `rankCompare` orders on `longestRun` first,
so every substring hit outranks every subsequence-only hit and a filled cut leaves pass
2 nothing to contribute above it. The check runs on the FILTERED count, so it needs no
safety margin.

**The gitignore filter runs before the cut to `limit`**, never after — filtering after
the cut is precisely what let the corpus report more rows than it held.

**`positions` are not returned.** The client re-runs `fuzzyMatch` over the ~200 rows it
got back to build its highlights, so `platform/lib/fuzzy.ts` stays the single source of
truth for what highlights, and the server's port of it stays free to carry positions
internally without them becoming a wire contract.

**Parity is a test, not an intention.** `index/rank.py` is a port of `fuzzy.ts` +
`listing/search.ts`, which remain the authority — the in-folder search still ranks a
live streamed walk in the browser, and only a browser-side ranker can rank a stream. The
same box is therefore answered by either ranker depending on coverage, so both assert
against `tests/fixtures/rank-parity.json` (generated from the JS side by `bun
scripts/gen-rank-fixture.ts`): `tests/test_index_rank.py` and
`frontend/src/apps/explorer/listing/rank-parity.test.ts`.

**Known and logged:** stage A's cap can in principle drop a row stage B would have
ranked first. Tier ordering makes it unlikely (a name-substring hit outranks a
fuzzy-only one in `rank_compare` too), and a cap that bites is a `logger.warning` plus
`truncated: true` — silent truncation is what this route removes, not what it
reintroduces.

## Non-goals

- **Watching the filesystem** — there is no daemon. Freshness comes from the startup
  scan, on-demand scans, and the open-folder check `GET /api/fs/list` performs
  (`scan-incremental.md §5`): a folder whose own mtime is ahead of the index gets its
  root rescanned in the background. A folder nobody opens still waits for a startup or
  manual scan, and a change deeper than the folder being viewed does not move its
  mtime — an ambient watcher remains a later project.
- **Indexing remote mounts** — refused structurally (`scan-ignore.md §7`).
- **Deciding what SQL is safe** — the guard is `query.md §5`; these routes only adapt it
  to HTTP.

## See also

- `scan.md` — the lifecycle §1 and §4 drive.
- `query.md` — what §1's read routes return.
- `scan-ignore.md` — the list §3 edits.
