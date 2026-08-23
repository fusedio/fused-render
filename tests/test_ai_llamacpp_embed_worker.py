"""The llama.cpp / GGUF TEXT EMBEDDING runner's own behaviour — what is true
of `runners/llama_embed.py` and nothing else.

The contract half (routes, states, progress, the port handshake) is
`worker_base`'s and is covered by `tests/test_ai_worker_base.py`. The request
shape and the prompt rules are `text_embed_common`'s and are covered by
`tests/test_ai_text_embed_common.py`. What is left here is what this runner
decides for itself: which ids it will fetch, **which repos it refuses and how
early**, how it sizes a context from a GGUF header, and what a round trip
through it returns.

Loaded by PATH with `worker_base` primed in `sys.modules`, exactly as
`tests/test_ai_llamacpp_worker.py` loads its own runner: the module finds its
base off `sys.path` in an interpreter of its own, so importing it the packaged
way would be testing an import that never ships.

**`llama_cpp` is not installed in this test venv, per AI-11c** — and on the
machine this file was written on it could not have been: the maintainer's
pinned CPU wheel aborts with `0xc000001d` (illegal instruction) on any x86_64
CPU without the instruction set its `ggml-cpu.dll` was built against, which
that machine lacks. Every path that would touch it therefore works against
`_loaded["llm"]` set directly to a fake, exactly as the chat runner's tests
do — see `_FakeLlama`, whose `embed` returns the same nested-list shape
llama-cpp-python 0.3.29's own `Llama.embed` returns for a pooled model (read
out of its `llama.py`, the `decode_batch` branch on `pooling_type`).
"""
import importlib.util
import math
import struct
import sys
import threading
import types
from pathlib import Path

import pytest

