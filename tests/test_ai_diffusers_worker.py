"""What the Diffusers image runner FETCHES, and what it deliberately does not.

The recipe in `runners/torch_image.py` swaps a 2.6GB GGUF transformer in for
the repo's own 7.8GB bf16 one, and the whole value of that swap is bytes not
downloaded. It was silently worth nothing: the skip list named
`transformer/*.safetensors`, and `black-forest-labs/FLUX.2-klein-4B` ALSO
carries a root-level `flux-2-klein-4b.safetensors` — the same weights again, as
one ComfyUI-style bundle — which no skip pattern matched, `from_pretrained`
never opens, and `download_snapshot` fetched. 18.6GB arrived where the model
needs 10.8.

So the patterns are an ALLOW-list now, and this file is what keeps them honest:
the assertions are made against a FROZEN listing of the real repo, run through
`worker_base.selects` — the same filter the bar's total, the segmented fetch and
`snapshot_download` all use — rather than against a re-reading of the pattern
strings. A deny-list has to predict every extra bundle a repo might carry; this
test asks the only question that matters, which is what lands on the disk.
"""
import importlib.util
import os
import sys
import types

import pytest

RUNNERS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners",
)
#: The runner itself, at the runners ROOT — `diffusers_image/`,
#: `diffusers_image_cuda/` and `diffusers_image_rocm/` each hold a five-line
#: `worker.py` shell that imports it, so this is where the recipe lives.
WORKER_PATH = os.path.join(RUNNERS, "torch_image.py")
BASE_PATH = os.path.join(RUNNERS, "worker_base.py")

MODEL = "black-forest-labs/FLUX.2-klein-4B"

#: `black-forest-labs/FLUX.2-klein-4B` as the Hub reported it on 2026-08-16
#: (`/api/models/<id>?blobs=true`), byte sizes intact. Frozen rather than
#: fetched: a test that goes to the network cannot run on CI, and a listing that
#: moves under the assertions is not a regression test. Every file over 1MB is
#: here with its true size; the rest are rounded config-sized figures, which is
#: all any assertion below reads them as.
REPO_FILES = [
    (".gitattributes", 1_600),
    ("LICENSE.md", 10_000),
    ("README.md", 10_000),
    ("editing.jpg", 2_506_000),
    ("others.jpg", 3_387_000),
    ("realism.jpg", 2_856_000),
    ("model_index.json", 600),
    # The bundle nobody reads: the same transformer weights as the subfolder
    # below, in a single ComfyUI-style file. This entry is the bug.
    ("flux-2-klein-4b.safetensors", 7_751_106_000),
    ("transformer/config.json", 900),
    ("transformer/diffusion_pytorch_model.safetensors", 7_751_106_000),
    ("text_encoder/config.json", 1_200),
    ("text_encoder/model.safetensors.index.json", 60_000),
    ("text_encoder/model-00001-of-00002.safetensors", 4_967_000_000),
    ("text_encoder/model-00002-of-00002.safetensors", 3_078_000_000),
    ("tokenizer/tokenizer.json", 11_400_000),
    ("tokenizer/tokenizer_config.json", 3_000),
    ("tokenizer/vocab.json", 3_000_000),
    ("tokenizer/merges.txt", 2_400_000),
    ("tokenizer/added_tokens.json", 400),
    ("tokenizer/special_tokens_map.json", 400),
    ("tokenizer/chat_template.jinja", 500),
    ("vae/config.json", 800),
    ("vae/diffusion_pytorch_model.safetensors", 168_000_000),
    ("scheduler/scheduler_config.json", 500),
]

#: What `from_pretrained` actually opens for this pipeline: the index, every
#: component subfolder it names, and the transformer's CONFIG (the GGUF is a
#: bare tensor file — `from_single_file(config=…, subfolder="transformer")`
#: reads this to know what it is building, which is why "skip the subfolder"
#: was never an option). `text_encoder/` IS allow-listed: `_load_quantization`
#: quantizes it to NF4 at load time, in this process, but `from_pretrained`
#: still has to read the bf16 shards off disk first to have anything to
#: quantize.
READ_BY_THE_PIPELINE = {
    "model_index.json",
    "transformer/config.json",
    "text_encoder/config.json",
    "text_encoder/model.safetensors.index.json",
    "text_encoder/model-00001-of-00002.safetensors",
    "text_encoder/model-00002-of-00002.safetensors",
    "tokenizer/tokenizer.json",
    "tokenizer/tokenizer_config.json",
    "tokenizer/vocab.json",
    "tokenizer/merges.txt",
    "tokenizer/added_tokens.json",
    "tokenizer/special_tokens_map.json",
    "tokenizer/chat_template.jinja",
    "vae/config.json",
    "vae/diffusion_pytorch_model.safetensors",
    "scheduler/scheduler_config.json",
}

#: Bytes the transformer weights account for, in either of their two copies.
TRANSFORMER_BYTES = 7_751_106_000


class _Flag:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


class FakeBase:
    """`worker_base`, recording what the runner asked it to fetch."""

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.snapshot_calls = []
        self.file_calls = []
        self.state_calls = []
        self.serve_kwargs = None
        self.ticks = []
        self.CANCEL = _Flag()
        #: Set by a test to have the Nth tick answer "the ✕ was pressed".
        self.cancel_on_tick = None

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_on_tick is not None and len(self.ticks) >= self.cancel_on_tick:
            self.CANCEL.set()

    def download_snapshot(self, model_id, allow_patterns=None,
                          ignore_patterns=None, **kwargs):
        self.snapshot_calls.append({"model": model_id, "allow": allow_patterns,
                                    "ignore": ignore_patterns, **kwargs})
        return f"/snapshots/{model_id}"

    def download_file(self, repo_id, filename, detail=None):
        self.file_calls.append({"repo": repo_id, "file": filename,
                                "detail": detail})
        return f"/blobs/{filename}"

    def set_state(self, **fields):
        self.state_calls.append(fields)

    def serve(self, **kwargs):
        self.serve_kwargs = kwargs
        return None


