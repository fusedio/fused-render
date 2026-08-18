"""The mflux image runner's own logic, driven directly (SPEC AI-9a).

`tests/test_ai_mlx_whisper_worker.py` is the template, and the claim being
pinned is the same one: a second runner for a capability must be
indistinguishable from the first through the public API. Here that means the
`/generate` body, the reply dict, the denoising-step row and the ✕ all have to
match `diffusers_image/worker.py`, against a library whose shape is different in
every one of those places.

Testable because the module is **stdlib-only at import time** — `mflux` and
`mlx.core` are imported inside the functions that need them — so the whole flow
runs against stubs. What is NOT covered is any actual rendering: no Metal, no
weights, no pixels. The library contract the stubs encode (a per-model callback
registry, `call_in_loop`, `generate_image`'s keywords, `save(overwrite=)`) was
read off mflux 0.13 on an Apple Silicon machine, and the benchmark behind D310
ran that path for real.
"""
import importlib.util
import os
import sys
import threading
import types

import pytest

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "mflux_image", "worker.py",
)

MODEL = "mlx-community/FLUX.2-Klein-4B-4bit"


class FakeBase:
    """A stand-in for `worker_base`, recording every tick."""

    class Cancelled(Exception):
        pass

    def __init__(self):
        self.ticks = []
        self.CANCEL = _Flag()
        self.state = {}
        #: Set by a test to have the NEXT tick answer "the ✕ was pressed".
        self.cancel_on_tick = None

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_on_tick is not None and len(self.ticks) >= self.cancel_on_tick:
            self.CANCEL.set()

    def set_state(self, **fields):
        self.state.update(fields)

    def download_snapshot(self, model_id, **kwargs):
        self.snapshot_kwargs = kwargs
        return f"/snapshots/{model_id}"

    def serve(self, **kwargs):
        return None


class _Flag:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set


# -- the fakes standing in for mflux --------------------------------------------


class FakeRegistry:
    """`mflux.callbacks.callback_registry.CallbackRegistry`: it APPENDS, which
    is the property that makes where-you-register matter."""

    def __init__(self):
        self.callbacks = []

    def register(self, callback):
        self.callbacks.append(callback)


class FakeImage:
    def __init__(self):
        self.saved = []

    def save(self, path, export_json_metadata=False, overwrite=False):
        self.saved.append({"path": path, "overwrite": overwrite})
        # The real one RESOLVES a colliding path when overwrite is False, i.e.
        # writes somewhere else entirely. Modelled, because a runner that
        # forgets the flag answers with a file the caller was never told about.
        target = path if overwrite or not os.path.exists(path) else path + ".1"
        with open(target, "wb") as handle:
            handle.write(b"PNG")
        self.written = target


class FakeLatents:
    """An mlx array, with the one call the preview's `_as_numpy` makes on it."""

    def __init__(self, array, on_read=None):
        self._array = array
        self._on_read = on_read

    def astype(self, dtype):
        assert dtype == "float32", dtype
        if self._on_read is not None:
            self._on_read()
        return self._array


class FakeModel:
    """An mflux variant: a callback registry, and a loop that drives it."""

    def __init__(self, steps_to_run=None, latents=None, sigmas=None,
                 latents_per_step=None):
        self.callbacks = FakeRegistry()
        self.calls = []
        self.image = FakeImage()
        #: How many steps the fake loop actually runs, when it should differ
        #: from what was asked (nothing does this today; it exists so a cancel
        #: mid-loop is expressible).
        self.steps_to_run = steps_to_run
        #: What the loop hands the callback. None for the tests that are not
        #: about the preview — which is also the honest shape of a run with no
        #: schedule to read, so those tests pin that nothing breaks without one.
        self.latents = latents
        self.sigmas = sigmas
        #: One array per step, when a test needs the latents to actually MOVE.
        #: Identical latents mean zero velocity, and a zero velocity makes the
        #: denoised estimate equal the latent at every sigma — which is how a
        #: test can look like it pins the sigma indexing without pinning it.
        self.latents_per_step = latents_per_step
        #: Called after each step, for a test that watches the preview appear.
        self.watch = None

    def generate_image(self, seed, prompt, num_inference_steps=4, height=1024,
                       width=1024, guidance=1.0, **kwargs):
        self.calls.append({"seed": seed, "prompt": prompt, "guidance": guidance,
                           "num_inference_steps": num_inference_steps,
                           "height": height, "width": width, **kwargs})
        total = self.steps_to_run if self.steps_to_run is not None else num_inference_steps
        config = None
        if self.sigmas is not None:
            config = types.SimpleNamespace(
                scheduler=types.SimpleNamespace(sigmas=self.sigmas))
        for t in range(total):
            held = (self.latents if self.latents_per_step is None
                    else self.latents_per_step[t])
            for callback in self.callbacks.callbacks:
                # mflux calls it with keywords; the reporter's signature has to
                # match, and a positional call here would hide a rename.
                callback.call_in_loop(t=t, seed=seed, prompt=prompt,
                                      latents=held, config=config,
                                      time_steps=None)
            if self.watch is not None:
                self.watch(t)
        return self.image


