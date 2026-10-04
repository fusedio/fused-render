# SPEC: stop the watcher putting the machine in a permanent scan loop

## The bug, measured

Measured on the user's own dev server (branch state dir
`~/.fused-render/branches/index-live-w/index/runs`, 94 consecutive runs):

* root is `/Users/iamsdas` — the **whole home** — on every single run,
  `full: false`, `fsevents: true`;
* start-to-start interval pinned at **~30 s** (= `WATCH_FLUSH_FLOOR_S`);
* durations 12.4 s / 14.6 s / 29.2 s / 31.3 s → duty cycle ~40–100 %. The
  machine is almost always scanning;
* `changed_dirs` 19–46 out of `dirs: 79188`; `unchanged_dirs: 79123`;
  `reused_files: 651282`;
* `rows: 655166`, `partitions: 2`, **`skipped_rewrite: false` every run**;
* the `writing index` → `writing signatures` stretch is 12–18 s of every run.

That is the user's report — "every time I do an alt tab, it starts a new
indexing process which goes on for 10 sec" — and it is not about alt-tab at
all; it is a continuous loop. Two facts compound to produce it:

1. **Root escalation.** The watcher reduces changed paths to their parent
   folder. A write to a file sitting *directly in* `~` yields the folder `~`,
   so the flush asks for a scan of `~`, which is a 79,188-dir recursive walk
   plus a 5–17 s fsevents journal replay — to find ~20 changed dirs it
   already knew about.
2. **Whole-store compaction.** `store.py:521` skips the rewrite only when
   **nothing at all** changed. 19 changed dirs costs exactly the same
   655k-row rewrite as 79,188 would. The journal narrowing works perfectly
   and then the compaction throws every bit of the saving away.

Each fix alone leaves the loop viable-but-bad: the first without the second
still rescans 79k dirs to find 20 changes; the second without the first still
compacts 655k rows on every unrelated flush. Build **both**.

## Do not "fix" this by deprioritising root-level changes

The obvious-looking fix — ignore or rate-limit changes to files sitting
directly in a watched root — **breaks the feature this branch exists for.**
The user's canonical acceptance test is creating `~/a.txt` and searching for
it. That file is a root-level file. A root-level write must stay *fast*, not
become *ignored*.

So the goal for part 2 is "make a root-level change **cheap** to index", not
"stop reacting to it".

## Part 1 — a small changed set must not rewrite the whole store

`fused_render/index/store.py`. Map of the territory:

| line | thing |
|---|---|
| 389 | `compact` |
| 442 | `_compact_locked` |
| 456 | the `outside` predicate |
| 465 | `has_shards` |
| 471 | `n_new_dirs` |
| 479 | `changed, added, removed = 0, 0, 0` |
| 504 | `root_totals(src)` |
| 521 | the existing nothing-changed skip |
| 531 | `phase("writing index")` |
| 544 | `if has_shards:` |
| 596 | `phase("writing signatures")` |
| 634 | normal return |
| 638 | `_reclaim_partitions` |

The existing skip:

```python
# nothing changed anywhere -> keep the existing index untouched
if not has_shards and n_new_dirs == 0 and removed == 0 and has_old:
    ...
    return {..., "skipped_rewrite": True, **root_totals(old_src)}
```

Add a **partial merge**: when the changed/added/removed directory set is
small relative to the store, write only the affected rows and splice them
into the existing partitions, instead of re-emitting all 655k rows.

`outside` (:456) is already the mechanism that separates "rows under the root
being scanned" from "rows that must be preserved untouched". A partial merge
is the same idea at finer grain: preserve everything not under a changed dir.
Reuse that predicate's shape rather than inventing a second notion of
"untouched".

Non-negotiable constraints:

* **`store.py` is where index-store correctness lives.** The invariant that
  compaction keeps every row outside the root it is given is load-bearing;
  breaking it silently deletes a user's index.
