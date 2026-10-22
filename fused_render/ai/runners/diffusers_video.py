"""The shared Wan video runner body, over which `diffusers_video_cuda/` is a shell (D1424).

Text-to-video (and, on Wan 2.2 TI2V-5B, image-to-video) through diffusers'
`WanPipeline`. The second `VIDEO_GENERATION` engine, and the first that is not
Apple-only: `ltx_video/worker.py` is the MLX one, and this file answers the
same request and reply shapes so a page cannot tell which engine rendered.
Served by `worker_base`.

**Shipped UNMEASURED**, exactly as `torch_vision.py`'s CUDA shell was (D1414):
nobody has run this on NVIDIA hardware, so every number below that is not from
a model card (the 40 GiB placement line, the progress cadence, the memory
envelope in the registry note) is a plan rather than a measurement, and the
tests drive fakes, never a GPU.

**Only what the engine opens is fetched** (D1308). `download_patterns` names
`model_index.json` and the five component folders every Wan repo has, plus the
three optional ones (`transformer_2`, `image_encoder`, `image_processor`) when
the index names them non-null. Never the README, the assets, the example media.
One repo, one `download_snapshot`, one bar.

**Three facts are read off the repo's own `model_index.json`**, never off the
repo id: the pipeline class (a non-Wan repo is refused with a sentence), the
`expand_timesteps` flag (Wan 2.2 TI2V-5B's: it is the one fact that says the
model takes a reference image, and it renders at 24 fps where Wan 2.1 renders at
16), and — from the loaded pipeline — the spatial grid the canvas must sit on.
"""

import json
import os
import sys
import time

# This module IS in `runners/`, beside `worker_base`/`formats`
# (the folder shell inserts the same directory; see torch_image.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import worker_base  # noqa: E402 - the path insert above is what makes it importable

_loaded = {}

#: The device this runner's FOLDER promises. `diffusers_video_cuda/worker.py`
#: sets it to "cuda" before `main()`. Without it a torch that cannot see the
#: GPU would try to render a 5B video on CPU, which contradicts `registry._cuda`
#: (a bad driver fails loudly, before a byte is fetched).
EXPECTED_DEVICE = None

#: The pipeline classes `model_index.json` may name. `WanPipeline` is text-to-
#: video; `WanImageToVideoPipeline` is the image-conditioned variant (Wan 2.1
#: I2V repos). Mirrors `formats.WAN_PIPELINE_CLASSES` — this module runs in its
#: own venv and does not import that file's server-side half.
WAN_CLASSES = ("WanPipeline", "WanImageToVideoPipeline")

#: Folders every Wan repo carries, and the ones that exist only when the index
#: names them (`transformer_2` is Wan 2.2 A14B's second expert; the image
#: components are the I2V repos').
REQUIRED_FOLDERS = ("scheduler", "text_encoder", "tokenizer", "transformer", "vae")
OPTIONAL_FOLDERS = ("transformer_2", "image_encoder", "image_processor")

#: The GPU memory at or above which the whole pipeline lives on the card; below
#: it the pipeline is offloaded component by component. 40 GiB because the
#: smallest curated row's fp32 text encoder alone is 22.7 GB and the 5B
#: transformer another ~10 GB in bf16 — a plan, not a measurement.
RESIDENT_VRAM_BYTES = 40 * 2**30

#: Wan 2.1's frame grid is `4n + 1` (the VAE's temporal compression is 4).
FRAMES_STEP = 4

#: The Wan 2.2 model card's default negative prompt, verbatim (the README's
#: `negative_prompt` for T2V/TI2V). Wan was trained with it; an empty one
#: renders visibly worse, which is why the request has no field for it.
DEFAULT_NEGATIVE_PROMPT = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，"
    "整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，"
    "画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，"
    "静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走")

GUIDANCE_SCALE = 5.0
#: Wan 2.2 renders at 24 fps, Wan 2.1 at 16 — by the index's `expand_timesteps`.
FPS_EXPAND_TIMESTEPS = 24
FPS_DEFAULT = 16
#: Wan's own guidance on the scheduler's flow shift: 5.0 for 720p-class canvases,
#: 3.0 for 480p. "Class" is the longer side, so a portrait 720p reads the same.
FLOW_SHIFT_LARGE = 5.0
FLOW_SHIFT_SMALL = 3.0
LARGE_CANVAS_SIDE = 1024

DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FRAMES, DEFAULT_STEPS = 1280, 704, 121, 50


def _check_expected_device():
    """Refuse before any bytes are fetched when the folder promised CUDA and torch has none."""
    import torch

    if EXPECTED_DEVICE == "cuda" and not torch.cuda.is_available():
        raise RuntimeError(
            "diffusers-video-cuda loaded torch without CUDA; check the NVIDIA "
            "driver supports this torch build")


