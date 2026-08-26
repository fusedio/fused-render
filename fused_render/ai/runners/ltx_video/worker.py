"""Text-to-video (+ audio) on LTX-2.3, through `ltx-2-mlx` (SPEC §40).

The video engine, and since D468 the only one (see the plan's "ltx-2-mlx,
not mlx-video" decision) — a pure-MLX, MIT-licensed port of LTX-2 that
renders joint audio+video from int4-distilled weights on a 16 GB Mac. It
loads a model INTO its own interpreter, the same shape `mflux_image`'s worker
uses — `ltx_pipelines_mlx.DistilledPipeline` reads MLX safetensors directly
rather than shelling out to a bundled binary.

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
does not offer). `_curated_file_set` names exactly the files read by
`DistilledPipeline.load()`, `_load_vae_encoder`/`_load_decoders` and
`_load_upsampler` (verified by reading `ltx_pipelines_mlx/_base.py`,
`distilled.py` and `ti2vid_two_stages.py` at the pinned commit) — fetching the
rest would be silent waste.

**Exactly ONE transformer file, chosen from the repo's own LISTING, not a
glob.** `dgrauet/ltx-2.3-mlx-q4` ships BOTH `transformer-distilled.
safetensors` (11.3 GB) AND `transformer-distilled-1.1.safetensors` (the same
11.3 GB again) — `_base.py::_resolve_safetensors` prefers the versioned one,
alphabetically latest, and never opens the other. A pattern like
`"transformer-distilled*.safetensors"` matches BOTH names and `huggingface_
hub` would fetch both — 11.3 GB (q4) to 20.6 GB (q8) of dead weight this
runner would never load. `_resolve_versioned_name` mirrors `_resolve_
safetensors`'s own rule against the Hub's file LISTING instead of a local
directory, so `download` asks for the one file the loader will actually
open — checking the repo's shape before a byte moves.
"""

import fnmatch
import glob
import os
import sys
import threading
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import formats  # noqa: E402 - LTX_SPLIT_MANIFEST; see formats.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The constructed pipeline. One per process — `DistilledPipeline.__init__` is
#: cheap (it only resolves paths and builds the composition blocks; the actual
#: weights load happens inside `generate_and_save`, once per render, exactly
#: as upstream's own CLI drives it).
_loaded = {}

#: The MLX streams every thread in this process works on, keyed by device name —
#: ONE PER DEVICE. See `_pin_stream`.
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


def _pin_stream():
    """Put this thread's MLX work on the process's shared streams — EVERY device.

    `mflux_image/worker.py::_pin_stream` is the same function with the long
    story: from mlx 0.32 the default stream belongs to the THREAD that made
    it, an unevaluated array is a graph pinned to the stream it was built on,
    and forcing it from another thread aborts with `There is no Stream(cpu, 0)
    in current thread`. Copied rather than imported, for the reason
    `mlx_embed/worker.py`'s copy gives: these workers run in separate venvs
    with no shared module between them.

    **This runner reaches that abort on the SECOND render, not the first** —
    which is why the pin was missed here when the four sibling MLX runners got
    it. `DistilledPipeline` loads its weights inside `generate_and_save`, so
    render #1 builds and evaluates everything on one `ThreadingTCPServer`
    request thread and passes; the pipeline then KEEPS those components
    (`self.dit`, the VAE blocks, `self.upsampler` — `low_memory` frees some
    between stages, not all of them across calls), and render #2 arrives on a
    fresh request thread whose graph now mixes in render #1's arrays.
    Observed on mlx 0.32.1 against `dgrauet/ltx-2.3-mlx-q4`: renders #2 and #3
    both died at `distilled.py::generate_two_stage`'s `_materialize(video_
    upscaled)` — the first `mx.eval` after the upscaler runs — with exactly
    that message.

    `mx.cpu` as well as `default_device()`, `if key not in` rather than
    `setdefault` (which would mint a fresh stream per call and keep the
    first), and a no-op on an mlx too old to have the calls: all three are
    `mflux_image`'s, and its docstring says why each one is load-bearing.
    """
    import mlx.core as mx

    make = getattr(mx, "new_thread_unsafe_stream", None)
    pin = getattr(mx, "set_default_stream", None)
    if make is None or pin is None:
        return None
    devices = [mx.cpu, mx.default_device()]
    with _STREAMS_LOCK:
        streams = []
        for device in devices:
            key = str(device)
            if key not in _STREAMS:
                _STREAMS[key] = make(device)
            if _STREAMS[key] not in streams:
                streams.append(_STREAMS[key])
    for stream in streams:
        pin(stream)
    return streams


