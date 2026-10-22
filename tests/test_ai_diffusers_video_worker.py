"""The Wan video runner (`runners/diffusers_video.py`, D1424), with no GPU.

Nobody has run this runner on NVIDIA hardware — it ships UNMEASURED, the way
`vision-torch-cuda` did (D1414) — so every test here drives fakes of torch,
diffusers and `worker_base`, in the style of `test_ai_diffusers_worker.py`. What
they pin is the runner's own decisions: what it refuses and when, what it
fetches, what it derives from `model_index.json`, and the shape of its reply.
Nothing here says a render works; only that the runner asks the engine for the
right one.
"""
import importlib.util
import json
import os
import sys
import types

import pytest

RUNNERS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners",
)
WORKER_PATH = os.path.join(RUNNERS, "diffusers_video.py")
SHELL_PATH = os.path.join(RUNNERS, "diffusers_video_cuda", "worker.py")
BASE_PATH = os.path.join(RUNNERS, "worker_base.py")

WAN21 = "Wan-AI/Wan2.1-T2V-1.3B-Diffusers"
WAN22 = "Wan-AI/Wan2.2-TI2V-5B-Diffusers"

_GIB = 2**30

#: `Wan-AI/Wan2.2-TI2V-5B-Diffusers` as the Hub lists it, names only — the
#: assertions read which of these the runner's patterns select.
REPO_FILES = [
    ".gitattributes", "README.md", "LICENSE.txt",
    "assets/comp_effic.png", "examples/i2v_input.JPG",
    "model_index.json",
    "scheduler/scheduler_config.json",
    "text_encoder/config.json",
    "text_encoder/model.safetensors.index.json",
    "text_encoder/model-00001-of-00005.safetensors",
    "tokenizer/tokenizer.json", "tokenizer/spiece.model",
    "transformer/config.json",
    "transformer/diffusion_pytorch_model.safetensors.index.json",
    "transformer/diffusion_pytorch_model-00001-of-00003.safetensors",
    "vae/config.json", "vae/diffusion_pytorch_model.safetensors",
    # Present on an A14B-shaped repo, and on I2V repos.
    "transformer_2/config.json",
    "transformer_2/diffusion_pytorch_model.safetensors",
    "image_encoder/model.safetensors",
    "image_processor/preprocessor_config.json",
]


class _Flag:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


class FakeBase:
    """`worker_base`, recording what the runner asked of it."""

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.snapshot_calls = []
        self.state_calls = []
        self.serve_kwargs = None
        self.reports = []
        self.ticks = []
        self.CANCEL = _Flag()
        #: Set by a test to have the Nth `report_or_cancel` answer "the ✕ was pressed".
        self.cancel_on_tick = None

    def report(self, job=None, **fields):
        self.reports.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_on_tick is not None and len(self.ticks) >= self.cancel_on_tick:
            raise self.Cancelled()

    def download_snapshot(self, model_id, allow_patterns=None,
                          ignore_patterns=None, **kwargs):
        self.snapshot_calls.append({"model": model_id, "allow": allow_patterns,
                                    "ignore": ignore_patterns, **kwargs})
        return f"/snapshots/{model_id}"

    def set_state(self, **fields):
        self.state_calls.append(fields)

    def serve(self, **kwargs):
        self.serve_kwargs = kwargs


class FakePipe:
    """Enough of a Wan pipeline to be placed, ticked and asked for frames."""

    def __init__(self, spatial=8, patch=(1, 2, 2)):
        self.vae_scale_factor_spatial = spatial
        self.transformer = types.SimpleNamespace(
            config=types.SimpleNamespace(patch_size=patch))
        self.tiling = 0
        self.vae = types.SimpleNamespace(
            enable_tiling=lambda: setattr(self, "tiling", self.tiling + 1))
        self.scheduler = types.SimpleNamespace(config={"flow_shift": 1.0})
        self.placed = []
        self.calls = []

    def to(self, device):
        self.placed.append(("to", device))
        return self

    def enable_model_cpu_offload(self):
        self.placed.append(("offload", None))

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        for step in range(kwargs["num_inference_steps"]):
            kwargs["callback_on_step_end"](self, step, 0, {"latents": object()})
        return types.SimpleNamespace(frames=[["frame-0", "frame-1"]])


