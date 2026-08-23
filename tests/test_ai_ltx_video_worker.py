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
import fnmatch
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
import types

import pytest

from fused_render.ai.runners import formats

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "ltx_video", "worker.py",
)

MODEL = "dgrauet/ltx-2.3-mlx-q4"
GEMMA = "mlx-community/gemma-3-12b-it-4bit"

#: The real `dgrauet/ltx-2.3-mlx-q4` file listing (Hub API, 2026-08-23) —
#: used as the default fake Hub listing so `download` tests exercise the
#: actual ambiguity (`transformer-distilled.safetensors` AND `transformer-
#: distilled-1.1.safetensors` both present) rather than a listing shaped to
#: make the test easy.
REAL_Q4_LISTING = [
    ".gitattributes", "LICENSE", "README.md", "audio_vae.safetensors",
    "config.json", "connector.safetensors", "embedded_config.json",
    "ltx-2.3-22b-distilled-lora-384-1.1.safetensors",
    "ltx-2.3-22b-distilled-lora-384.safetensors", "quantize_config.json",
    "spatial_upscaler_x1_5_v1_0.safetensors", "spatial_upscaler_x1_5_v1_0_config.json",
    "spatial_upscaler_x2_v1_1.safetensors", "spatial_upscaler_x2_v1_1_config.json",
    "split_model.json", "temporal_upscaler_x2_v1_0.safetensors",
    "temporal_upscaler_x2_v1_0_config.json", "transformer-dev.safetensors",
    "transformer-distilled-1.1.safetensors", "transformer-distilled.safetensors",
    "vae_decoder.safetensors", "vae_encoder.safetensors", "vocoder.safetensors",
]


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
    """`ltx_pipelines_mlx.distilled.DistilledPipeline`, from the outside.

    `drive_denoise_loop`, when set, makes `generate_and_save` call `tqdm(...)`
    off the SAME `ltx_pipelines_mlx.utils.samplers` module the worker patches
    — reproducing the two calls `DistilledPipeline.generate_two_stage` really
    makes (stage 1: `stage1_steps` items, stage 2: a fixed-length refine) —
    so a test can drive the worker's shim through its real call shape rather
    than calling the shim directly.
    """

    def __init__(self, model_dir=None, gemma_model_id=None, **kwargs):
        self.model_dir = model_dir
        self.gemma_model_id = gemma_model_id
        self.kwargs = kwargs
        self.calls = []
        self.drive_denoise_loop = False
        self.stage2_steps = 3

    def generate_and_save(self, **kwargs):
        self.calls.append(kwargs)
        if self.drive_denoise_loop:
            import sys as _sys

            samplers = _sys.modules["ltx_pipelines_mlx.utils.samplers"]
            for _ in samplers.tqdm(range(kwargs["stage1_steps"]), desc="Denoising"):
                pass
            for _ in samplers.tqdm(range(self.stage2_steps), desc="Denoising (stage 2)"):
                pass
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


class FakeHfHub(types.ModuleType):
    """`huggingface_hub`, from the outside — only `list_repo_files` here.

    `worker_base` itself carries `download_snapshot` in the fake used
    everywhere else in this file; this is the SECOND, direct import
    `download` makes to read a repo's listing before choosing patterns.
    """

    def __init__(self, listing=REAL_Q4_LISTING, on_list=None):
        super().__init__("huggingface_hub")
        self.listing = listing
        self.on_list = on_list

    def list_repo_files(self, model_id):
        if self.on_list is not None:
            self.on_list(model_id)
        return list(self.listing)