WORKER_PATH = str(
    Path(__file__).resolve().parents[1]
    / "fused_render" / "ai" / "runners" / "llama_embed.py"
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
    base.cached_files = set()
    base._cached_file = (
        lambda repo, filename: f"/blobs/{repo}/{filename}"
        if (repo, filename) in base.cached_files else None)
    base.repo_folder = lambda model_id, repo_type="model": None

    monkeypatch.setitem(sys.modules, "worker_base", base)
    spec = importlib.util.spec_from_file_location(
        "llamacpp_embed_worker_under_test", WORKER_PATH)
    assert spec is not None and spec.loader is not None, WORKER_PATH
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.worker_base = base
    # A fresh process is what ships; a module-level dict shared between tests
    # in one process is not.
    module._loaded.clear()
    return module


def _fake_huggingface_hub(monkeypatch, *, files=None, error=None):
    """A fake `huggingface_hub` good enough for `_resolve_uncurated_repo`'s
    networked path. `list_repo_files` either returns `files` or raises
    `error`, mirroring the one function that code calls.

    `hf_hub_url` and `huggingface_hub.utils` are deliberately ABSENT unless a
    test adds them, so `_remote_header`'s import fails and it returns `b""` —
    which is the "could not look" state, and therefore the state in which
    this runner must NOT refuse. Every refusal test that wants the header
    peek to have an opinion supplies one explicitly (`_fake_remote_header`).
    """
    fake = types.ModuleType("huggingface_hub")
    calls = []

    def list_repo_files(model_id, **kwargs):
        calls.append(model_id)
        if error is not None:
            raise error
        return list(files or [])

    fake.list_repo_files = list_repo_files
    fake.calls = calls
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    return fake


def _gguf_header(**uints):
    """A minimal but REAL GGUF header carrying `uints` as `<arch>.<key>`
    pairs — parsed by `formats`' own reader rather than by a stub of it.

    Hand-built because the point of several tests below is that the runner
    reads bytes, and a fake that returned an integer directly would test the
    test. The layout is the GGUF spec's: magic, version, tensor count, kv
    count, then length-prefixed keys each followed by a type tag and a value.
    Type 4 is `uint32`, which is what a real converter writes for both
    `pooling_type` and `context_length` (verified against four published
    embedding GGUFs).
    """
    out = bytearray(b"GGUF")
    out += struct.pack("<I", 3)          # version
    out += struct.pack("<Q", 0)          # tensor count
    out += struct.pack("<Q", len(uints))  # kv count
    for key, value in uints.items():
        encoded = key.encode()
        out += struct.pack("<Q", len(encoded)) + encoded
        out += struct.pack("<I", 4) + struct.pack("<I", value)
    return bytes(out)


def _fake_remote_header(monkeypatch, worker, header):
    """Make `_remote_header` answer with `header` — the bytes a 2MB `Range`
    request would have returned — without any network."""
    monkeypatch.setattr(worker, "_remote_header", lambda repo, filename: header)


class _FakeLlama:
    """`llama_cpp.Llama`'s embedding surface, and only it.

    `embed(list)` returns one flat vector per input, which is what the real
    class returns whenever the model declares a pooling type — the ONLY case
    this runner can reach, because `load()` refuses every file that does not.
    The vectors are deliberately NOT unit length, so a test asserting
    normalization is asserting that the runner did it rather than that the
    fixture happened to.
    """

    def __init__(self, dim=4, **kwargs):
        self.dim = dim
        self.kwargs = kwargs
        self.seen = []

    def embed(self, texts):
        self.seen.append(list(texts))
        # A different, non-unit vector per input, derived from its length so
        # two different prompts cannot accidentally produce the same row.
        return [[float(len(text) + i + 1) for i in range(self.dim)]
                for text in texts]


# -- resolution: the curated table, and D412's generic fallback ----------------


def test_download_fetches_exactly_the_one_curated_file(worker):
    """No snapshot call and no second repo — `worker_base.download_file` is
    the whole of it, exactly as the chat runner does."""
    path = worker.download("nomic-embed-text-v1.5.Q8_0.gguf")
    assert path == ("/blobs/nomic-ai/nomic-embed-text-v1.5-GGUF/"
                    "nomic-embed-text-v1.5.Q8_0.gguf")


def test_a_curated_repo_id_resolves_to_its_one_recipe(worker):
    """The id shape the AI Models page's cache scan hands back is a REPO id,
    because that scan is keyed by repo folder and knows nothing of this
    table's filename keys."""
    path = worker.download("Qwen/Qwen3-Embedding-0.6B-GGUF")
    assert path.endswith("Qwen3-Embedding-0.6B-Q8_0.gguf")


def test_an_uncurated_repo_resolves_through_the_EMBEDDING_picker(worker, monkeypatch):
    """`pick_embedding_gguf_file`, not `pick_gguf_file` — and this is the test
    that tells them apart.

    Both pickers see the same two files. The chat picker prefers `Q4_K_M`
    because a chat model's download is the binding cost; this capability
    prefers `Q8_0` because these models are tens of megabytes and
    quantization error lands directly in the vector with no sampling step
    after it to absorb the damage. If this runner were wired to the chat
    picker every assertion in this file would still pass except this one.
    """
    fake = _fake_huggingface_hub(monkeypatch, files=[
        "README.md", "model-Q4_K_M.gguf", "model-Q8_0.gguf",
    ])
    _fake_remote_header(monkeypatch, worker, b"")
    path = worker.download("some-org/not-in-the-table")
    assert path == "/blobs/some-org/not-in-the-table/model-Q8_0.gguf"
    assert fake.calls == ["some-org/not-in-the-table"]


# -- REFUSING BEFORE THE DOWNLOAD, which is half of why this runner exists ----


def test_a_safetensors_repo_is_refused_without_downloading_anything(worker, monkeypatch):
    """**The defect this capability was written not to repeat.** The
    `embeddings` capability answers `BAAI/bge-small-en-v1.5` by fetching
    ~400MB, starting a worker and dying on a missing method. Here a repo that
    publishes no GGUF is refused off its FILE LISTING alone.

    The assertion that matters is not the message — it is that
    `worker_base.download_file` was never called. A refusal that arrives
    after the download is the old behaviour with better prose.
    """
    _fake_huggingface_hub(monkeypatch, files=[
        "README.md", "config.json", "model.safetensors", "tokenizer.json",
    ])
    fetched = []
    monkeypatch.setattr(worker.worker_base, "download_file",
                        lambda *a, **k: fetched.append(a))

    with pytest.raises(RuntimeError) as excinfo:
        worker.download("sentence-transformers/all-MiniLM-L6-v2")

    assert fetched == []
    message = str(excinfo.value)
    # Names the repo, says what it looks like, says what to pass instead —
    # the three things `_NOT_GGUF` promises.
    assert "sentence-transformers/all-MiniLM-L6-v2" in message
    assert "safetensors" in message
    assert "all-MiniLM-L6-v2-GGUF" in message


def test_a_chat_gguf_is_refused_on_its_header_before_downloading(worker, monkeypatch):
    """The second refusal, and the one a file listing cannot make.

    A repo can publish a perfectly good GGUF that is a CHAT model — and for
    the Qwen3 family the architecture string is identical to the embedding
    one, so no amount of filename or listing reading settles it. The runner
    reads the file's real header over a 2MB `Range` request and refuses on
    what the bytes say, still before any weight is fetched.
    """
    _fake_huggingface_hub(monkeypatch, files=["Qwen3-4B-Q8_0.gguf"])
    # A chat GGUF's header: a context length, and NO pooling key at all.
    _fake_remote_header(monkeypatch, worker,
                        _gguf_header(**{"qwen3.context_length": 32768}))
    fetched = []
    monkeypatch.setattr(worker.worker_base, "download_file",
                        lambda *a, **k: fetched.append(a))

    with pytest.raises(RuntimeError) as excinfo:
        worker.download("some-org/Qwen3-4B-GGUF")

    assert fetched == []
    message = str(excinfo.value)
    assert "no pooling type" in message
    # Points at the endpoint that DOES serve it, rather than only saying no.
    assert "text generation" in message


def test_a_reranker_is_refused_with_its_own_sentence(worker, monkeypatch):
    """A cross-encoder is neither of the other two cases and must not be told
    to go and use text generation: it scores a (query, document) PAIR and has
    no per-text vector, so there is no endpoint here for it at all."""
    _fake_huggingface_hub(monkeypatch, files=["bge-reranker-base-Q8_0.gguf"])
    _fake_remote_header(monkeypatch, worker,
                        _gguf_header(**{"bert.pooling_type": 4}))  # RANK

    with pytest.raises(RuntimeError) as excinfo:
        worker.download("BAAI/bge-reranker-base-gguf")

    message = str(excinfo.value)
    assert "RERANKER" in message
    assert "cosine similarity" in message


def test_an_embedding_gguf_passes_the_header_check_and_downloads(worker, monkeypatch):
    """The other side of the two tests above: a real embedding header is
    waved through rather than refused, so the peek is a filter and not a
    wall."""
    _fake_huggingface_hub(monkeypatch, files=["e5-base-Q8_0.gguf"])
    _fake_remote_header(monkeypatch, worker,
                        _gguf_header(**{"bert.pooling_type": 1}))  # MEAN
    path = worker.download("some-org/multilingual-e5-base-gguf")
    assert path == "/blobs/some-org/multilingual-e5-base-gguf/e5-base-Q8_0.gguf"


def test_an_unreadable_header_does_NOT_refuse(worker, monkeypatch):
    """**A refusal must rest on evidence.** No network, a mirror that ignores
    `Range`, a timeout — all of them make `_remote_header` return `b""`, and
    the runner must then proceed and let the load-time check answer instead.

    The alternative is a user who cannot load a model that would have worked,
    with no way to override, because the Hub was briefly slow.
    """
    _fake_huggingface_hub(monkeypatch, files=["mystery-Q8_0.gguf"])
    _fake_remote_header(monkeypatch, worker, b"")
    path = worker.download("some-org/mystery")
    assert path.endswith("mystery-Q8_0.gguf")


def _fake_hf_session(monkeypatch, status, content, seen=None):
    """Stand in for `huggingface_hub.utils.get_session()`, recording the
    kwargs `_remote_header` passes so a client-specific keyword cannot creep
    back in unnoticed."""
    class _Response:
        def __init__(self):
            self.status_code = status
            self.content = content

    class _Session:
        def get(self, url, **kwargs):
            if seen is not None:
                seen.append((url, kwargs))
            return _Response()

    utils = types.ModuleType("huggingface_hub.utils")
    utils.get_session = lambda: _Session()
    utils.build_hf_headers = lambda: {}
    hub = types.ModuleType("huggingface_hub")
    hub.hf_hub_url = lambda repo, filename, **kw: f"https://hub/{repo}/{filename}"
    hub.utils = utils
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "huggingface_hub.utils", utils)


