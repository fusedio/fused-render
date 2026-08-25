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

#: What the fake graph computes at a PAD position. Deliberately far from any
#: real row and not a multiple of one, so a mean that averaged it in cannot come
#: out parallel to the right answer — `unit_normalize` would hide a merely
#: scaled result. See `FakeSession.run`.
PAD_ROW = -9.0


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
        # `(batch, tokens, dim)`, with `tokens` taken from the feed the way a
        # real graph's output shape is — the prose pooling multiplies this by the
        # attention mask, so a fixed token count here would test a broadcast
        # that never happens in production.
        ids = feed.get("input_ids")
        tokens = ids.shape[1] if ids is not None and getattr(ids, "ndim", 0) > 1 else 4
        # Rank 3, and deliberately not a broadcastable view of `pooled`: a
        # runner reading this instead would produce rows of the wrong length.
        hidden = numpy.stack([pooled] * tokens, axis=1)
        # **The PAD positions carry something else, and that is what makes the
        # mask assertion mean anything.** With every position identical, a mean
        # that divided by the padded WIDTH instead of by the mask sum would
        # differ from a masked one only by a scalar — and `unit_normalize` erases
        # a scalar, so the mocked suite would go green on a runner that ignored
        # the mask entirely. A real graph computes something quite different for
        # a pad token, so this fake does too.
        mask = feed.get("attention_mask")
        if mask is not None and getattr(mask, "ndim", 0) > 1:
            hidden = numpy.where(numpy.asarray(mask)[:, :, None] > 0, hidden,
                                 PAD_ROW)
        by_name = {"last_hidden_state": hidden, "pooler_output": pooled,
                   # A fully-pooled sentence-transformers export publishes this
                   # instead, already the vector.
                   "sentence_embedding": pooled,
                   # …and a graph this runner cannot read at all publishes
                   # neither. Rank 2 on purpose: an unreadable output must fail
                   # by NAME, not by happening to have the wrong shape.
                   "logits": pooled}
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
        self.encoded = None

    def token_to_id(self, token):
        return 7 if token else None

    def enable_truncation(self, max_length):
        self.truncation = max_length

    def enable_padding(self, length=None, pad_id=None, pad_token=None):
        self.padding = {"length": length, "pad_id": pad_id, "pad_token": pad_token}

    def encode_batch(self, texts):
        self.encoded = list(texts)
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


# -- prose encoders ------------------------------------------------------------
#
# The same runner, one session instead of two, no image preprocessing, and a
# retrieval prefix in front of every text. The fake graph here emits
# `last_hidden_state` and NOTHING else, which is what every prose export this
# runner has been checked against really does (`nomic-embed-text-v1.5`,
# `multilingual-e5-small` and `bge-base-en-v1.5`, read out of their own graph
# protobufs — only the first is curated, and the contract has to hold for any
# export rather than just the shortlist) — so the pooling happens here rather
# than in the export, and the mask is what makes it correct.


def _prose_session(outputs=("last_hidden_state",)):
    """A BERT-family export: three inputs, and by default one output.

    Every ONNX input is REQUIRED, so a graph asking for `token_type_ids` is the
    case that proves the feed is built from `get_inputs()` rather than from a
    fixed dict — SigLIP's text tower takes `input_ids` alone.
    """
    return FakeSession(["input_ids", "attention_mask", "token_type_ids"],
                       list(outputs), [[1.0, 2.0, 2.0], [4.0, 0.0, 3.0]])


def _write_prose_snapshot(root, pooling="mean"):
    import json

    onnx = root / "onnx"
    onnx.mkdir(exist_ok=True)
    (onnx / "model.onnx").write_bytes(b"")
    (root / "config.json").write_text(json.dumps({
        "model_type": "bert", "max_position_embeddings": 512}))
    (root / "tokenizer_config.json").write_text(json.dumps({
        "pad_token": "[PAD]", "model_max_length": 512}))
    (root / "tokenizer.json").write_text("{}")
    pooling_dir = root / "1_Pooling"
    pooling_dir.mkdir(exist_ok=True)
    (pooling_dir / "config.json").write_text(json.dumps({
        "pooling_mode_cls_token": pooling == "cls",
        "pooling_mode_mean_tokens": pooling == "mean",
    }))


@pytest.fixture()
def prose(monkeypatch, tmp_path):
    """The runner loaded against a text-only export, through its real `load()`."""
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: str(tmp_path)
    base.serve = lambda **kw: None
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)
    monkeypatch.setitem(sys.modules, "worker_base", base)

    onnxruntime = types.ModuleType("onnxruntime")
    onnxruntime.get_available_providers = lambda: ["CPUExecutionProvider"]
    onnxruntime.InferenceSession = lambda path, providers=None: _prose_session()
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
        "onnx_embed_prose_under_test", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    _write_prose_snapshot(tmp_path)
    module.load("BAAI/bge-base-en-v1.5", str(tmp_path))
    module.fake_tokenizer = fake_tokenizer
    return module


