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
    # Remembered so `_ensure_pipeline` can rebuild the SAME pipeline after a
    # render consumes it — see that function's docstring for why one ever
    # needs to.
    _loaded["fetched"] = fetched
    # Hunyuan3D-MLX is Metal-only, like every other Apple-Silicon-gated
    # runner here — there is nothing to detect, but the page still shows it.
    worker_base.set_state(device="mps")


def _ensure_pipeline():
    """The loaded pipeline, rebuilding it first if the LAST render left it
    unusable.

    `ShapePipeline.__call__` (`pipeline_mlx.py`, upstream, unmodified — see
    this file's module docstring for why nothing here may edit it) frees the
    DiT at the end of every call it reaches step 6 of, successful or not:
    `del self.dit; self.dit = None`. That is a one-shot design — upstream's
    own CLI builds one pipeline per invocation and exits — but this worker
    keeps ONE pipeline for the process's whole life (module docstring,
    `_loaded`), so the SECOND `generate()` call in the same worker process,
    after ANY first call that reached the VAE decode (including one this
    file's own empty-surface handling below turns into a clean error),
    crashed with `TypeError: 'NoneType' object is not callable` inside the
    DiT loop — confirmed empirically while reproducing Defect 1 (worker
    log, `TypeError: 'NoneType' object is not callable` at `pipeline_mlx.
    py:171`). Rebuilding from the cached weights directory (`fetched`, no
    network) before every render is the fix that stays inside this fork's
    "packaging only" mandate: it never touches `hy3dshape` itself, only how
    many times this file calls `ShapePipeline.from_pretrained`.
    """
    pipeline = _loaded.get("pipeline")
    if pipeline is not None and getattr(pipeline, "dit", None) is not None:
        return pipeline
    fetched = _loaded.get("fetched")
    if fetched is None:
        raise RuntimeError("no model is loaded")
    ShapePipeline = _shape_pipeline_class()
    pipeline = ShapePipeline.from_pretrained(fetched)
    _loaded["pipeline"] = pipeline
    return pipeline


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


# ------------------------------------------------------------------ preprocessing

#: The torch reference's own border (`preprocessors.py::ImageProcessorV2.
#: __call__`'s default `border_ratio=0.15`, pinned commit) — the object is
#: scaled to fill `1 - _RECENTER_BORDER_RATIO` of the square canvas. The MLX
#: port's `preprocess_image` (`pipeline_mlx.py`) skips this whole step; see
#: `_prepare_cutout`'s docstring for what that costs.
_RECENTER_BORDER_RATIO = 0.15

#: An alpha value at or above this counts as "opaque" for the purpose of
#: deciding whether an image carries a real cutout mask. Not exactly 255:
#: a screenshot or a re-encoded PNG can round a handful of edge pixels to
#: 254/253 without meaning anything by it, and this function's job is to
#: reject "no matte at all", not to be a lossless-encoding detector.
_OPAQUE_ALPHA = 250

#: How many pixels deep the border SAMPLE goes, on all four edges, when an
#: image has no usable alpha and `_border_backdrop_mask` falls back to
#: colour-matting. 3 rather than 1: a single-pixel ring is disproportionately
#: sensitive to one stray anti-aliased or JPEG-ringing pixel deciding
#: "uniform" or "not" on its own.
_BORDER_SAMPLE_WIDTH = 3

#: A pixel counts as SUBJECT (not backdrop) once its Euclidean RGB distance
#: from the sampled border colour reaches this. Picked against a real
#: failing attach (a near-black, `~(1, 3, 2)`, flat-backdrop screenshot):
#: `rgb.max(axis=-1) >= 24` cleanly separated subject from backdrop there.
#: Euclidean distance from the ACTUAL sampled colour, not a fixed "distance
#: from black", is what makes this work the same for a white backdrop as a
#: black one — nothing here assumes which colour the border is.
_BACKDROP_DISTANCE_THRESHOLD = 24.0

#: The border counts as "near-uniform" (a flat backdrop, safe to auto-matte)
#: only if the 95th-percentile pixel in the border sample sits within this
#: distance of the sample's own mean colour. A real photo's border has far
#: more variation than compression noise ever does, and — the case this
#: threshold is equally the guard for — an object that happens to TOUCH the
#: frame edge puts its own pixels into the border sample too, pulling this
#: spread up exactly the same way a busy photo background would. Either way
#: the safe outcome is the same: fail the uniformity test and raise the
#: actionable error below, rather than guess a matte around a subject that
#: is bleeding into the sample it would be matted against.
_BACKDROP_UNIFORMITY_MAX = 20.0