def test_the_header_peek_reads_a_206_and_asks_for_a_range(worker, monkeypatch):
    """**A regression test for a bug that shipped silently and was found by
    running the server, not by the suite.**

    The first draft called `get_session().get(..., stream=True)` and read
    `response.raw`. That is the `requests` spelling; `huggingface_hub` 1.x
    returns an **httpx.Client**, whose `get()` has no `stream` keyword — so
    every call raised `TypeError`, `_remote_header`'s deliberately broad
    `except` turned it into `b""`, and the peek never ran. Nothing failed:
    "could not look" is a legitimate state that means "do not refuse", so the
    runner simply started downloading chat models it should have refused.

    This test pins the two things that would have caught it: the bytes come
    back, and the request carries a `Range` header and NO client-specific
    keyword.
    """
    seen = []
    _fake_hf_session(monkeypatch, 206, b"GGUFxx", seen)
    assert worker._remote_header("org/r", "m.gguf") == b"GGUFxx"
    (url, kwargs), = seen
    assert url == "https://hub/org/r/m.gguf"
    assert kwargs["headers"]["Range"].startswith("bytes=0-")
    # `stream`/`follow_redirects`/`allow_redirects` are each spelled by only
    # ONE of the two clients hf has shipped, so naming any of them is the
    # same TypeError-into-silence trap this test exists for.
    assert not ({"stream", "follow_redirects", "allow_redirects"} & set(kwargs))


