"""The transformers text runner's own behaviour — what is true of torch and
nothing else.

The contract half (routes, states, progress, the port handshake) is
`worker_base`'s and is covered by `tests/test_ai_worker_base.py`. What is left
here is what the runner decides for itself, and almost all of it is REFUSALS:
this is the first text runner that any machine can reach, so it is also the
first one a user can point at a checkpoint it cannot read.

Loaded by PATH with `worker_base` primed in `sys.modules`, exactly as
`tests/test_ai_mlx_worker.py` does: the runner finds its base off `sys.path` in
an interpreter of its own, so importing it the packaged way
(`fused_render.ai.runners.…`) would be testing an import that never ships.
"""
import contextlib
import importlib.util
import json
import queue
import sys
import threading
import time
import types
from pathlib import Path

import pytest

WORKER_PATH = str(
    Path(__file__).resolve().parents[1]
    / "fused_render" / "ai" / "runners" / "transformers_text" / "worker.py"
)


@pytest.fixture()
def worker(monkeypatch):
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_snapshot = lambda model_id, **kw: f"/snapshots/{model_id}"
    base.serve = lambda **kw: None
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)

    monkeypatch.setitem(sys.modules, "worker_base", base)
    spec = importlib.util.spec_from_file_location(
        "transformers_text_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    return module


def _snapshot(tmp_path, config=None, weights=("model.safetensors",)):
    """A snapshot directory shaped like the one `download` hands to `load`."""
    folder = tmp_path / "snapshot"
    folder.mkdir(exist_ok=True)
    if config is not None:
        (folder / "config.json").write_text(json.dumps(config))
    for name in weights:
        (folder / name).write_bytes(b"\0")
    return str(folder)


# -- refusals ------------------------------------------------------------------
#
# Every one of these is a repo the AI Models page will happily offer a Load
# button for: the button is gated on the TASK label, and the format is not in
# the label. What the loader would say on its own is an ImportError or a KeyError
# several frames inside transformers, printed beside the name of a repo that
# downloaded perfectly — the exact failure AI-10 describes for CTranslate2.


def test_an_mlx_checkpoint_is_named_as_mlx(worker, tmp_path):
    """The likeliest wrong turn on the whole page, and it deserves its own words.

    A Mac user's `mlx-community/…` model is portable in the sense that it
    syncs, and completely unloadable here — and the two lists sit one tab apart
    on the same page. "torch cannot read this" would leave someone trying the
    next MLX repo down the list.
    """
    path = _snapshot(tmp_path, {"quantization": {"group_size": 64, "bits": 4}})
    with pytest.raises(RuntimeError) as caught:
        worker.load("mlx-community/Qwen3-8B-4bit", path)
    message = str(caught.value)
    assert "MLX" in message and "Apple Silicon" in message
    # …and it names one that WILL load, because the alternative is a web search.
    assert "Qwen/Qwen3-4B-Instruct-2507" in message


def test_an_awq_checkpoint_names_the_scheme_and_not_a_missing_module(worker, tmp_path):
    path = _snapshot(tmp_path, {"quantization_config": {"quant_method": "awq"}})
    with pytest.raises(RuntimeError, match="AWQ"):
        worker.load("org/model-awq", path)


def test_a_bitsandbytes_checkpoint_says_it_needs_a_GPU(worker, tmp_path):
    """A 4-bit bnb repo is the obvious way to make an 8B model fit, and it does
    not work here — the runner ships neither bitsandbytes nor, on Windows, a
    torch that can see a GPU. Saying which of the two is missing is the
    difference between installing something and picking a different model."""
    path = _snapshot(tmp_path, {"quantization_config": {"quant_method": "bitsandbytes"}})
    with pytest.raises(RuntimeError, match="NVIDIA"):
        worker.load("unsloth/Qwen3-8B-bnb-4bit", path)


def test_a_gguf_only_repo_is_refused_rather_than_expanded(worker, tmp_path):
    """transformers CAN read a GGUF, by dequantizing it to float32 on the way in
    — so a 4GB file becomes ~16GB of RAM and the machine swaps. A load that
    "works" and then wedges the laptop is worse than a refusal."""
    path = _snapshot(tmp_path, {"model_type": "qwen3"}, weights=("model.Q4_K_M.gguf",))
    with pytest.raises(RuntimeError, match="GGUF"):
        worker.load("org/model-gguf", path)


def test_the_quantization_check_reads_the_config_not_the_repo_name(worker, tmp_path):
    """`mlx-community/…-4bit` is a NAMING CONVENTION, and a refusal must not rest
    on one — the same rule `ai_models._quantization` follows for the number it
    prints on a card. A repo whose config says nothing quantized passes the
    format gate however its name reads."""
    path = _snapshot(tmp_path, {"model_type": "qwen3"})
    # Past the refusals and into the real loader, which is not importable here —
    # reaching it IS the assertion.
    with pytest.raises(ImportError):
        worker.load("mlx-community/looks-4bit-but-is-not", path)


def test_a_repo_with_no_config_is_not_refused_for_that_alone(worker, tmp_path):
    """An unreadable `config.json` is not evidence of a bad format. Refusing on
    it would turn every repo whose card came down oddly into "wrong format"."""
    path = _snapshot(tmp_path, None)
    with pytest.raises(ImportError):
        worker.load("org/no-config", path)


# -- the dtype keyword ---------------------------------------------------------


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("4.51.0", "torch_dtype"),   # the runner's declared floor
        ("4.55.4", "torch_dtype"),
        ("4.56.0", "dtype"),
        ("4.57.0.dev0", "dtype"),   # a dev build must not parse as 4.5
        ("5.0.0", "dtype"),
        ("", "dtype"),              # unreadable: take the spelling that survives
    ],
)
def test_the_dtype_keyword_follows_the_installed_transformers(
        worker, monkeypatch, version, expected):
    """The wrong spelling is not a loud failure, which is why this is pinned.

    `from_pretrained` forwards unknown keyword arguments into the config rather
    than rejecting them, so a stale `torch_dtype` on transformers v5 would load
    a 4B model at float32 — sixteen gigabytes instead of eight — and OOM a
    laptop that had every right to run it, with nothing on screen to say why.
    """
    fake = types.ModuleType("transformers")
    fake.__version__ = version
    monkeypatch.setitem(sys.modules, "transformers", fake)
    assert list(worker._dtype_kwargs("bfloat16")) == [expected]


