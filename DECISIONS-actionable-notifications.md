# Decisions — SPEC-actionable-notifications.md

Running log, written as the build happens. A later builder resumes from this
file, not from the session that wrote it.

## Frontend, unit 1 (committed 5b26a5f4e)

- `AttentionRow.href` narrowed from `string | null` to `string`. Before this
  spec, a waiting task with no folder drew inert text (`href: null`) because
  there was nowhere to send it. `attentionRows()` now falls back to `/tasks`:
  `taskHref(task) ?? folderHref(task) ?? "/tasks"`. With every row guaranteed
  a real destination, `AttentionRowView`'s dead null-href branch in
  `RepoUpdatesDock.tsx` was deleted outright rather than kept defensively —
  correctness-over-compatibility: nothing exercises that branch anymore, and
  a defensive branch nothing exercises is a lie about what the code does.

- LAN pairing rows (`PairingRowView`) gained a `rowClick`: navigate to
  `/preferences?tab=lan`, then `void dismiss()` — the same "clicking
  navigates and clears the row" rule as everything else in the panel, with
  no failure/cancellation carve-out because pairings have no such states.

- Repo-update rows are unchanged per spec (buttons only, no whole-row click)
  — no code needed touching there at all.

## Frontend, unit 2 (committed 45ad8c35f)

- `Job.page`'s meaning widened in its doc comment: from "the `.html` that
  raised it, attribution only" to "where clicking this row goes" — either an
  absolute fs path or one of a handful of shell routes a few server
  producers name directly.

- `router.ts` gained `navigateToJobPage(page: string)`, the one place that
  tells the two `Job.page` shapes apart:
  - A closed `JOB_PAGE_ROUTES` allowlist (`/ai-models/local`,
    `/ai-models/benchmark`, `/claude-config`, `/preferences`, `/tasks`) —
    anything in it goes through `navigateUrl` untouched.
  - Anything else is treated as an fs path via `navigate()`, directory
    unless it ends in `.htm(l)`.
  - This mirrors the existing `LEGACY_SENTINELS` table pattern in the same
    file (a closed, explicit table beats a heuristic for a small, known set
    of shell destinations) rather than trying to sniff "is this a path or a
    route" from the string's shape.