def _fake_ffmpeg_binary():
    """A real, executable file named the way the REAL `imageio-ffmpeg` wheel
    actually names its binary — NOT `ffmpeg`.

    MEASURED against the installed 0.6.0 wheel on this machine (2026-08-23,
    `uv pip install --target . imageio-ffmpeg` into a scratch directory and
    listed): `imageio_ffmpeg/binaries/` holds exactly one file,
    `ffmpeg-macos-aarch64-v7.1`, and nothing named `ffmpeg` at all. A fake
    that returned a path already named `ffmpeg` (this file's previous
    version) could not have caught `_put_ffmpeg_on_path` merely prepending
    the binary's own directory to PATH — `shutil.which("ffmpeg")` would
    never have found anything there on a real machine, only in the test.
    Named with the same platform-and-version-qualified shape here so the
    premise cannot drift back silently.
    """
    fd, path = tempfile.mkstemp(prefix="ffmpeg-macos-aarch64-v", suffix=".1")
    os.close(fd)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def load_worker(monkeypatch, base, pipeline=None, with_ffmpeg=True,
                with_samplers_tqdm=True, hf_hub=None, ffmpeg_exe=None):
    """A fresh import of the ltx_video worker against the fakes.

    `monkeypatch.setitem` rather than a save/restore, because this runner
    imports every third-party name INSIDE the functions that need it — a stub
    withdrawn after the import would be gone by the time anything looked for
    it (the same reason `test_ai_mflux_worker.py`'s loader does this).

    `ffmpeg_exe`, when given, is the path `imageio_ffmpeg.get_ffmpeg_exe()`
    fakes returning — a REAL file, so `_put_ffmpeg_on_path`'s symlink/copy
    has something to point at and `shutil.which` can actually resolve it
    afterward (see `_fake_ffmpeg_binary` and `test_load_makes_ffmpeg_
    resolvable_via_PATH`, which is the test that would catch this fake
    drifting away from the real wheel's shape again).
    """
    made = pipeline if pipeline is not None else FakePipeline()

    monkeypatch.setitem(sys.modules, "worker_base", base)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hf_hub or FakeHfHub())

    distilled_mod = types.ModuleType("ltx_pipelines_mlx.distilled")

    def _make_pipeline(model_dir=None, gemma_model_id=None, **kwargs):
        made.model_dir = model_dir
        made.gemma_model_id = gemma_model_id
        made.kwargs = kwargs
        return made

    distilled_mod.DistilledPipeline = _make_pipeline
    monkeypatch.setitem(sys.modules, "ltx_pipelines_mlx", types.ModuleType("ltx_pipelines_mlx"))
    monkeypatch.setitem(sys.modules, "ltx_pipelines_mlx.distilled", distilled_mod)
    monkeypatch.setitem(sys.modules, "ltx_pipelines_mlx.utils", types.ModuleType("ltx_pipelines_mlx.utils"))
    samplers_mod = types.ModuleType("ltx_pipelines_mlx.utils.samplers")
    if with_samplers_tqdm:
        # A plain callable stand-in for `from tqdm import tqdm` — the real
        # thing is a class, but nothing here relies on that, only on the
        # module carrying SOME attribute named `tqdm` for the worker to
        # replace.
        samplers_mod.tqdm = lambda iterable, **kwargs: iter(iterable)
    monkeypatch.setitem(sys.modules, "ltx_pipelines_mlx.utils.samplers", samplers_mod)

    mlx = types.ModuleType("mlx")
    mlx_core = FakeMlxCore()
    mlx.core = mlx_core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)

    if with_ffmpeg:
        ffmpeg_mod = types.ModuleType("imageio_ffmpeg")
        exe = ffmpeg_exe if ffmpeg_exe is not None else _fake_ffmpeg_binary()
        ffmpeg_mod.get_ffmpeg_exe = lambda: exe
        monkeypatch.setitem(sys.modules, "imageio_ffmpeg", ffmpeg_mod)

    spec = importlib.util.spec_from_file_location("ltx_video_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, made


def snapshot(tmp_path, *, distilled=True, versioned=False, name="snap"):
    """A downloaded weights snapshot directory, as `download` would leave it.

    Includes `split_model.json` — `_FIXED_FILES` names it precisely so a real
    download carries it, and a fixture that silently dropped it would agree
    with nothing that actually exercises `formats.has_ltx_split_layout`
    (see `test_the_downloaded_file_set_is_recognised_by_loaders`, which
    crosses that seam directly against the real allow_patterns output
    instead of trusting this fixture's idea of the tree).
    """
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    for filename in ("config.json", "connector.safetensors", "vae_encoder.safetensors",
                     "vae_decoder.safetensors", "audio_vae.safetensors",
                     "vocoder.safetensors", "spatial_upscaler_x2_v1_1.safetensors",
                     "split_model.json"):
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
    """Against the REAL `dgrauet/ltx-2.3-mlx-q4` listing, which carries the
    exact ambiguity this worker has to resolve: two distilled-transformer
    names side by side, a dev transformer, two LoRAs, and a temporal/x1.5
    upscaler pair this build never opens."""
    worker, _ = load_worker(monkeypatch, base)

    fetched = worker.download(MODEL)

    assert fetched == f"/snapshots/{MODEL.replace('/', '_')}"
    ids = [model_id for model_id, _kwargs in base.downloads]
    assert MODEL in ids
    patterns = dict(base.downloads[ids.index(MODEL)][1])["allow_patterns"]
    # Exactly ONE transformer file — the versioned one `_resolve_safetensors`
    # actually prefers — never both, which is the bug a bare glob had.
    assert "transformer-distilled-1.1.safetensors" in patterns
    assert "transformer-distilled.safetensors" not in patterns
    assert "transformer-dev.safetensors" not in patterns
    assert not any("lora" in p.lower() for p in patterns)
    assert not any("temporal" in p.lower() for p in patterns)
    assert not any("x1_5" in p for p in patterns)
    # The one file `DistilledPipeline` never opens but `formats.has_ltx_
    # split_layout` requires — see `test_the_downloaded_file_set_is_
    # recognised_by_loaders` for why this line is load-bearing rather than
    # decorative.
    assert formats.LTX_SPLIT_MANIFEST in patterns


def test_the_downloaded_file_set_is_recognised_by_loaders(monkeypatch, base):
    """Crosses the seam between `download`'s real `allow_patterns` and
    `formats.has_ltx_split_layout`'s predicate — the seam this file's own
    `snapshot()` fixture and `test_ai_formats.py`'s synthetic file sets each
    modelled independently, and neither ever tested against the other. A
    fixture that agreed with `download` but not with the real predicate (or
    vice versa) could pass every other test in both files while a genuine
    `dgrauet/ltx-2.3-mlx-q4` download still got tagged `mlx-text` on the AI
    Models page.

    So this computes the REAL patterns `download` would request against the
    REAL Hub listing, filters that listing exactly as `huggingface_hub`'s
    `allow_patterns` fnmatch would (`download_snapshot`'s own contract), and
    feeds the result — the file set a real download actually leaves on
    disk — straight into the real `formats.loaders()`.
    """
    worker, _ = load_worker(monkeypatch, base)

    worker.download(MODEL)

    ids = [model_id for model_id, _kwargs in base.downloads]
    patterns = dict(base.downloads[ids.index(MODEL)][1])["allow_patterns"]
    downloaded = {name for name in REAL_Q4_LISTING
                 if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)}

    assert formats.has_ltx_split_layout(downloaded), downloaded
    codes = formats.loaders(
        repo_id=MODEL, names=downloaded, dirnames=set(), config={},
        torch_weights=True)
    assert set(codes) == {"ltx-video"}, (
        f"a real download of {MODEL} would be tagged {set(codes)!r}, not "
        f"ltx-video — the on-disk file set formats.py checks for and the "
        f"one download.py actually fetches have drifted apart")
    # The one upscaler file this repo actually ships, plus its config.
    assert "spatial_upscaler_x2_v1_1.safetensors" in patterns
    assert "spatial_upscaler_x2_v1_1_config.json" in patterns