# -- placement -----------------------------------------------------------------


def _fake_torch(cuda=False, mps=False, bf16=True):
    torch = types.ModuleType("torch")
    torch.bfloat16, torch.float16 = "bfloat16", "float16"
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: cuda, is_bf16_supported=lambda: bf16)
    torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: mps))
    return torch


def test_a_machine_with_no_gpu_loads_at_bfloat16_rather_than_float32(
        worker, monkeypatch):
    """The single decision this runner exists to get right.

    torch defaults an un-hinted checkpoint to float32, which DOUBLES a model
    published at 16 bits — so the one configuration this runner was written for,
    a Windows laptop with no GPU, would need 16GB to hold a 4B model instead of
    8. bfloat16 on a CPU without native support is emulated and slower than
    float32 in theory; a model that swaps is not slower, it is unusable.
    """
    monkeypatch.setitem(sys.modules, "torch", _fake_torch())
    assert worker._placement() == ("cpu", "bfloat16")


def test_cuda_takes_bfloat16_where_the_card_supports_it_and_float16_otherwise(
        worker, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=True))
    assert worker._placement() == ("cuda", "bfloat16")
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(cuda=True, bf16=False))
    assert worker._placement() == ("cuda", "float16")


def test_an_apple_machine_reaching_this_runner_takes_float16(worker, monkeypatch):
    """Only reachable when the MLX runner is unavailable, which makes this a
    fallback path — and a fallback is the wrong place to require a recent macOS
    for bfloat16 on Metal."""
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(mps=True))
    assert worker._placement() == ("mps", "float16")