- `DownloadManager.tsx`'s `JobRow` gained a `rowClick`, gated on
  `canOpen = isTerminal(job) && !!job.page`:
  - Gated on `isTerminal`, not just `page` truthiness, because `JobRow` is
    reused verbatim for in-flight/running jobs (both in this file's own Jobs
    section and via `RepoUpdatesDock.tsx`'s reuse for terminal rows) — a
    running job must never open, only one that has already reached
    Notifications.
  - `done` **dismisses on open**, reusing the exact `dismiss()` the row's own
    ✕ calls — having gone to look is the acknowledgement.
  - `error`/`cancelled` **navigate but do not dismiss** — D663 already fought
    and won the argument against a failure clearing itself on anything short
    of an explicit ✕; a click-to-open that also swept the row away would
    quietly resurrect the old 3s-TTL auto-dismiss problem under a new
    trigger. Verified in `JobRow.test.tsx` via an `onPatch` that throws if
    invoked — proof dismiss's filter-callback path never runs for either
    state.

### Open regression (found via full-suite run, before commit)

Adding `import { navigateToJobPage } from "@platform/lib/router"` to
`DownloadManager.tsx` — a component rendered by shared shell chrome
(`ActivityDock.tsx`, `AppPage.tsx`, `StatusBar.tsx`, etc.) — made `router.ts`
transitively reachable from many test files that previously had zero DOM
dependency. `router.ts` has three lines of module-scope code that
unconditionally read `location`/`window`/`history` (a `rewriteLegacyPath()`
IIFE, `IS_EMBED`, `IS_PREVIEW`/`ancestorIsPreview()`); in bun's test runner
(no DOM by default, one shared process for the whole run), a test file that
imports such a chain without first calling `installDomShim()` crashes with
`ReferenceError: location is not defined` at module-eval time, and because
bun's process is shared, the crash can surface as "Unhandled error between
tests" attributed to a different, unrelated file running nearby.

Full-suite baseline before this unit: 3217 pass, 2 fail, 2 errors (the 2/2
are a pre-existing, unrelated PNG-import issue in
`FilesHome.render.test.tsx`/`shareCard.test.ts`, not this feature's doing).
After adding the `router.ts` import to `DownloadManager.tsx`: 2676 pass, 38
fail, 25 errors.

Two candidate fixes were weighed:
- **(a) Propagate the shim.** Apply the same `installDomShim()` +
  dynamic-import conversion already used for `JobRow.test.tsx` and
  `DownloadManager.test.tsx` to every newly-broken file. This is the
  established codebase convention (see `testDomShim.ts`'s own header comment
  and the precedent in `appCardMenu.test.ts`, `appEntry.test.ts`,
  `router.test.ts`) — `router.ts` already unconditionally read these globals
  at module scope before this feature touched anything, so any file that
  transitively imports it in tests was always required to shim first. This
  feature didn't introduce the requirement, it just added new consumers of
  an existing one.
- **(b) Make the dependency lazy.** Replace `DownloadManager.tsx`'s static
  import with a dynamic `import("@platform/lib/router")` inside the `open`
  closure, so `router.ts` is never evaluated unless a terminal row is
  actually clicked. Rejected: it would make navigation asynchronous, which
  breaks the synchronous click-assertions already written and passing in
  `JobRow.test.tsx` (`pushedUrl(() => act(() => onClick()))`, asserting the
  pushed url immediately with no `await`) — rewriting those to tolerate an
  async open reintroduces exactly the kind of test complexity the codebase's
  own established shim convention exists to avoid, for no correctness
  benefit (in a real browser `location` always exists, so the static import
  is not actually a problem execution-wise — it's purely a test-environment
  gap).

Decision: **(a)**. Applied the `installDomShim()` + dynamic-import
conversion to each implicated file, matching the exact pattern in
`JobRow.test.tsx`/`appCardMenu.test.ts` (a static `import type` kept for
type-only usages, the value import moved to a dynamic `await import(...)`
placed after the `installDomShim()` call — a plain "call the shim before the
import" textually does nothing, because static ES imports are hoisted above
all other top-level code). `RepoUpdatesDock.test.tsx` was left untouched: it
already has its own hand-rolled `location`/`window`/`history` setup/teardown
with an explicit rationale in its own header comment for not using
`installDomShim()` or `mock.module` (a real, unfrozen `router.ts` every
other file can also safely import alongside it, learned the hard way from
an earlier `mock.module` attempt that broke two unrelated files).

The 38-fail/25-error number above was the raw symptom, not the true fix
scope. Bun runs every test file in one shared process, so one file's
uncaught module-eval crash can cascade into "Unhandled error between tests"
blocks misattributed to other, unrelated files running nearby in the same
invocation — the ~24-file failure list this looked like at first was mostly
that misattribution. A dispatched investigation ran files standalone
(one-at-a-time bun invocations) to separate real dependency chains from
collateral noise, and found only **three** files with a genuine, unshimmed
path reaching `router.ts`:

- `frontend/src/shell/ActivityDock.test.tsx` — renders `DownloadManager`'s
  `JobRow` directly.
- `frontend/src/platform/ui/StatusBar.test.tsx` — its bare-`DownloadManager`
  fallback path pulls the same chain in.
- `frontend/src/apps/ai_models/playground/appSeed.test.ts` — reaches
  `params.ts`'s `readParam`, which imports `router.ts` for `replaceSearch`
  (the same reason `params.test.ts` already carries this shim).

Each got the same `installDomShim()` + dynamic-import treatment, verified
standalone, then the full suite was re-run and landed back at baseline: 3226
pass, 2 fail, 2 errors (same pre-existing PNG-import 2/2; the higher pass
count is this feature's own new tests). `bunx tsc --noEmit -p .` and `node
scripts/check-boundaries.mjs` (485 files) both clean. Committed as its own
unit in **45ad8c35f**.

## Producer→destination table (backend)

Settled, from the spec:

| Producer | Job(s) | `page` |
|---|---|---|
| `ai/supervisor.py` | `sys:ai-model:*` | `/ai-models/local` |
| `ai/benchmark.py` | benchmark job | `/ai-models/benchmark` |
| `capture/__init__.py` | capture session job | the page that started the capture, threaded from `X-Fused-Page` on `POST /api/capture/start` |
| `claude_install.py` | install job | `/claude-config` |
| `github_setup.py` | `PUBLISH_JOB_ID` | the containment-checked repo root already resolved via `_resolve_repo_root(root)` |
| `github_setup.py` | `JOB_ID` (gh CLI install) | `/preferences` |

The `JOB_ID` (gh CLI install) destination was the least obvious call in the
table, so the reasoning is worth restating: no repo root applies to it,
there is no dedicated GitHub-settings tab to send it to, `/preferences` is
already where the legacy `_account` sentinel in `router.ts` lands, and an
exhaustive grep across the frontend confirmed no UI currently calls any
`/api/github/*` endpoint at all — so this destination is unreachable in
practice today, and `/preferences` is simply the most defensible target
available rather than one validated against any live call site.

`ai/supervisor.py`'s other job-id-prefix families (outside `sys:ai-model:*`)
are deliberately left with `page` unset — the spec's binding table names
only `sys:ai-model:*`, and the spec is binding even where its coverage looks
inconsistent with the rest of that module's job surface.

Status: **done, all 7 steps, across three commits.**
1. `fused_render/jobs.py` — widen `page`'s doc comment. **Recorded as done
   here, but never applied**: `git show --stat 4e91086a4` does not touch
   `fused_render/jobs.py`, and no commit through the merge of `origin/main`
   does either. `Job.page`'s doc comment stayed "the .html that raised it,
   attribution only" through the whole build, materially wrong for every
   server producer that is not a page at all — fixed in the fix-review
   round instead (63a881258).
2. `fused_render/ai/supervisor.py` — `BENCHMARK_JOB_PREFIX` constant,
   `_job_page(job)` helper, `_report` passes `page=_job_page(job)`.
   (4e91086a4) — `tests/test_ai_supervisor_job_page.py` (new, 5 tests).
3. `fused_render/ai/benchmark.py` — replace the two inline
   `jobs.SERVER_ID_PREFIX + "ai-benchmark-"` literals with
   `supervisor.BENCHMARK_JOB_PREFIX`. (4e91086a4)
4. `fused_render/claude_install.py` — `page="/claude-config"` on its
   `jobs.upsert(...)` call. (4e91086a4) — `tests/test_claude_install.py`
   gained `test_the_reported_job_points_at_the_claude_config_page`.
5. `fused_render/github_setup.py` — `page="/preferences"` on
   `_report_install`'s `jobs.upsert(...)`; added a `root` field to
   `_publish_state`, threaded `real_root` through `publish_start`, and
   `page=snapshot.get("root") or ""` on `_report_publish`'s
   `jobs.upsert(...)`. (5cffcceb5) — `tests/test_github_setup.py` gained
   `test_the_reported_install_job_points_at_preferences` and
   `test_the_reported_publish_job_points_at_the_repo_root`.
6. `fused_render/capture/__init__.py` — `page` param on `_Session.__init__`
   and `start()` (keyword-only, default `""`), threaded through, read by
   `_report` as `page=session.page`. (225b8a242)
7. `fused_render/server/routers/capture.py` — `x_fused_page` header param on
   `api_capture_start`, extracted via `unquote` mirroring
   `routers/jobs.py`'s exact pattern (`unquote(x_fused_page) if
   x_fused_page else ""`), passed to `capture.start(mode, body,
   page=page)`. (225b8a242) — `tests/test_capture.py` gained
   `test_the_row_opens_the_page_that_started_the_capture` and
   `test_no_page_header_leaves_the_row_with_no_destination`.

Server-side spoof-proofing (`X-Fused-Page` header only, never request body)
was reconfirmed correct and unchanged in `routers/jobs.py` — no edit needed
there; `routers/capture.py` mirrors the identical channel.

Scoped test runs, all green: `tests/test_ai_supervisor_job_page.py` (5
passed) plus `test_ai_supervisor_video.py`/`test_ai_benchmark.py` combined
(89 passed); `tests/test_claude_install.py` (29 passed);
`tests/test_github_setup.py` (80 passed) plus
`test_server_github.py`/`test_github_login.py` (43 passed);
`tests/test_capture.py` (41 passed, 10 skipped — pre-existing, unrelated);
`tests/test_capture.py`+`test_capture_mixdown.py`+`test_capture_stream.py`
combined (96 passed, 11 skipped).

A local-only wrinkle surfaced and was resolved on the capture unit: any
`TestClient(create_app(...))`-based test fails locally with `RuntimeError:
React shell not built (fused_render/static/shell-dist/ missing)` unless
`frontend/`'s shell has been built at least once in the worktree
(`shell-dist/` is gitignored, so this is a one-time local artifact, not
something to commit). Ran `bun run build` in `frontend/` once to produce it
locally, confirmed via `git check-ignore` that the output directory is
untracked, and then all capture tests ran and passed for real rather than
being reasoned about from the `routers/jobs.py` mirror alone.

## Fix-review round

A code review of the finished build found real gaps. Fixed here, in order.

### Finding 4 — `page` corrupted by whitespace collapse (committed 63a881258)

`upsert` ran every `page` value through `_text(page, PAGE_MAX)`, the same
one-line collapse used for `title`/`detail` — fine for a label, wrong for a
navigation target: `" ".join(text.split())` folds a double space or a
trailing space inside a real path into a string `navigate()` then 404s on
silently. Added `_page_text` — trims only the padding around the whole
value, leaves everything internal untouched — and pointed `upsert` at it.
`github_setup.py`'s repo-root report and `capture/__init__.py`'s
session-page report both go through this same `upsert`, so both are fixed
by the one change; neither needed its own edit.

New test: `test_the_page_header_keeps_internal_whitespace_verbatim`
(`tests/test_jobs_api.py`) — a header value padded on both ends and
carrying a double space inside the path, asserting the double space
survives and only the outer padding is gone. `test_a_page_attributes_its_own_rows_through_the_header`
(the existing single-space fixture) still passes unchanged.

### Finding 5 — the false "step 1 done" claim

`Job.page`'s doc comment was never actually widened despite the record
above claiming commit 4e91086a4 did it — confirmed via `git show --stat`
and a full branch diff, neither touches `fused_render/jobs.py`. Fixed now:
the comment describes the fs-path/route split the server producers
already use, matching its frontend twin (`frontend/src/platform/lib/
jobs.ts`'s own `page` comment). The record above is annotated in place
rather than silently rewritten, so a future reader can see the claim was
wrong and why.

### Finding 2 — four producers the spec's table missed

The "five producers" table undercounted: `sys:env-install:*`, `sys:ai-claude:*`,
`sys:index:*` and `sys:update:*` also reach Notifications, all previously with
`page == ""`. Each now has a destination:

- `sys:env-install:*` (`fused_render/envinstall.py`) — the app folder whose
  environment is being installed, the same `project_dir` already resolved for
  the row's title. An install failure or a stalled venv build points at
  exactly the app it belongs to. (committed cc538425a; new test
  `test_the_mirrored_row_points_at_the_app_folder`,
  `tests/test_env_install.py`)
- `sys:index:*` (`fused_render/server/routers/index.py`) — Preferences'
  Indexing tab (`/preferences?tab=indexing`), the only place a scan's root
  list lives and the only place it can be cancelled or retried from outside
  the row itself. `navigateToJobPage`'s `JOB_PAGE_ROUTES` is an exact-string
  allowlist, so the query string had to be listed as its own literal entry —
  `"/preferences?tab=indexing"` alongside bare `"/preferences"` — rather than
  handled by any prefix or path-only match; `rewriteLegacyUrl` already
  round-trips this exact string unchanged (`router.test.ts`'s legacy-URL
  tests), so no router rewrite logic needed to change, only the allowlist.
  (new test `test_the_row_points_at_the_indexing_tab`,
  `tests/test_index_jobs.py`; frontend test extended in
  `router.test.ts`'s `navigateToJobPage dispatches a Job.page value`)
- `sys:update:*` (`fused_render/update/mac.py`) — `/preferences`, already in
  `JOB_PAGE_ROUTES`. No dedicated update page or Preferences tab exists: the
  update surface is sidebar chrome present on every route (`UpdateBadge.tsx`'s
  badge, `ServerStatusBanner.tsx`'s restart card), not a page of its own —
  the same reasoning the earlier build already used for the gh-CLI-install
  job's identical fallback. (test extended:
  `test_install_opens_a_cancellable_download_row_for_the_version`,
  `tests/test_mac_update.py`)

The remaining two prefixes this table names — `sys:ai-claude:*`
(`fused_render/server/ai.py`) and the four `ai/supervisor.py`-owned prefixes
(`ai-image:`, `ai-text:`, `ai-transcribe:`, `ai-video:`) — are Finding 1,
below.

### Finding 1 — `_job_page` returning `""` for four `ai/supervisor.py` prefixes

`_job_page()` only ever answered for `sys:ai-model:*` and `sys:ai-benchmark-*`;
every image, text, transcribe and video job, plus the Claude relay in
`server/ai.py`, got `page == ""` regardless of which page made the request.
Unlike the four Finding 2 producers, these all run behind an existing page
that already knows its own route — so instead of a fixed `_job_page` answer,
the calling page now travels in over `X-Fused-Page` (the same header
`routers/jobs.py` and `routers/capture.py` already read) and is threaded
through as an explicit `page=` argument, which `_report` takes over
`_job_page(job)` entirely when a caller supplies one.

`/api/ai/image`, `/api/ai/video` and `/api/ai` (Claude and local/Apple text)
read and unquote `X-Fused-Page` and pass it to
`start_image`/`start_video`/`_ai_relay`. Image and video set `page` only on
the opening report: the row cannot be evicted and rebuilt mid-render the way
a transcription's can, and `upsert` keeps whatever truthy `page` is already
on the row through every later tick that omits it. Text generation and
transcription instead carry `page` inside their row-identity dicts
(`text_row_fields`, `transcribe_row_fields`) alongside `title` and `model`,
since those rows are restated on every tick and must survive
`jobs._sweep`'s eviction the same way the rest of that identity does. The
Claude tier falls back to `/claude-config` when no page was given — unlike
local/Apple, a direct module-level call or a test may never supply
`X-Fused-Page` at all, and Claude Code's settings page is where that model
is configured, so the row is never left with nowhere to go.

(new tests: `test_relay_remote_job_row_page_defaults_to_claude_config`,
`test_relay_remote_job_row_page_is_the_callers_page_when_given`
(`tests/test_server_ai.py`);
`test_an_image_rows_page_is_the_caller_supplied_X_Fused_Page`,
`test_an_image_row_with_no_X_Fused_Page_has_no_page`,
`test_a_video_rows_page_is_the_caller_supplied_X_Fused_Page`,
`test_a_transcript_rows_page_is_the_caller_supplied_X_Fused_Page`,
`test_api_ai_threads_X_Fused_Page_into__ai_relay`
(`tests/test_ai_runtime.py`))

## Second fix-review round

Three more gaps found in the finished build. Fixed here, in order.

### Fix A — a Playground render with no `X-Fused-Page` opens its own output

The AI Models Playground raises image/video renders from the shell, not from
inside a page iframe, so it never sends `X-Fused-Page` — by design, the shell
is not a page and gets no fake one manufactured on its behalf. That left
every Playground render with `page == ""` and, per the Finding 1 table above,
no destination at all.

`_start_render` (`fused_render/ai/supervisor.py`, shared by `start_image` and
`start_video`) now gives a render with no caller-supplied page a destination
on its TERMINAL report, once the output path is actually known: the rendered
file itself on success (`result["path"]`), the folder that would have held it
on failure or cancellation (`os.path.dirname(request["out"])`) — the file
never got written in that case, but the route already created its parent
directory before starting the render, so the folder is a real destination
where the file itself would not be. A caller-supplied page (an app that DID
send `X-Fused-Page`) still wins outright; it is set once, at open, and
`upsert` keeps a truthy `page` already on a row through every later tick that
does not repeat it, so the two writes never race.

`navigateToJobPage`'s own `isDir` paint hint was wrong for this new case: it
called anything not ending in `.htm(l)` a directory, which made a `.png` or
`.mp4` output paint as a folder. It now tests the basename's own trailing
extension (`/\.[^./]+$/`) instead — any file-shaped basename paints as a
file, `.html`/`.htm` included, since those already match; a folder whose name
happens to contain a dot elsewhere still paints as a folder.

(`frontend/src/platform/lib/router.ts`, `router.test.ts`;
`fused_render/ai/supervisor.py`, `tests/test_ai_runtime.py`:
`test_an_image_row_with_no_X_Fused_Page_opens_its_own_output_file_once_done`,
`test_a_caller_supplied_page_still_wins_once_the_image_is_done`,
`test_a_failed_image_render_with_no_caller_page_points_at_its_output_folder`,
`test_a_video_row_with_no_X_Fused_Page_opens_its_own_output_file_once_done`,
`test_a_failed_video_render_with_no_caller_page_points_at_its_output_folder`)

### Fix B — a successful model load stays out of Notifications, without leaving the store

The literal ask was to have `supervisor.py`'s `_report(job, state="done",
detail="Model loaded")` remove the job from the store on a successful load,
so the terminal `sys:ai-model:*` row never persists as a notification.
Doing that in `supervisor.py` itself turns out to break a real consumer:
`fused.ai.models.load(wait=True)` (`_wait_job` in
`fused_render/templates/shared/fused_ai.py`) polls `GET /api/jobs` for this
exact job id and only stops polling once it observes the row itself go
`done` — a regression test already pins this exact ordering
(`test_a_successful_load_reports_a_visible_done_state`: reporting `done` and
dismissing the row back-to-back breaks this same caller, since a dismissed
row's next poll never shows `done` at all). `_wait_ready`'s row-merge (D628,
`NOTES-merge-model-load-row.md`)
does not have the same problem — its merge only reads the load's row while
the load is RUNNING, and clears in a `finally` on every exit path before the
row ever needs to be read again — but the `_wait_job` conflict alone rules
out a store-side removal on the success path the brief asked for.

Grepped for every other consumer of a terminal `sys:ai-model:*` row: the AI
Models Local page filters to `owner === "server" && !isTerminal(j)`
(`activeJobByModel` in `jobs.ts`) and so never depended on the terminal row
surviving either way.

Built instead as a FRONTEND-ONLY filter, the same shape the codebase already
uses for `sys:schedule:*` (`isScheduleJob`/`jobRows`, a row a backend
reporter/poller still needs but that must never surface as a notification):
`jobRows` (`frontend/src/platform/lib/jobs.ts`) now also drops a
`sys:ai-model:*` row once it has gone `done` (`isQuietModelLoad`). The
backend store, `_report`, and `_wait_ready` are untouched — a failed or
cancelled load still surfaces as a notification, and the row is still
observably present and `done` to any live watcher for as long as the
process's own aging (`_sweep`/`FINISHED_TTL_S`) would otherwise keep it. The
`Downloaded` terminal report for a model download is a separate,
long-running row and was left alone, as scoped.

This is a deviation from the literal instruction ("remove the job from the
store") — flagging it here because the store-side approach could not be made
to work without breaking `_wait_job`'s poll.

(`frontend/src/platform/lib/jobs.ts`, `jobs.test.ts`: `AI_MODEL_JOB_PREFIX`,
`isQuietModelLoad`, and three new tests around it)

### Fix C — dismiss-on-open no longer races a page's own re-attachment

`JobRow`'s open handler (`DownloadManager.tsx`) navigated to `job.page` and
then dismissed the row whenever `state === "done"`, with no regard for what
kind of destination `page` was. For a job a PAGE raised, `job.page` is that
same page, and the page re-attaches to its job by id on mount — the row
could be deleted server-side in the gap between the navigation firing and
the page's own mount effect running.

The open handler now dismisses on a `done` row only when the destination is
a shell route (`isJobPageRoute(job.page)`); an fs-path destination (a
rendered image/video with nothing else watching it, per Fix A) still opens
on click but the row stays. Failed and cancelled rows are unaffected — they
never dismissed on open before this fix and still don't, only their explicit
✕ clears them.

(`frontend/src/platform/ui/DownloadManager.tsx`;
`frontend/src/platform/ui/JobRow.test.tsx`: new test "a done job whose page
is an fs path navigates but does NOT dismiss itself")

## Third fix-review round

A fresh brief lettered A-F arrived against the state left by the second
round above. Item A (Playground text/transcription rows with no
destination) was dropped mid-task by explicit instruction: `TextStage.tsx`
sends `history: []` and persists nothing, so a finished text generation has
no output to open, and a transcription's destination is the transcript
files it writes under `<home>/ai/transcripts`, not the source media the
brief had assumed — a follow-on change on this same branch is what decides
whether those rows get stored at all, and the `JOB_PAGE_ROUTES` additions A
would have needed come out with it. No edits for A had been started, so
nothing needed reverting.

### Fix B — a model download and an unload were also hidden by the load's own filter

`isQuietModelLoad` (`frontend/src/platform/lib/jobs.ts`) matched on
`sys:ai-model:` id prefix plus `state === "done"`. That id family
(`job_id_for(model)` in `fused_render/ai/supervisor.py`) is shared by three
different producers reporting through the same row: a resident load
(`_bring_up`), a weights-only download (`_fetch_only`, reached via
`download()`), and an unload/eviction (`_remove`). The prefix+state test
could not tell a load's own finish from a download's or an unload's finish
on the same id, so a completed download or a completed unload also vanished
from Notifications — exactly the row Fix B in the prior round meant to
keep.

Fixed with an explicit field, `Job.quiet`, set only by the report that means
"a terminal row with nothing to act on": `_bring_up`'s success line now
passes `quiet=True`. `quiet` is plumbed through `fused_render/jobs.py` the
same way `waiting_for` already is — a dataclass field, an `upsert()` gate
restricted to `"quiet" in body and server` so no page-owned report can set
it, sticky across ticks like every other job field. `isQuietModelLoad` now
just reads `job.quiet`; no more string/prefix matching. A stale comment
citing a nonexistent `supervisor._ai_model_job_id` was corrected to the real
`job_id_for`.

Because `quiet` is sticky and the id is shared, a load's `quiet=True` would
otherwise leak into the very next unload's or download's row on that same
id — reintroducing a version of the original bug (an unload or download
looking finished-and-quiet right after a load). `_remove` and `_fetch_only`
both now pass `quiet=False` on their own success reports, restating it
explicitly rather than relying on a fresh row starting `False`, since the
row is not fresh — it is the same one the load just marked quiet. Pinned
with dedicated regression tests for all three producers, plus two more
proving the leak scenario itself (`test_an_unload_clears_quiet_EVEN_THOUGH_the_row_was_just_quiet`,
`test_a_weights_only_download_clears_quiet_EVEN_THOUGH_the_row_was_just_quiet`
in `tests/test_ai_runtime.py`).

(`frontend/src/platform/lib/jobs.ts`, `jobs.test.ts`; `fused_render/jobs.py`;
`fused_render/ai/supervisor.py`; `tests/test_jobs_api.py`,
`tests/test_ai_runtime.py`)

### Fix C — the dismissal regression test asserted nothing

`JobRow.test.tsx`'s three "must not dismiss" tests (fs-path destination,
error state, cancelled state) used an `onPatch` that threw, meaning to prove
the row survives. `dismiss()` swallows its own exceptions in a `catch`, so a
throwing `onPatch` looks identical to a normal dismiss from the assertion's
point of view — the test passed whether or not a dismiss happened at all.
Rewritten to count `dismissFn` calls and assert `dismissCalls === 0`, the
same shape the sibling "must dismiss" test at the shell-route case already
used to assert exactly one call. Verified against a real regression: with
`DownloadManager.tsx`'s `open()` guard temporarily removed, all three failed
showing `dismissCalls === 1`; restored, all pass.

(`frontend/src/platform/ui/JobRow.test.tsx`)

### Fix D — a render with no caller page and no result path still had nowhere to land

`_start_render`'s success report used `page=page or result.get("path") or
""`. The failure path two lines away already falls back to `out_dir` when
neither a caller-supplied page nor a result path is available; the success
path didn't, so a render that succeeds without either landed on `page=""` —
back to having no destination, the exact failure mode the rest of this spec
exists to close. Success now falls back to `out_dir` the same way failure
does: `page=page or result.get("path") or out_dir or ""`.

(`fused_render/ai/supervisor.py`; `tests/test_ai_runtime.py`)

### Fix E — a quiet row had no way to age out

`_sweep` (`fused_render/jobs.py`) keeps every terminal, non-schedule row
until its own dismissal clears it — correct for a row a person can see and
click ✕ on, but a quiet row (Fix B) is never drawn, so nothing can ever
dismiss it; without a carve-out it would sit in the `MAX_JOBS=64` pool for
the rest of the process's life. `_sweep`'s existing carve-out for
`sys:schedule:*` rows — aged out on the same read-gated clock
(`FINISHED_TTL_S`/`FINISHED_UNREAD_DROP_S`) instead of waiting on a
dismissal that will never come — now also covers `job.quiet`.

`list_jobs` always runs `_sweep` before `mark_read` stamps a row read, so a
newly-terminal row is guaranteed at least one full read before it can age
out; this is what lets `fused.ai.models.load(wait=True)`'s `_wait_job` poll
still observe the row go `done` even though the row is quiet. Pinned
directly: one test drives a quiet row past both TTLs and confirms it's gone
from `list_jobs`, a second drives `_wait_job`'s own poll across that same
row and confirms it still observes `done` before the row disappears.

(`fused_render/jobs.py`; `tests/test_jobs_api.py`)

### Fix F — a dotted folder name painted as a file

`navigateToJobPage`'s directory/file hint used `/\.[^./]+$/` against the
destination's basename — any trailing dot-plus-non-dot-non-slash counts as
an extension. That matches real folder names with no extension at all:
`github_setup.py`'s repo root, `envinstall.py`'s `project_dir`, and
`_start_render`'s own failure-path `out_dir` can all be dotted (`site.com`,
`app.v2`) without being files. Tightened to a closed, known list —
`/\.(html?|png|mp4)$/i`, matching the extensions `routers/ai_runtime.py`
actually names for its outputs — so a dotted folder name paints as a
directory again. Added a direct test for `site.com`, `app.v2`, and
`.config`, verified RED against the old regex before tightening it.

(`frontend/src/platform/lib/router.ts`, `router.test.ts`)

## Explicitly out of scope (per spec, unchanged)

Toasts, `fused.trackJob` API/no new `Job` field, native OS notifications,
whole-row clicks on repo rows, per-producer status-text changes,
`sys:schedule:*` rows.

## Three-tier model replaces `quiet`/schedule-prefix suppression (committed 2de2214c6, fdaa1cff1, b83e2c0ca, 0b67dc1e9)

Two ad-hoc mechanisms — `Job.quiet: bool` and `_sweep`'s `sys:schedule:*`
id-prefix carve-out — did the same job (keep a row out of Notifications
without keeping it out of Jobs) for two different reasons, and neither
generalized: a third row that wanted the same treatment needed a third
special case. Both are replaced with one closed-set field, `Job.tier:
"attention" | "trail" | "transient"`, default `"trail"`, server-only
settable the same way `quiet` was gated in `upsert()`. `_sweep` and
`jobs.ts`'s `jobRows` both read `effective_tier`/`effectiveTier` rather than
the stored field directly — a terminal job in `error` or `cancelled` is
always `attention` regardless of what its producer declared, because a
producer that assumed success has nothing left to say once the run actually
failed.

- **Tier is sticky, producers are not.** `job_id_for(model)` is shared
  across a resident model's load, its weights-only download and its unload —
  the same id family that used to make `quiet` leak between them (Fix B/Fix
  B above). `Job.tier` is exactly as sticky, so every producer restates its
  own tier on every terminal report rather than relying on what an earlier
  report on the same id left behind. Three regression tests
  (`test_ai_runtime.py`) drive load→download→unload on one id and assert
  each report's tier independently, closing the same leak shape Fix B
  closed for `quiet`.

- **Unload's classification flipped, not just its name.** The `quiet`-era
  reasoning for `_remove` (unload) was "not quiet — a real state change
  worth a row." Re-examined under the new question ("did the user ask for
  this, and is there anything left to look at?"), freeing memory writes
  nothing a click could open, so unload is `transient` now — the opposite of
  what it was. This is a genuine behavior change: an unload's own success no
  longer draws a Notification.

- **A scheduled run's own row never reaches Notifications, in any state —
  this was checked, not just assumed.** `schedule.py`'s `_report` sets
  `tier=jobs.TRANSIENT` on every call, but that tier is never what keeps a
  failed or cancelled scheduled tick out of the panel: `jobRows`
  (`frontend/src/platform/lib/jobs.ts`) excludes every `sys:schedule:*` id
  unconditionally, before `effectiveTier`'s error/cancelled override is even
  consulted, and `terminalNotifications` is built on top of `jobRows`. A
  scheduled run ending in `error`/`cancelled` reads as `attention` under
  `effectiveTier` — that part is real — but reading as `attention` and
  reaching Notifications are different questions, and only the second one
  is out of scope. An earlier pass through this section (superseded, see the
  out-of-scope entry below) conflated the two and claimed a failed scheduled
  run "becomes visible"; it does not, on either surface `jobRows` feeds.

- **`_apple_wait_row` (`fused_render/server/ai.py`) is untouched, on
  purpose.** It builds its own id (`supervisor.JOB_PREFIX + model`,
  unsanitized) rather than going through `job_id_for(model)`, so it was never
  actually in the shared-id leak Item 2 worries about, and it is not named
  in Item 2's producer list. Left at the default `"trail"` tier rather than
  guessed at — it is out of this increment's scope, not overlooked.

- **Building the frontend shell locally.** A large batch of
  `client`-fixture-dependent pytest tests failed at collection with
  `RuntimeError: React shell not built (fused_render/static/shell-dist/
  missing)`, unrelated to any tier change (confirmed by running one such
  test against an unmodified tree first). Ran `bun install && bun run build`
  in `frontend/` to produce the missing (gitignored) build artifact, which
  unblocked the full scoped pytest run rather than leaving those tests
  permanently erroring in this environment.

### Item 3 — grouping reuses ActivityDock's existing section convention

`RepoUpdatesDock.tsx`'s panel now draws two `.dl-section`s — "Needs you"
(every waiting-task row, plus every terminal job whose `effectiveTier` is
`attention`) and "Worth keeping" (pairings, repo rows, every other terminal
job) — rather than inventing a new heading class. `.dl-section` and
`.dl-section-head` already existed for ActivityDock's own Running/Background
tasks split (status-bar merge), including the "heading draws only when 2+
sections are present at once" rule; RepoUpdatesDock's two sections follow
the identical rule rather than a bespoke one, so a panel holding only repo
rows (the common case) still shows no header at all, unchanged from before
this item.

`TERMINAL_VISIBLE_CAP` now folds `terminalTrail` only — an attention-tier
terminal job (a failed or cancelled run) never folds behind "N older
notifications", however many ordinary finished jobs are piled up in the
other section. The chip's `label` reads `"${attentionCount} needs you"` and
takes the same `is-failure` tone the failure tint always used the moment
either attention source (`visibleAttention` or `terminalAttention`) is
non-empty; the numeral keeps counting every row from every source exactly
as before, unaffected by which section a row lands in.

One existing test's expectation flipped as a direct, correct consequence:
a failed job used to draw after an ordinary repo row in the old flat list;
now, because its `effectiveTier` is `attention`, it draws in the section
that renders first. Updated in place
(`RepoUpdatesDock.test.tsx`, "a failure comes before an ordinary repo row —
Needs you precedes Worth keeping") rather than left broken or worked around.

## Explicitly out of scope (per spec, unchanged) — UPDATE

Corrected: the entry this heading originally introduced claimed a scheduled
run that ends in `error`/`cancelled` draws a row in Notifications via
`effectiveTier`'s override. It does not — `jobRows` excludes every
`sys:schedule:*` id unconditionally, in every state, independent of tier
(see this file's "Three-tier model" section above, corrected in place).
`sys:schedule:*` rows stay exactly where the spec's own out-of-scope list
put them: never in the panel, on the same read-gated `FINISHED_TTL_S` age-out
they always had. A scheduled run that fails is not lost — `scheduleEvents.ts`
already pushes a persistent, server-acked "Task failed" toast that survives a
reload, independent of the job row's own fate. Toasts, `fused.trackJob`
API/no new `Job` field, native OS notifications, whole-row clicks on repo
rows, per-producer status-text changes, and `sys:schedule:*` rows all remain
out of scope, unchanged.

## Fourth fix-review round

Another review found six more gaps. Fixed here, in order.

### Finding 1 — `_sweep` aged out transient rows by the wrong tier (committed 1be00c2d0)

`_sweep`'s terminal branch gated the read-gated-clock ageing on
`effective_tier(job) == TRANSIENT`, not on the STORED `job.tier`.
`effective_tier` upgrades any `error`/`cancelled` row to `attention` for
VISIBILITY — a failed transient run is still worth showing while it exists
— so the moment a `schedule.py` tick (or any other transient producer)
ended in `error`, `_sweep` read it as `attention` and fell into the
keep-until-dismissed `else: continue` branch, with no surface able to ever
dismiss it. Retention and visibility are two different questions: fixed by
gating retention on the stored `job.tier` instead, and rewrote both the
inline comment and `_sweep`'s own docstring to say so explicitly, so the
next reader does not repeat the inversion.

Swept every other `effective_tier` call site (`ai/supervisor.py`,
`server/routers/index.py`) — both are comment-only prose describing
visibility, not a second instance of the bug.

New/rewritten test: `test_a_transient_row_that_ends_in_error_still_ages_out_on_the_read_gated_clock`
(`tests/test_jobs_api.py`) replaces a same-named-in-spirit test that
previously passed under both the old and new code, because it never called
`read_jobs()` before its final assertion and so exercised the larger
`FINISHED_UNREAD_DROP_S` backstop instead of the `first_read_at`/
`FINISHED_TTL_S` path the fix actually changes. Confirmed RED against the
old `effective_tier`-gated code, GREEN against the fix.

### Finding 2 — `jobRows` dropped transient jobs while still running (committed 3fdb99a95)

`jobRows` filtered `effectiveTier(j) !== "transient"` unconditionally, so a
running index scan or text generation — both declared `transient` — never
got a row at all, contradicting the tier's own documented meaning ("shown
while running, never kept once terminal"). Fixed by filtering transient
rows only once the job is `isTerminal`.

**Design correction made while fixing this, not requested separately:** a
naive `!isTerminal(j) || effectiveTier(j) !== "transient"` would have broken
D661 — `ActivityDock.tsx`'s own header comment requires a scheduled
message's own row to never appear in Activity, in ANY state, including
running. That guarantee previously held only as an emergent property of the
OLD (over-broad) filter; the general fix removes the accident that made it
true. Kept D661 intact by adding an explicit `id.startsWith(SCHEDULE_JOB_PREFIX)`
exclusion inside `jobRows`, independent of the tier check.

New tests (`frontend/src/platform/lib/jobs.test.ts`): a running index scan
and a running text generation both declared transient now get a row; a
scheduled run's job stays excluded in every state (`running`, `done`,
`error`), not only while transient-and-terminal.

### Finding 5 — `isFailure` was stranded dead code (committed 333085c4c)

No caller under `frontend/src` remained once `RepoUpdatesDock.tsx` moved to
reading `effectiveTier` (three-tier model migration, above). Deleted the
function and its doc comment; simplified `isTerminal`'s adjacent comment to
describe only what it still does.

### Finding 3 — a transcription worker's rebuilt row lost its page (committed 1bce20612)

`transcribe_row_fields` puts `"page"` in the row-identity dict a
transcription WORKER subprocess spreads into every `worker_base.report`
tick it posts to `/api/jobs` — but that request only ever carries
`X-Fused`/`X-Fused-Worker` headers, never `X-Fused-Page` (a worker has no
page context to attach one from; that header travels only with a page's own
runtime calls). `api_jobs_report` read `page` exclusively from that header,
and `jobs.upsert` never reads `body["page"]` at all, so a row evicted
mid-decode (`jobs._sweep`) and rebuilt from the worker's next tick came back
with `page == ""` — permanently, since nothing else supplies it once the
row is a fresh `Job()`.

Confirmed `text_row_fields` (the finding's second thing to check) has no
matching bug: its only caller path is `server/ai.py`'s `tick()` closure,
which always goes through `_report()` — an in-process helper that correctly
pops `page` from its kwargs — never a worker's raw HTTP tick.

Fixed by letting `api_jobs_report` fall back to `body.get("page")`
specifically when the request is worker-token-authenticated and no
`X-Fused-Page` header was sent. This does not weaken the header-only rule a
PAGE's own report still lives under (a page cannot claim a different
destination by typing one into its body) — a worker-token request is
already fully trusted to write every other row-identity field (`title`,
`model`, `kind`, ...), so trusting a body-supplied `page` for a worker
report specifically is no new exposure.

New test: `test_a_worker_rebuilding_a_forgotten_row_restores_its_page_from_the_body`
(`tests/test_ai_runtime.py`) — dismisses a row outright (simulating an
eviction), then has a worker-token tick recreate it with `page` in the
body; asserts the response's `page` is not empty. Confirmed RED against
the header-only code, GREEN against the fix.

### Finding 4 — opening reports on a shared model id didn't restate tier (committed 539935f48)

`_start_resident` and `load(weights_only=True)` both open their job with a
`state="running"` report on `job_id_for(model)` — an id a resident load, a
weights-only download and an unload all share, and each of those three
already restates its own tier on its TERMINAL report (`_bring_up`'s success:
`TRANSIENT`; `_fetch_only`'s success: `TRAIL`; `_remove`: `TRANSIENT`) for
exactly this reason. Neither OPENING report restated tier, so a load
started right after a download ran its whole `running` phase under that
download's stale `TRAIL`, and a download started right after a load (or an
unload) ran under a stale `TRANSIENT` — `Job.tier`'s own comment already
requires every producer on a shared id to restate its tier rather than lean
on whatever an earlier report left. Fixed by restating `tier=jobs.TRANSIENT`
on the resident load's opening report and `tier=jobs.TRAIL` on the
download's.

Swept every other `_report` call on this shared id (the progress ticks
inside `_bring_up`/`_fetch_only`, and their `cancelled`/`error` terminal
reports): none needed a further fix, because within one continuous run
nothing else writes to the id between the (now correct) opening report and
that run's own end, so the sticky field carries the right value through
without every single tick needing to restate it — the same pattern the
three existing terminal restatements already establish.

New tests (`tests/test_ai_runtime.py`):
`test_a_resident_loads_opening_report_does_not_inherit_a_downloads_trail_tier`
and `test_a_weights_only_downloads_opening_report_does_not_inherit_a_loads_transient_tier`,
each driving a stale tier onto the shared id first (via `supervisor.unload`
and the sibling flow) and then asserting the OPENING report of the other
flow already carries its own tier before its thread has had a chance to
reach a terminal state. `FAKE_LOAD_SECONDS` is widened in the first so the
assertion reliably lands before the fake worker reaches `ready`.

### Finding 6 — the GitHub publish endpoints leaked the repo's filesystem root (committed 3a30b8d7b)

`_publish_state["root"]` is the realpath'd, containment-checked repo root
`_report_publish` uses to give the published job row a click destination —
real news to that row, not to a page reading the publish endpoints. Both
`GET /api/github/publish` (`publish_status()`) and the `POST` that starts a
publish (`publish_start()`'s return value, handed straight back as the
response) copied `_publish_state` verbatim, including `root`, so either
endpoint answered with the server's own absolute filesystem path.

Added `public_publish_record()` — `_publish_state` (or `publish_start`'s
returned snapshot) minus `"root"` — and pointed both routes at it.
`publish_status()` itself is unchanged and still answers with `root` for
in-process callers (`test_github_setup.py`'s existing assertions on the
resolved root) that have a real reason to see it; only the HTTP surface is
redacted.

New test: `test_publish_status_does_not_leak_the_repositorys_filesystem_path`
(`tests/test_server_github.py`) — starts a publish and reads the status
back through both endpoints, asserting neither the JSON body nor the raw
response text contains the repo's absolute path.

## Fifth round

Five items, done in order.

### Item 1 — merge `origin/main`

Merged cleanly (native Claude chat, #1074). The one break: its
`transcribe.test.ts` builds a `Job` object literal through a local `job()`
helper with no `tier` field, and `Job.tier` is required. Added
`tier: "trail"` to that helper's defaults, matching every other field it
already stubs. `bunx tsc --noEmit` is clean across the whole merged tree —
no other `Job` literal was missing `tier`.

### Item 2 — opening a notification row always dismisses it

`DownloadManager.tsx`'s `JobRow` open handler dismissed a `done` row only
when its destination was a shell route (`isJobPageRoute`), leaving an
fs-path destination — and every `error`/`cancelled` row, unconditionally —
to linger until an explicit ✕. Both gates are gone: `open()` now calls
`navigateToJobPage(job.page)` then unconditionally `void dismiss()`.
`canOpen` (`isTerminal(job) && !!job.page`) is untouched — `JobRow` is
reused verbatim as `DownloadManagerView`'s in-flight row, and a running job
must still never open. The long comment above the block no longer explains
two exemptions; it states the one rule that holds now (going to look is the
acknowledgement) and keeps the still-true reasoning for gating `canOpen` on
`isTerminal`.

**What was checked for the re-attachment risk:** searched every in-repo
caller of a job-watching API — `fused.watchJob` (`fused_render/static/
runtime.js`) and the Playground's own local `watchJob` client
(`frontend/src/apps/ai_models/playground/client.ts`, used by `ImageStage.tsx`,
`VideoStage.tsx`, `TranscribeStage.tsx`). Every one of them calls `watch()`
in the same async flow that started the job, and `watch()`'s own loop
exits the instant the row goes non-`running`, handing the caller the
terminal record directly as its resolved value — before a `JobRow` could
ever become clickable at all (`canOpen` requires `isTerminal`, which by
construction is already true by the time `watch()` has returned). None of
them re-reads a terminal row from the server after the fact; nothing in
this codebase depends on a terminal fs-path row surviving past the click
that opens it. No caller here breaks. The scenario the fs-path exemption
was written to protect — an app page that persists a job id across its own
remount and reattaches to an already-terminal row via `fused.watchJob` —
is not exercised by any bundled app or template in this repo (grepped
`fused.watchJob`/`trackJob` across every `.html` in the tree: no hits
outside `runtime.js`'s own definition and doc comments); it remains a real
risk for a user's own custom app that adopts that pattern, but there is no
in-repo caller to point at breaking.

`JobRow.test.tsx`'s three "must not dismiss" tests (fs-path destination,
error, cancelled) now assert exactly one `dismissFn` call instead of zero;
the error-row test doubles as the "failed row clears on open" case the
brief asked for directly.

(`frontend/src/platform/ui/DownloadManager.tsx`, `JobRow.test.tsx`)

### Item 3 (HIGH) — the index-run mirror no longer resurrects derived failures

`_mirror_one_run_job` (`fused_render/server/routers/index.py`) upserted a
job the moment `list_runs` handed back ANY run, including one already
terminal on disk before this process started. `list_runs` reads run
directories off disk (`KEEP_RUNS=20`), so a run that finished — or was
abandoned by a worker that died — in an earlier process still shows up on
this process's very first tick; a dead worker's abandoned run reports
itself via `_with_liveness` as a synthetic "the scan worker died without
finishing (no activity for 300s)" error on every such read, and
`_mirrored_terminal` (which would otherwise stop the re-upserting) starts
empty on every restart. Result: up to 20 stale "Indexing files" failures
resurrecting in the red needs-you section at every launch, exactly as the
brief described — confirmed by reproducing it directly in
`tests/test_index_jobs.py` before the fix (both RED tests below failed
against the unmodified code).

Implemented the class fix, not a `sys:index:*` carve-out in `_sweep`:
added `_seen_running`, a module-level set of run ids THIS PROCESS has
itself observed `running`. A run is now mirrored into a job at all only
once this process has actually seen it running — checked right where
`running` is computed, before any `jobs.upsert` call. A run already
terminal the very first time this process reads it is skipped outright
(marked straight into `_mirrored_terminal`, no upsert, no row ever
created, same as the existing "already handled" fast path). A run seen
running here — including one a previous process started that is still
genuinely scanning when this process boots (`start()` spawns a detached
subprocess, so this is a real case, not hypothetical) — is mirrored
normally and gets its terminal row exactly as before once it finishes.

**The signal chosen is liveness actually observed by this process, not a
wall clock.** It needed no heuristic: `run.get("running")` is already the
same fact `_mirror_one_run_job` computes for every other purpose on this
same tick, so "was this run ever seen running by THIS process" falls out
of tracking that fact once, cheaply, in a set — durable for the process's
whole lifetime and exact rather than approximate (no age threshold to
tune, no window where a fast-finishing legitimate run could be
misclassified beyond the bridge's own tick cadence, which every
scan-starting call site already wakes immediately via
`_wake_index_job_bridge`).

New tests in `tests/test_index_jobs.py`:
`test_a_run_already_done_before_this_process_started_draws_no_row`,
`test_a_run_already_errored_before_this_process_started_draws_no_row`
(both RED against the unmodified code, GREEN after the fix), and
`test_a_run_seen_running_by_this_process_still_draws_its_terminal_row`
(already passed before the fix — pinned to prove the carve-out doesn't
overreach). The existing autouse fixture's reset now also clears
`_seen_running` between tests.

### Item 4 (LOW) — deleted two unreachable tier restatements in `_fetch_only`

`load(weights_only=True)` already opens `job_id_for(model)`'s row with
`tier=jobs.TRAIL` before a weights-only download's own thread can reach
either of its failure-shaped terminal reports (the busy-wait loop's own
`cancelled` report, and the outer `except` block's `cancelled`/`error`
report) — `Job.tier` sticks until a later report says otherwise, so
restating `TRAIL` on those two reports could never actually change what
the row already said. Deleted both restatements (and their now-false
justifying comments) from `fused_render/ai/supervisor.py`. Confirmed
empirically first: all three `restates_trail_tier` tests still passed
with the restatements removed. The `_bring_up` twins (the resident load's
own `cancelled`/`error` reports) are untouched — `_start_resident`'s
opening report sets `TRANSIENT`, not `TRAIL`, so those two restatements
are genuinely load-bearing and their comments stay accurate.

`test_a_failed_weights_only_download_restates_trail_tier_instead_of_inheriting_transient`
(`tests/test_ai_runtime.py`) claimed the download's failure path needed
its own restatement to avoid inheriting a stale `TRANSIENT` from a
load/unload of the same model — false for this path, since the download's
own OPENING report already stamps `TRAIL` first. Renamed to
`test_a_failed_weights_only_download_keeps_trail_tier_instead_of_inheriting_transient`
and its docstring rewritten to say what actually holds: the tier survives
from the opening report, not from either terminal one. The two resident-
load tests keep their original names and docstrings — those restatements
are real, so the original premise still holds for them.

### Item 5 (LOW) — corrected two comments that overstated the schedule toast

`ActivityDock.tsx`'s header comment claimed the "Task finished:"/"Task
failed:" toast "is the one surface" for a scheduled run once its own
Activity row was removed, and `jobs.py`'s `_sweep` docstring gave the same
toast as part of the reason a scheduled row safely ages out. Neither toast
text exists anywhere in the codebase (grepped for it) and the claim is
wrong for a successful run: `toastForEvent` (`platform/lib/
schedule-toast.ts`) returns `null` for `kind === "done"`, so a successful
scheduled run produces no toast at all — its only surface is the
Scheduled/Tasks page. Both comments now say a missed or failed run gets a
toast and a successful one does not. The age-out conclusion itself
(`_sweep`'s `sys:schedule:*` carve-out ages every terminal state out
unconditionally on id alone) was already correct and is unchanged.

### Test/type state at the end of this round

Scoped runs, all green: `tests/test_ai_runtime.py` + `tests/test_jobs_api.py`
combined (614 passed, 1 skipped, pre-existing); `tests/test_index_jobs.py`
(29 passed). Frontend: `jobs.test.ts`, `DownloadManager.test.tsx`,
`JobRow.test.tsx`, `RepoUpdatesDock.test.tsx`, `ActivityDock.test.tsx`,
`router.test.ts` combined (227 passed). `bunx tsc --noEmit -p .` clean;
`node scripts/check-boundaries.mjs` clean (705 files). Did not run the
full pytest suite — left to the orchestrator, per brief.

## Sixth round — `page` canonicalized at every backend producer

`test-python-windows` failed two of this branch's own tests:
`test_an_image_row_with_no_X_Fused_Page_opens_its_own_output_file_once_done`
and its video twin, both asserting `finished["page"] == started["path"]`.
On Windows the two sides disagreed in spelling only — `page` came back
backslashed, `path` came back forward-slashed for the same file.

**The canonical spelling, and the evidence for it.** `ai_runtime.py`'s own
`path`/`previewPath`/`image` reply fields all run through
`_view_url_codec.canonical_fs_path` before they leave the router, with a
comment naming the reason: "this goes back to a page that will put it in a
`/api/fs/raw` URL, and a Windows path that reached it backslashed would not
match what the shell stored for the same file." `canonical_fs_path` is
also what a `/view` URL decodes to and what `X-Fused-Page` carries — it is
string-based (checks for a drive-letter root, not the host OS), so it is a
no-op on a POSIX path and idempotent on an already-canonical one. That
makes it the one spelling every fs path crossing this API boundary is
already held to; `page` was the one field on this branch that reached
`jobs.upsert` without it.

**Where `page` broke that rule, and the fix at each site** (found by
grepping `page=` into every `jobs.upsert`/`_report` call across
`fused_render/`, then checking which built the value with `os.path` or
`pathlib`):

- `ai/supervisor.py`'s `_start_render` (image and video share it): the
  terminal report's `page=page or result.get("path") or out_dir or ""`
  took `result["path"]` straight off the worker's own reply (built with
  `os.path` on the worker's side of the boundary) and `out_dir` from
  `os.path.dirname`, neither canonicalized. Both now run through
  `canonical_fs_path` before they reach `page`.
- `github_setup.py`'s `_report_publish`: `page=snapshot.get("root")` was
  `_resolve_repo_root`'s `os.path.realpath`'d root, raw. Wrapped in
  `canonical_fs_path`.
- `envinstall.py`'s `_mirror_into_jobs`: `page=project_dir` is caller-
  supplied and reaches here through more than one path (`ai/supervisor.py`
  passes `runner.folder`; `/api/env/install` passes a client-asserted
  string) — normalized at this one production point rather than trusting
  every caller. Wrapped in `canonical_fs_path`.
- Checked and cleared: `capture/__init__.py`'s `page=session.page` is a
  pass-through of the caller's own `X-Fused-Page` (never built with
  `os.path`), as are `server/ai.py`'s `page=page or "/claude-config"` and
  every `page=` in `claude_install.py`, `index.py`, `update/mac.py` — all
  static route strings, no fs path involved.

**Kept the assertion strength, updated what it compares against.** The two
CI-failing tests, and the two hitherto-Windows-only-broken siblings in the
same modules (`test_the_reported_publish_job_points_at_the_repo_root` in
`tests/test_github_setup.py`, `test_the_mirrored_row_points_at_the_app_folder`
in `tests/test_env_install.py` — both compared a mirrored `page` against a
raw `os.path`/`pathlib` value that only happened to already be canonical on
POSIX), now compare against `canonical_fs_path(...)` of that same raw
value rather than the raw value itself — still a real equality on the same
file, just expressed in the form both sides are actually held to.

**New coverage, pinned on every platform, not just the Windows job:** four
new tests feed a Windows-shaped string (`C:\Users\...`) directly through
each fixed producer and assert the row's `page` comes back forward-slashed
— `test_an_image_result_path_that_comes_back_windows_shaped_still_lands_canonical`
and its video twin in `tests/test_ai_runtime.py` (mocks `generate_image`/
`generate_video` to return the Windows-shaped `path` directly, so the
render worker's own OS is irrelevant to the test), and
`test_the_mirrored_row_canonicalizes_a_windows_shaped_project_dir` in
`tests/test_env_install.py` (calls `_mirror_into_jobs` directly with a
Windows-shaped `project_dir`, `progress` stubbed to finish on the first
poll). `github_setup.py`'s fix is exercised by the same
`test_the_reported_publish_job_points_at_the_repo_root` re-expressed
above — no separate Windows-shaped test was added there, since
`_resolve_repo_root` always returns a POSIX path on the CI's own host and
the existing test already runs the real `_report_publish` path end to end
through `canonical_fs_path`.

Scoped runs, all green: `tests/test_ai_runtime.py` (553 passed, 1 skipped,
pre-existing), `tests/test_github_setup.py` + `tests/test_env_install.py`
combined (256 passed). Did not run the full pytest suite — left to the
orchestrator, per brief.