def test_download_fetches_a_single_unversioned_transformer_when_that_is_all_there_is(
        monkeypatch, base):
    """No versioned file at all — the plain name must still be requested;
    `_resolve_versioned_name`'s fallback, exercised against a listing with
    only one candidate."""
    listing = [n for n in REAL_Q4_LISTING if "transformer-distilled-1.1" not in n]
    worker, _ = load_worker(monkeypatch, base, hf_hub=FakeHfHub(listing=listing))

    worker.download(MODEL)

    patterns = dict(base.downloads[0][1])["allow_patterns"]
    assert "transformer-distilled.safetensors" in patterns


def test_download_refuses_a_repo_with_no_distilled_transformer_at_all(monkeypatch, base):
    listing = [n for n in REAL_Q4_LISTING if "transformer-distilled" not in n]
    worker, _ = load_worker(monkeypatch, base, hf_hub=FakeHfHub(listing=listing))

    with pytest.raises(RuntimeError, match="transformer-distilled"):
        worker.download(MODEL)

    # Refused BEFORE either repo is fetched — a listing lookup is cheap, and
    # spending a user's bandwidth on a repo this runner cannot load anyway
    # would be the download equivalent of the worse mistake.
    assert base.downloads == []


def test_download_reports_a_hub_listing_failure_with_the_model_id(monkeypatch, base):
    class FailingHub(FakeHfHub):
        def list_repo_files(self, model_id):
            raise RuntimeError("network is down")

    worker, _ = load_worker(monkeypatch, base, hf_hub=FailingHub())

    with pytest.raises(RuntimeError, match="network is down"):
        worker.download(MODEL)


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


