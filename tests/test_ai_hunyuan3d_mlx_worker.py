"""The hunyuan3d_mlx runner's own `generate()` logic, driven directly (SPEC
§48, code review 2026-08-28).

Same shape `tests/test_ai_ltx_video_worker.py` uses: the module is stdlib +
mlx-only at import time, so it loads against fakes injected into
`sys.modules` — no Metal, no weights, no real `hy3dshape`. `load()` and the
`sys.modules` import-bypass machinery (`_shape_pipeline_class`) are NOT
exercised here — `generate()` only reads `_loaded["pipeline"]`, which this
file sets directly to a fake, the same shortcut `test_ai_mflux_worker.py`
takes for its own pipeline object.

What is pinned here is the pair of defects code review found on this
branch: `steps`/`guidance` of exactly `0` reaching the pipeline call as `0`
rather than silently becoming the default (finding 1 — `or default` treats
0 as absent), and a ✕ on either cancellation channel actually stopping the
render from landing as a completed job (finding 2 — nothing checked either
channel at all before this).
"""
import importlib.util
import os
import sys
import types

WORKER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "hunyuan3d_mlx", "worker.py",
)


class FakeCancelled(Exception):
    pass


class FakeBase:
    """A stand-in for `worker_base` — `test_ai_ltx_video_worker.py`'s
    `FakeBase`, plus a controllable `CANCEL` event and a `report_or_cancel`
    that can actually raise, which that file's version never needed to."""

    Cancelled = FakeCancelled

    def __init__(self, cancel_requested_on_tick=False):
        self.ticks = []
        self.state = {}
        #: Set directly, the same way a `/cancel` POST to the real worker
        #: sets the real `worker_base.CANCEL` — a plain object with
        #: `is_set()`/`set()`/`clear()`, not `threading.Event` itself, so a
        #: test can flip it from inside a fake pipeline's `__call__`.
        self.CANCEL = types.SimpleNamespace(_flag=False,
                                            is_set=lambda: self.CANCEL._flag,
                                            set=lambda: setattr(self.CANCEL, "_flag", True),
                                            clear=lambda: setattr(self.CANCEL, "_flag", False))
        #: When True, every `report_or_cancel` call raises — the OTHER
        #: cancellation channel (a job row's own `cancel_requested`,
        #: independent of `CANCEL`).
        self.cancel_requested_on_tick = cancel_requested_on_tick

    def report(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})

    def report_or_cancel(self, job=None, **fields):
        self.ticks.append({"job": job, **fields})
        if self.cancel_requested_on_tick:
            raise self.Cancelled()

    def set_state(self, **fields):
        self.state.update(fields)

    def download_snapshot(self, model_id, **kwargs):
        raise NotImplementedError("not exercised by these tests")

    def serve(self, **kwargs):
        return None


class FakePipeline:
    """`hy3dshape.pipeline_mlx.ShapePipeline`, from the outside — records
    every call and writes a fake `.glb` at `export`'s target, the same
    "modelled just enough to exercise the worker's own file handling" shape
    `test_ai_ltx_video_worker.py`'s `FakePipeline.generate_and_save` uses.

    `on_call`, when set, runs INSIDE `__call__` — the hook these tests use to
    simulate a ✕ arriving WHILE the (otherwise uninterruptible) render is in
    flight, the case `generate`'s post-call cancellation check exists for.
    """

    def __init__(self, on_call=None):
        self.calls = []
        self.on_call = on_call
        self.exported_to = None

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call()
        return FakeMesh(self)


class FakeMesh:
    def __init__(self, pipeline):
        self._pipeline = pipeline
        self.faces = _FakeFacesArray()

    def export(self, path):
        self._pipeline.exported_to = path
        with open(path, "wb") as handle:
            handle.write(b"glTF")


class _FakeFacesArray:
    """`mesh.faces.shape[0]` is the only thing `generate` reads off `faces`."""

    shape = (1234, 3)


class FakeMlxCore(types.ModuleType):
    """`mlx.core`, as `_pin_stream` uses it — no-ops are enough here since
    these tests never touch a real stream."""

    def __init__(self):
        super().__init__("mlx.core")
        self.cpu = "CPU"

    def default_device(self):
        return "GPU"

    def get_active_memory(self):
        return 0