def _install(monkeypatch, *, cuda=True, total=48 * _GIB, bf16=True, pipe=None,
             index=None, tmp_path=None, i2v_class=False):
    """Fake `torch`, `diffusers` and `huggingface_hub`. Returns a record of what they saw."""
    record = types.SimpleNamespace(
        pipe=pipe or FakePipe(), vae_calls=[], pipe_calls=[], schedulers=[],
        exports=[], from_pipe=[], hub_calls=[], i2v=None)

    torch = types.ModuleType("torch")
    torch.float32, torch.bfloat16, torch.float16 = "float32", "bfloat16", "float16"

    class Generator:
        def __init__(self, device=None):
            self.device = device

        def manual_seed(self, seed):
            self.seed = seed
            return self

    torch.Generator = Generator
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: cuda,
        is_bf16_supported=lambda: bf16,
        get_device_properties=lambda i: types.SimpleNamespace(total_memory=total),
        memory_allocated=lambda: 7,
        max_memory_allocated=lambda: 11,
        empty_cache=lambda: None)
    monkeypatch.setitem(sys.modules, "torch", torch)

    diffusers = types.ModuleType("diffusers")

    class AutoencoderKLWan:
        @staticmethod
        def from_pretrained(path, **kwargs):
            record.vae_calls.append((path, kwargs))
            return "the-vae"

    class WanPipeline:
        @staticmethod
        def from_pretrained(path, **kwargs):
            record.pipe_calls.append(("WanPipeline", path, kwargs))
            return record.pipe

    class WanImageToVideoPipeline:
        @staticmethod
        def from_pretrained(path, **kwargs):
            record.pipe_calls.append(("WanImageToVideoPipeline", path, kwargs))
            return record.pipe

        @staticmethod
        def from_pipe(pipe):
            record.from_pipe.append(pipe)
            record.i2v = FakePipe()
            record.i2v.scheduler = pipe.scheduler
            return record.i2v

    class UniPCMultistepScheduler:
        @staticmethod
        def from_config(config, **kwargs):
            record.schedulers.append(kwargs)
            return types.SimpleNamespace(config=config, made_with=kwargs)

    diffusers.AutoencoderKLWan = AutoencoderKLWan
    diffusers.WanPipeline = WanPipeline
    diffusers.WanImageToVideoPipeline = WanImageToVideoPipeline
    diffusers.UniPCMultistepScheduler = UniPCMultistepScheduler
    utils = types.ModuleType("diffusers.utils")
    utils.export_to_video = lambda frames, out, fps=None: (
        record.exports.append((list(frames), out, fps)),
        open(out, "wb").write(b"mp4"))
    diffusers.utils = utils
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setitem(sys.modules, "diffusers.utils", utils)

    hub = types.ModuleType("huggingface_hub")

    def hf_hub_download(repo, filename, local_files_only=False, **kwargs):
        record.hub_calls.append((repo, filename, local_files_only))
        path = tmp_path / "hub-model_index.json"
        path.write_text(json.dumps(index if index is not None else _index()))
        return str(path)

    hub.hf_hub_download = hf_hub_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    return record


def _index(**over):
    base = {"_class_name": "WanPipeline", "_diffusers_version": "0.35.0",
            "transformer": ["diffusers", "WanTransformer3DModel"],
            "transformer_2": [None, None],
            "vae": ["diffusers", "AutoencoderKLWan"]}
    base.update(over)
    return base


def _snapshot(tmp_path, **over):
    """A fetched snapshot directory holding only its `model_index.json`."""
    folder = tmp_path / "snapshot"
    folder.mkdir(exist_ok=True)
    (folder / "model_index.json").write_text(json.dumps(_index(**over)))
    return str(folder)


