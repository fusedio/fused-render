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
    / "fused_render" / "ai" / "runners" / "llama_text.py"
)


@pytest.fixture()
def worker(monkeypatch):
    base = types.ModuleType("worker_base")
    base.CANCEL = threading.Event()
    base.download_file = lambda repo, filename, **kw: f"/blobs/{repo}/{filename}"
    base.serve_calls = []
    base.serve = lambda **kw: base.serve_calls.append(kw)
    base.recorded = {}
    base.set_state = lambda **fields: base.recorded.update(fields)
    # A read-only cache lookup in real life (`worker_base._cached_file`,
    # `try_to_load_from_cache` underneath — cannot start a download). The
    # fake mirrors that contract: a dict of already-cached (repo, file)
    # pairs a test populates, and a miss returns None rather than fetching
    # anything.
    base.cached_files = set()
    base._cached_file = (
        lambda repo, filename: f"/blobs/{repo}/{filename}"
        if (repo, filename) in base.cached_files else None)

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


def test_a_repo_id_resolves_to_its_already_cached_recipe(worker):
    """The regression code review found: `unsloth/Qwen3.5-9B-GGUF` downloaded
    through the filename id `Qwen3.5-9B-Q4_K_M.gguf` is what the AI Models
    page then offers a Load button for UNDER ITS REPO ID (the local cache is
    keyed by repo, not by this table's filenames) — and before this fix that
    second Load died with the "not curated" refusal for the exact model a
    user just fetched through this engine."""
    worker.worker_base.cached_files.add(
        ("unsloth/Qwen3.5-9B-GGUF", "Qwen3.5-9B-Q4_K_M.gguf"))
    path = worker.download("unsloth/Qwen3.5-9B-GGUF")
    assert path == "/blobs/unsloth/Qwen3.5-9B-GGUF/Qwen3.5-9B-Q4_K_M.gguf"


def test_a_repo_id_with_exactly_one_recipe_resolves_even_cold(worker):
    """No ambiguity to refuse when a repo curates only one quantization —
    `unsloth/Qwen3.8-27B-GGUF` has exactly one entry in the catalog."""
    path = worker.download("unsloth/Qwen3.8-27B-GGUF")
    assert path == ("/blobs/unsloth/Qwen3.8-27B-GGUF/"
                    "Qwen3.8-27B-UD-Q3_K_XL.gguf")


def test_a_repo_id_with_two_recipes_and_nothing_cached_is_refused_by_name(worker):
    """`unsloth/Qwen3.5-4B-GGUF` curates TWO quantizations
    (Q5_K_M and Q8_0) — with neither on disk yet, a bare repo id is genuinely
    ambiguous, and guessing would risk a multi-gigabyte download of the wrong
    one rather than a `FileNotFoundError`."""
    with pytest.raises(RuntimeError, match="ambiguous"):
        worker.download("unsloth/Qwen3.5-4B-GGUF")


def test_load_also_resolves_a_repo_id_the_same_way(worker, monkeypatch):
    """`load`'s own curation check must not regress independently of
    `download`'s — both call `_resolve_model_id`."""
    fake_llama_cpp = types.ModuleType("llama_cpp")

    class Llama:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.metadata = {}

    fake_llama_cpp.Llama = Llama
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama_cpp)
    worker.worker_base.cached_files.add(
        ("unsloth/Qwen3.8-27B-GGUF", "Qwen3.8-27B-UD-Q3_K_XL.gguf"))

    worker.load("unsloth/Qwen3.8-27B-GGUF", "/blobs/whatever.gguf")
    assert worker._loaded["llm"].kwargs["model_path"] == "/blobs/whatever.gguf"


