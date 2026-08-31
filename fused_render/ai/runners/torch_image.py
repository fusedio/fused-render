"""Text-to-image on diffusers: one resident pipeline, four routes (SPEC §40).

**This module is the whole of the runner and it sits at the runners ROOT**,
beside `worker_base.py`, `formats.py` and `preview.py` — which is the rule
`preview.py` states about itself, applied one level out. THREE folders serve this
one engine — `diffusers_image/`, `diffusers_image_cuda/` and
`diffusers_image_rocm/` — and they differ only in which index their
`pyproject.toml` takes torch from. CPU is the default, CUDA and ROCm are variants
a user picks on the Engines tab, and the hardware they select is a fact about the
wheel, never about the code. Each folder's `worker.py` is a five-line shell around
`torch_image.main()`; a second copy of the pipeline recipes, the GGUF swap or the
preview wiring under any of them would fail no test, because each copy would pass
its own.

The image counterpart of `mlx_text/worker.py`, and the same shape: the HTTP
contract, the download reporting and the state machine are `worker_base`'s, and
what lives here is only what is true of diffusers in particular — which pipeline
class to build, which device to put it on, and how a denoising loop reports
itself.

Two things make this different from the text runner, and both are deliberate:

* **`/generate` answers with ONE JSON object, not a stream.** An image is an
  artefact, not a sequence of tokens; there is nothing to stream until it is
  finished. Its progress is DENOISING STEPS, and those go to the job row the
  caller is already watching (`body["job"]`) — the same download manager that
  shows the weights arriving shows the picture being made.
* **The PNG is written by this process to a path the SERVER chose** (`body["out"]`).
  The server owns where user files go; this process owns the pixels. It never
  invents a location.

**Cancelling works through the job row.** `pipe()` is one opaque C call with no
interruption point, so the only place a stop can be honoured is the per-step
callback diffusers gives us — and the reply to the progress tick we were sending
anyway is how the manager's ✕ reaches a process that cannot look at anything
else. That is why `report_or_cancel` returns the record.

**Quantized single-file transformers.** The one model this runner ships a recipe
for — FLUX.2 klein — is unusable in full precision on the machines this app runs
on: the bf16 transformer is ~8GB and OOMs a 16GB Mac. The recipe swaps in a
~2.6GB Q4_K_M GGUF for that one component and takes the rest of the pipeline
(text encoder, VAE, tokenizer, scheduler) from the normal repo, with the
transformer subfolder deliberately NOT downloaded. Anything without a recipe
loads the ordinary way through `AutoPipelineForText2Image`.
"""

import os
import sys
import time

# The base sits in THIS directory, and so does everything else this imports.
# Each `worker.py` shell has already inserted `runners/` on the way in (it is one
# directory up from the shell — see mlx_text/worker.py); repeated here because a
# module may not assume something was done before it was imported, and this is
# the same self-directory insert `partial.py` falls back to.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine_options  # noqa: E402 - refuse `image` on arrival; see its docstring
import formats  # noqa: E402 - the shared format checks; see formats.py
import preview  # noqa: E402 - the ONE live-thumbnail writer; see preview.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded pipeline and the device its generator wants. One per process.
_loaded = {}

