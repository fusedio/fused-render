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
    # What `load()` would have recorded for a SigLIP checkpoint. Set here
    # because these tests drive `generate()` with a mocked model rather than
    # loading one, and `generate()` now forks on the family.
    module._loaded["family"] = module._DUAL
    module._loaded["scheme"] = "none"
    module._loaded["model_id"] = "google/siglip2-base-patch16-384"
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


def test_load_hands_the_library_the_repo_id_and_not_the_snapshot_path(
        worker, monkeypatch, tmp_path):
    """The one place this runner cannot mirror `mlx_text.worker`.

    mlx-embeddings 0.1.x reads the vision tower's geometry out of the REPO NAME
    (`re.search(r"patch\\d+-(\\d+)", path_to_repo)` in its `load_model`), so a
    content-addressed snapshot directory makes that regex return None and the
    load dies on `AttributeError: 'NoneType' object has no attribute 'group'`.
    Handing it `path` looks right, matches every sibling runner, and fails on
    the real model with a message that names neither cause nor cure — so the
    argument is pinned here rather than left to be rediscovered.
    """
    import json

    seen = []
    monkeypatch.setattr(worker, "_mlx_load", lambda arg: (seen.append(arg), (1, 2))[1])
    # `path` is not unused any more — `load()` reads `config.json` out of it to
    # decide the family — so the snapshot has to exist. It is still a
    # content-addressed directory name, which is the point of the test.
    snapshot = tmp_path / "snapshots" / "f775b65a79762255128c981547af89addcfe0f88"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text(json.dumps({"model_type": "siglip"}))

    worker.load("google/siglip2-base-patch16-384", str(snapshot))

    assert seen == ["google/siglip2-base-patch16-384"]


# -- prose encoders ------------------------------------------------------------
#
# The same folder, the same venv, the same resident slot — one engine that reads
# both a SigLIP checkpoint and a BERT-family one, because mlx-embeddings ships
# modules for both and dispatches on `model_type` itself. PR #780 put the text
# encoders in a SECOND folder (`mlx_text_embed/`), which was right there and
# wrong here: that folder existed to buy a second resident slot for a second
# CAPABILITY, and under one capability a second slot is not a feature, it is two
# models resident where the app promises one.


class FakeBaseModelOutput:
    """`mlx_embeddings.models.base.BaseModelOutput`, narrowed to the one field
    this runner reads.

    `text_embeds` carries the POOLED, L2-normalized sentence vector — mean for
    bert/xlm_roberta, config-driven for modernbert — which is why the runner has
    no pooling branch. `last_hidden_state` is carried too, at a DIFFERENT RANK,
    so a runner that reached for the wrong field fails here on shape rather than
    passing with nonsense (the #813 discipline, applied to this seam).
    """

    def __init__(self, pooled):
        self.text_embeds = pooled
        self.last_hidden_state = [[row] * 4 for row in pooled]


class FakeProseModel:
    """`Model.__call__(input_ids, attention_mask=...) -> BaseModelOutput`, and
    it RECORDS what it was called with — the sequence cap is only observable in
    the tokenizer call, and the field read is only observable here."""

    def __init__(self, output_field="text_embeds"):
        self.calls = []
        self._field = output_field

    def __call__(self, input_ids, attention_mask=None):
        self.calls.append((input_ids, attention_mask))
        output = FakeBaseModelOutput([[1.0, 2.0, 2.0], [4.0, 0.0, 3.0]][
            :len(getattr(input_ids, "data", input_ids))])
        if self._field != "text_embeds":
            del output.text_embeds
        return output


class FakeProseTokenizer:
    """`TokenizerWrapper.batch_encode_plus`, which is how upstream's own
    "Multiple Texts Comparison" example encodes — and it records `max_length`,
    since a cap taken from a constant instead of the config is invisible
    otherwise."""

    def __init__(self):
        self.calls = []

    def batch_encode_plus(self, texts, **kwargs):
        self.calls.append({"texts": list(texts), **kwargs})
        return {"input_ids": [[0]] * len(texts),
                "attention_mask": [[1]] * len(texts)}


@pytest.fixture()
def prose(worker):
    """The same module, with a prose checkpoint's state on it instead."""
    worker._loaded.clear()
    worker._loaded["model"] = FakeProseModel()
    worker._loaded["tokenizer"] = FakeProseTokenizer()
    worker._loaded["family"] = worker._TEXT
    worker._loaded["scheme"] = "bge"
    worker._loaded["length"] = 512
    worker._loaded["model_id"] = "BAAI/bge-base-en-v1.5"
    return worker


def test_prose_texts_produce_one_unit_vector_each(prose):
    result = prose.generate({"texts": ["a cat", "a dog"]})
    assert len(result["vectors"]) == 2
    assert result["dim"] == 3
    for row in result["vectors"]:
        assert abs(sum(v * v for v in row) ** 0.5 - 1.0) < 1e-9


def test_the_pooled_field_is_read_by_NAME(prose):
    """`text_embeds` is the ONE seam this whole path rests on: every
    `Model.__call__` in `mlx_embeddings/models/` returns a `BaseModelOutput`
    whose `text_embeds` is already pooled and already normalized, which is why
    there is no pooling branch here. `last_hidden_state` sits beside it at a
    different rank — reading that one would produce rows of the wrong length
    rather than an error."""
    result = prose.generate({"texts": ["a cat"]})
    assert result["dim"] == 3