def test_every_curated_recipe_is_also_in_the_catalog(worker):
    """The two tables answer different questions — this one "how do I fetch
    it", the catalog "should I suggest it" — and a model this runner can load
    that the catalog never mentions (or the reverse) is exactly the drift
    `formats.COMPONENT_REPOS`'s docstring warns about one level up.

    Uses the `worker` fixture rather than a second hand-rolled import: an
    EARLIER version of this test loaded the module a second time with
    `sys.modules.setdefault("worker_base", ...)`, which is not undone by
    `setdefault` and is not `monkeypatch`-tracked either — an empty stub
    module survived for the rest of the xdist worker's session and was ready
    to be handed to the next test's bare `import worker_base`, an
    order-dependent flake nothing here was exercising on purpose. The fixture
    already does this safely (`monkeypatch.setitem`).
    """
    from fused_render.ai import catalog

    recipe_ids = set(worker._GGUF_RECIPES)
    catalog_ids = {entry["id"] for entry in catalog.SUGGESTIONS["llamacpp-text"]}
    assert recipe_ids == catalog_ids


# -- the chat template, rendered from the GGUF's own metadata ---------------


class _FakeLlamaModel:
    """Mirrors `llama_cpp._internals.LlamaModel`, reachable as `Llama._model`
    — the surface this runner actually calls. `Llama` ITSELF has no
    `token_get_text`/`add_bos_token`/`add_eos_token` (checked against the
    installed 0.3.29): a fake that put those methods directly on `_FakeLlama`
    would be MORE capable than the real object, which is exactly what let a
    call to the wrong one pass every test while raising `AttributeError` in
    production (code review finding 1)."""

    def __init__(self, texts=None, add_bos=True, add_eos=False):
        self._texts = {1: "<bos>", 2: "<eos>"} if texts is None else texts
        self._add_bos = add_bos
        self._add_eos = add_eos

    def token_get_text(self, token_id):
        return self._texts.get(token_id, "")

    def add_bos_token(self):
        return self._add_bos

    def add_eos_token(self):
        return self._add_eos


class _FakeLlama:
    """Enough of `llama_cpp.Llama` to answer what this runner asks of it."""

    def __init__(self, chunks=(), metadata=None, tokens=5,
                add_bos_token=True, add_eos_token=False):
        self._chunks = list(chunks)
        self.metadata = metadata or {}
        self._tokens = tokens
        self._model = _FakeLlamaModel(add_bos=add_bos_token, add_eos=add_eos_token)
        self.tokenize_calls = []

    def token_bos(self):
        return 1

    def token_eos(self):
        return 2

    def tokenize(self, data, add_bos=True):
        self.tokenize_calls.append(add_bos)
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
    """A bad template must cost the RENDER, never the generation.

    `jinja2.exceptions.UndefinedError` (raised by `{{ undefined.attr }}`) IS a
    `TemplateError` subclass — confirmed against the installed jinja2 — so
    `_prompt_text`'s narrowed `except jinja2.exceptions.TemplateError` still
    catches this real template defect and only this real template defect;
    see the "only a template's own failure is caught" tests below for what
    it must NOT catch.
    """
    llm = _FakeLlama(metadata={"tokenizer.chat_template": "{{ undefined.attr }}"})
    rendered = worker._prompt_text(
        llm, [{"role": "user", "content": "hi"}], "")
    assert rendered == "hi"


def test_a_programming_bug_in_render_is_not_swallowed_as_a_template_failure(worker):
    """The exact regression code review finding 1 named: `_prompt_text` used
    to catch bare `Exception`, so a call to a method that does not exist on
    the real `Llama` (see `_FakeLlamaModel`'s docstring) silently produced
    the plain-join fallback on EVERY chat request, with nothing on screen
    saying why. A `TypeError` from this module's own code must propagate."""
    llm = _FakeLlama(metadata={"tokenizer.chat_template": _TEMPLATE})

    def boom(*_args, **_kwargs):
        raise TypeError("not a template problem")

    import jinja2

    real_environment = jinja2.Environment
    try:
        jinja2.Environment = boom
        with pytest.raises(TypeError, match="not a template problem"):
            worker._prompt_text(llm, [{"role": "user", "content": "hi"}], "")
    finally:
        jinja2.Environment = real_environment


