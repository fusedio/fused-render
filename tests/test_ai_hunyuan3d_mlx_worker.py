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

from PIL import Image
import numpy as np

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

    def __init__(self, on_call=None, denoise_steps=0, decode_levels=0):
        self.calls = []
        self.on_call = on_call
        self.exported_to = None
        #: A real `ShapePipeline` never goes `None` until the loop below
        #: reaches its own "free the DiT" step — `_ensure_pipeline` reads
        #: this to decide whether the CACHED pipeline is still usable.
        #: `object()`, not `None`: any non-`None` value reads as "still
        #: loaded" the same way the real `HunYuanDiTPlain` instance does.
        self.dit = object()
        # `scheduler.step`/`vae._query_sdf_volume` — the two bound methods
        # `generate()` wraps for progress (Defect/Task C). Present even on
        # tests that never call them, because `generate()` unconditionally
        # reads and reassigns both.
        self.scheduler = types.SimpleNamespace(step=lambda *a, **k: None)
        self.vae = types.SimpleNamespace(_query_sdf_volume=lambda *a, **k: None)
        #: How many times THIS fake's `__call__` should invoke
        #: `self.scheduler.step` / `self.vae._query_sdf_volume` itself,
        #: mimicking the real pipeline's denoising loop and hierarchical
        #: decode — the only way `generate()`'s wrappers ever actually fire.
        self._denoise_steps = denoise_steps
        self._decode_levels = decode_levels

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        for _ in range(self._denoise_steps):
            self.scheduler.step()
        for _ in range(self._decode_levels):
            self.vae._query_sdf_volume()
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


def _write_cutout(path, size=64, cx=32, cy=32, r=20):
    """A transparent-background PNG with one opaque disc — real enough for
    `_prepare_cutout` to crop to (a plain RGBA the way a Playground attach
    hands the worker one), asymmetric by default (`cx`/`cy` off-centre from
    the frame's own centre) so a recentre test can tell "moved" from "not
    moved"."""
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    yy, xx = np.mgrid[0:size, 0:size]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    arr[mask] = (200, 50, 50, 255)
    Image.fromarray(arr, mode="RGBA").save(path)
    return str(path)


def _write_rgb(path, size=64):
    """A plain opaque photo — no alpha channel, and no distinct subject
    either (one uniform colour, no bbox anything could crop to)."""
    Image.new("RGB", (size, size), (80, 120, 200)).save(path)
    return str(path)


def _write_opaque_rgba(path, size=64):
    """RGBA where every pixel is fully opaque and uniform — no alpha matte
    to use, and (like `_write_rgb`) nothing for the border-matte fallback
    to find a subject in either."""
    Image.new("RGBA", (size, size), (80, 120, 200, 255)).save(path)
    return str(path)


def _write_flat_backdrop(path, backdrop, subject, size=200, cx=130, cy=110, r=40):
    """A plain RGB image (no alpha) on a near-uniform backdrop, with a
    distinct subject disc that does NOT touch the frame edge — the shape
    of a real failing attach this branch exists for (D623's revision): an
    exported screenshot with a flat background and no alpha channel at
    all. `backdrop`/`subject` are `(r, g, b)` tuples so the same helper
    covers both a dark and a light backdrop."""
    arr = np.empty((size, size, 3), dtype=np.uint8)
    arr[:, :] = backdrop
    yy, xx = np.mgrid[0:size, 0:size]
    mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    arr[mask] = subject
    Image.fromarray(arr, mode="RGB").save(path)
    return str(path)


