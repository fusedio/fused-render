"""The ONNX embedding runner's own behaviour — what is true of `onnxruntime`
and `embed_common` together, not what `test_ai_embed_common.py` already covers
on its own.

Targets `runners/onnx_embed.py` directly, not any of the four folders'
`worker.py` shells — the same choice `tests/test_ai_diffusers_worker.py` makes
for `torch_image.py`, and for the identical reason: `onnx_embed/`,
`onnx_embed_directml/`, `onnx_embed_cuda/` and `onnx_embed_rocm/` each hold a
five-line shell that imports this file, so testing a shell would test four
copies of the same five lines rather than the runner itself.

Loaded by PATH with `worker_base` primed in `sys.modules`, exactly as
`tests/test_ai_transformers_worker.py` does it: the runner finds its base off
`sys.path` in an interpreter of its own, so importing it the packaged way
(`fused_render.ai.runners.…`) would be testing an import that never ships.
`embed_common` is NOT stubbed — it is
stdlib-plus-PIL and both are really installed here, so the runner's own
`sys.path.insert` reaches the real file, exactly as it does in production.
`numpy` is not stubbed either, for a stronger reason: the preprocessing and the
output reads are numpy ARITHMETIC, and a fake array would test the fake's
opinion of a transpose.

`onnxruntime` and `tokenizers` ARE stubbed: neither is installed in this
environment (both live in a runner's own venv, built on first use), and the
whole point of these tests is to exercise `generate()` against a MOCKED session
rather than a real 1.5 GB export.

**The fakes carry outputs of deliberately different RANKS, and that is the
point of this file.** `session.run(None, feed)` returns a plain list in graph
order, so a runner that indexed it blind would read whichever tensor the
exporter emitted first — and for both real SigLIP2 towers that is
`last_hidden_state`, not the vector. #813 shipped exactly that mistake on the
torch side because the fake of the day encoded the wrong contract and the suite
went green against a model that structurally could not exhibit it. Here
`last_hidden_state` comes FIRST in every fake graph and has a third axis, so a
wrong read fails on shape instead of passing with nonsense.
"""
import importlib.util
import re
import sys
import threading
import types
from pathlib import Path

import numpy
import pytest
from PIL import Image

WORKER_PATH = str(
    Path(__file__).resolve().parents[1]
    / "fused_render" / "ai" / "runners" / "onnx_embed.py"
)

#: Not 384: the fake graphs do not care what side length they are handed, and a
#: small one keeps `_preprocess_images`' assertions readable. The runner reads
#: this out of `preprocessor_config.json` (see `_image_settings`), which is what
#: `load()` is given below.
SIDE = 8

#: What both fake towers return, and the shapes are the assertion. `pooled` is
#: `(batch, dim)`; `hidden` is `(batch, tokens, dim)` — a different rank, listed
#: FIRST, so `outputs[0]` is the wrong answer by construction.
DIM = 3


class FakeSpec:
    """An `onnxruntime` input/output descriptor — only `.name` is read."""

    def __init__(self, name):
        self.name = name


class FakeSession:
    """One `InferenceSession`, over a graph whose inputs and outputs are
    declared by name.

    `run()` records the feed it was handed so a test can assert what actually
    reached the graph (the padded length, the pixel shape) rather than only what
    came back out.
    """

    def __init__(self, inputs, outputs, rows):
        self._inputs = [FakeSpec(name) for name in inputs]
        self._outputs = [FakeSpec(name) for name in outputs]
        self._rows = rows
        self.feeds = []

    def get_inputs(self):
        return list(self._inputs)

    def get_outputs(self):
        return list(self._outputs)

    def run(self, requested, feed):
        assert requested is None, "the runner asks for every output, by contract"
        self.feeds.append(feed)
        batch = len(next(iter(feed.values())))
        pooled = numpy.asarray([self._rows[at % len(self._rows)]
                                for at in range(batch)], dtype=numpy.float32)
        # Rank 3, and deliberately not a broadcastable view of `pooled`: a
        # runner reading this instead would produce rows of the wrong length.
        hidden = numpy.stack([pooled] * 4, axis=1)
        by_name = {"last_hidden_state": hidden, "pooler_output": pooled}
        return [by_name[spec.name] for spec in self._outputs]


def _text_session():
    """SigLIP2's real text tower, in miniature: `input_ids` alone in, and
    `last_hidden_state` BEFORE `pooler_output` out — the order read straight out
    of `onnx-community/siglip2-base-patch16-384-ONNX`'s own graph."""
    return FakeSession(["input_ids"], ["last_hidden_state", "pooler_output"],
                       [[1.0, 2.0, 2.0], [4.0, 0.0, 3.0]])


