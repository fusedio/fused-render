# Status-bar Activity/Notifications merge — decisions log

## Plan
Collapse Models/Engines/Jobs into one "Activity" chip; Notifications stays.

Architecture chosen: move the row-rendering components (`ModelRow`/`MemoryCell`/
`memoryBand`, `EngineRow`/`engineLabel`) from `shell/ModelsDock.tsx` /
`shell/EnginesDock.tsx` into `platform/ui/DownloadManager.tsx`. This is legal
under `check-boundaries.mjs`: those components only need `AiLoadedModel` /
`RunningEngine` types and `unloadAiModel`/`stopEngine` calls, all of which
already live in `platform/lib/api.ts`. Only the *data sources* — `useAiRuntime`
(apps/ai_models) and the running-engines poll — are shell-only, so those stay
in a new `shell/ActivityDock.tsx`, which is now the thing `App.tsx` mounts. It
composes the queue slot (logic moved from `shell/QueueDock.tsx`, which is
deleted), the engines poll (moved from `shell/EnginesDock.tsx`, deleted) and
`useAiRuntime` (formerly `shell/ModelsDock.tsx`, deleted), then renders one
`<DownloadManager queue=... engines=... models=... />`.

`DownloadManager`'s panel now renders up to three labelled sections — Running,
Background tasks, Models — in that order, each shown only when non-empty, with
a heading only when 2+ sections are present. The chip label is "Activity". The
`StatusDot` continues to reflect only Running work (jobs + queued) per the
brief; a muted `.is-idle` still means nothing at all (no jobs, no queue, no
engines, no models).

