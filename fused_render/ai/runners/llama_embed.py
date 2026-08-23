"""Text embeddings on llama.cpp / GGUF: one resident encoder, one route (SPEC §40).

**This module is the whole of the runner and it sits at the runners ROOT**,
beside `worker_base.py`, `formats.py` and `llama_text.py`: TWO folders serve
this one engine — `llamacpp_embed/` and `llamacpp_embed_vulkan/` — and each
holds only a `pyproject.toml` and a five-line `worker.py` shell around
`main()` below. They differ in which wheel index their manifest takes
`llama-cpp-python` from, and the hardware that names is a fact about the
wheel, never about the code. The arrangement, the naming rule and the reasons
for both are `llama_text.py`'s; read that module's docstring first, because
everything this one does NOT say is said there.

**Named `llama_embed`, not `llamacpp_embed`, for `llama_text.py`'s exact
reason** — a shared module whose stem matches a sibling FOLDER is a
`sys.path` footgun the moment anyone adds an `__init__.py`. Two folders here
import this one file, so the collision is two folders away from happening by
accident rather than zero.

-------------------------------------------------------------------------------

**Why this is a separate CAPABILITY from `embeddings`, and not a fifth model
type inside it.** `registry.EMBEDDINGS` serves DUAL ENCODERS — SigLIP and
CLIP — through `get_text_features`/`get_image_features`. An ordinary text
embedding model has neither method: it is an encoder plus a pooling
configuration, and loading one through that capability's runners fails
several frames inside transformers with `AttributeError: 'BertModel' object
has no attribute 'get_text_features'`, AFTER the download. Two capabilities
rather than one branch is a decision recorded at `registry.TEXT_EMBEDDINGS`;
what belongs HERE is the consequence a page can feel: because the supervisor
holds one resident model PER CAPABILITY, a user can keep a SigLIP model and a
text embedder loaded at the same time and neither evicts the other. Folding
these models into `embeddings` would have made "index my photos" and "search
my notes" fight over one slot on a machine with room for both.

-------------------------------------------------------------------------------

**The refusal happens BEFORE the download, and that is the point of half this
file.** The `embeddings` capability gets this wrong today: `fused.ai.embed`
with `BAAI/bge-small-en-v1.5` fetches ~400MB, starts a worker, and only then
dies on the missing method above — a multi-minute wait ending in a traceback
about an attribute, for a mistake that was decidable from the repo's file
listing before a single weight moved. This runner refuses in two places, both
ahead of the fetch:

1. **The listing.** A repo with no root-level `.gguf` at all cannot be loaded
   by llama.cpp under any circumstances, and `huggingface_hub.list_repo_files`
   answers that for free. This is the branch that catches
   `sentence-transformers/all-MiniLM-L6-v2` and `BAAI/bge-small-en-v1.5` — the
   two repos a person is most likely to try first, because they are the
   canonical names for this task and neither publishes GGUF.
2. **A 2MB `Range` request against the one file that WOULD be fetched.** A
   repo can publish a perfectly good GGUF that is a CHAT model, and no amount
   of filename reading settles that (see `formats.gguf_pooling_type`: the
   Qwen3-Embedding GGUF and the Qwen3 chat GGUF declare the same
   architecture). So this reads the file's actual header over HTTP — 2MB of a
   possibly multi-gigabyte file — and refuses on what the bytes say. Verified
   against the Hub on 2026-08-23: `resolve/main` answers `206 Partial
   Content` for a `Range` header and the first four bytes are `GGUF`.

Both refusals name the repo, say what it looks like, and say what to pass
instead. **Neither is allowed to refuse on ignorance**: if the listing cannot
be read or the range request does not come back, resolution proceeds and the
load-time check catches it — one download later, which is the old behaviour
and no worse than it. A refusal has to rest on evidence, because the cost of
a false one is a user who cannot load a model that would have worked and has
no way to override.

-------------------------------------------------------------------------------

**`n_ctx` is per-model here, unlike `llama_text._N_CTX`.** llama.cpp pools a
sequence only if the whole sequence fits in one batch, so for this runner
`n_ctx` is not a conversation budget that truncates gracefully — it is the
longest text the caller can embed. Sized from the GGUF's own
`context_length` (`formats.gguf_context_length`), which reads 512 for bge,
2048 for nomic-embed and EmbeddingGemma, and 32768 for Qwen3-Embedding: one
pinned number would either truncate the long models or allocate 32k of
context for a page embedding one-line labels. `n_batch` and `n_ubatch` are
raised to match, because llama.cpp's own embedding path asserts the batch can
hold the sequence and llama-cpp-python's `Llama.embed` raises
`ValueError: Requested tokens (N) exceed batch size` at its default 512
otherwise.

Deliberately llama-cpp-python + huggingface_hub only — no jinja2, unlike
`llama_text.py`: an encoder has no chat template to render, and a dependency
this runner does not use is a dependency users download for nothing.
"""