def _border_backdrop_mask(rgb):
    """A subject mask built from the image's OWN border colour, for an
    image with no usable alpha channel — the auto-matte fallback for a flat
    backdrop (D623's revision: reproduced against a real failing attach,
    `/…/gallery/20260817-233317-37e1f7ef8305.png` — mode RGB, no alpha, a
    near-uniform near-black `~(1, 3, 2)` backdrop filling 86.5% of the
    frame). Returns a boolean `(H, W)` mask (`True` = subject) if the
    border reads as a flat backdrop, or `None` if it does not — see
    `_BACKDROP_UNIFORMITY_MAX`'s docstring for what "does not" covers,
    including a subject that touches the frame edge.
    """
    import numpy as np

    height, width, _ = rgb.shape
    depth = max(1, min(_BORDER_SAMPLE_WIDTH, height // 2, width // 2))
    border = np.concatenate([
        rgb[:depth, :, :].reshape(-1, 3),
        rgb[-depth:, :, :].reshape(-1, 3),
        rgb[:, :depth, :].reshape(-1, 3),
        rgb[:, -depth:, :].reshape(-1, 3),
    ], axis=0).astype(np.float64)

    backdrop = border.mean(axis=0)
    spread = np.linalg.norm(border - backdrop, axis=-1)
    if np.percentile(spread, 95) > _BACKDROP_UNIFORMITY_MAX:
        return None

    distance = np.linalg.norm(rgb.astype(np.float64) - backdrop, axis=-1)
    return distance >= _BACKDROP_DISTANCE_THRESHOLD


def _prepare_cutout(image_path, border_ratio=_RECENTER_BORDER_RATIO):
    """Recentre a cutout the way the torch reference's `ImageProcessorV2.
    recenter` does (`hy3dshape/preprocessors.py`, pinned commit): crop to
    the subject's bounding box, scale the object to `1 - border_ratio` of a
    square canvas, centre it, composite on white.

    **Why this exists at all.** The MLX port's own `ShapePipeline.
    preprocess_image` (`pipeline_mlx.py`) does none of this — it only
    composites RGBA-on-white and resizes to the encoder's input size. Fed a
    raw screenshot (subject off-centre, filling a small fraction of the
    frame — the exact shape of what the Playground's file picker hands
    this worker), the DiT's conditioning embedding is different enough from
    what it was trained on that the flow-matching decode never crosses the
    SDF's zero level anywhere in the volume: `_query_sdf_volume`
    (`hy3dshape/models/autoencoders/model_mlx.py`) gets zero near-surface
    points to query and `np.concatenate([])` raises `ValueError: need at
    least one array to concatenate` — reproduced empirically driving the
    live worker (see Defect 1's write-up) with a default-steps,
    default-guidance render of an OFF-CENTRE cutout: recentring is what
    fixed it, not more steps. This function ports the reference's geometry
    in PIL + numpy (this venv's own pyproject.toml declares neither cv2 nor
    torch, which is what `preprocessors.py` itself imports) — a geometry
    port, not a call into the reference file, and zero bytes of `hy3dshape`
    change (D620).

    **The subject mask comes from alpha when there is one, and from the
    image's OWN border colour when there is not (D623, revised).** An
    earlier version of this function required a real alpha channel and
    rejected everything else — but a common real attach (an exported
    Playground screenshot, say) is a plain RGB image on a flat backdrop with
    no alpha at all, and requiring one would reject the user's own working
    input right alongside a genuine mistake. `_border_backdrop_mask` samples
    the image's border and, if it reads as a flat backdrop, builds a mask by
    colour distance from it — the auto-matte path. Only when NEITHER an
    alpha channel NOR a uniform border is present does this function give
    up and raise: at that point there is no matte this function can
    construct without guessing, and a guess would produce a garbage mesh
    silently rather than an honest, actionable failure.

    Returns a square `PIL.Image` in RGB, white-backed, ready for
    `ShapePipeline.__call__`'s own `image=` argument (which accepts a PIL
    Image directly, so nothing downstream needs its own file path).
    """
    from PIL import Image
    import numpy as np

    with Image.open(image_path) as opened:
        im = opened.convert("RGBA")
    arr = np.asarray(im)
    alpha = arr[..., 3]
    rgb = arr[..., :3]

    if alpha.min() < _OPAQUE_ALPHA:
        # Alpha channel present and carrying a real matte — reuse its own
        # values (not a hard threshold of them), so an antialiased cutout
        # edge stays smooth through the recentre below, exactly as the
        # reference's own `recenter` (a straight `image[..., 3]`) does.
        alpha_channel = alpha
    else:
        auto_mask = _border_backdrop_mask(rgb)
        if auto_mask is None:
            raise ValueError(
                "this image has no transparent background AND no flat "
                "backdrop for the model to matte the subject against. "
                "Hunyuan3D-2.1 needs either a CUTOUT (a transparent PNG) "
                "or a picture on a plain, even-coloured background — not a "
                "busy photo. Remove the background first, then try again.")
        # A hard 0/255 matte, not a soft one: colour-distance-from-backdrop
        # carries no antialiasing information the way a real alpha channel
        # does, so there is nothing smoother to preserve here.
        alpha_channel = (auto_mask.astype(np.uint8)) * 255
        # Rebuild `im` against OUR mask rather than the source's own (opaque)
        # alpha band — the crop/paste below reads `im`'s alpha to decide what
        # is subject vs. backdrop, and the source's real alpha (all 255,
        # this branch only runs when it was) would paste the bbox's own
        # backdrop-coloured corners onto the white canvas verbatim instead
        # of matting them away.
        im = Image.fromarray(np.dstack([rgb, alpha_channel]), mode="RGBA")

    mask = alpha_channel > 0
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        raise ValueError(
            "this image is entirely backdrop — there is no subject to "
            "convert. Attach a picture with an opaque subject on a "
            "transparent or plain background.")

    y_min, y_max = int(ys.min()), int(ys.max())
    x_min, x_max = int(xs.min()), int(xs.max())
    h = y_max - y_min
    w = x_max - x_min
    if h == 0 or w == 0:
        raise ValueError(
            "this image's subject is a single row or column of pixels — "
            "there is nothing to convert. Attach a cutout with a real "
            "subject.")

    height, width = mask.shape
    size = max(height, width)
    desired = size * (1 - border_ratio)
    scale = desired / max(h, w)
    h2 = max(1, round(h * scale))
    w2 = max(1, round(w * scale))

    # PIL's crop box is (left, upper, right, lower) — `xs` is the column
    # axis (width), `ys` the row axis (height); `+ 1` because `x_max`/`y_max`
    # are inclusive pixel indices and `crop`'s right/lower edge is not.
    crop = im.crop((x_min, y_min, x_max + 1, y_max + 1))
    # LANCZOS for the downscale — the reference's cv2.INTER_AREA has no
    # exact PIL equivalent; this is a geometry port, not a pixel-identical
    # one (this function's docstring), and LANCZOS is PIL's own recommended
    # filter for shrinking.
    crop = crop.resize((w2, h2), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(crop, ((size - w2) // 2, (size - h2) // 2), crop)

    bg = Image.new("RGB", (size, size), (255, 255, 255))
    bg.paste(canvas, mask=canvas.split()[3])
    return bg


def _decode_level_count(octree_resolution, min_resolution=63):
    """How many hierarchical resolution levels `decode_to_mesh`
    (`hy3dshape/models/autoencoders/model_mlx.py`, pinned commit) will query
    for a given `octree_resolution` — the same arithmetic as that
    function's own resolution pyramid (`while r >= min_resolution:
    resolutions.append(r); r //= 2`), read and copied here rather than
    called into, purely so the decode phase's progress tick has a real
    `total`. If upstream's pyramid shape ever changes this drifts — at
    worst `done` reaches `total` a level early or late, since the tick
    itself only ever fires as many times as `_query_sdf_volume` is
    actually called (see `_query_wrapper` in `generate`)."""
    r = octree_resolution
    resolutions = []
    if r < min_resolution:
        resolutions.append(r)
    while r >= min_resolution:
        resolutions.append(r)
        r = r // 2
    return len(resolutions)


def _is_empty_surface_error(exc):
    """True iff `exc` is the exact `ValueError` `_query_sdf_volume`
    (`hy3dshape/models/autoencoders/model_mlx.py`, pinned commit) raises
    when the hierarchical decoder finds ZERO near-surface voxels at some
    resolution level — `np.concatenate(all_logits, axis=0)` on an empty
    list — i.e. the model produced no shape for this conditioning image at
    all. Detected by walking the traceback for THAT function's own frame
    and confirming its OWN local state (`all_logits == []`), not by
    matching numpy's exception string alone: an identical string from an
    unrelated empty-array bug anywhere else must not be swallowed and
    turned into this function's actionable message."""
    if not isinstance(exc, ValueError):
        return False
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        code = frame.f_code
        if code.co_name == "_query_sdf_volume" and code.co_filename.endswith("model_mlx.py"):
            all_logits = frame.f_locals.get("all_logits")
            if isinstance(all_logits, list) and len(all_logits) == 0:
                return True
        tb = tb.tb_next
    return False


#: `generate`'s own message when `_is_empty_surface_error` fires — named so
#: the test asserting it stays in sync with what a caller actually reads.
_EMPTY_SURFACE_MESSAGE = (
    "the model found no visible surface in this render — it produced an "
    "empty shape. This usually means the conditioning image is too far "
    "from what the model was trained on. Try a background-removed cutout "
    "with a larger, more distinct subject, or more denoising steps "
    "(this render used %d).")


# ------------------------------------------------------------------ generation

#: Upstream's own defaults (`ShapePipeline.__call__`'s signature, `pipeline_
#: mlx.py` at the pinned commit) — named explicitly here, not left to the
#: pipeline's own defaults, so the numbers this worker reports back match
#: what the registry traits table (Task 2) promises a caller who sends none
#: of these fields.
_DEFAULT_STEPS = 50
_DEFAULT_GUIDANCE = 5.0
_DEFAULT_OCTREE_RESOLUTION = 256

#: `ShapePipeline.__call__`'s own `num_chunks` default (`pipeline_mlx.py`)
#: is 10000 — kept here, deliberately UNCHANGED from that default. Defect
#: 2's own hypothesis was that this default forces "hundreds of full GPU
#: syncs" at the final octree-256 decode level and that raising it would be
#: the fix for a sluggish render. Measured instead (this fork's perf
#: write-up has the full table): at octree 128, `_query_sdf_volume`'s
#: TOTAL time across both hierarchical levels held at 2.3-3.3 seconds
#: across every step count tried (4 through 50), while the WHOLE render
#: scaled from 45 to 679 seconds — decode is 0.5-5% of wall time, the DiT
#: denoising loop is the rest, at roughly 13.5 seconds per step regardless
#: of `num_chunks`. Raising this constant is not the fix for the
#: sluggishness this defect set out to explain; it is left at the fork's
#: own default rather than changed on an unmeasured guess about the
#: higher-resolution final level (an octree-256 confirmation run was
#: started but did not finish inside this session's time budget — see the
#: write-up for what is and is not confirmed at that resolution). Not
#: exposed as a request field either way — nothing about it is a fact a
#: caller should have an opinion on, unlike `steps`/`guidance`/
#: `octreeResolution`.
_NUM_CHUNKS = 10000


def generate(body):
    """Render one mesh. Returns `{path, seconds, seed, steps, guidance,
    octreeResolution, faces}`.

    **Progress is real, not interpolated — two phases, each with its own
    `total`.** `ShapePipeline.__call__` itself takes no progress callback of
    any kind — unlike `ltx_video`'s patchable `samplers.tqdm` or `mflux_
    image`'s `mflux.callbacks` registry, there is no name in the pipeline a
    worker can stand in front of without editing the upstream loop itself,
    which this fork's packaging-only mandate rules out (D620, this file's
    module docstring). But `pipeline.scheduler.step` runs exactly once per
    denoising iteration and `pipeline.vae._query_sdf_volume` exactly once
    per hierarchical decode level (`pipeline_mlx.py` / `model_mlx.py` at
    the pinned commit) — both are bound methods reached through the
    pipeline object THIS FILE already holds, so wrapping them here needs no
    edit to `hy3dshape` at all. Wrapped for the duration of one call and
    restored in a `finally`, so wrapping never leaks across renders on the
    same long-lived pipeline (see `_ensure_pipeline`) or stacks a second
    layer on top of itself.

    **A ✕ is checked at every tick, not only at the two ends of the call —
    and this function checks BOTH cancellation channels explicitly (code
    review, 2026-08-28, finding 2: neither channel was checked here at all
    before this).** `worker_base.CANCEL` is set directly by a `/cancel` POST
    to this worker (`supervisor.cancel_generation`); `report_or_cancel`'s
    `cancel_requested` is a SEPARATE signal, carried on THIS render's own
    job row (the download manager's ✕) and readable only by posting a tick
    to it. Checking before `pipeline(...)` catches a ✕ pressed before the
    render actually started (the model may still have been loading);
    checking on every step/level tick catches one pressed WHILE the render
    ran and actually stops the loop this time, rather than only stopping
    the artefact from landing on disk once compute already in flight
    finished anyway.
    """
    _pin_stream()

    pipeline = _ensure_pipeline()

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
                                 done=0, total=steps, detail="Preparing the image")
    # `CANCEL` is the OTHER channel (see this function's own docstring) —
    # checked here too, for the identical pre-start reason.
    if worker_base.CANCEL.is_set():
        raise worker_base.Cancelled()

    # Recentred exactly like the torch reference's own preprocessor — see
    # `_prepare_cutout`'s docstring for why the MLX pipeline needs this done
    # for it, and Defect 1's write-up for the empirical evidence.
    conditioning_image = _prepare_cutout(image)

    total_levels = _decode_level_count(octree_resolution)
    step_count = 0
    level_count = 0
    original_step = pipeline.scheduler.step
    original_query = pipeline.vae._query_sdf_volume

    def _step_wrapper(*args, **kwargs):
        nonlocal step_count
        result = original_step(*args, **kwargs)
        step_count += 1
        worker_base.report_or_cancel(
            job=job, state="running", kind="task", unit="",
            done=step_count, total=steps,
            detail="Denoising step %d/%d" % (step_count, steps))
        if worker_base.CANCEL.is_set():
            raise worker_base.Cancelled()
        return result

    def _query_wrapper(*args, **kwargs):
        nonlocal level_count
        result = original_query(*args, **kwargs)
        level_count += 1
        # Clamped: `_decode_level_count` is arithmetic copied from upstream
        # (see its docstring), not a value read FROM upstream, so a future
        # pyramid change could make the real call count differ from the
        # prediction — `done` must never read past `total` on the row.
        done = min(level_count, total_levels)
        worker_base.report_or_cancel(
            job=job, state="running", kind="task", unit="",
            done=done, total=total_levels,
            detail="Decoding the mesh — level %d/%d" % (done, total_levels))
        if worker_base.CANCEL.is_set():
            raise worker_base.Cancelled()
        return result

    pipeline.scheduler.step = _step_wrapper
    pipeline.vae._query_sdf_volume = _query_wrapper
    try:
        mesh = pipeline(
            image=conditioning_image,
            num_inference_steps=steps,
            guidance_scale=guidance,
            octree_resolution=octree_resolution,
            num_chunks=_NUM_CHUNKS,
            seed=seed,
        )
    except ValueError as e:
        if _is_empty_surface_error(e):
            # `from None`, deliberately — `describe_failure` (worker_base.py)
            # walks a `raise ... from e` chain and appends the ROOT cause's
            # own message, which would put numpy's raw
            # `ValueError: need at least one array to concatenate` right back
            # in front of the user this branch exists to protect them from.
            raise ValueError(_EMPTY_SURFACE_MESSAGE % steps) from None
        raise
    finally:
        # ALWAYS restored — success, cancel, or any other exception — so a
        # second render on this same long-lived pipeline (`_ensure_pipeline`)
        # never finds a wrapper still installed, closed over THIS render's
        # `job`/`steps`, and two renders in a row never stack a second
        # wrapper on top of the first.
        pipeline.scheduler.step = original_step
        pipeline.vae._query_sdf_volume = original_query

    # One more check after the call returns: the ✕ ticks above stop the
    # LOOP, but a ✕ pressed after the last step/level tick and before
    # `pipeline(...)` actually returns (marching cubes, mesh assembly —
    # neither has a hook of its own) must still stop the mesh from being
    # written and the render from being reported done.
    if worker_base.CANCEL.is_set():
        raise worker_base.Cancelled()
    worker_base.report_or_cancel(job=job, state="running", kind="task", unit="",
                                 done=total_levels, total=total_levels, detail="Generating shape")

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
