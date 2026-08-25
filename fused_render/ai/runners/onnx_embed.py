"""Embeddings on ONNX Runtime: one resident encoder, one or two towers (SPEC §40).

**This module is the whole of the runner and it sits at the runners ROOT**,
beside `worker_base.py`, `embed_common.py` and `torch_image.py` — the rule
`preview.py` states about itself, applied to embeddings. FOUR folders serve this
engine — `onnx_embed/`, `onnx_embed_directml/`, `onnx_embed_cuda/` and
`onnx_embed_rocm/` — and they differ only in which `onnxruntime` distribution
their `pyproject.toml` installs. Each folder's `worker.py` is a five-line shell
around `onnx_embed.main()`, the same shape `diffusers_image_cuda/worker.py`
documents at length for the image family; a second copy of the padding rule, the
output-name assertion or the provider order under any of them would fail no
test, because each copy would pass its own.

The HTTP contract, the download reporting and the state machine are
`worker_base`'s; what lives here is only what is true of an embedding export
read through an `InferenceSession` in particular.

**TWO SHAPES OF CHECKPOINT, one runner.** A DUAL ENCODER (`siglip`, `clip`) has
a text tower and a vision tower that project into one space, so it opens two
sessions and answers both `texts` and `paths`. A PROSE ENCODER (`bert`,
`xlm-roberta`, `nomic_bert`, `modernbert`) has one tower, opens one session, and
refuses `paths` by name — a text encoder handed pixel values raises somewhere
inside the graph about a tensor rank, which tells a page author nothing, and one
that happened to accept them would embed noise and return a plausible vector.
The fork is decided ONCE, in `load()`, off `formats.embed_model_type` over the
cached `config.json`; nothing below re-derives it.

The prose half is what makes this capability useful for more than captions:
SigLIP's text tower truncates at 64 tokens, so no chunk size turns it into a
paragraph encoder, while a prose encoder takes 512 to 8192 and makes RAG,
document search and clustering possible at all.

**Why this replaced a working torch runner.** A dual encoder is one forward
pass over a short sequence or one image — the compute was never the problem. The
WHEEL was: the withdrawn `transformers_embed*` folders installed torch plus
transformers, 0.2 GB on the CPU index and up to 5.9 GB on an accelerated one,
for a model whose own weights are 1.5 GB. `onnxruntime` is 14-202 MB depending
on the provider and reads the same checkpoint re-exported. Same vectors — there
is a real-weights parity gate, `tests/test_ai_onnx_embed_real_weights.py`,
asserting >=0.999 cosine on both towers, and it is what licensed the removal —
for a fraction of the environment.

**`allow_patterns` is correctness here, not an optimization.** These exports
ship every quantization side by side — `onnx-community/siglip2-base-patch16-384
-ONNX` is 33 files and 11.42 GB in total, the so400m export 29.5 GB — so the
bare `download_snapshot(model_id)` call every other runner here makes would
fetch seven redundant copies of both towers. `download()` below pins the fp32 set, and
`test_ai_onnx_embed_real_weights.py` asserts the fetched byte total against
`catalog.py`'s own figure so a widened pattern list cannot quietly reintroduce
the full pull. **fp32, not fp16 or int8**: the parity gate is what licenses this
engine replacing the torch one, and a precision change would void it.

**Reading the wrong output tensor is this file's headline risk**, and it is not
hypothetical — #813 shipped a runner that reached for the wrong field of a
transformers 5 output, returning a plausible, wrong-shaped vector rather than
raising. An `InferenceSession` makes the same mistake easy: `run(None, feed)`
returns outputs in graph order, and indexing that list reads whatever happens to
be first. So every read here goes through `_output_index`, which asserts the
output NAME and raises naming the model and what the graph actually publishes.
The fakes in `tests/test_ai_onnx_embed_worker.py` carry outputs of deliberately
different ranks for the same reason.

**No `memory()`, unlike `mlx_embed/worker.py`.** That runner supplies one
because MLX keeps memory-mapped, lazy arrays RSS reports as the interpreter
rather than as the model. onnxruntime
publishes no equivalent query, and a CPU session's weights are ordinary process
memory that `worker_base`'s default RSS probe already measures — so `serve()` is
called without `memory=` rather than with a function that can only ever answer
`None`. The accelerated folders' GPU pools are therefore under-reported, which
is a known gap and better than an invented number.

`embed_common.py` (this same directory) is where the request validation and the
unit-normalization live, shared with `mlx_embed/worker.py` because these two
engines produce vectors in the SAME SPACE and must refuse and shape a request
identically.
"""

import json
import os
import sys

# Each `worker.py` shell has already inserted `runners/` on the way in (it is
# one directory up from the shell — see mlx_text/worker.py); repeated here
# because a module may not assume something was done before it was imported,
# and this is the same self-directory insert `partial.py` falls back to.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import embed_common  # noqa: E402 - the shared request shape; see embed_common.py
import formats  # noqa: E402 - the model-type sets and the prompt table
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded sessions, the tokenizer, the preprocessing numbers and the
#: execution provider they are on. One per process.
_loaded = {}

