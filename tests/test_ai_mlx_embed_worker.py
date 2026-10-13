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
import os
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
    the content, so a plain Python list stands in for a real numpy array.

    `calls` records every text invocation's kwargs, exactly `FakeProseTokenizer`
    does below — the SigLIP2 regression is only observable in whether
    `max_length` reaches this call at all, so it has to be recorded rather than
    only its return value.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, text=None, images=None, **kwargs):
        if text is not None:
            self.calls.append({"text": list(text), **kwargs})
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
    # SigLIP2's real text tower — what `load()` would have resolved via
    # `_text_length`'s architectural default, since the real checkpoint's
    # config declares no `max_position_embeddings` anywhere.
    module._loaded["length"] = 64
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
    assert worker._text_length({"max_position_embeddings": 8192},
                                worker._TEXT, "test-model") == 8192
    assert worker._text_length({"max_position_embeddings": 512},
                                worker._TEXT, "test-model") == 512
    # The `1e30` sentinel some exporters leave in, and an absent field: both
    # fall back rather than asking the tokenizer to pad to infinity — for a
    # `model_type` with no architectural default and a non-dual family, the
    # floor is still `_DEFAULT_TEXT_LENGTH`.
    assert worker._text_length({"max_position_embeddings": 10 ** 30},
                                worker._TEXT, "test-model") == 512
    assert worker._text_length({}, worker._TEXT, "test-model") == 512


# -- SigLIP2's undeclared 64-position text tower -------------------------------
#
# `mlx_embeddings.utils.load` hands a dual checkpoint's config to
# `transformers.AutoProcessor.from_pretrained`, which for SigLIP is a plain
# `SiglipTokenizer`. That tokenizer's OWN `model_max_length` — 64 by the class
# signature's default — is overwritten the moment `tokenizer_config.json`
# supplies one, and every curated SigLIP2 row's `tokenizer_config.json` carries
# the unstripped `1e30` sentinel. transformers' own
# `_get_padding_truncation_strategies` then treats `padding="max_length"` with
# no explicit `max_length` and `model_max_length` above `LARGE_INTEGER` as
# `DO_NOT_PAD` — no padding AND no truncation — so a call that never states
# `max_length` explicitly does not silently pad to 512 the way `onnx_embed`
# does; it pads to nothing at all, and a batch of unequal-length texts fails to
# stack into a rectangular array. Either way the fix is the same: state the
# length explicitly, off the checkpoint's architecture rather than the
# library's own fallback.


def test_siglip2_curated_rows_resolve_to_64_not_512(worker):
    """A SigLIP2 config with nothing declared must resolve to the real
    64-position text tower, not `_DEFAULT_TEXT_LENGTH`. A test that only
    checked "not the sentinel" would have passed throughout this bug's life —
    512 is also not the sentinel, and is exactly the wrong answer here."""
    config = {"model_type": "siglip",
              "text_config": {"model_type": "siglip_text_model",
                               "vocab_size": 256000}}
    for model_id in ("google/siglip2-base-patch16-384",
                      "mlx-community/siglip2-so400m-patch16-384"):
        assert worker._text_length(config, worker._DUAL, model_id) == 64


def test_a_dual_encoder_with_no_length_anywhere_refuses_by_name(worker):
    """No curated MLX row is a `clip` one (`mlx-embeddings` ships no `clip`
    module at all), so there is no architectural default for it — a dual
    encoder that declares neither a length nor a recognised architecture must
    refuse rather than pad to `_DEFAULT_TEXT_LENGTH` and risk the SigLIP2
    gather failure this module exists to prevent."""
    with pytest.raises(RuntimeError, match="some/clip-export"):
        worker._text_length({"model_type": "clip"}, worker._DUAL,
                             "some/clip-export")


def test_the_dual_text_call_states_max_length_explicitly(worker):
    """The seam the regression actually lived in: `_text_vectors` must hand
    the processor an explicit `max_length`, not trust the tokenizer's own
    `model_max_length` — that attribute is silently the SigLIP2 sentinel on
    every curated row, and relying on it produces no padding at all rather
    than a padded-to-512 vector."""
    worker.generate({"texts": ["a cat", "a dog"]})
    assert worker._loaded["processor"].calls[0]["max_length"] == 64


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


