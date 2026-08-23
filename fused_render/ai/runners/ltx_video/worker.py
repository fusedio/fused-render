"""Text-to-video (+ audio) on LTX-2.3, through `ltx-2-mlx` (SPEC §40).

The accessible video engine (see the plan's "ltx-2-mlx, not mlx-video"
decision) — a pure-MLX, MIT-licensed port of LTX-2 that renders joint
audio+video from int4-distilled weights on a 16 GB Mac, where `h3-video`'s
144.1 GB checkpoint needs 64 GB-class RAM. Unlike `h3_video/worker.py`, this
runner loads a model INTO its own interpreter, the same shape `mflux_image`'s
worker uses — `ltx_pipelines_mlx.DistilledPipeline` reads MLX safetensors
directly rather than shelling out to a bundled binary.

**Two repos, not one, and both are reported downloads.** `DistilledPipeline`
needs the LTX weights AND a Gemma-3 text encoder it does not ship — upstream's
own default names `mlx-community/gemma-3-12b-it-4bit`, and `download` fetches
that id explicitly (rather than letting the pipeline's own `mlx_lm.load`
reach for it lazily on first render) for the same reason `mlx_whisper/
worker.py` pre-fetches its speech detector: a "Download" that leaves a cache
which cannot work offline has not done the thing the button said it would.
Unlike that VAD prefetch, this one is NOT best-effort — the pipeline cannot
encode a single prompt without it, so a failure here fails the whole
download, exactly like the primary weights repo.

**Only a narrow file set is fetched from the weights repo.** `dgrauet/
ltx-2.3-mlx-q4` (and its byte-identical q8 sibling) also carries a `transformer-
dev.safetensors` (the non-distilled, CFG transformer `DistilledPipeline` never
touches), two `ltx-2.3-22b-distilled-lora-384*.safetensors` (fused into the
NON-distilled two-stage pipeline only — `distilled.py`'s own `load()` loads
the distilled checkpoint directly, no LoRA fusion), and a `spatial_upscaler_
x1_5_v1_0*` / `temporal_upscaler_x2_v1_0*` pair (read by pipelines this build
does not offer). `_ALLOW_PATTERNS` is exactly the file set read by
`DistilledPipeline.load()`, `_load_vae_encoder`/`_load_decoders` and
`_load_upsampler` (verified by reading `ltx_pipelines_mlx/_base.py`,
`distilled.py` and `ti2vid_two_stages.py` at the pinned commit) — fetching the
rest would be silent waste, the same trade `h3_video/worker.py`'s own
`FL2VA/*`-only download makes against the FL2VA/Ref2VA split.
"""

import glob
import os
import sys
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The constructed pipeline. One per process — `DistilledPipeline.__init__` is
#: cheap (it only resolves paths and builds the composition blocks; the actual
#: weights load happens inside `generate_and_save`, once per render, exactly
#: as upstream's own CLI drives it).
_loaded = {}

#: Upstream's own default (`DistilledPipeline.__init__`, `ltx_pipelines_mlx.
#: distilled`) — named explicitly here rather than left to the pipeline's own
#: default so the id THIS worker downloads is provably the id that loads, and
#: so `catalog.py`'s size note is sizing the checkpoint that is actually
#: fetched. LTX-2.5 conversions ship a tuned encoder in-directory instead
#: (`gemma4-12b-ltx-v1/`) and skip this download entirely — see
#: `resolve_text_encoder` upstream — but the curated LTX-2.3 weights this
#: runner targets (Task 6) do not carry one.
_GEMMA_MODEL_ID = "mlx-community/gemma-3-12b-it-4bit"