def make_mflux(model=None):
    """The `mflux` package tree the worker imports, as modules in sys.modules."""
    made = model if model is not None else FakeModel()

    class Flux2Klein:
        def __init__(self, model_config=None, model_path=None, **kwargs):
            made.built = {"model_config": model_config, "model_path": model_path,
                          **kwargs}
            self.__dict__.update(made.__dict__)
            self._real = made
            made.instance = self

        def __getattr__(self, name):
            return getattr(self._real, name)

    variants = types.ModuleType("mflux.models.flux2.variants")
    variants.Flux2Klein = Flux2Klein

    class ModelConfig:
        @staticmethod
        def flux2_klein_4b():
            return "FLUX2_KLEIN_4B_CONFIG"

    config_mod = types.ModuleType("mflux.models.common.config")
    config_mod.ModelConfig = ModelConfig
    return made, variants, config_mod


class FakeMlxCore(types.ModuleType):
    """`mlx.core` as this runner uses it: two DEVICES and their STREAMS.

    The same double `tests/test_ai_mlx_whisper_worker.py` keeps, with the one
    difference that broke this runner: from mlx 0.32 the default stream is per
    (thread, DEVICE), so a worker that pins only `default_device()` — the GPU —
    still aborts on `Stream(cpu, 0)`. Both devices are therefore distinct
    objects here, and every `new_thread_unsafe_stream` records which one it was
    asked for.
    """

    def __init__(self, **extra):
        super().__init__("mlx.core")
        self.float32 = "float32"
        self.cpu = "CPU"
        self.gpu = "GPU"
        #: the device of every `new_thread_unsafe_stream`, in order.
        self.made = []
        #: (thread name, stream) for every `set_default_stream`.
        self.pinned = []
        self._lock = threading.Lock()
        for name, value in extra.items():
            setattr(self, name, value)

    def default_device(self):
        return self.gpu

    def new_thread_unsafe_stream(self, device):
        with self._lock:
            self.made.append(device)
            return f"SHARED-{device}-STREAM"

    def set_default_stream(self, stream):
        with self._lock:
            self.pinned.append((threading.current_thread().name, stream))

    def get_active_memory(self):
        return 0


