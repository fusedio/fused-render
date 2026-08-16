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
read off mflux 0.13 on an Apple Silicon machine, and the benchmark behind D305
ran that path for real.
"""
import importlib.util
import os
import sys
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


class FakeModel:
    """An mflux variant: a callback registry, and a loop that drives it."""

    def __init__(self, steps_to_run=None):
        self.callbacks = FakeRegistry()
        self.calls = []
        self.image = FakeImage()
        #: How many steps the fake loop actually runs, when it should differ
        #: from what was asked (nothing does this today; it exists so a cancel
        #: mid-loop is expressible).
        self.steps_to_run = steps_to_run

    def generate_image(self, seed, prompt, num_inference_steps=4, height=1024,
                       width=1024, guidance=1.0, **kwargs):
        self.calls.append({"seed": seed, "prompt": prompt, "guidance": guidance,
                           "num_inference_steps": num_inference_steps,
                           "height": height, "width": width, **kwargs})
        total = self.steps_to_run if self.steps_to_run is not None else num_inference_steps
        for t in range(total):
            for callback in self.callbacks.callbacks:
                # mflux calls it with keywords; the reporter's signature has to
                # match, and a positional call here would hide a rename.
                callback.call_in_loop(t=t, seed=seed, prompt=prompt, latents=None,
                                      config=None, time_steps=None)
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
    if mlx_core is not None:
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
    ~23.6GB against ~14.1GB active (D305). Reporting the pool would tell the AI
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
