# Change detection on home-page focus

## The bug

Home search is index-backed and global. Download a file to `~/Downloads`, go to the
home page, search for it — it is not there, and stays missing until something
happens to scan that folder.

Every existing trigger misses this case:

* **Startup scheduler** — once per boot, debounced by `SCAN_DEBOUNCE_S` (~5 min).
* **Folder-open freshness** (`index/freshness.py`, fired from `/api/fs/list` via
  `routers/fs_read.py:253`) — checks the folder *being listed*. Home search lists
  no folder.
* **`index_touch`** (`server/index_touch.py`) — only for mutations this app makes.
  A browser download is out of band.
* **Repos tab** (`routers/git_repos.py:165`, `_note_tab_opened`) — the closest
  precedent, and its docstring names why it does not help here:

  > a root's own mtime moves only when its DIRECT entries change, so a repo
  > cloned three levels down does not make the root look stale and is not
  > detected here

  Scan roots default to the server's `start_dir`, i.e. `~` (`index/config.py:54`).
  A file landing in `~/Downloads` moves `~/Downloads`'s mtime, **not** `~`'s. So
  the mtime check that backs `note_folder_opened` is structurally blind to it.

## The fix

The macOS FSEvents journal records changes **at any depth**, which is exactly the
property the mtime check lacks. `index/fsevents.py:hint()` replays it. Today that
replay only ever runs *inside* a scan (`index/scan.py:426`, the
`"checking for changes"` phase). Run it standalone, on home-page focus, and only
start a scan when it reports something.

Confirmed properties of `hint(cfg, root)` — all verified in the source, do not
re-litigate them:

* **Read-only.** It reads `fsevents.json`, calls `device_uuid`, and replays.
  `fsevents.save_state` is called from `index/scan.py:480` and `:577` and nowhere
  else in the codebase. So a standalone call does not consume the journal
  position: it always measures from the last successful scan of that root, and
  calling it fifty times is the same as calling it once.
* **It distinguishes "quiet" from "cannot tell."** `(set(), [])` means the journal
  reported nothing under this root. `None` means unusable.