def load_worker(monkeypatch, base, with_mflux=True, model=None, mlx_core=None):
    """A fresh import of the mflux worker against the fakes.

    `monkeypatch.setitem` rather than a save/restore, because this runner
    imports mflux INSIDE the functions that need it — a stub withdrawn after the
    import would be gone by the time anything looked for it.
    """
    made = None
    monkeypatch.setitem(sys.modules, "worker_base", base)
    if with_mflux:
        made, variants, config_mod = make_mflux(model)
        for name, module in (
            ("mflux", types.ModuleType("mflux")),
            ("mflux.models", types.ModuleType("mflux.models")),
            ("mflux.models.flux2", types.ModuleType("mflux.models.flux2")),
            ("mflux.models.flux2.variants", variants),
            ("mflux.models.common", types.ModuleType("mflux.models.common")),
            ("mflux.models.common.config", config_mod),
        ):
            monkeypatch.setitem(sys.modules, name, module)
    # `mlx.core` is no longer only `memory()`'s business: `load` and `generate`
    # both pin this process's shared streams (`_pin_stream`), so a render test
    # that left it out would be testing an import that cannot happen in
    # production. A caller may still hand in its own — an mlx too old to have
    # thread-local streams, say — and gets exactly that.
    if mlx_core is None:
        mlx_core = FakeMlxCore()
    mlx = types.ModuleType("mlx")
    mlx.core = mlx_core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)
    spec = importlib.util.spec_from_file_location("mflux_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, made


def snapshot(tmp_path, *, components=("transformer", "text_encoder", "vae")):
    """A downloaded repo directory, as `download` would leave it."""
    root = tmp_path / "snap"
    for name in components:
        (root / name).mkdir(parents=True)
    return str(root)


@pytest.fixture()
def base():
    return FakeBase()


def _request(tmp_path, **over):
    return {"prompt": "a red fox", "out": str(tmp_path / "fox.png"),
            "job": "sys:ai-image:abc", **over}


# -- the API the other image runner already defined ------------------------------


def test_the_reply_is_the_SAME_SHAPE_the_diffusers_runner_produces(
        monkeypatch, base, tmp_path):
    """A page must not be able to tell which backend rendered for it — the same
    claim the two whisper runners carry, and the reason a second image runner is
    not a second image API."""
    worker, model = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    request = _request(tmp_path, width=512, height=768, steps=3, guidance=2.5, seed=7)

    result = worker.generate(request)

    assert set(result) == {"path", "seconds", "seed", "width", "height", "steps"}
    assert result["path"] == request["out"]
    assert (result["width"], result["height"]) == (512, 768)
    assert result["steps"] == 3 and result["seed"] == 7
    assert os.path.exists(request["out"])


def test_the_defaults_are_the_OTHER_RUNNERS_defaults(monkeypatch, base, tmp_path):
    """28 steps and guidance 4.0 — diffusers' numbers, not mflux's own 4 and
    1.0. A caller that omits them must get the same picture-making behaviour
    from either engine; switching engines is a performance decision, not a
    silent change to what an unparameterised render means."""
    worker, model = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    worker.generate(_request(tmp_path))

    assert model.calls[0]["num_inference_steps"] == 28
    assert model.calls[0]["guidance"] == 4.0
    assert (model.calls[0]["width"], model.calls[0]["height"]) == (1024, 1024)


def test_the_image_is_written_where_the_SERVER_said(monkeypatch, base, tmp_path):
    """`overwrite=True` is not optional: mflux's default resolves a colliding
    path by writing somewhere ELSE, and the server has already told the caller
    where this image will be. The default would answer a request with a file at
    a path nobody was given."""
    worker, model = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    out = tmp_path / "fox.png"
    out.write_bytes(b"stale")  # something already there, as a re-render finds

    worker.generate(_request(tmp_path, out=str(out)))

    assert model.image.saved[0]["overwrite"] is True
    assert model.image.written == str(out), "the image landed at a path nobody was told"
    assert out.read_bytes() == b"PNG"


def test_a_missing_out_is_refused(monkeypatch, base, tmp_path):
    worker, _ = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    with pytest.raises(ValueError):
        worker.generate({"prompt": "x"})


def test_generating_with_no_model_loaded_says_so(monkeypatch, base, tmp_path):
    worker, _ = load_worker(monkeypatch, base)
    with pytest.raises(RuntimeError):
        worker.generate(_request(tmp_path))


# -- progress and the ✕ ----------------------------------------------------------


def test_progress_is_DENOISING_STEPS_on_the_callers_row(monkeypatch, base, tmp_path):
    """`generate_image()` takes no callback argument — the hook is a
    registration on the model's own callback registry. What reaches the row has
    to be identical to the diffusers runner's all the same: `unit: ""`, done as
    the step just taken, total as the steps asked for."""
    worker, model = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    worker.generate(_request(tmp_path, steps=4))

    steps = [t for t in base.ticks if "Denoising" in str(t.get("detail"))]
    assert [t["done"] for t in steps] == [0, 1, 2, 3, 4]
    assert {t["total"] for t in steps} == {4}
    assert {t["unit"] for t in steps} == {""}
    assert {t["job"] for t in steps} == {"sys:ai-image:abc"}


def test_a_cancel_in_the_denoising_loop_unwinds_the_render(
        monkeypatch, base, tmp_path):
    """The only interruption point in a minutes-long call. mflux's loop catches
    `KeyboardInterrupt` and nothing else, so a Cancelled raised in the hook
    propagates out of `generate_image()` rather than being swallowed into a
    half-rendered image."""
    worker, model = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    base.cancel_on_tick = 3  # the opening report, then two steps
    request = _request(tmp_path, steps=10)

    with pytest.raises(base.Cancelled):
        worker.generate(request)

    assert not os.path.exists(request["out"]), "a cancelled render still wrote a file"
    # It stopped EARLY rather than running the loop out and raising at the end.
    steps = [t for t in base.ticks if t.get("done")]
    assert len(steps) < 10, base.ticks


def test_the_reporter_is_registered_ONCE_however_many_renders_run(
        monkeypatch, base, tmp_path):
    """`CallbackRegistry.register` APPENDS and the registry belongs to the
    MODEL, not to a call. Registering per request would have the second render
    report every step twice — the second time to a job row that is over — and
    the tenth report ten times."""
    worker, model = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))

    worker.generate(_request(tmp_path, steps=2))
    worker.generate(_request(tmp_path, steps=2, out=str(tmp_path / "b.png")))

    assert len(model.callbacks.callbacks) == 1
    second = [t for t in base.ticks if t.get("job") == "sys:ai-image:abc"
              and t.get("done") == 1]
    assert len(second) == 2, "one tick per step per render, not per registration"