def test_load_reads_the_CURATED_conversions_own_config_shape(worker, monkeypatch,
                                                              tmp_path):
    """The curated MLX prose row, driven through `load()` with the config that
    repo really ships.

    **Every field here was read off
    `mlx-community/nomicai-modernbert-embed-base-bf16` itself** (2026-08-25), not
    invented, because each one is a way the row could be silently wrong:

    * `model_type: "modernbert"` — in `formats.MLX_EMBED_MODEL_TYPES`, so the
      family resolves to `_TEXT` and the prose path serves it. A conversion whose
      `model_type` had been rewritten would land in `_DUAL` or raise.
    * `max_position_embeddings: 8192`, and the repo's `sentence_bert_config.json`
      agrees at `max_seq_length: 8192` — which is why the catalog note claims
      8192 rather than a number nobody read. It is also exactly
      `_MAX_TEXT_LENGTH`, so `_text_length` returns it unclamped; a conversion
      claiming more would be clamped rather than trusted.
    * `classifier_pooling: "mean"` — read by mlx-embeddings' own modernbert
      module (`models/modernbert.py` pools on `config.classifier_pooling` and
      returns an already-normalized `text_embeds`), which is why this runner has
      no pooling branch. **The repo ships no `1_Pooling/` directory at all** even
      though its `modules.json` names one, and that is harmless HERE for exactly
      that reason — the ONNX runner would need that file, this engine never opens
      it.
    * the scheme resolves to `nomic`, which it does only because the curated
      table names it: the id spells the account `nomicai-`, so no hint matches.
    """
    import json

    monkeypatch.setattr(worker, "_mlx_load", lambda _arg: ("MODEL", "TOKENIZER"))
    (tmp_path / "config.json").write_text(json.dumps({
        "model_type": "modernbert",
        "architectures": ["ModernBertModel"],
        "max_position_embeddings": 8192,
        "classifier_pooling": "mean",
    }))
    worker.load("mlx-community/nomicai-modernbert-embed-base-bf16", str(tmp_path))

    assert worker._loaded["family"] == worker._TEXT
    assert worker._loaded["tokenizer"] == "TOKENIZER"
    assert worker._loaded["length"] == 8192
    assert worker._loaded["scheme"] == "nomic"


def test_the_curated_mlx_prose_row_is_one_this_runner_can_actually_open():
    """The catalog's MLX prose row against this runner's own gate.

    A curated row whose `model_type` the gate rejects is a Load button that
    refuses — `formats.MLX_EMBED_MODEL_TYPES` is deliberately NARROWER than
    mlx-embeddings' module list (a decoder-derived embedder wears its chat
    architecture's `model_type`, so admitting `qwen3` would route every Qwen3
    chat checkpoint here), which makes "the library ships a module for it" the
    wrong test and this the right one.

    Kept as a static pair rather than a live config read: this file never
    touches the network, and the `model_type` is the fact the catalog comment
    already records for each row.
    """
    from fused_render.ai import catalog
    from fused_render.ai.runners import formats

    curated = {e["id"] for e in catalog.SUGGESTIONS["mlx-embed"]}
    families = {
        "mlx-community/nomicai-modernbert-embed-base-bf16": "modernbert",
        "google/siglip2-base-patch16-384": "siglip",
        "mlx-community/siglip2-so400m-patch16-384": "siglip",
        "mlx-community/embeddinggemma-2-bf16": "embedding_gemma2",
    }
    assert curated == set(families), (
        "the MLX list changed — add the new row's model_type here, since a row "
        "outside MLX_EMBED_MODEL_TYPES is a Load button that refuses")
    for repo_id, family in families.items():
        assert family in formats.MLX_EMBED_MODEL_TYPES, repo_id


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


