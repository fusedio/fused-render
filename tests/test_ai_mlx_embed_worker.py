"""The MLX embedding runner's own behaviour — what is true of `mlx.core` and
`embed_common` together.

Loaded by PATH with `worker_base` primed in `sys.modules`, exactly as
`tests/test_ai_mlx_worker.py` does. `mlx.core` is stubbed the same shape that
file's `FakeMlxCore` is (two devices, their streams, and now an `array` class
standing in for `mx.array`) — `mlx` is not installed here, and the point is to
exercise `generate()` against a MOCKED model, never a real one.
"""
import importlib.util
import sys
import threading
import types
from pathlib import Path

import re

import pytest
from PIL import Image

WORKER_PATH = str(
    Path(__file__).resolve().parents[1]
    / "fused_render" / "ai" / "runners" / "mlx_embed" / "worker.py"
)


class FakeMxArray:
    """Stands in for `mx.array` — both the constructor `mx.array(x)` is called
    with, and the class `isinstance(array, mx.array)` checks against in
    `worker._to_lists`."""

    def __init__(self, data):
        self.data = data

    def astype(self, _dtype):
        return self

    def tolist(self):
        return self.data


class FakeMlxCore(types.ModuleType):
    """The same double `tests/test_ai_mlx_worker.py`'s `FakeMlxCore` keeps for
    `_pin_stream`, plus `.array`/`.float32` for `_to_lists`."""

    def __init__(self):
        super().__init__("mlx.core")
        self.cpu = "CPU"
        self.gpu = "GPU"
        self.float32 = "float32"
        self.array = FakeMxArray
        self.made = []
        self.pinned = []
        self._lock = threading.Lock()

    def default_device(self):
        return self.gpu

    def new_thread_unsafe_stream(self, device):
        with self._lock:
            self.made.append(device)
            return f"SHARED-{device}-STREAM"

    def set_default_stream(self, stream):
        with self._lock:
            self.pinned.append((threading.current_thread().name, stream))


class FakeProcessor:
    """`processor(text=..., ...)` / `processor(images=..., ...)`, `np`-shaped —
    the worker only reads the keys back off to wrap them in `mx.array`, never
    the content, so a plain Python list stands in for a real numpy array."""

    def __call__(self, text=None, images=None, **_kwargs):
        if text is not None:
            return {"input_ids": [[0]] * len(text), "attention_mask": [[1]] * len(text)}
        return {"pixel_values": [[0]] * len(images)}


class FakeModel:
    def get_text_features(self, **_kwargs):
        return [[1.0, 2.0, 2.0], [4.0, 0.0, 3.0]]

    def get_image_features(self, **_kwargs):
        return [[3.0, 4.0]]


@pytest.fixture()
def worker(monkeypatch):
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: f"/snapshots/{model_id}"
    base.serve = lambda **kw: None
    monkeypatch.setitem(sys.modules, "worker_base", base)

    mlx_core = FakeMlxCore()
    mlx = types.ModuleType("mlx")
    mlx.core = mlx_core
    monkeypatch.setitem(sys.modules, "mlx", mlx)
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)

    spec = importlib.util.spec_from_file_location(
        "mlx_embed_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._loaded["model"] = FakeModel()
    module._loaded["processor"] = FakeProcessor()
    return module


def test_texts_produce_one_unit_vector_each(worker):
    result = worker.generate({"texts": ["a cat", "a dog"]})
    assert len(result["vectors"]) == 2
    assert result["dim"] == 3
    for row in result["vectors"]:
        norm = sum(v * v for v in row) ** 0.5
        assert abs(norm - 1.0) < 1e-9


def test_paths_open_a_real_image_and_return_one_vector(worker, tmp_path):
    path = tmp_path / "pic.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(path)
    result = worker.generate({"paths": [str(path)]})
    assert len(result["vectors"]) == 1
    assert result["dim"] == 2
    norm = sum(v * v for v in result["vectors"][0]) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_a_batch_over_the_cap_is_refused(worker):
    with pytest.raises(ValueError, match="64"):
        worker.generate({"texts": ["x"] * 65})


def test_an_unreadable_path_names_the_file(worker, tmp_path):
    missing = tmp_path / "nope.png"
    with pytest.raises(ValueError, match=re.escape(str(missing))):
        worker.generate({"paths": [str(missing)]})


def test_no_model_loaded_is_a_plain_runtime_error(worker):
    worker._loaded.clear()
    with pytest.raises(RuntimeError, match="no model is loaded"):
        worker.generate({"texts": ["a cat"]})


def test_generate_pins_the_shared_streams(worker):
    """`_pin_stream` runs first thing in `generate()`, exactly as `load()`
    does — the same abort `mlx_text/worker.py`'s docstring documents is what
    this guards against on the generation path too."""
    worker.generate({"texts": ["a cat"]})
    assert worker._STREAMS  # something was pinned, on at least one device


def test_load_hands_the_library_the_repo_id_and_not_the_snapshot_path(worker, monkeypatch):
    """The one place this runner cannot mirror `mlx_text.worker`.

    mlx-embeddings 0.1.x reads the vision tower's geometry out of the REPO NAME
    (`re.search(r"patch\\d+-(\\d+)", path_to_repo)` in its `load_model`), so a
    content-addressed snapshot directory makes that regex return None and the
    load dies on `AttributeError: 'NoneType' object has no attribute 'group'`.
    Handing it `path` looks right, matches every sibling runner, and fails on
    the real model with a message that names neither cause nor cure — so the
    argument is pinned here rather than left to be rediscovered.
    """
    seen = []
    monkeypatch.setattr(worker, "_mlx_load", lambda arg: (seen.append(arg), (1, 2))[1])

    worker.load("google/siglip2-base-patch16-384",
                "/snapshots/f775b65a79762255128c981547af89addcfe0f88")

    assert seen == ["google/siglip2-base-patch16-384"]