#: Repo id -> the quantized single-file transformer to use instead of the one in
#: the repo, and the pipeline class that takes it.
#:
#: A TABLE rather than a heuristic: which quantization of which component is
#: safe for a given model is an editorial judgement (the same one `catalog.py`
#: makes about what to suggest), not something to infer from a file listing. A
#: model absent from here is not unsupported — it loads the ordinary way.
#:
#: **`keep` is an ALLOW-list, and that is the whole design.** It was a deny-list
#: naming `transformer/*.safetensors`, and it saved nothing: the FLUX.2 repo
#: also ships `flux-2-klein-4b.safetensors` at its ROOT — the same 7.75GB of
#: transformer weights again, bundled the way ComfyUI wants them — which no skip
#: pattern matched, `from_pretrained` never opens, and the download therefore
#: fetched. The recipe cost 18.6GB where the model needs 10.8. A deny-list has
#: to predict every extra bundle a repo may carry (and repos gain them without
#: warning); what `from_pretrained` READS is knowable and finite —
#: `model_index.json` plus the component subfolders it names — so that is what
#: is written down.
#:
#: The transformer's CONFIG is kept while its weights are not, and it is the one
#: entry whose absence would be invisible until a machine went offline:
#: `from_single_file(config=<repo>, subfolder="transformer")` reads it to know
#: what it is building, so a recipe that dropped the whole subfolder would leave
#: a cache that still needs the network to load — "Download" reporting success
#: and then failing offline, which is the one promise that button makes.
#:
#: `repo` names the component repo the GGUF comes out of, and the FILENAME is
#: read from `formats.COMPONENT_REPOS` rather than repeated here: that repo also
#: lands in the Hub cache, where the AI Models page has to be able to say what
#: it is, and the page runs in a process that cannot import this venv.
_GGUF_RECIPES = {
    "black-forest-labs/FLUX.2-klein-4B": {
        "repo": "unsloth/FLUX.2-klein-4B-GGUF",
        "pipeline": "Flux2KleinPipeline",
        "transformer": "Flux2Transformer2DModel",
        "subfolder": "transformer",
        # `*` crosses `/` in fnmatch, so `tokenizer*/*` covers a `tokenizer_2`
        # a future FLUX may add, and nothing at the root ever matches.
        "keep": ["model_index.json", "scheduler/*", "tokenizer*/*",
                 "text_encoder*/*", "vae/*", "transformer/config.json"],
    },
}


def _component_file(recipe):
    """The single file to pull out of the recipe's component repo."""
    return formats.COMPONENT_REPOS[recipe["repo"]]["file"]


def _recipe(model_id):
    return _GGUF_RECIPES.get(model_id)


# --------------------------------------------------------------- model loading


def download(model_id):
    """Fetch what this model needs, and NOT what it doesn't.

    For a GGUF recipe that distinction is the whole point: the base repo's own
    transformer weights are the ~8GB bf16 component the quantized file replaces,
    so downloading them would cost the user several gigabytes for weights that
    are then ignored — and on a 16GB machine, the difference between a model
    that runs and one that does not.

    The scope is `recipe["keep"]`, an ALLOW-list of what `from_pretrained`
    opens; see `_GGUF_RECIPES` for why it is not the deny-list it started as.
    The transformer's CONFIG is inside it, because a "download" that leaves a
    cache which cannot load offline has not done the thing the button said it
    would.
    """
    recipe = _recipe(model_id)
    if not recipe:
        return {"snapshot": worker_base.download_snapshot(model_id), "gguf": None}

    snapshot = worker_base.download_snapshot(
        model_id, allow_patterns=list(recipe["keep"]))
    filename = _component_file(recipe)
    gguf = worker_base.download_file(
        recipe["repo"], filename,
        detail=f"Fetching {filename} (quantized transformer)…")
    return {"snapshot": snapshot, "gguf": gguf}


#: How much VRAM `_place` refuses to plan into, on top of every component
#: size it measures — 3 GiB by default, overridable with `FUSED_RENDER_AI_
#: VRAM_HEADROOM_GB` (a float, e.g. `1.5`). This is a CONSERVATIVE PLACEHOLDER,
#: not a measured figure: the denoising loop's own activations (latents,
#: attention maps, the VAE's decode buffers) cost real VRAM on top of the
#: weights this module CAN measure in-process, and nobody has yet profiled
#: how that scales with resolution on the hardware this shipped for (a 15.9
#: GiB RX 9060 XT running FLUX.2-klein-4B). 3 GiB is a guess wide enough to
#: survive a 1024² render without starving diffusers' allocator; it is also
#: the ONE NUMBER standing between a placement decision that fits and a mid
#: -render OOM at a resolution nobody has actually measured here. Narrowing
#: it needs a real profile (`torch.cuda.max_memory_allocated()` across a
#: sweep of resolutions), not a smaller guess.
_VRAM_HEADROOM_BYTES = 3 * (1 << 30)

#: Env var for `_VRAM_HEADROOM_BYTES`, same "set AND parsable" precedence as
#: `prefs.ai_idle_unload_minutes_override` — an unset, unparsable, OR
#: out-of-range (negative, infinite, or implausibly large) value is silently
#: ignored rather than treated as an intentional zero, so a typo in this
#: variable degrades to the documented default instead of removing the safety
#: margin it exists to keep. See `_vram_headroom_bytes` for why "parses" is
#: not the same question as "sane".
_VRAM_HEADROOM_ENV = "FUSED_RENDER_AI_VRAM_HEADROOM_GB"


