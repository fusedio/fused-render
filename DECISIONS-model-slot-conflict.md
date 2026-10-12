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

`Worker.evicted_model` records WHICH model a request-path load's own
eviction displaced. `request_path_evicted`/`request_path_at` alone only say
"this worker displaced somebody recently" — not WHO — which let a one-way
A -> B -> C sequence (three distinct models, nobody fighting) misread as
A -> B -> A closing its loop, since both shapes set those two fields on B
identically. `_request_path_conflict` now also requires the model asking
for the slot to be the SAME model `current.evicted_model` names before it
will open a conflict.

## Atomic hold: writing a resolve's `ConflictState` with its eviction

`_start_resident` and `_claim_for_removal` (the shared core under `load`
and `unload`) each take a `hold: ConflictState | None` parameter now.
When given, the held state is written into `_conflicts` in the SAME `_lock`
hold that evicts (`_start_resident`'s non-request-path branch) or pops
(`_claim_for_removal`) the old worker — not after `load`/`unload` return.

`resolve_conflict` builds the `ConflictState` up front and passes it as
`hold=` to `load`/`unload`, instead of (as before) recording it in a second,
separate `with _lock:` block taken only after the call returned. That
earlier shape left the capability's conflict either absent or still naming
the stale pre-resolve standoff for the whole gap between the eviction/unload
and this function regaining the lock — up to `_terminate`'s ~9s-worst-case
teardown — a window another request-path load, or a second
`resolve_conflict`/`release_conflict` call, could race into and see
inconsistent state. The hold now stands from the instant the old worker is
popped, before its teardown even starts.

## Clearing an unheld conflict when its resident is removed

An unheld `ConflictState` names a standoff over a slot that still has a
resident (the loser's own request-path loads are the ones refused; the
resident keeps serving normally — see `ModelSlotConflict`'s message). Once
that resident is removed by ANY path — a manual unload, or the idle reaper
— the standoff it named is over, so `_claim_for_removal` now pops an
unheld conflict for every worker it removes, in the same locked block. A
HELD conflict (the user's own `resolve_conflict` call) is left standing
even when its resident worker is removed out from under it (idle reap, or
a manual unload of a capability currently held by `resolve_conflict`) —
only `release_conflict` lifts a hold; that is the one of two choices
`SPEC-model-slot-conflict.md` left open (expire vs. clear-on-removal), and
unheld conflicts are cleared on removal rather than on a timer, matching
how every other piece of this feature already reacts to table changes
rather than wall-clock deadlines.

## Scope: which capabilities can raise `ModelSlotConflict`

`generate_text`/`_generate_sync`'s request-path loads can hit
`_request_path_conflict` directly, closing the swap loop within one
capability's own retries. `embed` and `decide` do NOT share text
generation's capability key — `registry.EMBEDDINGS` ("embeddings") and
`registry.DECISIONS` ("text-classification") are both distinct from
`registry.TEXT_GENERATION` ("text-generation") — so each has its own,
independent slot and can open its own `ModelSlotConflict` the same way text
generation does, through the same `request_path=True` load inside
`_generate_sync`.

Image, video, speech, and transcribe all route their miss-handling load
through `_wait_ready`, which also now passes `request_path=True` (see
below) — so every capability that can go resident can, in principle, raise
a `ModelSlotConflict`. The render paths' loads are one-shot rather than a
request-triggered retry loop, so a standoff on one of THEIR capabilities can
only be seen by them, never closed by their own retries the way text
generation's tool-calling loop closes one on itself — but a hold (or a
standoff another caller already opened) now refuses their load too, instead
of silently evicting through a side door `_request_path_conflict` never saw.

A render's `ModelSlotConflict` cannot reach its HTTP route as a 409 the way
text/embed/decide's synchronous call can: `start_image`/`start_video`/
`start_transcribe` all open a job and load on a background thread, so the
route itself has already returned the job id by the time the load happens.
The exception instead reaches `_start_render`'s own catch-all the same way
every other load failure already does, and is reported onto the job as an
error with the conflict's own message — no new wire field, no 409 mapping
on these four routes. This is why `ModelSlotConflict`'s message text matters
for these paths in particular: it is the only place the standoff is
visible.

## An unheld conflict's message names every contender and the resident by role

`ModelSlotConflict`'s unheld message used to list only the non-resident
models and say "neither can finish loading" — wrong on both counts: the
resident keeps answering normally throughout a standoff (only the OTHER
contenders' request-path loads are refused), and excluding it from the
model list understated who is actually involved. The message now names
every model in the conflict (via a new `_join_names` helper, grammatical
for two names or three-plus with an Oxford comma) and, when a resident is
standing, says it explicitly stays loaded while naming who can't take over.

## One wire shape for a ModelSlotConflict's 409 body

`ModelSlotConflict.to_wire()` is the one place the 409 `error` object's
field list lives (`type`, `code`, `message`, `capability`, `models`,
`resident`, `held`). `server/ai.py`'s `_local_relay` and
`server/routers/ai_runtime.py`'s `_conflict_error` both wrap it in their own
envelope rather than building the object inline a second time.

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