def load_worker(monkeypatch, base):
    monkeypatch.setitem(sys.modules, "worker_base", base)
    monkeypatch.syspath_prepend(RUNNERS)
    spec = importlib.util.spec_from_file_location(
        "diffusers_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fresh_base_module():
    """The real `worker_base`, for its pattern filter — see test_ai_hub_fetch."""
    spec = importlib.util.spec_from_file_location(
        "worker_base_for_patterns", BASE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def base():
    return FakeBase()


@pytest.fixture(scope="module")
def selects():
    return _fresh_base_module().selects


def fetched(worker, base, selects):
    """`{name: size}` for what a `download(MODEL)` would land on the disk.

    The runner's own patterns, applied to the frozen listing through the filter
    the download itself uses, so this measures the DOWNLOAD rather than the
    author's intention for it.
    """
    worker.download(MODEL)
    call = base.snapshot_calls[-1]
    return {name: size for name, size in REPO_FILES
            if selects(name, allow=call["allow"], ignore=call["ignore"])}


# -- the bug this file exists for ----------------------------------------------


def test_the_unread_root_bundle_is_not_downloaded(base, monkeypatch, selects):
    """7.75GB of transformer weights sit at the repo root as well as in
    `transformer/`, and the pipeline opens neither copy — the GGUF replaces
    both. The deny-list caught one and paid for the other."""
    worker = load_worker(monkeypatch, base)

    landed = fetched(worker, base, selects)

    assert "flux-2-klein-4b.safetensors" not in landed
    assert "transformer/diffusion_pytorch_model.safetensors" not in landed


def test_the_download_is_only_what_the_pipeline_reads(base, monkeypatch, selects):
    """Stated as a set rather than as a size, because the size is a consequence.
    The README, the three sample JPEGs and both copies of the transformer are
    all bytes the load never touches."""
    worker = load_worker(monkeypatch, base)

    landed = fetched(worker, base, selects)

    assert set(landed) == READ_BY_THE_PIPELINE


def test_the_transformer_config_survives_the_filter(base, monkeypatch, selects):
    """The one file whose absence would be invisible until a machine went
    offline: `from_single_file(config=MODEL, subfolder="transformer")` reads it,
    so a "Download" that skipped it reports success and then fails to load."""
    worker = load_worker(monkeypatch, base)

    landed = fetched(worker, base, selects)

    assert "transformer/config.json" in landed


def test_the_split_costs_about_ten_gigabytes_not_eighteen(base, monkeypatch,
                                                          selects):
    """The figure `catalog.py`'s `size_gb` promises and D310's benchmark
    assumed. Bounded rather than exact — the repo may gain a config file — but
    tight enough that either copy of the transformer reappearing breaks it."""
    worker = load_worker(monkeypatch, base)

    landed = fetched(worker, base, selects)
    gguf = 2_604_300_000  # unsloth/FLUX.2-klein-4B-GGUF, Q4_K_M, Hub metadata

    total = sum(landed.values()) + gguf
    assert 10.5e9 < total < 11.0e9
    # The saving is one whole copy of the transformer weights, and the recipe
    # used to buy none of it: with the old deny-list the root bundle rode along.
    assert total + TRANSFORMER_BYTES > 18.5e9


# -- the component repo, named in one place ------------------------------------


def test_the_gguf_repo_is_a_registered_component(base, monkeypatch):
    """The GGUF lands in the Hub cache as a repo of its own, and the AI Models
    page — in the server process, which cannot import a runner's venv — has to
    be able to say what it is. `formats.COMPONENT_REPOS` is that shared place,
    and the recipe takes the FILENAME from it rather than keeping a second
    copy."""
    from fused_render.ai.runners import formats

    worker = load_worker(monkeypatch, base)
    worker.download(MODEL)

    call = base.file_calls[-1]
    assert call["repo"] in formats.COMPONENT_REPOS
    entry = formats.COMPONENT_REPOS[call["repo"]]
    assert entry["file"] == call["file"]
    assert entry["of"] == MODEL


# -- the live preview (SPEC §40) -------------------------------------------------
#
# The second thing this runner does per step, beside reporting: project the
# model's denoised estimate to a 32x32 PNG the page can point an <img> at. The
# arithmetic and the file lifecycle belong to `runners/preview.py` and are
# tested there; what is pinned HERE is the wiring only — that the latents and
# the sigma this pipeline hands the callback reach that module correctly, and
# that a pipeline with no fitted projection renders exactly as it did before.
#
# No torch and no weights: the pipeline is a fake that drives the callback the
# way diffusers does, with numpy latents of the shape a real klein render holds.


class FakeTensor:
    """A latent tensor, with the three calls the callback makes on it.

    `.detach().to("cpu", dtype).numpy()` — one transfer rather than
    `.float().cpu()`'s two, which is the whole reason the worker spells it this
    way, so the fake insists on it.
    """

    def __init__(self, array, on_read=None):
        self._array = array
        self._on_read = on_read

    def detach(self):
        return self

    def to(self, device, dtype):
        assert device == "cpu", device
        if self._on_read is not None:
            self._on_read()
        return self

    def numpy(self):
        return self._array


class FakePipe:
    """Enough of a diffusers pipeline to run the denoising loop.

    The sigma schedule is klein's shape: one more entry than there are steps and
    ending at zero, already advanced past `sigmas[step]` by the time the
    callback runs — which is the off-by-one the worker's `_sigma_after` exists
    to get right.
    """

    def __init__(self, vae_class="AutoencoderKLFlux2", sigmas=None, on_read=None,
                 latents=None, latents_per_step=None):
        self.vae = type(vae_class, (), {})()
        self.scheduler = types.SimpleNamespace(
            sigmas=sigmas or [1.0, 0.9, 0.7, 0.4, 0.0])
        self.on_read = on_read
        self.latents = latents
        #: One array per step, when a test needs the latents to actually MOVE —
        #: identical latents mean zero velocity, and a zero velocity makes the
        #: denoised estimate equal the latent at every sigma, which is how a
        #: test can look like it pins the sigma indexing without pinning it.
        self.latents_per_step = latents_per_step
        self.watch = None

    def __call__(self, prompt=None, height=None, width=None, guidance_scale=None,
                 num_inference_steps=None, generator=None,
                 callback_on_step_end=None):
        import numpy

        tokens = (height // 16) * (width // 16)
        array = (self.latents if self.latents is not None
                 else numpy.zeros((1, tokens, 128), dtype=numpy.float32))
        for step in range(num_inference_steps):
            held = array if self.latents_per_step is None else self.latents_per_step[step]
            callback_on_step_end(self, step, 0, {
                "latents": FakeTensor(held, on_read=self.on_read)})
            if self.watch is not None:
                self.watch(step)
        return types.SimpleNamespace(images=[FakeSavedImage()])


class FakeSavedImage:
    def save(self, path):
        with open(path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")


def fake_torch():
    """`torch`, for the two things `generate` asks of it."""
    torch = types.ModuleType("torch")
    torch.float32 = "float32"

    class Generator:
        def __init__(self, device=None):
            self.device = device

        def manual_seed(self, seed):
            self.seed = seed
            return self

    torch.Generator = Generator
    return torch


def loaded_worker(monkeypatch, base, pipe):
    """The worker with `pipe` already loaded, as `load` would leave it."""
    monkeypatch.setitem(sys.modules, "torch", fake_torch())
    worker = load_worker(monkeypatch, base)
    worker._loaded.update({"pipe": pipe, "seed_device": "cpu",
                           "vae": type(pipe.vae).__name__})
    return worker


def _request(tmp_path, **over):
    from fused_render.ai.runners import preview

    out = str(tmp_path / "fox.png")
    return {"prompt": "a red fox", "out": out, "job": "sys:ai-image:abc",
            "width": 512, "height": 512, "steps": 4,
            "outPreview": preview.preview_path(out), **over}


def test_load_passes_the_quantization_config_and_no_text_encoder_kwarg(
        monkeypatch, base):
    """The klein recipe's `load()` branch: the transformer is built via
    `from_single_file` and handed in as `transformer=`, a built object, but
    the text encoder is left for the pipeline's own `from_pretrained` to
    construct from the snapshot on disk — `quantization_config=` is what
    turns that construction into NF4 rather than bf16. Passing a
    `text_encoder=` kwarg here would hand the pipeline an already-built,
    unquantized object and skip `_load_quantization` entirely."""
    pipe = FakePipe()
    pipe.to = lambda device: None
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.backends = types.SimpleNamespace(mps=None)
    torch.bfloat16 = "bfloat16"

    seen = {}

    class FakeTransformer:
        @classmethod
        def from_single_file(cls, *a, **k):
            return "the-transformer-object"

    class FakeQuantConfig:
        def __init__(self, **kwargs):
            seen["quant_kwargs"] = kwargs

    class FakePipelineCls:
        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            seen["pipeline_kwargs"] = kwargs
            seen["pipeline_model_id"] = model_id
            return pipe

    diffusers = types.ModuleType("diffusers")
    diffusers.GGUFQuantizationConfig = lambda **k: "the-gguf-quant-config"
    diffusers.PipelineQuantizationConfig = FakeQuantConfig
    diffusers.Flux2Transformer2DModel = FakeTransformer
    diffusers.Flux2KleinPipeline = FakePipelineCls
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setitem(sys.modules, "torch", torch)

    worker = load_worker(monkeypatch, base)
    fetched = {"snapshot": "/snapshots/x", "gguf": "/blobs/transformer.gguf"}
    worker.load(MODEL, fetched)

    assert "text_encoder" not in seen["pipeline_kwargs"]
    assert seen["pipeline_kwargs"]["transformer"] == "the-transformer-object"
    assert seen["pipeline_kwargs"]["quantization_config"] is not None
    assert seen["quant_kwargs"]["components_to_quantize"] == ["text_encoder"]
    assert seen["pipeline_model_id"] == MODEL


def test_the_vae_CLASS_NAME_is_what_the_projection_table_is_keyed_by(monkeypatch, base):
    """Captured at load, off the VAE rather than off the repo id or the
    pipeline: the latent space a fitted matrix belongs to is the autoencoder's,
    and two checkpoints sharing one VAE share one projection."""
    pipe = FakePipe()
    pipe.to = lambda device: None
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.backends = types.SimpleNamespace(mps=None)
    torch.bfloat16 = "bfloat16"
    diffusers = types.ModuleType("diffusers")
    diffusers.AutoPipelineForText2Image = types.SimpleNamespace(
        from_pretrained=lambda *a, **k: pipe)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)

    worker = load_worker(monkeypatch, base)
    worker.load("some/other-model", None)
    assert worker._loaded["vae"] == "AutoencoderKLFlux2"


# -- VAE tiling at load (closes the "VAE decode buffers" gap `_VRAM_HEADROOM_
# BYTES` admits it cannot measure) -----------------------------------------------
#
# `Flux2KleinPipeline` has no `enable_vae_tiling`/`enable_vae_slicing`, so `load`
# calls `enable_tiling()` on the VAE object itself, off the same `vae = getattr
# (pipe, "vae", None)` the projection-table key above is captured from.


def _load_with_pipe(monkeypatch, base, pipe):
    """`load_worker` plus a `diffusers.AutoPipelineForText2Image.from_pretrained`
    that hands back `pipe` — the no-recipe path, same as the projection-table
    test above."""
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.backends = types.SimpleNamespace(mps=None)
    torch.bfloat16 = "bfloat16"
    diffusers = types.ModuleType("diffusers")
    diffusers.AutoPipelineForText2Image = types.SimpleNamespace(
        from_pretrained=lambda *a, **k: pipe)
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)

    worker = load_worker(monkeypatch, base)
    worker.load("some/other-model", None)
    return worker


def test_load_enables_tiling_on_the_pipelines_vae_when_it_has_the_method(monkeypatch, base):
    """The whole point: a VAE that can tile gets asked to, at load time, so the
    no-op-below-threshold gate inside `_decode` is what protects small renders
    rather than this file trying to guess a resolution cutoff itself."""
    calls = []
    fake_vae = types.SimpleNamespace(enable_tiling=lambda: calls.append(1))
    pipe = types.SimpleNamespace(to=lambda device: None, vae=fake_vae)

    _load_with_pipe(monkeypatch, base, pipe)

    assert calls == [1]


def test_load_tolerates_a_pipeline_with_no_vae(monkeypatch, base):
    """A pipeline shape with no `vae` attribute at all must still load —
    the same "an optional capability's absence must not break loading"
    convention `_register_extra_quantizers` follows for a missing backend."""
    pipe = types.SimpleNamespace(to=lambda device: None)

    worker = _load_with_pipe(monkeypatch, base, pipe)

    assert worker._loaded["vae"] is None


def test_load_tolerates_a_vae_without_enable_tiling(monkeypatch, base):
    """A VAE class lacking `enable_tiling` (a fake in a test, some future
    pipeline's autoencoder) must not stop the load either — unlike
    `_load_quantization`'s quantization config, this was never something the
    caller explicitly asked for and needs to be told failed."""
    fake_vae = types.SimpleNamespace()  # no enable_tiling attribute
    pipe = types.SimpleNamespace(to=lambda device: None, vae=fake_vae)

    worker = _load_with_pipe(monkeypatch, base, pipe)

    assert worker._loaded["vae"] == "SimpleNamespace"


# -- the headroom env var's own edge cases (`_vram_headroom_bytes`) --------------
#
# No torch involved: the function reads only `os.environ`, so these exercise
# it directly rather than through `_place`.


def test_vram_headroom_negative_env_is_ignored(monkeypatch, base):
    """A negative headroom would make `_place` plan into MORE VRAM than is
    free — the opposite of what this knob exists to guard against — so it
    must degrade to the documented default exactly like an unparsable string
    does, not be accepted as "a very small margin"."""
    monkeypatch.setenv("FUSED_RENDER_AI_VRAM_HEADROOM_GB", "-4")
    worker = load_worker(monkeypatch, base)

    assert worker._vram_headroom_bytes() == worker._VRAM_HEADROOM_BYTES


def test_vram_headroom_infinite_env_is_ignored(monkeypatch, base):
    """`float("inf")` parses without raising, and `int(inf * (1 << 30))`
    raises `OverflowError` — a class the old `except ValueError:` did not
    catch. It used to reach `_place`'s outer blanket `except`, which silently
    turned the load into plain offload instead of the documented default;
    this pins the direct, cheaper fix instead."""
    monkeypatch.setenv("FUSED_RENDER_AI_VRAM_HEADROOM_GB", "inf")
    worker = load_worker(monkeypatch, base)

    assert worker._vram_headroom_bytes() == worker._VRAM_HEADROOM_BYTES


def test_vram_headroom_absurdly_large_env_is_ignored(monkeypatch, base):
    """Finite but not remotely a plausible headroom in GiB — the same
    "sanity beats trust" reasoning as the negative case, at the other end."""
    monkeypatch.setenv("FUSED_RENDER_AI_VRAM_HEADROOM_GB", "2000")
    worker = load_worker(monkeypatch, base)

    assert worker._vram_headroom_bytes() == worker._VRAM_HEADROOM_BYTES


# -- the group-offload block-size knob's own edge cases (`_num_blocks_per_group`) -
#
# Same "set AND sane" precedence `_vram_headroom_bytes` follows, exercised the
# same way: no torch involved, `_num_blocks_per_group` reads only `os.environ`.


def test_group_offload_blocks_default_when_env_is_unset(monkeypatch, base):
    monkeypatch.delenv("FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS", raising=False)
    worker = load_worker(monkeypatch, base)

    assert worker._num_blocks_per_group() == worker._NUM_BLOCKS_PER_GROUP


def test_group_offload_blocks_env_override_is_honoured(monkeypatch, base):
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS", "4")
    worker = load_worker(monkeypatch, base)

    assert worker._num_blocks_per_group() == 4


def test_group_offload_blocks_zero_env_is_ignored(monkeypatch, base):
    """Zero blocks is not a smaller group, it is a nonsense group — must
    degrade to the documented default exactly like a negative headroom does,
    not be accepted as "the finest possible granularity"."""
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS", "0")
    worker = load_worker(monkeypatch, base)

    assert worker._num_blocks_per_group() == worker._NUM_BLOCKS_PER_GROUP


def test_group_offload_blocks_negative_env_is_ignored(monkeypatch, base):
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS", "-4")
    worker = load_worker(monkeypatch, base)

    assert worker._num_blocks_per_group() == worker._NUM_BLOCKS_PER_GROUP


def test_group_offload_blocks_infinite_env_is_ignored(monkeypatch, base):
    """`float("inf")` parses without raising, and `int(inf)` raises
    `OverflowError` — the same class `_vram_headroom_bytes` had to add a
    dedicated catch for; this knob follows its pattern exactly."""
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS", "inf")
    worker = load_worker(monkeypatch, base)

    assert worker._num_blocks_per_group() == worker._NUM_BLOCKS_PER_GROUP


def test_group_offload_blocks_absurdly_large_env_is_ignored(monkeypatch, base):
    """Finite but not remotely a plausible block count for any shipped
    pipeline — the same "sanity beats trust" reasoning as the headroom
    knob's own absurdly-large case."""
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS", "2000")
    worker = load_worker(monkeypatch, base)

    assert worker._num_blocks_per_group() == worker._NUM_BLOCKS_PER_GROUP


def test_group_offload_blocks_unparsable_env_is_ignored(monkeypatch, base):
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_BLOCKS", "soon")
    worker = load_worker(monkeypatch, base)

    assert worker._num_blocks_per_group() == worker._NUM_BLOCKS_PER_GROUP


# -- the group-offload disk-residency knob (`_group_offload_disk_path`) -----------
#
# "Set AND sane" like the other two knobs, but there is no numeric middle
# ground to sanity-check here — the only recognized override turns disk
# residency OFF, and the default (unset, or anything that is not exactly
# "off") is ON. `conftest.py` redirects `FUSED_RENDER_HOME` to an isolated
# per-run temp directory for the whole suite, so calling `_group_offload_
# disk_path()` directly — real `os.makedirs` and `atexit.register` included —
# is safe here without extra monkeypatching.


def test_group_offload_disk_enabled_by_default_when_env_is_unset(monkeypatch, base):
    monkeypatch.delenv("FUSED_RENDER_AI_GROUP_OFFLOAD_DISK", raising=False)
    worker = load_worker(monkeypatch, base)

    assert worker._group_offload_disk_enabled() is True
    path = worker._group_offload_disk_path()
    assert path is not None
    home = os.environ["FUSED_RENDER_HOME"]
    assert os.path.realpath(path).startswith(os.path.realpath(home))
    assert os.path.isdir(path)


def test_group_offload_disk_env_off_disables_it(monkeypatch, base):
    """The one recognized override — falls back to memory mode, exactly the
    shape `enable_group_offload` had before disk residency existed."""
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_DISK", "off")
    worker = load_worker(monkeypatch, base)

    assert worker._group_offload_disk_enabled() is False
    assert worker._group_offload_disk_path() is None


def test_group_offload_disk_env_off_is_case_insensitive_and_trims_whitespace(
        monkeypatch, base):
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_DISK", "  OFF  ")
    worker = load_worker(monkeypatch, base)

    assert worker._group_offload_disk_enabled() is False
    assert worker._group_offload_disk_path() is None


def test_group_offload_disk_bogus_env_degrades_to_the_default(monkeypatch, base):
    """A typo of "off" (or any other gibberish) is not a recognized disable
    string, so it is treated like unset — disk offload stays ON, the same
    "sane, not just parsed" posture the other two knobs use."""
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_DISK", "0ff")
    worker = load_worker(monkeypatch, base)

    assert worker._group_offload_disk_enabled() is True
    assert worker._group_offload_disk_path() is not None


def test_place_falls_back_to_memory_mode_group_offload_when_the_disk_env_is_off(
        monkeypatch, base):
    """End-to-end through `_place`: the disk env knob reaches `enable_group_
    offload`'s `offload_to_disk_path=` kwarg as `None`, the same "no disk
    path passed at all" shape the rung had before disk residency existed."""
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_DISK", "off")
    torch, nn = _fake_torch_for_placement(free_bytes=int(2 * _GIB))
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert len(pipe.group_offload_calls) == 1
    assert pipe.group_offload_calls[0]["offload_to_disk_path"] is None


# -- size-aware GPU placement (`_place`) ------------------------------------------
#
# Measured on the user's machine: FLUX.2-klein-4B via the ROCm GGUF recipe, a
# 15.9 GiB RX 9060 XT. Loaded and idle, `_place()`'s old unconditional
# `enable_model_cpu_offload()` left `RssAnon` at 11.7 GiB (the weights, parked
# in system RAM) and the worker's own VRAM (`drm-total-vram`) at 0.59 GiB —
# HIP context and staging only — on a card that was otherwise 2.0 GiB used out
# of 15.9. Component sizes ON DISK, not resident: text encoder bf16 8.05 GB
# (the OLD bf16-then-NF4 route this recipe no longer takes — kept as the
# `text_encoder_bytes` fixtures' size below since these placement tests exist
# to exercise the ladder's arithmetic, not to describe today's GGUF-encoder
# footprint, which has not been measured on hardware), transformer GGUF
# Q4_K_M 2.60 GB, VAE 0.17 GB.
#
# A third placement — pinning transformer+VAE resident while the text encoder
# offloaded per call — was built, measured, and removed; see `_place`'s own
# docstring for why (unreachable for this exact pipeline shape, and not
# actually cheaper than all-gpu once accelerate's offload hook CHAIN is
# accounted for).
#
# What IS left below is a three-way decision: all-gpu, `enable_group_offload`
# (block level, memory mode — the new rung) when it does not fit, and plain
# `enable_model_cpu_offload` as the terminal fallback when group offload
# itself raises, or when the probe never got far enough to try it.


class _FakeParam:
    """Enough of a `torch.nn.Parameter`/buffer for `_place`'s byte count:
    `numel() * element_size()`, nothing else is read."""

    def __init__(self, numel, element_size=2):
        self._numel = numel
        self._element_size = element_size

    def numel(self):
        return self._numel

    def element_size(self):
        return self._element_size


def _make_torch_nn():
    """A `torch.nn` stand-in with just enough of `Module` for `isinstance`
    checks — `_place` skips any `pipe.components` entry that is not one
    (the tokenizer, the scheduler), same as the real pipeline's non-tensor
    components."""
    class Module:
        pass
    return types.SimpleNamespace(Module=Module)


def _make_component(nn, param_bytes, buffer_bytes=0):
    """A fake pipeline component: `param_bytes` split across two parameters
    (so a component summing only the first would under-count) plus optional
    buffer bytes, exercising whichever of parameters()/buffers() `_place`
    reads."""
    per_param = param_bytes // 2
    params = [_FakeParam(per_param // 2, element_size=2),
              _FakeParam(per_param - per_param // 2, element_size=2)]
    buffers = [_FakeParam(buffer_bytes // 2, element_size=2)] if buffer_bytes else []

    class Component(nn.Module):
        def parameters(self):
            return iter(params)

        def buffers(self):
            return iter(buffers)

    return Component()


class _FakePlacementPipe:
    """Enough of a diffusers pipeline for `_place`'s size-aware branches to
    exercise the REAL mechanics of `enable_model_cpu_offload`, not just count
    calls to it.

    `enable_model_cpu_offload` (`pipelines/pipeline_utils.py`, verified in the
    installed package around line 1249-1283) runs TWO passes, in order:

    1. Pop every name in `model_cpu_offload_seq.split("->")` out of the
       component set and give it a hook — REGARDLESS of `_exclude_from_
       cpu_offload`. A name still in this string never reaches step 2.
    2. Whatever remains: `.to(device)` (placed, stays resident) if its name
       is in `_exclude_from_cpu_offload`, a hook otherwise.

    `_place` no longer has a branch that ever populates `_exclude_from_
    cpu_offload` (the per-component pin this fixture was built to catch a
    no-op fix for was removed — see `_place`'s docstring), so step 2 always
    hooks everything here now. The two-pass emulation is kept anyway rather
    than collapsed back to a call-counter: `hooked_names`/`placed_names`
    still prove that plain offload actually hooks every component it should,
    the same fidelity bar that caught the original no-op.
    """

    _exclude_from_cpu_offload = []

    def __init__(self, nn, components, model_cpu_offload_seq, to_raises_on=None,
                 group_offload_raises=False, group_offload_fails_at=None):
        self.nn = nn
        self.components = components
        self.model_cpu_offload_seq = model_cpu_offload_seq
        #: `.to(device)` raises when `device == to_raises_on` — simulating a
        #: competing process (or an undercounted component) turning the
        #: all-gpu MOVE itself into a failure, as opposed to the size PROBE
        #: that `mem_get_info_raises` already covers.
        self._to_raises_on = to_raises_on
        #: `enable_group_offload` raises immediately, before hooking ANY
        #: component — simulating whatever diffusers version/backend
        #: combination makes the new rung itself unusable outright, which
        #: must degrade to plain offload exactly like a raising `.to("cuda")`
        #: does for the all-gpu case.
        self._group_offload_raises = group_offload_raises
        #: Raises only on a call that carries a disk path — set directly on
        #: the instance rather than threaded through `_placement_pipe`'s own
        #: constructor, which callers already reach for the two group-
        #: offload-always-fails shapes above. Simulates the torchao "clean
        #: failure only in disk mode" case Finding #3's retry rung exists
        #: for: a call with `offload_to_disk_path=None` (the retry itself)
        #: succeeds normally.
        self._group_offload_raises_if_disk = False
        #: The real `enable_group_offload` hooks pipeline components ONE AT A
        #: TIME in a loop (`pipelines/pipeline_utils.py`), so a raise on
        #: component N leaves components before it already hooked. Naming a
        #: component here reproduces exactly that: every component ahead of
        #: it in `self.components` (that survives `exclude_modules`) gets
        #: recorded into `group_offloaded_names` before the raise, so
        #: `_maybe_raise_error_if_group_offload_active` has something real to
        #: find.
        self._group_offload_fails_at = group_offload_fails_at
        for name, component in components.items():
            setattr(self, name, component)
        self.to_calls = []
        self.offload_calls = 0
        self.group_offload_calls = []
        self.group_offloaded_names = []
        self.hooked_names = []
        self.placed_names = []

    def to(self, device):
        self.to_calls.append(device)
        if device == self._to_raises_on:
            raise RuntimeError("HIP out of memory")

    def enable_group_offload(self, exclude_modules=None, **kwargs):
        self.group_offload_calls.append({"exclude_modules": exclude_modules, **kwargs})
        if self._group_offload_raises:
            raise RuntimeError("group offload unsupported here")
        if self._group_offload_raises_if_disk and kwargs.get("offload_to_disk_path") is not None:
            raise RuntimeError("disk offload unsupported here (torchao-shaped failure)")
        exclude_modules = exclude_modules or []
        for name, component in self.components.items():
            if name in exclude_modules or not isinstance(component, self.nn.Module):
                continue
            if name == self._group_offload_fails_at:
                raise RuntimeError(f"group offload unsupported for {name!r}")
            self.group_offloaded_names.append(name)

    def _maybe_raise_error_if_group_offload_active(self, raise_error=False):
        """Mirrors the real pipeline method
        (`pipelines/pipeline_utils.py:_maybe_raise_error_if_group_offload_
        active`) closely enough for `_place`'s fallback guard to exercise it:
        true when ANY component still carries a group-offload hook, and
        raises instead of returning when asked to."""
        active = bool(self.group_offloaded_names)
        if active and raise_error:
            raise ValueError(
                "You are trying to apply model/sequential CPU offloading to a "
                "pipeline that contains components with group offloading enabled."
            )
        return active

    def enable_model_cpu_offload(self):
        self.offload_calls += 1
        remaining = {name: component for name, component in self.components.items()
                     if isinstance(component, self.nn.Module)}
        self.hooked_names = []
        self.placed_names = []
        for name in self.model_cpu_offload_seq.split("->"):
            if remaining.pop(name, None) is not None:
                self.hooked_names.append(name)
        for name, component in remaining.items():
            if name in self._exclude_from_cpu_offload:
                self.placed_names.append(name)
            else:
                self.hooked_names.append(name)


def _fake_torch_for_placement(free_bytes, mem_get_info_raises=False):
    torch = fake_torch()
    nn = _make_torch_nn()
    torch.nn = nn

    def mem_get_info():
        if mem_get_info_raises:
            raise RuntimeError("no ROCm device visible")
        return free_bytes, free_bytes * 4  # (free, total) — total unused by _place

    torch.cuda = types.SimpleNamespace(is_available=lambda: True,
                                       mem_get_info=mem_get_info)
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: False))
    # Identity stand-in: enough for `_place` to hand `enable_group_offload`
    # something keyed the same way a real `torch.device` would be, without
    # this fixture needing the real class's `.type`/equality machinery.
    torch.device = lambda name: name
    return torch, nn


_GIB = 1 << 30


def _placement_pipe(nn, transformer_bytes, vae_bytes, text_encoder_bytes,
                    to_raises_on=None, group_offload_raises=False,
                    group_offload_fails_at=None):
    """A 4-component pipeline (`text_encoder`, `transformer`, `vae`, plus a
    non-Module `tokenizer` that `_place` must skip when summing bytes and
    never mistake for something `enable_model_cpu_offload` places)."""
    components = {
        "text_encoder": _make_component(nn, text_encoder_bytes),
        "transformer": _make_component(nn, transformer_bytes),
        "vae": _make_component(nn, vae_bytes),
        "tokenizer": object(),
    }
    seq = "->".join(name for name, component in components.items()
                    if isinstance(component, nn.Module))
    return _FakePlacementPipe(nn, components, seq, to_raises_on=to_raises_on,
                              group_offload_raises=group_offload_raises,
                              group_offload_fails_at=group_offload_fails_at)


def test_place_puts_everything_on_gpu_when_it_all_fits(monkeypatch, base):
    torch, nn = _fake_torch_for_placement(free_bytes=20 * _GIB)
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert pipe.to_calls == ["cuda"]
    assert pipe.offload_calls == 0
    assert {"placement": "all-gpu"} in base.state_calls


def test_place_uses_group_offload_when_it_does_not_all_fit(monkeypatch, base):
    """Below the all-gpu floor: the new rung, `enable_group_offload` at block
    level, REPLACES plain offload rather than stacking beside it — plain
    `enable_model_cpu_offload` must not be called at all when group offload
    itself succeeds."""
    torch, nn = _fake_torch_for_placement(free_bytes=int(2 * _GIB))  # < 3 GiB headroom
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert pipe.offload_calls == 0
    assert pipe.to_calls == []
    assert len(pipe.group_offload_calls) == 1
    kwargs = pipe.group_offload_calls[0]
    assert kwargs["onload_device"] == "cuda"
    assert kwargs["offload_device"] == "cpu"
    assert kwargs["offload_type"] == "block_level"
    assert kwargs["num_blocks_per_group"] == worker._NUM_BLOCKS_PER_GROUP
    # Disk residency is on by default — a real path under this test's own
    # isolated FUSED_RENDER_HOME (see conftest.py's redirect), never None and
    # never under a tmpfs-backed dir like $XDG_RUNTIME_DIR or /tmp.
    disk_path = kwargs["offload_to_disk_path"]
    assert disk_path is not None
    home = os.environ["FUSED_RENDER_HOME"]
    assert os.path.realpath(disk_path).startswith(os.path.realpath(home))
    # Unsettled pending the hardware gate (SPEC/D) — must not be forced on.
    assert kwargs.get("use_stream") is not True
    # The VAE must reach `enable_group_offload` as an EXCLUSION, not silently
    # absorbed into block offload — `AutoencoderKLFlux2` (the klein recipe's
    # VAE) has no `_group_offload_block_modules`, so block-offloading it would
    # leave its weights on the offload device when `.decode(...)` is called,
    # which never fires a group-offload hook (only `.forward()` does). The
    # text encoder is NOT excluded: it is a GGUF component now, and GGUF's
    # quantized tensors round-trip through `ModuleGroup` correctly, unlike
    # the bitsandbytes NF4 weights this recipe used to load it with.
    assert kwargs["exclude_modules"] == ["vae"]
    assert {"placement": "group-offload"} in base.state_calls


def test_place_falls_back_to_plain_offload_when_group_offload_raises(monkeypatch, base):
    """Group offload itself failing (an unsupported diffusers/backend
    combination) must degrade to today's unconditional `enable_model_cpu_
    offload()` — the same "a load that would have succeeded via plain
    offload must not fail outright" reasoning the all-gpu case's own
    raising `.to("cuda")` already follows.

    Disk mode is forced off here: a clean failure in disk mode gets its own
    retry rung (`test_place_retries_in_memory_mode_after_a_clean_disk_mode_
    failure` below) before falling back to plain offload, which would make
    `group_offload_calls` land at 2, not 1. This test is about the
    fallback-to-plain-offload contract on its own, so it pins the simpler,
    single-attempt shape that contract had before disk residency existed."""
    monkeypatch.setenv("FUSED_RENDER_AI_GROUP_OFFLOAD_DISK", "off")
    torch, nn = _fake_torch_for_placement(free_bytes=int(2 * _GIB))
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB),
                            group_offload_raises=True)

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert len(pipe.group_offload_calls) == 1  # the attempt happened
    assert pipe.offload_calls == 1  # and fell back to it
    assert pipe.placed_names == []
    assert set(pipe.hooked_names) == {"transformer", "vae", "text_encoder"}
    assert {"placement": "offload"} in base.state_calls
    assert {"placement": "group-offload"} not in base.state_calls


def test_place_reraises_original_error_when_group_offload_fails_partway(monkeypatch, base):
    """Finding #2: `enable_group_offload` hooks components ONE AT A TIME in a
    loop, so a raise on the third hookable component (`extra`, after
    `text_encoder` and `transformer` already got hooked — `vae` is the only
    exclusion now and is never reached) leaves both earlier components
    group-offloaded even though the call overall failed. `enable_model_cpu_
    offload` opens with `_maybe_raise_error_if_group_offload_active(raise_
    error=True)` and refuses to run AT ALL while any component still carries
    a group-offload hook, so blindly falling back to it here would replace
    the real failure with a confusing `ValueError` about group offload being
    active — on a load that plain offload could never have completed anyway.
    `_place` must detect that partial state via `_maybe_raise_error_if_
    group_offload_active(raise_error=False)` and re-raise the ORIGINAL error
    instead of attempting (and failing) the fallback.

    A FOURTH hookable component (`extra`, beside `text_encoder` and
    `transformer`) is built by hand here rather than through `_placement_
    pipe`, which only wires up the three names an ordinary klein pipeline
    has — this test needs one that fails partway through a MULTI-component
    hook loop, which needs at least two components to have already
    succeeded before the failure."""
    torch, nn = _fake_torch_for_placement(free_bytes=int(2 * _GIB))
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    components = {
        "text_encoder": _make_component(nn, int(8.05 * _GIB)),
        "transformer": _make_component(nn, int(2.6 * _GIB)),
        "extra": _make_component(nn, int(0.1 * _GIB)),
        "vae": _make_component(nn, int(0.17 * _GIB)),
        "tokenizer": object(),
    }
    seq = "->".join(name for name, component in components.items()
                    if isinstance(component, nn.Module))
    pipe = _FakePlacementPipe(nn, components, seq,
                              group_offload_fails_at="extra")

    with pytest.raises(RuntimeError, match="extra"):
        worker._place(pipe)

    # The partial hook is real — `text_encoder` and `transformer` both got
    # group-offloaded before the raise on `extra`. `vae` was never attempted
    # at all: it is the only component excluded from `enable_group_offload`.
    assert pipe.group_offloaded_names == ["text_encoder", "transformer"]
    # The fallback must not even be attempted: `enable_model_cpu_offload`
    # would only raise its own confusing error over this state.
    assert pipe.offload_calls == 0
    assert {"placement": "group-offload"} not in base.state_calls
    assert {"placement": "offload"} not in base.state_calls


def test_place_retries_in_memory_mode_after_a_clean_disk_mode_failure(monkeypatch, base):
    """Finding #3: a clean failure (nothing hooked) that happened only
    because `offload_to_disk_path` was set — e.g. a torchao-quantized
    component's `_check_disk_offload_torchao` guard firing at hook-install
    time — gets one retry with `offload_to_disk_path=None` before falling
    all the way back to plain offload. The retry succeeds here, landing on
    `group-offload` exactly as if disk residency had never been attempted."""
    torch, nn = _fake_torch_for_placement(free_bytes=int(2 * _GIB))
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))
    pipe._group_offload_raises_if_disk = True

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert len(pipe.group_offload_calls) == 2
    assert pipe.group_offload_calls[0]["offload_to_disk_path"] is not None
    assert pipe.group_offload_calls[1]["offload_to_disk_path"] is None
    assert pipe.offload_calls == 0
    assert {"placement": "group-offload"} in base.state_calls
    assert {"placement": "offload"} not in base.state_calls


def test_place_falls_back_to_plain_offload_after_disk_mode_retry_also_fails(monkeypatch, base):
    """Finding #3, the other half: when even the memory-mode retry fails
    cleanly, `_place` falls all the way back to plain `enable_model_cpu_
    offload()` — two `enable_group_offload` attempts (disk, then memory),
    both clean failures, before the same fallback `test_place_falls_back_
    to_plain_offload_when_group_offload_raises` proves for the single-
    attempt memory-mode case above."""
    torch, nn = _fake_torch_for_placement(free_bytes=int(2 * _GIB))
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB),
                            group_offload_raises=True)

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert len(pipe.group_offload_calls) == 2
    assert pipe.group_offload_calls[0]["offload_to_disk_path"] is not None
    assert pipe.group_offload_calls[1]["offload_to_disk_path"] is None
    assert pipe.offload_calls == 1
    assert {"placement": "offload"} in base.state_calls
    assert {"placement": "group-offload"} not in base.state_calls