def test_a_family_MLX_cannot_read_is_refused_BY_NAME(worker, monkeypatch,
                                                     tmp_path):
    """**`_family` checked the union while its message named the MLX subset.**

    `nomic_bert` and `clip` are in `EMBED_MODEL_TYPES` and not in
    `MLX_EMBED_MODEL_TYPES`, so they passed the very check this function exists
    to make and died inside `mlx_embeddings.load` instead — an opaque library
    error where the named refusal below belongs.

    The concrete case is the first one: a page hard-coding the ONNX default
    `nomic-ai/nomic-embed-text-v1.5` and opened on a Mac.
    """
    import json

    monkeypatch.setattr(worker, "_mlx_load", lambda _arg: ("MODEL", "TOKENIZER"))
    for model_type, repo_id in (("nomic_bert", "nomic-ai/nomic-embed-text-v1.5"),
                                ("clip", "openai/clip-vit-base-patch32")):
        (tmp_path / "config.json").write_text(json.dumps(
            {"model_type": model_type, "max_position_embeddings": 512}))
        with pytest.raises(RuntimeError) as excinfo:
            worker.load(repo_id, str(tmp_path))
        message = str(excinfo.value)
        assert repo_id in message
        assert model_type in message
        # The message lists what this runner DOES read, and the family being
        # refused must not be in that list — which is what made the old error
        # self-contradictory.
        assert model_type not in message.split("reads")[1]


def test_the_families_MLX_does_read_still_load(worker, monkeypatch, tmp_path):
    """The guard against the stricter check becoming a wall — every member of
    `MLX_EMBED_MODEL_TYPES` must still reach a family."""
    import json

    monkeypatch.setattr(worker, "_mlx_load", lambda _arg: ("MODEL", "TOKENIZER"))
    monkeypatch.setattr(worker, "_mlx_vlm_load",
                        lambda _arg: ("MODEL", FakeUnifiedProcessor()))
    for model_type in sorted(worker.formats.MLX_EMBED_MODEL_TYPES):
        (tmp_path / "config.json").write_text(json.dumps(
            {"model_type": model_type, "max_position_embeddings": 512,
             "text_config": {"max_position_embeddings": 64}}))
        worker.load("org/" + model_type, str(tmp_path))
        assert worker._loaded["family"] in (worker._DUAL, worker._TEXT,
                                            worker._UNIFIED)


class FakeUnifiedTokenizer:
    pad_token_id = 0

    def __init__(self):
        self.calls = []

    def __call__(self, texts, **kwargs):
        self.calls.append({"texts": list(texts), **kwargs})
        return {"input_ids": [[7] * len(text.split()) for text in texts]}


class FakeUnifiedProcessor:
    """The three placeholder tokens and the three input shapes the real
    `EmbeddingGemma2Processor` produces — `pixel_values` for pictures,
    `input_features` for sound, `pixel_values_videos` for video — each
    carrying a number the fake model turns into a recognisable vector: an
    image's width, an audio file's size in bytes, a video file's size in
    bytes. Sound and video arrive as PATHS, exactly as the worker hands them
    over, and `failing` names paths the "decoder" refuses, with the error
    class and text mlx-audio raises for a missing ffmpeg when asked to."""
    image_token = "<|image|>"
    audio_token = "<|audio|>"
    video_token = "<|video|>"

    def __init__(self, failing=()):
        self.tokenizer = FakeUnifiedTokenizer()
        self.calls = []
        self.failing = dict(failing)

    def __call__(self, text=None, images=None, audio=None, videos=None, **kwargs):
        self.calls.append({"text": list(text), "images": images, "audio": audio,
                           "videos": videos, **kwargs})
        out = {"input_ids": [[0]] * len(text)}
        if images is not None:
            out["pixel_values"] = [image[0].size[0] for image in images]
        for key, paths in (("input_features", audio), ("pixel_values_videos", videos)):
            if paths is None:
                continue
            for path in paths:
                if path in self.failing:
                    raise self.failing[path]
            out[key] = [os.path.getsize(path) for path in paths]
        return out


class FakeUnifiedModel:
    def __init__(self):
        self.calls = []

    def __call__(self, input_ids=None, attention_mask=None, pixel_values=None,
                 input_features=None, pixel_values_videos=None, **_kw):
        self.calls.append({"input_ids": input_ids.data,
                           "attention_mask": attention_mask.data if attention_mask else None,
                           "pixel_values": pixel_values.data if pixel_values else None,
                           "input_features": input_features.data if input_features else None,
                           "pixel_values_videos": (pixel_values_videos.data
                                                   if pixel_values_videos else None)})
        if pixel_values is not None:
            rows = [[float(width), 1.0] for width in pixel_values.data]
        elif input_features is not None:
            # 1000 + the file's size, so the ratio test below recovers the
            # kind (thousands) and the file (the rest) from one number.
            rows = [[1000.0 + size, 1.0] for size in input_features.data]
        elif pixel_values_videos is not None:
            rows = [[2000.0 + size, 1.0] for size in pixel_values_videos.data]
        else:
            rows = [[float(sum(mask)), 1.0] for mask in attention_mask.data]
        return types.SimpleNamespace(text_embeds=FakeMxArray(rows))