#: Upstream's own default (`DistilledPipeline.__init__`, `ltx_pipelines_mlx.
#: distilled`) — named explicitly here rather than left to the pipeline's own
#: default so the id THIS worker downloads is provably the id that loads, and
#: so `catalog.py`'s size note is sizing the checkpoint that is actually
#: fetched. LTX-2.5 conversions ship a tuned encoder in-directory instead
#: (`gemma4-12b-ltx-v1/`) and skip this download entirely — see
#: `resolve_text_encoder` upstream — but the curated LTX-2.3 weights this
#: runner targets (Task 6) do not carry one.
_GEMMA_MODEL_ID = "mlx-community/gemma-3-12b-it-4bit"

#: The file names that carry no version ambiguity — read by `DistilledPipeline.
#: load()`, `_load_vae_encoder`/`_load_decoders`, or `LTXModelConfig.from_
#: checkpoint_dir` (verified by reading `_base.py`, `distilled.py` and
#: `_orchestration.py` at the pinned commit) and present under exactly one
#: name on every curated repo. The transformer and the spatial upscaler are
#: NOT here — see `_distilled_transformer_filename` and `_spatial_upscaler_
#: filename`, which pick a single winning name out of the repo's own listing
#: rather than a glob that could match more than one real file.
#:
#: `formats.LTX_SPLIT_MANIFEST` ("split_model.json") is included too, and it
#: is the one entry `DistilledPipeline` never opens at all — it exists so
#: `formats.has_ltx_split_layout` can tell a cached download of THIS engine's
#: layout apart from an ordinary directory of MLX safetensors. Without it on
#: disk, a real `dgrauet/ltx-2.3-mlx-q4` download fails that predicate,
#: `loaders()` falls through to the plain-safetensors branch, and the AI
#: Models page offers the checkpoint as `mlx-text` — a Load button for an
#: LTX-2.3 model aimed at a chat runner. `test_ai_ltx_video_worker.py`'s own
#: `test_the_downloaded_file_set_is_recognised_by_loaders` is the seam test
#: that catches this drifting apart again; it failed before this file
#: carried the manifest, and this comment is why it stopped.
_FIXED_FILES = (
    "config.json",
    "embedded_config.json",
    "connector.safetensors",
    "vae_encoder.safetensors",
    "vae_decoder.safetensors",
    "audio_vae.safetensors",
    "vocoder.safetensors",
    formats.LTX_SPLIT_MANIFEST,
)


def _resolve_versioned_name(names, stem):
    """Mirrors `_base.py::_resolve_safetensors`'s own rule — prefer a
    versioned `{stem}-*.safetensors`, alphabetically latest; else the plain
    `{stem}.safetensors` — against a Hub file LISTING rather than a local
    directory, so `download` can ask for the one file the loader will
    actually open instead of every name that could conceivably match.
    Returns None when neither form is present in `names`.
    """
    versioned = sorted(name for name in names
                       if fnmatch.fnmatch(name, f"{stem}-*.safetensors"))
    if versioned:
        return versioned[-1]
    plain = f"{stem}.safetensors"
    return plain if plain in names else None


def _distilled_transformer_filename(names):
    """The one transformer file `DistilledPipeline.load()` would actually
    open: `transformer.safetensors` if present (no curated repo ships this
    name today, but upstream tries it FIRST), else the versioned-preferred
    `transformer-distilled*` — `_resolve_versioned_name`'s own rule. `None`
    when the repo has neither, which `download` treats as a refusal."""
    if "transformer.safetensors" in names:
        return "transformer.safetensors"
    return _resolve_versioned_name(names, "transformer-distilled")


#: Checked by NAME before construction, the same "refuse by name before
#: touching the library" trade `mflux_image.load` also
#: make — the alternative is a `FileNotFoundError` deep inside `DistilledPipeline.
#: load()` on the FIRST render, minutes into a job, rather than at Download time.
_DISTILLED_TRANSFORMER_GLOB = "transformer-distilled*.safetensors"