# -- the generic structural quantization detector (`_unsafe_for_group_offload`) --
#
# No name list and no `import bitsandbytes`/`gguf` — see that function's own
# docstring for the full reasoning. These fakes build the two shapes real
# quantizers actually produce (bitsandbytes' separate `quant_state` tensor,
# GGUF's `.data`-only parameter) structurally, rather than asserting against
# either package by name.


def _fake_torch_with_tensor():
    """A `torch` stand-in whose `Tensor` is a real, empty class — enough for
    `_unsafe_for_group_offload`'s own `isinstance(value, torch.Tensor)`
    check, and for test fakes to hang arbitrary extra attributes off an
    instance the same way a quantized parameter would."""
    torch = types.ModuleType("torch")

    class Tensor:
        pass

    torch.Tensor = Tensor
    return torch, Tensor


class _DetectorComponent:
    """Enough of a pipeline component for `_unsafe_for_group_offload`'s own
    walk: `named_parameters()`/`named_buffers()`, nothing else read."""

    def __init__(self, params=(), buffers=()):
        self._params = list(params)
        self._buffers = list(buffers)

    def named_parameters(self):
        return iter(self._params)

    def named_buffers(self):
        return iter(self._buffers)


def test_unsafe_for_group_offload_excludes_a_component_with_an_untracked_tensor_attribute(
        monkeypatch, base):
    """A registered parameter that itself holds a SECOND tensor as a plain
    attribute — nowhere `named_parameters`/`named_buffers` would find it —
    is exactly what block offload's `.data`-only move would leave behind on
    the old device."""
    torch, Tensor = _fake_torch_with_tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    weight = Tensor()
    weight.quant_state = Tensor()
    component = _DetectorComponent(params=[("weight", weight)])

    assert worker._unsafe_for_group_offload(component) is True