@pytest.fixture()
def unified(worker):
    worker._loaded.clear()
    processor = FakeUnifiedProcessor()
    worker._loaded.update({
        "model": FakeUnifiedModel(),
        "processor": processor,
        "tokenizer": processor.tokenizer,
        "family": worker._UNIFIED,
        "scheme": "gemma-embedding",
        "model_id": "mlx-community/embeddinggemma-2-bf16",
        "length": 8192,
    })
    return worker


def test_embeddinggemma2_loads_through_mlx_vlm_from_the_snapshot_path(
        worker, monkeypatch, tmp_path):
    import json

    seen = {}

    def fake_vlm_load(path):
        seen["path"] = path
        return "MODEL", FakeUnifiedProcessor()

    monkeypatch.setattr(worker, "_mlx_vlm_load", fake_vlm_load)
    monkeypatch.setattr(worker, "_mlx_load", lambda _arg: pytest.fail("mlx-embeddings used"))
    (tmp_path / "config.json").write_text(json.dumps({
        "model_type": "embedding_gemma2",
        "architectures": ["EmbeddingGemma2Model"],
        "text_config": {"model_type": "embedding_gemma2_text",
                        "max_position_embeddings": 262144},
        "vision_config": {"model_type": "gemma4_vision"},
        "audio_config": {"model_type": "gemma4_audio"},
    }))
    worker.load("mlx-community/embeddinggemma-2-bf16", str(tmp_path))

    assert seen["path"] == str(tmp_path)
    assert worker._loaded["family"] == worker._UNIFIED
    assert worker._loaded["scheme"] == "gemma-embedding"
    assert worker._loaded["length"] == 8192
    assert worker._loaded["tokenizer"] is worker._loaded["processor"].tokenizer


def test_embeddinggemma2_is_an_image_capable_family_with_a_prompt_scheme():
    from fused_render.ai.runners import formats

    config = {"model_type": "embedding_gemma2", "architectures": ["EmbeddingGemma2Model"]}
    assert formats.embed_model_type(config) == "embedding_gemma2"
    assert "embedding_gemma2" in formats.DUAL_EMBED_MODEL_TYPES
    assert "embedding_gemma2" in formats.MLX_EMBED_MODEL_TYPES
    assert "embedding_gemma2" not in formats.ONNX_EMBED_MODEL_TYPES
    assert formats.MLX_VLM_EMBED_MODEL_TYPES <= formats.MLX_EMBED_MODEL_TYPES
    assert formats.text_embed_scheme("mlx-community/embeddinggemma-2-bf16") == "gemma-embedding"


def test_unified_texts_get_the_retrieval_prefix_and_keep_their_order(unified):
    result = unified.generate({"texts": ["one two three", "one"], "kind": "query"})
    call = unified._loaded["tokenizer"].calls[-1]
    assert call["texts"] == ["task: search result | query: one two three",
                             "task: search result | query: one"]
    assert call["max_length"] == 8192
    assert call["truncation"] is True
    lengths = [len(t.split()) for t in call["texts"]]
    expected = [n / (n * n + 1) ** 0.5 for n in lengths]
    assert [round(v[0], 6) for v in result["vectors"]] == [round(e, 6) for e in expected]
    assert len(unified._loaded["model"].calls) == 1


def test_unified_documents_take_the_untitled_document_prefix(unified):
    unified.generate({"texts": ["notes.txt body"], "kind": "document"})
    assert unified._loaded["tokenizer"].calls[-1]["texts"] == [
        "title: none | text: notes.txt body"]