def _spatial_upscaler_filename(names):
    """The one stage-2 upscaler file `_load_upsampler` would actually open —
    `ti2vid_two_stages.py`'s own stem preference order, `ltx-2.3-spatial-
    upscaler-x2` before `spatial_upscaler_x2_v1_1`, each resolved the same
    versioned-preferred way. `None` when the repo has neither (refused by
    `_load_upsampler` itself at render time with its own clear message —
    this function does not duplicate that refusal, only its file choice)."""
    for stem in ("ltx-2.3-spatial-upscaler-x2", "spatial_upscaler_x2_v1_1"):
        resolved = _resolve_versioned_name(names, stem)
        if resolved is not None:
            return resolved
    return None


def download(model_id):
    """The curated file set from `model_id`, plus the Gemma-3 text encoder.

    Both fetches are reported — neither is optional. `download_snapshot`
    raises on a real failure (a bad id, an exhausted retry budget, a ✕
    mid-fetch) and that propagates here uncaught: a render that cannot encode
    a single prompt is not a render this runner can offer, unlike the VAD
    detector `mlx_whisper/worker.py` shrugs off.

    The listing call is the same trade `download`
    makes before a single byte moves: cheap, and it is what lets this
    function refuse a repo with no distilled transformer — or build a
    patterns list that names exactly one real file per group — before
    spending any of a user's bandwidth on files nobody is going to open.

    **`worker_base.download_plan`, not two bare `download_snapshot` calls**
    (SPEC AI-5n, D496). Two sequential calls each report their OWN repo's
    total, so the bar read 19.1 GB for the weights and then jumped straight to
    "complete" having never shown the 8.07 GB the Gemma-3 text encoder was
    always going to cost too — 30% short of the catalog's own `size_gb`, which
    counts both repos as AI-11a requires. `download_plan` prices the two
    phases as the one download the button actually started.
    """
    import huggingface_hub

    try:
        names = huggingface_hub.list_repo_files(model_id)
    except Exception as error:  # noqa: BLE001 - a Hub lookup failure is a fact
                                 # about the id/network, not a bug in this runner
        raise RuntimeError(
            f"could not read {model_id}'s file listing: {error}") from error

    transformer_name = _distilled_transformer_filename(names)
    if transformer_name is None:
        raise RuntimeError(
            f"{model_id} has no transformer-distilled*.safetensors — this "
            "runner loads ltx-2-mlx's DistilledPipeline, which reads an "
            "LTX-2.3 checkpoint converted by mlx-forge in this exact layout. "
            "A Diffusers or torch LTX repo will not load here.")

    patterns = list(_FIXED_FILES) + [transformer_name]
    upscaler_name = _spatial_upscaler_filename(names)
    if upscaler_name is not None:
        patterns.append(upscaler_name)
        # `Path(...).stem` rather than a plain suffix strip: the upstream
        # naming convention (`_load_upsampler`'s own `f"{weights_path.stem}_
        # config.json"`) is stem-based, and this keeps the two in lockstep
        # regardless of which stem won above.
        import pathlib

        patterns.append(f"{pathlib.Path(upscaler_name).stem}_config.json")

    fetched, _gemma = worker_base.download_plan([
        (model_id, patterns, None),
        (_GEMMA_MODEL_ID, None, None),
    ])
    return fetched


