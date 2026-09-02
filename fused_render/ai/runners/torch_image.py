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

import atexit
import os
import shutil
import sys
import time
import uuid

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
#:
#: **`quantize_4bit` names the components to quantize AT LOAD, and it exists
#: because swapping the transformer only fixed the smaller half of the bill.**
#: The recipe below replaces a 7.75GB bf16 transformer with a 2.43GiB Q4_K_M
#: GGUF and leaves everything else at bf16 — but this repo's TEXT ENCODER is
#: 7.5GB on its own, so after the swap the encoder is ~70% of what the worker
#: holds. Measured on a 15.9GiB RX 9060 XT, 512x512 at 4 steps, prompt and seed
#: fixed:
#:
#:   * bf16 text encoder: `memory_allocated` 10.15GiB, `drm-memory-vram`
#:     10.37GiB, warm render 5.66s / 5.72s, load+`to("cuda")` 3.45s + 3.00s.
#:   * NF4 text encoder: `memory_allocated` 5.10GiB, `drm-memory-vram` 5.35GiB,
#:     warm render 5.76s, load+`to("cuda")` 5.32s + 0.18s.
#:
#: So it HALVES the resident set and is a wash on the clock — 5.76s against
#: 5.72s is inside the run-to-run spread, and the total time to a loaded
#: pipeline actually FALLS (5.50s vs 6.45s), because quantizing costs ~1.9s at
#: load and then there are half as many bytes to copy to the card. Peak during
#: a render drops too (6.65GiB vs a 12.28GiB reserved high-water), which is
#: what `_place()` is measuring against.
#:
#: **NF4 rather than int8, and bitsandbytes rather than a second repo**, for
#: three converging reasons: `bitsandbytes` is already declared in all three
#: manifests and verified on this hardware (see any of their headers), so this
#: costs no new dependency; `tonera/FLUX.2-klein-4B-int8-diffusers` — the model
#: `catalog.py` already recommends — ships its OWN text encoder as bnb NF4
#: (`"load_in_4bit": true, "bnb_4bit_quant_type": "nf4"`), so this is the same
#: format the app already loads rather than a new one; and quantizing at load
#: needs no pre-quantized upload to exist for a given repo, which a table of
#: editorial judgements should not have to wait for.
#:
#: **What this does NOT fix is the download.** The bf16 encoder is still
#: fetched and then quantized in this process, so `keep` is unchanged and the
#: bytes on disk are what they were — see `catalog.py`'s SDNQ entry for the
#: other route, which is smaller to fetch and faster per render but arrives as
#: a whole separate repo (`recommended`, and measured strictly better than
#: this GGUF-transformer recipe on the same RX 9060 XT — smaller download,
#: lower `memory_allocated`, faster warm render). This recipe is what a user
#: gets who downloads `black-forest-labs/FLUX.2-klein-4B` itself rather than
#: the SDNQ repo the AI Models page recommends; it is not the path the
#: maintainer runs.
#:
#: A recipe WITHOUT this key quantizes nothing, which is why it is a list of
#: component names rather than a boolean: the next recipe may want its VAE left
#: alone, or a pipeline with three text encoders may want two of them.
_GGUF_RECIPES = {
    "black-forest-labs/FLUX.2-klein-4B": {
        "repo": "unsloth/FLUX.2-klein-4B-GGUF",
        "pipeline": "Flux2KleinPipeline",
        "transformer": "Flux2Transformer2DModel",
        "subfolder": "transformer",
        "quantize_4bit": ["text_encoder"],
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


def _register_extra_quantizers():
    """Teach diffusers the quantization backends it does not ship, by importing
    the packages that register themselves into its mapping.

    Today that is exactly one: `sdnq`, which
    `Disty0/FLUX.2-klein-4B-SDNQ-4bit-dynamic` (`catalog.py`) is serialized
    with. diffusers 0.39 knows autoround, bitsandbytes, gguf, modelopt, quanto
    and torchao — every other quantized repo this app loads is one of those,
    and needs nothing here.

    **Imported for a SIDE EFFECT, which is why it looks like an unused
    import.** `sdnq/quantizer.py` ends by mutating
    `diffusers.quantizers.auto.AUTO_QUANTIZER_MAPPING` and its transformers
    twin. Nothing in this file calls an sdnq name, and nothing should: the
    whole integration is that `from_pretrained` can now resolve
    `"quant_method": "sdnq"` out of a component's `config.json` on its own.
    See the runner manifests for the version ceiling that risk buys.

    **Absence is tolerated here, unlike `_load_quantization`'s refusal to
    swallow its own failure.** The asymmetry is real: that function runs only
    when a recipe explicitly asked for quantization, so failing it silently
    would load a model at four times its promised size. This runs on EVERY
    load, including the many that have nothing to do with sdnq, and a missing
    optional package must not be what stops an ordinary bf16 pipeline from
    loading. A model that genuinely needs it still fails loudly — diffusers
    raises on the unknown `quant_method` it cannot resolve, naming it — so
    nothing is hidden, it is just reported by the layer that actually knows the
    requirement was unmet.
    """
    try:
        import sdnq  # noqa: F401 - imported to register, not to call
    except Exception as error:  # noqa: BLE001 - an optional backend must never break loading
        # A load that never needed sdnq must still succeed, which is why this
        # is caught rather than raised — but a load that DOES need it now fails
        # later with diffusers' own "Unknown quantization type, got sdnq"
        # ValueError, which names what went missing but not why. This
        # breadcrumb is the why: it puts the real import error (a triton
        # mismatch, a missing wheel, whatever broke `sdnq/quantizer.py`'s own
        # import chain) next to that later failure instead of leaving it
        # silently discarded.
        sys.stderr.write("[fused] sdnq did not register its quantization "
                         f"backend: {error.__class__.__name__}: {error}\n")


def _load_quantization(recipe):
    """The `quantization_config=` for `recipe`'s `quantize_4bit` components, or
    None when it names none — see `_GGUF_RECIPES` for the measured numbers and
    why NF4.

    **Double quantization is on.** It quantizes the per-block constants the
    first pass produces, which is the difference between ~0.5 and ~0.4 bits of
    overhead per weight — small in ratio, but this is a 7.5GB component and the
    accuracy cost is the one bitsandbytes documents as negligible. It is also
    what `tonera/FLUX.2-klein-4B-int8-diffusers`'s own text encoder was
    serialized with (`"bnb_4bit_use_double_quant": true`), so the two models
    this runner loads now hold their encoders in the same format.

    **`bnb_4bit_compute_dtype` matches the pipeline's `torch_dtype`, and has
    to.** NF4 is a STORAGE format: every matmul dequantizes to the compute
    dtype first, and a float32 compute dtype against bf16 activations is both
    slower and a dtype mismatch waiting to surface inside an attention block.
    The default is float32, so leaving it out would be choosing the wrong one
    silently.

    **Also correct on Apple Silicon, where `_place()` sends the whole pipe to
    `mps` instead of `cuda`.** This is not a CUDA/ROCm-only trick that happens
    to survive that move: bitsandbytes has shipped a real `mps` backend since
    0.49.0 (`backends/mps/ops.py`), registering `quantize_4bit`,
    `dequantize_4bit`, `gemv_4bit` and `gemm_4bit` behind pure-PyTorch
    fallbacks that need no compiled kernel — only the Hub kernel it also tries
    is macOS-26-and-up. `Params4bit.to()` has no device allow-list; it
    dispatches through whichever of those is registered for the tensor's
    device. The runner manifests' `bitsandbytes>=0.46.1,<1` floor predates a
    macOS wheel — PyPI carries none for bitsandbytes before 0.49.0 — so
    without a committed lockfile a Mac's `uv sync` can only land on 0.49.0 or
    newer, which is exactly the range with the mps backend. The same NF4
    format already ships on `mps` for `tonera/FLUX.2-klein-4B-int8-diffusers`'s
    text encoder, which resolves its `quantization_config` straight out of
    that repo's `config.json` rather than through this function — this recipe
    is the second caller of a device this dependency already had to work on,
    not the first.

    Deliberately NOT wrapped in a try/except. A `_place()` probe that raises
    must degrade to offload because a measurement is advisory; a quantization
    the recipe explicitly asked for is not — if it cannot be applied, the model
    would load at four times the size this recipe promises `_place()` it needs,
    and quietly land in offload on a card that could have held it. Loud is
    correct: `worker_base` turns the raise into the page's load error, with the
    library's own message about what was missing.
    """
    components = (recipe or {}).get("quantize_4bit")
    if not components:
        return None

    import torch
    from diffusers import PipelineQuantizationConfig

    return PipelineQuantizationConfig(
        quant_backend="bitsandbytes_4bit",
        quant_kwargs={
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_use_double_quant": True,
            "bnb_4bit_compute_dtype": torch.bfloat16,
        },
        components_to_quantize=list(components),
    )


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


#: How many transformer blocks `_place`'s group-offload rung moves onto the
#: GPU as one unit — diffusers' own `num_blocks_per_group`, passed straight
#: through to `pipe.enable_group_offload(..., offload_type="block_level")`.
#: 1 is the finest granularity: the smallest possible unit is swapped in at a
#: time, trading render speed for the lowest possible VRAM ceiling — the
#: right default for a rung whose entire purpose is fitting a model onto a
#: card too small for `_VRAM_HEADROOM_BYTES`'s all-gpu bound to clear.
#: Overridable with `FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS` (an int, e.g. `4`)
#: once real hardware shows a coarser grouping is worth the VRAM it costs —
#: nobody has profiled that trade on the card this shipped for yet, so 1 is
#: a conservative starting point, not a measured optimum.
_NUM_BLOCKS_PER_GROUP = 1

#: Env var for `_NUM_BLOCKS_PER_GROUP`, same "set AND sane" precedence as
#: `_VRAM_HEADROOM_ENV` — an unset, unparsable, or out-of-range (zero,
#: negative, infinite, or implausibly large) value is silently ignored
#: rather than treated as an intentional override, so a typo here degrades
#: to the documented default instead of handing diffusers a group size that
#: makes no sense. See `_num_blocks_per_group` for why "parses" is not the
#: same question as "sane".
_NUM_BLOCKS_PER_GROUP_ENV = "FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS"


def _num_blocks_per_group():
    """`_NUM_BLOCKS_PER_GROUP`, or the env override — sanity-checked, not
    just parsed, for the same two reasons `_vram_headroom_bytes` is: a bare
    `int()`/`except ValueError` pair would let `-4` through (a negative
    group size diffusers has no sane way to honour) and let a string large
    enough to overflow `int()` raise `OverflowError` — a class `except
    ValueError` never catches, which would otherwise reach `_place`'s own
    outer guard and silently fall all the way to plain offload rather than
    the documented default every other unparsable value gets. A plausible
    group size is a positive integer under 1024 — comfortably above any
    block count a shipped pipeline has — and anything outside that,
    including zero, is treated exactly like a value that failed to parse.
    """
    raw = os.environ.get(_NUM_BLOCKS_PER_GROUP_ENV)
    if not raw:
        return _NUM_BLOCKS_PER_GROUP
    try:
        value = float(raw)
        if not (0 < value < 1024):
            return _NUM_BLOCKS_PER_GROUP
        return int(value)
    except (ValueError, OverflowError):
        return _NUM_BLOCKS_PER_GROUP


#: Env var gating `_group_offload_disk_path()` — same "set AND sane"
#: precedence as `_VRAM_HEADROOM_ENV`/`_NUM_BLOCKS_PER_GROUP_ENV`. Unset
#: falls through to the default (disk offload ON); the one recognized
#: override is the literal value `"off"` (case-insensitive), which disables
#: `offload_to_disk_path` and drops the group-offload rung back to memory
#: mode — every other value, including a typo of "off" itself, is treated
#: as unset rather than as an intentional but malformed override, so a
#: mistyped value degrades to disk-on instead of silently doing nothing.
_GROUP_OFFLOAD_DISK_ENV = "FUSED_RENDER_AI_GROUP_OFFLOAD_DISK"


def _group_offload_disk_enabled():
    """Whether `_place`'s group-offload rung should pass `offload_to_disk_
    path` at all. True unless `_GROUP_OFFLOAD_DISK_ENV` is set to exactly
    "off" (case-insensitive, surrounding whitespace ignored) — the same
    "sane, not just parsed" posture the other two knobs use, applied to a
    boolean instead of a number: there is no ambiguous middle ground to
    reject the way `-4` or `inf` needs rejecting for a byte count, so the
    only two states are "recognized disable string" and "everything else
    defaults to on."
    """
    raw = os.environ.get(_GROUP_OFFLOAD_DISK_ENV)
    if raw is None:
        return True
    return raw.strip().lower() != "off"


def _group_offload_base_dir():
    """The directory `_group_offload_disk_path()`'s per-load subdirectories
    live under — `~/.fused-render/cache/group-offload` (or `$FUSED_RENDER_
    HOME`'s equivalent), the app's own real-disk home, never `$TMPDIR`/`/tmp`
    and never `$XDG_RUNTIME_DIR`: `fused_render/supervisor/paths.py`
    documents `$XDG_RUNTIME_DIR` as tmpfs-backed on Linux, and a generic
    `/tmp` is exactly as likely to be a tmpfs mount on the machines this
    ships to. Spilling group-offloaded weights onto tmpfs would just move
    the same anonymous, unreclaimable pages this feature exists to get rid
    of from the process's RSS to a filesystem backed by that same RAM —
    clean-looking in `/proc/<pid>/smaps_rollup` (file-backed pages, evictable
    in principle) but not actually freeing anything on a memory-constrained
    host, since the kernel would need to evict them back into a RAM the
    tmpfs itself consumes. `~/.fused-render` is the same `os.environ.get
    ("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")` fallback
    used throughout the rest of the app (`tasks_store.py`, `community.py`,
    `meta_migration.py`, and others), reused here rather than inventing a new
    base path.
    """
    home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
    return os.path.join(home, "cache", "group-offload")


def _sweep_stale_group_offload_dirs(base):
    """Remove every subdirectory of `base` left behind by a PROCESS THAT NO
    LONGER EXISTS, best-effort, before this load claims its own subdirectory.

    **This is the mechanism disk cleanup actually depends on — `atexit` is
    not.** A worker's two shutdown paths (`worker_base.serve`'s `/quit`
    handler, which spawns a thread that calls `os._exit(0)`; and
    `supervisor._kill_tree`, SIGTERM then SIGKILL) both skip `atexit`
    handlers entirely — `os._exit` by definition, and a signal Python has
    installed no handler for by not giving the interpreter a chance to run
    its exit machinery at all. `enable_group_offload`'s disk path also
    writes its whole offloaded weight set to disk the moment it is enabled
    (`GroupOffloadingHook.initialize_hook` calls `group.offload_()` at LOAD
    time, per the installed `diffusers/hooks/group_offloading.py`), not
    lazily on first render — so every ordinary load-then-quit cycle through
    this rung would otherwise leave its whole subdirectory on disk forever,
    with `atexit`'s registration in `_group_offload_disk_path()` firing only
    on the rare unhandled-exception exit. Ten loads on a machine that takes
    this rung costs the user tens of GB with no indication why, absent this
    sweep.

    **Only a directory whose pid prefix names a process that is no longer
    alive is removed** — never every sibling, and never by simply sweeping
    everything under `base` on the theory that "this process's own directory
    is the only one that matters right now": two workers can legitimately be
    group-offloading at once, each holding its own live subdirectory, and a
    sweep that cannot tell a dead worker's leftovers from a live one's
    in-progress files would delete out from under it. `os.kill(pid, 0)`
    (raises `ProcessLookupError` for a dead pid, `OSError` for one this
    process cannot signal, returns cleanly for a live one) is the liveness
    check; a directory is only ever removed on the FIRST of those two,
    matching the identity `_group_offload_disk_path()` writes below
    (`<pid>-<random>`). A directory whose name this function cannot parse as
    that shape is left alone rather than guessed at.

    Best-effort throughout: a missing `base` (nothing has ever offloaded to
    disk on this machine), a permission error walking it, or a permission
    error removing one stale entry must not turn an optimisation's own
    housekeeping into a reason a load fails. This runs once per call to
    `_group_offload_disk_path()`, which is once per load that reaches the
    group-offload rung — O(number of stale directories on disk), never
    O(their contents), so it costs nothing proportional to weight size.
    """
    try:
        entries = os.listdir(base)
    except OSError:
        return
    for entry in entries:
        pid_str = entry.split("-", 1)[0]
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            pass
        except OSError:
            # Alive, or this process cannot tell (EPERM on a foreign-owned
            # pid) — either way, not confidently dead, so leave it alone.
            continue
        else:
            continue  # the process answered: it is alive, its directory stays.
        try:
            shutil.rmtree(os.path.join(base, entry), ignore_errors=True)
        except OSError:
            pass


def _group_offload_disk_path():
    """Where `_place`'s group-offload rung should spill block weights to
    while they are offloaded, or `None` when `_group_offload_disk_enabled()`
    says disk offload is off (memory mode: `enable_group_offload` with no
    `offload_to_disk_path` at all, its own long-standing default).

    **Identity is `<pid>-<random>`, never pid alone, and the directory is
    swept clean before use even though the identity is already unique —
    both, not either.** `enable_group_offload`'s own disk-write skip (`if
    not self._is_offloaded_to_disk and not os.path.exists(self.safetensors_
    file_path)`, `diffusers/hooks/group_offloading.py`) is keyed by a hash
    of the module's own group id — deterministic across processes, and
    across any two loads of the same recipe. A pid-only directory name is
    reused whenever Linux recycles a pid (commonly every ~32768 loads on
    this platform's default `pid_max`), and combined with a directory that
    nothing ever swept (see `_sweep_stale_group_offload_dirs`), a worker
    whose pid collides with a dead one would inherit that directory
    fully populated, skip every write, and go on to ONLOAD THE WRONG
    MODEL'S WEIGHTS with no error at all. A random suffix makes that
    collision astronomically unlikely by construction; deleting whatever
    (if anything) already sits at this exact path before `os.makedirs`
    closes the gap for the one case a random suffix does not cover on its
    own — a stale directory this process's own earlier, uncleaned run left
    at the exact same identity, which cannot happen with a fresh `uuid4`
    but costs nothing to guard against anyway.

    Deliberately real disk — see `_group_offload_base_dir()` for why, and
    for the `FUSED_RENDER_HOME` convention this reuses.

    Created eagerly (`os.makedirs(..., exist_ok=True)`) so `enable_group_
    offload` never has to create it itself mid-load. `atexit.register`
    still runs on a normal Python-level exit (an unhandled exception, a
    plain `return` from `main()`) as a best-effort EXTRA — cheap insurance
    for the one shutdown path that does reach it — but `_sweep_stale_group_
    offload_dirs` above, not this, is the mechanism cleanup actually
    depends on; see that function's own docstring for why `atexit` alone
    was not enough.
    """
    if not _group_offload_disk_enabled():
        return None
    base = _group_offload_base_dir()
    _sweep_stale_group_offload_dirs(base)
    identity = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
    path = os.path.join(base, identity)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    if path not in _GROUP_OFFLOAD_DISK_PATHS_REGISTERED:
        atexit.register(shutil.rmtree, path, ignore_errors=True)
        _GROUP_OFFLOAD_DISK_PATHS_REGISTERED.add(path)
    return path


#: Guards `_group_offload_disk_path()`'s `atexit.register` against
#: registering the same directory's cleanup twice — `_place` can call it
#: more than once in a process only in tests, but a duplicate `atexit`
#: registration would just call `shutil.rmtree` on an already-removed
#: directory a second time at exit, harmless with `ignore_errors=True` but
#: still worth not doing.
_GROUP_OFFLOAD_DISK_PATHS_REGISTERED = set()


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


def _has_untracked_tensor(obj, registered_ids, _depth=0):
    """True when `obj` (a parameter, a buffer, or — one recursion level down
    — a plain object hanging off one) carries a plain attribute that is a
    `torch.Tensor` not in `registered_ids`. `registered_ids` is the SAME set
    at every level of the recursion: it always names the owning component's
    actual `named_parameters()`/`named_buffers()`, never the attributes of
    whatever `obj` happens to be at this level, so a tensor found two levels
    down (bitsandbytes' `weight.quant_state.absmax`) is judged against the
    same registration `ModuleGroup` itself would check, not against
    `quant_state`'s own attributes as if reaching `absmax` by recursion
    somehow registered it. `_depth` bounds the walk to component → attribute
    → attribute's own attribute and no further, a fixed, small cost
    regardless of what a component's attributes reference.
    """
    import torch

    if not hasattr(obj, "__dict__"):
        return False
    for value in vars(obj).values():
        if isinstance(value, torch.Tensor):
            if id(value) not in registered_ids:
                return True
        elif _depth < 1 and hasattr(value, "__dict__"):
            if _has_untracked_tensor(value, registered_ids, _depth=_depth + 1):
                return True
    return False


def _unsafe_for_group_offload(component):
    """True when `component` holds quantization state that `enable_group_
    offload`'s tensor movement would leave behind, so it must be excluded
    from block offload.

    Governing fact (installed `diffusers/hooks/group_offloading.py`,
    `ModuleGroup.__init__`): a group collects tensors ONLY from `module.
    parameters()` and `module.buffers()`, and moves each one by reassigning
    `tensor.data` (`_transfer_tensor_to_device`) — it never calls `.to()`.
    A parameter whose entire quantized representation lives in `.data`
    itself, with any quantization metadata kept as plain non-tensor Python
    attributes, survives that move correctly: diffusers' own `GGUFParameter`
    (`diffusers/quantizers/gguf/utils.py`) is exactly this shape — nothing
    on it but `.data` and a `quant_type` string. A parameter that instead
    keeps PART of its quantized representation in a SEPARATE tensor hanging
    off it as a plain attribute is silently corrupted by the move: the
    attribute tensor never gets reassigned, so after an onload the weight's
    `.data` sits on the new device while its quant state is still on the
    old one. bitsandbytes' `Params4bit` is this shape — `quant_state.
    absmax`, `quant_state.code`, and (double-quantized) `quant_state.
    state2`'s own `absmax`/`code` are ordinary tensors kept as attributes of
    `quant_state`, itself a plain attribute of the parameter, never
    registered as a parameter or buffer anywhere `ModuleGroup` would find
    it.

    Detected STRUCTURALLY, by `_has_untracked_tensor`: walk each parameter's
    and buffer's own plain attributes (recursing one level into any
    attribute that is itself a plain object, since bitsandbytes nests a
    second `quant_state` this way for double quantization) looking for a
    tensor that is not itself a registered parameter or buffer of
    `component` — the SAME `registered_ids` set is threaded through every
    level of that recursion, not recomputed at each level, because a nested
    object's own tensor attributes (`quant_state.absmax`) are exactly the
    ones this is trying to catch, not tensors to treat as newly "registered"
    just because they were reached by recursing. No name list, and no
    `import bitsandbytes`/`gguf`/`sdnq` — those packages are optional and
    this module gains no hard dependency on any of them just to decide
    whether a component is safe to move. Walking already-resident Python
    attributes costs nothing proportional to weight size, so this is fine
    to run at load time.

    **Anything this function cannot make sense of comes back UNSAFE
    (excluded).** `component.named_parameters()` raising, or a shape this
    walk does not recognize, is treated the same as a confirmed-bad one:
    staying resident costs memory, where guessing SAFE and being wrong
    silently corrupts a render.

    **`sdnq` is not installed in this environment, and this function has
    never actually been run against an SDNQ-quantized component.** Nothing
    here verifies whether SDNQ's tensors are safe under `ModuleGroup`'s
    movement; the structural check above may exclude an SDNQ component or
    may not, depending on how SDNQ happens to shape its parameters, and
    neither outcome should be read as SDNQ having been checked against a
    real object.
    """
    try:
        registered_ids = {id(t) for _, t in component.named_parameters()}
        registered_ids |= {id(t) for _, t in component.named_buffers()}
        tensors = [t for _, t in component.named_parameters()]
        tensors += [t for _, t in component.named_buffers()]
        for tensor in tensors:
            if _has_untracked_tensor(tensor, registered_ids):
                return True
    except Exception:  # noqa: BLE001 - an unrecognized shape is unsafe, not a crash
        return True
    return False


def _group_offload_exclusions(pipe):
    """Which of `pipe.components` block offload must never touch: `vae`
    always — see `_place`'s docstring for the decode-path reason, a hook
    problem `_unsafe_for_group_offload` has nothing to say about — plus
    whatever that structural check finds unsafe to move by `.data`
    reassignment.

    Generic and quantizer-agnostic on purpose: this used to exclude (or
    not) a component by name, on reasoning specific to what THAT recipe's
    text encoder happened to be quantized with. That does not generalize
    to a catalog where a user can point this app at an arbitrary HF repo
    whose component quantization this app has never seen and cannot
    hand-tune for. Asking each component structurally whether ITS tensors
    survive `enable_group_offload`'s movement, rather than asking which
    model this is, is the version of this check that keeps working for a
    repo nobody curated.
    """
    exclusions = ["vae"]
    for name, component in pipe.components.items():
        if name == "vae" or component is None or not hasattr(component, "named_parameters"):
            continue
        if _unsafe_for_group_offload(component):
            exclusions.append(name)
    return exclusions


def _place(pipe):
    """Put the pipeline on the best device here: `(device, seed_device)`.

    Three cases on CUDA/ROCm — SPEC/D measured on the user's own machine: a
    FLUX.2-klein-4B pipeline via the ROCm GGUF recipe, on a 15.9 GiB RX 9060
    XT with 2.0 GiB already used system-wide. The unconditional `enable_
    model_cpu_offload()` this branch used to call regardless of card size
    left `RssAnon` at 11.7 GiB (the weights, parked in system RAM by
    accelerate) and the worker's own VRAM at 0.59 GiB (HIP context and
    staging only) — the wrong side of the trade on a card that could hold
    the whole model resident:

    1. **All-GPU** — every component's measured size plus `_vram_headroom_
       bytes()` clears `torch.cuda.mem_get_info()`'s free figure: `pipe.to
       ("cuda")`, nothing streamed per render.
    2. **Group offload** — the probe ran and said it does not fit: diffusers'
       own `pipe.enable_group_offload(onload_device="cuda", offload_device=
       "cpu", offload_type="block_level", num_blocks_per_group=_num_blocks_
       per_group(), exclude_modules=["vae"], offload_to_disk_path=
       _group_offload_disk_path())`. It moves one group of blocks onto the
       GPU at a time instead of parking every weight in system RAM for the
       process's whole life, which lowers the VRAM ceiling a render needs
       without accelerate's offload ever entering the picture. REPLACES
       case 3 here, never stacks on it: `_raise_error_if_accelerate_model_or_
       sequential_hook_present` makes diffusers' own hooks and accelerate's
       mutually exclusive, so a pipeline cannot carry both at once.
       `use_stream` is left at its default (unset) — SPEC/D's gate on the RX
       9060 XT is what settles whether streaming is safe on ROCm, by
       measurement, and that gate has not run; this rung must not force it
       on ahead of that answer.

       `offload_to_disk_path=` is what turns the offloaded blocks' host
       pages from anonymous (parked in RAM for the process's whole life,
       same as plain offload leaves them) into file-backed and kernel-
       evictable — see `_group_offload_disk_path()`'s own docstring for
       where that path lives, why it must be real disk, and the env knob
       that can turn it back off. `_group_offload_disk_enabled() is False`
       makes this argument `None`, which `enable_group_offload` treats
       exactly like never passing it at all: memory mode, the same shape
       this rung shipped with before disk residency existed.

       `exclude_modules=` (`_group_offload_exclusions(pipe)`) names every
       component that must never reach `enable_group_offload`: `vae`,
       always, plus whatever `_unsafe_for_group_offload` finds holding
       quantization state outside the tensors `ModuleGroup` actually moves
       — see that function's own docstring for the structural check and
       why it does not need to know which model this is.

       `vae` is load-bearing, not an optimization: block offload's hooks
       only fire on a module's `forward`, and diffusers'
       `AutoencoderKL` is the ONE VAE class that opts a decode-only call path
       into the same hooking via `_group_offload_block_modules = ["quant_
       conv", "post_quant_conv", "encoder", "decoder"]`
       (`diffusers/models/autoencoders/autoencoder_kl.py`), which makes
       `apply_group_offloading` recurse into the encoder/decoder so their
       inner `ModuleList`s each get their own hook. `AutoencoderKLFlux2` —
       the klein recipe's VAE — declares no such attribute, so its encoder,
       decoder and quant_conv all land in one "unmatched group" whose hook
       sits on the VAE's own `forward`. Every FLUX pipeline decodes via
       `self.vae.decode(latents, return_dict=False)`, never `.forward(...)`,
       and diffusers' `@apply_forward_hook` decorator
       (`diffusers/utils/accelerate_utils.py`) only fires accelerate's own
       `_hf_hook` on `.decode`/`.encode` — it has no idea group offload's
       hooks exist. A block-offloaded `AutoencoderKLFlux2` would therefore
       decode with its weights still parked on the offload device against
       CUDA latents, and die there after the whole denoising loop had
       already run. Excluding it moves it once to the onload device instead
       and leaves it resident — at 0.17 GiB, cheap enough next to the
       transformer this rung exists for that it is not worth measuring.
    3. **Offload** — group offload itself raised with nothing left hooked
       (after one retry in memory mode, if the first attempt was in disk
       mode — see the `except` below for why disk mode gets a second try
       before falling here), or the probe raised before it could answer
       whether the model fits: today's unconditional `enable_model_cpu_
       offload()`, unchanged, and still the terminal fallback either way.
       (A group-offload raise that left some components already hooked
       cannot reach this cleanly — see the `except` below.)

    A raising `mem_get_info()` or a raising component measurement (an older
    torch, an exotic component type this probe did not anticipate) degrades
    straight to case 3, skipping case 2 entirely — group offload is only
    attempted once the probe has actually answered "it does not fit"; a
    probe that never got that far has nothing for `enable_group_offload` to
    act on either. This is the same "a probe must never break loading"
    reasoning `release()`'s per-backend try/except documents just below,
    applied to the measurement instead of the reclaim. That promise covers
    the MEASUREMENT; the all-gpu case's own `pipe.to("cuda")` and case 2's
    own `pipe.enable_group_offload(...)` get the identical treatment for the
    same reason — `_vram_headroom_bytes()`'s margin is explicitly a guess,
    `free` is sampled once before the move rather than continuously, and a
    competing process (or a component whose real device cost exceeds `numel
    * element_size`) can turn a move that looked safe into a raise. A load
    that would have SUCCEEDED via plain offload must not fail outright just
    because a faster path was tried first, so a raising `.to("cuda")` falls
    back to `enable_model_cpu_offload()` exactly like case 3. A raising
    `enable_group_offload(...)` falls back the same way ONLY when the raise
    left nothing hooked: the method hooks components one at a time in a
    loop, and `enable_model_cpu_offload` opens by refusing to run at all
    (`_maybe_raise_error_if_group_offload_active(raise_error=True)`) while
    any component still carries a group-offload hook. A raise that hit
    component N after already hooking components before it cannot be undone
    through any supported diffusers call, so that case re-raises the
    ORIGINAL error instead of trading it for a confusing "group offload
    active" `ValueError` — see the `except` below for the exact check.

    **A fourth case — pinning the "hot" set (denoiser + VAE) resident while
    leaving the text encoder to offload's per-call fetch — was built,
    measured, and removed.** A code review surfaced five defects, and
    chasing them down showed the branch could not pay for itself:

    - *Unreachable for the shipping pipeline.* With exactly one non-hot
      `nn.Module`, `hot_bytes + max(other) == total_bytes` exactly, so the
      `elif` was byte-for-byte the `if` above it and could never be the
      first to pass. FLUX.2-klein-4B — the pipeline this feature was built
      for — is that shape (`text_encoder`, `transformer`, `vae`).
    - *It did not save what it claimed.* accelerate's offload is a CHAIN:
      `CpuOffload.pre_forward` offloads `prev_module_hook`'s model
      (`accelerate/hooks.py:744-765`). With the full seq `"text_encoder->
      transformer->vae"`, the transformer's first forward evicted the text
      encoder before denoising even started. Truncating the seq to
      `"text_encoder"` (removing the hot names, as an earlier revision of
      this function did) deleted that link, so the text encoder stayed
      resident for the ENTIRE denoising loop — making hot-gpu's steady-state
      VRAM equal to all-gpu's while claiming to be the cheaper option.
    - *The pin round-tripped every render anyway.* Every FLUX `__call__`
      ends in `maybe_free_model_hooks()`, which re-enters `enable_model_
      cpu_offload` and unconditionally runs `self.to("cpu", silence_dtype_
      warnings=True)` (`diffusers/pipelines/pipeline_utils.py:1244`) before
      re-placing the excluded set — so the "pinned" components made a
      GPU→CPU→GPU trip on every single image, not just at load time.
    - *No fallback on the pin*, unlike the all-gpu move beside it.
    - *`max(others)` undercounts peak* for a pipeline that keeps several
      non-hot components resident at once (a `safety_checker`/`watermarker`
      already in a class's `_exclude_from_cpu_offload`, or a `controlnet`
      absent from the seq) — an error in the OOM direction.

    A survey of what could ever reach the branch found nothing worth keeping
    it for, either. Measured from the HuggingFace API (raw repo totals,
    which OVERCOUNT because these repos carry several precision variants of
    the same weights): FLUX.1-schnell has 2 non-hot components with a 0.25
    GB gap between `hot+max(other)` and `total`; SDXL 2 components with a
    1.24 GB gap; SD3.5-medium 3 components with a 3.27 GB raw gap (~1.65 GB
    at what actually loads in bf16). SD3.5's three text encoders are the
    only structurally good fit for this — and even there, on the 15.9 GiB
    RX 9060 XT this feature was built for (~13.9 GiB free, 3 GiB headroom)
    SD3.5-medium at bf16 needs 19.3 GiB for all-gpu and 17.7 GiB for
    hot-gpu, so it still lands in plain offload; quantized far enough to
    reach hot-gpu, it fits all-gpu outright instead.

    Recorded here so the next person with the same idea finds the
    accelerate-chain reason it is not as easy as it looks, rather than
    reinventing it: pinning is a hook-chain problem, not a "which components
    stay resident" problem, and the seq-truncation fix has to preserve the
    chain through whatever it keeps hot. If a three-text-encoder pipeline
    (SD3.5 and friends) ever joins the catalog, that is when to revisit —
    and the chain semantics above are the thing to get right this time.

    **None of that accelerate-chain reasoning applies to case 2 above.**
    `enable_group_offload` is diffusers' own mechanism — it installs its own
    forward hooks (`diffusers/hooks/group_offloading.py`) and never touches
    `model_cpu_offload_seq`, `_exclude_from_cpu_offload`, or any of
    accelerate's `CpuOffload`/`prev_module_hook` machinery. The five defects
    above — the unreachable branch, the chain re-eviction, the round-trip
    through `maybe_free_model_hooks()`, the missing fallback, `max(others)`
    undercounting peak — were all specific to PINNING a subset of components
    resident inside accelerate's sequential-hook chain. Group offload pins
    nothing and reads no such sequence; it is a different mechanism solving
    a different problem (lowering the VRAM ceiling for whatever does not
    fit, not keeping a hot subset resident), so a future reader should not
    assume this rung was already tried under a different name and rejected.

    The MPS and CPU branches are untouched: MPS's unified memory makes
    offloading pure overhead there (see below), and CPU has nothing to place.
    MPS generators are unreliable, so the seed is taken on the CPU whatever
    the pipeline runs on; a reproducible seed is worth more than the
    microsecond. The two return values differ on MPS, and collapsing them is
    what hid the device from `/health` for as long as this function only
    answered the seed's question: a FLUX render on a Windows CPU is tens of
    minutes, and nothing on screen said which case the user was in.

    Every case reports which one happened via `set_state(placement=...)`,
    which reaches the WORKER's own `/health` endpoint (`worker_base.snapshot`)
    and, from there, `Worker.placement` and `describe()`'s `"placement"` key
    — the app's own `/health`-adjacent API. Both the load loop and
    `refresh_memory()` lift it out of `/health` the same way `device` is
    lifted, and `describe()` emits it beside `residentBytes`, so the value
    rides all the way out to the API response. Nothing in `frontend/` reads
    it yet — `AiLoadedModel` (`frontend/src/platform/lib/api.ts`) has no
    `placement` field — so the AI Models page cannot show it; wiring that up
    is a separate, frontend-only change.
    """
    import torch

    if torch.cuda.is_available():
        placement = None
        try:
            free, _ = torch.cuda.mem_get_info()
            headroom = _vram_headroom_bytes()
            sizes = {
                name: _component_bytes(component)
                for name, component in pipe.components.items()
                if isinstance(component, torch.nn.Module)
            }
            total_bytes = sum(sizes.values())
            placement = "all-gpu" if total_bytes + headroom <= free else "offload"
        except Exception:  # noqa: BLE001 - the size probe must never break loading
            placement = None

        if placement == "all-gpu":
            try:
                pipe.to("cuda")
            except Exception:  # noqa: BLE001 - the move must degrade like the probe above
                pipe.enable_model_cpu_offload()
                placement = "offload"
        elif placement == "offload":
            # The probe RAN and said it does not fit — group offload is only
            # tried when that answer is trustworthy. `placement is None`
            # (the probe raised) skips straight to the `else` below instead.
            disk_path = _group_offload_disk_path()
            try:
                pipe.enable_group_offload(
                    onload_device=torch.device("cuda"),
                    offload_device=torch.device("cpu"),
                    offload_type="block_level",
                    num_blocks_per_group=_num_blocks_per_group(),
                    # The VAE decodes via `.decode(...)`, never `.forward(...)`
                    # — block offload only hooks `forward`, so a VAE without
                    # `_group_offload_block_modules` (AutoencoderKLFlux2, the
                    # klein recipe's VAE) would silently never come back off
                    # the offload device. See the docstring above for the
                    # full mechanism. 0.17 GiB resident is not worth the risk
                    # of getting this wrong. Every other exclusion in this
                    # list came from `_unsafe_for_group_offload` structurally
                    # inspecting that component's own parameters — see that
                    # function's docstring, not this comment, for why.
                    exclude_modules=_group_offload_exclusions(pipe),
                    offload_to_disk_path=disk_path,
                )
                placement = "group-offload"
            except Exception:  # noqa: BLE001 - REPLACES plain offload, never stacks on it
                # `enable_group_offload` hooks components one at a time in a
                # loop, so a raise partway through can leave earlier
                # components already group-offloaded even though this call
                # failed overall. `enable_model_cpu_offload` opens with
                # `_maybe_raise_error_if_group_offload_active(raise_error=
                # True)` and refuses to run AT ALL while any component still
                # carries a group-offload hook — calling it unconditionally
                # here would trade the real failure for a confusing
                # `ValueError` about group offload being active, on a load
                # that the docstring promises will fall back to plain
                # offload. Ask the same question with `raise_error=False`
                # first: a clean failure (nothing got hooked) is safe to act
                # on further; a partial failure re-raises the ORIGINAL error,
                # since there is no supported diffusers call to strip a
                # partially-applied set of group-offload hooks back off and
                # let anything else proceed.
                if pipe._maybe_raise_error_if_group_offload_active(raise_error=False):
                    raise
                # A clean failure in DISK mode gets one retry in memory mode
                # before falling all the way to plain offload: disk mode's
                # `initialize_hook` runs `group.offload_()` immediately, at
                # hook-install time, which for a torchao-quantized component
                # (`tonera/FLUX.2-klein-4B-int8-diffusers`'s transformer, the
                # catalog's own curated recipe) hits `_check_disk_offload_
                # torchao` and raises `ValueError` before a single render has
                # happened — turning a load that succeeds fine in memory mode
                # (and used to succeed via plain offload, before disk
                # residency existed) into a hard failure. The same shape
                # applies to a disk that is out of space or unwritable
                # (ENOSPC/EACCES). Nothing about this retry is torchao-
                # specific: any clean failure that showed up only because
                # `offload_to_disk_path` was set gets the same second chance,
                # on the theory that the mechanism this rung exists to
                # provide (group offload without accelerate's chain) is worth
                # more than the disk residency on top of it.
                if disk_path is not None:
                    try:
                        pipe.enable_group_offload(
                            onload_device=torch.device("cuda"),
                            offload_device=torch.device("cpu"),
                            offload_type="block_level",
                            num_blocks_per_group=_num_blocks_per_group(),
                            exclude_modules=_group_offload_exclusions(pipe),
                            offload_to_disk_path=None,
                        )
                        placement = "group-offload"
                    except Exception:  # noqa: BLE001 - same fallback contract as above
                        if pipe._maybe_raise_error_if_group_offload_active(raise_error=False):
                            raise
                        pipe.enable_model_cpu_offload()
                        placement = "offload"
                else:
                    pipe.enable_model_cpu_offload()
                    placement = "offload"
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

    # Before either branch below: the no-recipe branch is the one that loads
    # the SDNQ repo, and `from_pretrained` reads `quant_method` out of the
    # component configs itself — so the mapping has to be populated first.
    _register_extra_quantizers()
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
        #
        # `quantization_config=None` when the recipe names nothing is the
        # library's own default for the parameter, so the no-recipe and
        # no-`quantize_4bit` paths reach exactly the call they reached before.
        # `components_to_quantize` is what keeps this off the transformer: it
        # is already quantized and already BUILT, passed in above as an object
        # rather than a name, and asking diffusers to quantize it again would
        # be asking it to re-quantize GGUF weights.
        pipe = pipeline_cls.from_pretrained(
            model_id, transformer=transformer, torch_dtype=torch.bfloat16,
            quantization_config=_load_quantization(recipe))
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
    # Tile the VAE's own decode rather than the pipeline's: `Flux2KleinPipeline`
    # has neither `enable_vae_tiling` nor `enable_vae_slicing` (`hasattr` is
    # False for both, diffusers 0.39), so the usual one-line pipeline wrapper
    # is not there to call — but `AutoencoderKLFlux2.enable_tiling()` is, and
    # going straight to the VAE object captured above is what this recipe has
    # instead. It is a no-op until a render is actually big enough to need it:
    # `AutoencoderKLFlux2._decode` only takes the tiled path when `self.use_
    # tiling and (z.shape[-1] > self.tile_latent_min_size or z.shape[-2] >
    # self.tile_latent_min_size)`, so calling this unconditionally at load
    # time costs nothing on a small render — the VAE itself gates on the
    # latent's own shape, a number this function does not have and should not
    # try to guess at. That gate is also why this closes the gap `_VRAM_
    # HEADROOM_BYTES`'s own docstring admits it cannot measure: "the VAE's
    # decode buffers" is the one of its three unmeasured terms that scales
    # quadratically with resolution, and tiling removes that term from the
    # decode rather than asking the 3 GiB guess to keep covering for it.
    # Absence is tolerated the same way `_register_extra_quantizers` tolerates
    # a missing optional backend just above: a pipeline with no VAE, or a VAE
    # class without `enable_tiling` (a fake in a test, some future pipeline
    # shape), must not stop an otherwise-working load over an optimization
    # nobody asked for by name — unlike `_load_quantization`'s quantization
    # config, which IS an explicit request and is deliberately not swallowed.
    if vae is not None:
        try:
            vae.enable_tiling()
        except Exception:  # noqa: BLE001 - an optional optimization must never break loading
            pass
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
    grid = preview.token_grid(_loaded.get("vae"), width, height)

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
