"""The llama.cpp / GGUF text runner's own behaviour — what is true of
llama-cpp-python and nothing else.

The contract half (routes, states, progress, the port handshake) is
`worker_base`'s and is covered by `tests/test_ai_worker_base.py`. What is left
here is what this runner decides for itself: which ids it will fetch at all
(SPEC AI-11's curated-only rule), how it renders a GGUF's own embedded chat
template, and how it streams and cancels without a producer thread.

Loaded by PATH with `worker_base` primed in `sys.modules`, exactly as
`tests/test_ai_transformers_worker.py` does: the runner finds its base off
`sys.path` in an interpreter of its own, so importing it the packaged way
would be testing an import that never ships. `llama_cpp` itself is never
imported by these tests — the real dependency is not installed in the test
venv, matching AI-11c's precedent for `torch_text`, and every path that would
touch it works against `_loaded["llm"]` set directly to a fake.
"""
import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

WORKER_PATH = str(
    Path(__file__).resolve().parents[1]
    / "fused_render" / "ai" / "runners" / "llamacpp_text.py"
)


@pytest.fixture()
def worker(monkeypatch):
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_file = lambda repo, filename, **kw: f"/blobs/{repo}/{filename}"
    base.serve = lambda **kw: None
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)

    monkeypatch.setitem(sys.modules, "worker_base", base)
    spec = importlib.util.spec_from_file_location(
        "llamacpp_text_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    return module


# -- the curated-only rule -------------------------------------------------
#
# A GGUF repo commonly publishes two dozen quantizations of one model, so
# there is no rule for turning a bare repo id into "the one file this means" —
# only the (repo, file) pairs this runner curates ever load.


def test_download_refuses_an_uncurated_id_by_name(worker):
    with pytest.raises(RuntimeError, match="curated"):
        worker.download("some-org/not-in-the-table")


def test_download_fetches_exactly_the_one_curated_file(worker):
    """No snapshot call, and no second repo — `worker_base.download_file` is
    the whole of it (see the module docstring on why there is no external
    tokenizer/config fetch for these repos)."""
    path = worker.download("Qwen3.5-9B-Q4_K_M.gguf")
    assert path == "/blobs/unsloth/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf"


def test_load_refuses_an_uncurated_id_before_importing_llama_cpp(worker):
    """The curation check comes first, the same order `torch_text.load` checks
    its own format refusal before importing torch — a model this runner was
    never going to serve is a fact about the request, not about llama.cpp."""
    assert "llama_cpp" not in sys.modules
    with pytest.raises(RuntimeError, match="curated"):
        worker.load("some-org/not-in-the-table", "/blobs/whatever.gguf")
    assert "llama_cpp" not in sys.modules


def test_every_curated_recipe_is_also_in_the_catalog():
    """The two tables answer different questions — this one "how do I fetch
    it", the catalog "should I suggest it" — and a model this runner can load
    that the catalog never mentions (or the reverse) is exactly the drift
    `formats.COMPONENT_REPOS`'s docstring warns about one level up."""
    from fused_render.ai import catalog

    spec = importlib.util.spec_from_file_location(
        "llamacpp_text_for_catalog_check", WORKER_PATH)
    module = importlib.util.module_from_spec(spec)
    # `worker_base` is imported at module scope; a bare stub is enough since
    # nothing here calls into it.
    sys.modules.setdefault("worker_base", types.ModuleType("worker_base"))
    spec.loader.exec_module(module)

    recipe_ids = set(module._GGUF_RECIPES)
    catalog_ids = {entry["id"] for entry in catalog.SUGGESTIONS["llamacpp-text"]}
    assert recipe_ids == catalog_ids


# -- the chat template, rendered from the GGUF's own metadata ---------------


class _FakeLlama:
    """Enough of `llama_cpp.Llama` to answer what this runner asks of it."""

    def __init__(self, chunks=(), metadata=None, tokens=5):
        self._chunks = list(chunks)
        self.metadata = metadata or {}
        self._tokens = tokens

    def token_bos(self):
        return 1

    def token_eos(self):
        return 2

    def token_get_text(self, token_id):
        return {1: "<bos>", 2: "<eos>"}.get(token_id, "")

    def tokenize(self, data, add_bos=True):
        return list(range(self._tokens))

    def create_completion(self, prompt, **kwargs):
        self.last_call = {"prompt": prompt, **kwargs}
        for chunk in self._chunks:
            yield {"choices": [{"text": chunk}]}


_TEMPLATE = (
    "{% for m in messages %}{{ m.role }}: {{ m.content }}\n{% endfor %}"
    "{% if add_generation_prompt %}assistant:{% endif %}"
)


def test_the_chat_template_comes_from_the_ggufs_own_metadata(worker):
    """No file on disk to read — see the module docstring on why these
    curated repos ship no external tokenizer_config.json at all."""
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})
    assert worker._chat_template(llm) == _TEMPLATE
    assert worker._chat_template(_FakeLlama(metadata={})) is None


def test_a_chat_prompt_is_rendered_through_the_templates_own_shape(worker):
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})
    rendered = worker._prompt_text(
        llm, [{"role": "user", "content": "hi"}], "")
    assert rendered == "user: hi\nassistant:"