def test_the_reporter_stops_pointing_at_a_finished_request(
        monkeypatch, base, tmp_path):
    """The other half of registering once: the live request is read out of a
    module slot, so it has to be cleared — including on the cancel path, or a
    reporter left pointing at a closed row ticks it forever."""
    worker, model = load_worker(monkeypatch, base)
    worker.load(MODEL, snapshot(tmp_path))
    worker.generate(_request(tmp_path, steps=2))
    assert worker._request == {}

    base.cancel_on_tick = 2
    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path, steps=5, out=str(tmp_path / "c.png")))
    assert worker._request == {}


# -- loading ---------------------------------------------------------------------


def test_the_snapshot_DIRECTORY_is_what_mflux_is_given(monkeypatch, base, tmp_path):
    """Never the repo id. mflux resolves a local path ahead of everything else,
    so passing the directory is what keeps this load off the network — and
    `download` has already reported those bytes to the job row, so a second
    fetch inside `load` would be an unreported download the user watches as a
    stalled "Loading…"."""
    worker, model = load_worker(monkeypatch, base)
    path = snapshot(tmp_path)

    worker.load(MODEL, path)

    assert model.built["model_path"] == path
    assert model.built["model_config"] == "FLUX2_KLEIN_4B_CONFIG"
    assert base.state == {"device": "mps"}


def _pinned_run(monkeypatch, base, tmp_path, mlx_core):
    """A load and a render, on the threads production uses.

    `load` runs on `worker_base.serve`'s bring-up thread, which then EXITS, and
    `generate` arrives on a `ThreadingTCPServer` request thread — which is the
    whole of the bug, so the test has to reproduce the threading and not just
    the calls.
    """
    worker, model = load_worker(monkeypatch, base, mlx_core=mlx_core)
    loader = threading.Thread(
        target=worker.load, args=(MODEL, snapshot(tmp_path)), name="bring-up")
    loader.start()
    loader.join()
    render = threading.Thread(
        target=worker.generate, args=(_request(tmp_path, steps=2),), name="request-1")
    render.start()
    render.join()
    return worker, model


