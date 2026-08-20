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
#: was never an option).
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
        pass

    def serve(self, **kwargs):
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
