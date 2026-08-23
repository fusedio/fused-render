"""The ltx_video runner's own logic, driven directly (SPEC §40).

Same shape `tests/test_ai_mflux_worker.py` uses: the module is stdlib-only at
import time (`ltx_pipelines_mlx`, `mlx.core` and `imageio_ffmpeg` are all
imported inside the functions that need them), so the whole flow runs against
fakes injected into `sys.modules` — no Metal, no weights, no real ffmpeg. What
is pinned here is the CONTRACT: which repos `download` asks for and with what
patterns, what `load` refuses and why, and that `generate` drives
`DistilledPipeline.generate_and_save` with this worker's defaults and returns
the same reply shape `h3_video.generate` does.
"""
import importlib.util
import os
import sys
import types

import pytest

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "ltx_video", "worker.py",
)

MODEL = "dgrauet/ltx-2.3-mlx-q4"
GEMMA = "mlx-community/gemma-3-12b-it-4bit"


class FakeBase:
    """A stand-in for `worker_base`, recording every tick and every download."""

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.ticks = []
        self.state = {}
        #: One entry per `download_snapshot` call: (model_id, kwargs).
        self.downloads = []

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def set_state(self, **fields):
        self.state.update(fields)

    def download_snapshot(self, model_id, **kwargs):
        self.downloads.append((model_id, kwargs))
        return f"/snapshots/{model_id.replace('/', '_')}"

    def serve(self, **kwargs):
        return None


class FakePipeline:
    """`ltx_pipelines_mlx.distilled.DistilledPipeline`, from the outside."""

    def __init__(self, model_dir=None, gemma_model_id=None, **kwargs):
        self.model_dir = model_dir
        self.gemma_model_id = gemma_model_id
        self.kwargs = kwargs
        self.calls = []

    def generate_and_save(self, **kwargs):
        self.calls.append(kwargs)
        # A real call writes the mp4 at `output_path` — modelled so `generate`'s
        # own `os.makedirs` and the file's presence are both exercised.
        with open(kwargs["output_path"], "wb") as handle:
            handle.write(b"MP4")
        return kwargs["output_path"]


class FakeMlxCore(types.ModuleType):
    def __init__(self):
        super().__init__("mlx.core")

    def get_active_memory(self):
        return 0


