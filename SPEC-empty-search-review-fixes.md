# SPEC: the eight review findings on the zero-match search trigger

Scope is the covered-but-empty search trigger shipped in `b0f338bad`,
`398f08eec`, `824ec55b0`, `a137ca73b`, `674d5a796`. Design context is
`SPEC-empty-search-scan.md` (now tracked at the worktree root). Nothing here
touches `fused_render/index/` — that is a different round and it is done.

Files in play, all frontend:
`frontend/src/apps/explorer/listing/useListingSearch.ts`,
`frontend/src/apps/explorer/FilesHome.tsx`,
`frontend/src/apps/explorer/listing/empty-result.tsx`,
`frontend/src/apps/explorer/listing/FileSearchField.tsx`.

## 1 — MAJOR: the server debounce is a no-op on the first ask per root

`SCAN_DEBOUNCE_S` (`routers/index.py:298`, 5 minutes) is compared against
`runner.last_scan`, which is documented as "Epoch seconds of the last scan
STARTED for **exactly this root**" (`runner.py:430`). A *covered* folder was
covered by an **enclosing** root, so it has no `last_scan` record of its own
and the floor does not apply to the first query against it. The spec's claim
that the server floor is the only debounce needed is therefore wrong for
exactly the case this feature introduces.

Note what is and is not true: the resulting scan is **incremental** (no
`rescan_all` is passed at `routers/index.py:1588`) with dir-cache reuse, so
this is not a full recursive walk per keystroke. It is still an unbounded
number of first-asks across distinct folders.

The sharper half of this finding: the trigger also fires from
`FileSearchField.tsx:67`, a **third** call site that has no
`onScanRequested` wiring and no UI that explains why a scan started. Either
give it the same wiring or exclude it deliberately, and say which in the
commit message.

## 2 — MAJOR: the "still building" branch keys on the wrong scan

The new branch tests `/api/index/status`'s `scanning`, which is true for
**any** scan anywhere on the machine. But `reason === ""` means "covered
**and** nothing is scanning *this root*" — `_scan_in_flight(cfg, root)`
applies `_covers` in both directions (`routers/index.py` ~:650, and
`_rank_reason` returns `"scanning"` at :733). So an unrelated scan of an
unrelated root makes a genuinely-empty result claim a build is in progress.

**Do not fix this the way a naive reading suggests** — i.e. do not switch the
condition to `reason === "scanning"`. `reason` is frozen at rank time, which
is precisely why the live poll was given authority in both directions
(`indexGap`). Instead gate the branch on **our own scan request having
actually started**: the `started` field of the `requestFolderScan` reply.
That is the only signal that means "a scan we asked for, for this root, is
running", and it closes finding 3 at the same time.

## 3 — MEDIUM: `started` is dropped, so the note can claim a refused build

`requestFolderScan` resolves with a `FolderScanRequest` whose refusals are
durable and expected (`refused` / `debounced` / `joined`). The covered-case
call ignores it, so the UI announces a build for a scan that was refused.
The existing uncovered-case call already handles this —
`useListingSearch.ts:440`, `if (!r.started) setPolling(false)`. Match it.

## 4 — MEDIUM: the new reply handler has no epoch guard

Its sibling twelve lines above guards with
`if (sourceEpoch.current !== epoch) return;` (`useListingSearch.ts:439` and
`:443`). The new covered-case handler does not, so a reply that lands after
the user has navigated or retyped applies to the wrong query.

## 5 — MEDIUM: the two fire-once guards reset on different conditions

`useListingSearch.ts:338` clears `firedEmptyScan` on `[fsPath, pinned]`;
`FilesHome.tsx` clears it on `[home]` alone. The two implementations of one
feature therefore disagree about when a retry is allowed. Pick one rule,
apply it to both, and note in a comment why that rule is the right one.
`SPEC-empty-search-scan.md` asks for "at most once per distinct trimmed
query string" — neither implementation does that; decide whether to
implement it as specified or record why per-path is better.

## 6 — MEDIUM/LOW: the covered branch reads a possibly-stale answer

It reads `displayAnswer`, which on a failed request can still hold a
**previous** query's answer. Zero hits in a held answer is not evidence
about the current query. Test `answer` instead, or gate on `failure === ""`.

## 7 — LOW: the spec citation now resolves

Roughly eight shipped code comments cite `SPEC-empty-search-scan.md`, which
was untracked when they were written. It was committed in `219518870`, so
this finding is already resolved — **verify** that the cited filename in
every comment matches the committed path exactly, then treat this as closed.

## 8 — LOW: a test passes with the feature deleted

The "a thrown fetch is silent" test passes even with the trigger removed,
because the stub rejects before anything is pushed to `folderScanCalls`.
Make it prove the call happened *and* that the rejection was swallowed —
otherwise it is one of this repo's known worthless tests.

## What the reviewer explicitly cleared — do not re-litigate

* no render loop on the re-query path;
* no double-fire against the pre-existing uncovered scan (`nextStep` returns
  `"scan"` only for `"uncovered"`);
* the source greps in `search-mode-chip.test.ts` still resolve.

## Testing rules

* Every item above is logic, so every item gets a test. (The CSS-only
  exemption does not apply to this round.)
* **`bun mock.module` is process-wide.** One file's stub breaks unrelated
  files in a full `bun test`, and a filtered run is structurally blind to it.
  A *complete* stub is worse than a narrow one. Local-green/CI-red is this
  bug, not a flake.
* **The local frontend baseline is not CI's.** Do not conclude "pre-existing"
  from a local run alone.
* Some **pytest** tests assert on literal frontend source lines. After every
  frontend edit, grep `tests/` for any symbol or line you removed or renamed.
* Verify each fix against a **pre-existing** answer object, not only a
  freshly created one — a create-path-only hook once shipped dead for every
  real object while the suite stayed green.
* A headless test cannot see layout. If any fix turns out to depend on
  geometry, say so in your report rather than claiming it verified.
* Run **only the touched test files** while iterating. The orchestrator runs
  the full suite once at the end.

## Working rules

* Worktree `/Users/iamsdas/Work/fused-render/.claude/worktrees/index-live-watch`,
  branch `index-live-watch`, PR #1279. Fold in — no second branch, no
  stacked PR. Never `cd` to the main checkout. Never bare `git stash`.
* `scripts/dev.sh` runs on port 1931, started by the user. Do not start,
  stop, or poke a dev server.
* Use `bun` / `bunx`, never `npm` / `npx`.
* Commit per finding or per tight group; push each commit immediately.
* Append to `DECISIONS.md` — **append-only**, never overwrite.
* Report a to-verify list for anything you could not check, and expect the
  rendered appearance of the "still building" copy in both search boxes to
  be on it: that one needs a human looking at a browser.