* Pick the threshold for "small" from something structural (a fraction of
  total dirs, and/or a fraction of total rows), not a magic constant with an
  invented justification. This repo has been burned twice by confident
  magic-number comments that were simply wrong — if you cannot derive it,
  say so in the comment and in `DECISIONS.md` rather than asserting a reason.
* The full-rewrite path stays the fallback for anything that is not clearly
  small, and for `full`/`rescan_all` runs.
* Report it in the summary so it is measurable from the run logs — an
  explicit flag alongside `skipped_rewrite` (e.g. `merged_rewrite`), not a
  silent behaviour change.

**Required test, the one that matters most:** for the same inputs, a partial
merge must produce a store **equivalent to what the full rewrite produces** —
same row set, same signatures, same query answers. Write that as an
equivalence test over a fixture store (build one, mutate a few dirs, then
compare merge-path output against full-rewrite output row for row). A test
that only asserts "it was faster" or "the flag was set" is not acceptable
coverage for this change.

Also cover: a dir whose rows all disappear; a dir added under a previously
empty parent; a changed set at exactly the threshold boundary on both sides;
and that rows outside the changed set are byte-identical afterwards.

## Part 2 — the watcher already knows what changed; let it say so

The scanner **already has** a code path that visits only an explicit set of
directories: the fsevents fast path. `fused_render/index/scan.py:587`:

```python
def _run_fsevents(cfg, rules, guard, root, hint, cache, sink, ev, cancel_flag, ...):
    """The FSEvents fast path: visit ONLY the dirs the OS journal reports and
    account explicitly for everything it didn't (specs/scan-incremental.md §4)."""
    forced, subtrees = hint
```

`hint` is `(forced, subtrees)`: `forced` dirs are visited **non-recursively**,
`subtrees` recursively. `fsevents.hint()` produces it by replaying the OS
journal — 5–17 s of the observed runs, and it returns `None` spuriously.

**The watcher has strictly better information than the journal, for free.**
It observed the exact dirs that changed, in-process, with no replay. So:

Let a caller supply the hint directly, and have the scan use it instead of
calling `fsevents.hint()`.

* `runner.start(cfg, root, full=False)` (`runner.py:127`) gains an optional
  hint parameter; it is serialised into `spec.json` (`runner.py:189`) next to
  `root` / `full` / `ignore_sig` / `config` / `mounts_dir`.
* the worker passes it through to `scan`, where `scan.py:415-484` already
  branches `if hint is not None: summary = _run_fsevents(...)`. A supplied
  hint takes that branch without the journal thread.
* the watcher (`fused_render/server/index_watch.py`, and the folder
  reduction in `index_touch.py`) passes its observed dirs as `forced`, and
  stops collapsing a root-level file change into a recursive scan of the
  root.

Then `~/a.txt` becomes: watcher sees `~` changed → scan visits `~`
non-recursively → a handful of rows merge into the store (part 1). No journal
replay, no 79k-dir walk, no 655k-row rewrite.

Traps to handle explicitly, all real:

* **A supplied hint requires the incremental path.** `scan.py` computes
  `incremental = bool(cache)` and only then `hint = ... if incremental else
  None`. With no dir cache there is nothing to "account for" and a hinted
  scan would silently drop the rest of the store. A hint with no cache must
  fall back to a normal scan, not proceed.
* **The join in `start()` is now unsafe between differently-hinted runs.**
  `runner.py:174` joins a live run of the same root when the ignore sig
  matches and `live["full"] or not full`. A live run hinted with dirs *A*
  cannot answer a request hinted with dirs *B* — joining it loses *B*
  entirely and the caller is told a scan covered them. Handle it (union the
  hints, or decline to join, or supersede) and say in a comment which you
  chose and why. **This is the correctness trap of part 2** — the same class
  of bug the existing `ignore_sig` join check was written to prevent, and
  that docstring explains the shape of it.