def test_a_card_that_raises_when_asked_about_bf16_still_loads(worker, monkeypatch):
    """`is_bf16_supported` really does raise on a driver mismatch, and a probe
    that raises must cost the better dtype rather than the whole load."""
    torch = _fake_torch(cuda=True)

    def boom():
        raise RuntimeError("no CUDA driver")

    torch.cuda.is_bf16_supported = boom
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert worker._placement() == ("cuda", "float16")


# -- prompting -----------------------------------------------------------------


class _Tokenizer:
    """Enough of a tokenizer to answer the one question `_encode` asks."""

    chat_template = "{{ messages }}"

    def __init__(self):
        self.calls = []

    def apply_chat_template(self, messages, **kwargs):
        self.calls.append(("template", messages, kwargs))
        return _Tensor([1, 2, 3])

    def __call__(self, text, **kwargs):
        self.calls.append(("plain", text, kwargs))
        return {"input_ids": _Tensor([4, 5]), "attention_mask": _Tensor([1, 1])}


class _Tensor(list):
    def to(self, _device):
        return self


def test_a_chat_prompt_is_tokenized_BY_the_template_not_after_it(
        worker, monkeypatch):
    """Two BOS tokens is the failure this shape exists to prevent.

    A chat template emits the model's own turn markers INCLUDING its leading
    BOS, and `tokenizer(rendered_text)` adds a second one — which no model saw
    in training. It does not raise and it does not look broken; it just makes
    the answers quietly worse, which is the "looks almost right" failure the MLX
    runner warns about one layer up. Asking the template for ids directly is
    what makes the mistake unavailable rather than merely avoided.
    """
    torch = _fake_torch()
    torch.ones_like = lambda ids: _Tensor([1] * len(ids))
    monkeypatch.setitem(sys.modules, "torch", torch)

    tokenizer = _Tokenizer()
    worker._encode(tokenizer, [{"role": "user", "content": "hi"}], "", "cpu")
    assert [call[0] for call in tokenizer.calls] == ["template"]
    assert tokenizer.calls[0][2]["add_generation_prompt"] is True


def test_a_v5_batch_encoding_from_the_chat_template_is_unwrapped(
        worker, monkeypatch):
    """Transformers v5 made the mapping return consistent with tokenization.

    Before v5, ``apply_chat_template(..., return_tensors="pt")`` returned the
    input-id tensor directly. In v5 it returns a ``BatchEncoding`` carrying
    both ids and the attention mask. Passing that object to ``torch.ones_like``
    raises before the model sees the prompt, so the worker accepts both shapes
    across the dependency range it declares.
    """
    torch = _fake_torch()
    torch.ones_like = lambda _ids: pytest.fail(
        "a supplied attention mask must not be replaced")
    monkeypatch.setitem(sys.modules, "torch", torch)

    class V5Tokenizer(_Tokenizer):
        def apply_chat_template(self, messages, **kwargs):
            self.calls.append(("template", messages, kwargs))
            return {
                "input_ids": _Tensor([1, 2, 3]),
                "attention_mask": _Tensor([1, 1, 1]),
            }

    tokenizer = V5Tokenizer()
    ids, mask = worker._encode(
        tokenizer, [{"role": "user", "content": "hi"}], "", "cpu")

    assert list(ids) == [1, 2, 3]
    assert list(mask) == [1, 1, 1]
    assert [call[0] for call in tokenizer.calls] == ["template"]


