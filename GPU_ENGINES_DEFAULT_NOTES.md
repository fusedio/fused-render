# gpu-engines-default branch — build notes

Not the project's `DECISIONS.md`: that file is the canonical, D-numbered
project decision log (see its own header), and the brief for this branch
explicitly says not to invent a new D-number for this change. These are my
own process notes for review, kept separate so they never get mistaken for
an entry in that log. Delete this file before/at merge if the reviewer
doesn't want it kept.

## The policy reversal

`fused_render/ai/registry.py`'s `_RUNNERS` tuple order is the entire
`auto`-resolution mechanism (first row whose `available()` says yes, per
capability, wins). Until this branch, every accelerated row sat BELOW its
CPU sibling so `auto` could never reach it — GPU engines were opt-in only,
chosen from the Engines tab. The user reversed that policy: GPU-backed
engines are now preferred over CPU-backed ones.

Reordered (accelerated leads its CPU sibling in each family; `mlx-text`/
`mflux-image`/`mlx-embed` unchanged — already first, already the Apple
Silicon GPU row):

- text-generation: `mlx-text`, `llamacpp-text-vulkan`, `llamacpp-text`
- text-to-image: `mflux-image`, `diffusers-image-cuda`, `diffusers-image-rocm`, `diffusers-image`
- embeddings: `mlx-embed`, `onnx-embed-directml`, `onnx-embed-cuda`, `onnx-embed-rocm`, `onnx-embed`