def test_a_prose_export_loads_ONE_session_and_no_vision_tower(prose):
    assert prose._loaded["text"] is not None
    assert prose._loaded.get("vision") is None
    assert prose._loaded["family"] == "text"


def test_prose_texts_produce_one_unit_vector_each(prose):
    result = prose.generate({"texts": ["a cat", "a dog"]})
    assert len(result["vectors"]) == 2
    assert result["dim"] == DIM
    for row in result["vectors"]:
        assert abs(sum(v * v for v in row) ** 0.5 - 1.0) < 1e-6


def test_the_retrieval_PREFIX_reaches_the_tokenizer(prose):
    """The whole of what `kind` does, and the only place it is observable: the
    prefix is applied BEFORE tokenizing, so nothing downstream — not the ids,
    not the vector, not the reply — records that it happened."""
    prose.generate({"texts": ["red shoes"], "kind": "query"})
    assert prose.fake_tokenizer.encoded == [
        "Represent this sentence for searching relevant passages: red shoes"]


def test_the_document_side_of_bge_is_genuinely_UNPREFIXED(prose):
    """bge's card instructs the query only, which is the tie-breaker behind the
    "document" default (`embed_common.DEFAULT_KIND`): a bare call on this family
    embeds text verbatim, exactly as someone who has never heard of prompt
    schemes expects."""
    prose.generate({"texts": ["red shoes"]})
    assert prose.fake_tokenizer.encoded == ["red shoes"]


def test_the_scheme_comes_from_the_model_id(prose):
    assert prose._loaded["scheme"] == "bge"


def test_a_PATHS_request_is_refused_BY_NAME_on_a_prose_model(prose, tmp_path):
    """**Refused, never attempted.** Handing a text encoder pixel values raises
    somewhere inside the session about a tensor rank, which tells a page author
    nothing — and a graph that happened to accept them would embed noise and
    return a plausible vector. The refusal names the MODEL, because the fix is
    to pick a different one."""
    path = tmp_path / "pic.png"
    Image.new("RGB", (4, 4), (1, 2, 3)).save(path)
    with pytest.raises(ValueError) as exc:
        prose.generate({"paths": [str(path)]})
    message = str(exc.value)
    assert "BAAI/bge-base-en-v1.5" in message
    assert "texts" in message


def test_prose_pads_to_the_batchs_LONGEST_not_to_max_length(prose):
    """The opposite of the SigLIP rule, and for the same reason it is stated at
    all: a BERT-family graph takes an `attention_mask` and ignores its pads, so
    padding all 512 positions is 8x the compute for identical vectors. SigLIP's
    text tower has no mask, which is why THAT one must pad to max_length."""
    assert prose.fake_tokenizer.padding["length"] is None
    assert prose.fake_tokenizer.truncation == 512


def test_every_declared_input_is_fed_including_token_type_ids(prose):
    prose.generate({"texts": ["a cat"]})
    assert set(prose._loaded["text"].feeds[0]) == {
        "input_ids", "attention_mask", "token_type_ids"}


def test_mean_pooling_ignores_the_PAD_positions(prose):
    """The mask is the whole of what makes mean pooling correct. Two texts of
    different lengths ride one batch, so the shorter one is padded — and a mean
    that summed the padded positions too would average whatever the graph
    computes for a pad token into every short text's vector.

    **The divisor is not the interesting half, and saying so is the point.**
    Dividing by the batch's padded WIDTH instead of by each row's mask sum
    scales a row by a constant, and `unit_normalize` erases a constant — so that
    variant is not a bug at all. What matters is ZEROING the pad positions
    before the sum, and this test is written to fail on exactly that: `PAD_ROW`
    is far from any real row and not a multiple of one, so a vector that
    averaged it in cannot come out parallel to the right answer.

    Asserted against a hand-computed expectation rather than by inspection: the
    fake's `last_hidden_state` repeats one row across every REAL position and
    carries `PAD_ROW` at the rest, so a mask-aware mean returns that row exactly.
    """
    session = _prose_session()
    prose._loaded["text"] = session
    result = prose.generate({"texts": ["ab", "abcd"]})
    # Both rows are the fake's own vectors, unit-normalized — which is only true
    # if the mean divided by the MASK sum and not by the padded width.
    for row, expected in zip(result["vectors"], ([1.0, 2.0, 2.0], [4.0, 0.0, 3.0])):
        norm = sum(v * v for v in expected) ** 0.5
        for got, want in zip(row, expected):
            assert abs(got - want / norm) < 1e-6