from __future__ import annotations

import os
import sys

# The base sits in THIS directory, and so does everything else this imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import formats  # noqa: E402 - the shared format checks and recipes; see formats.py
import text_embed_common  # noqa: E402 - the shared request shape and prompts
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded `Llama` instance, and what this process needs to remember about
#: the model beside it: the prompt scheme its vectors were trained for. One
#: per process.
_loaded = {}

#: What to allocate when the GGUF's own header does not say
#: (`formats.gguf_context_length` returned None). 512 rather than something
#: generous, and that is the CONSERVATIVE direction on purpose: every
#: published text encoder supports at least 512 tokens, so this cannot exceed
#: what the model was trained for, while a larger guess on a 512-token model
#: makes llama.cpp allocate and then produce vectors from positions the model
#: never saw during training.
_FALLBACK_N_CTX = 512

#: A ceiling on what is allocated even when the header asks for more.
#: Qwen3-Embedding declares 32768, which is real — and a KV cache for it is
#: memory spent on a length nobody passes to a batch embedding API capped at
#: `MAX_ITEMS` short items. 8192 covers every document anyone chunks for
#: retrieval; a caller who needs more should chunk, which is what a retrieval
#: pipeline does anyway.
_MAX_N_CTX = 8192

#: What to say for a repo that publishes no root-level GGUF. Names the repo,
#: what it IS, and the two ways forward — the sentence
#: `sentence-transformers/all-MiniLM-L6-v2` earns, since it is the single
#: most likely thing for someone to type here.
_NOT_GGUF = (
    "{model_id!r} publishes no GGUF file this engine can load ({count} "
    "file(s) checked). It looks like a safetensors/PyTorch checkpoint — the "
    "format sentence-transformers and transformers read, which llama.cpp "
    "cannot open at all. Pass a GGUF conversion of the same model instead "
    "(search the Hub for '{stem}-GGUF', or use one of the curated ids: "
    "{curated}), or load it on Apple Silicon through the MLX engine, which "
    "does read safetensors."
)

#: …and for a repo that DOES publish a GGUF which turns out to be a chat
#: model. A different fact and a different fix, so a different sentence.
_NOT_AN_EMBEDDING_MODEL = (
    "{model_id!r} resolves to {filename!r}, which is a GGUF but not an "
    "embedding model: its header declares no pooling type ({pooling}), which "
    "is how a text encoder says it produces one vector per text. A file like "
    "this is a causal chat model — it has per-token outputs and nothing to "
    "pool them with. Load it through text generation instead (fused.ai), or "
    "pass an embedding GGUF here: {curated}."
)

#: …and for the reranker case, which is neither of the above. A cross-encoder
#: scores a PAIR and has no per-text vector at all, so "use the other
#: endpoint" would be wrong advice — there is no endpoint here for it.
_IS_A_RERANKER = (
    "{model_id!r} resolves to {filename!r}, which is a RERANKER (its header "
    "declares pooling type RANK). A reranker scores a query and a document "
    "together and has no per-text vector to return, so there is nothing this "
    "endpoint could give you for it. Embed with one of {curated} and rank by "
    "cosine similarity instead."
)

#: What to say when an uncurated repo's own file listing could not be read.
#: Named apart from `_NOT_GGUF` for `llama_text._LOOKUP_FAILED`'s reason: this
#: one means "try again", that one means "this repo will never resolve".
_LOOKUP_FAILED = (
    "Could not read {model_id!r}'s file listing on the Hub ({error}) — an "
    "uncurated repo id resolves by reading which GGUF files it actually "
    "publishes, so a network or Hub problem here means the pick cannot be "
    "made right now."
)


