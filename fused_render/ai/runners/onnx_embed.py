"""Embeddings on ONNX Runtime: one resident dual encoder, two towers (SPEC §40).

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
`worker_base`'s; what lives here is only what is true of a SigLIP/CLIP export
read through an `InferenceSession` in particular.

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
)

#: What the SigLIP2 towers publish, checked against the real exports rather than
#: assumed from the names (`onnx/text_model.onnx` and `onnx/vision_model.onnx`
#: of both curated repos, read directly out of their graph protobufs). BOTH
#: towers publish `pooler_output` (the vector, `(batch, dim)`) beside
#: `last_hidden_state` (per-token/per-patch, a different RANK) — which is
#: exactly the pair #813 got wrong on the torch side, and reading the wrong one
#: here would return a wrong-shaped vector rather than raising.
_POOLED_OUTPUT = "pooler_output"


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

    Returns `()` when this listing is not a dual-encoder export — `download()`
    turns that into the refusal, so this function stays a statement about the
    file layout and nothing else.
    """
    if _TEXT_GRAPH not in names:
        return ()
    patterns = []
    for graph in (_TEXT_GRAPH, _VISION_GRAPH):
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
            f"{model_id} is not an ONNX dual-encoder export — this runner opens "
            f"{_TEXT_GRAPH} and {_VISION_GRAPH} out of the repo, and neither "
            f"pair is there. An `onnx-community/*-ONNX` export of a SigLIP or "
            f"CLIP checkpoint is the layout it reads; a torch checkpoint of the "
            f"same model will not load here.")
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


def _tokenizer(path, config, tokenizer_config, model_id):
    """The export's own `tokenizer.json`, padded and truncated the way the
    checkpoint was trained.

    `tokenizers.Tokenizer.from_file` and nothing else: the file is the complete
    tokenizer — normalizer, vocabulary and post-processor — which is what lets
    this runner skip transformers entirely. The `padding="max_length"` rule
    (`_TEXT_PADDING`) is applied HERE, once, rather than per call, because
    `tokenizers` carries padding as session state on the object.
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
    # `length=length`, which is what `_TEXT_PADDING` MEANS — omit it and
    # `tokenizers` pads to the batch's longest instead, silently shifting every
    # vector relative to what the checkpoint saw in training.
    tokenizer.enable_padding(length=length, pad_id=pad_id or 0,
                             pad_token=pad_token)
    return tokenizer


def _text_length(config, tokenizer_config):
    """How long a sequence the text tower was built for.

    Off the CONFIG, never a constant: SigLIP2's text tower is 64 positions and a
    prose encoder's is hundreds or thousands, and a hardcoded number would
    either truncate one or ask the other for positions its graph has no weights
    for. `max_position_embeddings` under `text_config` is where a dual encoder
    declares it; `model_max_length` is the tokenizer's own copy, and is read
    only as a fallback because it carries a `1e30` sentinel on some repos.
    """
    text_config = config.get("text_config")
    if isinstance(text_config, dict):
        value = text_config.get("max_position_embeddings")
        if isinstance(value, int) and 0 < value <= _MAX_TEXT_LENGTH:
            return value
    value = config.get("max_position_embeddings")
    if isinstance(value, int) and 0 < value <= _MAX_TEXT_LENGTH:
        return value
    value = tokenizer_config.get("model_max_length")
    if isinstance(value, int) and 0 < value <= _MAX_TEXT_LENGTH:
        return value
    return _DEFAULT_TEXT_LENGTH


#: A sanity ceiling on what a config may claim, and a floor to fall back to.
#: `model_max_length` is `1000000000000000019884624838656` on a checkpoint whose
#: exporter left the sentinel in, and padding EVERY sequence to that is not a
#: slow call, it is an allocation failure.
_MAX_TEXT_LENGTH = 8192
_DEFAULT_TEXT_LENGTH = 512


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory."""
    providers, device = _placement()
    config = _read_json(os.path.join(path, "config.json"))
    tokenizer_config = _read_json(os.path.join(path, "tokenizer_config.json"))

    _loaded.clear()
    _loaded["text"] = _session(os.path.join(path, _TEXT_GRAPH), providers, model_id)
    _loaded["vision"] = _session(os.path.join(path, _VISION_GRAPH), providers,
                                 model_id)
    _loaded["tokenizer"] = _tokenizer(os.path.join(path, "tokenizer.json"),
                                      config, tokenizer_config, model_id)
    _loaded["image"] = _image_settings(
        _read_json(os.path.join(path, "preprocessor_config.json")), config)
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


def _text_vectors(texts):
    """One vector per string in `texts`, unnormalized, as a plain nested list."""
    import numpy

    session = _loaded["text"]
    tokenizer = _loaded["tokenizer"]
    model_id = _loaded["model_id"]

    encodings = tokenizer.encode_batch(texts)
    available = {
        "input_ids": numpy.array([e.ids for e in encodings], dtype=numpy.int64),
        "attention_mask": numpy.array([e.attention_mask for e in encodings],
                                      dtype=numpy.int64),
        "token_type_ids": numpy.array([e.type_ids for e in encodings],
                                      dtype=numpy.int64),
    }
    outputs = session.run(None, _feed(session, available))
    at = _output_index(session, _POOLED_OUTPUT, model_id, "text tower")
    return numpy.asarray(outputs[at], dtype=numpy.float32).tolist()


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

    # `retrieval` is unpacked and deliberately unused HERE: this runner serves
    # dual encoders only until the prose path lands, and SigLIP has no prompt
    # scheme to apply. Unpacked rather than dropped so the day it starts
    # mattering is a one-line change and not a search for the call site.
    source, items, _retrieval = embed_common.request_kind(body)
    vectors = _text_vectors(items) if source == "texts" else _image_vectors(items)
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