def test_the_load_and_the_render_share_ONE_mlx_stream_PER_DEVICE(
        monkeypatch, base, tmp_path):
    """From mlx 0.32 the default stream belongs to the THREAD that made it, and
    an unevaluated array forced anywhere else throws a C++ exception nothing
    catches. This runner loads on the bring-up thread and renders on a request
    thread, so every render died on its first denoising step: "the image process
    did not answer: Remote end closed connection without response", with one
    `libc++abi: … There is no Stream(cpu, 0) in current thread` line in the
    worker log.

    **BOTH devices, which is the difference from the whisper runner.** The
    default stream is per (thread, DEVICE) — measured on mlx 0.32.1, where one
    thread reports `Stream(cpu, 0)` and `Stream(gpu, 1)` and the next gets 2 and
    3 — so pinning `default_device()` alone leaves the CPU half of the graph
    owned by whichever thread first touched it. That is precisely the stream the
    abort named.
    """
    mlx_core = FakeMlxCore()

    _pinned_run(monkeypatch, base, tmp_path, mlx_core)

    threads = {name for name, _stream in mlx_core.pinned}
    streams = {stream for _name, stream in mlx_core.pinned}
    assert len(threads) > 1, f"only one thread pinned a stream: {mlx_core.pinned}"
    assert streams == {"SHARED-CPU-STREAM", "SHARED-GPU-STREAM"}, mlx_core.pinned
    # One stream per device for the whole process, not one per thread: a second
    # would be a second owner, which is the thing being prevented.
    assert sorted(mlx_core.made) == ["CPU", "GPU"], mlx_core.made


def test_an_mlx_without_thread_local_streams_is_left_alone(
        monkeypatch, base, tmp_path):
    """Streams were process-wide before 0.32 and there was nothing to pin. A
    runner that insisted on the newer call would turn a version skew into a
    worker that cannot render at all."""
    mlx_core = types.SimpleNamespace(float32="float32", cpu="CPU", gpu="GPU",
                                     get_active_memory=lambda: 0)

    _worker, model = _pinned_run(monkeypatch, base, tmp_path, mlx_core)

    assert model.image.saved, "the render never produced an image"


def test_the_whole_repo_is_downloaded_with_nothing_skipped(monkeypatch, base):
    """The visible difference from the diffusers runner's `download`: there is
    no full-precision component being replaced by a quantized one, so every
    file in the snapshot is a file the load will read."""
    worker, _ = load_worker(monkeypatch, base)
    assert worker.download(MODEL) == f"/snapshots/{MODEL}"
    assert base.snapshot_kwargs == {}


def test_a_model_with_no_variant_is_named_as_the_cause(monkeypatch, base, tmp_path):
    """mflux has no AutoPipeline: the variant class and its config are two
    arguments nothing can guess, so an unknown repo is refused with a sentence
    rather than attempted. The message points at both ways out — a repo that
    works, and the other engine."""
    worker, _ = load_worker(monkeypatch, base)

    with pytest.raises(RuntimeError) as caught:
        worker.load("black-forest-labs/FLUX.2-klein-4B", snapshot(tmp_path))
    message = str(caught.value)
    assert "mlx-community/FLUX.2-Klein-4B-4bit" in message
    assert "Diffusers" in message


def test_a_repo_in_the_WRONG_FORMAT_is_named_as_the_cause(monkeypatch, base, tmp_path):
    """A known repo id whose download is a diffusers or GGUF layout. mflux's own
    answer is a ValueError about path resolution, which says nothing a user can
    act on — and the check runs BEFORE the import, so the explanation does not
    depend on the runner environment being importable here."""
    worker, _ = load_worker(monkeypatch, base, with_mflux=False)
    bare = snapshot(tmp_path, components=("unet",))

    with pytest.raises(RuntimeError) as caught:
        worker.load(MODEL, bare)
    message = str(caught.value)
    assert "transformer/" in message and "MLX" in message


# -- memory ----------------------------------------------------------------------


def test_memory_is_the_ACTIVE_figure_not_the_allocator_POOL(monkeypatch, base):
    """The number that goes in a column beside the torch runner's.

    MLX's cache is buffers reserved from Metal and not returned — on this model
    ~23.6GB against ~14.1GB active (D310). Reporting the pool would tell the AI
    Models page that one resident image model costs two thirds of a 34GB
    machine, which is not what "this model is holding" means anywhere else in
    the app: the torch runner reports allocated bytes, not the driver's
    reservation, and the two have to be comparable.
    """
    mlx_core = types.SimpleNamespace(get_active_memory=lambda: 14_126_548_182,
                                     get_cache_memory=lambda: 23_610_000_000)
    worker, _ = load_worker(monkeypatch, base, mlx_core=mlx_core)
    assert worker.memory() == 14_126_548_182