def _named(entry):
    """Whether a `model_index.json` component entry names a class (`[null, null]` is a stub)."""
    return isinstance(entry, (list, tuple)) and bool(entry) and entry[0] is not None


def download_patterns(index):
    """The `allow_patterns` for a repo whose `model_index.json` is `index`."""
    folders = list(REQUIRED_FOLDERS)
    folders += [name for name in OPTIONAL_FOLDERS if _named(index.get(name))]
    return ["model_index.json"] + [f"{folder}/*" for folder in folders]


def _check_wan(model_id, index):
    """The sentence a repo that is not a Wan diffusers pipeline is refused with."""
    class_name = index.get("_class_name")
    if class_name not in WAN_CLASSES:
        raise RuntimeError(
            "diffusers-video-cuda serves Wan 2.1 / 2.2 diffusers repos "
            f"(WanPipeline); this repo is {class_name or 'not a diffusers pipeline'}")
    return class_name


def _read_index(path):
    with open(path, encoding="utf-8") as handle:
        index = json.load(handle)
    if not isinstance(index, dict):
        raise RuntimeError("model_index.json is not a JSON object")
    return index


def _fetched_index(model_id):
    """The repo's `model_index.json`, from the cache when it is there already.

    A few hundred bytes, fetched ahead of the snapshot because the SCOPE of the
    snapshot depends on what it says. Local first, so a model that is already on
    disk is priced and verified with no network at all.
    """
    import huggingface_hub

    try:
        try:
            path = huggingface_hub.hf_hub_download(
                model_id, "model_index.json", local_files_only=True)
        except Exception:  # noqa: BLE001 - not cached yet is the ordinary case
            path = huggingface_hub.hf_hub_download(model_id, "model_index.json")
        return _read_index(path)
    except Exception as error:  # noqa: BLE001 - a Hub failure is a fact about the id/network
        raise RuntimeError(
            f"could not read {model_id}'s model_index.json: {error}") from error


def download(model_id):
    _check_expected_device()
    index = _fetched_index(model_id)
    # Refused before the multi-gigabyte pull, not after it at load.
    _check_wan(model_id, index)
    return worker_base.download_snapshot(
        model_id, allow_patterns=download_patterns(index))


def load(model_id, fetched):
    # Cleared first: a failed load must not leave the previous model serving.
    _loaded.clear()
    _check_expected_device()
    import torch
    import diffusers

    index = _read_index(os.path.join(fetched, "model_index.json"))
    class_name = _check_wan(model_id, index)

    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    # The VAE stays fp32 — Wan's own model cards load it that way, and a bf16
    # decode is where the banding in an otherwise good render comes from.
    vae = diffusers.AutoencoderKLWan.from_pretrained(
        fetched, subfolder="vae", torch_dtype=torch.float32)
    pipeline_class = getattr(diffusers, class_name)
    pipe = pipeline_class.from_pretrained(fetched, vae=vae, torch_dtype=dtype)

    total = torch.cuda.get_device_properties(0).total_memory
    if total >= RESIDENT_VRAM_BYTES:
        pipe.to("cuda")
        placement = "resident"
    else:
        pipe.enable_model_cpu_offload()
        placement = "offload"
    # The 5B VAE decodes a 720p clip in one go otherwise, which is the largest
    # single allocation of a render.
    pipe.vae.enable_tiling()

    expand_timesteps = bool(index.get("expand_timesteps"))
    _loaded.update(
        pipe=pipe, model_id=model_id, placement=placement,
        expand_timesteps=expand_timesteps,
        # A repo of the image-conditioned class cannot render without one.
        image_required=class_name == "WanImageToVideoPipeline",
        fps=FPS_EXPAND_TIMESTEPS if expand_timesteps else FPS_DEFAULT,
        # What width/height must be a multiple of: the VAE's spatial stride
        # times the transformer's patch (16 on Wan 2.1, 32 on Wan 2.2).
        mod=pipe.vae_scale_factor_spatial * pipe.transformer.config.patch_size[1],
        i2v=None)
    worker_base.set_state(device="cuda", placement=placement)


def memory():
    import torch

    if not torch.cuda.is_available():
        return None
    try:
        return int(torch.cuda.memory_allocated()) or None
    except (RuntimeError, OSError):
        return None


def peak_memory():
    """The high-water mark of what this process has allocated on the card —
    SPEC AI-8c. Where `memory()` answers "right now", this answers "at its
    worst", which is what `fit` needs: a render's peak is its denoising step
    plus the VAE decode, and neither is what is resident when `/health` is read."""
    import torch

    if not torch.cuda.is_available():
        return None
    try:
        return int(torch.cuda.max_memory_allocated()) or None
    except (RuntimeError, OSError):
        return None


