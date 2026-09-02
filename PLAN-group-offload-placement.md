# Group-offload placement for the diffusers image runner

Source: shared artifact 659284ce-6996-48e9-8f7e-170ac18fab4f

**SUPERSEDED IN PART — see `DECISIONS.md` (D636 and the Task 2 note
following it) for the full record.** Read this plan for Task 1's original
reasoning, but treat the following as corrected or overtaken, not as
written below:

* **Task 1 shipped with one more exclusion than this plan specced.**
  `enable_group_offload`'s `exclude_modules` also drops `text_encoder`
  (when the pipeline has one), not just `vae` — the klein recipe's
  bitsandbytes NF4 text encoder keeps its dequantization state
  (`quant_state`) as a plain Python attribute invisible to `ModuleGroup`,
  which group offload's onload path would move `.data` without ever
  calling `Params4bit.to()` for, producing a device-mismatch crash on the
  first forward. A `Qwen3ForCausalLM`'s direct children are also not
  `ModuleList`s, so block-level offload would buy nothing there even
  without the crash. This plan's Task 1 section below does not mention
  this component at all.
* **Task 2, disk residency via `offload_to_disk_path`, is CANCELLED as
  specced here — not merely gated on the hardware run this plan asks for.**
  This plan's own risk note (below, "Quantized weights may not survive the
  disk round-trip") correctly flagged a correctness risk for quantized
  weights, but understated the memory finding: the text encoder excluded
  above is the DOMINANT component of the measured 11.7 GiB idle baseline
  (7.5GB bf16 / ~3.75GB NF4, against a 2.6GB GGUF transformer and a 0.17GB
  VAE) and, being excluded from group offload entirely, could never reach
  `offload_to_disk_path` regardless of how the gate below turns out. The
  safe subset this task could ever apply to — the GGUF transformer alone —
  does not move the number the maintainer cares about. Do not implement
  Task 2 as written below.
* **What happens instead is not yet decided.** Freeing the text encoder
  after `encode_prompt` (compute embeddings once, drop the module, reload
  from the HF cache on the next render) was investigated as a way to reach
  the dominant component, but is not the chosen direction — the maintainer
  wants disk-backed mmap residency instead, the same shape MLX already gets
  for its own models. Whether that is achievable for this pipeline's
  quantized components (a GGUF transformer, a bnb NF4 text encoder) is a
  separate, open investigation. Task 3 (forwarding placement through the
  supervisor, D635) is unaffected by any of this and already shipped.

Group-Offload Placement 
 Implementation plan · fused-render
 Group-offload placement for the diffusers image runner
 Cut the image worker's idle system-RAM residency after a render, without giving up the offload path that lets big models run on small cards.
 _place() makes a binary choice today: everything on the GPU, or accelerate's enable_model_cpu_offload() , which parks every weight in system RAM as dirty anonymous pages for the life of the process — the measured 11.7 GiB. We add a third rung between them using diffusers' own enable_group_offload , which offloads at block granularity and can keep weights on disk instead of in RAM. Plain offload stays as the unconditional fallback.
 gate run before Task 2
on the RX 9060 XT
 Quantized weights may not survive the disk round-trip
 Blocking — resolve before writing Task 2
 _check_disk_offload_torchao guards only TorchAO tensors. _offload_to_disk then serializes with save_file({key: tensor.data}) — and .data strips the subclass. Our klein-4B recipe is a GGUF transformer plus a bnb NF4 text encoder, both Tensor subclasses carrying quantization state out of band. The expected failure is a wrong image, not an exception. 
 Render one prompt and seed three ways: today's enable_model_cpu_offload , enable_group_offload with no disk path, and with one.
 Compare the three images. If the third diverges, disk residency is excluded for quantized components — Task 2 narrows to the VAE alone, or is dropped.
 Record Private_Dirty from /proc/<pid>/smaps_rollup 30 s after each render. If today's figure already reads mostly Private_Clean , the memory is reclaimable and this plan should be abandoned .
 Time one render per configuration, with and without use_stream , to settle the stream question in Task 1 by measurement rather than preference.
 torch_image.py _place() · 371
decision · 479-502
 The placement ladder
 all-gpu
 total_bytes + headroom ≤ free
 pipe.to(device) — nothing streamed per render.
 Unchanged. torch_image.py:490-497 
 group-offload
 does not fit → new rung
 pipe.enable_group_offload(...) at block level, optionally with a disk path.
 Task 1 adds it in memory mode; Task 2 adds disk residency.
 offload
 group offload raised, or probe raised
 pipe.enable_model_cpu_offload() — today's behaviour.
 Unchanged, and still the terminal fallback. torch_image.py:498-502 
 baseline RssAnon 11.7 GiB
RX 9060 XT · 15.9 GiB
klein-4B ROCm GGUF
 Where the bytes sit, 30 seconds after a render
 offload (measured today) 
 11.7 GiB
 group-offload, memory
 ~same
 group-offload, disk
 target
 Private_Dirty — anonymous, unreclaimable 
 Private_Clean — file-backed, evictable 
 hatched = estimate, not measured 
 Memory-mode group offload lowers the VRAM ceiling during a render but still holds every weight in host RAM afterwards. Only the disk path converts those pages to clean, file-backed ones. Task 1 alone therefore does not move the idle number — it is the safe scaffolding Task 2 needs.
 tasks ordered · gated