def test_unsafe_for_group_offload_excludes_bitsandbytes_double_quantized_shape(
        monkeypatch, base):
    """bitsandbytes' actual shape nests one level deeper than the flat case
    above: `weight.quant_state` is itself a plain, non-tensor object, and
    THAT object's own `absmax`/`code` attributes are the tensors block
    offload's move would strand. `registered_ids` is threaded through the
    recursion unchanged, so `absmax` is checked against `weight`'s
    registration — the component's actual `named_parameters()` — not
    treated as newly "registered" for having been reached via `quant_state`."""
    torch, Tensor = _fake_torch_with_tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    weight = Tensor()
    weight.quant_state = types.SimpleNamespace(absmax=Tensor(), code=Tensor())
    component = _DetectorComponent(params=[("weight", weight)])

    assert worker._unsafe_for_group_offload(component) is True


def test_unsafe_for_group_offload_allows_a_gguf_shaped_component(monkeypatch, base):
    """`GGUFParameter`'s real shape: nothing but `.data` and a plain string
    `quant_type` attribute — no separate tensor hanging off it, so the move
    `enable_group_offload` performs (`tensor.data` reassignment) carries the
    whole quantized representation along correctly."""
    torch, Tensor = _fake_torch_with_tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    weight = Tensor()
    weight.quant_type = "Q4_K"
    component = _DetectorComponent(params=[("weight", weight)])

    assert worker._unsafe_for_group_offload(component) is False