#: What `DistilledPipeline` (and the shared VAE/upsampler blocks it composes)
#: actually opens out of the weights repo — see the module docstring for how
#: this list was derived and what it deliberately excludes.
#:
#: `transformer-distilled*.safetensors` (a glob, not the plain name) because
#: `_base.py::_resolve_safetensors` prefers a versioned file
#: (`transformer-distilled-1.1.safetensors`) over the unversioned one when
#: both exist, taking the alphabetically latest — `dgrauet/ltx-2.3-mlx-q4`
#: ships both today, and a pattern naming only one would silently fetch the
#: file the loader is NOT going to prefer. Same reasoning for the spatial
#: upscaler's two possible stems.
_ALLOW_PATTERNS = [
    "config.json",
    "embedded_config.json",
    "connector.safetensors",
    "transformer-distilled*.safetensors",
    "vae_encoder.safetensors",
    "vae_decoder.safetensors",
    "audio_vae.safetensors",
    "vocoder.safetensors",
    "spatial_upscaler_x2_v1_1*.safetensors",
    "spatial_upscaler_x2_v1_1*.json",
    "ltx-2.3-spatial-upscaler-x2*.safetensors",
    "ltx-2.3-spatial-upscaler-x2*.json",
]

#: What a `DistilledPipeline`-readable snapshot always has: a distilled
#: transformer, under either name `_resolve_safetensors` accepts. Checked by
#: NAME before construction, the same "refuse by name before touching the
#: library" trade `mflux_image.load` and `h3_video.load` both make — the
#: alternative is a `FileNotFoundError` deep inside `DistilledPipeline.load()`
#: on the FIRST render, minutes into a job, rather than at Download/Load time.
_DISTILLED_TRANSFORMER_GLOB = "transformer-distilled*.safetensors"


def download(model_id):
    """The curated file set from `model_id`, plus the Gemma-3 text encoder.

    Both fetches are reported — neither is optional. `download_snapshot`
    raises on a real failure (a bad id, an exhausted retry budget, a ✕
    mid-fetch) and that propagates here uncaught: a render that cannot encode
    a single prompt is not a render this runner can offer, unlike the VAD
    detector `mlx_whisper/worker.py` shrugs off.
    """
    fetched = worker_base.download_snapshot(model_id, allow_patterns=_ALLOW_PATTERNS)
    worker_base.download_snapshot(_GEMMA_MODEL_ID)
    return fetched


def _put_ffmpeg_on_path():
    """Make `imageio_ffmpeg`'s bundled binary resolvable as plain `ffmpeg`.

    **Not the same mechanism `h3_video/worker.py` uses, and it cannot be** —
    that worker points an environment variable (`H3_FFMPEG`) at the binary
    because h3.c reads that variable by its own convention. `ltx_core_mlx.
    utils.ffmpeg.find_ffmpeg()` has no such override; it is a bare `shutil.
    which("ffmpeg")` (verified by reading it at the pinned commit), so the
    only lever this process has is PATH itself. Prepending the bundled
    binary's directory — rather than exporting a fixed `ffmpeg` shim — keeps
    a real system ffmpeg (if one happens to be ahead on this process's PATH
    already) from silently winning by directory order; measured: `imageio_
    ffmpeg.get_ffmpeg_exe()` returns a path whose directory holds a binary
    literally named `ffmpeg` (or `ffmpeg.exe` on Windows), so prepending its
    directory is enough for `shutil.which` to resolve it first.

    Idempotent and called from `load()` rather than `generate()`: this
    process renders one video at a time behind `worker_base.GENERATE_LOCK`,
    so there is nothing to gain from redoing it per request, and PATH is a
    process-wide fact this worker owns outright — no other code here reads
    or depends on it being unset.
    """
    import imageio_ffmpeg

    ffmpeg_dir = os.path.dirname(imageio_ffmpeg.get_ffmpeg_exe())
    path = os.environ.get("PATH", "")
    if ffmpeg_dir not in path.split(os.pathsep):
        os.environ["PATH"] = os.pathsep.join(
            [ffmpeg_dir, path] if path else [ffmpeg_dir])