Within each accelerated group, relative order is unchanged from before
(CUDA before ROCm for Diffusers; DirectML, then CUDA, then ROCm for ONNX
Embeddings — DirectML leads because it's the only one Windows can take and
it's vendor-neutral).

**The AMD radv over-commit hazard was explicitly accepted by the user.**
`llamacpp-text-vulkan`'s `_offload_schedule` over-commit backoff is known
not to engage on AMD — radv evicts other clients instead of erroring, which
took a desktop session down during testing (PR #706). The row's old comment
used this as the argument for keeping it opt-in; the user was shown that
hazard directly and chose the reversal anyway. The comment now records the
hazard as an accepted cost of the new default rather than an argument
against the ordering. Same treatment for `diffusers-image-rocm`'s "THE
SHARED RING" desktop-stall block — the kernel-log evidence stays, only the
framing sentence (why the row isn't a default) changed.

`code` values are unchanged on every row, so a stored engine preference
(`prefs.json`) still means what it meant — this is purely a reorder plus
comment rewrite, no field changed.

## Ripple — what needed updating, and what didn't

Checked (per the brief): `catalog.py`, `fit.py`, `runners/engine_options.py`,
`runners/formats.py`, `server/routers/ai_runtime.py`.

- **`catalog.py`**: `_SHARED_SUGGESTIONS` aliases each accelerated code to
  its unaccelerated sibling's curated model list (`diffusers-image-cuda` ->
  `diffusers-image`, etc.) — this is order-independent (a dict lookup), so
  no change needed there. But `runners_offering()` walks
  `registry.all_runners()` IN ORDER and returns the first-registered code
  first — that result changed for a shared-space model id (nomic-embed),
  now returning `onnx-embed-directml` first instead of `onnx-embed`. Fixed
  the one test that pinned the old first element
  (`tests/test_ai_catalog_embeddings.py`).
- **`fit.py`**: only names `llamacpp-text`/`llamacpp-text-vulkan` together
  as the fp16-precision exception set — order-independent, no change.
- **`runners/engine_options.py`**: exception-list keyed by code, no order
  dependency, no change.
- **`runners/formats.py`**: `DIFFUSERS_RUNNERS`/`LLAMACPP_RUNNERS`/
  `ONNX_EMBED_RUNNERS`/`DECISIVE` are all membership tuples (format
  evidence, not "what auto resolves to"), order-independent, no change.
- **`ai_runtime.py`**: no CPU-row/opt-in assumptions found in this file.

Docs/skill prose that stated the CPU-first policy in words, found by
grepping for "opt-in from the Engines tab" and similar and fixed:

- `docs/usage.md` — the "accelerated builds are opt-in" bullet, rewritten to
  describe the accelerated build as the default when the hardware is there.
- `skills/fused-render-ai/SKILL.md` — the capability table's "Runners
  (default first)" column and its prose (text-generation, text-to-image,
  embeddings rows), the image-memory/OOM guidance, the render-timeout
  guidance, and the "Two engines, one vector space" embeddings section.
- `frontend/src/apps/ai_models/local/RepoCard.tsx` — `DeviceNote`'s hint
  text and doc comment said the CPU engine "is the default off Apple
  Silicon"; reworded since a CPU-resident model now more often means either
  no supported GPU on the machine, or an explicit user choice on the
  Engines tab.

`DECISIONS.md` (the project's real decision log) has several historical
entries (D381, D416) that describe the OLD CPU-first policy as a decision
made at the time — left untouched deliberately, since that's an accurate
historical record of what was decided when, not a currently-stated policy
that needs to agree with today's registry.py. Same treatment as the D416
"torch on Windows/Linux" staleness the brief already flagged as out of
scope.

## Tests

Followed TDD: ran the affected test files, watched them fail against the
reordered table, then retargeted each failing assertion to the new
invariant (never deleted a test — every rename/rewrite keeps the original
test's intent, updated for the reversed policy). Files touched:

- `tests/test_ai_registry.py` — embeddings ordering assertions, the Windows
  auto-resolution test (now resolves to `onnx-embed-directml`), and a new
  `test_every_accelerated_row_leads_its_cpu_sibling` that pins the exact
  invariant the user asked for (accelerated index < CPU sibling index, for
  every family that has both) directly, independent of any other test.
- `tests/test_ai_runtime.py` — renamed/rewrote the ordering tests that
  pinned "auto stays on the unaccelerated row" and "vulkan sits below
  llamacpp-text", plus the two capability-wide ordering tests for text
  generation and embeddings.
- `tests/test_ai_catalog_embeddings.py` — `runners_offering()`'s first-match
  ordering assertion (see ripple section above).

`test_ai_catalog_overlay.py`, `test_ai_engine_options.py`,
`test_ai_formats.py` were in-scope per the brief but needed no changes —
ran clean against the reorder with no edits.

## Verified pre-existing/unrelated failures (not caused by this branch)

Ran the broader AI test surface (`test_ai_fit.py`, `test_hub_models.py`,
`test_ai_llamacpp_worker.py`, `test_shell_prefs.py`, `test_server_ai.py`,
`test_ai_models_api.py`, `test_ai_supervisor_hub_metadata_refresh.py`,
`test_ai_worker_base.py`, `test_ai_onnx_embed_real_weights.py`,
`test_ai_metrics.py`, `test_ai_runner_deps.py`, `test_build_model_mirror.py`,
`test_ai_benchmark_api.py`) plus the four already-listed. Two failures
showed up, both confirmed pre-existing by checking out the pre-branch base
commit (`efa073ae`) and rerunning the identical test in isolation — same
failure, no diff in the test file itself:

- `test_ai_metrics.py::test_a_missing_claude_binary_is_counted` — asserts
  `ai_error` vs `ai_unavailable` on a missing `claude` binary relay path,
  unrelated to registry ordering.
- `test_ai_worker_base.py::test_a_refused_body_this_cannot_frame_ends_the_connection`
  — a raw-socket HTTP framing test, flaky (`ConnectionResetError` on macOS),
  unrelated to registry ordering.

Both match the documented ~19-failure macOS local-suite baseline that
predates this branch.

## Ruled out

- Adding a D-number for the policy reversal — the brief explicitly says not
  to; referred to it throughout as "the policy decision" / "the GPU-first
  policy decision", cross-referencing the block comment above `_RUNNERS`
  rather than a decision-log id.
- Touching `DECISIONS.md` at all — see the ripple section above; it's
  historical record, not currently-stated policy, and the brief's own
  "do not add a D-number" instruction implies leaving that log's format and
  content alone.
- Changing any `code`, `label`, `short_label`, `family_label`, `_available`
  probe, or `folder` on any `Runner` row — brief said not to, and none of
  the ripple required it (every dependency keys off `code`, which is
  unchanged).
