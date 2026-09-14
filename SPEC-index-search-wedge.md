# SPEC — Index search wedge: a parked thread permanently halves the read lane

## Background / the bug being fixed

A user reported every search on the Files home screen taking 2+ seconds, across page
refreshes, with low CPU. Force-quitting and restarting the desktop app returned it to
~150ms. Diagnosis:

`/api/index/rank` (`fused_render/server/routers/index.py`, `api_index_rank`) holds one of
**two** process-wide `asyncio.Semaphore` permits (`_interactive_read_concurrency()`, width 2)
across `await asyncio.to_thread(_rank_body, ...)`. `asyncio.to_thread` **cannot kill its
thread**. If that worker thread parks on an uninterruptible filesystem syscall — most
plausibly `os.path.isdir` in `fused_render/index/query.py::_walk_from`, which walks the
**user-typed query string** segment by segment against the real filesystem *before*
`MountGuard` is ever consulted — the permit is consumed forever. The lane degrades from
width 2 to width 1 (or 0), every subsequent keystroke serialises behind it, CPU stays low,
and only a process restart clears it.

This repo has a known failure class of wedged NFS/rclone mounts parking threads on
`stat`/read indefinitely, curable only by force-unmount or restart. That is the trigger.

Six changes below: one removes the trigger, the rest make the damage non-permanent and
observable regardless of trigger.

---

## Item 1 — Guard `_walk_from`'s filesystem stat with `MountGuard`

**File:** `fused_render/index/query.py`, `_walk_from` (~`:465-489`).

Today it does `if not os.path.isdir(candidate): break` on path segments derived from the
user's raw query text. Against a wedged mount that call never returns.

- Before any `os.path.isdir` / `os.stat` on a candidate path, consult `MountGuard`
  (`fused_render/server/...mounts` — same class `_rank_reason` uses:
  `MountGuard(mounts_dir=...).blocks_root(path)`) and skip/abort the walk for a blocked path
  rather than stat it.
