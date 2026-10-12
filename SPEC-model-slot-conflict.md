# SPEC — Model slot conflicts: detect the swap loop, stop it, let the user choose

## Problem

`fused_render/ai/supervisor.py` holds one resident worker per capability
(`_workers`, keyed by capability; eviction in `_start_resident`). The request
path loads on miss:

```python
# supervisor.py ~2742-2750 (and the duplicate ~2790-2798, and any other
# request-path sites that call load() on a miss — grep for them all)
worker = ready_worker(capability, model)
if worker is None:
    ...
    if current is not None and current.model == model:
        raise ModelNotReady(...)
    started = load(model, capability)      # evicts whatever is resident
    raise ModelNotReady(f"{model} is loading now", started["jobId"])
```

Two consumers wanting different models of one capability (two apps, a
background app + chat, …) each evict the other on every retry. The retry
contract in `fused_render/static/runtime.js` (~276-298) and
`fused_render/templates/shared/fused_ai.py` (~506-568) has no cap, so it
loops forever. Only the Playground (`frontend/src/apps/ai_models/playground/client.ts`,
`withModelReady`, `MAX_LOAD_WAITS`) bounds it. There is no detection, no
warning, and no way for the user to pick a winner.

## Design (decided — build this)

### 1. Supervisor: detect contention, stop auto-evicting

- Track, per capability, the request-path evictions: when a request-path
  `load()` would evict a *different* resident/loading model, record
  `(time, evicted_model, incoming_model)`.
- **Conflict rule:** if a request-path load would evict a model that itself
  was brought in by a request-path load within `CONFLICT_WINDOW_S` (60s) —
  i.e. the slot is being swapped back and forth — do **not** evict. Instead
  put the capability into a *conflict* state and raise a new
  `ModelSlotConflict` error (subclass whatever the request path's errors
  subclass; must NOT be a `ModelNotReady`, so callers don't retry).
  The rule must catch A→B→A ping-pong on the second swap; a single
  one-off switch (A then later B) must still just work as today.
- Conflict state per capability: `{capability, models: [ordered distinct
  models that contended], resident: <model or None>, since, requesters?}`.
  Include who asked (app/page/bot identity) only if the request path
  already has that cheaply; don't plumb new identity through for it.
- While a capability is in conflict, request-path loads for any model other
  than the resident one raise `ModelSlotConflict` immediately (no eviction).
  Requests for the resident model are served normally.
- Explicit user actions are never blocked: `POST /api/ai/runtime/load`
  (manual load) and `/unload` keep working and **clear** the conflict for
  that capability (the user made a choice).
- Expose conflicts in `supervisor.describe()` (new `conflicts` list) so
  the existing runtime status endpoint/stream carries them.

### 2. API: resolve

- `POST /api/ai/runtime/conflicts/resolve` in
  `fused_render/server/routers/ai_runtime.py`, body
  `{capability, keep: "<model>"}` or `{capability, keep: null}`:
  - `keep: model` → make that model resident (load if needed) and **hold**
    the slot for it: other models' request-path loads keep getting
    `ModelSlotConflict` (message names the held model and tells the user
    how to change it) until the user loads/unloads manually or the hold is
    released.
  - `keep: null` → stop everything: unload the capability and hold it
    empty — request-path loads for that capability are refused with
    `ModelSlotConflict` until the user acts.
  - `POST /api/ai/runtime/conflicts/release {capability}` clears a hold /
    conflict without loading anything (back to normal auto-load).
- HTTP mapping: `ModelSlotConflict` → **409** with a JSON body that
  carries `code: "model_slot_conflict"`, the capability, the competing
  models, and a human message. Wire it wherever `ModelNotReady` is mapped
  to its HTTP response today.

### 3. Clients stop retrying on conflict

- `fused_render/static/runtime.js`: on the 409 conflict code, reject with a
  clear error instead of watching+retrying. Same for
  `fused_render/templates/shared/fused_ai.py` (raise a distinct exception,
  exported, not a retry).
- Playground `withModelReady`: treat the conflict as terminal and show the
  server's message.
- Update the docs that describe the retry contract (the `fused-render-ai`
  skill under `skills/`, and any docstrings) to describe the conflict
  outcome.

### 4. UI: warn and give control

- **Notification:** when a capability enters conflict, post a notification
  using the existing actionable-notifications system (see
  `SPEC-actionable-notifications.md` and the code it points to) — e.g.
  "Two things are fighting over the text model: org/a and org/b. Neither
  can finish loading." with actions **Keep org/a**, **Keep org/b**,
  **Stop both**. Actions call the resolve endpoint. One notification per
  conflict episode (no spam on every refused request).
- **Models status-bar chip / popover** (`frontend/src/shell/ModelsDock.tsx`):
  show a conflict row per conflicted capability with the same three
  choices, and when a hold is active show it ("Held for org/a — Release").
  Constraints from the user: Models stays its own chip whose dot fills
  whenever a model is resident; sibling popover panels share one fixed
  width (340px cap) when non-empty. A conflict may add a warning tint to
  the chip, but must not replace the resident dot semantics.

## Tests (TDD — write first)

In `tests/test_ai_runtime.py`, with the existing fake runner:

1. **Ping-pong is bounded:** two clients loop on the request path
   (`generate_text` or the shared helper), one with `org/a`, one with
   `org/b`, each retrying on `ModelNotReady` like runtime.js does. Assert
   evictions stay ≤ 2, one side gets `ModelSlotConflict`, and
   `describe()["conflicts"]` names both models.
2. A single switch A → (later, outside the window or not ping-pong) B
   still evicts and loads B with no conflict.
3. Manual `load`/`unload` clears the conflict.
4. Resolve `keep: model` → that model resident, other model's requests get
   409/`ModelSlotConflict` without evicting; `release` restores auto-load.
5. Resolve `keep: null` → capability unloaded and refused until release.
6. API test for the 409 body shape (wherever ai_runtime router tests live).
7. Frontend: unit test for ModelsDock conflict row / actions if the
   frontend has a test harness for shell components; Playground conflict
   is terminal.

## Out of scope

- Running two models of one capability at once (memory arithmetic says no).
- Per-app model declarations in manifests.
- `engine_host.py` / background-app process engines (unrelated system).

## Repo notes

- Use `bun`/`bunx`, never npm/npx.
- Full pytest locally is noisy (tmpfs quota); run scoped tests only:
  `pytest tests/test_ai_runtime.py -k <name>`. If tmp quota errors appear,
  `rm -rf /tmp/pytest-of-iamsdas` or set `TMPDIR=$HOME/.cache/pytest-tmp`.
- Comments/PR text describe the code as it is — no "previously"/"before
  this change" history.
- Record decisions and deviations in `DECISIONS-model-slot-conflict.md`.
