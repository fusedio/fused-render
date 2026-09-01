# Notes for a follow-up builder — merge-model-load-row

Scratch notes beside DECISIONS.md's D627 entry (which has the actual design).
This file is just the things that would otherwise only live in my head:
deviations from the brief, dead ends, and loose ends worth knowing about.

## Deviations from the brief

- The brief's suggested `_report(job, **(row or {}), waiting_for="", ...)`
  shape for the clearing tick (and the per-tick merge) would raise `TypeError:
  got multiple values for keyword argument 'unit'` whenever `row` itself
  carries `unit` (it does for a transcription row — `transcribe_row_fields`
  pins `unit: "s"`). Built a plain dict (`row` spread first, then the
  overrides) and called `_report(job, **tick)` / `_report(job, **final)`
  instead. Same override order the brief asked for, just constructed so it
  does not blow up on a caller row that already has the keys being
  overridden.

- `_job_record` ended up taking an optional `records` param
  (`_job_record(job_id, records=None)`) rather than being a bare
  `job_id -> dict | None` helper, so `_wait_ready`'s tick can fetch
  `jobs.list_jobs()` ONCE and hand the same list to both `_cancel_state`
  (checking the caller's row) and the load-row lookup — that's what "one
  scan per tick" in the brief actually required, since the two lookups are
  for two DIFFERENT ids (the caller's `job` and the load's `started["jobId"]`)
  and can't share a result any other way.

## A pre-existing test that needed updating (not just new tests)

`tests/test_ai_runtime.py::test_the_WAIT_FOR_A_COLD_MODEL_can_rebuild_an_evicted_row`
asserted `"Waiting for" in row["detail"]` while the wait was still live, and
`row["unit"] == "s"` at that same instant. Both are now false by design:
`detail` no longer carries a "Waiting for ..." prefix (it's the load's own
line, verbatim), and `unit` is MIRRORED from the load row while the merge
holds — for a transcription that's `""` (the load hasn't reported anything
byte-shaped yet at "Starting the model process…"), not the transcription's
own `"s"`. Updated the test to check `waiting_for` truthiness instead of the
detail substring, and moved the `unit == "s"` assertion to after the wait
ends (`_wait_job`'s return), where the finally-block restores it. Worth
grepping the rest of the suite for the same "Waiting for" assumption if this
area gets touched again — `test_a_download_waiting_on_someone_elses_env_build_says_so`
also has a "Waiting for" assertion but it's the ENV-BUILD-JOIN detail
(`_JOINED_INSTALL_DETAIL`, a completely different code path — `load(...,
weights_only=True)` joining someone else's `uv sync`, not `_wait_ready`) and
does not need to change.

## Frontend fallout from adding a required Job field

Adding `waiting_for: string` to the `Job` interface (not optional) broke
`tsc --noEmit` in five OTHER test files that build a full `Job` object by
hand rather than through a factory with defaults:
`src/apps/ai_models/playground/client.test.ts`,
`src/apps/ai_models/shared/modelSize.test.ts`, `src/platform/ui/JobRow.test.tsx`,
`src/shell/RepoUpdatesDock.test.tsx`, `src/shell/queue-dock-lib.test.ts` (this
last one used `as Job` so it wasn't a tsc error, but it's now inconsistent
with everything else and I fixed it too). All five just needed
`waiting_for: ""` added to their base fixture. Worth remembering: any FUTURE
required field on `Job` will hit the same five files (plus whatever new ones
exist by then) — a `Partial<Job>`-plus-defaults factory would make this a
one-line change instead of five, but that's a pre-existing pattern issue, not
something this change should have taken on.

## Grep finding for "other renderers of the same job list" (per the brief's step 4)

`shell/App.tsx`'s failed-jobs dock (`RepoUpdatesDock` via `ActivityDock`'s
`onFailed`) reads `failedJobs(next)` off `DownloadManager`'s own
`onJobs`-forwarded FULL snapshot (`shell/ActivityDock.tsx` ~line 289), which
is independent of `jobRows`/`inFlightJobs`/`mergedRows` entirely —
`failedJobs` only filters on `state === "error"`. Left untouched on purpose:
a failed waiter and a failed load are two DIFFERENT failures (D266) and both
have to reach Notifications, so `mergedRows` must not run upstream of that
path. No other caller of `jobRows`/`inFlightJobs`/`failedJobs` exists outside
`DownloadManager.tsx` and `ActivityDock.tsx`.

## Test-only heads-up

`DownloadManager.test.tsx`'s "one row while waiting" test had to explicitly
set `done: null, total: null` on its `BASE`-derived fixtures — `BASE` in that
file carries `done: 28, total: 28`, which `jobAmount` renders as a
"· 28 / 28" suffix on the status line that the assertion wasn't expecting.
Not a bug, just a fixture-reuse trap worth knowing about if this test file
gets extended.