def test_unsafe_for_group_offload_allows_a_plain_unquantized_component(monkeypatch, base):
    """An ordinary bf16 parameter with no extra attributes at all — the
    common case, and the one this check must not flag."""
    torch, Tensor = _fake_torch_with_tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    component = _DetectorComponent(params=[("weight", Tensor())])

    assert worker._unsafe_for_group_offload(component) is False


def test_unsafe_for_group_offload_is_fail_safe_when_named_parameters_raises(monkeypatch, base):
    """Anything this walk cannot make sense of comes back UNSAFE — staying
    resident costs memory, where guessing safe and being wrong silently
    corrupts a render."""
    torch, _Tensor = _fake_torch_with_tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)

    class _Broken:
        def named_parameters(self):
            raise RuntimeError("not a real module")

        def named_buffers(self):
            return iter(())

    assert worker._unsafe_for_group_offload(_Broken()) is True


def test_group_offload_exclusions_always_excludes_vae_even_when_structurally_safe(
        monkeypatch, base):
    """`vae` is excluded unconditionally — the decode-path reason
    `_place`'s own docstring gives, a hook problem the structural check has
    nothing to say about — even when its own parameters would otherwise
    pass the structural check cleanly."""
    torch, Tensor = _fake_torch_with_tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    vae = _DetectorComponent(params=[("weight", Tensor())])
    pipe = types.SimpleNamespace(components={"vae": vae})

    assert worker._group_offload_exclusions(pipe) == ["vae"]


