# SPEC: a fruitless search asks for a scan

## Why

The live watcher (`SPEC-index-live-watch.md`, PR #1279) is the push side of
index freshness: the filesystem tells us what changed. It works, but its
latency is structural — ~1.6 s debounce, then up to `WATCH_FLUSH_FLOOR_S`
(30 s) of global flush floor, then the scan, then a whole-store compaction.
A user who just made a file and immediately searches for it waits.

This is the pull side, and unlike the three trigger designs that failed
before it (see `DECISIONS.md` and the memory on time-based staleness), it is
**not a clock guess**. "The user typed a query and we found nothing" is real
evidence that the index may be behind. That is the entire basis for this
feature, and it is why it is sound where a staleness timer was not.

## What already exists (do not rebuild it)

Almost all of this is built. Read these before writing anything:

* `POST /api/index/scan-folder` — `api_index_scan_folder`,
  `fused_render/server/routers/index.py:1514`. Built for a search box:
  never errors, every refusal is durable (`refused` / `debounced` /
  `joined`), honours the indexing gate, refuses mount-backed roots before
  any syscall, and is debounced by the scheduler's own `SCAN_DEBOUNCE_S`
  specifically so a keystroke-rate caller cannot storm it. Its docstring
  already describes the intended UX: "the box asks, the scan runs, and the
  box polls `/api/index/rank` (reason `scanning`) until rows appear."
* `requestFolderScan(fsPath)` — `frontend/src/platform/lib/api.ts:843`.
  The client helper for that route. **It currently has no call site
  anywhere** — only comments refer to it. Confirm that yourself
  (`grep -rn requestFolderScan frontend/src`) before assuming otherwise.
* `indexGap(reason, scanning)` — `frontend/src/apps/explorer/lib/home-search.ts`
  (~line 895). Classifies a not-covered answer into
  `scanning | buildable | disabled | fda | unavailable`. The live status
  poll wins over the frozen `reason` in **both** directions.
* `EmptyResult` — `frontend/src/apps/explorer/listing/empty-result.tsx`.
  `reason === ""` is the only case that renders a bare "No matches"; every
  other reason renders a gap message. `gap === "scanning"` already renders
  the right "a scan is running" copy.
* `useIndexStatus(active, nonce)` + `indexNonce` / `onScanRequested()` —
  `frontend/src/apps/explorer/FilesHome.tsx:1455` and `:1544`. Bumping the
  nonce restarts the poll, turning its idle ten-second beat into an
  immediate look. `FilesHome.tsx:963` records why a keystroke-driven caller
  must use `requestFolderScan` and not `startIndexScan()`: the latter has no
  debounce and is a button-only path.
* `indexKey` — `FilesHome.tsx:1470`, the `scanning|last_completed_at` pair.
  Deliberately over-eager, and it covers every way a scan can end
  (completed, cancelled, failed, killed). Today it drives only the git-repos
  refetch.

## The change

### 1. Fire the scan on a covered-but-empty answer

In the **home search box** and the **in-folder search box**, when a
**settled** answer says the root is covered (`reason === ""`) and the hit
set is **empty**, call `requestFolderScan(<that answer's root>)` and bump the
index poll nonce so the poll sees the run without waiting for its idle beat.

Conditions, all required:

* the answer is settled, not an in-flight/provisional one;
* `reason === ""` — a covered answer. Every other reason already has its own
  handled gap state and its own affordance; do not touch those paths;
* zero **file** hits (the row model can carry a leading "Open" row and a
  trailing AI row — neither is a file hit; `fileCount` in `FilesHome` is the
  real count);
* the trimmed query is at least 2 characters. A one-character query matching
  nothing is not evidence of a stale index;
* fire **at most once per distinct trimmed query string** in a session, so
  backspacing and retyping the same query does not refire. The server's
  `SCAN_DEBOUNCE_S` is the cross-query floor — do not add a second timer in
  the client; `api_index_scan_folder`'s docstring is explicit that its floor
  is deliberately the only one.

Use the answer's own root (the same value the box searched), not a
hardcoded home and not "every configured root".

A refusal from the route (`refused` / `debounced` / `joined`) is a normal,
expected answer and must be silent — no error surface, no retry. The route
never throws, but the fetch itself can; swallow that too. A search must
never fail over housekeeping (`routers/index.py:627` is the existing
precedent for that rule).

### 2. Results must appear without retyping

This is the half that makes the feature real. Today nothing re-runs the
search query when a scan finishes, so a triggered scan would land rows the
user never sees.

Re-run the current query when the index's observable state changes — key off
the same `scanning|last_completed_at` pair `indexKey` already computes rather
than inventing a "scan completed" trigger. `FilesHome.tsx:1457-1469` explains
at length why completion is the wrong signal and why this pair is the right
one; that reasoning applies verbatim here. Extra re-queries are acceptable
(a cheap read, and the view is a pure function of the result) — the same
deliberate over-eagerness.

Do **not** write to the URL from this effect. An unconditional URL write in
an effect keyed on a version value is a known render-loop in this codebase
(`replaceState` is wrapped to fire `fused:urlchange`) and it blanked the
whole explorer once while 3152 frontend tests stayed green.

### 3. Copy while the triggered scan runs

The live poll drives `indexGap`, so once the poll sees the run the existing
`gap === "scanning"` branch should already replace "No matches" with the
scan copy. **Verify this actually happens** for a `reason === ""` answer —
`EmptyResult` currently computes `gap` only when `reason !== ""`
(`empty-result.tsx:34`), so a covered answer may render a bare "No matches"
regardless of the poll. If so, that guard needs to admit the case where a
scan we just asked for is running. Do not duplicate the copy; reuse the
existing branch.

## Out of scope

* No new server route and no change to `api_index_scan_folder`'s debounce.
  If you conclude a server change is genuinely required, stop and write down
  why in `DECISIONS.md` rather than widening the diff.
* Nothing about `WATCH_FLUSH_FLOOR_S` or the watcher. This feature is
  deliberately a second, independent path to the same queue.
* The four open items on PR #1279 (Windows test portability, the Linux
  ENOSPC fallback, startup-frozen ignore rules, the unidentified `$HOME`
  churn source). Leave them alone.