def test_load_makes_ffmpeg_resolvable_via_PATH(monkeypatch, base, tmp_path):
    """`ltx_core_mlx.utils.ffmpeg.find_ffmpeg()` is a bare `shutil.which
    ("ffmpeg")` with no environment-variable override (unlike h3.c's own
    `H3_FFMPEG` convention) — the only lever this process has is PATH, and
    `shutil.which` matches on the exact basename.

    Asserted through `shutil.which("ffmpeg")` itself — the actual call `find_
    ffmpeg()` makes — rather than by inspecting which directory landed on
    PATH: a fake `get_ffmpeg_exe()` that already returned something named
    `ffmpeg` (this test's previous version) could pass with an
    implementation that only prepended the binary's own directory, which
    does NOT work against the real wheel (see `_fake_ffmpeg_binary` and
    `_put_ffmpeg_on_path`'s docstring for the measurement). `_fake_ffmpeg_
    binary()` is named the way the real one is — NOT `ffmpeg` — so this can
    only pass if `load()` actually made a same-named link resolvable.
    """
    monkeypatch.delenv("PATH", raising=False)
    exe = _fake_ffmpeg_binary()
    worker, _ = load_worker(monkeypatch, base, ffmpeg_exe=exe)
    fetched = snapshot(tmp_path)

    worker.load(MODEL, fetched)

    resolved = shutil.which("ffmpeg")
    assert resolved is not None, os.environ.get("PATH")
    if sys.platform == "win32":
        # Copied there, not symlinked (`_put_ffmpeg_on_path`'s own docstring
        # says why) — same bytes, different path.
        with open(resolved, "rb") as a, open(exe, "rb") as b:
            assert a.read() == b.read()
    else:
        assert os.path.realpath(resolved) == os.path.realpath(exe)


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


# -- Task 4: per-step progress and cancellation via the tqdm shim -----------------


def test_the_denoise_module_still_exposes_a_module_level_tqdm(monkeypatch, base, tmp_path):
    """Assert the PREMISE, not only the conclusion: this worker reports
    per-step progress and cancellation by replacing `ltx_pipelines_mlx.utils.
    samplers`'s module-level `tqdm` name for the duration of a render. If
    upstream ever renames or removes that import, silently patching a name
    that no longer does anything would report NOTHING and cancel NOTHING,
    with every other test in this file still green (they all fake the
    attribute into existence). A render must fail loudly instead."""
    pipeline = FakePipeline()
    pipeline.drive_denoise_loop = True
    worker, _ = load_worker(monkeypatch, base, pipeline=pipeline,
                            with_samplers_tqdm=False)
    worker.load(MODEL, snapshot(tmp_path))

    with pytest.raises(RuntimeError, match="tqdm"):
        worker.generate(_request(tmp_path))