def test_a_renamed_field_raises_with_the_FIELD_NAME_in_the_message(prose):
    """Reachable only if an upstream minor renames it inside the manifest's
    ceiling — and then the message has to say which field went, not
    `AttributeError` on a dataclass the reader cannot see."""
    prose._loaded["model"] = FakeProseModel(output_field="embeddings")
    with pytest.raises(RuntimeError) as exc:
        prose.generate({"texts": ["a cat"]})
    assert "text_embeds" in str(exc.value)


def test_the_sequence_cap_comes_from_the_CHECKPOINT_and_not_a_constant(prose):
    """PR #780 used a flat `_MAX_LENGTH = 512`, and its own docstring conceded
    the cost: correct for the BERT-family encoders that trained at 512, LOSSY
    for anything longer — a ModernBERT trains at 8192 and a long passage would
    be silently cut at a sixteenth of it. The config states the real number, so
    it is read."""
    prose.generate({"texts": ["a cat"]})
    assert prose._loaded["model"].calls
    assert prose._loaded["tokenizer"].calls[0]["max_length"] == 512

    prose._loaded["length"] = 8192
    prose.generate({"texts": ["a cat"]})
    assert prose._loaded["tokenizer"].calls[-1]["max_length"] == 8192


def test_the_length_is_read_off_the_config_with_a_sane_ceiling(worker):
    assert worker._text_length({"max_position_embeddings": 8192}) == 8192
    assert worker._text_length({"max_position_embeddings": 512}) == 512
    # The `1e30` sentinel some exporters leave in, and an absent field: both
    # fall back rather than asking the tokenizer to pad to infinity.
    assert worker._text_length({"max_position_embeddings": 10 ** 30}) == 512
    assert worker._text_length({}) == 512


def test_the_retrieval_PREFIX_reaches_the_tokenizer(prose):
    prose.generate({"texts": ["red shoes"], "kind": "query"})
    assert prose._loaded["tokenizer"].calls[0]["texts"] == [
        "Represent this sentence for searching relevant passages: red shoes"]


def test_the_document_side_of_bge_is_genuinely_UNPREFIXED(prose):
    prose.generate({"texts": ["red shoes"]})
    assert prose._loaded["tokenizer"].calls[0]["texts"] == ["red shoes"]


def test_a_PATHS_request_is_refused_BY_NAME_on_a_prose_model(prose, tmp_path):
    path = tmp_path / "pic.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(path)
    with pytest.raises(ValueError) as exc:
        prose.generate({"paths": [str(path)]})
    assert "BAAI/bge-base-en-v1.5" in str(exc.value)
    assert "texts" in str(exc.value)


def test_prose_generation_pins_the_shared_streams_too(prose):
    prose.generate({"texts": ["a cat"]})
    assert prose._STREAMS


def test_load_records_the_family_off_the_checkpoint_config(worker, monkeypatch,
                                                           tmp_path):
    """One fork, decided once, in `load()` — off `formats`' own model-type sets
    rather than a second reading of `model_type` here, so the runner and the AI
    Models page cannot classify the same repo differently."""
    import json

    monkeypatch.setattr(worker, "_mlx_load", lambda _arg: ("MODEL", "TOKENIZER"))

    (tmp_path / "config.json").write_text(json.dumps({
        "model_type": "siglip", "text_config": {"max_position_embeddings": 64}}))
    worker.load("google/siglip2-base-patch16-384", str(tmp_path))
    assert worker._loaded["family"] == worker._DUAL
    assert worker._loaded["processor"] == "TOKENIZER"

    (tmp_path / "config.json").write_text(json.dumps({
        "model_type": "bert", "max_position_embeddings": 512}))
    worker.load("BAAI/bge-base-en-v1.5", str(tmp_path))
    assert worker._loaded["family"] == worker._TEXT
    assert worker._loaded["tokenizer"] == "TOKENIZER"
    assert worker._loaded["scheme"] == "bge"
    assert worker._loaded["length"] == 512


def test_there_is_no_second_mlx_embedding_FOLDER():
    """PR #780's `mlx_text_embed/` is deliberately not carried over, and this is
    the assertion that keeps it from creeping back.

    That folder existed because the capability was SPLIT: `embeddings` and
    `embed-text` were two capabilities, each holding one resident model, so two
    folders meant two slots. Unified, a second folder would be a second venv
    (another mlx-embeddings install), a second resident slot, and a second copy
    of `_pin_stream` — and the extra slot is not a feature: the app's contract is
    one resident model per capability, and a Mac holding both a SigLIP and a
    BERT at once is over budget in exactly the way that contract exists to
    prevent.
    """
    runners = Path(WORKER_PATH).parents[1]
    embed_folders = sorted(
        entry.name for entry in runners.iterdir()
        if entry.is_dir() and "embed" in entry.name)
    assert embed_folders == ["mlx_embed", "onnx_embed", "onnx_embed_cuda",
                             "onnx_embed_directml", "onnx_embed_rocm"], embed_folders
