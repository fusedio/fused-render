# Model-derived engine resolution and per-model engine pins

Written 2026-10-20. Follows the engine-pin-bump PR (branch `surya/engine-pin-bumps`); land that first, then build this as a SEPARATE PR from a fresh worktree off main (never stack).

## Goal

Engine choice follows the model instead of one global per-capability switch: a downloaded model runs on whichever installed engine can read it, the user can pin a single model to an engine, and the layer that admits a repo agrees with the layer that loads it. Approach: make the format classifier the single source of "which runners read this repo" (it already exists in `formats.loaders()` and is cached per repo), resolve the runner per request from `(capability, model)`, and store per-model pins beside the existing per-capability pref so the same "a pin that cannot be honoured is ignored with a reason" rule (D302) covers both.

Two PRs, built sequentially: **PR A = Tasks 1, 2, 3** (detection and loading agree; FLUX 3 loads), **PR B = Tasks 4, 5, 6** (model-derived resolution and pins). PR B has no code dependency on PR A beyond main.

TDD throughout: write the scoped test first, watch it fail, implement, watch it pass. Scoped tests only in the inner loop; the orchestrator runs the full suite once.

## Tasks

### Task 1 — one diffusers predicate for admission and loaders

- Modify `fused_render/ai/runners/formats.py:2205`: the branch `if DIFFUSERS_INDEX in names` is the bug. It must accept a snapshot that has only `modular_model_index.json` (`DIFFUSERS_MODULAR_INDEX`, already defined at `formats.py:367`) or whose `config` carries `library_name == "diffusers"`, matching what `hub_architecture.is_diffusers_repo` (`fused_render/ai/hub_architecture.py:282-294`) already admits. Put the predicate in `formats.py` (lowest layer; it has `names` and `config` in scope at the `loaders()` signature, `formats.py:2117`) and make `hub_architecture.is_diffusers_repo` delegate to it so there is one implementation, not two that drift.
- Check `hub_loadable._admits` (`fused_render/ai/hub_loadable.py:282-320`) still calls the same predicate after the move; it should need no change beyond the import.
- Test `tests/test_ai_formats.py`: a snapshot with `modular_model_index.json` and no `model_index.json` yields `DIFFUSERS_RUNNERS`; so does one with only `library_name: diffusers` in config. Follow the existing diffusers-index cases in that file.
- Test `tests/test_ai_hub_loadable.py`: a cross-layer invariant test iterating the file's existing admitted fixtures: whenever `loadable_kind` admits a repo, `formats.loaders()` on the same names/config is non-empty. This is the test that would have caught the klein 3 report.
- Verify: `pytest tests/test_ai_formats.py tests/test_ai_hub_loadable.py -q` green.
- Commit: `formats: admission and loaders share one diffusers predicate (modular index counts)`.

### Task 2 — ModularPipeline dispatch in the diffusers image runner

- Modify `fused_render/ai/runners/torch_image.py:556-558` (the generic `AutoPipelineForText2Image.from_pretrained` fallback in `load`, `torch_image.py:519`). When the fetched snapshot has `modular_model_index.json` and no `model_index.json`, build the pipeline through diffusers' `ModularPipeline` instead; `AutoPipelineForText2Image` cannot open a modular repo. Wrap the modular object in a tiny adapter inside `torch_image.py` so the generate call site (the code that calls `pipe(...)` and reads `.images`, further down in the same file) is unchanged. The builder must probe the exact `ModularPipeline` load-and-call sequence against diffusers 0.41 in a scratch venv (`/tmp/pinprobe-diffusers`) before writing the adapter; see Decisions.
- Modify `_place(pipe)` (same file, just after :558) only if the modular object lacks `.to`; the probe tells.
- Test `tests/test_ai_diffusers_worker.py`: with the fake `diffusers` module pattern that file already uses, a modular-only snapshot calls `ModularPipeline`, a `model_index.json` snapshot still calls `AutoPipelineForText2Image`.
- Add FLUX 3 klein catalog rows in `fused_render/ai/catalog.py` next to the FLUX.2 klein rows (`catalog.py:679-860`), with per-row `"defaults"` as at `catalog.py:676`: distilled variant 16 steps and no guidance; base variant 50 steps. Ids: `black-forest-labs/flux-3-image-klein` (and the base id the builder confirms on the Hub). Test: the existing catalog tests in `tests/test_ai_catalog*.py` that assert each row has a runner and defaults.
- Verify: `pytest tests/test_ai_diffusers_worker.py tests/test_ai_catalog*.py -q`.
- Commit: `torch_image: load modular diffusers repos via ModularPipeline; FLUX 3 klein catalog rows`.

### Task 3 — mflux variants derived from the installed mflux, not a two-entry table