def _put_ffmpeg_on_path():
    """Make `imageio_ffmpeg`'s bundled binary resolvable as plain `ffmpeg`.

    **Not an environment-variable handoff.** A worker shelling out to a
    compiled binary can point one at it by that binary's own convention;
    `ltx_core_mlx.
    utils.ffmpeg.find_ffmpeg()` has no such override; it is a bare `shutil.
    which("ffmpeg")` (verified by reading it at the pinned commit), so the
    only lever this process has is PATH itself.

    **A symlink is unavoidable — prepending the binary's own directory does
    NOT work.** MEASURED against the installed `imageio-ffmpeg` 0.6.0 wheel
    (2026-08-23: `uv pip install --target . imageio-ffmpeg` into a scratch
    directory, then listed `imageio_ffmpeg/binaries/`): it holds exactly one
    file, `ffmpeg-macos-aarch64-v7.1` — platform-and-version-qualified, NOT
    named `ffmpeg` at all. `shutil.which` matches on the exact basename, so
    an earlier version of this function that only prepended `dirname(get_
    ffmpeg_exe())` never actually resolved anything: `find_ffmpeg()` still
    returned `None` on any machine with no SYSTEM ffmpeg already on PATH,
    and the render died mid-flight inside `ltx_pipelines_mlx.utils.media_io`
    the first time it shelled out. A fresh temp directory holding one link
    literally named `ffmpeg` — pointing at the real binary — is what
    `shutil.which("ffmpeg")` actually needs to find.

    Symlinked on POSIX (no copy of a ~70-90MB binary); copied on Windows,
    where `os.symlink` needs a privilege (Developer Mode, or an elevated
    process) this worker cannot assume it has — a real cost on that
    platform, but `ltx-video` is Apple-Silicon-gated in the registry and
    never actually runs there; this only has to not crash a portable test
    suite.

    Idempotent and called from `load()` rather than `generate()`: this
    process renders one video at a time behind `worker_base.GENERATE_LOCK`,
    so there is nothing to gain from redoing it per request, and PATH is a
    process-wide fact this worker owns outright — no other code here reads
    or depends on it being unset.
    """
    import shutil
    import tempfile

    import imageio_ffmpeg

    real_exe = imageio_ffmpeg.get_ffmpeg_exe()
    link_dir = tempfile.mkdtemp(prefix="fused-render-ltx-ffmpeg-")
    link_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    link_path = os.path.join(link_dir, link_name)
    if sys.platform == "win32":
        shutil.copyfile(real_exe, link_path)
        os.chmod(link_path, 0o755)
    else:
        os.symlink(real_exe, link_path)
    path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        [link_dir, path] if path else [link_dir])