def _write_busy_border(path, size=200):
    """No alpha, and a border that is NOT a flat backdrop — a checkerboard
    reaching every edge, so `_border_backdrop_mask`'s uniformity test must
    fail and this function must raise rather than guess a matte."""
    arr = np.zeros((size, size, 3), dtype=np.uint8)
    yy, xx = np.mgrid[0:size, 0:size]
    checker = ((xx // 8) + (yy // 8)) % 2 == 0
    arr[checker] = (230, 230, 230)
    arr[~checker] = (10, 10, 10)
    Image.fromarray(arr, mode="RGB").save(path)
    return str(path)


def _request(tmp_path, **over):
    image = str(tmp_path / "chair.png")
    if not os.path.exists(image):
        _write_cutout(image)
    body = {"image": image, "out": str(tmp_path / "mesh.glb"), "job": "j1"}
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


# ---------------------------------------------------------------- Defect 1: preprocessing


def test_rgb_image_with_no_distinct_subject_is_rejected(monkeypatch, tmp_path):
    """No alpha, and a uniform-colour image with nothing a border-matte
    could find a subject in either — rejected, not silently treated as an
    empty cutout."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    image = _write_rgb(tmp_path / "photo.png")
    try:
        module.generate(_request(tmp_path, image=image))
        raised = None
    except ValueError as e:
        raised = str(e)

    assert raised is not None
    assert "entirely backdrop" in raised
    # Rejected before the pipeline is ever touched — an actionable error,
    # not a crash three frames into a render nobody could have finished.
    assert pipeline.calls == []


def test_fully_opaque_rgba_with_no_distinct_subject_is_rejected(monkeypatch, tmp_path):
    """The RGBA sibling of the RGB case above: an alpha channel carrying no
    real matte (every pixel 255) falls through to the SAME border-matte
    fallback, and a uniform image has no subject there either."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    image = _write_opaque_rgba(tmp_path / "opaque.png")
    try:
        module.generate(_request(tmp_path, image=image))
        raised = None
    except ValueError as e:
        raised = str(e)

    assert raised is not None
    assert "entirely backdrop" in raised
    assert pipeline.calls == []


def test_non_uniform_border_is_rejected(monkeypatch, tmp_path):
    """No alpha, and a border that is NOT a flat backdrop (a checkerboard
    reaching every edge) — `_border_backdrop_mask` must refuse to guess a
    matte, and `generate` must surface that as the actionable "no
    transparent background AND no flat backdrop" error, not a crash deep
    inside the pipeline."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline()
    module._loaded["pipeline"] = pipeline

    image = _write_busy_border(tmp_path / "busy.png")
    try:
        module.generate(_request(tmp_path, image=image))
        raised = None
    except ValueError as e:
        raised = str(e)

    assert raised is not None
    assert "no flat backdrop" in raised
    assert pipeline.calls == []


def _assert_recentred_geometry(out, canvas_hint=200):
    """Shared assertions for the auto-matte geometry tests below: square,
    white background, subject bbox centred and scaled to 85% on its longer
    side — the same checks `test_cutout_is_recentred_to_the_reference_
    geometry` runs for the alpha path."""
    out_arr = np.asarray(out)
    canvas = out_arr.shape[0]
    assert canvas == out_arr.shape[1]
    assert tuple(out_arr[1, 1]) == (255, 255, 255)

    non_white = np.any(out_arr != 255, axis=-1)
    ys, xs = np.nonzero(non_white)
    y_min, y_max, x_min, x_max = ys.min(), ys.max(), xs.min(), xs.max()
    out_h, out_w = y_max - y_min, x_max - x_min

    cy = (y_min + y_max) / 2
    cx = (x_min + x_max) / 2
    assert abs(cy - canvas / 2) <= 3
    assert abs(cx - canvas / 2) <= 3
    expected = canvas * 0.85
    assert abs(max(out_h, out_w) - expected) <= 3


def test_flat_dark_backdrop_is_auto_matted_and_recentred(tmp_path):
    """No alpha, a near-black flat backdrop (the exact shape of the real
    failing attach D623's revision was written against: mode RGB, no
    alpha, a near-uniform `~(1, 3, 2)` background) — auto-matted by border
    colour and recentred to the same geometry the alpha path uses."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hy3dworker_geometry_dark", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    path = tmp_path / "dark_backdrop.png"
    _write_flat_backdrop(path, backdrop=(1, 3, 2), subject=(210, 90, 60))

    out = module._prepare_cutout(str(path), border_ratio=0.15)
    assert out.mode == "RGB"
    _assert_recentred_geometry(out)


def test_flat_white_backdrop_is_auto_matted_and_recentred(tmp_path):
    """The distance test must be relative to the SAMPLED backdrop colour,
    not hardcoded against black — a white backdrop has to auto-matte
    exactly the same way a dark one does."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hy3dworker_geometry_white", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    path = tmp_path / "white_backdrop.png"
    _write_flat_backdrop(path, backdrop=(250, 250, 248), subject=(30, 80, 160))

    out = module._prepare_cutout(str(path), border_ratio=0.15)
    assert out.mode == "RGB"
    _assert_recentred_geometry(out)


def test_cutout_is_recentred_to_the_reference_geometry(tmp_path):
    """`_prepare_cutout` ported the torch reference's `ImageProcessorV2.
    recenter` geometry (`preprocessors.py`, pinned commit) — assert the
    ACTUAL transform, not merely that the function ran: the subject's
    bounding box lands centred on the square canvas, scaled so its longer
    side fills `1 - border_ratio` of the canvas (0.85 at the default 0.15
    border), on a white background."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("hy3dworker_geometry", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    # `_prepare_cutout` only needs PIL/numpy at call time — no `worker_base`
    # or `mlx` stub required, since the module-level code these tests skip
    # by loading directly (`_pin_stream`, `_shape_pipeline_class`) is never
    # reached by this call.
    spec.loader.exec_module(module)

    # A 40x60 disc, well off-centre in a 200x300 frame — asymmetric on both
    # axes so an un-centred or un-scaled result is caught either way.
    size_w, size_h = 200, 300
    arr = np.zeros((size_h, size_w, 4), dtype=np.uint8)
    yy, xx = np.mgrid[0:size_h, 0:size_w]
    mask = ((xx - 40) / 30) ** 2 + ((yy - 60) / 20) ** 2 <= 1
    arr[mask] = (10, 200, 10, 255)
    path = tmp_path / "offcentre.png"
    Image.fromarray(arr, mode="RGBA").save(path)

    out = module._prepare_cutout(str(path), border_ratio=0.15)

    assert out.mode == "RGB"
    out_arr = np.asarray(out)
    canvas = out_arr.shape[0]
    assert canvas == out_arr.shape[1]  # square
    # Background outside the subject is white.
    assert tuple(out_arr[2, 2]) == (255, 255, 255)

    # The recentred subject: find non-white pixels (the disc, resized).
    non_white = np.any(out_arr != 255, axis=-1)
    ys, xs = np.nonzero(non_white)
    y_min, y_max, x_min, x_max = ys.min(), ys.max(), xs.min(), xs.max()
    out_h, out_w = y_max - y_min, x_max - x_min

    # Centred: the subject's own bbox centre lands on the canvas centre,
    # within a couple of pixels of resampling slop.
    cy = (y_min + y_max) / 2
    cx = (x_min + x_max) / 2
    assert abs(cy - canvas / 2) <= 3
    assert abs(cx - canvas / 2) <= 3

    # Scaled to fill 0.85 of the canvas on its longer side (the original
    # disc is 60 tall x 40 wide, so height is the binding dimension).
    expected = canvas * 0.85
    assert abs(max(out_h, out_w) - expected) <= 3


def test_empty_surface_error_becomes_the_actionable_message(monkeypatch, tmp_path):
    """The exact crash Defect 1 reproduced against the live worker —
    `ValueError: need at least one array to concatenate`, raised from
    `_query_sdf_volume` with an empty `all_logits` — must never reach a
    caller verbatim. Built with a REAL traceback frame named
    `_query_sdf_volume` in a file ending `model_mlx.py`, so this pins
    `_is_empty_surface_error`'s actual detection (frame name + file +
    `all_logits == []`), not merely "some ValueError got remapped"."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)

    # A tiny module, compiled with a filename that ends `model_mlx.py` —
    # `_is_empty_surface_error` keys on `code.co_filename`, so the fake
    # frame must actually carry that name, not just the function name.
    src = (
        "def _query_sdf_volume():\n"
        "    all_logits = []\n"
        "    import numpy as np\n"
        "    return np.concatenate(all_logits, axis=0)\n"
    )
    fake_path = os.path.join(os.path.dirname(WORKER_PATH), "hy3dshape_fake_model_mlx.py")
    code = compile(src, fake_path, "exec")
    ns = {}
    exec(code, ns)  # noqa: S102 - test-only, builds a real traceback frame

    def on_call():
        ns["_query_sdf_volume"]()

    pipeline = FakePipeline(on_call=on_call)
    module._loaded["pipeline"] = pipeline

    try:
        module.generate(_request(tmp_path))
        message = None
    except ValueError as e:
        message = str(e)

    assert message is not None
    assert "need at least one array to concatenate" not in message
    assert "no visible surface" in message
    assert "__cause__" not in message


def test_an_unrelated_valueerror_is_not_swallowed(monkeypatch, tmp_path):
    """`_is_empty_surface_error` must not fire for a DIFFERENT `ValueError`
    that merely happens to come out of the pipeline call — only the exact
    empty-`all_logits`-in-`_query_sdf_volume` condition is remapped."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)

    def on_call():
        raise ValueError("some unrelated failure")

    pipeline = FakePipeline(on_call=on_call)
    module._loaded["pipeline"] = pipeline

    try:
        module.generate(_request(tmp_path))
        message = None
    except ValueError as e:
        message = str(e)

    assert message == "some unrelated failure"


# ---------------------------------------------------------------- Task C: real progress


def test_progress_ticks_report_denoise_then_decode_phases(monkeypatch, tmp_path):
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline(denoise_steps=3, decode_levels=2)
    module._loaded["pipeline"] = pipeline

    module.generate(_request(tmp_path, steps=3))

    denoise_ticks = [t for t in base.ticks if "Denoising step" in (t.get("detail") or "")]
    decode_ticks = [t for t in base.ticks if "Decoding" in (t.get("detail") or "")]

    assert [t["done"] for t in denoise_ticks] == [1, 2, 3]
    assert all(t["total"] == 3 for t in denoise_ticks)
    assert len(decode_ticks) == 2
    assert [t["done"] for t in decode_ticks] == [1, 2]
    # Every decode tick carries the SAME total — the predicted level count
    # for this request's octree resolution, not the running call count.
    assert len({t["total"] for t in decode_ticks}) == 1


def test_wrapping_is_restored_and_does_not_stack_across_two_renders(monkeypatch, tmp_path):
    """Two renders in sequence on the SAME cached pipeline (`_ensure_
    pipeline` reuses it while `.dit` stays set) must not leave the second
    render's wrapper stacked on the first's, or the first render's `job`
    closed over forever."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)
    pipeline = FakePipeline(denoise_steps=2, decode_levels=1)
    module._loaded["pipeline"] = pipeline

    original_step = pipeline.scheduler.step
    original_query = pipeline.vae._query_sdf_volume

    module.generate(_request(tmp_path, steps=2, job="job-one"))
    assert pipeline.scheduler.step is original_step
    assert pipeline.vae._query_sdf_volume is original_query

    module.generate(_request(tmp_path, steps=2, job="job-two"))
    assert pipeline.scheduler.step is original_step
    assert pipeline.vae._query_sdf_volume is original_query

    # Each render's ticks carry ITS OWN job id — no closure from the first
    # render's wrapper leaked into the second.
    job_one_ticks = [t for t in base.ticks if t["job"] == "job-one"]
    job_two_ticks = [t for t in base.ticks if t["job"] == "job-two"]
    assert any("Denoising" in (t.get("detail") or "") for t in job_one_ticks)
    assert any("Denoising" in (t.get("detail") or "") for t in job_two_ticks)


# ---------------------------------------------------------------- pipeline reuse


def test_a_poisoned_pipeline_is_rebuilt_before_the_next_render(monkeypatch, tmp_path):
    """`ShapePipeline.__call__` frees its own DiT at the end of every call
    it reaches the VAE decode of (`del self.dit; self.dit = None` — upstream,
    unmodified, see `_ensure_pipeline`'s docstring). Reproduced empirically
    against the live worker: the SECOND render in one process crashed with
    `TypeError: 'NoneType' object is not callable`. `_ensure_pipeline` must
    rebuild rather than reuse a pipeline whose `.dit` has gone `None`."""
    base = FakeBase()
    module = load_worker(monkeypatch, base)

    poisoned = FakePipeline()
    poisoned.dit = None  # what a real pipeline looks like after one render
    rebuilt = FakePipeline()
    built = []

    def fake_shape_pipeline_class():
        class _Ctor:
            @staticmethod
            def from_pretrained(_fetched):
                built.append(True)
                return rebuilt
        return _Ctor

    module._loaded["pipeline"] = poisoned
    module._loaded["fetched"] = "/weights/dir"
    monkeypatch.setattr(module, "_shape_pipeline_class", fake_shape_pipeline_class)

    result = module.generate(_request(tmp_path))

    assert built == [True]
    assert module._loaded["pipeline"] is rebuilt
    assert rebuilt.calls  # the rebuilt pipeline is what actually ran
    assert poisoned.calls == []
    assert result["path"]