- Modify `fused_render/ai/runners/formats.py:197-210` (`MFLUX_VARIANTS`), `:239` (`MFLUX_EDIT_VARIANTS`), `:319` (`MFLUX_FILL_VARIANTS`) and `fused_render/ai/runners/mflux_image/worker.py:194` (`_recipe_for`, which does `_VARIANTS.get(model_id)`). `hub_loadable.mflux_loadable_repos` (`hub_loadable.py:206`) already ast-parses the installed mflux's `AVAILABLE_MODELS`; extend that parser to return the variant/config name per repo id, and have `_recipe_for` build its recipe from that plus the repo's `base_model` tag. Keep the hand tables only for ids mflux does not list (the mlx-community quantised repos), as overrides layered over the derived map.
- The refusal messages at `worker.py:216-242` and the raise at `worker.py:354-359` stay; their condition becomes "not in derived map and not in override table".
- Test `tests/test_ai_mflux_worker.py:762` (unknown-id refusal) keeps passing; add a case where a repo id present only in a fixture `AVAILABLE_MODELS` source resolves to a recipe. Test `tests/test_ai_hub_loadable.py` for the extended parser output shape.
- Verify: `pytest tests/test_ai_mflux_worker.py tests/test_ai_hub_loadable.py -q`.
- Commit: `mflux: derive variants from the installed mflux AVAILABLE_MODELS; tables become overrides`.

### Task 4 — per-model engine pin pref

- Modify `fused_render/shell/prefs.py`: add `model_engines()` beside `inference_engines()` (`prefs.py:455`), stored under a new top-level prefs key `"model_engines"` mapping repo id to runner code. Validation reuses `_valid_engine_choice` (`prefs.py:493`); the capability to validate against is `hub_cache.cached_capability(repo_id).capability` (`fused_render/ai/hub_cache.py:1573`). Reject an id that is not in the cache (nothing to pin). Extend `_inference_engines_state` (`prefs.py:915`) with a `models` list: repo id, requested code, resolved code, reason, following the per-capability entry shape right above it.
- Add the PUT handler as a sibling of the per-capability engine PUT in the same module (locate it by the `_valid_engine_choice` caller in `prefs.py`), same header and error shape.
- Test `tests/test_shell_prefs.py`: copy the pattern at `:1045` (round-trips and persists) and `:1078` (a pin naming a withdrawn engine still serves, with a reason) for the model key.
- Frontend: `frontend/src/platform/lib/api.ts:1611` (`EnginesPrefs`) gains `models`; `frontend/src/apps/ai_models/engines/EnginesTab.tsx` renders, under each capability, the downloaded models of that capability with the existing `EngineSelect` (`frontend/src/apps/ai_models/engines/EngineSelect.tsx`, props at `:108-130`) and the "ignored: reason" text the capability row already shows. Note that pytest greps frontend source text (`tests/test_ai_models_api.py` and the bun tests under `frontend/src/apps/ai_models/lib/`); run `bun test frontend/src/apps/ai_models` scoped.
- Verify: `pytest tests/test_shell_prefs.py tests/test_ai_models_api.py -q` and `bun test frontend/src/apps/ai_models`.
- Commit: `prefs: per-model engine pins beside the per-capability preference`.

### Task 5 — resolve the runner from (capability, model)

- Add `resolve_for_model(capability, model_id)` to `fused_render/ai/registry.py` next to `resolve` (`registry.py:1876-1914`), returning the same `Resolution` dataclass (`registry.py:1822`). Order: (1) per-model pin from Task 4 if that runner is available; (2) candidates = available runners for the capability whose code is in `cached_capability(model_id)`'s loader set (`CacheReading`, `hub_cache.py:1382-1415`, carries `runner_code`/`runner_reason`; use the full `meta.loaders` tuple at `hub_cache.py:440`, not only the first code); the global per-capability pref wins if it is among the candidates, else registry order; (3) uncached or no candidates: fall back to `resolve(capability)`. Every non-default outcome carries a `reason`, exactly as `resolve` does for an ignored pref.
- Wire every `for_capability` call that has a model in hand; all siblings, in lockstep: `fused_render/server/routers/ai_runtime.py:2008` (image), `:2391` (video), `:2734` (STT), `:3435` (vision); `fused_render/ai/supervisor.py:1656` (the shared runner lookup; give it an optional `model` parameter), `:2697`, `:3299` (the `resolved.code != worker.runner_code` conflict check must compare against the model-derived resolution or it will restart workers forever).
- Delete the video-only guard at `ai_runtime.py:2418-2425` (the comment block "Naming a model explicitly does NOT pick its runner" at `:2394-2417` with it): model-derived resolution subsumes it. Its test in `tests/test_ai_runtime.py` flips from "refused with a hint" to "served by the other runner".
- Update the `registry.py` module docstring (`:45-60`) and the `prefs.py` header (`:13-17`): the per-capability pref is now the tie-breaker among runners that read the model, no longer the sole selector.
- Test `tests/test_ai_runtime.py`: the existing `registry.resolve` cases plus: a cached model only one available runner reads is served by that runner despite a pref naming another, with reason; a per-model pin wins over the pref; a pin naming an unavailable runner is ignored with reason and the model still runs; an uncached id behaves exactly as today.
- Append a DECISIONS.md entry (append-only; next id after D1383) recording the rule change relative to D302.
- Verify: `pytest tests/test_ai_runtime.py tests/test_shell_prefs.py tests/test_ai_supervisor*.py -q`.
- Commit: `registry: resolve the runner from (capability, model); pref is the tie-breaker`.