def test_CLS_pooling_is_read_off_the_pooling_config_not_guessed(monkeypatch,
                                                                prose, tmp_path):
    """**Pooling differs per model and the config is the only place it is
    written down.** `bge-base-en-v1.5` pools the CLS token; `multilingual-e5-small`
    and `nomic-embed-text-v1.5` take the masked mean. Mean-pooling a CLS model
    returns a well-shaped vector that is measurably worse, with nothing to
    signal it — so this reads `1_Pooling/config.json`, which every
    sentence-transformers repo ships, rather than assuming one mode.
    """
    _write_prose_snapshot(tmp_path, pooling="cls")
    prose.load("BAAI/bge-base-en-v1.5", str(tmp_path))
    assert prose._loaded["pooling"] == "cls"
    _write_prose_snapshot(tmp_path, pooling="mean")
    prose.load("intfloat/multilingual-e5-small", str(tmp_path))
    assert prose._loaded["pooling"] == "mean"


def test_a_graph_publishing_a_POOLED_output_is_used_directly(prose):
    """"Pooling comes from the export's own graph where present" — some
    sentence-transformers exports emit `sentence_embedding`, already pooled and
    already the vector. Read by NAME like everything else here, never inferred
    from a shape."""
    prose._loaded["text"] = _prose_session(
        outputs=("last_hidden_state", "sentence_embedding"))
    result = prose.generate({"texts": ["a cat"]})
    assert result["dim"] == DIM


def test_a_prose_graph_with_no_readable_output_raises_naming_what_it_has(prose):
    prose._loaded["text"] = _prose_session(outputs=("logits",))
    with pytest.raises(RuntimeError) as exc:
        prose.generate({"texts": ["a cat"]})
    assert "logits" in str(exc.value)
    assert "BAAI/bge-base-en-v1.5" in str(exc.value)


def test_download_pins_the_single_fp32_graph_for_a_prose_export(prose,
                                                                monkeypatch):
    """A text-only export ships `onnx/model.onnx` and the same eight
    quantizations beside it — `nomic-embed-text-v1.5`'s repo is 2.2 GB against a
    0.55 GB fetch — so the pattern list matters here for the identical reason."""
    listing = ["config.json", "tokenizer.json", "vocab.txt",
               "1_Pooling/config.json",
               "onnx/model.onnx", "onnx/model_fp16.onnx", "onnx/model_q4.onnx",
               "onnx/model_quantized.onnx", "model.safetensors"]
    monkeypatch.setattr(prose, "_repo_files", lambda _id: listing)
    seen = {}
    monkeypatch.setattr(prose.worker_base, "download_snapshot",
                        lambda model_id, **kw: seen.update(kw) or "/snap")
    prose.download("BAAI/bge-base-en-v1.5")

    patterns = seen["allow_patterns"]
    assert "onnx/model.onnx" in patterns
    assert "1_Pooling/config.json" in patterns
    # The safetensors are a whole second copy of the weights this engine cannot
    # open, and they are the biggest single file in the repo.
    assert "model.safetensors" not in patterns
    assert not [p for p in patterns
                if any(t in p for t in ("fp16", "q4", "quantized", "int8"))]


def test_the_dual_and_prose_layouts_are_told_apart_by_the_LISTING(prose,
                                                                  monkeypatch):
    """`download()` runs before anything is on the disk, so the repo's file
    listing is the only evidence available — not `config.json`, which is not
    fetched yet. A repo carrying `onnx/text_model.onnx` is a dual encoder and
    gets both towers; one carrying only `onnx/model.onnx` is a prose export and
    gets the one graph."""
    dual = ["onnx/text_model.onnx", "onnx/vision_model.onnx", "onnx/model.onnx"]
    text = ["onnx/model.onnx"]
    assert "onnx/vision_model.onnx" in prose._weight_patterns(dual)
    assert "onnx/model.onnx" not in prose._weight_patterns(dual)
    assert prose._weight_patterns(text) == ("onnx/model.onnx",)
    assert prose._weight_patterns(["README.md"]) == ()


