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
- **Caller identity (requester).** Every model request carries WHO is
  asking: a stable `requester` id plus a human label. Sources:
  - pages/apps through `runtime.js` (`fused.ai.*`, `fused.ai.models.*`):
    the app/page the runtime is running in (its folder/route — whatever
    the runtime already knows about itself), sent as a header on every AI
    call;
  - Python apps / background apps through `templates/shared/fused_ai.py`:
    the app folder (or background-app engine id), same header;
  - chat / bots / Playground / other shell surfaces: their own fixed ids
    (e.g. `shell:playground`, `chat:<id>`), same header;
  - **the user**: only the shell's own conflict controls (dock row,
    notification actions, AI Models page load/unload buttons) send
    `requester: user`. Server-side, a request with no header is an
    anonymous requester (never "user").
  The header must be set by our clients, not trusted as authorization —
  it's attribution for contention detection, nothing more.
- **Conflict rule (identity-based):** per capability, keep a short history
  of request-path evictions within `CONFLICT_WINDOW_S` (60s), each as
  `(time, evicted_model, evicted_requester, incoming_model,
  incoming_requester)` where a worker remembers the requester that loaded
  it. A conflict opens when a non-user request would evict a model that a
  **different requester** loaded, and that requester's model was itself
  evicted by (or evicted) another requester within the window — i.e. two
  or more distinct requesters are taking the slot from each other.
  Consequences that must hold (tests):
  - one requester alternating A→B→A→B (router + generator) is NEVER a
    conflict, however fast;
  - two requesters each wanting their own model: conflict on the second
    cross-requester eviction (A by r1, B by r2 evicts A, A by r1 tries to
    evict B → conflict);
  - three requesters rotating A→B→C→A: conflict caught as soon as a
    requester's model comes back to evict another requester's within the
    window (no exact-alternation pattern needed);
  - a one-off switch by a different requester (r2 replaces r1's model
    once, r1 never comes back) is ordinary.
  On conflict: do **not** evict; put the capability into a *conflict*
  state and raise `ModelSlotConflict` (must NOT be a `ModelNotReady`).
- Conflict state per capability: `{capability, models, requesters (id +
  label per model), resident, since, held}`. The notification and dock row
  name the requesters ("Notes app and Chat are both trying to use the text
  model").
- **Every non-user path goes through the conflict logic.** That includes
  `fused.ai.models.load`/`unload` from app/page code (runtime.js and
  fused_ai.py) — `/api/ai/runtime/load` and `/unload` called with a
  non-user requester are treated as request-path: a load may be refused
  with `ModelSlotConflict` and counts toward detection; an unload by a
  non-user requester never clears a hold or conflict (it may unload only
  if the resident model was loaded by that same requester — otherwise
  409 conflict). Render paths (image/video/speech/transcribe) included.
- While a capability is in conflict or held, any non-user load for a model
  other than the resident one raises `ModelSlotConflict` immediately (no
  eviction). Requests for the resident model are served normally.
- Only `requester: user` actions clear or override a conflict/hold:
  `/load`, `/unload`, `/conflicts/resolve`, `/conflicts/release` from the
  shell's controls. Internal server callers (benchmark, evict_stale_engines,
  reaper, Monitor stop) never clear a held conflict. Hold enforcement lives
  in ONE place under `_lock` in `_start_resident` (a `respect_holds`-style
  check), not copied into callers like benchmark.py.
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
   evictions stay ≤ 3, one side gets `ModelSlotConflict`, and
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
