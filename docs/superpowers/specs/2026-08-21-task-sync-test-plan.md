# Task lifecycle sync — test plan (2026-08-21)

Reported symptoms (Akshil):

1. Past-dated task sat "queued" (board: Upcoming) for a long time before pickup.
2. While running: board In Progress, dock "thinking" — agreed. Then board Done,
   dock never showed a completed row.
3. Next task: board stuck In Progress while dock showed completed.

Three surfaces that must agree, and their real cadences:

| surface | source | cadence |
|---|---|---|
| Board / List (Tasks page) | `GET /api/tasks` → `_status` derivation | 10s active / 30s idle (`tasksPulse`), poked by dock on run start/end |
| Dock queue half | `GET /api/schedule/queue` (queued/running/live) | 6s |
| Dock job half | `GET /api/jobs` (`sys:schedule:<id>` rows) | 1s active / 5s idle |
| scheduler | `schedule.tick()` | 30s |

## Stage matrix under test

For every lifecycle stage, assert all three server-side answers in one test:
board status (`/api/tasks`), queue membership (`schedule.queue()` + live list),
job registry row (`jobs.list_jobs()`).

### S1 Create
- 1.1 one-off future → pending; board Upcoming; queue empty; no job row
- 1.2 one-off past → pending; queued; board Upcoming; claimed on next tick
- 1.3 cron template → recurring + exactly one materialized occurrence
- 1.4 rule with past anchor → one catch-up occurrence, overdue immediately
- 1.5 chat handoff (session_id set) → task keyed on that session pre-run

### S2 Queue semantics
- 2.1 queue order == claim order (due, id)
- 2.2 cancel vs claim race → refused, never half-cancelled
- 2.3 run_now on pending / on busy session (refusal wording)
- 2.4 `live` list == sent && !turn exactly

### S3 Pickup latency + holds
- 3.1 past-due claimed on the first tick at/after due (bound: one POLL_INTERVAL)
- 3.2 hold: sibling `sending` in same session
- 3.3 hold: sibling `sent` no-turn (busy via claude_session_id)
- 3.4 hold: live transcript (user typing) — and release when quiet
- 3.5 release when verdict lands
- 3.6 stuck `sending` → error after 300s; siblings unaffected
- 3.7 unwatched `sent` closed by sweep (process-death recovery)
- 3.8 fresh-session entries never held
- 3.9 batch: same-session pair serializes (second deferred, not dropped)
- 3.10 create() does not nudge the loop — pickup is bounded only by tick cadence
  (documented fact; the perceived-latency half of symptom 1)

### S4 Execution + watcher
- 4.1 spawn error → entry error / job error / event failed / board Failed
- 4.2 spawn ok → sent, watched, job running+cancellable
- 4.3 parked permission → job detail "waiting for permission"
- 4.4 dock ✕ mid-turn → turn cancelled / job cancelled / board ???
- 4.5 done ok → turn ok + turn_at / job done "finished" / event done / board Done
- 4.6 done error → turn failed / job error / board Failed
- 4.7 watch ends w/o verdict → turn unknown / job error / board Failed
- 4.8 chain writeback: template + pending successor learn session id

### S5 Sync matrix — the reported bugs
- 5.1 sent, turn "", watcher alive, **no transcript yet** → board must be
  In Progress (suspect: reads Done in the spawn→first-report window)
- 5.2 sent, turn "", watcher alive, transcript quiet >45s (long silent tool
  call) → board must stay In Progress (busy set must carry it)
- 5.3 turn ok, transcript echo within 5s → board Done (suppression works)
- 5.4 turn ok, transcript active >5s after turn_at → In Progress (new work — by design)
- 5.5 turn ok but join MISSED (scheduled prompt outside 3-tail) → suppression
  still must not wedge board In Progress
- 5.6 turn cancelled → board lane? (suspect: reads Done while dock says Stopped)
- 5.7 turn ok + busy poisoned by older sent-no-turn entry same session →
  board stuck In Progress while dock shows completed (symptom 3 candidate)
- 5.8 job row lifecycle vs FINISHED_TTL: done row exists ≥30s for dock to draw
  (symptom 2: dock never showed completed)
- 5.9 identical-body recurring entries: join steals wrong prompt → ran_at wrong

### S6 Events
- 6.1 done/failed/missed emitted once each, ack survives replays