def load(model_id, fetched):
    """`fetched` is what `download` returned — the weights snapshot directory.

    No weights are loaded here — `DistilledPipeline.__init__` composes the
    lazy blocks and nothing more, and the actual DiT/VAE/upsampler load
    happens inside `generate_and_save`, once per render. What this DOES do,
    once, is the same refusal `mflux_image.load` makes for
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

    # Before the pipeline is built, not after: `__init__` composes MLX modules
    # on THIS (bring-up) thread, and `generate` runs on another. See
    # `_pin_stream`.
    _pin_stream()

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


def peak_memory():
    """The HIGH-WATER mark of what MLX has allocated over this process's whole
    life, in bytes — SPEC AI-8c, D495. Where `memory()` above answers "right
    now", this answers "at its worst", which is the number `fit` (AI-16) needs
    and `memory()` cannot supply: `DistilledPipeline(low_memory=True)` frees
    the transformer and the Gemma text encoder BETWEEN stages, so the resident
    figure at any moment `/health` happens to be polled is one stage's worth,
    never a bound on the whole render.

    `mx.get_peak_memory()` is maintained by MLX's own allocator across the
    process's whole life — not a sample this process has to keep taking, the
    allocator already knows. Probed through the same defensive getattr PAIR
    `memory()` above uses for the active-memory reading: the spelling moved
    from `mlx.core.metal` into `mlx.core` and the old one is deprecated, so a
    version skew costs the better answer rather than raising inside `/health`.
    """
    import mlx.core as mx

    for probe in (getattr(mx, "get_peak_memory", None),
                  getattr(getattr(mx, "metal", None), "get_peak_memory", None)):
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


def _assert_denoise_tqdm_hook_exists(samplers):
    """The premise `_StepTicker` depends on, checked before it is relied on.

    `denoise_loop` (`ltx_pipelines_mlx/utils/samplers.py`) wraps its per-step
    iterator with a bare module-level `tqdm(...)` call — `from tqdm import
    tqdm` at the top of that file — and has no other per-step hook at all
    (`utils/progress.py`'s `phase()` markers are per-STAGE, not per-step).
    `generate` replaces that name for the duration of a render to get both
    progress and cancellation out of the only seam upstream offers.

    **Why this check exists rather than a bare `setattr`.** `setattr(module,
    "tqdm", shim)` would SUCCEED even if upstream renamed the import — it
    would just create an attribute nothing reads, since `denoise_loop`'s
    compiled bytecode names whatever identifier its own `import` statement
    bound. The render would then proceed with the REAL tqdm silently back in
    control: no per-step ticks, no cancel point, and every other test in this
    file still green because they all fake this attribute into existence
    themselves. A rename upstream must fail a real render loudly instead —
    which is what raising here, before touching `generate_and_save` at all,
    guarantees.
    """
    if not hasattr(samplers, "tqdm"):
        raise RuntimeError(
            "ltx_pipelines_mlx.utils.samplers has no module-level `tqdm` "
            "attribute to replace. This runner reports per-step progress "
            "and honours the × by patching that name for the duration "
            "of a render; upstream must have renamed or removed the import "
            "this depends on, and progress/cancellation need to be "
            "re-derived against whatever replaced it.")


class _StepTicker:
    """A `tqdm`-compatible stand-in: `denoise_loop` calls `tqdm(steps, desc=
    …, disable=…)` and iterates the result with a plain `for`, so this only
    has to be CALLABLE with that signature and return something iterable —
    no `tqdm` import, no class hierarchy, nothing upstream inspects beyond
    those two things.

    **One instance covers the whole render, not one per `denoise_loop`
    call.** `DistilledPipeline.generate_two_stage` calls `denoise_loop` TWICE
    — stage 1 (`stage1_steps` items) then stage 2's fixed 3-step refine —
    and both go through the same patched name in the same module. The row
    therefore moves twice, restarting at 0 for stage 2: that is the two-stage
    pipeline's own shape (`DistilledPipeline` is inherently two internal
    passes for any request), not a bug in this shim, and it is the same
    "bare done/total, no clock" trade already made for
    its own subprocess's progress lines.

    Ticks BEFORE each step's work rather than after: `tqdm.__iter__` yields
    control back to the loop body immediately, so there is no "step just
    finished" moment to hook without wrapping the loop body itself, which is
    exactly the private, upstream-owned code this shim must not reach into.
    `done=N` therefore reads as "N steps completed so far, about to start the
    next" — the same reading a pre-spawn "step 0/N"
    tick gives its row before a single frame has rendered.
    """

    def __init__(self, job):
        self.job = job

    def __call__(self, iterable, desc=None, disable=False, **_kwargs):
        items = list(iterable)
        total = len(items)
        label = desc or "Denoising"
        for done, item in enumerate(items):
            worker_base.report_or_cancel(
                job=self.job, kind="task", unit="", done=done, total=total,
                # `label`, not "label — step N/M": the row renders `done`/`total`
                # itself (see torch_image.py's tick). The label still carries
                # the one thing the numbers cannot say, which stage is running
                # — tqdm's own `desc` distinguishes "Denoising (stage 2)".
                detail=label)
            yield item


def generate(body):
    """Render one video. Returns `{path, seconds, seed, width, height, frames,
    steps}` — the shape the video job has always emitted, so a page cannot tell which
    engine rendered for it.

    `steps` maps to `stage1_steps` — the one denoising-step count this API
    exposes, matching the registry traits table's "8 steps" default
    (Task 5). `stage2_steps` is left at the pipeline's own default (3): the
    distilled model runs two internal stages for any request, and exposing a
    second step count nobody asked for would be a knob with no answer to
    "why would I change this" — the same reasoning that keeps `guidance` off
    the API entirely for this engine.
    """
    # FIRST, before a single MLX array is touched: this is a fresh
    # `ThreadingTCPServer` request thread, and the components a previous render
    # left on the pipeline were built on a different one. See `_pin_stream`.
    _pin_stream()

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

    from ltx_pipelines_mlx.utils import samplers

    _assert_denoise_tqdm_hook_exists(samplers)
    original_tqdm = samplers.tqdm
    samplers.tqdm = _StepTicker(job)
    try:
        pipeline.generate_and_save(
            prompt=prompt, output_path=out, height=height, width=width,
            num_frames=frames, frame_rate=_FRAME_RATE, seed=seed,
            stage1_steps=steps)
    finally:
        # Restored unconditionally — on the cancel path too. This is
        # process-wide state on a third-party module, not this request's
        # own; leaving it patched past `generate`'s return (or its raise)
        # would have the NEXT render's denoise loop reporting through a
        # `_StepTicker` bound to a job that has already ended.
        samplers.tqdm = original_tqdm

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
                      streaming=False, memory=memory, peak_memory=peak_memory)
