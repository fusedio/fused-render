"""Text-to-image on mflux (MLX): one resident model, four routes (SPEC §40).

The Apple Silicon counterpart of `diffusers_image/worker.py`, and deliberately
its twin from the outside: the same `/generate` body, the same one-JSON reply,
the same PNG written to the path the SERVER chose, the same denoising-step
progress on the caller's job row, the same ✕. `fused.ai.image()` cannot tell
which of the two rendered for it, and that is the contract — the second image
runner must not become a second image API.

What is genuinely different is underneath:

* **One repo, already quantized.** The torch runner needs a recipe: a ~2.4GB
  Q4_K_M GGUF transformer swapped into a pipeline whose text encoder, VAE and
  tokenizer still come from the ~7.7GB bf16 base repo, because FLUX in full
  precision OOMs a 16GB machine. The `mlx-community` conversion is 4-bit
  throughout, so there is nothing to swap and nothing to skip — one snapshot,
  ~4.6GB, loaded as it is.
* **Progress comes from mflux's own callback registry**, not from a `callback_
  on_step_end=` argument. `generate_image()` takes no callback parameter; it
  calls `ctx.in_loop(t, latents)` on every denoising step, and the registry
  those callbacks live in is a public attribute of the model. So the hook is a
  registration rather than an argument — see `_StepReporter`, and note it is
  registered ONCE per model.
* **A cancel unwinds through that same callback**, which is the only
  interruption point in a minutes-long call. mflux's loop catches
  `KeyboardInterrupt` and nothing else, so a `worker_base.Cancelled` raised
  inside the hook propagates straight out of `generate_image()` — which is what
  we want, and is why the ✕ needs no orphan machinery here (unlike
  `mlx_whisper/worker.py`, whose library call has no per-step hook at all).

**Registered BELOW `diffusers-image` in the registry, so it is opt-in.** The
speed case is measured and real (D310), but it is measured on ONE 34GB machine,
and MLX's allocator reserved a ~23.6GB high-water pool there — larger than
torch's driver allocation on the same render. Nothing has been tried on a 16GB
Mac, which is exactly the machine this app's own catalog note says full-precision
FLUX already OOMs. Being available-but-not-default is what the engine picker is
for.
"""

import os
import sys
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import formats  # noqa: E402 - the shared format checks; see formats.py
import preview  # noqa: E402 - the ONE live-thumbnail writer; see preview.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded model. One per process.
_loaded = {}


#: Repo id -> the mflux VARIANT class that loads it and the model config that
#: describes its shape.
#:
#: A TABLE rather than a heuristic, for the reason `diffusers_image`'s recipe
#: table gives: which class loads which checkpoint is an editorial judgement,
#: not something to infer from a file listing. The difference here is that a
#: model ABSENT from this table cannot fall back to "load it the ordinary way" —
#: mflux has no `AutoPipeline`, and the variant and its config are two arguments
#: nothing can guess. So an unknown repo is refused with a sentence rather than
#: attempted, which is the same trade the whisper runners make about formats.
#: In `formats` rather than here, with the layout check below, because the AI
#: Models page needs BOTH halves to tag a cached repo honestly: a snapshot can
#: have perfect MLX components and still be a model this build cannot name a
#: variant class for.
_VARIANTS = formats.MFLUX_VARIANTS

#: What an mflux-readable snapshot always has: component subfolders of MLX
#: safetensors. Checked by NAME before the import, exactly as the whisper
#: runners check theirs — a repo in the wrong format is a fact about the
#: download, and mflux's own error for it is a `ValueError` about path
#: resolution that says nothing a user can act on.
_MLX_COMPONENTS = formats.MFLUX_COMPONENTS


# --------------------------------------------------------------- model loading


def download(model_id):
    """The whole repo, and nothing clever.

    No `ignore_patterns`, which is the visible difference from the diffusers
    runner's `download`: there is no full-precision component here being
    replaced by a quantized one, so every file in the snapshot is a file the
    load will read.
    """
    return worker_base.download_snapshot(model_id)