def test_group_offload_exclusions_includes_structurally_unsafe_non_vae_components(
        monkeypatch, base):
    """A quantizer-agnostic catalog: whichever component's own parameters
    turn out unsafe gets excluded, by structure, with no name list singling
    out `text_encoder` (or any other component) by name."""
    torch, Tensor = _fake_torch_with_tensor()
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    unsafe_weight = Tensor()
    unsafe_weight.quant_state = Tensor()  # a second, untracked tensor attribute
    pipe = types.SimpleNamespace(components={
        "text_encoder": _DetectorComponent(params=[("weight", unsafe_weight)]),
        "transformer": _DetectorComponent(params=[("weight", Tensor())]),
        "vae": _DetectorComponent(params=[("weight", Tensor())]),
        "tokenizer": object(),  # no named_parameters — skipped, not flagged
    })

    assert set(worker._group_offload_exclusions(pipe)) == {"vae", "text_encoder"}


# -- stale group-offload directory cleanup (`_sweep_stale_group_offload_dirs`) ---
#
# `os.kill` is monkeypatched on the real `os` module, not faked through
# `sys.modules` — `torch_image.py` imports `os` the ordinary way, so
# patching the attribute on the same module object the worker already holds
# a reference to reaches it without needing a fake stand-in module.


def test_sweep_stale_group_offload_dirs_removes_a_dead_pid_directory(monkeypatch, base, tmp_path):
    worker = load_worker(monkeypatch, base)
    stale = tmp_path / "424242-deadbeef"
    stale.mkdir()
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError()))

    worker._sweep_stale_group_offload_dirs(str(tmp_path))

    assert not stale.exists()


def test_sweep_stale_group_offload_dirs_keeps_a_live_pid_directory(monkeypatch, base, tmp_path):
    worker = load_worker(monkeypatch, base)
    live = tmp_path / "424242-deadbeef"
    live.mkdir()
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # answers: still alive

    worker._sweep_stale_group_offload_dirs(str(tmp_path))

    assert live.exists()


def test_sweep_stale_group_offload_dirs_leaves_an_unparseable_name_alone(
        monkeypatch, base, tmp_path):
    """A directory whose name this function cannot parse as `<pid>-<random>`
    is left alone rather than guessed at — `os.kill` is never even asked
    about it."""
    worker = load_worker(monkeypatch, base)
    odd = tmp_path / "not-a-pid-at-all"
    odd.mkdir()
    calls = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(pid))

    worker._sweep_stale_group_offload_dirs(str(tmp_path))

    assert odd.exists()
    assert calls == []


def test_sweep_stale_group_offload_dirs_leaves_an_unconfirmed_pid_alone(
        monkeypatch, base, tmp_path):
    """`os.kill` raising anything OTHER than `ProcessLookupError` — `EPERM`
    on a foreign-owned pid, most plausibly — means "alive, or this process
    cannot tell", never "confidently dead"; the directory must not be
    removed on that answer."""
    worker = load_worker(monkeypatch, base)
    unsure = tmp_path / "424242-deadbeef"
    unsure.mkdir()
    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(PermissionError()))

    worker._sweep_stale_group_offload_dirs(str(tmp_path))

    assert unsure.exists()


def test_group_offload_disk_path_never_repeats_within_a_process(monkeypatch, base):
    """Identity is `<pid>-<random>`, never pid alone — two calls in the same
    process (only ever happens in tests; `_place` calls this at most once
    per load) must not collide."""
    monkeypatch.delenv("FUSED_RENDER_AI_GROUP_OFFLOAD_DISK", raising=False)
    worker = load_worker(monkeypatch, base)

    first = worker._group_offload_disk_path()
    second = worker._group_offload_disk_path()

    assert first != second
    assert os.path.isdir(first)
    assert os.path.isdir(second)


def test_place_falls_back_to_offload_when_mem_get_info_raises(monkeypatch, base):
    """An older torch, or an exotic ROCm build missing `mem_get_info` —
    the placement PROBE failing must never break loading; it degrades to
    today's unconditional `enable_model_cpu_offload()`, the same reasoning
    `release()`'s per-backend try/except documents."""
    torch, nn = _fake_torch_for_placement(free_bytes=20 * _GIB, mem_get_info_raises=True)
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert pipe.offload_calls == 1
    assert pipe._exclude_from_cpu_offload == []
    assert pipe.placed_names == []
    # The probe never ran, so there is no "does it fit" answer to hand the
    # group-offload rung — it must be skipped entirely, not attempted blind.
    assert pipe.group_offload_calls == []


def test_place_falls_back_to_offload_when_component_sizing_raises(monkeypatch, base):
    """A component whose `.parameters()` raises (an exotic module type the
    measurement did not anticipate) must degrade the same way a missing
    `mem_get_info` does, not take the whole load down with it."""
    torch, nn = _fake_torch_for_placement(free_bytes=20 * _GIB)
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    class _Boom(nn.Module):
        def parameters(self):
            raise RuntimeError("not a real module")
    pipe.components["broken"] = _Boom()

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert pipe.offload_calls == 1
    assert pipe._exclude_from_cpu_offload == []
    assert pipe.placed_names == []
    assert pipe.group_offload_calls == []


def test_place_falls_back_to_offload_when_the_all_gpu_move_itself_raises(
    monkeypatch, base
):
    """Finding #3: the probe's own `try/except` covers the MEASUREMENT, but
    `pipe.to("cuda")` used to run outside it — so a competing process
    grabbing VRAM after `mem_get_info()` was sampled (or a component whose
    true device cost exceeds `numel * element_size`, the same undercount
    `_component_bytes` flags as possible) turned a load that used to succeed
    via unconditional offload into a hard failure surfaced as `state:
    error`. The probe already promises "must never break loading"; the
    ACTION it authorizes has to keep that promise too."""
    torch, nn = _fake_torch_for_placement(free_bytes=20 * _GIB)
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB),
                            to_raises_on="cuda")

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert pipe.to_calls == ["cuda"]  # the attempt happened
    assert pipe.offload_calls == 1  # and fell back to it
    assert pipe.placed_names == []
    assert {"placement": "offload"} in base.state_calls
    assert {"placement": "all-gpu"} not in base.state_calls