* **Only a completed scan advances the fsevents cursor** (`fsevents.save_state`).
  A watcher-hinted run must not stamp the journal cursor forward for events
  it never replayed, or a later journal-based scan will skip real changes.
  Check what the current fsevents path stamps and keep the cursor honest.
* `fsevents` is **macOS-only**. The watcher exists because inotify has no
  history. A supplied hint must work on every platform — it is not an
  fsevents feature, it just reuses that path's walker. Make sure nothing on
  the supplied-hint path requires a saved fsevents state, and that the
  Windows/Linux tests do not depend on it.

## Also in this round, first, so CI unblocks

PR #1279 is `mergeStateStatus: DIRTY`, so **zero CI has run** — "no checks
reported" reads green but means unverified. Before the code work:

1. Merge `origin/main` into this branch. The sole conflict is `DECISIONS.md`,
   where both sides appended. **`DECISIONS.md` is append-only** — a 2891-line
   tracked project log; a previous builder Write-overwrote it. Keep *both*
   sides' entries. Watch for **D-number collisions**: if main took the same
   D-numbers, renumber this branch's entries, because there is a CI test that
   fails on duplicate decision ids and it tests the merge ref, not your
   local HEAD.
2. `git add SPEC-empty-search-scan.md` and commit it. It is untracked, and
   ~8 shipped code comments cite it, so the citations currently resolve to
   nothing for anyone else.
3. Push. Then do the code work.

## Out of scope

* The eight code-review findings on the zero-match search trigger. A separate
  builder gets those — do not touch `useListingSearch.ts`, `FilesHome.tsx`
  or `empty-result.tsx` in this round.
* Tuning `WATCH_FLUSH_FLOOR_S`, `COALESCE_S`, `MUTATION_SCAN_FLOOR_S`,
  `MAX_FOLDERS`, `DEFER_DEADLINE_S` or `SCAN_DEBOUNCE_S`. The loop is a cost
  problem, not a cadence problem; making the interval longer only makes the
  feature slower while leaving the cost intact.
* Identifying what writes to `$HOME` (a known open item on #1279). Claude
  Code's own `~/.claude.json` is a large contributor — 77.9 % of one sample —
  but a machine without it still loops, so this is not the fix.
* Adding `.claude.json` to the default ignore list. `ignore.py`'s docstring
  records why: a user who has pressed Save carries a frozen list, so a
  `default_ignore()` change does not reach them.

## Working rules

* Branch is `index-live-watch`; it carries PR #1279. Fold into that same
  branch and PR — **no second branch, no stacked PR** (squash-merge makes a
  stacked PR report MERGED while its content never reaches main).
* Worktree is `/Users/iamsdas/Work/fused-render/.claude/worktrees/index-live-watch`.
  Work only there. Never `cd` to the main checkout. **Never use bare
  `git stash`** — the stash stack is shared with other worktrees and other
  live sessions; use a WIP commit instead.
* **`scripts/dev.sh` is running on port 1931, started by the user.** Do not
  start, stop, or poke a dev server, not even for a quick perf check.
  Editing `fused_render/` restarts it.
* **A concurrently running dev server contaminates the suite** (other
  worktrees' servers restage `~/.fused-render/.core-templates`, producing
  ~10 teardown errors that look like your diff). And note the scan loop
  itself was contaminating test runs before this fix.
* TDD: test first, watch it fail, implement, watch it pass. Commit per
  logical unit.
* **Run only the touched test files while iterating.** No full suite between
  steps — the orchestrator runs it once at the end.
* Do not put measured numbers in comments unless *you* measured them this
  session. The numbers at the top of this spec were measured by the
  orchestrator from run logs; cite them as such if you cite them.
* End every commit message with
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
* Push each commit immediately — the PR is open.
* Append decisions, dead ends, and anything this spec got wrong to
  `DECISIONS.md` (append-only) before reporting done.
* Report anything you could not verify as a to-verify list rather than
  claiming it verified. In particular you cannot verify the real end-to-end
  `~/a.txt` latency yourself — that is the user's test on their own server.