def test_pooler_output_is_deliberately_NOT_a_prose_output():
    """**The trap a future editor is most likely to fall into**, and the reason
    the two families have separate output tuples instead of one shared "read the
    pooled thing" rule.

    A BERT graph may well publish `pooler_output`, and it is the CLS token
    through a tanh-activated dense layer trained for next-sentence prediction —
    not a sentence embedding, and not what any of these models' cards tell you
    to use. Reading it would return a 768-dim unit vector that is simply worse
    at retrieval, which is #813's shape exactly: a plausible wrong answer rather
    than an error. It IS the correct read for a SigLIP tower, which is what
    makes the mistake inviting.

    Imported by path the way the fixtures do, because this asserts on a
    module-level constant and needs no session at all.
    """
    import importlib.util as _il

    spec = _il.spec_from_file_location("onnx_embed_constants", WORKER_PATH)
    module = _il.module_from_spec(spec)
    base = types.ModuleType("worker_base")
    base.serve = lambda **kw: None
    sys.modules["worker_base"] = base
    try:
        spec.loader.exec_module(module)
        assert module._POOLED_OUTPUT == "pooler_output"
        assert module._POOLED_OUTPUT not in module._PROSE_OUTPUTS
        assert module._PROSE_OUTPUTS == ("sentence_embedding", "last_hidden_state")
    finally:
        del sys.modules["worker_base"]


@pytest.fixture()
def pure():
    """The worker module imported by PATH with no session and no fakes.

    Same import-by-path shape as `test_the_pooled_output_name_is_pinned` above,
    and for the same reason: everything below reads only pure functions over
    JSON a repo shipped, so a fixture that built graphs would be scaffolding
    around the thing under test.
    """
    import importlib.util as _il

    spec = _il.spec_from_file_location("onnx_embed_pure", WORKER_PATH)
    module = _il.module_from_spec(spec)
    base = types.ModuleType("worker_base")
    base.serve = lambda **kw: None
    sys.modules["worker_base"] = base
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        del sys.modules["worker_base"]

# -- sequence length: the ceiling clamps, and RoBERTa's +2 offset --------------
#
# Fake configs throughout, deliberately: every case here is a claim a repo makes
# in JSON, and a download would test huggingface's uptime rather than this
# function. The repo each case is drawn from is named so the numbers are
# checkable against a real checkpoint.


def test_a_length_over_the_ceiling_is_CLAMPED_not_discarded(pure):
    """`jinaai/jina-embeddings-v3` declares 8194 — an `xlm-roberta`, which
    `TEXT_EMBED_MODEL_TYPES` admits, so this is reachable and not hypothetical.

    The old test was `value <= _MAX_TEXT_LENGTH`, a filter wearing a clamp's
    docstring: 8194 failed it, fell through every source and landed on 512,
    truncating a long-context encoder to a sixteenth of its context with the
    vectors still coming back unit length.
    """
    assert pure._text_length({"max_position_embeddings": 8194}, {}) == 8192
    assert pure._text_length({}, {"model_max_length": 100_000}) == 8192


def test_the_sentinel_is_discarded_so_a_real_claim_still_wins(pure):
    """The reason the ceiling cannot simply clamp everything. An unstripped
    `model_max_length` sentinel clamped to 8192 would look like an answer; here
    the config's own 8192 is the answer and the sentinel is skipped, and where
    nothing else speaks the floor is."""
    assert pure._text_length({"max_position_embeddings": 8192},
                              {"model_max_length": 10 ** 30}) == 8192
    assert pure._text_length({}, {"model_max_length": 10 ** 30}) == 512


def test_roberta_style_plus_two_does_not_reach_the_graph(pure):
    """**`intfloat/multilingual-e5-large`-shaped: config 514, tokenizer 512.**

    On RoBERTa and XLM-R `max_position_embeddings` is the usable length plus the
    `padding_idx` offset. Truncating at 514 walks position ids past the embedding
    table and dies inside the graph with a raw onnxruntime gather error naming
    nothing actionable, so the min of the two sources is taken rather than the
    config preferred.
    """
    assert pure._text_length({"max_position_embeddings": 514},
                              {"model_max_length": 512}) == 512
    # Also in the other order, so this is a min and not a "believe the tokenizer"
    # rule: a tokenizer shipping the larger number must not raise the ceiling
    # above what the graph has weights for.
    assert pure._text_length({"max_position_embeddings": 512},
                              {"model_max_length": 8192}) == 512


def test_siglip2s_short_text_tower_is_still_read_from_text_config(pure):
    """The dual-encoder path, unchanged and pinned: 64 positions, off
    `text_config`, where a dual encoder declares it."""
    assert pure._text_length(
        {"text_config": {"max_position_embeddings": 64}},
        {"model_max_length": 64}) == 64
    # And the vision-side config around it does not leak in.
    assert pure._text_length(
        {"text_config": {"max_position_embeddings": 64},
         "max_position_embeddings": 8192}, {}) == 64


def test_junk_claims_fall_through_to_the_floor(pure):
    """`bool` is an `int` in Python and `True` would otherwise read as a
    one-token sequence."""
    for claim in (0, -1, True, False, "512", None, 1.5):
        assert pure._text_length({"max_position_embeddings": claim}, {}) == 512