def _vram_headroom_bytes():
    """`_VRAM_HEADROOM_BYTES`, or the env override — sanity-checked, not just
    parsed. A bare `float()`/`except ValueError` pair let two bad values
    through: `-4` parses fine and returns a NEGATIVE headroom, which makes
    `_place` plan into MORE VRAM than is free — the opposite of the margin
    this knob exists to keep — and `inf` (or any string large enough to
    overflow) parses fine too, then raises `OverflowError` out of `int(...)`,
    a class `except ValueError` never caught. That second one used to reach
    `_place`'s own outer blanket `except`, so it "worked" by accident —
    silently landing on plain offload — rather than by returning the
    documented default the way every other unparsable value does. Both are
    now caught before `int()` ever runs: a plausible headroom is `0 <= value
    < 1024` (GiB), and anything outside that — negative, infinite, or just
    absurd — is treated exactly like a value that failed to parse at all.
    """
    raw = os.environ.get(_VRAM_HEADROOM_ENV)
    if not raw:
        return _VRAM_HEADROOM_BYTES
    try:
        value = float(raw)
        if not (0 <= value < 1024):
            return _VRAM_HEADROOM_BYTES
        return int(value * (1 << 30))
    except (ValueError, OverflowError):
        return _VRAM_HEADROOM_BYTES


def _component_bytes(module):
    """Bytes a loaded `torch.nn.Module` component will cost on a device —
    parameters AND buffers, because a component can hold real weight-sized
    tensors in either bucket (a GGUF-quantized transformer's scale/zero-point
    tensors are commonly registered as buffers, not parameters, and skipping
    them would undercount exactly the component this feature was built to
    place). Measured while the component is still on CPU — `_place` runs
    before ANY `.to()`/offload call, so this is the true per-component size
    for any repo, with no catalog lookup and no reliance on a config file
    agreeing with what actually got loaded.
    """
    total = 0
    for param in module.parameters():
        total += param.numel() * param.element_size()
    for buf in module.buffers():
        total += buf.numel() * buf.element_size()
    return total