def load_worker(monkeypatch, base, pipeline=None, with_ffmpeg=True):
    """A fresh import of the ltx_video worker against the fakes.

    `monkeypatch.setitem` rather than a save/restore, because this runner
    imports every third-party name INSIDE the functions that need it — a stub
    withdrawn after the import would be gone by the time anything looked for
    it (the same reason `test_ai_mflux_worker.py`'s loader does this).
    """
    made = pipeline if pipeline is not None else FakePipeline()

    monkeypatch.setitem(sys.modules, "worker_base", base)

    distilled_mod = types.ModuleType("ltx_pipelines_mlx.distilled")

    def _make_pipeline(model_dir=None, gemma_model_id=None, **kwargs):
        made.model_dir = model_dir
        made.gemma_model_id = gemma_model_id
        made.kwargs = kwargs
        return made

    distilled_mod.DistilledPipeline = _make_pipeline
    monkeypatch.setitem(sys.modules, "ltx_pipelines_mlx", types.ModuleType("ltx_pipelines_mlx"))
    monkeypatch.setitem(sys.modules, "ltx_pipelines_mlx.distilled", distilled_mod)

    mlx = types.ModuleType("mlx")
    mlx_core = FakeMlxCore()
    mlx.core = mlx_core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)

    if with_ffmpeg:
        ffmpeg_mod = types.ModuleType("imageio_ffmpeg")
        ffmpeg_dir = os.path.join(os.sep, "fake", "ffmpeg", "bin")
        ffmpeg_mod.get_ffmpeg_exe = lambda: os.path.join(ffmpeg_dir, "ffmpeg")
        monkeypatch.setitem(sys.modules, "imageio_ffmpeg", ffmpeg_mod)

    spec = importlib.util.spec_from_file_location("ltx_video_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, made


def snapshot(tmp_path, *, distilled=True, versioned=False, name="snap"):
    """A downloaded weights snapshot directory, as `download` would leave it."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for filename in ("config.json", "connector.safetensors", "vae_encoder.safetensors",
                     "vae_decoder.safetensors", "audio_vae.safetensors",
                     "vocoder.safetensors", "spatial_upscaler_x2_v1_1.safetensors"):
        (root / filename).write_bytes(b"")
    if distilled:
        name_ = "transformer-distilled-1.1.safetensors" if versioned else "transformer-distilled.safetensors"
        (root / name_).write_bytes(b"")
    return str(root)


@pytest.fixture()
def base():
    return FakeBase()


def _request(tmp_path, **over):
    return {"prompt": "a lighthouse in a storm", "out": str(tmp_path / "clip.mp4"),
            "job": "sys:ai-video:abc", **over}


# -- download: both repos, the right pattern set ---------------------------------


def test_download_fetches_the_weights_repo_with_the_curated_pattern_set(monkeypatch, base):
    worker, _ = load_worker(monkeypatch, base)

    fetched = worker.download(MODEL)

    assert fetched == f"/snapshots/{MODEL.replace('/', '_')}"
    ids = [model_id for model_id, _kwargs in base.downloads]
    assert MODEL in ids
    weights_kwargs = dict(base.downloads[ids.index(MODEL)][1])
    assert weights_kwargs["allow_patterns"] == worker._ALLOW_PATTERNS
    # The patterns cover exactly what `DistilledPipeline` opens — this is the
    # premise the module docstring documents deriving, pinned so a future
    # edit to the list is a deliberate one.
    assert "transformer-dev.safetensors" not in worker._ALLOW_PATTERNS
    assert not any("lora" in p.lower() for p in worker._ALLOW_PATTERNS)
    assert not any("temporal" in p.lower() for p in worker._ALLOW_PATTERNS)
    assert not any("x1_5" in p for p in worker._ALLOW_PATTERNS)


def test_download_also_fetches_the_gemma_text_encoder_UNPATTERNED(monkeypatch, base):
    """The whole repo, deliberately: unlike the weights repo, nothing here is
    being excluded — `mlx-community/gemma-3-12b-it-4bit` is already a 4-bit
    conversion with nothing to skip, the same argument `mflux_image/worker.py`
    makes about its own single-snapshot download."""
    worker, _ = load_worker(monkeypatch, base)

    worker.download(MODEL)

    ids = [model_id for model_id, _kwargs in base.downloads]
    assert GEMMA in ids
    assert base.downloads[ids.index(GEMMA)][1] == {}


def test_download_is_not_best_effort_on_a_gemma_failure(monkeypatch, base):
    """Unlike `mlx_whisper/worker.py`'s VAD prefetch, a failed Gemma download
    must fail the WHOLE download — this pipeline cannot encode a prompt
    without it, so a silently-absent encoder is not a smaller version of a
    working install."""
    class FailingBase(FakeBase):
        def download_snapshot(self, model_id, **kwargs):
            if model_id == GEMMA:
                raise RuntimeError("boom")
            return super().download_snapshot(model_id, **kwargs)

    failing = FailingBase()
    worker, _ = load_worker(monkeypatch, failing)

    with pytest.raises(RuntimeError, match="boom"):
        worker.download(MODEL)


# -- load: refuses a snapshot with no distilled transformer -----------------------


def test_load_refuses_a_snapshot_with_no_distilled_transformer(monkeypatch, base, tmp_path):
    worker, _ = load_worker(monkeypatch, base)
    fetched = snapshot(tmp_path, distilled=False)

    with pytest.raises(RuntimeError, match="transformer-distilled"):
        worker.load(MODEL, fetched)


def test_load_accepts_the_unversioned_distilled_transformer(monkeypatch, base, tmp_path):
    worker, made = load_worker(monkeypatch, base)
    fetched = snapshot(tmp_path, distilled=True, versioned=False)

    worker.load(MODEL, fetched)  # must not raise

    assert made.model_dir == fetched
    assert made.gemma_model_id == GEMMA


def test_load_accepts_the_versioned_distilled_transformer(monkeypatch, base, tmp_path):
    """`_resolve_safetensors` upstream prefers `transformer-distilled-1.1.
    safetensors` over the plain name when both exist — the refusal check must
    not require the unversioned file to be present too."""
    worker, made = load_worker(monkeypatch, base)
    fetched = snapshot(tmp_path, distilled=True, versioned=True)

    worker.load(MODEL, fetched)  # must not raise

    assert made.model_dir == fetched


def test_load_puts_the_bundled_ffmpeg_directory_on_PATH(monkeypatch, base, tmp_path):
    """`ltx_core_mlx.utils.ffmpeg.find_ffmpeg()` is a bare `shutil.which
    ("ffmpeg")` with no environment-variable override (unlike h3.c's own
    `H3_FFMPEG` convention) — the only lever this process has is PATH."""
    monkeypatch.delenv("PATH", raising=False)
    worker, _ = load_worker(monkeypatch, base)
    fetched = snapshot(tmp_path)

    worker.load(MODEL, fetched)

    ffmpeg_dir = os.path.join(os.sep, "fake", "ffmpeg", "bin")
    assert ffmpeg_dir in os.environ["PATH"].split(os.pathsep)


def test_load_sets_the_device_state(monkeypatch, base, tmp_path):
    worker, _ = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    assert base.state["device"] == "mps"


# -- generate: the shared reply shape, and the settled defaults -------------------


def test_a_render_reports_and_returns_its_path(monkeypatch, base, tmp_path):
    worker, made = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    request = _request(tmp_path, width=704, height=480, frames=97, steps=8, seed=7)

    result = worker.generate(request)

    assert set(result) == {"path", "seconds", "seed", "width", "height", "frames", "steps"}
    assert result["path"] == request["out"]
    assert (result["width"], result["height"]) == (704, 480)
    assert result["frames"] == 97 and result["steps"] == 8 and result["seed"] == 7
    assert os.path.exists(request["out"])
    # At least the pre-render tick — Task 4 adds per-step ticks on top of this.
    assert any(t.get("done") == 0 for t in base.ticks)


def test_the_defaults_match_the_registry_traits_table(monkeypatch, base, tmp_path):
    """704x480, 8k+1 frames (97 = 8*12+1), 8 steps — Task 5's traits table for
    `ltx-video`. A caller that omits every field gets exactly what the
    row promises."""
    worker, made = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    worker.generate(_request(tmp_path))

    call = made.calls[0]
    assert (call["width"], call["height"]) == (704, 480)
    assert call["num_frames"] == 97
    assert call["stage1_steps"] == 8
    assert call["frame_rate"] == 24.0


def test_generate_raises_when_nothing_is_loaded(monkeypatch, base, tmp_path):
    worker, _ = load_worker(monkeypatch, base)
    with pytest.raises(RuntimeError, match="no model is loaded"):
        worker.generate(_request(tmp_path))


def test_generate_requires_an_out_path(monkeypatch, base, tmp_path):
    worker, _ = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    with pytest.raises(ValueError, match="out"):
        worker.generate(_request(tmp_path, out=""))