#: SigLIP's own padding rule, and the same constant
#: `mlx_embed.worker._TEXT_PADDING` names — a fact about how the checkpoint
#: was TRAINED, not about which engine is reading it. SigLIP was trained with
#: every sequence padded to the tokenizer's max length rather than to the
#: batch's longest; padding to the batch's longest still runs and still returns
#: vectors, and they are measurably different ones. `tokenizers` pads to the
#: longest by DEFAULT, so this is the setting that must be spelled out here or
#: the parity gate fails for a reason nothing in the code would explain.
_TEXT_PADDING = "max_length"

#: The graph files this runner opens out of a dual-encoder export. Two separate
#: sessions rather than the merged `onnx/model.onnx` these repos also ship: the
#: merged graph wants BOTH `input_ids` and `pixel_values` on every call, so a
#: text-only request would have to invent a blank image (and pay for a pass
#: through the vision tower) to get a text vector out of it.
_TEXT_GRAPH = "onnx/text_model.onnx"
_VISION_GRAPH = "onnx/vision_model.onnx"

#: …and the ONE graph a prose export ships, under the name every `optimum`
#: export uses. The two layouts are told apart by the repo LISTING rather than
#: by the config, because `download()` runs before anything is on the disk —
#: see `_weight_patterns`.
_PROSE_GRAPH = "onnx/model.onnx"

#: Tensors over the 2 GB protobuf limit live in a sidecar of this name beside
#: the graph, and `onnxruntime` resolves it by relative path at session-open
#: time. So it is FETCHED, not optional: `onnx-community/siglip2-so400m-patch14
#: -384-ONNX`'s `onnx/text_model.onnx` is 0.6 MB of graph pointing at a 2.8 GB
#: `onnx/text_model.onnx_data`, and a pattern list naming only the graph
#: downloads a model that cannot open. The base export has no sidecar at all,
#: and a pattern matching nothing costs nothing.
_EXTERNAL_DATA_SUFFIX = "_data"

#: The small files beside the graphs, and every one is read. `config.json` says
#: which family and how long a sequence the text tower was built for,
#: `preprocessor_config.json` carries the image geometry and normalization
#: `_preprocess_images` applies by hand, and `tokenizer.json` IS the tokenizer —
#: `tokenizers.Tokenizer.from_file` needs nothing else, which is the whole
#: reason this runner needs no transformers.
_METADATA_PATTERNS = (
    "config.json",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    # **A prose export's pooling mode, and it is not optional.** Pooling differs
    # per model and this file is the only place it is written down:
    # `bge-base-en-v1.5` pools the CLS token while `multilingual-e5-small` and
    # `nomic-embed-text-v1.5` take the masked mean. Guessing one mode returns a
    # well-shaped vector that is measurably worse on the other family, with
    # nothing anywhere to signal it — see `_pooling_mode`.
    "1_Pooling/config.json",
)

#: What the SigLIP2 towers publish, checked against the real exports rather than
#: assumed from the names (`onnx/text_model.onnx` and `onnx/vision_model.onnx`
#: of both curated repos, read directly out of their graph protobufs). BOTH
#: towers publish `pooler_output` (the vector, `(batch, dim)`) beside
#: `last_hidden_state` (per-token/per-patch, a different RANK) — which is
#: exactly the pair #813 got wrong on the torch side, and reading the wrong one
#: here would return a wrong-shaped vector rather than raising.
_POOLED_OUTPUT = "pooler_output"

#: What a PROSE export publishes, best first — and the difference from the pair
#: above is why this is a tuple rather than one name.
#:
#: `sentence_embedding` is a fully-pooled output: a sentence-transformers export
#: that ran the pooling and normalization modules into the graph, so it IS the
#: vector and this runner must not pool it again. `last_hidden_state` is
#: per-token and needs pooling here, which is what every prose export this
#: runner has been checked against actually publishes — read directly out of
#: `nomic-embed-text-v1.5`, `multilingual-e5-small` and `bge-base-en-v1.5`'s own
#: graph protobufs, none of which emits a pooled output at all. (Only the first
#: is curated; the other two are loadable by hand, and all three were probed
#: because the CONTRACT has to hold for any export, not just the shortlist.)
#:
#: **`pooler_output` is deliberately NOT in this tuple, and that is the trap
#: worth naming.** A BERT graph may well publish one, and it is the CLS token
#: through a tanh-activated dense layer trained for next-sentence prediction —
#: not a sentence embedding, and not what any of these models' cards tell you to
#: use. It is the correct read for a SigLIP tower and the wrong read here, which
#: is precisely why the two families have separate output tuples instead of one
#: shared "read the pooled thing" rule.
_PROSE_OUTPUTS = ("sentence_embedding", "last_hidden_state")