def load_worker(monkeypatch, base, mlx_core=None):
    """A fresh import of the hunyuan3d_mlx worker against the fakes."""
    monkeypatch.setitem(sys.modules, "worker_base", base)

    mlx = types.ModuleType("mlx")
    mlx.core = mlx_core or FakeMlxCore()
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx.core)

    spec = importlib.util.spec_from_file_location(
        "hunyuan3d_mlx_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(tmp_path, **over):
    body = {"image": "/photos/chair.png", "out": str(tmp_path / "mesh.glb"), "job": "j1"}
    body.update(over)
    return body


# ---------------------------------------------------------------- finding 1


def test_steps_zero_reaches_the_pipeline_as_zero_not_the_default(monkeypatch, tmp_path):
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    module.generate(_request(tmp_path, steps=0))

    assert pipeline.calls[0]["num_inference_steps"] == 0


def test_guidance_zero_reaches_the_pipeline_as_zero_not_the_default(monkeypatch, tmp_path):
    """CFG off — the one qualitative setting this capability has — must
    actually reach the pipeline as `0.0`, not silently become the default
    `_DEFAULT_GUIDANCE` the way `body.get("guidance") or default` would."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    result = module.generate(_request(tmp_path, guidance=0))

    assert pipeline.calls[0]["guidance_scale"] == 0
    assert result["guidance"] == 0


def test_omitted_steps_and_guidance_still_fall_back_to_the_real_defaults(monkeypatch, tmp_path):
    """The other half of the same fix: `None` (genuinely absent) must still
    take the default — only `0` is the value `or` was wrongly swallowing."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    module.generate(_request(tmp_path))

    assert pipeline.calls[0]["num_inference_steps"] == module._DEFAULT_STEPS
    assert pipeline.calls[0]["guidance_scale"] == module._DEFAULT_GUIDANCE


# ---------------------------------------------------------------- finding 2


def test_a_cancel_flag_already_set_before_the_call_stops_the_render(monkeypatch, tmp_path):
    """`worker_base.CANCEL` set (a `/cancel` POST that reached this worker
    before `generate` was even invoked) must raise `Cancelled` WITHOUT ever
    calling the pipeline — the pre-start case."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline
    base.CANCEL.set()

    out = tmp_path / "mesh.glb"
    try:
        module.generate(_request(tmp_path, out=str(out)))
        raised = False
    except FakeCancelled:
        raised = True
    assert raised
    assert pipeline.calls == []
    assert not out.exists()


def test_a_cancelled_job_row_before_the_call_stops_the_render(monkeypatch, tmp_path):
    """The OTHER channel: this render's own job row already carries
    `cancel_requested` (the download manager's ✕), read back through
    `report_or_cancel`'s reply — same pre-start outcome as `CANCEL`."""
    base = FakeBase(cancel_requested_on_tick=True)
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    out = tmp_path / "mesh.glb"
    try:
        module.generate(_request(tmp_path, out=str(out)))
        raised = False
    except FakeCancelled:
        raised = True
    assert raised
    assert pipeline.calls == []
    assert not out.exists()


def test_a_cancel_flag_set_WHILE_the_pipeline_call_is_running_still_stops_the_export(
        monkeypatch, tmp_path):
    """The case `generate`'s post-call check exists for: the render cannot
    be interrupted mid-flight (no hook exists — this is the whole reason
    for the module's own "no mid-render INTERRUPTION point" docstring), but
    a ✕ that arrived during it must still stop the mesh from being written
    and the row from reading done."""
    base = FakeBase()

    def cancel_mid_render():
        base.CANCEL.set()

    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline(on_call=cancel_mid_render)
    module._loaded["pipeline"] = pipeline

    out = tmp_path / "mesh.glb"
    try:
        module.generate(_request(tmp_path, out=str(out)))
        raised = False
    except FakeCancelled:
        raised = True
    assert raised
    # The pipeline DID run (the compute could not be interrupted) — what
    # must not have happened is the export.
    assert len(pipeline.calls) == 1
    assert pipeline.exported_to is None
    assert not out.exists()


def test_an_uncancelled_render_writes_the_mesh_and_returns_normally(monkeypatch, tmp_path):
    """The ordinary path is unbroken by either check: no cancel anywhere,
    the mesh is exported and the reply is the usual shape."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    out = tmp_path / "mesh.glb"
    result = module.generate(_request(tmp_path, out=str(out)))

    assert out.exists()
    assert result["path"] == str(out)
    assert result["faces"] == 1234