def test_a_200_is_treated_as_a_failure_to_look_not_as_a_header(worker, monkeypatch):
    """**The memory bound, and the reason it is a status check.** A `206` is
    the server saying it honoured the `Range`, so the body cannot exceed the
    2MB asked for. A plain `200` is a server that ignored it and is handing
    over the whole multi-gigabyte file — which is exactly what a function
    whose job is to avoid a download must not read.

    Answering `b""` puts it in the "no evidence" state, so the runner
    proceeds and the load-time check decides. That is the right direction:
    a mirror with no Range support costs a user one download, not a model
    they cannot load.
    """
    _fake_hf_session(monkeypatch, 200, b"GGUF" + b"x" * 10_000)
    assert worker._remote_header("org/r", "m.gguf") == b""


def test_a_lookup_failure_is_a_different_refusal_from_a_bad_repo(worker, monkeypatch):
    """"Try again" and "this will never resolve" are different facts a user
    acts on differently — the distinction `llama_text` draws too."""
    _fake_huggingface_hub(monkeypatch, error=RuntimeError("offline"))
    with pytest.raises(RuntimeError, match="[Cc]ould not read"):
        worker.download("some-org/unreachable")


def test_a_repo_already_on_disk_needs_no_network_and_no_peek(worker, monkeypatch, tmp_path):
    """The local-cache-first fast path. A file already on this disk gets its
    header read by `load()` off the real bytes moments later, which is
    strictly better evidence than a range request — so neither the listing
    nor the peek is worth paying for."""
    snapshot = tmp_path / "snapshots" / "main"
    snapshot.mkdir(parents=True)
    (snapshot / "e5-base-Q8_0.gguf").write_bytes(b"GGUF")
    monkeypatch.setattr(worker.worker_base, "repo_folder",
                        lambda model_id, repo_type="model": str(tmp_path))

    def _no_network(*a, **k):
        raise AssertionError("the Hub must not be asked for a cached repo")

    monkeypatch.setattr(worker, "_remote_header", _no_network)
    fake = _fake_huggingface_hub(monkeypatch, files=[])
    fake.list_repo_files = _no_network

    path = worker.download("some-org/already-here")
    assert path.endswith("e5-base-Q8_0.gguf")