`useExclusiveSection`'s `SECTION_ORDER` shrinks to `["activity",
"notifications"]`. `useAutoExpandOnNew` for the merged chip is fed job ids as
`ids` (the only thing allowed to auto-open the panel) and queue/engine/model
ids as `alsoDrawn` (count for occupancy, never announce) — matching today's
per-source behaviour (only Jobs ever auto-opened).

## Deviations from the brief
- The brief's "keep `.q-all`, `.q-open`, `.q-spin`, `.q-note` only where no
  `.dl-*` equivalent exists" is read narrowly: `RepoUpdatesDock`'s own repo
  rows migrate `.q-row`/`.q-row-head`/`.q-title`/`.q-status` onto
  `.dl-row`/`.dl-row-head`/`.dl-title`/`.dl-status` (their dismiss ✕ was
  already `.dl-x`). The repo row's primary action button keeps `.q-all`
  rather than switching to `.dl-row-cancel`: `.dl-row-cancel`'s own comment in
  notifications.css documents it as reserved for a specific verb family
  (Unload/Stop/Cancel — "prominent, not alarming", deliberately non-red) which
  Update/Switch/"Fix with Claude" are not, so reusing it would misapply that
  weight. `QueueDock`'s own queue rows (pending/live scheduled messages) are
  NOT touched by this migration — the brief's item 4 names "the repo rows in
  RepoUpdatesDock" specifically, and those rows keep their `.q-open`/`.q-x`/
  `.q-spin`/`.q-note`, none of which the brief lists as things to remove. No
  `.q-*` rule ends up unused, since `QueueDock`'s rows still use the shared
  ones and the repo-only ones (`.q-title`, `.q-row`, `.q-row-head`,
  `.q-status`) are still exercised by `QueueDock`'s own rows even after
  `RepoUpdatesDock` stops using them for its own rows — so nothing was deleted
  from notifications.css.
- `ModelsDock.test.tsx`'s chip-level assertions (a standalone Models chip with
  its own toggle/idle/circle) no longer describe anything real — that chip is
  gone. The file is deleted; its pure-function tests (`memoryBand`) and row
  structure/behavior tests move into `platform/ui/DownloadManager.test.tsx`
  against the new `models` prop.
- `autoExpand.ts`'s `neverOpen` option is deleted, not merely unused: it was
  the flag the two now-deleted standalone Models/Engines chips passed so an
  arrival in `ids` would never open their panel. Nothing calls
  `useAutoExpandOnNew` wanting that shape any more — Activity's engine/model
  rows are fed as `alsoDrawn` instead (occupancy without announcing, which is
  what every remaining caller actually wants) — so per "delete stranded code
  outright" it is removed from the option type, the implementation, and its
  three dedicated tests in `autoExpand.test.tsx`.

## Final status
- Built: `platform/ui/DownloadManager.tsx`'s Activity chip now renders up to
  three labelled sections (Running / Background tasks / Models); `StatusBar`
  and `App.tsx` carry two slots (`activity`, `repoUpdates`); `exclusiveSection`
  and `autoExpand` updated to match; `shell/ActivityDock.tsx` replaces
  `QueueDock.tsx`/`ModelsDock.tsx`/`EnginesDock.tsx` (all three deleted);
  `RepoUpdatesDock`'s own repo rows migrated onto the `.dl-row` family.
- Tests: all scoped suites pass (`DownloadManager.test.tsx` 52,
  `StatusBar.test.tsx` 4, `RepoUpdatesDock.test.tsx` 28,
  `repo-updates-lib.test.ts`, `queue-dock-lib.test.ts`, `sidebar-tasks.test.ts`,
  `autoExpand.test.tsx`, `exclusiveSection.test.tsx`, `JobRow.test.tsx` — 191
  tests total in that group). A full `bun test src` run also passes except one
  pre-existing, unrelated failure in `appCardMenu.test.ts`
  (`window.addEventListener is not a function` inside `appShot.ts`, a file
  this change never touches).
- `bunx tsc --noEmit` is clean. `node scripts/check-boundaries.mjs` passes
  (412 files).

## Revision: Models split back out, panel widths unified (2026-08-31)

User review of the merge asked for two changes.

### 1. Models is its own chip again
Final chip order, left to right: **Models, Activity, Notifications**. The
merge's `platform/ui/DownloadManager.tsx` Models section (`ModelRow`/
`MemoryCell`/`memoryBand`) is deleted from that file; `shell/ModelsDock.tsx`
is resurrected from `git show 33fc407d^:frontend/src/shell/ModelsDock.tsx`
with one adaptation — the merge deleted `autoExpand.ts`'s `neverOpen` option
entirely (nothing else ever used it), so the pre-merge
`useAutoExpandOnNew(ids, collapsed, ready, { neverOpen: true })` call becomes
`useAutoExpandOnNew([], collapsed, ready, { alsoDrawn: [...model ids] })`: an
always-empty `ids` list can never contain an "arrival", so `autoOpen` can
never become true — structurally the same guarantee `neverOpen` used to give,
with no separate flag. Every resident model still rides in as `alsoDrawn`,
which is what keeps D580's "close when the last model unloads" behaviour
working. `shell/ModelsDock.test.tsx` is resurrected the same way from
`git show 33fc407d^:frontend/src/shell/ModelsDock.test.tsx`, unchanged (every
assertion still describes the resurrected component's real behaviour).

Activity keeps jobs (Running) + engines (Background tasks) — Engines was
never asked to stand apart the way Models was, so it stays folded.
`platform/ui/DownloadManager.tsx`'s `ModelsSlot` interface, `ModelRow`,
`MemoryCell`, `memoryBand`, and every model-shaped branch in
`DownloadManagerView` (the `alsoDrawn` concat, `modelCount`, the `idle`
predicate, the `Models` section JSX) are deleted outright, not left inert.
`shell/ActivityDock.tsx` drops `useAiRuntime`/`publishAiRuntime`/
`unloadAiModel` and the `models` prop it used to hand `<DownloadManager>`.
`platform/lib/exclusiveSection.ts`'s `SECTION_ORDER` becomes `["models",
"activity", "notifications"]`. `platform/ui/StatusBar.tsx` regains a `models`
prop, rendered first; `shell/App.tsx` imports `ModelsDock` and passes
`models={<ModelsDock />}`.

`DownloadManager.test.tsx`'s Models describe block (the one moved off
`ModelsDock.test.tsx` during the merge) is deleted along with the code it
exercised; the "three sections" describe block becomes "two sections" with
its model references replaced by a second engine-based case, and its
neverOpen-contract test now exercises an arriving engine instead of a model.
`StatusBar.test.tsx` gains a `models` chip in its composition test.

### 2. One shared panel width for every non-empty panel
`notifications.css`'s `.dl-panel` used to be `width: max-content; max-width:
min(340px, calc(100vw - 32px))` unconditionally (D608/D610) — every panel hugs
its own content, so Models/Activity/Notifications visibly popped open at
different widths. New rule, added right after `.dl-panel`:
```css
.dl-panel:has(.dl-rows) {
  width: min(340px, calc(100vw - 32px));
}
```
`.dl-rows` is the wrapper every non-empty section already renders
(`DownloadManagerView`'s Running/Background-tasks lists, `ModelsCardView`'s
row list, `RepoUpdatesCardView`'s row list) and nothing else does, so
`:has(.dl-rows)` is a reliable "this panel has rows" signal with no class to
keep in sync from JS — the codebase already uses `:has()` this way elsewhere
(`tasks.css`'s `.tasks-row:has(.tasks-act:focus-visible)`). An empty panel
(`.dl-panel-empty` alone) is untouched and still hugs its one sentence via the
base rule's `max-content`, exactly as D608/D610 left it.

The stale D608/D610 history in `.dl-panel`'s own comment (arguing for
`max-content` specifically so panels DON'T share a width) is rewritten to
describe this as round 3 of that history rather than silently contradicting
it. Two downstream comments that documented the old "bar-and-figures panels
keep width via `.dl-row`'s own 238px floor, `.q-row` panels hug" distinction
(`.dl-row`'s own comment, and `.dl-x`'s "THAT ZERO-SLACK CASE IS NOW THE
COMMON ONE" paragraph) are rewritten to say the `.dl-row` floor is now a
harmless belt-and-suspenders minimum rather than the mechanism that actually
sets a non-empty panel's width — the 238px floor is left in place rather than
deleted (a floor that costs nothing and might still matter for a hypothetical
`.dl-row` outside a `:has(.dl-rows)` panel is not the same "stranded code"
case as a whole feature's dead branches).

### Tests / verification
`shell/ModelsDock.test.tsx` (25 tests) and `platform/ui/DownloadManager.test.tsx`
+ `StatusBar.test.tsx` + `platform/lib/exclusiveSection.test.tsx` +
`autoExpand.test.tsx` (58 tests) pass on their own. A full `bun test src`
(with `TMPDIR` pointed off the sandbox's quota-constrained `/tmp` — this
sandbox's Bash tool went fully unresponsive for a stretch mid-session,
disk-quota related, the same local-env flakiness already on record, but
recovered) confirms the wider picture: 2909 pass, 1 pre-existing unrelated
failure (`appCardMenu.test.ts` / `appShot.ts`'s `window.addEventListener is
not a function`, a file this change never touches — the same failure the
merge's own DECISIONS.md entry recorded), across 2910 tests in 123 files.
`bunx tsc --noEmit` is clean. `node scripts/check-boundaries.mjs` passes (414
files).