def test_unified_text_batches_split_by_the_padded_token_budget(unified, monkeypatch):
    monkeypatch.setattr(unified, "_TOKEN_BUDGET", 40)
    texts = [" ".join(["w"] * n) for n in (12, 3, 9, 15, 4)]
    result = unified.generate({"texts": texts, "kind": "document"})
    calls = unified._loaded["model"].calls
    assert len(calls) > 1
    for call in calls:
        assert len(call["input_ids"]) * len(call["input_ids"][0]) <= 40
        for ids, mask in zip(call["input_ids"], call["attention_mask"]):
            real = sum(mask)
            assert mask == [1] * real + [0] * (len(mask) - real)
            assert ids[real:] == [0] * (len(ids) - real)
    prefix = len("title: none | text:".split())
    for text, vector in zip(texts, result["vectors"]):
        n = len(text.split()) + prefix
        assert round(vector[0], 6) == round(n / (n * n + 1) ** 0.5, 6)


def test_unified_paths_are_embedded_one_image_token_per_row(unified, tmp_path):
    paths = []
    for index in range(10):
        path = tmp_path / f"image{index}.png"
        Image.new("RGB", (10 + index, 8), (index, 0, 0)).save(path)
        paths.append(str(path))
    result = unified.generate({"paths": paths})
    calls = unified._loaded["processor"].calls
    assert [len(call["text"]) for call in calls] == [8, 2]
    for call in calls:
        assert set(call["text"]) == {"<|image|>"}
        assert all(len(row) == 1 for row in call["images"])
    widths = [round(v[0] / v[1]) for v in result["vectors"]]
    assert widths == [10 + index for index in range(10)]


def test_unified_paths_are_not_refused_as_a_text_encoder_would_be(unified, tmp_path):
    path = tmp_path / "photo.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(path)
    result = unified.generate({"paths": [str(path)]})
    assert len(result["vectors"]) == 1


# -- the unified encoder reads SOUND and VIDEO by path too --------------------
#
# The real model has a vision tower and an audio tower hung off one language
# model, and the pinned mlx-vlm processor takes `audio=` and `videos=` beside
# `images=`. The worker sorts `paths` by extension and embeds each kind in its
# own chunks, scattering the vectors back to the caller's order. Measured on
# the real checkpoint 2026-10-13 (`.wav`/`.mp3`/`.m4a` and two `.mp4`s): all
# 768-dim, the two tones at cosine 0.96–0.99 to each other, the red video at
# 0.93 to the red picture.


def _media(tmp_path):
    """Two pictures, three sound files and two videos, with distinct sizes so
    the fake's vectors identify each file — not real media, since the fake
    processor never decodes them; the real one is `_mlx_vlm_load`'s business
    and `test_ai_mlx_embed_real_weights.py`'s."""
    paths = {}
    for index, name in enumerate(("a.png", "b.jpg")):
        path = tmp_path / name
        Image.new("RGB", (20 + index, 8)).save(path, format="PNG")
        paths[name] = str(path)
    for index, name in enumerate(("one.wav", "two.MP3", "three.m4a",
                                  "clip.mp4", "clip2.mov")):
        path = tmp_path / name
        path.write_bytes(b"x" * (100 + index))
        paths[name] = str(path)
    return paths


def _ratio(vector):
    return round(vector[0] / vector[1])