def _curated_ids() -> str:
    """The curated ids, for a refusal sentence to point at.

    Read out of `formats.TEXT_EMBED_RECIPES` rather than written into the
    three messages above, so a refusal cannot come to recommend a model this
    app has stopped shipping — the failure mode where the error text is the
    last thing anyone updates.
    """
    return ", ".join(repr(key) for key in sorted(formats.TEXT_EMBED_RECIPES))


def _recipes_for_repo(repo_id):
    """Every curated recipe whose repo is `repo_id`, keyed by their filename ids."""
    return {key: recipe for key, recipe in formats.TEXT_EMBED_RECIPES.items()
            if recipe["repo"] == repo_id}


def _locally_cached_gguf_files(repo_id):
    """Root-level `.gguf` filenames `repo_id` already has ON DISK, no network.

    `llama_text._locally_cached_gguf_files`, and deliberately a copy rather
    than an import: these two modules are loaded as bare top-level modules by
    two different runner interpreters, and `llama_embed` importing
    `llama_text` would drag the chat runner's whole module — its jinja2 use,
    its offload schedule — into an environment whose manifest declares
    neither. See that function's docstring for the mechanism and for why an
    unreadable cache returns `[]` rather than raising.
    """
    folder = worker_base.repo_folder(repo_id)
    if not folder:
        return []
    names = set()
    try:
        with os.scandir(os.path.join(folder, "snapshots")) as entries:
            snapshot_dirs = [entry.path for entry in entries if entry.is_dir()]
    except OSError:
        return []
    for snapshot_dir in snapshot_dirs:
        try:
            with os.scandir(snapshot_dir) as entries:
                for entry in entries:
                    if (entry.name.lower().endswith(formats.GGUF_EXTENSION)
                            and entry.is_file()):
                        names.add(entry.name)
        except OSError:
            continue
    return sorted(names)


def _remote_header(repo_id, filename):
    """The first 2MB of a Hub-hosted GGUF, or `b""` if it could not be read.

    **The cheap half of "refuse before the download".** `hf_hub_url` builds
    the `resolve/main` URL huggingface_hub itself would fetch, and
    `build_hf_headers` supplies whatever token this machine holds so a gated
    repo answers rather than 401s. The `Range` header is what keeps this from
    being a download: the Hub answers `206 Partial Content` with exactly the
    requested bytes (checked directly, 2026-08-23).

    **Only `206 Partial Content` counts, and a plain `200` is treated as a
    failure to look.** That is the whole of what bounds this read. A 206 is
    the server saying it honoured the `Range`, so the body cannot be larger
    than the 2MB asked for; a 200 is a server that ignored it and is about to
    hand over the entire multi-gigabyte file, which is precisely what a
    function whose job is to AVOID a download must not accept. Rejecting 200
    is therefore not fastidiousness about status codes — it is the memory
    bound, expressed in the one place the protocol actually guarantees it.

    (An earlier draft asked for `stream=True` and read to a cap instead. That
    is the `requests` spelling, and `huggingface_hub` 1.x's `get_session()`
    returns an **httpx.Client**, whose `get()` has no such keyword — so every
    call raised `TypeError`, the broad `except` below turned it into `b""`,
    and the peek silently never ran. Caught by driving a real chat GGUF
    through the running server and watching it start downloading instead of
    refusing. Requiring 206 needs no streaming API at all, so it is correct on
    both clients.)

    `b""` for ANY failure — no network, a mirror that ignores `Range`, an odd
    status, a timeout. That is not defensiveness for its own sake; it is the
    contract the module docstring states: a refusal must rest on evidence, so
    "could not look" resolves to "do not refuse" and lets the load-time check
    answer instead. The timeout is short for the same reason — a slow Hub
    should delay a download by seconds, not replace it with a failure.
    """
    try:
        from huggingface_hub import hf_hub_url
        from huggingface_hub.utils import build_hf_headers, get_session

        url = hf_hub_url(repo_id, filename)
        headers = dict(build_hf_headers() or {})
        headers["Range"] = f"bytes=0-{formats._GGUF_HEADER_PEEK_BYTES - 1}"
        # No redirect keyword, deliberately: `requests` spells it
        # `allow_redirects` and `httpx` spells it `follow_redirects`, and
        # `huggingface_hub` has shipped both clients — naming either one is
        # the same TypeError-into-silence trap described above. Neither is
        # needed; hf's own session already follows the Hub's `resolve/main`
        # redirect to the CDN (checked: a bare `get` returns 206 with the
        # requested bytes).
        response = get_session().get(url, headers=headers, timeout=20)
        if response.status_code != 206:
            return b""
        return response.content[:formats._GGUF_HEADER_PEEK_BYTES]
    except Exception:  # noqa: BLE001 - see the docstring: any failure to LOOK
                       # must read as "no evidence", never as a refusal
        return b""