# -- load: the authoritative header read, and the context sizing --------------


def _load(worker, monkeypatch, tmp_path, model_id, header, *, dim=4):
    """Drive `load()` with a fake `llama_cpp` and a real header on disk.

    The header is written to a REAL file because `load()` reads it through
    `formats.gguf_pooling_type`, which opens a path — the same read that runs
    in production, rather than a stub of it.
    """
    path = tmp_path / "model.gguf"
    path.write_bytes(header)
    made = {}

    def _Llama(**kwargs):
        made.update(kwargs)
        return _FakeLlama(dim=dim, **kwargs)

    fake = types.ModuleType("llama_cpp")
    fake.Llama = _Llama
    fake.llama_supports_gpu_offload = lambda: False
    monkeypatch.setitem(sys.modules, "llama_cpp", fake)
    worker.load(model_id, str(path))
    return made


def test_load_refuses_a_chat_gguf_that_reached_the_disk(worker, monkeypatch, tmp_path):
    """The authoritative check. `_refuse_unloadable` may never have run — the
    peek can fail, and a locally-cached model skips it entirely — so this is
    the one that always runs, and it gives the identical sentence: the user's
    mistake is the same and only the timing differs."""
    _fake_huggingface_hub(monkeypatch, files=["chat-Q8_0.gguf"])
    _fake_remote_header(monkeypatch, worker, b"")
    with pytest.raises(RuntimeError, match="no pooling type"):
        _load(worker, monkeypatch, tmp_path, "some-org/chat-gguf",
              _gguf_header(**{"qwen3.context_length": 4096}))


@pytest.mark.parametrize("declared,expected", [
    # A bge's own 512 is used as-is.
    (512, 512),
    # nomic's 2048, likewise — one pinned number would have truncated it at a
    # bge's context or over-allocated for a bge.
    (2048, 2048),
    # Qwen3-Embedding declares 32768, which is real and is clamped: a KV cache
    # that size is memory spent on a length no batch API capped at 64 short
    # items will ever pass.
    (32768, 8192),
])
def test_the_context_is_sized_from_the_gguf_and_clamped(
        worker, monkeypatch, tmp_path, declared, expected):
    """`n_ctx` here is not a conversation budget that truncates gracefully —
    llama.cpp pools only within one batch, so it is the longest text the
    caller can embed. `n_batch`/`n_ubatch` must follow it, or
    llama-cpp-python's own `Llama.embed` refuses anything over its default
    512 with "Requested tokens (N) exceed batch size"."""
    _fake_huggingface_hub(monkeypatch, files=["e5-Q8_0.gguf"])
    _fake_remote_header(monkeypatch, worker, b"")
    made = _load(worker, monkeypatch, tmp_path, "some-org/e5-gguf",
                 _gguf_header(**{"bert.pooling_type": 1,
                                 "bert.context_length": declared}))
    assert made["n_ctx"] == expected
    assert made["n_batch"] == expected
    assert made["n_ubatch"] == expected
    # The flag the whole runner turns on; without it `Llama.embed` raises.
    assert made["embedding"] is True


def test_a_header_with_no_context_length_falls_back_conservatively(
        worker, monkeypatch, tmp_path):
    """512 rather than something generous, and that is the safe direction:
    every published text encoder supports at least that, while a larger guess
    on a 512-position model produces vectors from positions it never saw."""
    _fake_huggingface_hub(monkeypatch, files=["x-Q8_0.gguf"])
    _fake_remote_header(monkeypatch, worker, b"")
    made = _load(worker, monkeypatch, tmp_path, "some-org/x-gguf",
                 _gguf_header(**{"bert.pooling_type": 2}))
    assert made["n_ctx"] == 512


