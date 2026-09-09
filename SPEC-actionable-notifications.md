# Every Notification in the Status Bar Goes Somewhere

## Context

The status-bar Notifications panel aggregates four unrelated row kinds — repo-behind-remote rows, terminal jobs, LAN pairings, and tasks waiting on input — each with its own type, data source, and dismissal path. Two of those kinds are actionable and two are not: a terminal job has no click target at all (its `page` field already records the app that raised it, but renders only as a tooltip), and a LAN pairing is pure informational text. A waiting task whose `href` is null is likewise inert. Because every terminal state is kept until dismissed (D663), these targetless rows accumulate and hold permanent seats in a panel that folds anything past five rows behind an expander, so the rows crowding it are often the ones a user most needs to act on. The fix is to give every stored row a destination rather than to expire rows on a timer.

## What's Changing

Every row in the Notifications panel becomes actionable: clicking it navigates somewhere useful and clears the row. Terminal jobs gain a destination — the app that raised them for page-raised jobs, and their own originating page (`/ai-models`, `/claude-config`, the benchmark page, and so on) for the server-raised producers that currently set no attribution at all. LAN pairings point at the Preferences LAN tab, which already lists paired devices and is URL-addressable as `/preferences?tab=lan`. A waiting task with no folder of its own points at `/tasks`.

`Job.page` widens from "the `.html` that raised it, attribution only" to "where clicking this row goes" — either an absolute fs path or a shell route. The five server-side producers that call `jobs.upsert(..., server=True)` with no `page` (`ai/supervisor.py`, `ai/benchmark.py`, `capture/__init__.py`, `claude_install.py`, `github_setup.py`) begin setting one. This also delivers the behavior `jobs.py:278-279` already claims — "clicking it goes back there" — which was never built.

No auto-clear timer is introduced. Rows still live until dismissed; what changes is that dismissing one is now something you do *by acting on it*.

## Affected Flows

```
CURRENT
  page-raised job ─────> Notifications ─ no click ─────────── sits until dismissed
  server-raised job ───> Notifications ─ no click, no page ── sits until dismissed
  LAN pairing ─────────> Notifications ─ no click ─────────── sits until dismissed
  task asking ─────────> Notifications ─ row click → /explorer ✓  (inert if href null)
  repo behind ─────────> Notifications ─ Update / Fix with Claude ✓

DESIRED
  page-raised job ─────> Notifications ─ row click → the app that raised it ─┐
  server-raised job ───> Notifications ─ row click → /ai-models, /claude-… ──┤
  LAN pairing ─────────> Notifications ─ row click → /preferences?tab=lan ───┼─> navigates
  task asking ─────────> Notifications ─ row click → /explorer or /tasks ────┘   and dismisses
  repo behind ─────────> Notifications ─ Update / Fix with Claude ✓  (unchanged)
```

- **Terminal job rows**: gain a clickable body that navigates to the widened `page` target and dismisses the row. Dismissal here is the existing permanent server-side delete.
- **Server-side job reporting**: five producers start supplying a destination. `sys:schedule:*` rows are unaffected — `jobRows` already drops them before they reach Notifications.
- **LAN pairing rows**: gain a clickable body targeting the Preferences LAN tab; dismissal remains the existing server-side call.
- **Waiting-task rows**: a null `href` stops rendering as inert text and falls back to `/tasks`. Non-null `href` behavior is unchanged.
- **Repo-update rows**: unchanged. Their buttons already make them actionable, and the row body stays non-clickable.
- **The panel as a whole**: drains as the user works through it, since acting on a row consumes it.

## Constraints

- **No auto-dismiss timer, anywhere.** D663 (`fused_render/jobs.py:731-743`) reverted a 3-second post-read TTL on `done`/`cancelled` rows after finding it deleted "the very entry Notifications exists to keep, out from under a user who had not yet looked." `toast.ts:7-10` records the same decision for the toast stack. Both stand.
- **Failures persist until dismissed.** An `error` or `cancelled` row is never cleared on a clock regardless of whether it has a target.
- A 5s auto-clear on job rows would fire the permanent server-side `dismiss()`, and `fused.watchJob` gives up the moment a row disappears — so a timer would break a page still watching its own job, not merely tidy the panel.
- **`page`'s spoof-proofing is preserved.** A page-raised job's value continues to come from the `X-Fused-Page` header rather than the request body (`server/routers/jobs.py:61-64`), so a reporter still cannot claim a destination it does not own.
- Existing action buttons and the per-row ✕ keep working as nested controls inside a now-clickable row body; a keydown on a nested control must not activate the row (the guard already in `NotificationCard`).
- `TERMINAL_VISIBLE_CAP`'s fold, the chip's count and tone, and the localStorage dismissal keys keep their current behavior.

## Out of Scope

- **Toasts.** The ~60 `pushToast` call sites, the `MAX_TOASTS` cap, and the no-TTL rule are untouched.
- **Job output-file paths.** No job row opens a file; no new field and no `fused.trackJob` API change to let a producer name a result file.
- **Native OS notifications.** None exist in the codebase, and `fused_render/app.py:362-363` records that as a deliberate decision.
- **Whole-row clicks on repo rows.** Their buttons already satisfy the actionability rule.
- **Per-producer changes to what a job's title or status text says** — only its destination is in scope.
- **`sys:schedule:*` rows**, which never reach the panel and keep their existing read-gated `FINISHED_TTL_S` age-out.
## Producers and their destinations (settled)

There are **five** job-producing modules, not six. `fused_render/shell/onboarding.py`
only calls `jobs.list_jobs()`; it never calls `jobs.upsert`, so it is excluded.
`github_setup.py` raises two jobs. Each destination below is settled:

| Producer | Job | Destination |
|---|---|---|
| `ai/supervisor.py` | `sys:ai-model:*` | `/ai-models/local` |
| `ai/benchmark.py` | benchmark job | `/ai-models/benchmark` |
| `capture/__init__.py` | `JOB_PREFIX + session.id` | the page that started the capture, threaded from the `X-Fused-Page` header on `POST /api/capture/start` into `_Session` |
| `claude_install.py` | claude install job | `/claude-config` |
| `github_setup.py` | `PUBLISH_JOB_ID` ("Publishing to GitHub") | the containment-checked work-tree root the publish already resolves via `_resolve_repo_root(root)` |
| `github_setup.py` | `JOB_ID` ("Installing the GitHub CLI") | choose the most defensible target and record the reasoning in the decisions log; there is no repo root in play for a CLI install |

Routes confirmed to exist: `/ai-models/local`, `/ai-models/benchmark`,
`/claude-config`, `/preferences?tab=lan`, `/tasks`.