#: The per-token output that needs pooling, named so `_prose_vectors` can tell
#: "already a vector" from "pool this" without re-deriving the list above.
_HIDDEN_OUTPUT = "last_hidden_state"

#: The mask-aware mean, and the CLS token. The two modes
#: `1_Pooling/config.json` distinguishes, and the only two any curated row uses.
_POOLING_MEAN = "mean"
_POOLING_CLS = "cls"


# --------------------------------------------------------------- model loading


def _repo_files(model_id):
    """`model_id`'s file listing, or a `RuntimeError` naming the repo.

    One cheap listing call before any byte moves, the same trade
    `ltx_video.download` makes and for the same reason: it is what lets
    `download()` build a pattern list naming exactly the files this runner
    opens, and refuse a repo that is not an export at all, before spending a
    user's bandwidth.
    """
    import huggingface_hub

    try:
        return list(huggingface_hub.list_repo_files(model_id))
    except Exception as error:  # noqa: BLE001 - a Hub lookup failure is a fact
                                # about the id/network, not a bug in this runner
        raise RuntimeError(
            f"could not read {model_id}'s file listing: {error}") from error


def _weight_patterns(names):
    """The fp32 graphs to fetch out of `names`, plus their external-data
    sidecars where the repo actually ships one.

    **The listing is what tells a dual export from a prose one**, not the
    config: `download()` runs before a byte is on the disk, so `config.json` is
    not there to read yet. A repo carrying `onnx/text_model.onnx` is a dual
    encoder and gets both towers; one carrying only `onnx/model.onnx` is a prose
    export and gets the single graph. The dual branch is asked FIRST, because a
    dual export ships `onnx/model.onnx` TOO — a merged graph this runner never
    opens, and a third full copy of both towers if it were fetched.

    Returns `()` when the listing is neither — `download()` turns that into the
    refusal, so this function stays a statement about the file layout and
    nothing else.
    """
    if _TEXT_GRAPH in names:
        graphs = (_TEXT_GRAPH, _VISION_GRAPH)
    elif _PROSE_GRAPH in names:
        graphs = (_PROSE_GRAPH,)
    else:
        return ()
    patterns = []
    for graph in graphs:
        if graph not in names:
            return ()
        patterns.append(graph)
        sidecar = graph + _EXTERNAL_DATA_SUFFIX
        if sidecar in names:
            patterns.append(sidecar)
    return tuple(patterns)


def download(model_id):
    """The fp32 file set out of the export — never the whole repo.

    See the module docstring: a bare `download_snapshot(model_id)` here is 11.42
    GB for the base export and 29.5 GB for the so400m, because these repos
    publish eight quantizations of each tower side by side. The refusal below
    fires before any of that, on the listing alone.
    """
    names = _repo_files(model_id)
    weights = _weight_patterns(names)
    if not weights:
        raise RuntimeError(
            f"{model_id} has no ONNX export this runner can open — it reads "
            f"either {_TEXT_GRAPH} plus {_VISION_GRAPH} (a dual encoder) or "
            f"{_PROSE_GRAPH} on its own (a text encoder), and neither is in the "
            f"repo. A torch or safetensors checkpoint of the same model will "
            f"not load here; look for an `-ONNX` export of it, or an `onnx/` "
            f"folder in the repo itself.")
    return worker_base.download_snapshot(
        model_id, allow_patterns=list(_METADATA_PATTERNS) + list(weights))


#: Execution providers this runner will use, best first, with the device string
#: `/health` reports for each. Every folder's `onnxruntime` distribution
#: registers exactly one accelerated provider (or none, for the CPU folder), so
#: this one table serves all four and no folder needs a branch of its own — the
#: argument the withdrawn torch embedding runner made for its own single
#: `_placement()`, in the vocabulary onnxruntime uses.
#: `CPUExecutionProvider` is always present and always last, which is what makes
#: the CPU folder's answer fall out of the same loop.
_PROVIDERS = (
    ("CUDAExecutionProvider", "cuda"),
    ("ROCMExecutionProvider", "rocm"),
    ("DmlExecutionProvider", "directml"),
    ("CPUExecutionProvider", "cpu"),
)


def _placement():
    """`(providers, device)` — which execution provider chain to open sessions
    on, and the one-word name `/health` publishes for it.

    A LIST is handed to `InferenceSession` rather than a single provider,
    because onnxruntime falls back per OPERATOR: a graph node an accelerated
    provider has no kernel for runs on the CPU instead of failing the session.
    So the chain is "the best provider available, then CPU", and `device` names
    the head of it — the same "which device is serving" claim every runner's
    `/health` makes (`worker_base.STATE["device"]`), with the same caveat that it
    describes the placement asked for rather than the kernel each operator got.
    """
    import onnxruntime

    available = set(onnxruntime.get_available_providers())
    for provider, device in _PROVIDERS:
        if provider in available:
            chain = [provider]
            if provider != "CPUExecutionProvider":
                chain.append("CPUExecutionProvider")
            return chain, device
    # An onnxruntime build with no CPU provider is not a thing that ships, but
    # answering with an empty chain would surface as a confusing session error
    # rather than an environment one.
    raise RuntimeError(
        f"the onnxruntime in the runner environment at {sys.prefix} registers "
        f"no usable execution provider (it offers {sorted(available)}). That is "
        "an environment failure rather than a problem with this model.")