### Task 6 — show the reason on the model card

- `fused_render/server/routers/hub_models.py:1341` (`_model_row`) already has the `CacheReading`; emit `runner_reason` beside the existing engine field, and `frontend/src/apps/ai_models/lib/loadableChip.ts` renders "admitted, but no installed engine reads this format" (or the pin's ignored reason) instead of a bare missing engine. Follow the chip's existing variants and its test `loadableChip.test.ts`.
- `_active_engine_format` (`hub_models.py:515`) uses `for_capability`; for cached rows switch it to `resolve_for_model` so the ranking's "native format" matches the engine that will actually serve.
- Verify: `bun test frontend/src/apps/ai_models/lib` and `pytest tests/test_hub_models.py -q`.
- Commit: `hub models: card shows why no engine serves a repo; format axis follows the serving runner`.

## Decisions & risks

- Two sequential PRs, not one: Tasks 1-3 are bug fixes that stand alone; Tasks 4-6 change the resolution rule (D302) and deserve their own review. Never stack them.
- Per-model pin outranks the loader classifier on purpose: the pin is the escape hatch for classifier gaps, and the runner's own load refusal (e.g. `worker.py:354`) remains the backstop. A pin naming an unavailable runner is ignored with a reason, never an error, mirroring the per-capability rule.
- `ModularPipeline`'s load/call sequence in diffusers 0.41 is unverified in this repo (nothing greps for it today). The builder probes it in a scratch venv before writing the adapter; if it needs more than a load-components-then-call adapter, stop and report rather than guess. FLUX 3 is CUDA-only per the vendor blog; the Apple path for FLUX 3 is mflux (bump PR).
- The shape of mflux 0.23's `AVAILABLE_MODELS` values (variant class vs config name) is unverified; the builder reads `model_config.py` in the bump worktree's venv before extending the parser.
- Deleting the video-only guard removes a tested behaviour; the replacement test must assert the model now runs on the other runner, not merely that no error is raised.
- Rejected: a hand-maintained repo-id to engine table. That is the pattern that broke (2-entry mflux table, 1-entry GGUF recipe table, 5-entry component table).
- Rejected: dropping the per-capability pref entirely. It still decides among several runners that all read a model (mflux vs diffusers for a FLUX.2 repo on a Mac).
- Not in scope: llama-cpp-python pin, GGUF text runner recipes, and any engine not already in the registry.

## How we'll know it works

- On a CUDA machine with the diffusers engine installed, downloading `black-forest-labs/flux-3-image-klein` shows an engine on the card, Load succeeds, and a prompt renders at 16 steps with no guidance prompt in the UI.
- On an Apple machine with the image engine pref set to mflux, loading a model only diffusers reads runs it on diffusers; the Engines tab shows the capability pref unchanged and the model row's resolved engine with its reason.
- Pinning a model to an engine that is not installed shows "ignored: not installed" on that row and the model still generates on the engine that reads it.
- A downloaded repo no installed engine reads says so on its card instead of showing a Load button that fails.

## Build notes (Tasks 4-6, branch surya/model-engine-resolve)

- Task 4: no separate PUT handler for pins existed; `model_engines` was added to the single `PUT /api/prefs`. Pins are only accepted for cached, capability-bearing ids.
- Task 5: needed `CacheReading.loaders` (it lacked the full loader set) and a `resolve_for_loaders` helper so `hub_cache._engine` avoids recursing into itself. Many tests stubbing `registry.for_capability` also needed a `resolve_for_model` stub.
- Task 6 deviation: no code change. `_model_row` is a Hub-search row builder with no `CacheReading`; resolving per downloaded search row reads repo metadata, which `test_the_join_costs_what_the_results_cost_not_what_the_cache_costs` forbids. Downloaded cards already render `engine.reason` (aiModelGroups.ts), which Task 5 made follow the serving runner. `_active_engine_format` stays capability-level. If the ranking should follow the serving runner, it needs a metadata-free source (e.g. a cheap local loaders index) first.
- DECISIONS.md: D1387.