def test_place_mps_path_is_untouched_by_the_size_probe(monkeypatch, base):
    """MPS never measures anything and never calls `enable_model_cpu_offload`
    — unified memory makes offloading pure overhead there, per `_place`'s own
    docstring, and this branch must be unreachable from the CUDA logic."""
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(_make_torch_nn(), transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("mps", "cpu")
    assert pipe.to_calls == ["mps"]
    assert pipe.offload_calls == 0


def test_place_cpu_path_is_untouched_by_the_size_probe(monkeypatch, base):
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    torch.backends = types.SimpleNamespace(mps=None)
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(_make_torch_nn(), transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cpu", "cpu")
    assert pipe.to_calls == ["cpu"]
    assert pipe.offload_calls == 0


def test_place_headroom_env_override_is_honoured(monkeypatch, base):
    """`FUSED_RENDER_AI_VRAM_HEADROOM_GB` overrides the 3 GiB default. total
    (~10.82 GiB) + the 3 GiB default headroom is 13.82, which does not clear
    12 GiB free — but total + a 0.5 GiB override is 11.32, which does
    (all-gpu). Only that the override changes the outcome is asserted here."""
    monkeypatch.setenv("FUSED_RENDER_AI_VRAM_HEADROOM_GB", "0.5")
    torch, nn = _fake_torch_for_placement(free_bytes=12 * _GIB)
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    assert pipe.to_calls == ["cuda"]
    assert pipe.offload_calls == 0


def test_place_unparsable_headroom_env_override_is_ignored(monkeypatch, base):
    """The house pattern `prefs.ai_idle_unload_minutes_override` sets: a
    *set, unparsable* env value is treated as absent rather than crashing
    placement or silently becoming a headroom of `0`. total (~10.82 GiB): the
    3 GiB default headroom puts the all-gpu bound at 13.82, which does NOT
    clear 13 GiB free; a silently-zeroed headroom would put it at 10.82,
    which WOULD — so an unparsable override falling through to `0` instead
    of the documented default flips this test's outcome from offload to
    all-gpu."""
    monkeypatch.setenv("FUSED_RENDER_AI_VRAM_HEADROOM_GB", "soon")
    torch, nn = _fake_torch_for_placement(free_bytes=13 * _GIB)
    monkeypatch.setitem(sys.modules, "torch", torch)
    worker = load_worker(monkeypatch, base)
    pipe = _placement_pipe(nn, transformer_bytes=int(2.6 * _GIB),
                            vae_bytes=int(0.17 * _GIB),
                            text_encoder_bytes=int(8.05 * _GIB))

    device, seed_device = worker._place(pipe)

    assert (device, seed_device) == ("cuda", "cuda")
    # "Offload" here means "not all-gpu" — the probe answered "does not fit",
    # which now reaches the group-offload rung rather than plain offload.
    assert len(pipe.group_offload_calls) == 1
    assert pipe.offload_calls == 0
    assert pipe.to_calls == []
    assert {"placement": "group-offload"} in base.state_calls
    assert {"placement": "all-gpu"} not in base.state_calls


# -- releasing the allocator on an idle timer (D597) -----------------------------


def test_release_calls_cuda_even_when_the_mps_call_raises(monkeypatch, base):
    """THE regression this locks down. `torch/__init__.py` imports `torch.mps`
    unconditionally on every platform and `empty_cache` is a plain function
    that always exists on it — so a presence check (`getattr`/`hasattr`)
    always passes, even on a CPU/CUDA/ROCm build with no MPS backend at all.
    Calling it anyway reaches `torch._C._mps_emptyCache()`, which raises
    `RuntimeError("Cannot execute emptyCache() without MPS backend.")` — and
    on an unconditional call (no per-backend try/except) that exception took
    the CUDA branch down with it before it ever ran, on exactly the one build
    (`diffusers_image_cuda`/`_rocm`) that has a caching allocator worth
    reclaiming at all. `torch.backends.mps.is_available()` — `_place()`'s own
    gate above — is what actually distinguishes the two cases; this test's
    `mps.empty_cache` still raises even though `is_available()` correctly
    says `False`, to prove the gate is what stops the call, not luck."""
    torch = fake_torch()
    calls = []

    def raising_empty_cache():
        raise RuntimeError("Cannot execute emptyCache() without MPS backend.")

    torch.mps = types.SimpleNamespace(empty_cache=raising_empty_cache)
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: False))
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        empty_cache=lambda: calls.append("cuda"))
    monkeypatch.setitem(sys.modules, "torch", torch)

    worker = load_worker(monkeypatch, base)
    worker.release()  # must not raise, and must not skip the CUDA branch

    assert calls == ["cuda"]


def test_release_calls_mps_when_it_is_actually_available(monkeypatch, base):
    """The other half: a real Apple Silicon build DOES get the call."""
    torch = fake_torch()
    calls = []
    torch.mps = types.SimpleNamespace(empty_cache=lambda: calls.append("mps"))
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: True))
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch)

    worker = load_worker(monkeypatch, base)
    worker.release()

    assert calls == ["mps"]


def test_release_survives_both_backends_raising(monkeypatch, base):
    """Neither backend's failure reaches the caller — `_fire_release` already
    swallows and logs a raising `release`, so a release that raises here would
    only be double-handled, never a functional problem, but the whole point of
    the per-backend try/except is that ONE of the two is still allowed to
    succeed even when the other cannot, which this alone cannot show without
    the mixed case above. This pins the belt-and-braces half: even if the gate
    itself were ever wrong on some future torch build, this function still
    cannot raise into `worker_base`."""
    torch = fake_torch()

    def raising_mps():
        raise RuntimeError("boom")

    def raising_cuda():
        raise RuntimeError("boom")

    torch.mps = types.SimpleNamespace(empty_cache=raising_mps)
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: True))
    torch.cuda = types.SimpleNamespace(is_available=lambda: True, empty_cache=raising_cuda)
    monkeypatch.setitem(sys.modules, "torch", torch)

    worker = load_worker(monkeypatch, base)
    worker.release()  # must not raise


# -- the live-VRAM footprint hook (`worker_base.serve(footprint=...)`) ----------


def test_main_wires_a_footprint_hook_into_serve(monkeypatch, base):
    """`main()` is the one caller of `worker_base.serve` in this file — the
    same wiring `memory=`/`release=` already get, plus the new fourth hook."""
    worker = load_worker(monkeypatch, base)
    worker.main()
    assert base.serve_kwargs["footprint"] is worker._gpu_footprint


def test_the_footprint_hook_reports_reserved_not_allocated_bytes(monkeypatch, base):
    """RESERVED, not allocated: reserved is what the driver has actually been
    asked for, and it is the figure `release()`'s `torch.cuda.empty_cache()`
    call actually returns to the OS — so it is the number that visibly moves
    when the idle release fires. `memory_allocated()` would keep reporting
    only the tensors currently live and miss exactly the pool this hook
    exists to show being reclaimed."""
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: True,
        memory_allocated=lambda: 111,
        memory_reserved=lambda: 222)
    monkeypatch.setitem(sys.modules, "torch", torch)

    worker = load_worker(monkeypatch, base)

    assert worker._gpu_footprint() == 222


def test_the_footprint_hook_is_silent_when_there_is_no_cuda_device(monkeypatch, base):
    """CUDA only, never MPS: on darwin `phys_footprint` (`os_footprint_bytes`
    in `worker_base`) already counts the Metal pool a torch-on-MPS build
    reports through, so adding an MPS figure on top would double-count the
    same bytes. This hook must answer `None` on an MPS or CPU build, exactly
    like the CPU/MPS branches of `_place` never call `enable_model_cpu_
    offload` — same boundary, restated for the memory probe instead of the
    placement decision."""
    torch = fake_torch()
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    monkeypatch.setitem(sys.modules, "torch", torch)

    worker = load_worker(monkeypatch, base)

    assert worker._gpu_footprint() is None


def test_a_thumbnail_appears_from_the_SECOND_step_and_is_GONE_at_the_end(
        monkeypatch, base, tmp_path):
    """Step 1 has no predecessor, so it has no velocity and no estimate — and
    the last frame is duplicate bytes once the real PNG lands."""
    seen = []
    pipe = FakePipe()
    request = _request(tmp_path)
    pipe.watch = lambda step: seen.append(os.path.exists(request["outPreview"]))
    worker = loaded_worker(monkeypatch, base, pipe)
    worker.generate(request)

    assert seen == [False, True, True, True]
    assert os.listdir(tmp_path) == ["fox.png"]


def _frame_pixels(path):
    from PIL import Image

    import numpy

    with Image.open(path) as image:
        return image.size, numpy.frombuffer(
            image.convert("RGB").tobytes(), dtype=numpy.uint8).tolist()


def _expected_frame(preview, previous, current, sigma_previous, sigma_current):
    """The bytes `_write` would produce for one estimate, computed the same way.

    Through `preview`'s own `denoised` and `project` rather than a second
    implementation of the arithmetic — what is under test here is which SIGMAS
    the worker paired with which latents, not whether the maths is right (that
    is `test_ai_image_preview.py`'s job).
    """
    import numpy

    estimate = preview.denoised(previous, current, sigma_previous, sigma_current)
    rgb = preview.project(estimate, "AutoencoderKLFlux2")
    return numpy.asarray(rgb * 255.0 + 0.5, dtype=numpy.uint8).reshape(-1).tolist()


def test_the_thumbnail_is_the_estimate_at_the_sigma_just_REACHED(
        monkeypatch, base, tmp_path):
    """The wiring's one real risk: an off-by-one on `scheduler.sigmas` reads the
    level the run has LEFT rather than the one it arrived at — a preview that is
    permanently one step stale and looks perfectly fine.

    Two things make this actually pin it, and both were missing:

    * **The latents MOVE.** With the same array every step the velocity is zero,
      and a zero velocity makes `denoised` return the latent at any sigma
      whatsoever — so the assertion passed just as happily against
      `sigmas[step]`.
    * **The frame examined is not the LAST one.** The schedule ends at sigma 0,
      where the estimate degenerates to the latent again for the same reason. So
      this reads the frame written mid-render, where the two indexings give
      genuinely different pixels — and asserts that it is the one and not the
      other.
    """
    import numpy

    rng = numpy.random.default_rng(7)
    sigmas = [1.0, 0.9, 0.7, 0.318, 0.0]
    steps = [rng.standard_normal((1, 32 * 32, 128)).astype(numpy.float32) * 2.0
             for _ in range(4)]
    pipe = FakePipe(sigmas=sigmas, latents_per_step=steps)
    worker = loaded_worker(monkeypatch, base, pipe)
    # The worker's OWN reading of `preview.py` — a runner reaches it by path, so
    # `fused_render.ai.runners.preview` is a second module object with its own
    # `Sink` class, and patching that one would patch nothing the worker uses.
    preview = worker.preview
    request = _request(tmp_path)
    # Snapshot each frame as it lands: the clean exit removes the file, and the
    # frame that discriminates is mid-render anyway.
    shots = []
    pipe.watch = lambda step: shots.append(
        _frame_pixels(request["outPreview"])
        if os.path.exists(request["outPreview"]) else None)
    worker.generate(request)

    # After step index 2 the pair is (latents[1] at sigmas[2], latents[2] at
    # sigmas[3]) — the level the scheduler has ARRIVED at.
    right = _expected_frame(preview, steps[1], steps[2], sigmas[2], sigmas[3])
    # …and this is what reading `sigmas[step]` instead would have produced: the
    # same two latents, paired with the two levels one entry earlier.
    stale = _expected_frame(preview, steps[1], steps[2], sigmas[1], sigmas[2])
    assert right != stale, "the schedule chosen cannot tell the two apart"

    size, pixels = shots[2]
    assert size == (preview.MAX_SIDE, preview.MAX_SIDE)
    assert pixels == right
    assert pixels != stale


