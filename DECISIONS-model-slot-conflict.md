# Decisions — model slot conflicts

Notes kept alongside `SPEC-model-slot-conflict.md`: places the implementation
had to make a call the spec left implicit, and why.

## Detecting contention by identity, not by the shape of a sequence (supervisor, §1)

`_request_path_conflict` (`fused_render/ai/supervisor.py`) keys contention on
WHO is asking, carried end to end as `requester_id`/`requester_label`
(`X-Fused-Requester`/`X-Fused-Requester-Label`, mirroring the existing
`X-Fused-Page`/`X-Fused-Source` two-header pattern — plain strings, not a new
dataclass, since this is attribution for contention detection, not
authorization). `Worker.requester_id` remembers who loaded it.

The rule: a request-path eviction by the SAME requester that already holds
the slot (`current.requester_id == requester_id`) is never contention — it
returns `None` before anything is recorded, which is what makes one
requester's own router+generator alternation (A→B→A→B, however fast) never a
conflict. A cross-requester eviction is checked against
`_swap_chains[capability]`, a pruned (`CONFLICT_WINDOW_S`) list of
`(time, requester_id)` entries recorded only from PRIOR ordinary
cross-requester evictions (both the evictor's and the evictee's id, so a
later eviction of EITHER one's model is caught). If the evicted model's
requester already appears in that history, the eviction opens a
`ConflictState` instead of being carried out; otherwise it is let through and
both ids are appended to the chain.

This catches two requesters fighting on the SECOND cross-requester eviction
(r2 evicts r1's model — ordinary, recorded; r1 tries to evict r2's model back
— r2 is in the chain since it is, trivially, its own entry — conflict), and
three requesters rotating A→B→C→A on the THIRD eviction (r3 evicting r2's
model conflicts because r2 already appears in the chain from evicting r1),
never waiting for an exact alternation or a literal return to the first
model. A one-off switch by a requester who never comes back stays ordinary
forever — its chain entry is never matched against because nothing ever asks
for ITS evicted model again.

`_swap_chains[capability]` is popped whenever a conflict opens (the
`ConflictState` is the record from then on) and by any explicit load/unload
(`_start_resident`'s non-request-path branch) — the user settling the
capability discards whatever history was building toward a conflict.

A contender that shows up while a conflict already stands is folded into the
existing `ConflictState.models`/`requesters` (model -> label) rather than
starting a second conflict, so a notification or dock row raised later names
everyone still asking for the capability, not just whoever collided first.

(This replaces an earlier version keyed on the alternating SHAPE of a
`(time, evicted_model, incoming_model)` chain rather than on identity — it
could not tell one caller's own retry loop from two callers fighting, which
is exactly the distinction SPEC-model-slot-conflict.md §1 now requires.)

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

## Clearing a conflict on removal, held or not: `explicit` vs internal callers

`_claim_for_removal` already popped an UNHELD conflict for every worker it
removed (the entry above), but `unload()`'s OWN post-`_remove` block used to
pop every conflict on an affected capability unconditionally whenever no
`hold` was given — including a HELD one — and did it after `_remove`'s
teardown returns, outside `_lock`. That made every caller of bare `unload()`
indistinguishable from `/api/ai/runtime/unload` itself: `evict_stale_engines`
(an engine preference changing under a resident model), the benchmark
teardown, and Monitor's Stop button all silently lifted a user's own
`resolve_conflict` hold just because they happened to remove the worker it
was about, and a caller racing into the gap between the eviction and the
clear could see a conflict that was about to vanish as still standing.

`unload()` now takes an `explicit: bool = False` parameter instead. Default
false matches every one of those three internal callers unchanged — no call
site edit needed for any of them — and still clears an UNHELD conflict on a
worker it actually removes, through `_claim_for_removal`'s existing
per-worker clear, which also now runs before the loop-less `return` leaves
the locked block to begin with (unchanged from the previous entry; `explicit`
is additive to it, not a replacement). `_claim_for_removal` clears EVERY
conflict (held or not) on the matched capability in that same locked block,
atomically with the eviction, instead of after `_remove`'s ~9s-worst-case
teardown, whenever `explicit=True`. `hold` still requires `explicit=True`
(asserted): a hold is only ever written by an explicit resolve, never by an
internal caller.

A default rather than a required keyword (the alternative this feature
first considered) was the simpler correct shape here: the three internal
callers' bug was never "they forgot to say what they were doing", it was
`unload()` treating the ABSENCE of `hold` as proof of user intent. A default
that means "not the user" fixes that reading directly and costs zero edits
to call sites that were already behaving correctly in every other respect.

`/api/ai/runtime/unload` is no longer unconditionally `explicit=True`: app
code reaches the same route via `fused.ai.models.unload`
(SPEC-model-slot-conflict.md §1), so the route reads `X-Fused-Requester` and
passes `explicit=supervisor.is_user_requester(requester_id)` — true only for
the AI Models page's own "Unload"/"Stop" control (`X-Fused-Requester: user`).
Every other requester's unload is the ordinary, non-explicit path above
(never clears a hold or conflict), and is additionally ownership-checked by
`unload()` itself: a non-empty, non-user `requester_id` may only remove a
worker IT loaded, and gets a 409 `ModelSlotConflict` instead of evicting
someone else's model by naming it. `/api/ai/runtime/load` mirrors this split
the other way: `X-Fused-Requester: user` is the one explicit, never-refused
load (`request_path=False`, clears any standing conflict); every other
requester's load is `request_path=True`, the same ordinary, contestable path
`generate_text`'s own miss-handling takes, so a background app calling
`fused.ai.models.load` cannot silently win a standoff just by using the
button's own route. `resolve_conflict`'s own `load`/`unload` calls pass
`requester_id=USER_REQUESTER_ID` explicitly, so the `Worker` a resolve leaves
resident is attributed to the user the same way a direct `/load` is.

## Clearing an unheld conflict on removal by a path other than `_claim_for_removal`

Two more places pop a worker straight out of `_workers` without going
through `_claim_for_removal` at all: `_bring_up`'s own exception handler
(a cold load that crashes or is cancelled) and `_drop_gone` (a process
`refresh_memory`/`ready_worker` finds already dead). Both now call the same
`_clear_unheld_conflict_locked(capability)` helper `_claim_for_removal`
itself calls, inside the SAME locked block that pops the worker — a shared
one-line helper rather than three copies of "pop if present and not held",
so a conflict naming a resident that is gone by ANY path is cleared the
same way regardless of which of the four removal sites did the removing.

## `/conflicts/resolve`'s `keep` gets `/load`'s own checks, plus one of its own

`resolve_conflict(capability, keep)` hands a non-null `keep` straight to
`load()`, which is exactly right internally — the SAME load a manual
`/api/ai/runtime/load` would start — but the router in front of it used to
validate only that `keep` was a string or `null`, not whatever `/load`
itself checks before calling `supervisor.load`. A request naming a model no
engine here can serve reached a cold-load failure instead of the 409
`_engine_gap_refusal` already gives `/load` for the same model id, and a
`keep` naming neither side of the standing conflict — a third repo that
happens to resolve to the same capability — was accepted and loaded as if
resolving the conflict, rather than refused as unrelated to it.

`/conflicts/resolve` now runs `_engine_gap_refusal(keep)` itself, and reads
the capability's own `ConflictState` first (`supervisor.conflict_for`, a
locked read added for this) to reject a `keep` that is not one of
`standing.models` with a 409 naming the models it could have picked instead.
Both checks are skipped when `keep` is `null` (nothing to validate) and when
no conflict is standing yet — `resolve_conflict`'s own docstring already
treats that case as a legitimate pre-emptive hold, so any engine-servable
model remains a valid thing to hold the slot for before a conflict exists
to belong to.

## The Python client gets its own `ModelSlotConflict`, not just `AiError` fields

`fused_ai.py` (the templated local client, D470-D472) already carried
`capability`/`models`/`resident`/`held` as four extra, only-sometimes-set
fields on the one `AiError` type, keyed on checking `.type ==
"model_slot_conflict"` by hand (CodeRabbit's finding on this PR). A caller
that wants to handle just this terminal case — distinct from every other
`AiError`, which a retry loop may still legitimately retry — had no type to
`except` for it, the same way `ServerNotRunning` already exists precisely so
"the app isn't running" and "the call failed" need no message-text sniffing
to tell apart.

`ModelSlotConflict(AiError)` is now that type: no new fields (the four
already on `AiError` are exactly what SPEC-model-slot-conflict.md §3
documents), just a name `_error_from_payload` constructs instead of `AiError`
whenever the wire's `error.type` is `"model_slot_conflict"` — the one place
that reads that string already, so no other caller changes. `_request` (the
one HTTP round trip every `fused_ai.text`/`embed`/... call goes through)
raises whatever `_error_from_payload` built, so the subclass reaches a
caller exactly where `AiError` always has, with no second wiring to keep in
sync.

## Frontend wording and withdrawal fixes (B3, B7, B8, B10)

**B7 — "stays loaded", not "neither can finish loading".** Both the toast
(`useModelConflictNotify.ts`) and the dock row's tooltip
(`ModelsDock.tsx`'s `ConflictRow`) used to say nobody was making progress,
which was never true: the resident keeps answering every request normally
throughout the standoff, only the OTHER contenders' loads are refused. Both
now name the resident ("`<names>` are fighting over `<capability>`: `<resident>`
stays loaded, the rest can't take over.") when one exists, falling back to
the plain "are fighting over" sentence for the one case with no resident at
all (a `keep: null` hold reached via `conflict_for`'s pre-emptive path —
`resident` is `null` there, not a contender).

**B3 — the toast withdraws on `held`, not only on disappearing.**
`useModelConflictNotify`'s own `notified` map used to check `conflict.held`
only the FIRST time a capability was seen; once a toast existed, every later
poll short-circuited on "already notified" before ever looking at `held`
again. A resolve that lands without the capability ever leaving
`runtime.conflicts` — it stays listed, just `held: true` with a new resident
— left the stale toast's "Keep"/"Stop both" choices sitting in the
Notifications panel after they had already been acted on. The loop now
re-checks `held` on every poll for an already-seen capability too, and
withdraws the RETAINED row (`dismissNotification`, the same call the
"conflict cleared" path already made) the moment that happens. The live
POPUP, if one is still showing, is left to its own normal timeout — exactly
how a disappearing conflict already behaved before this fix, since
`dismissNotification` has never touched the popup (see its own doc comment);
only `_resetNotificationsForTest`/the natural exit timer ever does.

**B8 — a third (or later) contender still gets its own Keep.**
`NotificationCard` has exactly two "Keep"-shaped slots (`navAction`/
`extraAction`) beside its one `liveAction`, so `ConflictRow` used to destructure
only `[first, second]` out of `conflict.models` — a third contender had no
way to be kept from this row at all. Rather than growing `NotificationCard`
a third+ slot (a component six other call sites share, for a case only this
row needs), the overflow rides in the row's existing `figures` slot instead,
as plain `.q-all`-styled buttons, one per remaining contender. `liveAction`'s
label also switches from "Stop both" to "Stop all" once there is a third
contender — "both" promises a pair.

**B10 — `releaseModelConflict`'s doc comment.** It claimed "Unlike
`resolveModelConflict` this DOES reply with the full runtime", directly
contradicting `resolveModelConflict`'s OWN comment two functions above it
("same as `releaseModelConflict`") — both routes answer with the full
runtime; there is no "unlike" left to describe. Reworded to say so plainly.

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

## The toast withdraws itself, and its actions report failure

`useModelConflictNotify.ts`'s "seen" bookkeeping now maps each notified
capability to the `notify()` id it raised (`null` for a held entry, which
never raises one) rather than a bare set of capability names. When a
capability drops out of `runtime.conflicts`, the cleanup loop that already
ran (to let a later, genuinely new standoff notify again) now also calls
`dismissNotification(id)` on the way out — a conflict resolved, released, or
cleared by a manual load/unload must not leave its "Keep `<model>`"/"Stop
both" row sitting in the Notifications panel offering choices that no
longer apply. (`dismissNotification` only ever clears a *retained* row per
its own doc comment — `tone: "error"` is always retained, per
`notifications.ts`'s `isRetained`, so every toast this hook raises has one.)

Both actions' `resolveModelConflict(...)` calls now carry a `.catch()`,
mirroring the dock row's own non-silent-failure contract (`ModelsDock.tsx`'s
`ConflictRow` `run()` wrapper). The toast pipeline has no live-updating
status slot the way `NotificationCard`'s `status` prop gives the dock row,
so a failure instead raises a second, plain error toast ("Could not keep
`<model>` — check your connection and retry.") rather than silently leaving
the click looking like it did nothing. Both actions also switch from
`refreshAiRuntime` to `publishAiRuntime` on success, for the same reason
Finding 7 switched the dock's handlers: `resolveModelConflict` always
replies with the full runtime now, so publishing it directly avoids a
poll-in-flight racing the resolve and republishing stale pre-resolve state.

## Every request-path client surfaces the same conflict fields

`fused_render/static/runtime.js`'s `fused.ai.embed` and `fused.ai.decide`
now copy `capability`/`models`/`resident`/`held` onto a thrown
`model_slot_conflict` error, exactly as `fused.ai.text`'s own `fail()`
already did — a page handling the embed or decide verb now gets the same
fields to build the same "keep A / keep B / stop both" choice the text path
already offered, rather than only the bare message. Pulled into one
`copyConflictFields(err, error)` helper (next to `cancelledError`/
`rethrowAbort`, the other small shared-error helpers in that file) rather
than left as three separate inline copies, so the three request-path verbs
cannot drift apart field by field the way the 409 body itself used to
(see the `to_wire()` entry above for the server-side half of the same
problem).

The shell's own `frontend/src/apps/ai_models/playground/client.ts` had the
equivalent duplication on the TypeScript side: the `error` object's wire
shape was spelled out as an inline object-literal type four times (once on
`throwRequestPathError`'s own parameter, once per `data` cast in
`streamChat`, `readEmbedReply`, and `decide`). Extracted to one
`RequestPathError` type used in all four places. The `ModelSlotConflict`
import in `client.test.ts:21` was checked against this change and is still
exercised (two tests assert `rejects.toThrow(ModelSlotConflict)` /
`toBeInstanceOf(ModelSlotConflict)`) — removing it, as the finding's own
wording suggested, would have been wrong; it stays.

## Resident-dot semantics unchanged by conflicts

`ModelsCardView`'s `idle`/`tone`/`StatusChip` computation stays keyed off
`models.length` alone, never `conflicts` — a held-empty conflict
(`keep: null`) correctly leaves the dot outlined (nothing resident) while
still drawing its own conflict row, because the row is informative content,
not a loaded model. The chip's only conflict-aware change is an additive
warning tint (`.has-conflict .dl-toggle { box-shadow: inset 2px 0 0
var(--warning); }`), layered onto whatever tone the resident count already
produced rather than replacing it.