def _refuse_unloadable(model_id, repo_id, filename):
    """Raise if the Hub's copy of this GGUF is decidably not an embedding model.

    Silent when the header could not be read at all — see `_remote_header`.
    The three outcomes it CAN decide on are the three sentences at the top of
    this module, and they are separate because the fix differs: a chat model
    belongs on a different endpoint, a reranker belongs on no endpoint here,
    and an unreadable-but-present header belongs to the load.
    """
    header = _remote_header(repo_id, filename)
    if not header:
        return
    pooling = formats.gguf_uint_by_suffix_in(header, ".pooling_type")
    if pooling in formats.GGUF_EMBED_POOLING_TYPES:
        return
    if pooling == formats.GGUF_POOLING_RANK:
        raise RuntimeError(_IS_A_RERANKER.format(
            model_id=model_id, filename=filename, curated=_curated_ids()))
    raise RuntimeError(_NOT_AN_EMBEDDING_MODEL.format(
        model_id=model_id, filename=filename, curated=_curated_ids(),
        pooling="none declared" if pooling is None else f"pooling type {pooling}"))


def _resolve_uncurated_repo(model_id):
    """`(key, recipe)` for a bare repo id `TEXT_EMBED_RECIPES` never heard of.

    `llama_text._resolve_uncurated_repo`'s counterpart, and the same two ways
    of getting a file list, cheapest first: the local cache, then the Hub.
    Two things differ, both because this capability's models are different
    animals.

    The picker is `formats.pick_embedding_gguf_file`, which prefers `Q8_0`
    where the chat picker prefers `Q4_K_M` — see that function for why the
    preference inverts.

    And the refusals are *hard and specific* where the chat runner's are
    generic, which is deliverable 6 of this capability's whole point: a
    listing with no GGUF gets `_NOT_GGUF` naming the repo and the format it
    actually is, and a listing WITH one gets its header peeked over the
    network before a byte of weight is fetched.
    """
    local_files = _locally_cached_gguf_files(model_id)
    if local_files:
        chosen = formats.pick_embedding_gguf_file(local_files)
        if chosen:
            # No remote peek for a file already on this disk — `load()` reads
            # the real header off it moments later, which is strictly better
            # evidence than a range request and costs nothing.
            return model_id, {"repo": model_id, "file": chosen,
                              "scheme": formats.text_embed_scheme(model_id, chosen)}

    import huggingface_hub

    try:
        filenames = huggingface_hub.list_repo_files(model_id)
    except Exception as error:  # noqa: BLE001 - a Hub lookup failure is a fact
                                 # about the id/network, not a bug in this runner
        raise RuntimeError(
            _LOOKUP_FAILED.format(model_id=model_id, error=error)) from error

    chosen = formats.pick_embedding_gguf_file(filenames)
    if chosen is None:
        # The stem is what a user would search the Hub for — the repo's own
        # name without the owner, which is how GGUF conversions are almost
        # always titled (`bge-small-en-v1.5` -> `bge-small-en-v1.5-GGUF`).
        stem = model_id.split("/")[-1]
        raise RuntimeError(_NOT_GGUF.format(
            model_id=model_id, count=len(filenames), stem=stem,
            curated=_curated_ids()))

    _refuse_unloadable(model_id, model_id, chosen)
    return model_id, {"repo": model_id, "file": chosen,
                      "scheme": formats.text_embed_scheme(model_id, chosen)}


