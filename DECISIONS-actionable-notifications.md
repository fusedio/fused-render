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

## Frontend, unit 2 (uncommitted as of this writing — see "open regression" below)

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

(Once the full suite is reconfirmed clean at the pre-existing 2-fail/2-error
baseline, this section will be updated with the final file list and the
commit this unit landed in.)

## Producer→destination table (backend, not yet implemented as of this writing)

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

Status: **not yet implemented.** No backend Python file has been edited in
this build. Still to do, TDD (test first) for each:
1. `fused_render/jobs.py` — widen `page`'s doc comment.
2. `fused_render/ai/supervisor.py` — `BENCHMARK_JOB_PREFIX` constant,
   `_job_page(job)` helper, `_report` passes `page=_job_page(job)`.
3. `fused_render/ai/benchmark.py` — replace the two inline
   `jobs.SERVER_ID_PREFIX + "ai-benchmark-"` literals with
   `supervisor.BENCHMARK_JOB_PREFIX`.
4. `fused_render/claude_install.py` — `page="/claude-config"` on its
   `jobs.upsert(...)` call.
5. `fused_render/github_setup.py` — `page="/preferences"` on
   `_report_install`'s `jobs.upsert(...)`; add a `root` field to
   `_publish_state`, thread `real_root` through `publish_start`, and
   `page=snapshot.get("root") or ""` on `_report_publish`'s `jobs.upsert(...)`.
6. `fused_render/capture/__init__.py` — `page` param on `_Session.__init__`
   and `start()`, threaded through, used in `_report`.
7. `fused_render/server/routers/capture.py` — `x_fused_page` header param on
   `api_capture_start`, extracted via `unquote` mirroring
   `routers/jobs.py`'s exact pattern, passed to `capture.start(mode, body,
   page=page)`.

Server-side spoof-proofing (`X-Fused-Page` header only, never request body)
was reconfirmed correct and unchanged in `routers/jobs.py` — no edit needed
there.

## Explicitly out of scope (per spec, unchanged)

Toasts, `fused.trackJob` API/no new `Job` field, native OS notifications,
whole-row clicks on repo rows, per-producer status-text changes,
`sys:schedule:*` rows.
