# Decisions — model slot conflicts

Notes kept alongside `SPEC-model-slot-conflict.md`: places the implementation
had to make a call the spec left implicit, and why.

## Detecting the swap loop (supervisor, §1)

`_request_path_conflict` (`fused_render/ai/supervisor.py`) only opens a new
`ConflictState` when a REQUEST-PATH load evicts a worker that was *itself*
installed by a request-path load within `CONFLICT_WINDOW_S` — see
`Worker.request_path_evicted`/`request_path_at`. A worker that simply started
from an empty slot is an ordinary switch, not proof of a fight; a worker that
already displaced someone, evicted again this soon, is two callers taking the
slot off each other. This is why the first swap of a capability never raises
a conflict and the second (closing the loop) does — matching the spec's own
"stop the loop, don't flag the first switch" framing.

A contender that shows up while a conflict already stands is folded into the
existing `ConflictState.models` list rather than starting a second conflict,
so a notification or dock row raised later names everyone still asking for
the capability, not just whoever collided first.

## Accepted race in `resolve_conflict(capability, keep=<model>)`

`resolve_conflict` reads the existing conflict's `models`/`since` under
`_lock`, then calls `load(keep, capability, request_path=False)` *outside*
the lock (loading can block on I/O), and only re-takes `_lock` afterward to
record the new held `ConflictState`. Between those two locked sections
another caller could in principle race in (a manual load/unload, or another
`resolve_conflict`/`release_conflict` call) and see stale state. This is an
accepted, narrow race — the window is a single HTTP round trip on an
operation a human just triggered from the dock, not a hot path — and is
unchanged from the supervisor session's original design; it is recorded here
rather than fixed because fixing it would mean holding `_lock` across a
model load, which blocks every other capability's read of `_conflicts` for
the load's whole duration.

## Scope: which capabilities can raise `ModelSlotConflict`

Only `generate_text`/`_generate_sync`'s request-path loads can hit
`_request_path_conflict` — image, video, speech, and transcribe all resolve
their model up front and never re-enter the request-path load inside a
retry loop the way text generation's tool-calling loop does, so they
structurally cannot produce the "second swap within the window" shape this
feature detects. `embed`/`decide` share text generation's capability key and
so DO get a `ModelSlotConflict` mapped through `_conflict_error` when a
conflict is already standing on `text-generation` — not because they can
trigger one themselves, but for consistency: a caller hitting the shared
capability while it's held should see the same terminal error shape
regardless of which call name hit it.

## UI: the toast's two choices vs. the dock row's three (§4)

The spec asks for three actions wherever the conflict is surfaced: "Keep
`<model a>`", "Keep `<model b>`", "Stop both". The dock row
(`ModelsDock.tsx`'s `ConflictRow`) offers exactly that, because it talks
straight to `NotificationCard`, which has three independent action slots
(`liveAction`, `navAction`, `extraAction`).

The toast (`frontend/src/shell/useModelConflictNotify.ts`) goes through
`platform/lib/notifications.ts`'s `notify()`, whose `NotificationInput` only
exposes two action slots (`action`, `extraAction`) — confirmed by reading
every existing call site, none of which populates a third. Rather than
widen that pipeline for this one feature, the toast offers "Keep `<the
model that is NOT currently resident>`" and "Stop both", on the reasoning
that dismissing the toast without acting is equivalent to "keep the
resident model": the supervisor already serves the resident model normally
and only refuses the other one's request-path loads, held or not. The dock
row stays the one place that also names the current resident as an explicit
choice. This is a deliberate, narrower surface for the toast, not a missed
requirement — the full three-way choice is always one click away in the
Models dock.

## Toast cadence

One toast per conflict *episode* per capability: a `useRef` set of
capabilities already notified for their current standoff, cleared the
moment that capability drops out of `runtime.conflicts` (resolved, released,
or cleared by a manual load/unload), mirroring `useTaskStatusNotify.ts`'s
own "have I already told them about this" bookkeeping. A conflict that
enters already `held` (the user pre-empted detection via a dock action)
marks itself seen without ever raising a toast — it is the user's own
action moments ago, not news.

## Resident-dot semantics unchanged by conflicts

`ModelsCardView`'s `idle`/`tone`/`StatusChip` computation stays keyed off
`models.length` alone, never `conflicts` — a held-empty conflict
(`keep: null`) correctly leaves the dot outlined (nothing resident) while
still drawing its own conflict row, because the row is informative content,
not a loaded model. The chip's only conflict-aware change is an additive
warning tint (`.has-conflict .dl-toggle { box-shadow: inset 2px 0 0
var(--warning); }`), layered onto whatever tone the resident count already
produced rather than replacing it.