def release():
    import torch

    if torch.cuda.is_available():
        try:
            torch.cuda.empty_cache()
        except (RuntimeError, OSError):
            pass


# ------------------------------------------------------------------ generation


def _snap_side(value, mod):
    """Down to the model's grid, never below one cell."""
    return max(mod, value - value % mod)


def _snap_frames(value):
    """Up to the next `4n + 1`, mirroring the route's own rule defensively."""
    frames = max(1, value)
    remainder = (frames - 1) % FRAMES_STEP
    if remainder:
        frames += FRAMES_STEP - remainder
    return frames


def flow_shift_for(width, height):
    return FLOW_SHIFT_LARGE if max(width, height) >= LARGE_CANVAS_SIDE else FLOW_SHIFT_SMALL


def _i2v_pipe(pipe):
    """The image-conditioned view of the loaded pipeline, built once.

    `from_pipe` shares the components, so this costs no second copy of the
    weights."""
    if _loaded.get("i2v") is None:
        import diffusers

        _loaded["i2v"] = diffusers.WanImageToVideoPipeline.from_pipe(pipe)
    return _loaded["i2v"]


def _open_image(path, size):
    from PIL import Image, ImageOps

    with Image.open(path) as opened:
        return ImageOps.exif_transpose(opened).convert("RGB").resize(size)


def generate(body):
    """Render one video. Returns `{path, seconds, seed, width, height, frames,
    steps[, image]}` — the shape `ltx_video/worker.py` replies with, so a page
    cannot tell which engine rendered for it."""
    pipe = _loaded.get("pipe")
    if pipe is None:
        raise RuntimeError("no model is loaded")

    prompt = str(body.get("prompt") or "")
    out = str(body.get("out") or "")
    job = body.get("job") or None
    image = body.get("image") or None
    if not out:
        raise ValueError("'out' must be the path to write the video to")
    if image and not (_loaded["expand_timesteps"] or _loaded["image_required"]):
        raise ValueError(
            "this model is text-to-video only; Wan 2.2 TI2V-5B accepts a reference image")
    if _loaded["image_required"] and not image:
        raise ValueError("this model is image-to-video only; give it a reference image")

    mod = _loaded["mod"]
    width = _snap_side(int(body.get("width") or DEFAULT_WIDTH), mod)
    height = _snap_side(int(body.get("height") or DEFAULT_HEIGHT), mod)
    frames = _snap_frames(int(body.get("frames") or DEFAULT_FRAMES))
    steps = int(body.get("steps") or DEFAULT_STEPS)
    seed = body.get("seed")
    if seed is None:
        import secrets

        seed = secrets.randbelow(2**31)
    seed = int(seed)

    import torch
    from diffusers import UniPCMultistepScheduler
    from diffusers.utils import export_to_video

    run = _i2v_pipe(pipe) if image and not _loaded["image_required"] else pipe
    run.scheduler = UniPCMultistepScheduler.from_config(
        run.scheduler.config, flow_shift=flow_shift_for(width, height))
    extra = {"image": _open_image(image, (width, height))} if image else {}
    generator = torch.Generator("cuda").manual_seed(seed)

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    started = time.time()
    worker_base.report(job=job, state="running", kind="task", unit="",
                       done=0, total=steps, detail="Denoising")

    def on_step_end(_pipe, step, _timestep, callback_kwargs):
        # `report_or_cancel`, not `report`: this callback is the ONLY point in a
        # minutes-long `pipe()` call where a stop can be honoured, and the reply
        # to this tick is how the ✕ gets here. The row renders done/total itself,
        # so the detail carries no step count.
        worker_base.report_or_cancel(
            job=job, kind="task", unit="", done=step + 1, total=steps,
            detail="Denoising")
        if worker_base.CANCEL.is_set():
            raise worker_base.Cancelled()
        return callback_kwargs

    result = run(
        prompt=prompt, negative_prompt=DEFAULT_NEGATIVE_PROMPT,
        height=height, width=width, num_frames=frames,
        guidance_scale=GUIDANCE_SCALE, num_inference_steps=steps,
        generator=generator, callback_on_step_end=on_step_end, **extra)

    worker_base.report_or_cancel(
        job=job, kind="task", unit="", done=steps, total=steps,
        detail="Encoding video")
    export_to_video(result.frames[0], out, fps=_loaded["fps"])

    reply = {
        "path": out,
        "seconds": round(time.time() - started, 2),
        "seed": seed,
        "width": width,
        "height": height,
        "frames": frames,
        "steps": steps,
    }
    if image:
        # Echoed like `/api/ai/image`'s reply echoes its own `image`.
        reply["image"] = image
    return reply


def main():
    """Serve, forever. The entry point `diffusers_video_cuda/worker.py` calls."""
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, peak_memory=peak_memory,
                      release=release)


if __name__ == "__main__":
    main()