def _vision_session():
    return FakeSession(["pixel_values"], ["last_hidden_state", "pooler_output"],
                       [[3.0, 4.0, 0.0]])


class FakeEncoding:
    def __init__(self, ids, length):
        self.ids = list(ids) + [0] * (length - len(ids))
        self.attention_mask = [1] * len(ids) + [0] * (length - len(ids))
        self.type_ids = [0] * length


class FakeTokenizer:
    """`tokenizers.Tokenizer`, narrowed to what `_tokenizer` configures and
    `_text_vectors` calls — and it RECORDS the padding it was configured with,
    because `padding="max_length"` is a rule this runner has to state explicitly
    and nothing else would notice if it stopped."""

    def __init__(self):
        self.padding = None
        self.truncation = None

    def token_to_id(self, token):
        return 7 if token else None

    def enable_truncation(self, max_length):
        self.truncation = max_length

    def enable_padding(self, length=None, pad_id=None, pad_token=None):
        self.padding = {"length": length, "pad_id": pad_id, "pad_token": pad_token}

    def encode_batch(self, texts):
        length = (self.padding or {}).get("length") or max(len(t) for t in texts)
        return [FakeEncoding(range(1, min(len(text), length) + 1), length)
                for text in texts]


@pytest.fixture()
def worker(monkeypatch, tmp_path):
    """The runner module, with both fake sessions and a fake tokenizer loaded
    through its real `load()`.

    `load()` runs for real against a snapshot directory of empty files plus real
    JSON — so the config reads, the `os.path.isfile` guards and the placement
    are all exercised rather than bypassed, and only the two libraries this
    environment does not have are faked.
    """
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: str(tmp_path)
    base.serve = lambda **kw: None
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)
    monkeypatch.setitem(sys.modules, "worker_base", base)

    onnxruntime = types.ModuleType("onnxruntime")
    onnxruntime.get_available_providers = lambda: ["CPUExecutionProvider"]
    made = []

    def session(path, providers=None):
        made.append((path, providers))
        return _vision_session() if "vision" in path else _text_session()

    onnxruntime.InferenceSession = session
    monkeypatch.setitem(sys.modules, "onnxruntime", onnxruntime)

    tokenizers = types.ModuleType("tokenizers")
    fake_tokenizer = FakeTokenizer()

    class Tokenizer:
        @staticmethod
        def from_file(_path):
            return fake_tokenizer

    tokenizers.Tokenizer = Tokenizer
    monkeypatch.setitem(sys.modules, "tokenizers", tokenizers)

    spec = importlib.util.spec_from_file_location(
        "onnx_embed_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base

    _write_snapshot(tmp_path)
    module.load("onnx-community/siglip2-base-patch16-384-ONNX", str(tmp_path))
    module.made = made
    module.fake_tokenizer = fake_tokenizer
    return module


def _write_snapshot(root):
    """A dual-encoder export's file layout: real JSON, empty graphs.

    The graphs are empty because the fake `InferenceSession` never opens them —
    but they must EXIST, because `_session` refuses a missing file by name and
    that refusal is the one thing a test of the download's pattern list has to
    be able to trust.
    """
    import json

    onnx = root / "onnx"
    onnx.mkdir(exist_ok=True)
    (onnx / "text_model.onnx").write_bytes(b"")
    (onnx / "vision_model.onnx").write_bytes(b"")
    (root / "config.json").write_text(json.dumps({
        "model_type": "siglip",
        "text_config": {"max_position_embeddings": 64},
        "vision_config": {"image_size": SIDE},
    }))
    (root / "tokenizer_config.json").write_text(json.dumps({
        "pad_token": "</s>", "model_max_length": 64}))
    (root / "tokenizer.json").write_text("{}")
    (root / "preprocessor_config.json").write_text(json.dumps({
        "size": {"height": SIDE, "width": SIDE},
        "image_mean": [0.5, 0.5, 0.5],
        "image_std": [0.5, 0.5, 0.5],
        "rescale_factor": 1 / 255,
    }))


# -- the happy paths -------------------------------------------------------------


def test_texts_produce_one_unit_vector_each(worker):
    result = worker.generate({"texts": ["a cat", "a dog"]})
    assert len(result["vectors"]) == 2
    assert result["dim"] == DIM
    for row in result["vectors"]:
        norm = sum(v * v for v in row) ** 0.5
        assert abs(norm - 1.0) < 1e-6


def test_paths_open_a_real_image_and_return_one_vector(worker, tmp_path):
    path = tmp_path / "pic.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(path)
    result = worker.generate({"paths": [str(path)]})
    assert len(result["vectors"]) == 1
    assert result["dim"] == DIM
    norm = sum(v * v for v in result["vectors"][0]) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_the_vectors_are_plain_python_floats_not_numpy_or_tensors(worker):
    result = worker.generate({"texts": ["a cat", "a dog"]})
    for row in result["vectors"]:
        for value in row:
            assert type(value) is float  # noqa: E721 - deliberately the exact type


# -- the output-tensor read, which is the #813 repeat risk -----------------------


def test_the_pooled_output_is_read_by_name_not_by_position(worker):
    """`last_hidden_state` is output 0 in the real exports and in both fakes.

    A runner that read `outputs[0]` would come back with rank-3 rows here, and
    `dim` would be 4 (the token axis) rather than 3. That is the whole shape of
    #813: a plausible, wrong vector instead of an error.
    """
    result = worker.generate({"texts": ["a cat"]})
    assert result["dim"] == DIM
    assert all(isinstance(value, float) for value in result["vectors"][0])


def test_a_graph_with_no_pooled_output_raises_naming_the_model_and_the_outputs(
        worker):
    """The refusal `_output_index` exists for — and it must NAME what it found,
    because "the model does not work" is not actionable and "your export
    publishes ['sentence_embedding']" is."""
    worker._loaded["text"] = FakeSession(
        ["input_ids"], ["last_hidden_state"], [[1.0, 2.0, 2.0]])
    with pytest.raises(RuntimeError) as exc:
        worker.generate({"texts": ["a cat"]})
    assert "pooler_output" in str(exc.value)
    assert "last_hidden_state" in str(exc.value)
    assert "siglip2-base-patch16-384-ONNX" in str(exc.value)


def test_the_vision_tower_is_read_by_name_too(worker, tmp_path):
    """Asserted separately rather than trusted from the text case: two towers,
    two sessions, and a runner that asserted only one of them would pass every
    text test while returning nonsense for `paths`."""
    worker._loaded["vision"] = FakeSession(
        ["pixel_values"], ["last_hidden_state"], [[3.0, 4.0, 0.0]])
    path = tmp_path / "pic.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(path)
    with pytest.raises(RuntimeError, match="vision tower"):
        worker.generate({"paths": [str(path)]})


# -- the padding rule -----------------------------------------------------------


def test_the_text_tower_pads_to_max_length_not_to_the_batchs_longest(worker):
    """SigLIP was trained with every sequence padded to the tokenizer's max
    length. `tokenizers` pads to the batch's longest by default, and the
    difference does not raise — it shifts the vectors. So the configured length
    is asserted directly."""
    assert worker._TEXT_PADDING == "max_length"
    assert worker.fake_tokenizer.padding["length"] == 64
    assert worker.fake_tokenizer.truncation == 64


def test_the_sequence_length_comes_off_the_config_not_a_constant(worker,
                                                                 tmp_path):
    """64 is SigLIP2's text tower and a prose encoder's is hundreds — a constant
    here would truncate one or ask the other for positions its graph has no
    weights for."""
    config = {"text_config": {"max_position_embeddings": 12}}
    assert worker._text_length(config, {}) == 12
    # The `1e30` sentinel some exporters leave in `model_max_length`: padding
    # every sequence to it is an allocation failure, not a slow call.
    assert worker._text_length({}, {"model_max_length": 10 ** 30}) == 512


def test_only_the_inputs_the_graph_declares_are_fed(worker):
    """SigLIP2's text graph takes `input_ids` alone; a BERT-family one also
    wants `attention_mask` and `token_type_ids`. Every ONNX input is REQUIRED,
    so the feed is built from the graph rather than from a fixed dict."""
    worker.generate({"texts": ["a cat"]})
    assert set(worker._loaded["text"].feeds[0]) == {"input_ids"}


def test_a_graph_asking_for_an_input_this_runner_cannot_supply_says_so(worker):
    worker._loaded["text"] = FakeSession(
        ["input_ids", "sinusoidal_positions"],
        ["last_hidden_state", "pooler_output"], [[1.0, 2.0, 2.0]])
    with pytest.raises(RuntimeError, match="sinusoidal_positions"):
        worker.generate({"texts": ["a cat"]})


# -- the download's pattern list, which is the 11.42 GB risk --------------------


def test_download_pins_the_fp32_graphs_and_nothing_else(worker, monkeypatch):
    """The bare `download_snapshot(model_id)` call the torch runner makes would
    fetch 11.42 GB out of this repo — eight quantizations of each tower. The
    pattern list is the fix, and it is asserted against the real listing."""
    listing = [
        "config.json", "preprocessor_config.json", "tokenizer.json",
        "tokenizer_config.json", "special_tokens_map.json",
        "quantize_config.json", "tokenizer.model", "README.md",
        "onnx/model.onnx", "onnx/model_fp16.onnx", "onnx/model_q4.onnx",
        "onnx/text_model.onnx", "onnx/text_model_fp16.onnx",
        "onnx/text_model_int8.onnx", "onnx/vision_model.onnx",
        "onnx/vision_model_q4f16.onnx",
    ]
    monkeypatch.setattr(worker, "_repo_files", lambda _id: listing)
    seen = {}
    monkeypatch.setattr(worker.worker_base, "download_snapshot",
                        lambda model_id, **kw: seen.update(kw) or "/snap")
    worker.download("onnx-community/siglip2-base-patch16-384-ONNX")

    patterns = seen["allow_patterns"]
    assert "onnx/text_model.onnx" in patterns
    assert "onnx/vision_model.onnx" in patterns
    # The merged graph is a third full copy of both towers and this runner opens
    # two separate sessions, so it must not be fetched either.
    assert "onnx/model.onnx" not in patterns
    quantized = [p for p in patterns
                 if any(tag in p for tag in ("fp16", "int8", "q4", "bnb4",
                                            "uint8", "quantized"))]
    assert quantized == []


def test_download_fetches_the_external_data_sidecar_when_the_repo_ships_one(
        worker, monkeypatch):
    """The so400m export's towers are over the 2 GB protobuf limit, so its
    `onnx/text_model.onnx` is 0.6 MB of graph pointing at a 2.8 GB
    `onnx/text_model.onnx_data`. A pattern list naming only the graph downloads
    a model that cannot open."""
    listing = ["config.json", "tokenizer.json",
               "onnx/text_model.onnx", "onnx/text_model.onnx_data",
               "onnx/vision_model.onnx"]
    monkeypatch.setattr(worker, "_repo_files", lambda _id: listing)
    seen = {}
    monkeypatch.setattr(worker.worker_base, "download_snapshot",
                        lambda model_id, **kw: seen.update(kw) or "/snap")
    worker.download("onnx-community/siglip2-so400m-patch14-384-ONNX")

    patterns = seen["allow_patterns"]
    assert "onnx/text_model.onnx_data" in patterns
    # …and no sidecar is invented for the tower that has none.
    assert "onnx/vision_model.onnx_data" not in patterns


def test_a_repo_that_is_not_an_export_is_refused_by_name(worker, monkeypatch):
    monkeypatch.setattr(worker, "_repo_files",
                        lambda _id: ["config.json", "model.safetensors"])
    with pytest.raises(RuntimeError, match="google/siglip2-base-patch16-384"):
        worker.download("google/siglip2-base-patch16-384")


# -- placement -----------------------------------------------------------------


def test_the_cpu_build_reports_cpu(worker):
    assert worker._loaded["device"] == "cpu"
    assert worker.worker_base.recorded["device"] == "cpu"


def test_an_accelerated_provider_wins_and_keeps_cpu_as_a_fallback(worker,
                                                                 monkeypatch):
    """onnxruntime falls back per OPERATOR, so the chain is "the best provider,
    then CPU" rather than one provider — and `device` names the head of it."""
    monkeypatch.setitem(
        sys.modules, "onnxruntime",
        _providers_module(["CUDAExecutionProvider", "CPUExecutionProvider"]))
    chain, device = worker._placement()
    assert chain == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert device == "cuda"


def test_every_registered_provider_maps_to_a_device_word(worker, monkeypatch):
    for provider, expected in (("ROCMExecutionProvider", "rocm"),
                               ("DmlExecutionProvider", "directml")):
        monkeypatch.setitem(
            sys.modules, "onnxruntime",
            _providers_module([provider, "CPUExecutionProvider"]))
        assert worker._placement()[1] == expected


def _providers_module(providers):
    module = types.ModuleType("onnxruntime")
    module.get_available_providers = lambda: list(providers)
    module.InferenceSession = lambda *a, **kw: None
    return module


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