def _order_spies(monkeypatch, worker, base, events):
    """Record every frame write and every progress tick, in the order they happen."""
    real_write = worker.preview.Sink._write

    def spy_write(self, rgb, grid):
        events.append("frame")
        return real_write(self, rgb, grid)

    real_report = base.report_or_cancel

    def spy_report(job=None, **fields):
        events.append("tick %d" % fields["done"])
        return real_report(job=job, **fields)

    monkeypatch.setattr(worker.preview.Sink, "_write", spy_write)
    monkeypatch.setattr(base, "report_or_cancel", spy_report)


def test_the_FRAME_is_written_BEFORE_the_tick_that_announces_it(monkeypatch, base,
                                                                tmp_path):
    """The order is load-bearing and reads as arbitrary, so it is pinned rather
    than left to be tidied up later.

    `done` on the tick is exactly what `runtime.js` turns into the cache-busted
    `&step=N` preview URL. Report first and a page can be handed step N's URL
    before step N's PNG exists — `watchJob` polls about every 700ms and the
    write is about 68ms, so the window is real. The first frame 404s, which is
    survivable; the nastier half is that the URL is keyed by the step and will
    never be requested again, so a fetch landing in that window caches the
    PREVIOUS frame's bytes under step N's URL and that step shows a stale
    picture for its whole duration.

    Step 1 has no frame at all — a velocity needs two latents — which is the
    documented early-404 and not this bug.
    """
    events = []
    worker = loaded_worker(monkeypatch, base, FakePipe())
    _order_spies(monkeypatch, worker, base, events)
    worker.generate(_request(tmp_path))
    assert events == ["tick 1", "frame", "tick 2", "frame", "tick 3",
                      "frame", "tick 4"]


def test_a_cancel_is_still_honoured_on_the_tick_it_arrives_on(monkeypatch, base,
                                                             tmp_path):
    """The cost of writing the frame first: the ✕ is learned one frame-write
    later than it was. What must NOT change is which tick honours it — the
    reply to the report is the only channel a cancel has, so the raise still
    happens on that same callback and never a step later. A cancelled render
    writing one extra frame is free; it is discarded on the way out."""
    worker = loaded_worker(monkeypatch, base, FakePipe())
    base.cancel_on_tick = 3          # the opening report, then two steps
    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))
    # Two `report_or_cancel` ticks reached, i.e. it unwound inside the second
    # step's callback rather than carrying on into a third.
    assert [tick["done"] for tick in base.ticks] == [0, 1, 2]
    assert os.listdir(tmp_path) == []


def test_a_VAE_with_no_fitted_projection_renders_exactly_as_BEFORE(
        monkeypatch, base, tmp_path):
    """No file, no branch — and no device sync either: the latents are handed
    over as a closure precisely so a no-op sink never touches them."""
    def touched():
        raise AssertionError("a no-op preview pulled the latents off the device")

    pipe = FakePipe(vae_class="AutoencoderKL", on_read=touched)
    worker = loaded_worker(monkeypatch, base, pipe)
    worker.generate(_request(tmp_path))
    assert os.listdir(tmp_path) == ["fox.png"]


def test_a_request_that_named_no_preview_file_renders_exactly_as_BEFORE(
        monkeypatch, base, tmp_path):
    """A worker request from before this feature carries no `outPreview`."""
    def touched():
        raise AssertionError("a no-op preview pulled the latents off the device")

    pipe = FakePipe(on_read=touched)
    worker = loaded_worker(monkeypatch, base, pipe)
    request = _request(tmp_path)
    del request["outPreview"]
    worker.generate(request)
    assert os.listdir(tmp_path) == ["fox.png"]


def test_the_thumbnail_is_removed_when_the_render_is_CANCELLED(
        monkeypatch, base, tmp_path):
    """A ✕ means the user does not want this picture, at any resolution."""
    pipe = FakePipe()
    worker = loaded_worker(monkeypatch, base, pipe)
    base.cancel_on_tick = 4          # the opening report, then three steps
    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path))
    assert os.listdir(tmp_path) == []


def test_a_pipeline_with_NO_sigma_schedule_still_RENDERS(monkeypatch, base, tmp_path):
    """A preview must never be able to raise out of a denoising callback and
    lose a render that was going to succeed."""
    pipe = FakePipe()
    pipe.scheduler = None
    worker = loaded_worker(monkeypatch, base, pipe)
    worker.generate(_request(tmp_path))
    assert os.listdir(tmp_path) == ["fox.png"]


# -- the NF4 text encoder, and the third-party quantizer registration ------------
#
# `_GGUF_RECIPES`' `quantize_4bit` halves what the klein recipe holds (measured
# on an RX 9060 XT: `memory_allocated` 10.15GiB -> 5.10GiB at 512x512 / 4 steps,
# with the warm render unchanged, 5.72s -> 5.76s). These pin the CONFIG rather
# than the effect, since nothing here loads a real model.


class _NoMonkey:
    """`load_worker` wants a monkeypatch; these two calls need no patching."""

    def setitem(self, *a):
        pass

    def syspath_prepend(self, *a):
        pass


def test_a_recipe_that_names_no_components_asks_for_no_quantization():
    """`None` is the parameter's own default, so a recipe without the key
    reaches exactly the `from_pretrained` call it reached before this
    existed — including the empty-list case, which is a recipe that has been
    edited down to nothing rather than one that never asked."""
    worker = load_worker(_NoMonkey(), FakeBase())
    assert worker._load_quantization(None) is None
    assert worker._load_quantization({}) is None
    assert worker._load_quantization({"quantize_4bit": []}) is None


def test_the_klein_recipe_quantizes_its_TEXT_ENCODER_and_nothing_else(
        monkeypatch, base):
    """The transformer is already Q4_K_M and is passed to `from_pretrained` as a
    BUILT object, so asking diffusers to quantize it too would be asking it to
    re-quantize GGUF weights. The text encoder is the 7.5GB bf16 component that
    makes this worth doing — ~70% of what the worker would otherwise hold."""
    torch = fake_torch()
    torch.bfloat16 = "bfloat16"
    seen = {}

    class FakeQuantConfig:
        def __init__(self, quant_backend=None, quant_kwargs=None,
                     components_to_quantize=None):
            seen.update({"backend": quant_backend, "kwargs": quant_kwargs,
                         "components": components_to_quantize})

    diffusers = types.ModuleType("diffusers")
    diffusers.PipelineQuantizationConfig = FakeQuantConfig
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)

    worker = load_worker(monkeypatch, base)
    recipe = worker._recipe(MODEL)
    assert recipe["quantize_4bit"] == ["text_encoder"]
    assert worker._load_quantization(recipe) is not None

    assert seen["components"] == ["text_encoder"]
    assert seen["backend"] == "bitsandbytes_4bit"
    # NF4 rather than bitsandbytes' fp4 default, double quant on, and a compute
    # dtype matching the pipeline's `torch_dtype` — that last one defaults to
    # float32, which would dequantize every matmul into the wrong dtype.
    assert seen["kwargs"]["load_in_4bit"] is True
    assert seen["kwargs"]["bnb_4bit_quant_type"] == "nf4"
    assert seen["kwargs"]["bnb_4bit_use_double_quant"] is True
    assert seen["kwargs"]["bnb_4bit_compute_dtype"] == torch.bfloat16


def test_a_missing_sdnq_does_not_stop_an_ordinary_model_loading(monkeypatch, base):
    """`_register_extra_quantizers` runs on EVERY load, including the many with
    nothing to do with sdnq, so an absent optional backend has to be a no-op.
    A model that genuinely needs it still fails loudly — diffusers raises on
    the `quant_method` it cannot resolve, naming it — so this swallow hides
    nothing, it just lets the other models through."""
    monkeypatch.setitem(sys.modules, "torch", fake_torch())
    worker = load_worker(monkeypatch, base)
    monkeypatch.setitem(sys.modules, "sdnq", None)   # a None entry raises on import
    worker._register_extra_quantizers()


def test_a_missing_sdnq_still_writes_why_it_did_not_register(monkeypatch, base, capsys):
    """The swallow above must not be a black hole: a load that genuinely
    needed sdnq fails later with diffusers' own `Unknown quantization type,
    got sdnq` ValueError, which names what is missing but not why it never
    registered. `_register_extra_quantizers` has to leave that reason
    somewhere a person debugging the later failure can find it."""
    monkeypatch.setitem(sys.modules, "torch", fake_torch())
    worker = load_worker(monkeypatch, base)
    monkeypatch.setitem(sys.modules, "sdnq", None)   # a None entry raises on import
    worker._register_extra_quantizers()
    err = capsys.readouterr().err
    assert "sdnq" in err
    assert "did not register" in err