def test_memory_falls_back_to_the_OLD_mlx_spelling(monkeypatch, base):
    mlx_core = types.ModuleType("mlx.core")
    mlx_core.metal = types.SimpleNamespace(get_active_memory=lambda: 42)
    worker, _ = load_worker(monkeypatch, base, mlx_core=mlx_core)
    assert worker.memory() == 42


def test_memory_answers_None_rather_than_raising(monkeypatch, base):
    """A memory probe must never break `/health`."""
    mlx_core = types.SimpleNamespace(get_active_memory=lambda: 0)
    worker, _ = load_worker(monkeypatch, base, mlx_core=mlx_core)
    assert worker.memory() is None


def test_the_worker_never_shells_out(monkeypatch, base):
    """The rule every runner folder carries: a subprocess call to a binary the
    app does not ship works here and fails on a user's machine."""
    source = open(WORKER_PATH, encoding="utf-8").read()
    assert "import subprocess" not in source
    assert "subprocess." not in source
    assert "os.system" not in source and "Popen" not in source


# -- the live preview (SPEC §40) -------------------------------------------------
#
# The same thumbnail the diffusers runner writes, from the same shared module,
# out of a callback with a different signature and latents in a different
# library's arrays. The arithmetic and the lifecycle are tested in
# `test_ai_image_preview.py`; what is pinned HERE is that this engine's wiring
# reaches it — and reaches it identically, since a page must not be able to tell
# which one rendered.


def _mlx_core():
    """`mlx.core`, for the one attribute `_as_numpy` reads off it."""
    return types.SimpleNamespace(float32="float32")


def _previewing_worker(monkeypatch, base, tmp_path, *, model=None, vae=True):
    """A loaded worker whose recipe does (or does not) name an autoencoder.

    The table is reached through `worker.formats`, not through
    `fused_render.ai.runners.formats`: a runner imports it by path off
    `sys.path`, so the packaged module is a second object and patching it would
    patch nothing this worker reads.
    """
    worker, made = load_worker(monkeypatch, base, model=model,
                               mlx_core=_mlx_core())
    recipe = dict(worker.formats.MFLUX_VARIANTS[MODEL])
    if not vae:
        recipe.pop("vae", None)
    monkeypatch.setitem(worker.formats.MFLUX_VARIANTS, MODEL, recipe)
    worker.load(MODEL, snapshot(tmp_path))
    return worker, made


def _packed(rng, tokens=32 * 32):
    return rng.standard_normal((1, tokens, 128)).astype("float32")


def test_a_thumbnail_appears_from_the_SECOND_step_and_is_GONE_at_the_end(
        monkeypatch, base, tmp_path):
    """Step 1 has no predecessor, so it has no velocity and no estimate — and
    the last frame is duplicate bytes once the real PNG lands. The same two
    sentences the diffusers runner's copy of this test carries, deliberately."""
    import numpy

    rng = numpy.random.default_rng(3)
    model = FakeModel(latents=FakeLatents(_packed(rng)),
                      sigmas=[1.0, 0.9, 0.7, 0.4, 0.0])
    worker, made = _previewing_worker(monkeypatch, base, tmp_path, model=model)
    request = _request(tmp_path, width=512, height=512, steps=4,
                       outPreview=str(tmp_path / "fox.preview.png"))
    seen = []
    made.watch = lambda t: seen.append(os.path.exists(request["outPreview"]))
    worker.generate(request)

    assert seen == [False, True, True, True]
    assert sorted(os.listdir(tmp_path)) == ["fox.png", "snap"]