def _place(pipe):
    """Put the pipeline on the best device here: `(device, seed_device)`.

    Three cases on CUDA/ROCm now, not one — SPEC/D measured on the user's own
    machine: a FLUX.2-klein-4B pipeline via the ROCm GGUF recipe, on a 15.9
    GiB RX 9060 XT with 2.0 GiB already used system-wide. The unconditional
    `enable_model_cpu_offload()` this branch used to call regardless of card
    size left `RssAnon` at 11.7 GiB (the weights, parked in system RAM by
    accelerate) and the worker's own VRAM at 0.59 GiB (HIP context and
    staging only) — the wrong side of the trade on a card that could hold
    the whole model, or at least its hot path, resident:

    1. **All-GPU** — every component's measured size plus `_vram_headroom_
       bytes()` clears `torch.cuda.mem_get_info()`'s free figure: `pipe.to
       ("cuda")`, nothing streamed per render.
    2. **Hot-GPU** — the whole model does not fit, but the HOT set does: the
       denoiser (`transformer`, or `unet` on a pipeline that still calls it
       that) plus the `vae`, the two components that run on every step of
       generation. The text encoder runs once per render and is left to
       offload's per-call fetch — on this machine's numbers that is 2.8 GiB
       of pinned VRAM against an 8.05 GB component that would otherwise be
       fetched and freed once anyway. Diffusers' own `pipelines/pipeline_
       utils.py` (~line 1279, verified against the installed package) is
       what makes this work: `enable_model_cpu_offload` gives every component
       named in `pipe._exclude_from_cpu_offload` a plain `model.to(device)`
       and an offload hook to everything else. That list is a CLASS
       attribute defaulting to `[]` on the pipeline base — appending to it
       in place would leak the hot-set names onto every OTHER instance (and
       future instance) of the same pipeline class, so it is copied first.
    3. **Offload** — neither fits: today's unconditional `enable_model_cpu_
       offload()`, unchanged.

    A raising `mem_get_info()` or a raising component measurement (an older
    torch, an exotic component type this probe did not anticipate) degrades
    straight to case 3 — the same "a probe must never break loading" reasoning
    `release()`'s per-backend try/except documents just below, applied to the
    measurement instead of the reclaim.

    The MPS and CPU branches are untouched: MPS's unified memory makes
    offloading pure overhead there (see below), and CPU has nothing to place.
    MPS generators are unreliable, so the seed is taken on the CPU whatever
    the pipeline runs on; a reproducible seed is worth more than the
    microsecond. The two return values differ on MPS, and collapsing them is
    what hid the device from `/health` for as long as this function only
    answered the seed's question: a FLUX render on a Windows CPU is tens of
    minutes, and nothing on screen said which case the user was in.

    Every case reports which one happened, alongside `device`, so `/health`
    (and `fit`, eventually) can tell "on a GPU" apart from "on a GPU but
    streaming every component through it".
    """
    import torch

    if torch.cuda.is_available():
        placement = None
        hot_names = []
        try:
            free, _ = torch.cuda.mem_get_info()
            headroom = _vram_headroom_bytes()
            sizes = {
                name: _component_bytes(component)
                for name, component in pipe.components.items()
                if isinstance(component, torch.nn.Module)
            }
            total_bytes = sum(sizes.values())
            denoiser_name = "transformer" if "transformer" in sizes else (
                "unet" if "unet" in sizes else None)
            hot_names = [n for n in (denoiser_name, "vae") if n in sizes]
            hot_bytes = sum(sizes[n] for n in hot_names)
            if total_bytes + headroom <= free:
                placement = "all-gpu"
            elif hot_names and hot_bytes + headroom <= free:
                placement = "hot-gpu"
            else:
                placement = "offload"
        except Exception:  # noqa: BLE001 - the size probe must never break loading
            placement = None

        if placement == "all-gpu":
            pipe.to("cuda")
        elif placement == "hot-gpu":
            # Copy before append — see the docstring's point 2. Reading
            # through `pipe.` falls back to the CLASS attribute when the
            # instance has never set one of its own, exactly the case being
            # guarded against here.
            pipe._exclude_from_cpu_offload = list(pipe._exclude_from_cpu_offload) + hot_names
            pipe.enable_model_cpu_offload()
        else:
            pipe.enable_model_cpu_offload()
            placement = "offload"

        worker_base.set_state(placement=placement)
        return "cuda", "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        pipe.to("mps")
        return "mps", "cpu"
    pipe.to("cpu")
    return "cpu", "cpu"


def load(model_id, fetched):
    import torch

    recipe = _recipe(model_id)
    if recipe:
        import diffusers
        from diffusers import GGUFQuantizationConfig

        transformer_cls = getattr(diffusers, recipe["transformer"])
        pipeline_cls = getattr(diffusers, recipe["pipeline"])
        transformer = transformer_cls.from_single_file(
            fetched["gguf"],
            config=model_id,
            subfolder=recipe["subfolder"],
            quantization_config=GGUFQuantizationConfig(compute_dtype=torch.bfloat16),
            torch_dtype=torch.bfloat16,
        )
        # `from_pretrained` on the repo id rather than on the snapshot path:
        # diffusers resolves the components it still needs from the cache we
        # just filled, and skips the transformer subfolder because one is
        # passed here — which is also why `download` did not fetch it.
        pipe = pipeline_cls.from_pretrained(
            model_id, transformer=transformer, torch_dtype=torch.bfloat16)
    else:
        from diffusers import AutoPipelineForText2Image

        pipe = AutoPipelineForText2Image.from_pretrained(
            model_id, torch_dtype=torch.bfloat16)

    device, seed_device = _place(pipe)
    _loaded["seed_device"] = seed_device
    _loaded["pipe"] = pipe
    # The key the live preview's projection table is keyed by, captured HERE
    # because the VAE is what defines the latent space a fitted matrix belongs
    # to — not the pipeline and not the repo id, either of which would need a
    # new table row for every checkpoint that shares one autoencoder. A VAE
    # class that `preview.PROJECTIONS` has no entry for gets no preview at all,
    # which is what keeps this additive for every other pipeline.
    vae = getattr(pipe, "vae", None)
    _loaded["vae"] = None if vae is None else type(vae).__name__
    # See `worker_base.STATE["device"]`: "this machine has a GPU" and "this
    # pipeline is using one" are different facts, and only this process knows the
    # second. Since D381 that gap is the ORDINARY case rather than a Windows
    # quirk — the default image engine pins the `whl/cpu` torch on every
    # platform, so a fitted NVIDIA or AMD card is unused here unless the user
    # opted into the CUDA or ROCm row, while the same pin lands on `mps` on a
    # Mac. Three outcomes from one folder, and none of them visible outside it.
    worker_base.set_state(device=device)