def load_worker(monkeypatch, base, expected="cuda"):
    monkeypatch.setitem(sys.modules, "worker_base", base)
    monkeypatch.syspath_prepend(RUNNERS)
    spec = importlib.util.spec_from_file_location(
        "diffusers_video_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.EXPECTED_DEVICE = expected
    return module


@pytest.fixture()
def base():
    return FakeBase()


@pytest.fixture(scope="module")
def selects():
    spec = importlib.util.spec_from_file_location("worker_base_for_patterns", BASE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.selects


def _loaded(monkeypatch, base, tmp_path, *, index_over=None, pipe=None, **install):
    """A worker with a model loaded through the real `load()`."""
    index_over = index_over or {}
    record = _install(monkeypatch, pipe=pipe, tmp_path=tmp_path, **install)
    worker = load_worker(monkeypatch, base)
    worker.load(WAN22, _snapshot(tmp_path, **index_over))
    return worker, record


def _request(tmp_path, **over):
    return {"prompt": "a red fox", "out": str(tmp_path / "out" / "fox.mp4"),
            "job": "sys:ai-video:abc", "width": 832, "height": 480,
            "frames": 81, "steps": 4, "seed": 7, **over}


# -- the folder promised CUDA ---------------------------------------------------


def test_a_torch_without_cuda_refuses_before_any_byte_is_fetched(monkeypatch, base, tmp_path):
    record = _install(monkeypatch, cuda=False, tmp_path=tmp_path)
    worker = load_worker(monkeypatch, base)

    with pytest.raises(RuntimeError) as raised:
        worker.download(WAN22)

    assert str(raised.value) == (
        "diffusers-video-cuda loaded torch without CUDA; check the NVIDIA "
        "driver supports this torch build")
    assert base.snapshot_calls == []
    assert record.hub_calls == []


def test_load_refuses_the_same_way_and_leaves_nothing_serving(monkeypatch, base, tmp_path):
    record = _install(monkeypatch, tmp_path=tmp_path)
    worker = load_worker(monkeypatch, base)
    worker.load(WAN22, _snapshot(tmp_path))
    assert "pipe" in worker._loaded

    sys.modules["torch"].cuda.is_available = lambda: False
    with pytest.raises(RuntimeError, match="without CUDA"):
        worker.load(WAN22, _snapshot(tmp_path))

    assert worker._loaded == {}
    assert record.pipe_calls and len(record.pipe_calls) == 1


def test_the_cpu_folder_promise_is_not_checked_when_none_was_made(monkeypatch, base, tmp_path):
    _install(monkeypatch, cuda=False, tmp_path=tmp_path)
    worker = load_worker(monkeypatch, base, expected=None)

    worker.download(WAN22)  # no refusal: this folder promised nothing

    assert len(base.snapshot_calls) == 1


# -- what is fetched ------------------------------------------------------------


def _landed(selects, allow):
    return {name for name in REPO_FILES if selects(name, allow=allow)}


def test_the_download_is_only_what_the_pipeline_reads(monkeypatch, base, tmp_path, selects):
    """The README, the sample media and the unnamed optional components are
    bytes the load never touches."""
    _install(monkeypatch, tmp_path=tmp_path, index=_index())
    worker = load_worker(monkeypatch, base)

    worker.download(WAN22)

    landed = _landed(selects, base.snapshot_calls[-1]["allow"])
    assert landed == {
        "model_index.json", "scheduler/scheduler_config.json",
        "text_encoder/config.json", "text_encoder/model.safetensors.index.json",
        "text_encoder/model-00001-of-00005.safetensors",
        "tokenizer/tokenizer.json", "tokenizer/spiece.model",
        "transformer/config.json",
        "transformer/diffusion_pytorch_model.safetensors.index.json",
        "transformer/diffusion_pytorch_model-00001-of-00003.safetensors",
        "vae/config.json", "vae/diffusion_pytorch_model.safetensors",
    }
    assert base.snapshot_calls[-1]["model"] == WAN22


def test_optional_components_are_fetched_when_the_index_names_them(monkeypatch, base, tmp_path, selects):
    index = _index(transformer_2=["diffusers", "WanTransformer3DModel"],
                   image_encoder=["transformers", "CLIPVisionModel"],
                   image_processor=["transformers", "CLIPImageProcessor"])
    _install(monkeypatch, tmp_path=tmp_path, index=index)
    worker = load_worker(monkeypatch, base)

    worker.download(WAN22)

    landed = _landed(selects, base.snapshot_calls[-1]["allow"])
    assert {"transformer_2/config.json", "image_encoder/model.safetensors",
            "image_processor/preprocessor_config.json"} <= landed
    assert "README.md" not in landed and "assets/comp_effic.png" not in landed


def test_the_index_is_read_from_the_cache_first(monkeypatch, base, tmp_path):
    record = _install(monkeypatch, tmp_path=tmp_path)
    worker = load_worker(monkeypatch, base)

    worker.download(WAN22)

    assert record.hub_calls == [(WAN22, "model_index.json", True)]


# -- what is refused ------------------------------------------------------------


def test_a_repo_that_is_not_a_wan_pipeline_is_refused_with_a_sentence(monkeypatch, base, tmp_path):
    record = _install(monkeypatch, tmp_path=tmp_path)
    worker = load_worker(monkeypatch, base)

    with pytest.raises(RuntimeError) as raised:
        worker.load("black-forest-labs/FLUX.2-klein-4B",
                    _snapshot(tmp_path, _class_name="Flux2KleinPipeline"))

    assert str(raised.value) == (
        "diffusers-video-cuda serves Wan 2.1 / 2.2 diffusers repos "
        "(WanPipeline); this repo is Flux2KleinPipeline")
    assert record.pipe_calls == []


def test_a_non_wan_repo_is_refused_before_it_is_downloaded(monkeypatch, base, tmp_path):
    _install(monkeypatch, tmp_path=tmp_path, index=_index(_class_name="FluxPipeline"))
    worker = load_worker(monkeypatch, base)

    with pytest.raises(RuntimeError, match="this repo is FluxPipeline"):
        worker.download("some/flux")

    assert base.snapshot_calls == []


def test_an_image_is_refused_on_a_model_that_is_text_to_video_only(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)  # no expand_timesteps
    request = _request(tmp_path, image=str(tmp_path / "ref.png"))

    with pytest.raises(ValueError) as raised:
        worker.generate(request)

    assert str(raised.value) == (
        "this model is text-to-video only; Wan 2.2 TI2V-5B accepts a reference image")
    assert record.pipe.calls == []


def test_generate_without_a_loaded_model_says_so(monkeypatch, base, tmp_path):
    _install(monkeypatch, tmp_path=tmp_path)
    worker = load_worker(monkeypatch, base)

    with pytest.raises(RuntimeError, match="no model is loaded"):
        worker.generate(_request(tmp_path))


# -- load: dtype, placement, facts read off the index ---------------------------


def test_load_builds_an_fp32_vae_then_a_bf16_pipeline_on_it(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)

    snapshot = _snapshot(tmp_path)
    assert record.vae_calls == [(snapshot, {"subfolder": "vae", "torch_dtype": "float32"})]
    assert record.pipe_calls == [
        ("WanPipeline", snapshot, {"vae": "the-vae", "torch_dtype": "bfloat16"})]
    assert record.pipe.tiling == 1


def test_a_card_without_bf16_gets_float16(monkeypatch, base, tmp_path):
    _, record = _loaded(monkeypatch, base, tmp_path, bf16=False)

    assert record.pipe_calls[0][2]["torch_dtype"] == "float16"


@pytest.mark.parametrize("total, expected", [
    (40 * _GIB, [("to", "cuda")]),
    (80 * _GIB, [("to", "cuda")]),
    (40 * _GIB - 1, [("offload", None)]),
    (24 * _GIB, [("offload", None)]),
])
def test_the_pipeline_lives_on_the_card_only_from_40_gib(monkeypatch, base, tmp_path, total, expected):
    worker, record = _loaded(monkeypatch, base, tmp_path, total=total)

    assert record.pipe.placed == expected
    assert worker._loaded["placement"] == ("resident" if expected[0][0] == "to" else "offload")
    # The real device and placement reach `/health`, not a claim about the wheel.
    assert base.state_calls[-1] == {"device": "cuda", "placement": worker._loaded["placement"]}


def test_tiling_is_enabled_whichever_way_the_pipeline_is_placed(monkeypatch, base, tmp_path):
    _, small = _loaded(monkeypatch, base, tmp_path, total=16 * _GIB)
    _, large = _loaded(monkeypatch, base, tmp_path, total=80 * _GIB)

    assert small.pipe.tiling == 1 and large.pipe.tiling == 1


def test_fps_and_image_support_come_from_the_index(monkeypatch, base, tmp_path):
    wan22, _ = _loaded(monkeypatch, base, tmp_path, index_over={"expand_timesteps": True})
    wan21, _ = _loaded(monkeypatch, base, tmp_path)

    assert (wan22._loaded["expand_timesteps"], wan22._loaded["fps"]) == (True, 24)
    assert (wan21._loaded["expand_timesteps"], wan21._loaded["fps"]) == (False, 16)


def test_the_canvas_grid_is_the_vaes_stride_times_the_patch(monkeypatch, base, tmp_path):
    wan21, _ = _loaded(monkeypatch, base, tmp_path, pipe=FakePipe(spatial=8))
    wan22, _ = _loaded(monkeypatch, base, tmp_path, pipe=FakePipe(spatial=16))

    assert wan21._loaded["mod"] == 16
    assert wan22._loaded["mod"] == 32


# -- generate -------------------------------------------------------------------


def test_the_reply_has_ltx_videos_shape(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)

    reply = worker.generate(_request(tmp_path))

    assert set(reply) == {"path", "seconds", "seed", "width", "height", "frames", "steps"}
    assert reply["path"] == str(tmp_path / "out" / "fox.mp4")
    assert (reply["seed"], reply["width"], reply["height"],
            reply["frames"], reply["steps"]) == (7, 832, 480, 81, 4)
    assert os.path.isfile(reply["path"])


def test_the_defaults_when_the_request_names_none(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path, pipe=FakePipe(spatial=16))

    reply = worker.generate({"prompt": "x", "out": str(tmp_path / "v.mp4")})

    assert (reply["width"], reply["height"], reply["frames"], reply["steps"]) == (
        1280, 704, 121, 50)
    call = record.pipe.calls[0]
    assert call["guidance_scale"] == 5.0
    assert call["negative_prompt"] == worker.DEFAULT_NEGATIVE_PROMPT
    assert "色调艳丽" in worker.DEFAULT_NEGATIVE_PROMPT


def test_a_seed_is_chosen_and_echoed_when_absent(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)
    request = _request(tmp_path)
    del request["seed"]

    reply = worker.generate(request)

    assert isinstance(reply["seed"], int)
    assert record.pipe.calls[0]["generator"].seed == reply["seed"]
    assert record.pipe.calls[0]["generator"].device == "cuda"


def test_a_seed_of_zero_is_a_seed_not_an_absence(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)

    reply = worker.generate(_request(tmp_path, seed=0))

    assert reply["seed"] == 0


@pytest.mark.parametrize("asked, snapped", [
    (1, 1), (5, 5), (6, 9), (80, 81), (81, 81), (82, 85), (120, 121), (121, 121)])
def test_frames_snap_up_to_4n_plus_1(monkeypatch, base, tmp_path, asked, snapped):
    worker, record = _loaded(monkeypatch, base, tmp_path)

    reply = worker.generate(_request(tmp_path, frames=asked))

    assert reply["frames"] == snapped
    assert record.pipe.calls[0]["num_frames"] == snapped


def test_the_canvas_snaps_down_to_the_models_grid(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path, pipe=FakePipe(spatial=16))

    reply = worker.generate(_request(tmp_path, width=1000, height=500))

    assert (reply["width"], reply["height"]) == (992, 480)
    call = record.pipe.calls[0]
    assert (call["width"], call["height"]) == (992, 480)


@pytest.mark.parametrize("width, height, shift", [
    (832, 480, 3.0), (1024, 576, 5.0), (1280, 704, 5.0), (704, 1280, 5.0),
    (960, 960, 3.0)])
def test_flow_shift_follows_the_canvas(monkeypatch, base, tmp_path, width, height, shift):
    worker, record = _loaded(monkeypatch, base, tmp_path, pipe=FakePipe(spatial=8))

    worker.generate(_request(tmp_path, width=width, height=height))

    assert record.schedulers == [{"flow_shift": shift}]
    assert record.pipe.scheduler.made_with == {"flow_shift": shift}


def test_progress_ticks_start_at_zero_and_count_steps(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)

    worker.generate(_request(tmp_path, steps=3))

    assert base.reports == [{"job": "sys:ai-video:abc", "state": "running",
                             "kind": "task", "unit": "", "done": 0, "total": 3,
                             "detail": "Denoising"}]
    assert [(t["done"], t["total"]) for t in base.ticks[:3]] == [(1, 3), (2, 3), (3, 3)]
    assert all(t["job"] == "sys:ai-video:abc" and t["kind"] == "task" for t in base.ticks)
    # The last tick is the encode, and it comes after the last step and before the file.
    assert base.ticks[-1]["detail"] == "Encoding video"


def test_the_mp4_is_written_at_the_fps_the_index_implies(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path,
                             index_over={"expand_timesteps": True})

    worker.generate(_request(tmp_path))

    frames, out, fps = record.exports[0]
    assert (frames, out, fps) == (["frame-0", "frame-1"], str(tmp_path / "out" / "fox.mp4"), 24)


def test_a_cancel_raised_in_the_step_callback_stops_the_render(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)
    base.cancel_on_tick = 2

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path, steps=10))

    assert len(base.ticks) == 2  # stopped inside the callback, not at the end
    assert record.exports == []  # and no mp4 was written