def load(model_id, fetched):
    """`fetched` is what `download` returned — the snapshot directory."""
    recipe = _VARIANTS.get(model_id)
    # BOTH checks come before the import, and they answer different questions.
    # This one is about the CATALOG: a repo nobody has written a variant for.
    if recipe is None:
        raise RuntimeError(
            f"{model_id} is not a model this runner knows how to build. It "
            "loads mflux's own MLX conversions, and each one needs a variant "
            "class this build has to name explicitly. Try "
            "mlx-community/FLUX.2-Klein-4B-4bit, or switch this capability to "
            "the Diffusers engine on the AI Models page's Engines tab.")
    # …and this one is about the DOWNLOAD: a repo of the right name in the wrong
    # format, which is what a torch or GGUF image repo looks like from here.
    missing = [name for name in _MLX_COMPONENTS
               if not os.path.isdir(os.path.join(fetched, name))]
    if missing:
        raise RuntimeError(
            f"{model_id} has no {missing[0]}/ folder — this runner loads MLX "
            "conversions, whose weights are split into transformer/, "
            "text_encoder/ and vae/ subfolders. A diffusers or GGUF repo will "
            "not load here.")

    import importlib

    from mflux.models.common.config import ModelConfig

    variants = importlib.import_module(recipe["module"])
    variant_cls = getattr(variants, recipe["variant"])
    model_config = getattr(ModelConfig, recipe["config"])()
    # `model_path=fetched` is the SNAPSHOT DIRECTORY, never the repo id. mflux
    # resolves a local path ahead of anything else, so this load touches no
    # network — which matters because `download` has already reported those
    # bytes to the job row, and a second fetch inside `load` would be an
    # unreported download the user watches as a stalled "Loading…".
    # **This runner is threaded exactly like `mlx_whisper/worker.py` and needs
    # no `_pin_stream`, and here is why — because "same shape, but fine" is the
    # claim that rots silently when a dependency moves.**
    #
    # The shape: mlx 0.32 gives every thread its own default stream, `load` runs
    # on `worker_base`'s bring-up thread (which then exits), and `generate` runs
    # on a `ThreadingTCPServer` request thread. An UNEVALUATED array is a graph
    # owned by the stream it was built on, so forcing one from another thread
    # throws out of `metal::get_command_encoder` — an uncaught C++ exception
    # that aborts the worker with no Python traceback. That is what took out
    # every MLX Whisper transcription; the whisper runner's `_pin_stream`
    # docstring has the mechanism in full.
    #
    # Why it does not reach here: an EVALUATED array crosses threads freely, and
    # nothing in this construction stays lazy. Every array in the model arrives
    # through `WeightLoader` → `mx.load(...)`, which returns materialised
    # safetensors data rather than a graph, and `WeightApplier` installs it with
    # `model.update(...)`. On a pre-quantized repo — which is all this runner
    # loads — `apply_and_quantize` takes the `stored_q is not None` branch, so
    # `nn.quantize` runs FIRST and the loaded weights then overwrite what it
    # computed. Nothing is derived-and-kept the way whisper's underscored
    # `_positional_embedding` and `_mask` are, and those were the whole leak.
    #
    # Measured, not reasoned: building this variant on a thread that then exits
    # and forcing `mx.eval` over all 1558 arrays reachable in the model — every
    # attribute, underscored ones included — from a second thread evaluates
    # cleanly; and a real 4-step 256² render through this worker, load thread
    # gone, returned a PNG. Re-run both if mflux or mlx is bumped.
    model = variant_cls(model_config=model_config, model_path=fetched)
    # ONE registration, at load time. `CallbackRegistry.register` APPENDS, and
    # the registry belongs to the model rather than to a call — so registering
    # per request would leave a generation reporting once per previous request
    # as well, each to a job row that is over. The reporter reads the live
    # request out of `_request` instead.
    model.callbacks.register(_StepReporter())
    _loaded["model"] = model
    # The key the live preview's projection table is keyed by. The torch runner
    # reads it off `type(pipe.vae).__name__`; there is no such object here, so
    # it comes out of the recipe — see `formats.MFLUX_VARIANTS` for why an
    # autoencoder class name is the right thing for an MLX table to carry.
    _loaded["vae"] = recipe.get("vae")
    # See `worker_base.STATE["device"]`. MLX is Metal or nothing, so unlike the
    # torch runner there is nothing to detect — but the page shows this field to
    # explain a speed, and a user comparing the two engines should be able to
    # read which one they are on.
    worker_base.set_state(device="mps")


