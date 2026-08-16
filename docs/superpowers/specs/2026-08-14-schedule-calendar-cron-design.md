# Schedule: calendar view, recurring (cron) runs, page-side creation

Date: 2026-08-14. Builds on PR #508 (scheduled messages). Branch
`agent/20260814-schedule-calendar`, cut from `claude/fused-render-scheduling-8nm8ji`.

## Decisions (from review with Akshil)

- Calendar week view is the default on `/scheduled`; existing card list stays as a
  second tab (toggle). Calendar shows past runs (sent/missed/error) and upcoming
  (pending + projected recurrences), color-coded.
- Recurrence expressed as presets (once / hourly / daily at HH:MM / weekly on day)
  plus a raw cron field for power use. Stored internally as a 5-field cron string.
- Jobs can be created from the `/scheduled` page (**New job** modal, and clicking an
  empty calendar slot prefills the time) as well as from the chat composer's
  Send now pill (which gains an exact date-time picker and repeat options).
- A recurring occurrence that comes due while the app is closed is **skipped**
  (marked missed, schedule rolls forward). No catch-up replay for recurring jobs.
  One-shot messages keep the existing 24h catch-up bound.
- No new Python dependency: a small hand-rolled 5-field cron parser
  (`fused_render/cron.py`) computes next occurrences. Supported syntax: `*`,
  numbers, `a-b` ranges, `a,b` lists, `*/n` and `a-b/n` steps; dow 0–7 (0 and 7 =
  Sunday); standard OR rule when both dom and dow are restricted. Cron times are
  LOCAL time — that is what a person scheduling "daily at 9am" means.

## Backend model: occurrence-as-entry

- A recurring job is stored as a **template** entry in the same
  `scheduled_messages.json` store: `state: "recurring"`, `repeats: "<cron>"`.
  Templates are never claimed or sent themselves.
- Each tick, a materialization sweep ensures every live template has exactly one
  `pending` **occurrence** entry — a normal one-shot carrying `template_id` and
  `due` = next cron time. The occurrence flows through the existing state machine
  (pending → sending → sent/missed/error) untouched, so job rows, events, toasts,
  and the watcher all work unchanged.
- Occurrences get a small per-entry late bound (`max_late: 60`) instead of the
  global 24h one — that is what implements "skip missed": overdue at launch →
  swept to `missed`, and the sweep materializes the next occurrence.
- `create()` accepts `repeats`; validates the cron string; stores the template and
  materializes the first occurrence immediately (so LaunchAgent wake sync sees it).
- `cancel(template_id)` cancels the template AND its pending occurrence.
- `GET /api/schedule` additionally returns, per template, `upcoming`: the next
  projected occurrence times within 14 days (server-side cron math), so the
  calendar can draw future ghost boxes without a JS cron parser.

## Frontend

- `Scheduled.tsx`: segmented toggle Calendar | List. Calendar = hand-rolled CSS
  grid week view (7 day columns, hour rows), prev/next week + Today. Boxes:
  pending (accent), sent (ok tone), missed/error (danger tone), projected
  recurrences (ghost/outline). Click a box → popover with prompt, folder, state,
  permission mode, repeat rule, and Cancel for pending/recurring.
- **New job** modal: target path, prompt, date-time (datetime-local), repeat
  preset + raw cron, permission mode. Clicking an empty slot opens it with that
  slot's time prefilled. POSTs to `/api/schedule` with the D3 header, same as the
  composer.
- Composer Send now menu: adds "Pick a time…" (datetime-local) and repeat
  presets/raw cron; sends `repeats` alongside `due`.

## Out of scope

- OS wake verification (does the LaunchAgent wake a sleeping Mac) — separate
  manual spike; requires physically sleeping the machine.
- Editing an existing job in place (cancel + recreate covers it for now).

## Testing

- `tests/test_cron.py`: parser next-occurrence table tests (presets, steps,
  ranges, dow 0/7, dom-or-dow rule, DST-adjacent times).
- `tests/test_schedule.py` additions: template materialization, skip-missed
  roll-forward, cancel cascades, upcoming projection.
- API tests: create with `repeats`, invalid cron → 400.
- Frontend: pure-logic tests where the repo already has them (schedule-toast
  pattern); visual verification in the running app.