* **`None` is ambiguous and must never be read as "quiet."** It is returned for
  non-darwin, no saved event id, a multi-volume root, a UUID mismatch, dropped or
  purged history, a ctypes failure, a 20s timeout, **and a change set of
  ≥200_000 events** (`_replay`'s `max_events` bail). That last one is precisely a
  huge download. Treat `None` as "no answer", never as "nothing changed".
* **It can raise.** `hint` does `int(st["event_id"])` with no guard of its own —
  `index/scan.py` says so explicitly and deliberately lets it raise into the run's
  failure handler. This new caller has no run to fail into and MUST wrap it.

## Decisions already taken (do not revisit)

1. **Home page only.** Not app-wide focus. Other surfaces keep the existing
   `/api/fs/list` freshness path.
2. **macOS-only accelerator.** Off darwin `hint` returns `None`, and on this path
   `None` is a no-op. Linux/Windows keep exactly today's behaviour. Do **not** add
   a fallback that starts a scan on focus — that was considered and rejected as a
   real recurring cost on platforms where nothing scales with the window since
   the last scan.
3. **Gate on hidden duration, not on every focus.** The signal is "the user went
   away and did something else", not "the user alt-tabbed between our own
   windows".

## Errata (code review, fix round)

This spec's "Behaviour, per configured scan root" list said "Non-empty ->
start the ordinary incremental scan of that root" and left it there. That is
wrong on a real `~` root: `fsevents.hint()` applies NO ignore-rule filtering
of its own (`fsevents.py`'s docstring only ever promises it prefix-filters by
root), so its raw output on a home root routinely includes noise nobody
would call a "change" — this app's own writes under its state home
(`~/.fused-render/**`, previously not excluded from a raw hint at all) and,
on macOS, constant unrelated churn under `~/Library` (Safari/Mail caches,
saved app state, Spotlight — not previously in `index/ignore.py`'s
`DEFAULT_IGNORE_NAMES` either). Collapsing an unfiltered hint straight to a
boolean, as originally specced, made the design's own stated quiet case —
`(set(), [])`, no scan — effectively unreachable on a real home root: one of
these two sources fires on very nearly every check.

The fix (`index/detect.py`'s `_filter_hint`) runs the SAME per-path filter
`scan._run_fsevents` already applies to a journal-driven hint
(`ignored_for_index(rules, p, tree=True) or guard.blocks(p) or
is_inside_leaf_dir(p)`) before deciding whether anything changed. **This
paragraph originally continued: "and `index/ignore.py`'s `default_ignore()`
now also names `~/Library`" — that part was corrected in the final fix
round, see below.** A change under `ignored_for_index`/`guard.blocks`/
`is_inside_leaf_dir` is correctly still invisible to `hint()`'s only OTHER
caller (`scan.py`'s incremental walk skips them for the same reason a scan
would never want to spend index rows on them), so filtering them out of
THIS caller's boolean is consistent, not a new exemption invented for this
trigger alone.

Two more behavioural gates were missing entirely (code review, not a spec
error): a live run of the root must never be superseded by a focus event
(`runner.active_run` guard, mirroring `freshness.note_folder_opened`), and
`runner.start` joining an already-running scan must not be reported as
"started" by this trigger (it would mislead the caller's log and wake the
Activity card for a scan this trigger had no hand in). See DECISIONS.md.

## Errata (final fix round): `~/Library` moved OUT of `default_ignore()`

The code-review round above added `~/Library` to `index/ignore.py`'s
`default_ignore()` specifically so `_filter_hint` would drop macOS's
constant `~/Library` churn from a raw hint. That fix put the trigger's
correctness in the wrong place: `default_ignore()` feeds `IndexConfig.ignore`
(`index/config.py:49`), which is USER-EDITABLE and, once a user has ever
pressed Save in the Indexing preferences panel, FROZEN to whatever
`default_ignore()` returned at save time (`index/config.py:151-155` writes
it verbatim; `frontend/src/shell/Indexing.tsx:160,187` is what saves it).
Anyone who saved before this entry existed keeps a snapshot without it
forever, and `_filter_hint`'s noise-dropping — hence the whole quiet path —
silently degrades back into "scan on nearly every focus event" for exactly
that population. A trigger's own correctness must never be able to be
broken by a user editing an unrelated preference.

The final fix round therefore:

* Reverted the `~/Library` entry from `default_ignore()`. It was also too
  broad a DEFAULT on its own terms — `~/Library` holds `Application
  Support`, `Mail` and `Fonts`, content a user may legitimately want home
  search to reach — independent of the editability problem above.
* Gave `index/detect.py` its own small, non-editable noise list
  (`_NOISE_HOME_SUFFIXES` / `_os_noise_roots()` / `_is_os_noise()`),
  consulted directly by `_filter_hint` alongside `ignored_for_index`/
  `guard.blocks`/`is_inside_leaf_dir`. The trigger now answers "is this
  journal entry noise?" entirely from its own authority, never from
  `cfg.rules`.
* Separately, and for an unrelated reason (requested, not a correctness
  fix), added `~/Library/Caches` to `default_ignore()` as its own PATH
  pattern — `~/Library/Caches` is never searchable content, same rationale
  as `.cache`. This does change `IgnoreRules.sig()` and forces a full
  rescan on upgrade for anyone without a saved custom list; see
  DECISIONS.md for why that is accepted here.

See DECISIONS.md ("Final fix round") for the full account, including the
unrelated Windows CI regression this round also fixed.

## Errata (2026-09-19): the core premise — "standalone replay makes the quiet
## path cheap" — is wrong on a real home root, and the design changed

Everything above this section describes and repeatedly patches the
STANDALONE-REPLAY design ("The fix" §, above): replay `fsevents.hint`
by itself on focus, only start a scan when it reports a change. That
design's central claim — that the quiet path is cheap because a replay is
"0.1-2.9s of the journal" — was measured against a root scanned recently.
It does not hold on a root that has gone quiet for hours, which is exactly
the situation this feature exists to help with.

Measured on this machine's real `~` (2026-09-19, do not re-litigate):
`fsevents._replay` gives up and returns `None` past a 20s timeout or a
200,000-event cap. Right after a scan, a check cost 48ms for 123 events.
7.2 hours after the last scan, it cost 11.9s for 199,943 events — sitting on
the cap — and a call in the same minute returned `None` at 5.8s having
answered nothing at all. `~` generates roughly 28,000 FSEvents/hour, so the
replay stops being able to answer after about 7 hours without a scan. Since
`None` must be treated as a no-op (every round of errata above insists on
this, correctly), the design's actual behaviour on a real home root was:
the longer since the last scan — exactly when a new download is most likely
to be missing — the more certain the feature was to burn 6-12s of
background work and then do nothing. No amount of noise-filtering
(the two errata rounds above) fixes this: they were both about the
CONTENT of a successful replay, and this failure mode is about the replay
not completing at all.

The fix is not another patch to the standalone-replay design; it removes
the standalone replay. `index/detect.py` no longer calls `fsevents.hint`
itself. On a qualifying focus event it now just starts the ORDINARY
incremental scan (`runner.start`) of a stale-enough root — `run_scan`
already tries the same journal replay internally, racing the dir-cache
read, and when it can't answer, falls back to its own cache-shortcut walk
(one `scandir` + mtime compare per directory, no per-file rehash for
anything unchanged) rather than to "no answer". That fallback was measured
directly: forcing the `None` case against a same-day copy of this
machine's real production cache for `/Users/iamsdas` (~78,700 directories),
the walk took 4.4s wall-clock — bounded by directory count, not event
count, and it always leaves the index actually current. See DECISIONS.md
("2026-09-19 — standalone replay removed") for the full measurement and the
new design.

Everything in "Decisions already taken", "The fix", and both prior errata
sections above should now be read as history explaining how the feature
got to this point — including WHY the noise-filtering and ordering fixes
they describe (MountGuard-before-syscall, the `active_run` guard, the
already-running check) were correct — not as the current design.
`index/detect.py`'s own module docstring is the current source of truth.

## What to build

### Server

New module `fused_render/index/detect.py`, holding the policy — mirroring how
`index/freshness.py` holds the folder-open policy and `routers/index.py` holds
only the wiring. Do not add a third policy inline in the router.

Behaviour, per configured scan root:

* Refuse unless the root is past a per-root floor, `DETECT_INTERVAL_S = 30.0`.
  Keep this state in the module the way `routers/index._freshness_checked` does
  (bounded by root count, no eviction needed). Document the constant the way this
  codebase documents its siblings — what it trades off, and against which other
  constant.
* Call `fsevents.hint(cfg, root)` inside a `try/except`. Any exception: log at
  debug, treat as `None`, never propagate. Housekeeping must never become the
  answer (same rule `git_repos._note_tab_opened` follows).
* `None` → no-op. `(set(), [])` → no-op. Non-empty → start the ordinary
  incremental scan of that root.
* The scan start must honour every gate the other triggers honour: the indexing
  pref being off, the macOS Full Disk Access gate (`shell/index_gate.py`), the
  `MountGuard` / never-a-mount / never-`/` refusals (`index/ignore.py`), and
  `freshness.MIN_INTERVAL_S` read off `scans.json` via `runner.last_scan`. Reuse
  the existing helpers for these rather than re-deriving them — `freshness.py` is
  the model for what a trigger is allowed to skip.
* Set `routers/index._index_job_wake` after starting a run, so the Activity card
  ticks immediately instead of waiting out `INDEX_JOB_IDLE_S` — every other scan
  starter does this.

New endpoint in `fused_render/server/routers/index.py`. Follow the file's own
conventions for the guard (`_require_fused`) and for running work off the event
loop; match what the neighbouring scan endpoints do rather than inventing a
shape. It takes the client's hidden-duration and must not block the request on
the replay — fire-and-forget on a thread, like `note_folder_opened` does
(it "never raises and never blocks", and the caller pays one lock acquire).

Enforce the minimum hidden duration **server-side** (`MIN_HIDDEN_S = 30.0`); the
client value is an input, not a decision.

### Frontend

* Put the pure decision — "given hidden-since timestamp and now, should this
  fire?" — in its own module with `bun test` coverage, following the
  `shell/indexing-lib.ts` / `apps/explorer/lib/home-search.ts` precedent of
  keeping the testable half out of the component.
* Wire it into the home surface (`frontend/src/apps/explorer/FilesHome.tsx`)
  with BOTH a `visibilitychange` listener and a `window` `blur`/`focus` pair
  — not `visibilitychange` alone (code review: fixed after the first build).
  `visibilitychange` alone misses the documented motivating scenario on the
  packaged desktop app: switching to a DIFFERENT application while the
  window stays visible fires neither `visibilitychange` (the document never
  becomes hidden) nor a plain blur-on-hide check, only `document.hasFocus()`
  going false, which only `window`'s own `blur`/`focus` observe. Track "away"
  as `document.hidden || !document.hasFocus()`, not per-event edges, so
  whichever of the three events fires first on the way out/back is the one
  that acts — one away/back transition must still fire the endpoint exactly
  once no matter how many of the three events it raises along the way. Do
  not fire on mount.
* Add the client call to `frontend/src/platform/lib/api.ts` alongside the other
  index calls.
* Failures are silent, the way `index-status.ts`'s poll failure is silent: this
  is an accelerator, and a failed detect is never something the user can act on.

### Why this shape is cheap

The quiet path costs a journal replay (0.1–2.9s, scaling with the window since
the last scan — and focus events make that window short) and starts no scan. No
scan means `last_completed_at` does not move, so no `noteIndexLifecycle`
(`platform/lib/index-freshness.ts`), no corpus invalidation, and no "indexing…"
caption flicker on home. That is the whole reason detection runs standalone here
instead of just starting an incremental scan on focus.

Accept one known cost: on the changed path the scan replays the journal a second
time (it captures `fs_id0` before reading anything, so its own replay is the
authoritative one). Do **not** thread the hint through `spec.json` to avoid it —
the worker "re-derives nothing from the environment" by design.

## Tests

Python — new `tests/test_index_detect.py`:

* `hint` returns `None` → no scan started.
* `hint` returns `(set(), [])` → no scan started. This is the case the whole
  design exists for; assert it explicitly.
* `hint` returns a non-empty change set → a scan is started for that root.
* `hint` raises → no scan, no exception escapes.
* Per-root `DETECT_INTERVAL_S` floor refuses a second call inside the window.
* `freshness.MIN_INTERVAL_S` still refuses when a scan just ran.
* Indexing pref off → no-op.
* Hidden duration below `MIN_HIDDEN_S` → no-op.

Frontend — `bun test` for the pure gate module.

Look at `tests/test_index_freshness.py` first and follow its fixtures and its
style of asserting on the trigger rather than on a real scan.

## House rules for this build

* **Scoped tests only in your inner loop.** `pytest tests/test_index_detect.py`,
  `pytest tests/test_index_freshness.py`, and a filtered `bun test` for the
  frontend module. Do **not** run the full suite — the orchestrator runs it once
  at the end. The macOS local baseline is ~19 red for reasons unrelated to this
  branch, so a full run would tell you nothing anyway.
* **Never start a dev server.** Not `dev.sh`, not a hand-rolled uvicorn, not for
  a "quick check". Other worktrees' servers restage shared state and contaminate
  test runs.
* Set the worktree up first with the repo's own `setting-up-dev-env` skill — a
  fresh worktree silently tests the main checkout otherwise.
* Some tests in `tests/` assert against literal frontend source lines. If you
  rename or delete a frontend symbol, `grep tests/` for it before you finish.
* Commit per logical unit with clear messages. Do not squash.
* Record decisions, dead ends, and anything this spec got wrong in
  `DECISIONS.md` in the worktree root as you go.