def _resolve_model_id(model_id):
    """`(key, recipe)` for whatever `model_id` actually means, or raise.

    `llama_text._resolve_model_id`'s shape exactly — a curated FILENAME key,
    a bare repo id this table curates, or a bare repo id it has never heard
    of — and see that function for why each of the three exists and why an
    ambiguous curated repo is refused by name rather than guessed at.

    The one difference is that no repo in `TEXT_EMBED_RECIPES` curates more
    than one file today, so the ambiguity branch cannot currently fire. It is
    kept rather than dropped: the table is a list this app edits, and the
    first day someone curates a second quantization of one repo is not the
    day to discover that a bare repo id silently downloads whichever one
    `dict` happened to yield first.
    """
    if model_id in formats.TEXT_EMBED_RECIPES:
        return model_id, formats.TEXT_EMBED_RECIPES[model_id]

    candidates = _recipes_for_repo(model_id)
    if not candidates:
        return _resolve_uncurated_repo(model_id)

    for key, recipe in candidates.items():
        if worker_base._cached_file(recipe["repo"], recipe["file"]):
            return key, recipe

    if len(candidates) == 1:
        (key, recipe), = candidates.items()
        return key, recipe

    raise RuntimeError(
        f"{model_id!r} curates more than one quantization here "
        f"({', '.join(repr(k) for k in sorted(candidates))}) and none of them "
        f"is on this machine yet, so which one 'load' means is ambiguous — "
        f"pick one of those ids instead of the bare repo id.")


# --------------------------------------------------------------- model loading


def download(model_id):
    """The one GGUF file this model means — never the whole repo.

    `llama_text.download`'s reasoning applies unchanged, and the resolution
    step ahead of it is where every refusal in this module fires: by the time
    `download_file` is called, the repo has been shown to publish a GGUF and
    — where the Hub would answer a range request — that GGUF has been shown
    to declare a pooling type. Nothing multi-gigabyte has moved to reach
    either conclusion.
    """
    _key, recipe = _resolve_model_id(model_id)
    filename = recipe["file"]
    return worker_base.download_file(
        recipe["repo"], filename, detail=f"Fetching {filename}…")