- `query.py` is a library module — do **not** introduce a server-layer import cycle. If
  `MountGuard` is not cleanly importable there, thread an optional guard/predicate callable
  down from the caller (`search_ranked`'s caller in `routers/index.py`) with a default that
  preserves today's behaviour. Choose whichever is cleaner and record the choice in
  `DECISIONS.md`.
- Behaviour for a blocked path: treat it as "not a directory" (the same `break`), do not
  raise.

## Item 2 — Time-bound the semaphore permit in `api_index_rank`

**File:** `fused_render/server/routers/index.py`, `api_index_rank` (~`:1525-1650`).

The permit must never be held longer than a bounded interval, whatever the worker does.

- Wrap the worker: `await asyncio.wait_for(asyncio.shield(fut), ABANDON_S)` where `fut` is
  the `asyncio.to_thread(...)` task and `ABANDON_S = 5.0` (module constant, documented).
- `shield` is required: on timeout we abandon the future but must **not** cancel-await it,
  since the thread cannot be killed and awaiting it would re-block.
- On `asyncio.TimeoutError`: call `token.cancel()` (cooperative — reaches a running DuckDB
  statement via `con.interrupt()`), log a WARNING naming the root + query + elapsed, keep a
  reference to the abandoned future in a module-level set with a done-callback that discards
  it (so it is not GC'd mid-flight and so a later completion is not an "exception never
  retrieved" warning), release the permit by exiting the `async with`, and return
  **HTTP 503** with a short JSON body (`{"error": "index read timed out"}`).
- Keep the existing 499-on-cancel behaviour intact.

## Item 3 — Dedicated thread pool for index reads

**File:** `fused_render/server/routers/index.py` (alongside the lane helpers, ~`:60-138`).

Abandoned threads must not consume the process-wide default executor, which is shared with
every other `asyncio.to_thread` caller in the app.

- Create a module-level `ThreadPoolExecutor(max_workers=6, thread_name_prefix="index-read")`
  — deliberately wider than the lane width (2) so a few abandoned threads do not starve the
  lane. Do **not** call `loop.set_default_executor`.
- Route the rank/search/stats worker calls through it via
  `loop.run_in_executor(_INDEX_READ_POOL, functools.partial(...))` instead of
  `asyncio.to_thread`.
- Comment the sizing relationship (pool 6 > lane 2) so a future edit to one prompts the
  other.
- **Loud failure when exhausted:** track how many permits/threads are currently abandoned
  (Item 2's set). When the count reaches the pool size, log an ERROR and return 503
  immediately rather than queueing on the executor's unbounded work queue — an unbounded
  wait is the exact failure mode being fixed.

## Item 4 — Move `_rank_reason` off the event loop

**File:** `fused_render/server/routers/index.py`, `_rank_reason` (~`:430-478`) and its call
site in `api_index_rank`.

On a cache miss `_rank_reason` calls `MountGuard(...).blocks_root(root)`, whose own comment
says a stat under a wedged rclone mount blocks the thread indefinitely — and it currently
runs **on the event loop**, so it can stall the whole server, not just one request.

- Compute the reason inside the worker (in `_rank_body`, or as a second
  `run_in_executor` call on the same pool inside the lane) so it never runs on the loop.
- Preferred: fold it into `_rank_body` so it costs no extra hop. Preserve the exact reason
  values the existing tests and the frontend `RankReason` contract expect.

## Item 5 — Cache the per-generation parquet schema lookup

**File:** `fused_render/index/query.py`, `search_ranked` (~`:806-1112`), `_src_cols`.

Every keystroke pays one `DESCRIBE` against the parquet set (~8ms measured on a fresh
connection) purely to learn the column set, which is uniform per manifest generation.

- Add a module-level cache `dict` keyed on `(cfg root identity, manifest["generation"])`
  → the resolved column tuple. `generation` is an integer in `partitions.json` that
  increments on compaction, so a new generation invalidates naturally.
- Bound it (e.g. keep at most ~16 entries, evict oldest) so it cannot grow without limit.
- Do **not** introduce a shared DuckDB instance or connection pool. Connect-per-request plus
  the semaphore cap stays exactly as it is — this is a deliberate design decision (a shared
  instance would widen the blast radius of the very wedge being fixed).

## Item 6 — Split the timing log

**File:** `fused_render/server/routers/index.py`, the `logger.debug("index rank: ...")` line
at the end of `api_index_rank`.

Today one number covers lane wait + query, so a wedged lane is indistinguishable from a slow
query in the logs.

- Measure and log both separately: time spent waiting to acquire the semaphore, and time
  spent in the worker.
- Log at DEBUG normally; log at **WARNING** when the total exceeds ~750ms, including both
  components in the message so the wedge is self-identifying.

---

## Tests (add to `tests/test_index_api.py` unless noted)

TDD: write each test first, watch it fail, then implement.

1. **Regression test for the wedge (fails today).** Stub the rank worker body so the first
   request blocks forever (a `threading.Event` that is never set). Fire that request, then
   fire subsequent rank requests **in the same process**. Assert they answer in under ~300ms
   and that both lane permits are available afterwards (i.e. two further concurrent requests
   both proceed). Today the second request blocks for the life of the process.
2. **Pool exhaustion is a fast explicit error.** Block enough workers to reach the pool
   size; assert the next request returns a fast 503 with the expected body rather than
   waiting.
3. **A slow `MountGuard` does not stall the event loop.** Stub `MountGuard.blocks_root` to
   sleep ~1s, trigger a rank request that misses the reason cache, and assert an
   **unrelated** route (e.g. a trivial existing GET) still answers promptly while it is in
   flight.

Also: existing tests in `tests/test_index_api.py` (~`:241-470`) and
`tests/test_index_rank_concurrency.py` (~`:343`) assert `elapsed < 2.0`. That bound is part
of the blind spot that let this ship — where those assertions cover the interactive lane,
tighten them to something that would actually have caught a serialised lane (~300-500ms), but
only where the test's own setup makes that bound legitimate. Do not tighten a bound you then
have to fight; note any you left alone and why in `DECISIONS.md`.

---

## Explicitly out of scope

- DuckDB connection pooling / a shared DuckDB instance. Measured at ~10ms of the budget and
  it would widen the wedge's blast radius. Connect-per-request + semaphore cap stays.
- Any frontend change. `FilesHome.tsx` (abort-per-keystroke, memo LRU, trailing debounce) and
  `api.ts::indexRank` were audited and are sound.
- Changing the lane widths (interactive 2, query 1).

## Working rules

- Repo dev env is already set up in this worktree: `.venv` (3.12) and `frontend` build.
  Use `.venv/bin/python -m pytest`.
- **Scoped tests only in your inner loop** — `-k` / single file / at most the touched module.
  Never run the full suite; the orchestrator runs it once at the end.
- Commit per logical unit (roughly per item) with a clear message. Do not squash.
- macOS local suite baseline is ~19 red. If you do run anything broad, judge by diffing
  failure-ID sets against `main`, never by failure count.
- Do not start a dev server (`scripts/dev.sh`) — not needed, and it contaminates shared state.
- Record decisions, dead ends, and anything this spec got wrong in `DECISIONS.md` in this
  worktree.