# ------------------------------------------------------------------ generation


def _eta(remaining):
    if remaining is None:
        return ""
    if remaining < 60:
        return " · ~%ds left" % round(remaining)
    return " · ~%.1f min left" % (remaining / 60)


def memory():
    """What torch says it is holding, in bytes.

    RSS is wrong here for the same reason it was wrong for MLX (AI-8a), by a
    different mechanism: the weights live in a GPU allocator's pool, and on MPS
    that pool is not counted in the process's resident set — so an 11.9B
    pipeline reported **33 MB in memory**, which is the interpreter and nothing
    else. `worker_base` takes the larger of this and RSS, so a CPU-only run
    (where the tensors ARE in RSS and these probes read zero) still reports
    honestly.

    Both backends are asked because a machine has one or the other, and neither
    import is safe to assume: `torch.mps` exists only on a torch built for it.
    """
    import torch

    total = 0
    mps = getattr(torch, "mps", None)
    if mps is not None and hasattr(mps, "current_allocated_memory"):
        try:
            total += int(mps.current_allocated_memory())
        except (RuntimeError, OSError):
            pass
    if torch.cuda.is_available():
        try:
            total += int(torch.cuda.memory_allocated())
        except (RuntimeError, OSError):
            pass
    return total or None


def release():
    """Hand the torch caching allocator's pool back to the OS — the non-MLX
    analogue of `mlx_text.worker.release` and friends, one edit here covering
    all three folders this file serves (CPU, CUDA, ROCm) per the module
    docstring. `worker_base.serve(release=...)` fires this `worker_base.
    _RELEASE_IDLE_S` seconds after this worker's LAST render if nothing new
    started in the meantime — see `worker_base._release`'s docstring for the
    measured numbers and why a timer rather than an unconditional per-call
    clear.

    **`torch.mps` is not a presence check.** An earlier version of this
    function gated on `getattr(torch, "mps", None)` / `hasattr(mps,
    "empty_cache")` — but `torch/__init__.py` imports `torch.mps`
    unconditionally on every platform, and `empty_cache` is a plain Python
    function that always exists on it; both those guards always pass. On a
    CPU/CUDA/ROCm build the call still reaches `torch._C._mps_emptyCache()`
    -> `MPSHooksInterface::emptyCache()`, which is `TORCH_CHECK(false,
    "Cannot execute emptyCache() without MPS backend.")` — a `RuntimeError`,
    raised BEFORE the CUDA branch below ever ran, so the one build that
    actually has a caching allocator worth reclaiming (`diffusers_image_cuda`
    /`_rocm`) got nothing at all. The correct presence check is the one
    `_place()` above already uses: `torch.backends.mps.is_available()`.

    Each backend's call is also its OWN `try/except`, independent of the
    other's — the same shape `memory()` above uses for its two probes —
    so a raise from one (an API this app's floor of torch does not yet have,
    say) can never take the other one down with it the way the bug above did.
    `RuntimeError`/`OSError` is the pair `memory()` already catches for the
    same backends; anything else is a real bug and is left to surface via
    `worker_base._fire_release`'s own swallow-and-log, not hidden twice here.

    Deliberately does NOT call `torch.cuda.reset_peak_memory_stats` or touch
    anything upstream of the denoising loop — see the module boundary this
    whole feature respects: reclaiming what a finished render left behind,
    never changing what the next render is allowed to cost.
    """
    import torch

    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        try:
            torch.mps.empty_cache()
        except (RuntimeError, OSError):
            pass
    if torch.cuda.is_available():
        try:
            torch.cuda.empty_cache()
        except (RuntimeError, OSError):
            pass