def _read_json(path):
    """One small JSON file beside the graphs, or `{}` when it is not there.

    `{}` rather than a raise: `preprocessor_config.json` is genuinely optional
    for a text-only export, and the callers below already have to state their
    own defaults for what they read out of it.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _session(path, providers, model_id):
    """One `InferenceSession`, or a `RuntimeError` that names the ENVIRONMENT
    rather than a module — `mlx_embed.worker._mlx_load`'s shape, for its reason:
    an ImportError out of a library this deep loses the fact that it is an
    environment problem by the time it reaches the AI Models page."""
    try:
        import onnxruntime
    except ImportError as e:
        raise RuntimeError(
            f"onnxruntime could not be imported from the runner environment at "
            f"{sys.prefix} ({e.__class__.__name__}: {e}). That is an "
            "environment failure rather than a problem with this model.") from e
    if not os.path.isfile(path):
        raise RuntimeError(
            f"{model_id}'s snapshot has no {os.path.basename(path)} — the "
            f"download fetched a file set this runner cannot open (see "
            f"`download`'s allow_patterns).")
    return onnxruntime.InferenceSession(path, providers=providers)


def _tokenizer(path, config, tokenizer_config, model_id, family):
    """The export's own `tokenizer.json`, padded and truncated the way the
    checkpoint was trained.

    `tokenizers.Tokenizer.from_file` and nothing else: the file is the complete
    tokenizer — normalizer, vocabulary and post-processor — which is what lets
    this runner skip transformers entirely. Padding is applied HERE, once,
    rather than per call, because `tokenizers` carries it as session state on
    the object.

    **The padding rule is per FAMILY and the two are opposites**, which is why
    `family` is a parameter rather than something this function could infer:

    * a DUAL encoder pads to `length` (`_TEXT_PADDING`, "max_length"). SigLIP's
      text tower takes no `attention_mask` at all — its ONNX graph declares
      `input_ids` and nothing else — so pad tokens are simply MORE TOKENS to it,
      and it was trained with every sequence padded to the tokenizer's maximum.
      Padding to the batch's longest instead still runs, still returns vectors,
      and returns measurably different ones.
    * a PROSE encoder pads to the batch's LONGEST (`length=None`). A BERT-family
      graph takes an `attention_mask` and ignores its pads by construction, and
      `_prose_vectors` pools over that mask — so padding all 512 positions is up
      to eight times the compute for bit-identical vectors.
    """
    try:
        from tokenizers import Tokenizer
    except ImportError as e:
        raise RuntimeError(
            f"tokenizers could not be imported from the runner environment at "
            f"{sys.prefix} ({e.__class__.__name__}: {e}). That is an "
            "environment failure rather than a problem with this model.") from e
    if not os.path.isfile(path):
        raise RuntimeError(
            f"{model_id}'s snapshot has no tokenizer.json — this runner reads "
            "the fast tokenizer file directly and has no transformers "
            "fallback to build one from a vocabulary.")
    tokenizer = Tokenizer.from_file(path)
    length = _text_length(config, tokenizer_config)
    pad_token = tokenizer_config.get("pad_token")
    pad_token = pad_token if isinstance(pad_token, str) else "</s>"
    pad_id = tokenizer.token_to_id(pad_token)
    tokenizer.enable_truncation(max_length=length)
    # `length=length` for a dual encoder, which is what `_TEXT_PADDING` MEANS —
    # omit it and `tokenizers` pads to the batch's longest instead, silently
    # shifting every vector relative to what the checkpoint saw in training.
    # `None` for a prose encoder, which is the correct answer there for the
    # opposite reason. See this function's docstring.
    tokenizer.enable_padding(length=length if family == _DUAL else None,
                             pad_id=pad_id or 0, pad_token=pad_token)
    return tokenizer


#: What `load()` decided this checkpoint IS, and the only fork below it.
#:
#: Named constants rather than a bare bool, because the two are not each other's
#: negation to a reader: "not dual" would have to be read as "prose", and a
#: third family (a reranker, a late-interaction model) would silently join
#: whichever branch the bool fell through to.
_DUAL = "dual"
_TEXT = "text"


def _family(config, model_id):
    """`_DUAL` or `_TEXT`, off the cached `config.json`'s `model_type`.

    Asked ONCE, in `load()`, and stored — every function below reads
    `_loaded["family"]` rather than re-deriving it, so there is exactly one
    place a checkpoint's kind is decided.

    `formats.py`'s own sets, never a second reading of the same field: the AI
    Models page decides which engines are offered a Load button from those
    constants, and a runner that classified the same config differently would
    accept a repo the page never offered or refuse one it did.
    """
    family = formats.embed_model_type(config)
    if family is None:
        raise RuntimeError(
            f"{model_id}'s config declares "
            f"model_type={config.get('model_type')!r}, which is not an "
            f"embedding family this runner reads "
            f"({', '.join(sorted(formats.EMBED_MODEL_TYPES))}).")
    return _DUAL if family in formats.DUAL_EMBED_MODEL_TYPES else _TEXT


def _pooling_mode(pooling_config):
    """`_POOLING_CLS` or `_POOLING_MEAN`, off a prose repo's
    `1_Pooling/config.json`.

    **Read, never assumed, because it differs per model and nothing else records
    it.** `BAAI/bge-base-en-v1.5` pools the CLS token; `intfloat/multilingual-e5-small`
    and `nomic-ai/nomic-embed-text-v1.5` take the masked mean. All three are
    `model_type: bert`-shaped BERT graphs publishing the identical
    `last_hidden_state`, so there is no evidence in the config, the graph or the
    tensor shapes that tells them apart — and pooling one the other way returns
    a 768-dim unit vector that is simply worse at retrieval, with nothing
    anywhere to signal it. `1_Pooling/config.json` is what every
    sentence-transformers repo ships to state the answer, and
    `download()` fetches it for that reason.

    Mean is the fallback when the file is missing or says neither: it is the
    majority convention, and it is the one mode that uses the attention mask —
    so a wrong fallback degrades a CLS model rather than silently averaging pad
    embeddings into a vector.
    """
    if pooling_config.get("pooling_mode_cls_token") is True:
        return _POOLING_CLS
    return _POOLING_MEAN


def _declared_length(value):
    """One length claim, cleaned: a usable int, or None to try the next source.

    Three outcomes, and the middle one is the fix. A claim ABOVE the ceiling but
    otherwise plausible is CLAMPED, not discarded — `jinaai/jina-embeddings-v3`
    declares 8194, and the old `value <= _MAX_TEXT_LENGTH` test dropped it on the
    floor, fell through every remaining source and landed on
    `_DEFAULT_TEXT_LENGTH`, truncating a long-context encoder to 512. The
    ceiling was always meant as a clamp — `mlx_embed`'s copy of this says so in
    as many words — so it clamps.

    The SENTINEL still has to be discarded rather than clamped, which is why
    there are two thresholds instead of one. `model_max_length` is
    `1000000000000000019884624838656` on a checkpoint whose exporter left it in;
    clamping that to 8192 would look like an answer and be a fabrication, and on
    a 512-position graph it would run position ids off the end of the table. A
    value in the millions is not a claim about a transformer, so it is treated
    as an absent one and the next source gets a turn.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    if value >= _SENTINEL_TEXT_LENGTH:
        return None
    return min(value, _MAX_TEXT_LENGTH)