## Tests

Logic changes, so they get tests. Frontend tests are the bulk:

* fires on a covered answer with zero file hits;
* does **not** fire when there is at least one file hit;
* does **not** fire for `uncovered` / `mount` / `package` / `ignored` /
  `disabled` / `fda` — those keep their existing affordances;
* does **not** fire for a one-character query;
* fires once for a given query, not once per keystroke or per re-render;
* a route refusal and a thrown fetch are both silent and non-fatal;
* the query re-runs when the index key changes, and does not loop.

Traps that have burned this repo, all real:

* `bun mock.module` is **process-wide**. One file's stub breaks unrelated
  files in a full `bun test`, and a filtered run is structurally blind to
  it. A complete stub is worse than a narrow one. Local-green/CI-red is
  this bug, not a flake.
* Some **pytest** tests assert on literal frontend source lines. After any
  frontend edit, `grep tests/ ` for every symbol or line you removed or
  renamed.
* Verify the trigger against a **pre-existing** answer object, not only a
  freshly created one — a create-path-only hook once shipped dead for every
  real object while the suite stayed green.
* A headless test cannot see layout. If anything here turns out to depend on
  geometry, say so in your report rather than claiming it verified.

Run **only the touched test files** while iterating — no full suite between
steps. The orchestrator runs the full suite once at the end.

## Notes for the builder

* Branch is `index-live-watch`, which already carries PR #1279 (marked
  ready, not merged). This work folds into that same branch and PR — do not
  open a second branch or a stacked PR.
* `scripts/dev.sh` is running on port 1931, started by the user. Do not
  start, stop, or poke a dev server. Editing `fused_render/` restarts it;
  frontend edits need its rebuild.
* Commit per logical unit. End each message with
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
* Do not put measured numbers in comments unless you actually measured
  them in this session.
* Append decisions, dead ends, and anything this spec got wrong to
  `DECISIONS.md` before reporting done.