def test_PACKED_and_UNPATCHIFIED_latents_are_both_understood(
        monkeypatch, base, tmp_path):
    """mflux hands the callback `(B, N, 128)` on some paths and
    `(B, 128, h, w)` on others. Both are the same picture, and neither runner
    reshapes before handing it over — that would be a second copy of the unpack
    rule `preview._tokens` owns.

    The two steps hold DIFFERENT latents, so the estimate is a real
    extrapolation rather than the degenerate zero-velocity case: an unpack that
    agreed on a constant field and disagreed on a moving one would otherwise
    pass this.
    """
    import numpy

    rng = numpy.random.default_rng(4)
    first = _packed(rng, tokens=16)
    packed = _packed(rng, tokens=16)
    shapes = [
        (first, packed),
        (first[0].reshape(4, 4, 128).transpose(2, 0, 1)[None],
         packed[0].reshape(4, 4, 128).transpose(2, 0, 1)[None]),
    ]
    written = []
    for index, pair in enumerate(shapes):
        # One directory each: `snapshot` makes a fresh repo layout per load.
        room = tmp_path / str(index)
        room.mkdir()
        model = FakeModel(sigmas=[1.0, 0.5, 0.0],
                          latents_per_step=[FakeLatents(a) for a in pair])
        worker, _ = _previewing_worker(monkeypatch, base, room, model=model)
        request = _request(room, width=64, height=64, steps=2,
                           out=str(room / "fox.png"),
                           outPreview=str(room / "fox.preview.png"))
        monkeypatch.setattr(worker.preview.Sink, "discard", lambda self: None)
        worker.generate(request)
        with open(request["outPreview"], "rb") as handle:
            written.append(handle.read())
    assert written[0] == written[1]


def test_the_thumbnail_is_the_estimate_at_the_sigma_just_REACHED(
        monkeypatch, base, tmp_path):
    """`_sigma_after` is a SECOND copy of the off-by-one, against a second
    library's schedule, and it had no test at all — the diffusers runner's
    covered only its own.

    The same two properties make this pin it rather than merely pass: the
    latents MOVE (a zero velocity makes the estimate equal the latent at any
    sigma, so any indexing would satisfy it) and the frame examined is
    MID-RENDER (the schedule ends at sigma 0, where the estimate degenerates the
    same way). Read one entry earlier, these pixels are different ones.
    """
    import numpy
    from PIL import Image

    rng = numpy.random.default_rng(21)
    sigmas = [1.0, 0.9, 0.7, 0.318, 0.0]
    steps = [_packed(rng, tokens=32 * 32) * 2.0 for _ in range(4)]
    model = FakeModel(sigmas=sigmas,
                      latents_per_step=[FakeLatents(a) for a in steps])
    worker, made = _previewing_worker(monkeypatch, base, tmp_path, model=model)
    preview = worker.preview
    out = str(tmp_path / "fox.preview.png")
    shots = []

    def snapshot(t):
        if not os.path.exists(out):
            shots.append(None)
            return
        with Image.open(out) as image:
            shots.append((image.size, numpy.frombuffer(
                image.convert("RGB").tobytes(), dtype=numpy.uint8).tolist()))

    made.watch = snapshot
    worker.generate(_request(tmp_path, width=512, height=512, steps=4,
                             outPreview=out))

    def frame(sigma_previous, sigma_current):
        estimate = preview.denoised(steps[1], steps[2], sigma_previous, sigma_current)
        rgb = preview.project(estimate, "AutoencoderKLFlux2")
        return numpy.asarray(rgb * 255.0 + 0.5, dtype=numpy.uint8).reshape(-1).tolist()

    # After step index 2: latents[1] at sigmas[2], latents[2] at sigmas[3] — the
    # level the schedule has ARRIVED at, not the one it left.
    right = frame(sigmas[2], sigmas[3])
    stale = frame(sigmas[1], sigmas[2])
    assert right != stale, "the schedule chosen cannot tell the two apart"

    size, pixels = shots[2]
    assert size == (preview.MAX_SIDE, preview.MAX_SIDE)
    assert pixels == right
    assert pixels != stale