def _text_length(config, tokenizer_config):
    """How long a sequence the text tower was built for.

    Off the CONFIG, never a constant: SigLIP2's text tower is 64 positions and a
    prose encoder's is hundreds or thousands, and a hardcoded number would
    either truncate one or ask the other for positions its graph has no weights
    for.

    **The MINIMUM of what the config and the tokenizer say, not the first of
    them, and the reason is RoBERTa.** On RoBERTa and XLM-R
    `max_position_embeddings` is the usable length PLUS TWO — the offset that
    leaves room for `padding_idx` — so `intfloat/multilingual-e5-large`-shaped
    repos declare 514 while their `tokenizer_config` correctly says 512.
    Preferring the config there truncates at 514, walks position ids past the
    embedding table, and fails deep inside the graph with a raw onnxruntime
    gather error that names nothing the user can act on. Taking the min of the
    two sources gets 512 without this function having to know which
    architectures carry the offset — the tokenizer is the artefact that was
    saved for USE, so where the two disagree it is the safer of the two to
    believe.

    Erring SHORT is the whole point: a sequence shorter than the graph allows
    costs a little context on very long inputs, which is also exactly what
    `sentence-transformers` itself does when a repo ships `max_seq_length: 512`
    beside an 8192-position config. A sequence LONGER than the graph allows is
    not a degradation, it is a crash.
    """
    candidates = []
    text_config = config.get("text_config")
    if isinstance(text_config, dict):
        candidates.append(_declared_length(
            text_config.get("max_position_embeddings")))
    candidates.append(_declared_length(config.get("max_position_embeddings")))
    candidates.append(_declared_length(tokenizer_config.get("model_max_length")))
    usable = [value for value in candidates if value is not None]
    return min(usable) if usable else _DEFAULT_TEXT_LENGTH