def test_bos_and_eos_tokens_reach_the_template_via_the_internal_model(worker):
    """The exact call path code review finding 1 named: `Llama` itself has no
    `token_get_text` (checked against the installed 0.3.29), so this has to
    reach `llm._model.token_get_text` — and the fake mirrors that shape
    rather than shortcutting it, so a regression to the wrong call raises
    `AttributeError` here instead of silently falling back to the plain join.

    `add_bos_token=False`/`add_eos_token=False`: the template has to render
    the literal text itself for a model that does not auto-add either, which
    is the one case `_bos_token_for_template`/`_eos_token_for_template` hand
    back a non-empty string.
    """
    llm = _FakeLlama(
        metadata={"tokenizer.chat_template": "{{ bos_token }}mid{{ eos_token }}"},
        add_bos_token=False, add_eos_token=False)
    assert worker._prompt_text(llm, [], "") == "<bos>mid<eos>"


def test_bos_token_is_omitted_when_create_completion_will_add_it_itself(worker):
    """`create_completion` decides on its own, every call, whether to
    prepend the real BOS token (`Llama._model.add_bos_token()`) — a template
    that ALSO renders the literal `bos_token` string would then put two BOS
    tokens in the sequence. `add_bos_token=True` (the common case) must
    render an EMPTY bos_token so the template's own text carries none."""
    llm = _FakeLlama(
        metadata={"tokenizer.chat_template": "[{{ bos_token }}]"},
        add_bos_token=True)
    assert worker._prompt_text(llm, [], "") == "[]"


def test_a_model_with_no_content_or_multimodal_content_does_not_crash_the_fallback(worker):
    """The fallback join is the HOT path once finding 1 is fixed (a model
    with no chat template still needs it), and `content` is not always a
    string on the wire: `None` (a tool-call-only turn) and a multimodal parts
    list both reached `"\n\n".join(...)` before this and raised `TypeError`
    on the one path that had no chat template to fall back FROM."""
    llm = _FakeLlama(metadata={})
    rendered = worker._prompt_text(llm, [
        {"role": "user", "content": None},
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]},
    ], "")
    assert rendered == "\n\nhi"


# -- prompt token counting (SPEC AI-3) ---------------------------------------


def test_the_tokenized_prompt_length_is_the_input_token_count(worker):
    llm = _FakeLlama(tokens=7)
    assert worker._prompt_tokens(llm, "hello") == 7


def test_a_tokenizer_that_raises_costs_the_metric_not_the_generation(worker):
    class Boom(_FakeLlama):
        def tokenize(self, data, add_bos=True):
            raise RuntimeError("no")

    assert worker._prompt_tokens(Boom(), "hello") is None


def test_the_token_count_follows_the_models_own_bos_policy(worker):
    """A fixed `add_bos=True` counted a token `create_completion` might not
    actually add — metric drift on every model whose GGUF turns its own
    auto-BOS off, which is the same flag `_bos_token_for_template` reads for
    the same reason (code review finding 8)."""
    auto_bos = _FakeLlama(add_bos_token=True)
    worker._prompt_tokens(auto_bos, "hello")
    assert auto_bos.tokenize_calls == [True]

    manual_bos = _FakeLlama(add_bos_token=False)
    worker._prompt_tokens(manual_bos, "hello")
    assert manual_bos.tokenize_calls == [False]


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


def test_memory_is_actually_wired_into_serve(worker):
    """`memory()` returning None is only meaningful if `worker_base.serve`
    is actually told about it — `torch_text.main`/`torch_image.main` both
    pass `memory=memory`, and this runner's own `main()` used to omit the
    kwarg entirely, which made the function dead code no future real
    measurement could ever reach (code review finding 7)."""
    worker.main()
    assert worker.worker_base.serve_calls[-1]["memory"] is worker.memory
