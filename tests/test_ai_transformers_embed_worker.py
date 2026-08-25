"""The shared transformers embedding runner's own behaviour — what is true of
torch and `embed_common` together, not what `embed_common.py`'s own tests
already cover on their own.

Targets `runners/torch_embed.py` directly, not any of the three folders'
`worker.py` shells — the same choice `tests/test_ai_diffusers_worker.py` makes
for `torch_image.py`, and for the identical reason: `transformers_embed/`,
`transformers_embed_cuda/` and `transformers_embed_rocm/` each hold a
five-line shell that imports this file, so testing a shell would test three
copies of the same five lines rather than the runner itself.

Loaded by PATH with `worker_base` primed in `sys.modules`, exactly as
`tests/test_ai_transformers_worker.py` does: the runner finds its base off
`sys.path` in an interpreter of its own, so importing it the packaged way
(`fused_render.ai.runners.…`) would be testing an import that never ships.
`embed_common` is NOT stubbed — it is stdlib-plus-PIL and both are really
installed here, so the runner's own `sys.path.insert` reaches the real file,
exactly as it does in production.

`torch` IS stubbed: it is not installed in this environment (it lives in the
runner's own venv, built on first use), and the whole point of these tests is
to exercise `generate()` with a MOCKED model rather than a real multi-GB one.
"""
import contextlib
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
    / "fused_render" / "ai" / "runners" / "torch_embed.py"
)


class FakeTorch(types.ModuleType):
    """Only what `generate()` touches: an inference-mode context manager and a
    dtype token to pass through — no tensor math, since the mocked model
    already returns plain nested lists."""

    def __init__(self):
        super().__init__("torch")
        self.float32 = "float32"

    def inference_mode(self):
        return contextlib.nullcontext()


class FakeTensor(dict):
    """What `processor(...)` hands back for one field: `.to(device)` is a
    no-op (the mocked model does not care what device a dict value claims to
    be on)."""

    def to(self, *_args, **_kwargs):
        return self


class FakeFeatures(list):
    """What `model.get_text_features`/`get_image_features` hand back: a plain
    nested list wearing torch's `.to()`/`.tolist()` so the worker's own code —
    written for a real tensor — needs no branch for the fake."""

    def to(self, *_args, **_kwargs):
        return self

    def tolist(self):
        return list(self)


class FakeProcessor:
    """Returns one row of features per text/image, shaped like SigLIP's own
    processor output closely enough for `generate()` to not care which fields
    are real: the worker only reads keys back off the dict to move them
    device-side, never their content."""

    def __call__(self, text=None, images=None, **_kwargs):
        if text is not None:
            return {"input_ids": FakeTensor(rows=len(text)),
                    "attention_mask": FakeTensor(rows=len(text))}
        return {"pixel_values": FakeTensor(rows=len(images))}


class FakeOutput:
    """What transformers 5 hands back from `get_text_features` /
    `get_image_features`: a `BaseModelOutputWithPooling`, NOT the vector.

    **The shape is the point of the fake.** An earlier version of this file
    returned the `FakeFeatures` above directly, which is the transformers 4.x
    contract, and so the suite went green against a model that could not
    exhibit the bug the real one had — `worker._pooled` did not exist and
    `features.to(...)` raised `AttributeError:
    'BaseModelOutputWithPooling' object has no attribute 'to'` on every real
    embed call. `last_hidden_state` is carried too, and deliberately given a
    DIFFERENT rank, so a worker that reached for the wrong field would fail
    here on shape rather than pass with nonsense.
    """

    def __init__(self, pooled):
        self.pooler_output = FakeFeatures(pooled)
        self.last_hidden_state = FakeFeatures([[row] * 4 for row in pooled])


class FakeModel:
    """`get_text_features`/`get_image_features` each return one deterministic
    vector per item, so a test can assert both the COUNT and the SHAPE without
    caring what the numbers mean."""

    def get_text_features(self, **_kwargs):
        return FakeOutput([[1.0, 2.0, 2.0], [4.0, 0.0, 3.0]][:2])

    def get_image_features(self, **_kwargs):
        return FakeOutput([[3.0, 4.0]])


@pytest.fixture()
def worker(monkeypatch):
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: f"/snapshots/{model_id}"
    base.serve = lambda **kw: None
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)
    monkeypatch.setitem(sys.modules, "worker_base", base)
    monkeypatch.setitem(sys.modules, "torch", FakeTorch())

    spec = importlib.util.spec_from_file_location(
        "transformers_embed_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    module._loaded["model"] = FakeModel()
    module._loaded["processor"] = FakeProcessor()
    module._loaded["device"] = "cpu"
    return module


# -- the happy paths -------------------------------------------------------------


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


def test_the_vectors_are_plain_python_floats_not_numpy_or_tensors(worker):
    result = worker.generate({"texts": ["a cat", "a dog"]})
    for row in result["vectors"]:
        for value in row:
            assert type(value) is float  # noqa: E721 - deliberately the exact type


# -- refusals, identical to embed_common's own but exercised through generate() --


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