def test_cancel_flag_set_by_the_supervisor_also_stops_it(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path)
    base.CANCEL.set()

    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path, steps=10))

    assert record.exports == []


# -- the reference image (Wan 2.2 TI2V-5B) --------------------------------------


def _png(tmp_path, size=(100, 60)):
    from PIL import Image

    path = tmp_path / "ref.png"
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return str(path)


def test_a_reference_image_goes_through_the_i2v_pipeline_on_the_snapped_canvas(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path,
                             index_over={"expand_timesteps": True},
                             pipe=FakePipe(spatial=16))
    image = _png(tmp_path)

    reply = worker.generate(_request(tmp_path, image=image, width=1000, height=500))

    assert reply["image"] == image
    assert record.from_pipe == [record.pipe]
    assert record.pipe.calls == []  # the T2V view did not render
    sent = record.i2v.calls[0]["image"]
    assert sent.size == (992, 480)  # resized to the snapped canvas
    assert sent.mode == "RGB"


def test_the_i2v_view_is_built_once_and_reused(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path,
                             index_over={"expand_timesteps": True})
    image = _png(tmp_path)

    worker.generate(_request(tmp_path, image=image))
    worker.generate(_request(tmp_path, image=image))

    assert len(record.from_pipe) == 1
    assert len(record.i2v.calls) == 2