def test_load_remembers_the_curated_prompt_scheme(worker, monkeypatch, tmp_path):
    """The scheme travels with the loaded weights rather than being
    re-derived per request — it is a property of the model, and `generate` is
    on the hot path of a 64-item batch."""
    _load(worker, monkeypatch, tmp_path, "nomic-embed-text-v1.5.Q8_0.gguf",
          _gguf_header(**{"nomic-bert.pooling_type": 1}))
    assert worker._loaded["scheme"] == "nomic"


def test_an_uncurated_model_gets_its_scheme_from_the_filename(
        worker, monkeypatch, tmp_path):
    """The documented heuristic — the filename is the only evidence a GGUF
    header carries about a prompt convention. It is reported back on every
    reply precisely because it is a guess."""
    _fake_huggingface_hub(monkeypatch, files=["bge-large-en-v1.5-Q8_0.gguf"])
    _fake_remote_header(monkeypatch, worker, b"")
    path = tmp_path / "bge-large-en-v1.5-Q8_0.gguf"
    path.write_bytes(_gguf_header(**{"bert.pooling_type": 2}))
    fake = types.ModuleType("llama_cpp")
    fake.Llama = lambda **kwargs: _FakeLlama(**kwargs)
    fake.llama_supports_gpu_offload = lambda: False
    monkeypatch.setitem(sys.modules, "llama_cpp", fake)
    worker.load("some-org/bge-large-en-v1.5-gguf", str(path))
    assert worker._loaded["scheme"] == "bge"


# -- the round trip -----------------------------------------------------------


def test_a_batch_round_trips_with_the_right_dim_and_unit_length(worker):
    """The core contract: one vector per text, of the model's own width, and
    every one of them unit length so a cosine similarity is a plain dot
    product. The fake returns deliberately non-unit rows, so this asserts the
    runner normalized rather than that the fixture did."""
    worker._loaded["llm"] = _FakeLlama(dim=6)
    worker._loaded["scheme"] = "none"

    result = worker.generate({"texts": ["alpha", "beta", "gamma"]})

    assert result["dim"] == 6
    assert len(result["vectors"]) == 3
    for row in result["vectors"]:
        assert len(row) == 6
        assert math.isclose(math.sqrt(sum(v * v for v in row)), 1.0, rel_tol=1e-9)


def test_the_document_prefix_is_applied_by_default(worker):
    """`kind` defaults to "document", and on a scheme with a document prefix
    that prefix reaches the model — checked at the model boundary rather than
    by inspecting the reply, because what the encoder READ is the thing that
    decides the vector."""
    llm = _FakeLlama()
    worker._loaded["llm"] = llm
    worker._loaded["scheme"] = "nomic"

    result = worker.generate({"texts": ["the roof leaks"]})

    assert llm.seen == [["search_document: the roof leaks"]]
    assert result["kind"] == "document"
    assert result["promptScheme"] == "nomic"


def test_the_query_prefix_is_a_different_prefix(worker):
    """The asymmetry, demonstrated: the same text embedded as a query reaches
    the model differently from the same text embedded as a document. This is
    the whole reason `kind` exists."""
    llm = _FakeLlama()
    worker._loaded["llm"] = llm
    worker._loaded["scheme"] = "nomic"

    worker.generate({"texts": ["the roof leaks"], "kind": "query"})
    worker.generate({"texts": ["the roof leaks"], "kind": "document"})

    assert llm.seen == [["search_query: the roof leaks"],
                        ["search_document: the roof leaks"]]


def test_bges_document_side_is_no_prefix_at_all(worker):
    """The tie-breaker behind the "document" default: bge's card instructs the
    QUERY only, so a bare call on that family embeds text verbatim — exactly
    what someone who has never heard of prompt schemes expects."""
    llm = _FakeLlama()
    worker._loaded["llm"] = llm
    worker._loaded["scheme"] = "bge"

    worker.generate({"texts": ["the roof leaks"]})

    assert llm.seen == [["the roof leaks"]]


def test_generate_with_no_model_says_so(worker):
    worker._loaded.clear()
    with pytest.raises(RuntimeError, match="no model is loaded"):
        worker.generate({"texts": ["a"]})