def memory():
    """What MLX itself says it is holding, in bytes.

    `get_active_memory`, deliberately — NOT `get_cache_memory`, which on this
    model reads roughly 23.6GB against an active figure of about 14.1GB (D310's
    benchmark). The difference is MLX's allocator pool: buffers it has reserved
    from Metal and not returned, kept precisely so the next generation does not
    have to ask again. Reporting the pool would tell the AI Models page that one
    resident image model costs two thirds of a 34GB machine, which is not what
    "this model is holding" means anywhere else in this app — the torch runner
    reports allocated bytes, not the driver's reservation, and the two figures
    have to be comparable for the page to put them in one column.

    `worker_base` takes the larger of this and RSS, so a wrong answer in either
    direction is corrected by the other. The pool is still worth knowing about
    and is written down in D310, because it is a fact about MEMORY PRESSURE even
    though it is not a fact about this model's size.
    """
    import mlx.core as mx

    for probe in (getattr(mx, "get_active_memory", None),
                  getattr(getattr(mx, "metal", None), "get_active_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


# ------------------------------------------------------------------ generation


def _eta(remaining):
    """Wall-clock left. `diffusers_image/worker.py`'s, and it has to be — the
    two runners' rows are rendered by the same job manager, and a user
    comparing engines reads these two strings against each other."""
    if remaining is None:
        return ""
    if remaining < 60:
        return " · ~%ds left" % round(remaining)
    return " · ~%.1f min left" % (remaining / 60)


#: What the in-flight request is, for the reporter to read. One slot: this
#: process renders one image at a time (`worker_base.GENERATE_LOCK`), and a
#: second slot would only be a way for a finished request to keep reporting.
_request = {}


def _sigma_after(config, t):
    """The noise level the schedule has ARRIVED at, after step index `t`.

    `config.scheduler.sigmas` is the whole schedule with a trailing zero, and
    the latents the callback is handed after step `t` are at `sigmas[t + 1]` —
    the same indexing `diffusers_image/worker.py` documents against
    `pipeline.scheduler.sigmas`, because it is the same schedule.

    None when there is no schedule to read, which mflux always has — but a
    preview must not be able to raise out of the one callback this runner
    cancels through and lose a render that was going to succeed.
    """
    sigmas = getattr(getattr(config, "scheduler", None), "sigmas", None)
    if sigmas is None or len(sigmas) <= t + 1:
        return None
    return float(sigmas[t + 1])


def _as_numpy(latents):
    """The step's latents as numpy, for the preview projection.

    `astype(float32)` first because numpy has no bfloat16 and mflux's loop is
    free to work in it. Both packed `(B, N, 128)` and unpatchified
    `(B, 128, h, w)` come through unchanged — `preview` takes either, so the
    unpack rule is not restated here.

    **This costs the render nothing.** Touching the array forces the same
    `mx.eval` the generation loop performs immediately after the callback
    returns, and it happens ON that thread — so there is no unevaluated graph
    crossing a thread boundary and no `_pin_stream` concern (see `load`'s
    docstring for the failure mode that would be).
    """
    import mlx.core as mx
    import numpy

    return numpy.asarray(latents.astype(mx.float32))


class _StepReporter:
    """mflux's in-loop callback, and this runner's only interruption point.

    Duck-typed against `mflux.callbacks.callback.InLoopCallback`:
    `CallbackRegistry.register` looks for the METHOD, not for a base class, so
    what is required is the exact name and signature and nothing else. Written
    out rather than imported because importing mflux's Protocol would put a
    third-party import at module scope, and this file is stdlib-only at import
    time so its logic can be tested without Metal.

    The keyword names matter: mflux calls `call_in_loop(t=…, seed=…, …)`.
    """

    def call_in_loop(self, t, seed, prompt, latents, config, time_steps):
        request = _request
        job = request.get("job")
        steps = request.get("steps") or 0
        started = request.get("step_times")
        if started is None:
            return
        now = time.time()
        started.append(now - request["last"])
        request["last"] = now
        # `t` is the index of the step just taken.
        done = t + 1
        average = sum(started) / len(started) if started else None
        remaining = (steps - done) * average if average else None
        # `report_or_cancel`, not `report`: this callback is the ONLY point in a
        # minutes-long `generate_image()` where a stop can be honoured, and the
        # reply to this tick is how the ✕ gets here. Same sentence, same fields
        # and same unit as the diffusers runner's — two engines, one row.
        worker_base.report_or_cancel(
            job=job, kind="task", unit="", done=done, total=steps,
            detail="Denoising — step %d/%d%s" % (done, steps, _eta(remaining)))
        if worker_base.CANCEL.is_set():
            # Straight out of `generate_image()`: mflux's loop catches only
            # `KeyboardInterrupt`, so this is not swallowed and turned into a
            # half-rendered image — it unwinds the call, which is what a ✕ means.
            raise worker_base.Cancelled()
        # The live thumbnail, from the SAME hook and for the same reason the
        # progress tick is here: this is the only place in a minutes-long
        # `generate_image()` where anything can be seen. A CLOSURE, not the
        # array — a sink that is not writing must not be charged for the
        # conversion, which is what keeps the branch out of this loop and the
        # two runners' callbacks the same shape.
        sigma = _sigma_after(config, t)
        if sigma is not None:
            request["preview"].add(lambda: _as_numpy(latents), sigma=sigma,
                                   grid=request["grid"])


def generate(body):
    """Render one image. Returns `{path, seconds, seed, width, height, steps}`.

    Byte-for-byte the diffusers runner's parameters and reply. The defaults are
    ITS defaults too — 28 steps and guidance 4.0, rather than mflux's own 4 and
    1.0 — because a caller that omits them must get the same picture-making
    behaviour from either engine. Switching engines is a performance decision,
    not a silent change to what an unparameterised render means.
    """
    model = _loaded.get("model")
    if model is None:
        raise RuntimeError("no model is loaded")

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

    started = time.time()
    # The live thumbnail. A no-op when the request named no preview file or when
    # nothing has been fitted for this model's latent space — see `preview.sink`.
    frames = preview.sink(body.get("outPreview"), _loaded.get("vae"))
    # Published before the call and cleared after it, so the registered reporter
    # is reporting about THIS request and no other.
    _request.clear()
    _request.update({"job": job, "steps": steps, "step_times": [], "last": started,
                     "preview": frames, "grid": preview.token_grid(width, height)})
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=steps, detail="Denoising — step 0/%d" % steps)
    # The sink wraps the SAVE as well as the render: its exit is the lifecycle,
    # and a clean one means the real PNG has landed and the preview is now
    # duplicate bytes. A cancel or a failure discards it too (`preview.Sink`).
    with frames:
        try:
            image = model.generate_image(
                seed=seed, prompt=prompt, num_inference_steps=steps,
                height=height, width=width, guidance=guidance)
        finally:
            # Even on the cancel path: a reporter left pointing at a finished
            # request would tick a row that is closed.
            _request.clear()

        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        # `overwrite=True` is NOT optional. mflux's default resolves a colliding
        # path by writing somewhere ELSE (`ImageUtil.resolve_output_path`), and
        # the server has already told the caller where this image will be — so
        # the default would answer a request with a file at a path nobody was
        # given, while `out` stayed empty or stale. The server owns the
        # location; this process owns the pixels.
        image.save(out, overwrite=True)
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