def load(model_id, fetched):
    """`fetched` is what `download` returned — the weights snapshot directory.

    No weights are loaded here — `DistilledPipeline.__init__` composes the
    lazy blocks and nothing more, and the actual DiT/VAE/upsampler load
    happens inside `generate_and_save`, once per render. What this DOES do,
    once, is the same refusal `mflux_image.load` and `h3_video.load` make for
    a repo in the wrong shape: a snapshot with no distilled transformer under
    either name is not something this pipeline can open, and finding that out
    now — with a sentence — beats a `FileNotFoundError` raised deep inside the
    library on the first render.
    """
    has_distilled = (
        os.path.isfile(os.path.join(fetched, "transformer.safetensors"))
        or bool(glob.glob(os.path.join(fetched, _DISTILLED_TRANSFORMER_GLOB)))
    )
    if not has_distilled:
        raise RuntimeError(
            f"{model_id} has no transformer-distilled*.safetensors — this "
            "runner loads ltx-2-mlx's DistilledPipeline, which reads an "
            "LTX-2.3 checkpoint converted by mlx-forge in this exact layout. "
            "A Diffusers or torch LTX repo will not load here.")

    _put_ffmpeg_on_path()

    from ltx_pipelines_mlx.distilled import DistilledPipeline

    # `low_memory=True` (the pipeline's own default) is left alone rather than
    # named explicitly: it is the setting that keeps this runner inside the
    # ~30GB envelope the catalog note promises, by freeing the transformer and
    # the text encoder between stages instead of holding every component
    # resident at once. Naming it here would only invite someone to "speed
    # this up" by flipping it on a machine the catalog note is written for.
    _loaded["pipeline"] = DistilledPipeline(
        model_dir=fetched, gemma_model_id=_GEMMA_MODEL_ID)
    # LTX-2.3 on MLX is Metal-only, like every other Apple-Silicon-gated
    # runner here — there is nothing to detect, but the page still shows it.
    worker_base.set_state(device="mps")


def memory():
    """What MLX itself says it is holding, in bytes — `mflux_image.memory`'s
    own probe, verbatim: the pipeline holds nothing itself (its blocks are
    plain attributes of MLX modules), so MLX's own allocator is the only
    honest source, the same as every other MLX runner here."""
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


#: LTX-2.3 was trained at 24fps (upstream CLI's own `--frame-rate` help text);
#: values far from it drift out of distribution. Not exposed on the API for
#: the same reason `guidance` is not (see the plan's decision table) — there
#: is no dial upstream itself recommends turning.
_FRAME_RATE = 24.0


def generate(body):
    """Render one video. Returns `{path, seconds, seed, width, height, frames,
    steps}` — `h3_video.generate`'s own shape, so a page cannot tell which
    engine rendered for it.

    `steps` maps to `stage1_steps` — the one denoising-step count this API
    exposes, matching the registry traits table's "8 steps" default
    (Task 5). `stage2_steps` is left at the pipeline's own default (3): the
    distilled model runs two internal stages for any request, and exposing a
    second step count nobody asked for would be a knob with no answer to
    "why would I change this" — the same reasoning that keeps `guidance` off
    the API entirely for this engine.
    """
    pipeline = _loaded.get("pipeline")
    if pipeline is None:
        raise RuntimeError("no model is loaded")

    prompt = str(body.get("prompt") or "")
    width = int(body.get("width") or 704)
    height = int(body.get("height") or 480)
    frames = int(body.get("frames") or 97)
    steps = int(body.get("steps") or 8)
    seed = int(body.get("seed") or 0)
    out = str(body.get("out") or "")
    job = body.get("job") or None
    if not out:
        raise ValueError("'out' must be the path to write the video to")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    started = time.time()
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=steps, detail="Rendering — step 0/%d" % steps)

    pipeline.generate_and_save(
        prompt=prompt, output_path=out, height=height, width=width,
        num_frames=frames, frame_rate=_FRAME_RATE, seed=seed,
        stage1_steps=steps)

    return {
        "path": out,
        "seconds": round(time.time() - started, 2),
        "seed": seed,
        "width": width,
        "height": height,
        "frames": frames,
        "steps": steps,
    }


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