one commit each
 Tasks
 Task 1 
 Add the group-offload rung, memory mode only
 Modify
 fused_render/ai/runners/torch_image.py:479-502 — insert the new rung between the all-gpu move and the plain-offload fallback, reusing the sizes and total_bytes probe already computed above it.
 torch_image.py:325 — add a num_blocks_per_group knob following the _vram_headroom_bytes() env-var pattern exactly, including its rejection of absurd values.
 torch_image.py:371 — extend the docstring with the new case, and state why the removed hot-gpu case's accelerate hook-chain reasoning does not apply here.
 Test
 tests/test_ai_diffusers_worker.py:515-750 — extend _fake_torch_for_placement and _placement_pipe with an enable_group_offload spy, then assert the bucket chosen and kwargs passed for: fits, does not fit, group offload raises, probe raises. Test-first.
 Verify
 pytest tests/test_ai_diffusers_worker.py -q
 Commit
 Image runner: group-offload rung between all-gpu and plain offload
 Task 2 
 Disk residency for the group-offload rung
 Gated 
 Modify
 torch_image.py — pass offload_to_disk_path in the Task 1 call, scoped to whichever components the gate cleared.
 torch_image.py:156 — add a cache-key helper beside _recipe() deriving the directory from model id plus the recipe that produced the weights.
 Create
 Nothing. The directory lives under the existing ~/.fused-render/cache root; find how other runners resolve that root rather than introducing a path constant.
 Test
 tests/test_ai_diffusers_worker.py — unit-test the cache-key helper: a changed recipe produces a different directory, the same recipe the same one. Assert the path reaches enable_group_offload via the Task 1 spy.
 Verify
 pytest tests/test_ai_diffusers_worker.py -q
 Then on the ROCm box: render, wait 30 s, confirm Private_Dirty has dropped against the gate's recorded baseline.
 Commit
 Image runner: disk residency for group-offloaded weights
 Task 3 
 Forward placement past the supervisor
 Modify
 fused_render/ai/supervisor.py:242-256 — add a placement field to Worker , following resident_bytes and its neighbours.
 supervisor.py:1159-1170 and supervisor.py:2734-2761 — lift placement out of the health response at both sites that read it.
 supervisor.py:2829 — emit it from describe() beside residentBytes .
 Test
 Whichever module already covers describe() — assert a worker reporting a placement surfaces it, and one reporting none stays None .
 Verify
 pytest tests/test_ai_diffusers_worker.py tests/test_ai_registry_tags.py -q
 Commit
 Supervisor: surface worker placement through describe()
 decisions & risks
 Key decisions & risks
 Group offload replaces plain offload, never stacks on it. _raise_error_if_accelerate_model_or_sequential_hook_present makes them mutually exclusive systems — diffusers' own hooks versus accelerate's.
 The removed hot-gpu case is not this case. That branch failed on accelerate's offload chain ; group offload uses no accelerate hooks, so none of the five recorded defects transfer. The docstring must say so, or the next reader assumes this was already tried and rejected.
 Task 1 does not fix the idle number on its own and should not be described as if it does. It buys a lower VRAM ceiling and the scaffolding for Task 2.
 use_stream is settled by the gate's measurement, not by preference. With streams the disk path pins a fresh host buffer per group per step; without them it loads straight to the device and retains nothing on the host — which is what the idle goal wants.
 The disk cache persists deliberately. _offload_to_disk skips the write when the file exists, so a stable path doubles as a warm-start cache.
 A stale cache directory would load wrong weights silently. That is why the key includes the recipe, not just the model id. Nothing evicts this directory today; the growth is real and left unaddressed here.
 Streams on ROCm are unverified. diffusers creates the stream behind torch.cuda.is_available() , which HIP satisfies; whether pinning and record_stream behave there is a question only the gate can answer, on the one card we can measure.
 CI cannot reach any of this. No GPU on the runners, so the committed tests cover the decision and the cache key. Offload behaviour is machine-verified or not verified at all — a green suite is not proof.
 No parallel implementations to update. enable_model_cpu_offload appears only at torch_image.py:498 and :501 ; the three runner folders share this module by design, and the video runners use no diffusers offload.
 Deferred: tuning num_blocks_per_group per model class, and evicting the disk cache. Both need numbers we will not have until Task 2 has run for a while.
 proof machine-verified
not CI
 How we'll know it works
 On the RX 9060 XT: load FLUX.2-klein-4B, render, wait 30 seconds, read Private_Dirty from the worker's smaps_rollup . It should sit materially below the 11.7 GiB recorded in tests/test_ai_diffusers_worker.py:515-530 , with the freed pages showing as reclaimable rather than merely moved.
 The same prompt and seed produce the same image as before the change — the gate's comparison, re-run once the code has landed.
 A second launch against a warm cache directory reaches its first render faster than a cold one, confirming the persisted groups are reused.
 And the AI Models page can say which of the three placements a worker actually chose, which it cannot today.