def test_the_FRAME_is_written_BEFORE_the_tick_that_announces_it(monkeypatch, base,
                                                                tmp_path):
    """The same ordering rule as the diffusers runner's, pinned the same way and
    for the same reason: `done` on the tick is what `runtime.js` turns into the
    cache-busted `&step=N` preview URL, so reporting first can hand a page the
    URL for a frame that is not on disk — and because that URL is keyed by the
    step and never requested twice, a fetch in the window caches the PREVIOUS
    frame's bytes under it for that step's whole duration.

    Two engines, one order. It reads as arbitrary from either side."""
    import numpy

    rng = numpy.random.default_rng(11)
    model = FakeModel(latents=FakeLatents(_packed(rng)),
                      sigmas=[1.0, 0.9, 0.7, 0.4, 0.0])
    worker, _ = _previewing_worker(monkeypatch, base, tmp_path, model=model)
    events = []
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
    worker.generate(_request(tmp_path, width=512, height=512, steps=4,
                             outPreview=str(tmp_path / "fox.preview.png")))
    assert events == ["tick 1", "frame", "tick 2", "frame", "tick 3",
                      "frame", "tick 4"]


def test_a_variant_that_names_NO_autoencoder_renders_exactly_as_BEFORE(
        monkeypatch, base, tmp_path):
    """No file, no branch — and no conversion either: the latents go over as a
    closure precisely so a no-op sink never touches them."""
    def touched():
        raise AssertionError("a no-op preview converted the latents")

    import numpy

    rng = numpy.random.default_rng(5)
    model = FakeModel(latents=FakeLatents(_packed(rng), on_read=touched),
                      sigmas=[1.0, 0.9, 0.7, 0.4, 0.0])
    worker, _ = _previewing_worker(monkeypatch, base, tmp_path, model=model,
                                   vae=False)
    worker.generate(_request(tmp_path, width=512, height=512, steps=4,
                             outPreview=str(tmp_path / "fox.preview.png")))
    assert sorted(os.listdir(tmp_path)) == ["fox.png", "snap"]


def test_a_request_that_named_no_preview_file_renders_exactly_as_BEFORE(
        monkeypatch, base, tmp_path):
    """A worker request from before this feature carries no `outPreview`."""
    def touched():
        raise AssertionError("a no-op preview converted the latents")

    import numpy

    rng = numpy.random.default_rng(6)
    model = FakeModel(latents=FakeLatents(_packed(rng), on_read=touched),
                      sigmas=[1.0, 0.9, 0.7, 0.4, 0.0])
    worker, _ = _previewing_worker(monkeypatch, base, tmp_path, model=model)
    worker.generate(_request(tmp_path, width=512, height=512, steps=4))
    assert sorted(os.listdir(tmp_path)) == ["fox.png", "snap"]


def test_the_thumbnail_is_removed_when_the_render_is_CANCELLED(
        monkeypatch, base, tmp_path):
    """A ✕ means the user does not want this picture, at any resolution."""
    import numpy

    rng = numpy.random.default_rng(7)
    model = FakeModel(latents=FakeLatents(_packed(rng)),
                      sigmas=[1.0, 0.9, 0.7, 0.4, 0.0])
    worker, _ = _previewing_worker(monkeypatch, base, tmp_path, model=model)
    base.cancel_on_tick = 4          # the opening report, then three steps
    with pytest.raises(base.Cancelled):
        worker.generate(_request(tmp_path, width=512, height=512, steps=4,
                                 outPreview=str(tmp_path / "fox.preview.png")))
    assert os.listdir(tmp_path) == ["snap"]


def test_both_image_workers_import_the_ONE_previewer_rather_than_a_copy():
    """The structural half: a second `preview.py` under either runner folder is
    the drift AI-10c forbids, and no behavioural test would catch it — both
    copies would pass their own. Asserted from the SECOND runner's tests, the
    way `test_ai_partial_transcript.py` does for the two whisper engines."""
    runners = os.path.dirname(os.path.dirname(WORKER_PATH))
    for folder in ("diffusers_image", "mflux_image"):
        assert not os.path.exists(os.path.join(runners, folder, "preview.py")), folder
        with open(os.path.join(runners, folder, "worker.py"), encoding="utf-8") as fh:
            assert "import preview" in fh.read(), folder


def test_the_key_BOTH_runners_use_is_the_same_string():
    """The torch runner reads `type(pipe.vae).__name__` and this one reads its
    variant recipe, and the two have to land on one row of one table — the whole
    reason the projection lives at the runners root and not in either worker."""
    from fused_render.ai.runners import formats, preview

    assert formats.MFLUX_VARIANTS[MODEL]["vae"] in preview.PROJECTIONS