#: A sanity ceiling on what a config may claim, and a floor to fall back to.
#: The ceiling CLAMPS rather than rejects — see `_declared_length`.
_MAX_TEXT_LENGTH = 8192
_DEFAULT_TEXT_LENGTH = 512

#: Above this a length claim is not a claim, it is an unstripped sentinel:
#: `model_max_length` is `1000000000000000019884624838656` on a checkpoint whose
#: exporter left it in, and padding EVERY sequence to that is not a slow call,
#: it is an allocation failure. Discarded rather than clamped, because clamping
#: it would dress a missing answer up as a real one. A million positions is four
#: orders of magnitude past the longest real encoder, so nothing is caught here
#: that a checkpoint meant.
_SENTINEL_TEXT_LENGTH = 1_000_000


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory.

    ONE session for a prose export and TWO for a dual encoder, decided by
    `_family` off the config and recorded on `_loaded` so no function below has
    to ask again. A prose load reads no `preprocessor_config.json` and builds no
    image settings: there is no vision tower to hand pixels to, and inventing
    the geometry anyway would leave `generate()` able to reach an image path
    that then fails inside a graph.
    """
    providers, device = _placement()
    config = _read_json(os.path.join(path, "config.json"))
    tokenizer_config = _read_json(os.path.join(path, "tokenizer_config.json"))
    family = _family(config, model_id)

    _loaded.clear()
    _loaded["family"] = family
    if family == _DUAL:
        _loaded["text"] = _session(os.path.join(path, _TEXT_GRAPH), providers,
                                   model_id)
        _loaded["vision"] = _session(os.path.join(path, _VISION_GRAPH),
                                     providers, model_id)
        _loaded["image"] = _image_settings(
            _read_json(os.path.join(path, "preprocessor_config.json")), config)
    else:
        _loaded["text"] = _session(os.path.join(path, _PROSE_GRAPH), providers,
                                   model_id)
        _loaded["pooling"] = _pooling_mode(
            _read_json(os.path.join(path, "1_Pooling", "config.json")))
    # The retrieval prompt scheme, resolved from the model id ONCE. `"none"` for
    # a dual encoder, which has no query/passage convention — so `prompted()`
    # returns the texts unchanged and `kind` is a parameter with nothing to do,
    # which is what `ai_runtime` refuses it on.
    _loaded["scheme"] = formats.text_embed_scheme(model_id)
    _loaded["tokenizer"] = _tokenizer(os.path.join(path, "tokenizer.json"),
                                      config, tokenizer_config, model_id, family)
    _loaded["model_id"] = model_id
    _loaded["device"] = device
    # Published on `/health` — the field the AI Models page reads to say which
    # device is serving.
    worker_base.set_state(device=device)


# ------------------------------------------------------------------ embedding


def _output_index(session, wanted, model_id, what):
    """Where `wanted` sits in what `session.run(None, …)` returns, or a
    `RuntimeError` naming the model, the tower and what the graph really has.

    **The one function standing between this runner and a repeat of #813.**
    `run(None, feed)` hands back a plain list in GRAPH order, so `outputs[0]` is
    whatever the exporter happened to emit first — and for both SigLIP2 towers
    that list holds `pooler_output` (the vector) beside `last_hidden_state`
    (per-token, a different rank). Indexing blind would therefore not raise; it
    would return a differently-shaped array that `unit_normalize` would happily
    normalize into nonsense, which is precisely how #813 shipped. Asked by NAME,
    and a graph that does not publish the name fails loudly here rather than
    quietly downstream.
    """
    names = [output.name for output in session.get_outputs()]
    if wanted not in names:
        raise RuntimeError(
            f"{model_id}'s {what} publishes {names} and not {wanted!r} — this "
            f"runner reads that output by name (see _output_index). A repo "
            f"exported with a different output set is not one it can serve.")
    return names.index(wanted)


def _feed(session, available):
    """`available` narrowed to exactly the inputs `session` declares.

    Every input of an ONNX graph is REQUIRED — there is no "optional argument"
    the way a torch `forward` has one — and the exports differ: SigLIP2's text
    tower takes `input_ids` alone while a BERT-family graph also wants
    `attention_mask` and `token_type_ids`. So the feed is built from the graph's
    own input list rather than from a fixed dict, and a graph asking for
    something this runner cannot supply says so by name.
    """
    feed = {}
    for spec in session.get_inputs():
        if spec.name not in available:
            raise RuntimeError(
                f"the session wants an input named {spec.name!r}, which this "
                f"runner does not know how to supply (it has "
                f"{sorted(available)}).")
        feed[spec.name] = available[spec.name]
    return feed


def _encode(texts, kind):
    """`(outputs, mask)` — `texts` prefixed for `kind`, tokenized, and run
    through the text session.

    Shared by both families because everything up to the output read IS shared:
    the prefix, the tokenizer's own padding rule (already configured per family
    in `_tokenizer`), and the graph-declared feed. `mask` comes back beside the
    outputs because the prose pooling needs it and the dual read does not, and
    recomputing it there would mean tokenizing twice.

    **The prefix is applied HERE, before tokenizing, and that is the only place
    it is observable.** Nothing downstream records that it happened — not the
    ids, not the vector, not the reply — which is why
    `tests/test_ai_onnx_embed_worker.py` asserts on what reached the tokenizer
    rather than on what came back.
    """
    import numpy

    session = _loaded["text"]
    tokenizer = _loaded["tokenizer"]

    encodings = tokenizer.encode_batch(
        embed_common.prompted(texts, kind, _loaded["scheme"]))
    mask = numpy.array([e.attention_mask for e in encodings], dtype=numpy.int64)
    available = {
        "input_ids": numpy.array([e.ids for e in encodings], dtype=numpy.int64),
        "attention_mask": mask,
        "token_type_ids": numpy.array([e.type_ids for e in encodings],
                                      dtype=numpy.int64),
    }
    return session.run(None, _feed(session, available)), mask


def _text_vectors(texts, kind):
    """One vector per string in `texts` from a DUAL encoder's text tower,
    unnormalized, as a plain nested list.

    `kind` reaches `_encode` and, for a dual encoder, changes nothing: SigLIP
    and CLIP have no retrieval convention, so their scheme is `"none"` and
    `prompted()` hands the texts back untouched. Passed through anyway rather
    than dropped here, so there is one encode path and not two.
    """
    import numpy

    session = _loaded["text"]
    outputs, _mask = _encode(texts, kind)
    at = _output_index(session, _POOLED_OUTPUT, _loaded["model_id"],
                       "text tower")
    return numpy.asarray(outputs[at], dtype=numpy.float32).tolist()


def _prose_vectors(texts, kind):
    """One vector per string in `texts` from a PROSE encoder, unnormalized, as a
    plain nested list.

    Two ways a prose export can answer, and which one applies is asked by NAME
    (`_PROSE_OUTPUTS`, best first) rather than inferred from a rank:

    * `sentence_embedding` — the export ran the pooling module into the graph,
      so this IS the vector and pooling it again would be wrong.
    * `last_hidden_state` — per-token, and what all three curated exports
      actually publish. Pooled here, in the mode `1_Pooling/config.json` names.

    **The mask is what makes the mean correct.** Texts of different lengths ride
    one batch, so the short ones are padded — and a mean over the padded axis
    divides by the batch's longest length, quietly shrinking every short text's
    vector toward whatever the model computes for a pad token. `mask` is
    broadcast over the feature axis and summed, so each row divides by its own
    token count.
    """
    import numpy

    session = _loaded["text"]
    model_id = _loaded["model_id"]
    outputs, mask = _encode(texts, kind)
    name = _prose_output_name(session, model_id)
    tensor = numpy.asarray(outputs[[o.name for o in session.get_outputs()].index(name)],
                           dtype=numpy.float32)
    if name != _HIDDEN_OUTPUT:
        return tensor.tolist()

    if _loaded.get("pooling") == _POOLING_CLS:
        # Position 0, which for every BERT-family tokenizer is the `[CLS]`
        # token the post-processor prepends. No mask needed: position 0 is
        # never a pad.
        return tensor[:, 0, :].tolist()
    weights = mask.astype(numpy.float32)[:, :, None]
    totals = (tensor * weights).sum(axis=1)
    # `maximum(..., 1.0)`, not because a real encoding can have an all-zero
    # mask, but because a divide-by-zero here would surface as `nan` vectors
    # that `unit_normalize` passes straight through — a silent answer, which is
    # the one thing this file is written to avoid.
    counts = numpy.maximum(weights.sum(axis=1), 1.0)
    return (totals / counts).tolist()


def _prose_output_name(session, model_id):
    """Which of `_PROSE_OUTPUTS` this graph publishes, or a `RuntimeError`
    naming the model and what it actually has.

    `_output_index`'s argument, for a family where there is more than one
    acceptable answer: asked by NAME and in a stated preference order, never by
    picking whatever tensor happens to have the right rank. See `_PROSE_OUTPUTS`
    for why `pooler_output` is not one of the acceptable answers here even
    though a BERT graph may publish one.
    """
    names = [output.name for output in session.get_outputs()]
    for wanted in _PROSE_OUTPUTS:
        if wanted in names:
            return wanted
    raise RuntimeError(
        f"{model_id}'s graph publishes {names} and none of "
        f"{list(_PROSE_OUTPUTS)} — this runner reads one of those by name (see "
        f"_PROSE_OUTPUTS). A repo exported with a different output set is not "
        f"one it can serve.")


def _image_settings(preprocessor, config):
    """The geometry and normalization `_preprocess_images` applies, off the
    export's own `preprocessor_config.json`.

    Read rather than assumed, because it is the half of a vision tower's
    contract that has no tensor to check it against: a wrong `image_mean` or a
    resize to the wrong side length produces a perfectly well-shaped vector that
    means something else. SigLIP2's own values (384px, mean and std 0.5) are the
    fallbacks, and `config.vision_config.image_size` is consulted before them
    because the graph's patch count is derived from it.
    """
    size = preprocessor.get("size")
    side = None
    if isinstance(size, dict):
        for key in ("height", "shortest_edge", "width"):
            if isinstance(size.get(key), int):
                side = size[key]
                break
    elif isinstance(size, int):
        side = size
    if side is None:
        vision_config = config.get("vision_config")
        if isinstance(vision_config, dict) and isinstance(
                vision_config.get("image_size"), int):
            side = vision_config["image_size"]
    mean = preprocessor.get("image_mean")
    std = preprocessor.get("image_std")
    rescale = preprocessor.get("rescale_factor")
    return {
        "side": side or 384,
        "mean": mean if isinstance(mean, list) and len(mean) == 3 else [0.5] * 3,
        "std": std if isinstance(std, list) and len(std) == 3 else [0.5] * 3,
        "rescale": rescale if isinstance(rescale, (int, float)) else 1 / 255,
    }


def _preprocess_images(paths):
    """`pixel_values` for `paths`, as `(batch, 3, side, side)` float32.

    The four steps a transformers image processor performs, by hand, because
    this runner has no transformers: open as RGB (`embed_common.open_image`),
    resize to the square the config names, scale to 0-1, then standardize. PIL's
    BICUBIC is the resample SigLIP's own processor uses; a different filter is a
    small, real difference in the vectors, which is why it is named rather than
    left to a default.

    Opened one at a time through `embed_common.open_image` rather than in a
    comprehension inside the array build, for `mlx_embed.worker._image_vectors`'
    reason: a bad path in the middle of a batch names itself instead of
    surfacing as a PIL error with no filename attached.
    """
    import numpy
    from PIL import Image

    settings = _loaded["image"]
    side = settings["side"]
    rows = []
    for path in paths:
        image = embed_common.open_image(path)
        image = image.resize((side, side), Image.BICUBIC)
        array = numpy.asarray(image, dtype=numpy.float32) * settings["rescale"]
        array = (array - numpy.asarray(settings["mean"], dtype=numpy.float32)) \
            / numpy.asarray(settings["std"], dtype=numpy.float32)
        # HWC out of PIL, CHW into the graph.
        rows.append(numpy.transpose(array, (2, 0, 1)))
    return numpy.stack(rows).astype(numpy.float32)


def _image_vectors(paths):
    """One vector per path in `paths`, unnormalized, as a plain nested list."""
    import numpy

    session = _loaded["vision"]
    model_id = _loaded["model_id"]
    available = {"pixel_values": _preprocess_images(paths)}
    outputs = session.run(None, _feed(session, available))
    at = _output_index(session, _POOLED_OUTPUT, model_id, "vision tower")
    return numpy.asarray(outputs[at], dtype=numpy.float32).tolist()


def generate(body):
    """One embedding call. Returns `{vectors, dim}` — see `embed_common.py`.

    Not job-backed and not streaming: a batch of at most
    `embed_common.MAX_ITEMS` items is one forward pass through a small tower,
    over before a progress row would ever have drawn.
    """
    if not _loaded.get("text"):
        raise RuntimeError("no model is loaded")

    source, items, kind = embed_common.request_kind(body)
    if source == "paths" and _loaded["family"] != _DUAL:
        # **Refused by NAME, never attempted.** A text encoder has no vision
        # tower: handing it pixel values raises somewhere inside the graph about
        # a tensor rank, which tells a page author nothing, and a graph that
        # happened to accept them would embed noise and hand back a
        # well-shaped, meaningless vector. The MODEL is named because the fix is
        # to pick a different one — the route refuses the same request earlier
        # for the same reason (`ai_runtime._accepts_paths`), and this is the
        # second half of that pair, for a caller reaching `/generate` directly.
        raise ValueError(
            f"{_loaded['model_id']} is a text encoder — it has no vision tower, "
            f"so 'paths' is not something it can read. Pass 'texts' instead, or "
            f"pick a dual encoder (a SigLIP or CLIP export) to embed images.")
    if source == "texts":
        vectors = (_text_vectors(items, kind) if _loaded["family"] == _DUAL
                   else _prose_vectors(items, kind))
    else:
        vectors = _image_vectors(items)
    vectors = embed_common.unit_normalize(vectors)
    dim = len(vectors[0]) if vectors else 0
    return {"vectors": vectors, "dim": dim}


def main():
    """Serve, forever. The entry point each variant's `worker.py` shell calls.

    A function rather than a `__main__` block because this file is imported, not
    run: the process the supervisor spawns is `<variant>/worker.py`, whose whole
    body is a path insert and a call to this — see `torch_image.main()`.

    No `memory=`: see the module docstring on why RSS is the honest answer for
    an onnxruntime session.
    """
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False)