def test_unified_paths_mix_pictures_sound_and_video_in_the_callers_order(
        unified, tmp_path):
    media = _media(tmp_path)
    order = ["one.wav", "a.png", "clip.mp4", "two.MP3", "b.jpg", "clip2.mov",
             "three.m4a"]
    result = unified.generate({"paths": [media[n] for n in order]})
    assert len(result["vectors"]) == len(order)
    # The thousands digit tags the kind (0 picture, 1 sound, 2 video) and the
    # rest identifies the file — so order and kind are both checked at once.
    values = [_ratio(v) for v in result["vectors"]]
    assert [value // 1000 for value in values] == [1, 0, 2, 1, 0, 2, 1]
    expect = {"one.wav": 100, "two.MP3": 101, "three.m4a": 102, "clip.mp4": 103,
              "clip2.mov": 104, "a.png": 20, "b.jpg": 21}
    assert [value % 1000 for value in values] == [expect[n] for n in order]


def test_unified_sound_goes_through_the_audio_token_as_paths(unified, tmp_path):
    media = _media(tmp_path)
    unified.generate({"paths": [media["one.wav"], media["two.MP3"]]})
    call = unified._loaded["processor"].calls[-1]
    assert call["text"] == ["<|audio|>", "<|audio|>"]
    assert call["audio"] == [media["one.wav"], media["two.MP3"]]
    assert call["images"] is None and call["videos"] is None
    assert call["padding"] is True


def test_unified_video_goes_one_clip_per_forward(unified, tmp_path):
    media = _media(tmp_path)
    unified.generate({"paths": [media["clip.mp4"], media["clip2.mov"]]})
    calls = unified._loaded["processor"].calls
    assert [call["videos"] for call in calls] == [[media["clip.mp4"]],
                                                  [media["clip2.mov"]]]
    assert all(call["text"] == ["<|video|>"] for call in calls)


def test_unified_sound_is_chunked_like_pictures(unified, tmp_path):
    paths = []
    for index in range(10):
        path = tmp_path / f"s{index}.wav"
        path.write_bytes(b"x" * (10 + index))
        paths.append(str(path))
    result = unified.generate({"paths": paths})
    calls = unified._loaded["processor"].calls
    assert [len(call["audio"]) for call in calls] == [8, 2]
    assert [_ratio(v) % 1000 for v in result["vectors"]] == [10 + i for i in range(10)]


def test_a_missing_sound_file_is_named_before_any_decoder_runs(unified, tmp_path):
    missing = str(tmp_path / "gone.wav")
    with pytest.raises(ValueError, match="no such file: .*gone.wav"):
        unified.generate({"paths": [missing]})
    assert unified._loaded["processor"].calls == []
    (tmp_path / "folder.mp4").mkdir()
    with pytest.raises(ValueError, match="not a file: .*folder.mp4"):
        unified.generate({"paths": [str(tmp_path / "folder.mp4")]})


def test_a_decoder_failure_names_the_ONE_file_in_the_batch_that_broke(
        unified, tmp_path):
    media = _media(tmp_path)
    unified._loaded["processor"].failing = {
        media["two.MP3"]: RuntimeError("Unsupported audio format: garbage")}
    with pytest.raises(ValueError) as info:
        unified.generate({"paths": [media["one.wav"], media["two.MP3"],
                                    media["three.m4a"]]})
    message = str(info.value)
    assert media["two.MP3"] in message
    assert "not a readable audio" in message
    assert "Unsupported audio format" in message
    assert media["one.wav"] not in message


def test_an_m4a_without_ffmpeg_says_what_to_install_or_convert_to(
        unified, tmp_path):
    media = _media(tmp_path)
    banner = ("ffmpeg not found!\n\nffmpeg is required for M4A/AAC/WebM audio "
              "decoding.\nInstall ffmpeg:\n  macOS:  brew install ffmpeg")
    unified._loaded["processor"].failing = {media["three.m4a"]: RuntimeError(banner)}
    with pytest.raises(ValueError) as info:
        unified.generate({"paths": [media["three.m4a"]]})
    message = str(info.value)
    assert media["three.m4a"] in message
    assert "needs ffmpeg on PATH" in message
    assert "wav, mp3, flac or ogg" in message
    assert "brew" not in message, "the install banner is not relayed"


def test_an_unreadable_video_is_named_as_a_video(unified, tmp_path):
    media = _media(tmp_path)
    unified._loaded["processor"].failing = {
        media["clip.mp4"]: ValueError("Cannot open video: " + media["clip.mp4"])}
    with pytest.raises(ValueError, match="is not a readable video .*Cannot open video"):
        unified.generate({"paths": [media["clip.mp4"]]})


def test_a_decoders_ValueError_is_attributed_to_its_file_too(unified, tmp_path):
    """OpenCV's "Cannot open video" and transformers' own checks arrive as
    `ValueError`, which must not skip the per-path retry: in a batch of sound
    files the sentence still has to name the one that broke."""
    media = _media(tmp_path)
    unified._loaded["processor"].failing = {
        media["two.MP3"]: ValueError("Unsupported sample rate")}
    with pytest.raises(ValueError) as info:
        unified.generate({"paths": [media["one.wav"], media["two.MP3"]]})
    assert str(info.value).startswith(media["two.MP3"] + " is not a readable audio")
    assert info.value.__cause__ is not None, "the decoder's traceback is kept"


def test_a_batch_that_only_fails_TOGETHER_is_not_blamed_on_its_first_file(
        unified, tmp_path):
    media = _media(tmp_path)
    processor = unified._loaded["processor"]
    real = processor.__call__.__func__

    def flaky(self, text=None, **kwargs):
        if len(text) > 1:
            raise RuntimeError("batch too wide")
        return real(self, text=text, **kwargs)

    processor.__class__ = type("FlakyProcessor", (FakeUnifiedProcessor,), {"__call__": flaky})
    with pytest.raises(ValueError) as info:
        unified.generate({"paths": [media["one.wav"], media["two.MP3"]]})
    message = str(info.value)
    assert "2 audio files each decode alone but not together" in message
    assert "batch too wide" in message
    assert media["one.wav"] in message and media["two.MP3"] in message
    assert not message.startswith(media["one.wav"])


def test_a_short_model_output_is_an_error_not_a_None_in_the_vectors(
        unified, tmp_path):
    media = _media(tmp_path)
    model = unified._loaded["model"]
    real = model.__call__.__func__

    def short(self, **kwargs):
        out = real(self, **kwargs)
        out.text_embeds = FakeMxArray(out.text_embeds.data[:-1])
        return out

    model.__class__ = type("ShortModel", (FakeUnifiedModel,), {"__call__": short})
    with pytest.raises(RuntimeError, match="returned 1 vectors for 2 audio inputs"):
        unified.generate({"paths": [media["one.wav"], media["two.MP3"]]})


def test_a_SIGLIP_refuses_a_sound_file_by_name(worker, tmp_path):
    media = _media(tmp_path)
    with pytest.raises(ValueError) as info:
        worker.generate({"paths": [media["a.png"], media["one.wav"]]})
    message = str(info.value)
    assert worker._loaded["model_id"] in message
    assert media["one.wav"] in message
    assert "an audio file" in message
    assert "embeddinggemma-2" in message
    assert worker._loaded["processor"].calls == [], "refused before any decode"


def test_a_SIGLIP_refuses_a_video_by_name_too(worker, tmp_path):
    media = _media(tmp_path)
    with pytest.raises(ValueError, match="a video file"):
        worker.generate({"paths": [media["clip.mp4"]]})


def test_the_mlx_vlm_load_keeps_the_audio_tower(worker, monkeypatch, tmp_path):
    """PR #1553 nulled `audio_config` at load, which dropped the audio tower
    and made every sound file raise inside the model. The config must now
    reach `load_embedding_model` whole."""
    import json
    import types as _types

    seen = {}
    loader = _types.ModuleType("mlx_vlm.embedding_loader")
    loader.load_embedding_model = lambda root, config=None: seen.setdefault(
        "config", config) and "MODEL"
    gemma = _types.ModuleType("mlx_vlm.models.embedding_gemma2")
    gemma.EmbeddingGemma2Processor = _types.SimpleNamespace(
        from_pretrained=lambda root: "PROCESSOR")
    utils = _types.ModuleType("mlx_vlm.utils")
    utils.load_config = lambda root: json.loads((root / "config.json").read_text())
    package = _types.ModuleType("mlx_vlm")
    models = _types.ModuleType("mlx_vlm.models")
    for name, module in (("mlx_vlm", package), ("mlx_vlm.models", models),
                         ("mlx_vlm.models.embedding_gemma2", gemma),
                         ("mlx_vlm.embedding_loader", loader),
                         ("mlx_vlm.utils", utils)):
        monkeypatch.setitem(sys.modules, name, module)
    (tmp_path / "config.json").write_text(json.dumps({
        "model_type": "embedding_gemma2",
        "audio_config": {"model_type": "gemma4_audio"},
        "vision_config": {"model_type": "gemma4_vision"}}))

    model, processor = worker._mlx_vlm_load(str(tmp_path))

    assert model == "MODEL" and processor == "PROCESSOR"
    assert seen["config"]["audio_config"] == {"model_type": "gemma4_audio"}


def test_every_family_has_a_media_answer_and_they_match_the_shared_table(worker):
    from fused_render.ai.runners import embed_common

    assert set(worker._FAMILY_MEDIA) == {worker._DUAL, worker._TEXT, worker._UNIFIED}
    assert worker._FAMILY_MEDIA[worker._UNIFIED] == embed_common.EMBED_MEDIA["embedding_gemma2"]
    assert worker._FAMILY_MEDIA[worker._DUAL] == embed_common.EMBED_MEDIA["siglip"]
    assert worker._FAMILY_MEDIA[worker._TEXT] == ()
