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