def test_reasoning_is_off_by_default(worker, monkeypatch):
    """Qwen3's template defaults thinking ON, and three of four suggestions are Qwen3.

    Left on, an ordinary question emits a `<think>` block first — hundreds of
    tokens the caller cannot tell apart from the answer, because `/generate`
    streams whatever the model produces. At a few tokens a second on the CPU
    path this runner exists for, that is minutes of apparent silence on a
    machine already suspected of being slow. Reported by review on the PR that
    added this runner.
    """
    torch = _fake_torch()
    torch.ones_like = lambda ids: _Tensor([1] * len(ids))
    monkeypatch.setitem(sys.modules, "torch", torch)

    tokenizer = _Tokenizer()
    worker._encode(tokenizer, [{"role": "user", "content": "hi"}], "", "cpu")
    assert tokenizer.calls[0][2]["enable_thinking"] is False


def test_a_tokenizer_that_refuses_the_keyword_still_answers(worker, monkeypatch):
    """`enable_thinking` goes to every model, which is safe — kwargs land in the
    Jinja context and a template that never mentions it does not read it. The
    other skew is a tokenizer whose signature rejects it outright, and a model
    that will not take the hint should still answer, just verbosely."""
    torch = _fake_torch()
    torch.ones_like = lambda ids: _Tensor([1] * len(ids))
    monkeypatch.setitem(sys.modules, "torch", torch)

    class Picky(_Tokenizer):
        def apply_chat_template(self, messages, **kwargs):
            if "enable_thinking" in kwargs:
                raise TypeError("unexpected keyword argument 'enable_thinking'")
            return super().apply_chat_template(messages, **kwargs)

    tokenizer = Picky()
    ids, _mask = worker._encode(tokenizer, [{"role": "user", "content": "hi"}], "", "cpu")
    assert list(ids) == [1, 2, 3]
    assert [call[0] for call in tokenizer.calls] == ["template"]


def test_a_raw_prompt_skips_the_template_entirely(worker, monkeypatch):
    """`raw` means "no chat template", so it gets the ordinary treatment —
    special tokens and all. Rendering it through the template anyway would
    answer a raw continuation as a chat turn, which is plausible text that is
    silently not what was asked for."""
    torch = _fake_torch()
    torch.ones_like = lambda ids: _Tensor([1] * len(ids))
    monkeypatch.setitem(sys.modules, "torch", torch)

    tokenizer = _Tokenizer()
    worker._encode(tokenizer, [], "once upon a time", "cpu")
    assert [call[0] for call in tokenizer.calls] == ["plain"]
    assert tokenizer.calls[0][1] == "once upon a time"


def test_a_model_with_no_chat_template_falls_back_rather_than_inventing_one(
        worker, monkeypatch):
    torch = _fake_torch()
    torch.ones_like = lambda ids: _Tensor([1] * len(ids))
    monkeypatch.setitem(sys.modules, "torch", torch)

    tokenizer = _Tokenizer()
    tokenizer.chat_template = None
    worker._encode(tokenizer, [{"role": "user", "content": "hi"},
                               {"role": "assistant", "content": "hello"}], "", "cpu")
    assert [call[0] for call in tokenizer.calls] == ["plain"]
    assert tokenizer.calls[0][1] == "hi\n\nhello"


def test_a_disconnected_stream_stops_and_joins_generation(worker, monkeypatch):
    """A broken response must not leave the model running past the request lock."""
    fake_transformers = types.ModuleType("transformers")

    class StoppingCriteria:
        pass

    class StoppingCriteriaList(list):
        pass

    class Streamer:
        def __init__(self, *_args, **_kwargs):
            self.items = queue.Queue()

        def __iter__(self):
            while True:
                item = self.items.get(timeout=2)
                if item is None:
                    return
                yield item

        def end(self):
            self.items.put(None)

    fake_transformers.StoppingCriteria = StoppingCriteria
    fake_transformers.StoppingCriteriaList = StoppingCriteriaList
    fake_transformers.TextIteratorStreamer = Streamer
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    torch = _fake_torch()
    torch.inference_mode = contextlib.nullcontext
    torch.ones_like = lambda ids: _Tensor([1] * len(ids))
    monkeypatch.setitem(sys.modules, "torch", torch)

    producer = {}

    class Model:
        device = "cpu"

        def generate(self, **kwargs):
            producer["thread"] = threading.current_thread()
            kwargs["streamer"].items.put("first token")
            criterion = kwargs["stopping_criteria"][0]
            deadline = time.monotonic() + 2
            while not criterion(None, None):
                assert time.monotonic() < deadline, "generation was not stopped"
                time.sleep(0.005)

    tokenizer = _Tokenizer()
    tokenizer.pad_token_id = 0
    tokenizer.eos_token_id = 1
    worker._loaded.update(model=Model(), tokenizer=tokenizer)

    def disconnected(payload):
        if payload["type"] == "chunk":
            raise BrokenPipeError("client disconnected")

    with pytest.raises(BrokenPipeError, match="client disconnected"):
        worker.generate({"prompt": "hello", "max_tokens": 8}, disconnected)

    assert "thread" in producer
    assert not producer["thread"].is_alive()