def test_reasoning_is_off_by_default(worker):
    """Three of this runner's curated models are Qwen3.5 GGUFs, whose upstream
    template defaults reasoning ON (AI-11d) — passed unconditionally here
    because, unlike transformers' `apply_chat_template`, Jinja never raises on
    a context variable a template does not reference."""
    template = "{{ enable_thinking }}"
    llm = _FakeLlama(metadata={"tokenizer.chat_template": template})
    assert worker._prompt_text(llm, [], "") == "False"


def test_a_raw_prompt_skips_the_template_entirely(worker):
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})
    assert worker._prompt_text(llm, [], "once upon a time") == "once upon a time"


def test_a_model_with_no_chat_template_falls_back_to_a_plain_join(worker):
    llm = _FakeLlama(metadata={})
    rendered = worker._prompt_text(
        llm, [{"role": "user", "content": "hi"},
              {"role": "assistant", "content": "hello"}], "")
    assert rendered == "hi\n\nhello"


def test_a_broken_template_falls_back_rather_than_failing_the_reply(worker):
    """A bad template must cost the RENDER, never the generation."""
    llm = _FakeLlama(metadata={"tokenizer.chat_template": "{{ undefined.attr }}"})
    rendered = worker._prompt_text(
        llm, [{"role": "user", "content": "hi"}], "")
    assert rendered == "hi"


# -- prompt token counting (SPEC AI-3) ---------------------------------------


def test_the_tokenized_prompt_length_is_the_input_token_count(worker):
    llm = _FakeLlama(tokens=7)
    assert worker._prompt_tokens(llm, "hello") == 7


def test_a_tokenizer_that_raises_costs_the_metric_not_the_generation(worker):
    class Boom(_FakeLlama):
        def tokenize(self, data, add_bos=True):
            raise RuntimeError("no")

    assert worker._prompt_tokens(Boom(), "hello") is None


# -- streaming and cancellation ----------------------------------------------


def test_generate_streams_chunks_then_a_done_frame(worker):
    llm = _FakeLlama(chunks=["Hel", "lo"], tokens=3)
    worker._loaded["llm"] = llm
    frames = []
    worker.generate({"prompt": "hi", "max_tokens": 8}, frames.append)
    chunks = [f for f in frames if f["type"] == "chunk"]
    done = frames[-1]
    assert [c["text"] for c in chunks] == ["Hel", "lo"]
    assert done["type"] == "done" and done["ok"] is True
    assert done["tokens"] == 2
    assert done["input_tokens"] == 3
    assert "seconds" in done


def test_generate_passes_sampling_params_straight_through(worker):
    llm = _FakeLlama(chunks=["x"])
    worker._loaded["llm"] = llm
    worker.generate(
        {"prompt": "hi", "max_tokens": 40, "temperature": 0.2, "top_p": 0.4},
        lambda payload: None)
    assert llm.last_call["max_tokens"] == 40
    assert llm.last_call["temperature"] == 0.2
    assert llm.last_call["top_p"] == 0.4


def test_no_model_loaded_answers_a_clean_failure(worker):
    frames = []
    worker.generate({"prompt": "hi"}, frames.append)
    assert frames == [{"type": "done", "ok": False, "error": "no model is loaded"}]


def test_cancellation_needs_no_thread_to_join(worker):
    """`create_completion`'s own generator IS the token loop here — unlike
    `torch_text`, which needs a producer thread for `TextIteratorStreamer`,
    breaking out of this loop on `CANCEL` is the whole of stopping it."""
    llm = _FakeLlama(chunks=["a", "b", "c", "d"], tokens=2)
    worker._loaded["llm"] = llm

    seen = []

    def collector(payload):
        seen.append(payload)
        if payload.get("type") == "chunk" and len(seen) == 2:
            worker.worker_base.CANCEL.set()

    try:
        worker.generate({"prompt": "hi"}, collector)
    finally:
        worker.worker_base.CANCEL.clear()

    done = seen[-1]
    assert done["type"] == "done" and done["cancelled"] is True
    assert done["input_tokens"] == 2
    # Stopped after the second chunk rather than running the fake's four to
    # completion.
    assert len([f for f in seen if f["type"] == "chunk"]) == 2


def test_a_disconnected_write_propagates_with_nothing_left_running(worker):
    """No producer thread exists here, so a `write` that raises on a client
    disconnect simply ends the generator — there is nothing to join."""
    llm = _FakeLlama(chunks=["a", "b", "c"])
    worker._loaded["llm"] = llm

    def disconnected(payload):
        if payload["type"] == "chunk":
            raise BrokenPipeError("client disconnected")

    with pytest.raises(BrokenPipeError, match="client disconnected"):
        worker.generate({"prompt": "hi"}, disconnected)


# -- device and memory --------------------------------------------------------


def test_load_reports_cpu_unconditionally(worker, monkeypatch):
    """No probe: unlike torch, there is no second device this process could
    have landed on, so the field is set the same way every time."""
    fake_llama_cpp = types.ModuleType("llama_cpp")

    class Llama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.metadata = {}

    fake_llama_cpp.Llama = Llama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama_cpp)

    worker.load("Qwen3.5-9B-Q4_K_M.gguf", "/blobs/model.gguf")
    assert worker.worker_base.recorded == {"device": "cpu"}
    assert worker._loaded["llm"].kwargs["model_path"] == "/blobs/model.gguf"


def test_memory_defers_entirely_to_rss(worker):
    """mmap'd weights are already counted in RSS (`use_mmap=True` is the
    default), so there is no second accounting system to add on top — unlike
    torch's CUDA/MPS allocators, which `worker_base` takes the larger of."""
    assert worker.memory() is None