def test_a_text_request_on_the_image_model_does_not_build_the_i2v_view(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path,
                             index_over={"expand_timesteps": True})

    reply = worker.generate(_request(tmp_path))

    assert "image" not in reply
    assert record.from_pipe == []
    assert "image" not in record.pipe.calls[0]


def test_an_i2v_class_repo_loads_as_that_class_and_needs_an_image(monkeypatch, base, tmp_path):
    worker, record = _loaded(monkeypatch, base, tmp_path,
                             index_over={"_class_name": "WanImageToVideoPipeline"})

    assert record.pipe_calls[0][0] == "WanImageToVideoPipeline"
    with pytest.raises(ValueError, match="image-to-video only"):
        worker.generate(_request(tmp_path))
    worker.generate(_request(tmp_path, image=_png(tmp_path)))
    assert record.pipe.calls[0]["image"].size == (832, 480)
    assert record.from_pipe == []


# -- memory hooks and the shell -------------------------------------------------


def test_memory_hooks_read_the_cuda_allocator(monkeypatch, base, tmp_path):
    worker, _ = _loaded(monkeypatch, base, tmp_path)

    assert worker.memory() == 7
    assert worker.peak_memory() == 11
    assert worker.release() is None


def test_main_serves_every_hook(monkeypatch, base, tmp_path):
    _install(monkeypatch, tmp_path=tmp_path)
    worker = load_worker(monkeypatch, base)

    worker.main()

    assert set(base.serve_kwargs) == {
        "download", "load", "generate", "streaming", "memory", "peak_memory", "release"}
    assert base.serve_kwargs["streaming"] is False


def test_the_cuda_shell_promises_cuda_and_nothing_else(monkeypatch, base):
    monkeypatch.setitem(sys.modules, "worker_base", base)
    monkeypatch.delitem(sys.modules, "diffusers_video", raising=False)
    spec = importlib.util.spec_from_file_location("diffusers_video_cuda_shell", SHELL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        assert module.diffusers_video.EXPECTED_DEVICE == "cuda"
    finally:
        sys.modules.pop("diffusers_video", None)
