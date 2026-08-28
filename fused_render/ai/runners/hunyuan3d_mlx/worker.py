"""Image-to-3D shape generation on Hunyuan3D-2.1, through `hy3dshape` (SPEC §48).

The shape engine — a pure-MLX port of Tencent's Hunyuan3D-2.1 DiT shape
pipeline, from `iamsdas/Hunyuan3D-2.1-mlx` (a fork of `dgrauet/Hunyuan3D-2.1-
mlx`; see this folder's `pyproject.toml` for why it is a fork and what the
fork actually changes — packaging only). It loads a model INTO its own
interpreter, the same shape `mflux_image`'s and `ltx_video`'s workers use:
`hy3dshape.pipeline_mlx.ShapePipeline` reads MLX safetensors directly rather
than shelling out to a bundled binary.

**Shape only.** Upstream's fork also ports the Stage 2 PBR texture pipeline
(`hy3dpaint`) — a second ~7.6 GB of weights, a six-view diffusion render, and
a real-ESRGAN super-res pass. This runner never imports that half; a mesh
here is untextured geometry, exported straight from the VAE's marching-cubes
decode. See the plan's "shape only" decision for why that tier is deferred
rather than built alongside this one.

**The import bypass, and why it exists.** `hy3dshape/hy3dshape/__init__.py`
unconditionally imports the PyTorch reference pipeline (`from .pipelines
import ...`, which does a bare `import torch` and `from diffusers...`), and
so do three of its ancestor packages on the way down to the MLX-only modules
this runner actually needs (`models/__init__.py` imports the PT `ShapeVAE`
and `Hunyuan3DDiT`; `models/autoencoders/__init__.py` and `models/denoisers/
__init__.py` do the same for their own PT siblings). Python always executes
a package's `__init__.py` before any submodule of it — there is no way to
import `hy3dshape.pipeline_mlx` through the ordinary `import` statement
without also paying for `import torch`, `diffusers`, `transformers`,
`torchvision`, `pymeshlab`, `rembg` and `onnxruntime`, none of which this
runner's `pyproject.toml` declares and none of which the MLX pipeline itself
ever touches (verified by reading `pipeline_mlx.py` and its three sibling
`_mlx.py` modules at the pinned commit — every import in all four is
stdlib, `mlx`, `mlx_arsenal`, `numpy`, or `PIL`).

`_shape_pipeline_class` below reaches `ShapePipeline` a different way:
before ANY `hy3dshape` name is looked up, it registers hand-built stand-in
modules for `hy3dshape`, `hy3dshape.models`, `hy3dshape.models.autoencoders`
and `hy3dshape.models.denoisers` directly in `sys.modules` — plain
`types.ModuleType` objects with `__path__` set to the real package
directories and nothing else. Python's import machinery treats a name
already present in `sys.modules` as already imported and never re-executes
it, so when `pipeline_mlx.py`'s own `from .models.conditioner_mlx import
ImageEncoder` (and its two siblings) resolve their parent packages, they
find these stand-ins instead of the real, torch-importing `__init__.py`
files — and then fall through to the ordinary path-based finder for the
leaf submodule itself (`conditioner_mlx.py`, `hunyuandit_mlx.py`, `model_
mlx.py`), which is untouched, unforked, real upstream code. Zero bytes of
`hy3dshape` are modified to make this work; the stand-ins exist only in this
process's `sys.modules`, for the lifetime of this worker.

This is worth the indirection ONLY because it is the difference between a
runner whose venv holds the MLX stack alone (this folder's `pyproject.toml`)
and one that also drags in a full PyTorch/diffusers/transformers install for
code paths it never runs — the same "lean, Apple-Silicon-only, no dead
weight" bar `ltx_video` and `mflux_image` hold themselves to.
"""

import importlib.util
import os
import sys
import threading
import time
import types

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The constructed pipeline. One per process — `ShapePipeline.__init__` only
#: assigns its four component modules and casts their dtype; the actual
#: weights load happens in `from_pretrained`, once, in `load()` below.
_loaded = {}

#: The MLX streams every thread in this process works on, keyed by device
#: name — ONE PER DEVICE. See `_pin_stream`.
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