def load(model_id, gguf_path):
    """`gguf_path` is what `download` returned — the one `.gguf` file's path.

    The resolution check comes first, before the heavy import, for
    `llama_text.load`'s reason: a model this runner was never going to serve
    is a fact about the REQUEST, and importing first would replace a clear
    refusal with whatever llama.cpp raises.

    **Then the header is read AGAIN, off the local file.** That is not a
    duplicate of `_refuse_unloadable`: this read is the authoritative one —
    real bytes on this disk rather than a range request that may not have
    happened at all — and it is the only check that runs for a model resolved
    out of the local cache. A file that reaches here without a usable pooling
    type gets the same sentence it would have got before the download,
    because the user's mistake is identical and only the timing differs.
    """
    key, _recipe = _resolve_model_id(model_id)

    pooling = formats.gguf_pooling_type(gguf_path)
    filename = os.path.basename(gguf_path)
    if pooling == formats.GGUF_POOLING_RANK:
        raise RuntimeError(_IS_A_RERANKER.format(
            model_id=model_id, filename=filename, curated=_curated_ids()))
    if pooling not in formats.GGUF_EMBED_POOLING_TYPES:
        raise RuntimeError(_NOT_AN_EMBEDDING_MODEL.format(
            model_id=model_id, filename=filename, curated=_curated_ids(),
            pooling="none declared" if pooling is None else f"pooling type {pooling}"))

    import llama_cpp
    from llama_cpp import Llama

    n_ctx = min(formats.gguf_context_length(gguf_path) or _FALLBACK_N_CTX,
                _MAX_N_CTX)

    # A real llama.cpp API asked at call time, not inferred from which folder
    # imported this module — `llama_text.load`'s note applies verbatim. False
    # on a CPU-only build, true on Apple Silicon's Metal-linked wheel and on
    # a Vulkan build with a working driver.
    #
    # **No offload SCHEDULE here, unlike the chat runner.** `llama_text` walks
    # a shrinking sequence of `n_gpu_layers` because an 8B chat model may not
    # fit in VRAM and there is no API to ask how much there is. The largest
    # model this capability curates is 639MB; there is no card this app runs
    # on that fits a chat model and not this, so the whole probe would be a
    # loop that always succeeds on its first attempt. All layers or none.
    n_gpu_layers = -1 if llama_cpp.llama_supports_gpu_offload() else 0

    llm = Llama(
        model_path=gguf_path,
        # The flag the whole runner turns on. Without it `Llama.embed` raises
        # "must be created with embedding=True", and the context allocates a
        # KV cache for generation this runner will never do.
        embedding=True,
        n_ctx=n_ctx,
        # Both raised to `n_ctx`, and neither is optional. llama.cpp pools a
        # sequence only within one batch, and llama-cpp-python's own
        # `Llama.embed` refuses a text longer than `n_batch` outright
        # (`ValueError: Requested tokens (N) exceed batch size`) — at the
        # default 512 that would cap every model here at a bge's context
        # however long its own header says it supports.
        n_batch=n_ctx,
        n_ubatch=n_ctx,
        n_threads=os.cpu_count() or 4,
        n_gpu_layers=n_gpu_layers,
        verbose=False,
    )

    _loaded["llm"] = llm
    # The prompt convention travels with the loaded model rather than being
    # re-derived per request: it is a property of these weights, and
    # `generate` is on the hot path of a 64-item batch.
    #
    # Keyed on the RESOLVED key and the ACTUAL filename, not on `model_id` and
    # the recipe's. For a curated model the two are the same and
    # `text_embed_scheme` reads the table, which is authoritative. For an
    # uncurated repo they can differ: `_resolve_uncurated_repo` picks a
    # filename from the Hub listing or the cache, while `gguf_path` is the
    # file that actually got loaded, and the filename heuristic should run
    # over the bytes in memory rather than over the pick that led to them.
    _loaded["scheme"] = formats.text_embed_scheme(key, filename)

    # Reported through the same field every other runner uses, so a page
    # reading it needs no special case for this engine.
    worker_base.set_state(device="gpu" if n_gpu_layers else "cpu")


def memory():
    """None — RSS alone is the honest answer here, `llama_text.memory`'s reason
    verbatim: llama.cpp `mmap`s the GGUF and there is no second allocator to
    interrogate the way a torch or MLX runner has one."""
    return None


# ------------------------------------------------------------------ embedding


def generate(body):
    """One embedding call. Returns `{vectors, dim, kind, promptScheme}`.

    Not job-backed and not streaming, for `transformers_embed.generate`'s
    reason: a batch of at most `MAX_ITEMS` short items is one forward pass
    through a small encoder, over before a progress row would have drawn.

    `kind` and `promptScheme` travel BACK on the reply, and that is not
    decoration. The scheme for an uncurated model is a filename heuristic
    (`formats.TEXT_EMBED_SCHEME_HINTS`) and the kind has a default the caller
    may not have thought about — both are decisions this runner made on the
    caller's behalf that change what the vectors mean, and a caller who can
    see them can correct them. A caller who cannot would have no way to tell
    a good scheme from a wrong one, because both return unit vectors of the
    right dimension.
    """
    llm = _loaded.get("llm")
    if llm is None:
        raise RuntimeError("no model is loaded")

    texts, kind = text_embed_common.request_texts(body)
    scheme = _loaded.get("scheme") or "none"
    prompted = text_embed_common.prompted(texts, kind, scheme)

    # `Llama.embed` returns one flat vector per input BECAUSE the model
    # declares a pooling type — `load()` refuses every file that does not, so
    # the nested per-token shape that function returns for
    # `LLAMA_POOLING_TYPE_NONE` is unreachable here (read directly off
    # llama-cpp-python 0.3.29's `llama.py`, the `decode_batch` branch on
    # `pooling_type`).
    vectors = llm.embed(prompted)
    vectors = text_embed_common.unit_normalize(vectors)
    return {
        "vectors": vectors,
        "dim": len(vectors[0]) if vectors else 0,
        "kind": kind,
        "promptScheme": scheme,
    }


def main():
    """Serve, forever. The entry point both folders' `worker.py` calls."""
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