# -- what the prompt cost ------------------------------------------------------


def test_the_encoded_prompt_length_is_the_input_token_count(worker):
    """`input_tokens` (SPEC AI-3) is free here — the prompt has just been
    tokenized and its last dimension IS the answer — where anything upstream
    would have to estimate it and then disagree with the model."""

    class _Ids:
        shape = (1, 37)

    assert worker._prompt_tokens(_Ids()) == 37


def test_a_sequence_without_a_shape_is_still_counted(worker):
    """The stub tokenizers in this file hand back plain lists, and so does any
    tokenizer returning python ids — length is the same answer."""
    assert worker._prompt_tokens([4, 5, 6]) == 3


def test_a_shape_this_cannot_read_costs_the_metric_not_the_generation(worker):
    """None means "not reported", which every reader already handles."""

    class _Odd:
        shape = None

    assert worker._prompt_tokens(_Odd()) is None
    assert worker._prompt_tokens(object()) is None


def test_both_terminal_frames_carry_the_prompt_count(worker, monkeypatch):
    """Including the CANCELLED one: the prompt was read whether or not the
    answer was still wanted by the end. The MLX runner is held to the same rule
    in tests/test_ai_mlx_worker.py — one API, one shape of terminal frame."""
    fake_transformers = types.ModuleType("transformers")

    class StoppingCriteria:
        pass

    class StoppingCriteriaList(list):
        pass

    class Streamer:
        """Hands back one token and then ends, so `generate` reaches its
        terminal frame without a thread to wait on."""

        def __init__(self, *_args, **_kwargs):
            self.items = queue.Queue()

        def __iter__(self):
            yield "one token"

        def end(self):
            pass

    fake_transformers.StoppingCriteria = StoppingCriteria
    fake_transformers.StoppingCriteriaList = StoppingCriteriaList
    fake_transformers.TextIteratorStreamer = Streamer
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    torch = _fake_torch()
    torch.inference_mode = contextlib.nullcontext
    torch.ones_like = lambda ids: _Tensor([1] * len(ids))
    monkeypatch.setitem(sys.modules, "torch", torch)

    class Model:
        device = "cpu"

        def generate(self, **_kwargs):
            return None

    tokenizer = _Tokenizer()
    tokenizer.pad_token_id = 0
    tokenizer.eos_token_id = 1
    worker._loaded.update(model=Model(), tokenizer=tokenizer)

    # `_Tokenizer.__call__` (the no-chat-template path) encodes to two ids.
    frames = []
    worker.generate({"prompt": "hello", "max_tokens": 8}, frames.append)
    done = frames[-1]
    assert done["type"] == "done" and done["ok"] is True
    assert done["input_tokens"] == 2 and done["tokens"] == 1

    frames.clear()
    worker_base = sys.modules["worker_base"]
    worker_base.CANCEL.set()
    try:
        worker.generate({"prompt": "hello", "max_tokens": 8}, frames.append)
    finally:
        worker_base.CANCEL.clear()
    assert frames[-1]["cancelled"] is True
    assert frames[-1]["input_tokens"] == 2
