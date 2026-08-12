"""Text-to-image on diffusers: one resident pipeline, four routes (SPEC §40).

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

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
#: `skip` names the WEIGHT files the quantized checkpoint replaces — never the
#: whole subfolder. `from_single_file(config=<repo>, subfolder="transformer")`
#: reads that subfolder's `config.json` to know what it is building, so a recipe
#: that ignored `transformer/*` would leave a cache that still needs the network
#: to load: "Download" would report success and then fail offline, which is the
#: one promise that button makes.
_GGUF_RECIPES = {
    "black-forest-labs/FLUX.2-klein-4B": {
        "repo": "unsloth/FLUX.2-klein-4B-GGUF",
        "file": "flux-2-klein-4b-Q4_K_M.gguf",
        "pipeline": "Flux2KleinPipeline",
        "transformer": "Flux2Transformer2DModel",
        "subfolder": "transformer",
        "skip": ["transformer/*.safetensors", "transformer/*.bin",
                 "transformer/*.safetensors.index.json"],
    },
}


def _recipe(model_id):
    return _GGUF_RECIPES.get(model_id)


# --------------------------------------------------------------- model loading


def download(model_id):
    """Fetch what this model needs, and NOT what it doesn't.

    For a GGUF recipe that distinction is the whole point: the base repo's own
    `transformer/` weights are the ~8GB bf16 component the quantized file
    replaces, so downloading them would cost the user several gigabytes for
    weights that are then ignored — and on a 16GB machine, the difference
    between a model that runs and one that does not.

    Its CONFIG still comes down, though (`skip` names weight files, not the
    subfolder), because a "download" that leaves a cache which cannot load
    offline has not done the thing the button said it would.
    """
    recipe = _recipe(model_id)
    if not recipe:
        return {"snapshot": worker_base.download_snapshot(model_id), "gguf": None}

    snapshot = worker_base.download_snapshot(
        model_id, ignore_patterns=list(recipe["skip"]))
    gguf = worker_base.download_file(
        recipe["repo"], recipe["file"],
        detail=f"Fetching {recipe['file']} (quantized transformer)…")
    return {"snapshot": snapshot, "gguf": gguf}


def _place(pipe):
    """Put the pipeline on the best device here, and say where to seed.

    Three cases and one wrinkle. On CUDA, `enable_model_cpu_offload` keeps a
    model bigger than the card's VRAM working. On Apple Silicon that same call
    is counterproductive — "GPU memory" there is the SAME unified pool as system
    RAM, so offloading buys nothing and adds transfers — hence a plain move. And
    MPS generators are unreliable, so the seed is taken on the CPU whatever the
    pipeline runs on; a reproducible seed is worth more than the microsecond.
    """
    import torch

    if torch.cuda.is_available():
        pipe.enable_model_cpu_offload()
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        pipe.to("mps")
        return "cpu"
    pipe.to("cpu")
    return "cpu"


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

    _loaded["seed_device"] = _place(pipe)
    _loaded["pipe"] = pipe


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


def generate(body):
    """Render one image. Returns `{path, seconds, seed, width, height, steps}`."""
    import torch

    pipe = _loaded.get("pipe")
    if pipe is None:
        raise RuntimeError("no pipeline is loaded")

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

    def on_step_end(pipeline, step, timestep, callback_kwargs):
        now = time.time()
        step_times.append(now - last[0])
        last[0] = now
        done = step + 1
        average = sum(step_times) / len(step_times) if step_times else None
        remaining = (steps - done) * average if average else None
        # `report_or_cancel`, not `report`: this callback is the ONLY point in a
        # minutes-long `pipe()` call where a stop can be honoured, and the reply
        # to this tick is how the ✕ gets here.
        worker_base.report_or_cancel(
            job=job, kind="task", unit="", done=done, total=steps,
            detail="Denoising — step %d/%d%s" % (done, steps, _eta(remaining)))
        if worker_base.CANCEL.is_set():
            raise worker_base.Cancelled()
        return callback_kwargs

    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=steps, detail="Denoising — step 0/%d" % steps)
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


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