def test_ticks_arrive_per_denoising_step(monkeypatch, base, tmp_path):
    pipeline = FakePipeline()
    pipeline.drive_denoise_loop = True
    worker, _ = load_worker(monkeypatch, base, pipeline=pipeline)
    worker.load(MODEL, snapshot(tmp_path))

    worker.generate(_request(tmp_path, steps=8))

    # 8 stage-1 ticks + 3 stage-2 ticks, on top of the pre-render 0/8 tick
    # `generate` itself already sends (see `test_a_render_reports_and_returns
    # _its_path`) — so at least 8 additional "task" ticks with `total`
    # matching the stage they came from.
    # `state="running"` marks `generate`'s own pre-render tick (sent via
    # `report`, not `report_or_cancel`) — excluded here since it is not one
    # of the shim's per-step ticks even though it shares `kind="task"`.
    task_ticks = [t for t in base.ticks if t.get("kind") == "task" and "state" not in t]
    stage1_ticks = [t for t in task_ticks if t.get("total") == 8]
    stage2_ticks = [t for t in task_ticks if t.get("total") == 3]
    assert len(stage1_ticks) >= 8, task_ticks
    assert len(stage2_ticks) >= 3, task_ticks
    # Monotonic within a stage, and restarting at the next one (the two-stage
    # pipeline's own shape — see `_StepTicker`'s docstring).
    assert [t["done"] for t in stage1_ticks] == list(range(8))
    assert [t["done"] for t in stage2_ticks] == list(range(3))


def test_a_cancel_between_denoising_steps_ends_the_render(monkeypatch, base, tmp_path):
    class CancellingBase(FakeBase):
        def report_or_cancel(self, job=None, **fields):
            self.ticks.append({"job": job, **fields})
            if fields.get("done") == 3:
                raise self.Cancelled()

    cancelling = CancellingBase()
    pipeline = FakePipeline()
    pipeline.drive_denoise_loop = True
    worker, _ = load_worker(monkeypatch, cancelling, pipeline=pipeline)
    worker.load(MODEL, snapshot(tmp_path))

    with pytest.raises(cancelling.Cancelled):
        worker.generate(_request(tmp_path, out=str(tmp_path / "cancelled.mp4")))

    # The render never reached `generate_and_save`'s own "write the file" tail
    # for THIS call — the raise unwound out of the denoise loop, straight
    # through `generate_two_stage`, exactly as `worker_base.Cancelled` does
    # for every other runner's callback.
    assert not os.path.exists(tmp_path / "cancelled.mp4")


def test_the_tqdm_patch_is_restored_after_a_render(monkeypatch, base, tmp_path):
    """The shim is process-wide state on a third-party module — it must not
    leak past the request it was installed for, or a second render (or a
    failed one) would be instrumented by a stale reporter bound to a job
    that has already ended."""
    pipeline = FakePipeline()
    pipeline.drive_denoise_loop = True
    worker, _ = load_worker(monkeypatch, base, pipeline=pipeline)
    worker.load(MODEL, snapshot(tmp_path))
    samplers = sys.modules["ltx_pipelines_mlx.utils.samplers"]
    original = samplers.tqdm

    worker.generate(_request(tmp_path))

    assert samplers.tqdm is original


def test_the_tqdm_patch_is_restored_even_after_a_cancel(monkeypatch, base, tmp_path):
    class CancellingBase(FakeBase):
        def report_or_cancel(self, job=None, **fields):
            self.ticks.append({"job": job, **fields})
            if fields.get("done") == 1:
                raise self.Cancelled()

    cancelling = CancellingBase()
    pipeline = FakePipeline()
    pipeline.drive_denoise_loop = True
    worker, _ = load_worker(monkeypatch, cancelling, pipeline=pipeline)
    worker.load(MODEL, snapshot(tmp_path))
    samplers = sys.modules["ltx_pipelines_mlx.utils.samplers"]
    original = samplers.tqdm

    with pytest.raises(cancelling.Cancelled):
        worker.generate(_request(tmp_path))

    assert samplers.tqdm is original