def _sigma_after(pipeline, step):
    """The noise level the scheduler has ARRIVED at, after step index `step`.

    `scheduler.sigmas` is the whole schedule with a trailing zero, and by the
    time `callback_on_step_end` runs for step `i` the scheduler has already
    advanced from `sigmas[i]` to `sigmas[i + 1]` — so the latents in the
    callback are at `i + 1`. That off-by-one is the difference between a
    preview that converges and one that is permanently one step stale, which is
    invisible on a 32x32 thumbnail, hence writing it down.

    None when the schedule is not there or is shorter than the loop, which no
    scheduler in this pipeline does — but a preview must not be able to raise
    out of a denoising callback and lose a render that was going to succeed.
    """
    sigmas = getattr(getattr(pipeline, "scheduler", None), "sigmas", None)
    if sigmas is None or len(sigmas) <= step + 1:
        return None
    return float(sigmas[step + 1])


def generate(body):
    """Render one image. Returns `{path, seconds, seed, width, height, steps}`."""
    import torch

    pipe = _loaded.get("pipe")
    if pipe is None:
        raise RuntimeError("no pipeline is loaded")

    # The endpoint already refuses `image` before a job opens (it knows the
    # RESOLVED runner code); this is the second door `engine_options.py`'s own
    # docstring requires — the one a caller reaches by talking to this worker
    # directly, bypassing the endpoint entirely. This process's own runner
    # CODE is not knowable here: `diffusers_image[_cuda|_rocm]/worker.py` are
    # three five-line shells that all call `torch_image.main()` identically
    # (see the module docstring), and `worker_base.serve`'s argv carries no
    # such flag. "diffusers-image" is safe to use as a stand-in ONLY because
    # every entry this family carries in `engine_options.UNSUPPORTED` reads
    # the identical sentence — a fact about the LIBRARY, not about which
    # wheel is actually running — so whichever of the three this process is,
    # the refusal is correct. A future option whose wording legitimately
    # differs by hardware could not reuse this call as-is.
    engine_options.unsupported_or_raise("diffusers-image", image=body.get("image"))

    prompt = str(body.get("prompt") or "")
    width = int(body.get("width") or 1024)
    height = int(body.get("height") or 1024)
    steps = int(body.get("steps") or 28)
    guidance = float(body.get("guidance") or 4.0)
    seed = int(body.get("seed") or 0)
    out = str(body.get("out") or "")
    job = body.get("job") or None
    if not out:
        raise ValueError("'out' must be the path to write the image to")

    generator = torch.Generator(device=_loaded["seed_device"]).manual_seed(seed)
    started = time.time()
    step_times = []
    last = [started]
    # The live thumbnail. A no-op when the request named no preview file or when
    # nothing has been fitted for this VAE, which is what lets the callback below
    # call it unconditionally — see `preview.sink`.
    frames = preview.sink(body.get("outPreview"), _loaded.get("vae"))
    grid = preview.token_grid(width, height)

    def on_step_end(pipeline, step, timestep, callback_kwargs):
        now = time.time()
        step_times.append(now - last[0])
        last[0] = now
        done = step + 1
        average = sum(step_times) / len(step_times) if step_times else None
        remaining = (steps - done) * average if average else None
        # **The FRAME comes before the TICK, and the order is load-bearing.**
        # `done` is what `runtime.js` turns into the cache-busted `&step=N`
        # preview URL, so a tick published first can hand the page step N's URL
        # while step N's PNG is still being written — the poll is about every
        # 700ms and the write about 68ms, so the window is real. The 404 that
        # produces is survivable; the nastier half is that the URL is keyed by
        # the step and is never requested twice, so a fetch landing in the
        # window caches the PREVIOUS frame's bytes under it and that step shows
        # a stale picture for its whole duration. The cost of this order is that
        # the ✕ is learned one frame-write later, which is 68ms against a step
        # measured in seconds — and it is still honoured on THIS callback.
        #
        # `callback_on_step_end_tensor_inputs` defaults to `["latents"]` and
        # `Flux2KleinPipeline._callback_tensor_inputs` is `["latents",
        # "prompt_embeds"]`, so the latents arrive here without asking for them.
        #
        # A CLOSURE, not the array: pulling `(1, H*W, 128)` off the GPU is a
        # synchronisation, and most of the 68ms this feature was measured at. A
        # sink that is not writing must not be charged for it, and passing a
        # thunk is what keeps the `if preview:` branch out of this loop. `.to`
        # in one call rather than `.float().cpu()` — one transfer, not two.
        sigma = _sigma_after(pipeline, step)
        if sigma is not None:
            frames.add(
                lambda: callback_kwargs["latents"].detach().to(
                    "cpu", torch.float32).numpy(),
                sigma=sigma, grid=grid)
        # `report_or_cancel`, not `report`: this callback is the ONLY point in a
        # minutes-long `pipe()` call where a stop can be honoured, and the reply
        # to this tick is how the ✕ gets here.
        # NO step count in the detail. `done`/`total` are right here in the same
        # call, and the row renders them itself — as "1 / 28" plus a percentage
        # in its head (`jobAmount`/`dl-pct`, DownloadManager.tsx). Spelling the
        # same pair into the caption too printed it twice on one row, in the
        # half that has the least space to spare. The ETA stays: it is the one
        # thing here that `done`/`total` cannot be turned into downstream.
        worker_base.report_or_cancel(
            job=job, kind="task", unit="", done=done, total=steps,
            detail="Denoising%s" % _eta(remaining))
        if worker_base.CANCEL.is_set():
            raise worker_base.Cancelled()
        return callback_kwargs

    # Step 0 is the one tick with no frame behind it, and that is not the
    # ordering bug above: a frame needs two latents, so nothing exists to write
    # until the second step. `runtime.js` documents that early 404 as ordinary
    # and tells a page to hide the <img> on error.
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=steps, detail="Denoising")
    # The sink wraps the SAVE as well as the render: its exit is the lifecycle,
    # and a clean one means the real PNG has landed and the preview is now
    # duplicate bytes. A cancel or a failure discards it too (`preview.Sink`).
    with frames:
        image = pipe(
            prompt=prompt,
            height=height,
            width=width,
            guidance_scale=guidance,
            num_inference_steps=steps,
            generator=generator,
            callback_on_step_end=on_step_end,
        ).images[0]

        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        image.save(out)
    return {
        "path": out,
        "seconds": round(time.time() - started, 2),
        "seed": seed,
        "width": width,
        "height": height,
        "steps": steps,
    }