def _pin_stream():
    """Put this thread's MLX work on the process's shared streams — EVERY device.

    Copied from `mflux_image/worker.py::_pin_stream` (that docstring has the
    long story): from mlx 0.32 the default stream belongs to the THREAD that
    made it, an unevaluated array is a graph pinned to the stream it was
    built on, and forcing it from another thread aborts with `There is no
    Stream(cpu, 0) in current thread`. This runner needs it for the same
    reason `ltx_video` does — `load()` builds the pipeline (and its lazily-
    materialized weights) on the bring-up thread, while `generate()` runs on
    a fresh `ThreadingTCPServer` request thread every time.

    Copied rather than imported: this worker runs in its own venv with no
    module shared with its MLX siblings.
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


#: The exact file set `ShapePipeline.from_pretrained` opens — `config.json`,
#: `image_encoder.safetensors`, `dit.safetensors`, `vae.safetensors` for the
#: components, `split_model.json` for the DiT's quantization info if the
#: repo carries a quantized checkpoint (verified by reading `pipeline_mlx.
#: py::from_pretrained` at the pinned commit). Unlike `ltx_video`'s download,
#: there is no versioned-filename ambiguity to resolve here — every name is
#: fixed and none of them glob-match anything else the repo carries — so a
#: plain `allow_patterns` list is the whole download.
#:
#: The curated weights repo also carries the Stage 2 texture pipeline's
#: checkpoints (`paint_clip.safetensors`, `paint_dino.safetensors`, `paint_
#: unet.safetensors`, `paint_vae.safetensors`, `realesrgan_x4plus.
#: safetensors` — none read by this shape-only runner). Fetching only the
#: names below is what keeps the reported download at ~7.4 GB rather than
#: the repo's full ~15 GB (Task 6's catalog entry prices exactly this set).
_CURATED_FILES = (
    "config.json",
    "split_model.json",
    "image_encoder.safetensors",
    "dit.safetensors",
    "vae.safetensors",
)


def _shape_pipeline_class():
    """`hy3dshape.pipeline_mlx.ShapePipeline`, reached without ever running
    `hy3dshape`'s own (torch-importing) `__init__.py` chain. See this file's
    module docstring for why the bypass exists and how it works; this
    function is the whole mechanism.
    """
    if "ShapePipeline" in _loaded:
        return _loaded["ShapePipeline"]

    spec = importlib.util.find_spec("hy3dshape")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError(
            "the 'hy3dshape' package is not installed in this venv — check "
            "this folder's pyproject.toml [tool.uv.sources] pin")
    root = spec.submodule_search_locations[0]

    def stub_package(name, *parts):
        # A bare package stand-in: a name, a `__path__` pointing at the real
        # directory, nothing else. `find_spec`/`exec_module` never runs for
        # these — they are inserted directly into `sys.modules`, which is
        # the one thing that makes Python's import system treat a dotted
        # name as "already imported" and skip its `__init__.py` entirely.
        if name in sys.modules:
            return
        module = types.ModuleType(name)
        module.__path__ = [os.path.join(root, *parts)]
        module.__package__ = name
        sys.modules[name] = module

    # Every ancestor package `pipeline_mlx.py`'s relative imports touch —
    # and ONLY those. `conditioner_mlx.py`, `hunyuandit_mlx.py` and `model_
    # mlx.py` themselves need no stand-in: once their parent package is
    # present in `sys.modules` with the right `__path__`, the ordinary
    # path-based finder locates the leaf `.py` file on its own.
    stub_package("hy3dshape")
    stub_package("hy3dshape.models", "models")
    stub_package("hy3dshape.models.autoencoders", "models", "autoencoders")
    stub_package("hy3dshape.models.denoisers", "models", "denoisers")

    from hy3dshape.pipeline_mlx import ShapePipeline

    _loaded["ShapePipeline"] = ShapePipeline
    return ShapePipeline


def download(model_id):
    """The curated shape-only file set from `model_id`. See `_CURATED_FILES`."""
    return worker_base.download_snapshot(model_id, allow_patterns=list(_CURATED_FILES))


def load(model_id, fetched):
    """`fetched` is what `download` returned — the weights snapshot directory.

    `ShapePipeline.from_pretrained` accepts either a Hub repo id (which it
    would re-download) or a local directory; passing `fetched` — already on
    disk — takes the local-directory branch and touches the network again
    never. Weights load happens here, once, on this bring-up thread; `_pin_
    stream` runs first for the same reason `ltx_video.load` orders it first,
    ahead of anything that could build or evaluate an MLX array.
    """
    _pin_stream()
    ShapePipeline = _shape_pipeline_class()
    _loaded["pipeline"] = ShapePipeline.from_pretrained(fetched)
    # Hunyuan3D-MLX is Metal-only, like every other Apple-Silicon-gated
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
    """The HIGH-WATER mark of what MLX has allocated over this process's
    whole life, in bytes — SPEC AI-8c, D497. `mflux_image.peak_memory`'s own
    probe, verbatim, for the same reason: `fit` (AI-16) needs the worst-case
    figure, not a sample that happens to land after the DiT has already been
    freed (see `generate`'s own `del pipeline.dit`, upstream in `pipeline_
    mlx.py::__call__`)."""
    import mlx.core as mx

    for probe in (getattr(mx, "get_peak_memory", None),
                  getattr(getattr(mx, "metal", None), "get_peak_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


def release():
    """Hand MLX's allocator pool back to the OS — `mflux_image.release`'s own
    probe, verbatim. `worker_base.serve(release=...)` fires this `worker_
    base._RELEASE_IDLE_S` seconds after this worker's last execution if
    nothing new started by then, never per-call."""
    import mlx.core as mx

    clear = getattr(mx, "clear_cache", None)
    if clear is not None:
        clear()


# ------------------------------------------------------------------ generation

#: Upstream's own defaults (`ShapePipeline.__call__`'s signature, `pipeline_
#: mlx.py` at the pinned commit) — named explicitly here, not left to the
#: pipeline's own defaults, so the numbers this worker reports back match
#: what the registry traits table (Task 2) promises a caller who sends none
#: of these fields.
_DEFAULT_STEPS = 50
_DEFAULT_GUIDANCE = 5.0
_DEFAULT_OCTREE_RESOLUTION = 256


def generate(body):
    """Render one mesh. Returns `{path, seconds, seed, steps, guidance,
    octreeResolution, faces}`.

    **No mid-render progress ticks, and no mid-render INTERRUPTION point.**
    `ShapePipeline.__call__` runs its whole denoising loop and its VAE decode
    inside one Python call with no callback hook of any kind — unlike `ltx_
    video`'s patchable `samplers.tqdm` or `mflux_image`'s `mflux.callbacks`
    registry, there is no name in this pipeline a worker can stand in front
    of without editing the upstream loop itself, which this fork's packaging-
    only mandate rules out (see this file's module docstring). A single
    `report_or_cancel()` before the call is the honest shape of what this
    worker can say about a render in progress: it has started, and it has
    not yet returned.

    **A ✕ is checked at both ends of the one call this worker cannot
    interrupt, never in the middle of it — and this function checks BOTH
    cancellation channels explicitly, at both ends, rather than relying on
    one implicitly (code review, 2026-08-28, finding 2: neither channel was
    checked here at all before this).** `worker_base.CANCEL` is set directly
    by a `/cancel` POST to this worker (`supervisor.cancel_generation`);
    `report_or_cancel`'s `cancel_requested` is a SEPARATE signal, carried on
    THIS render's own job row (the download manager's ✕) and readable only
    by posting a tick to it. Checking before `pipeline(...)` catches a ✕
    pressed before the render actually started (the model may still have
    been loading); checking again immediately after catches one pressed
    WHILE the render ran, which cannot stop the compute already in flight
    but must still stop the artefact from landing on disk and the row from
    reading "done" — a cancelled render must not silently finish and
    succeed.
    """
    _pin_stream()

    pipeline = _loaded.get("pipeline")
    if pipeline is None:
        raise RuntimeError("no model is loaded")

    image = str(body.get("image") or "")
    if not image:
        raise ValueError("'image' must be the path to the input image")
    # `if ... is not None else default`, not `body.get(...) or default` —
    # `or` treats 0 as absent, and 0 is the one qualitative guidance value
    # (CFG off) this capability has. The route already clamps/validates
    # both before they ever reach a worker, but this file is not ONLY
    # reachable through that route (a test, or a future caller) and must
    # not reintroduce the bug the route was just fixed for (code review,
    # 2026-08-28, finding 1).
    steps_in = body.get("steps")
    steps = int(steps_in if steps_in is not None else _DEFAULT_STEPS)
    guidance_in = body.get("guidance")
    guidance = float(guidance_in if guidance_in is not None else _DEFAULT_GUIDANCE)
    octree_in = body.get("octreeResolution")
    octree_resolution = int(octree_in if octree_in is not None else _DEFAULT_OCTREE_RESOLUTION)
    seed = int(body.get("seed") or 0)
    out = str(body.get("out") or "")
    job = body.get("job") or None
    if not out:
        raise ValueError("'out' must be the path to write the mesh to")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)

    started = time.time()
    # `report_or_cancel`, not `report` — a ✕ already sitting on this job's
    # row (pressed while a cold model was still loading, before this
    # function was ever called) must stop the render before it starts,
    # exactly as the image and video routes' own pre-generation checks do.
    worker_base.report_or_cancel(job=job, state="running", kind="task", unit="",
                                 done=0, total=1, detail="Generating shape")
    # `CANCEL` is the OTHER channel (see this function's own docstring) —
    # checked here too, for the identical pre-start reason.
    if worker_base.CANCEL.is_set():
        raise worker_base.Cancelled()

    mesh = pipeline(
        image=image,
        num_inference_steps=steps,
        guidance_scale=guidance,
        octree_resolution=octree_resolution,
        seed=seed,
    )

    # The one checkpoint this pipeline offers after the compute it cannot be
    # interrupted during (see the docstring above): a ✕ from EITHER channel
    # must still stop the mesh from being written and the render from being
    # reported done, even though it could not stop the render itself.
    if worker_base.CANCEL.is_set():
        raise worker_base.Cancelled()
    worker_base.report_or_cancel(job=job, state="running", kind="task", unit="",
                                 done=1, total=1, detail="Generating shape")

    mesh.export(out)

    return {
        "path": out,
        "seconds": round(time.time() - started, 2),
        "seed": seed,
        "steps": steps,
        "guidance": guidance,
        "octreeResolution": octree_resolution,
        "faces": int(mesh.faces.shape[0]),
    }


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, peak_memory=peak_memory,
                      release=release)