def test_the_recipes_and_the_catalog_cannot_drift(worker):
    """Two tables key the same models by the same filenames, and nothing but
    this stops them diverging — the pin `test_ai_llamacpp_worker.py` already
    has for the chat pair, which `formats.TEXT_EMBED_RECIPES`' own docstring
    promises exists for this one.

    A recipe with no catalog row is a model nobody is offered; a catalog row
    with no recipe is a Download button whose resolution then raises. Neither
    fails anywhere else.
    """
    from fused_render.ai import catalog
    from fused_render.ai.runners import formats

    assert set(formats.TEXT_EMBED_RECIPES) == {
        entry["id"] for entry in catalog.SUGGESTIONS["llamacpp-embed"]}


def test_a_downloaded_embedding_gguf_still_has_a_capability(worker):
    """**The Load button, and the bug that took it away.**

    `worker_base.download_file` fetches the ONE `.gguf` and nothing else, so a
    curated model's snapshot has no README and no config — `_format_task`
    returns None for it, and the capability a cached card shows comes instead
    from `hub_cache._engine`, which intersects `meta.loaders` with
    `formats.DECISIVE`.

    The first cut of this change added the new codes to `loaders()` and NOT to
    `DECISIVE`, so that intersection was empty: a model the user had just
    downloaded rendered with no task, no engine tag and no Load button, while
    `cached_capability` recovered it through its own `meta.loaders` fallback —
    card and load route disagreeing, which is the exact split `_engine`'s
    docstring says cannot happen. Chat GGUFs were unaffected, so nothing else
    noticed.
    """
    from fused_render.ai.runners import formats

    loaders = formats.loaders(
        repo_id="nomic-ai/nomic-embed-text-v1.5-GGUF",
        names={"nomic-embed-text-v1.5.Q8_0.gguf"}, dirnames=set(), config={},
        torch_weights=False, gguf_architecture="nomic-bert",
        gguf_pooling_type=formats.GGUF_POOLING_MEAN)
    assert set(loaders) & set(formats.DECISIVE) == set(loaders), (
        f"{sorted(set(loaders) - set(formats.DECISIVE))} are runners "
        f"`loaders()` names for a GGUF snapshot but `DECISIVE` does not, so a "
        f"cached repo they load gets no capability and no Load button")


def test_the_embedding_picker_takes_files_the_chat_picker_refuses(worker):
    """Why hub search needs its own branch rather than reusing
    `pick_gguf_file` now that a second capability declares
    `hub_filter_tags=("gguf",)`.

    `_gguf_rank` excludes F16/BF16/F32 outright — correct for a chat model,
    where full precision is a download nobody wants — and `pick_gguf_file`
    falls back only when there is exactly one candidate. A small encoder
    converted with stock `convert_hf_to_gguf.py` publishes precisely
    `…-f16.gguf` and `…-f32.gguf` and nothing else, so the chat picker drops
    the repo from Discover entirely while the embedding picker ranks both.
    """
    from fused_render.ai.runners import formats

    names = ["model-f16.gguf", "model-f32.gguf", "README.md"]
    assert formats.pick_gguf_file(names) is None
    # F16 over F32: twice the bytes for weights trained in half precision.
    assert formats.pick_embedding_gguf_file(names) == "model-f16.gguf"


def test_the_two_recipe_tables_share_no_key(worker):
    """`formats.gguf_recipe` consults the chat table FIRST, so a shared key
    would silently resolve an embedding id to a chat model's repo and
    download several gigabytes of the wrong thing. Disjoint by construction
    today; pinned so it stays that way."""
    from fused_render.ai.runners import formats

    assert not (set(formats.GGUF_RECIPES) & set(formats.TEXT_EMBED_RECIPES))


def test_main_wires_every_callback_the_base_expects(worker):
    """An unwired `memory()` would be silently ignored forever, including the
    day someone gives it a real probe — the same rule `llama_text.memory`'s
    docstring states."""
    worker.main()
    (kwargs,) = worker.worker_base.serve_calls
    assert kwargs["download"] is worker.download
    assert kwargs["load"] is worker.load
    assert kwargs["generate"] is worker.generate
    assert kwargs["memory"] is worker.memory
    assert kwargs["streaming"] is False