def _gpu_footprint():
    """`worker_base.serve(footprint=...)`'s hook: what this worker is holding
    in a DISCRETE GPU's own memory, invisible to RSS and (off darwin) to
    `os_footprint_bytes()`'s platform reading — see `worker_base._footprint`'s
    own docstring for the gap this closes and the measured numbers that found
    it (a ROCm FLUX.2-klein-4B worker: 11.7 GiB of weights in RSS, 0.59 GiB of
    driver context neither RSS nor a `psutil` read can see).

    **`memory_reserved()`, not `memory_allocated()`** — deliberately the
    opposite choice from `memory()` above, which sums `memory_allocated()`
    because it feeds `resident_bytes()` -> `peak_resident_bytes()` ->
    `fit.py`'s "measured" rung, where the question is "what did the tensors
    actually cost". This hook answers a different question — "what is the
    driver holding onto RIGHT NOW" — and `release()`'s `torch.cuda.empty_
    cache()` call hands back the RESERVED pool, not merely whatever happened
    to be allocated at that instant. Reporting `memory_allocated()` here would
    make the idle-release timer firing invisible in `os_footprint_bytes()`:
    the number would already have looked small before the reclaim, since
    allocated tracks live tensors and a finished render has none.

    **CUDA only, never MPS** — the trap this docstring exists to name: on
    darwin, `worker_base.os_footprint_bytes()`'s `phys_footprint` reading
    ALREADY counts the Metal pool a torch-on-MPS build allocates through
    (`resident_bytes()`'s own docstring measured 23 GB of it, invisible to
    RSS but not to `phys_footprint`). Reporting an MPS figure through this
    hook on top of that would double the same bytes into the total — the
    additive combination `os_footprint_bytes()` performs is only correct
    because CUDA/ROCm VRAM and Linux RSS are genuinely disjoint; Metal's pool
    and `phys_footprint` are not. Hence the single `torch.cuda.is_available()`
    gate below and nothing checking `torch.backends.mps` at all.
    """
    import torch

    if torch.cuda.is_available():
        return int(torch.cuda.memory_reserved())
    return None


def main():
    """Serve, forever. The entry point each variant's `worker.py` shell calls.

    A function rather than a `__main__` block because this file is imported, not
    run: the process the supervisor spawns is `<variant>/worker.py`, whose whole
    body is a path insert and a call to this.
    """
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, release=release,
                      footprint=_gpu_footprint)
