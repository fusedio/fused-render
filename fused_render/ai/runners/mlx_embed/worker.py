"""Embeddings on MLX: one resident encoder, one or two towers (SPEC §40).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. The HTTP contract, the download reporting and the
state machine are `worker_base`'s; what lives here is only what is true of
`mlx-embeddings`' own ports in particular.

**TWO SHAPES OF CHECKPOINT, one folder.** A DUAL ENCODER (`siglip`) has a text
tower and a vision tower and answers both `texts` and `paths`. A PROSE ENCODER
(`bert`, `xlm-roberta`, `modernbert`) has one tower, is read through a different
call, and refuses `paths` by name. `mlx-embeddings` 0.1.0 ships modules for all
of those and dispatches on `model_type` itself, so ONE install serves both; the
fork here is decided once, in `load()`, off `formats.embed_model_type` over the
cached `config.json`.

**ONE FOLDER, and that is a decision rather than an omission.** PR #780 put the
text encoders in a second folder, `mlx_text_embed/`, and was right to: text
embeddings were a separate CAPABILITY there, so a second folder meant a second
resident slot for it. Unified, a second folder would buy a second venv (another
mlx-embeddings install), a second resident slot and a second copy of
`_pin_stream` — and the extra slot is not a feature. The app's contract is one
resident model per capability, and a Mac holding a SigLIP and a BERT at once is
over budget in exactly the way that contract exists to prevent.
`tests/test_ai_mlx_embed_worker.py` asserts the folder list, so it cannot creep
back.

**Only SigLIP, never CLIP, on the dual side.** `mlx-embeddings` 0.1.x ships a
`siglip` module and no `clip` one, so a CLIP checkpoint in safetensors resolves
to NOTHING at all here (`formats.MLX_EMBED_MODEL_TYPES`, and `registry`'s
comment on this row) — `onnx_embed` reads a CLIP graph export, but that is a
different file. This file never sees one and has no branch for it.

**`mlx_embeddings.load()` does the whole load**, unlike `mlx_text`'s bare
`mlx_lm.load`: it also resolves the processor (`AutoProcessor` for a model
whose config carries a `vision_config`, which SigLIP's does), so there is no
separate processor line here the way `runners/onnx_embed.py` has one — the
library already made that choice for a checkpoint shaped like this one.

`embed_common.py` (one directory up) is where the request validation and the
unit-normalization live, shared with `runners/onnx_embed.py` because the two
engines produce vectors in the SAME SPACE (`registry.py`'s comment on this row)
and must refuse and shape a request identically.
"""

import os
import sys

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading  # noqa: E402

import embed_common  # noqa: E402 - the shared request shape; see embed_common.py
import formats  # noqa: E402 - the model-type sets and the prompt table
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded model, its processor or tokenizer, the family `load()` decided and
#: the prompt scheme these weights want. One per process — see the module
#: docstring.
_loaded = {}

#: What `load()` decided this checkpoint IS, and the only fork below it.
#:
#: Named constants rather than a bare bool, because the two are not each other's
#: negation to a reader: "not dual" would have to be read as "prose", and a
#: third family joining `formats.EMBED_MODEL_TYPES` would silently land in
#: whichever branch the bool fell through to. The same two names
#: `runners/onnx_embed.py` uses, deliberately — a reader comparing the two
#: engines should not have to translate.
_DUAL = "dual"
_TEXT = "text"

#: The field on `mlx_embeddings`' `BaseModelOutput` that carries the pooled,
#: normalized sentence vector — the single seam the prose path rests on. Ported
#: from PR #780's `mlx_text_embed/worker.py`, whose own comment on it is kept:
#:
#: Named as a constant rather than written inline because it is the ONE thing an
#: upstream minor could rename out from under this file, and if it does,
#: `_prose_vectors` raises with this name in the message instead of an
#: `AttributeError` on a dataclass nobody here can see. (The manifest's `<0.2`
#: ceiling exists so that cannot arrive unannounced.)
_TEXT_EMBEDS_FIELD = "text_embeds"

#: A sanity ceiling on what a config may claim, and the floor to fall back to.
#: `max_position_embeddings` carries a `1e30`-scale sentinel on some exports,
#: and asking the tokenizer to pad to that is an allocation failure rather than
#: a slow call. 512 is the BERT-family trained length and the conservative
#: answer for anything unrecognised.
_MAX_TEXT_LENGTH = 8192
_DEFAULT_TEXT_LENGTH = 512

#: The MLX streams every thread in this process works on, keyed by device
#: name. Exactly `mlx_text/worker.py`'s `_STREAMS`/`_pin_stream` — this worker
#: is threaded the same way (`worker_base.serve`'s bring-up thread loads, a
#: `ThreadingTCPServer` request thread generates), so an unevaluated array
#: built on one and forced from the other is the same abort that module's
#: docstring documents at length. Not shared as an import: a per-process
#: module-level dict cannot cross the two separate interpreters these two
#: runners run in.
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()

#: SigLIP's own padding rule. See `runners/onnx_embed.py`'s `_TEXT_PADDING`
#: — the same convention, because it is a fact about how the checkpoint was
#: trained and not about which engine is reading it.
_TEXT_PADDING = "max_length"


def _pin_stream():
    """Put this thread's MLX work on the process's shared streams.

    Identical to `mlx_text.worker._pin_stream`, copied rather than imported:
    the two workers run in separate interpreters built from separate
    `pyproject.toml`s, so there is no module either can import from the
    other's folder. See that function's docstring for the mechanism this
    guards against.
    """
    import mlx.core as mx

    make = getattr(mx, "new_thread_unsafe_stream", None)
    pin = getattr(mx, "set_default_stream", None)
    if make is None or pin is None:
        return None
    devices = [mx.cpu, mx.default_device()]
    with _STREAMS_LOCK:
        streams = []
        for device in devices:
            key = str(device)
            if key not in _STREAMS:
                _STREAMS[key] = make(device)
            if _STREAMS[key] not in streams:
                streams.append(_STREAMS[key])
    for stream in streams:
        pin(stream)
    return streams


# --------------------------------------------------------------- model loading


def download(model_id):
    """The whole repo. `mlx-embeddings` reads a directory of safetensors plus
    the processor's own config — the same shape every other runner here
    downloads whole."""
    return worker_base.download_snapshot(model_id)


def _mlx_load(repo_id):
    """`mlx_embeddings.load`, or an error that names the ENVIRONMENT rather
    than a module. Same shape as `mlx_text.worker._mlx_load`, for the same
    reason: an ImportError out of a library this deep loses the fact that it
    is an environment problem by the time it reaches the AI Models page.

    **Takes the REPO ID, not the snapshot path** — the one place this worker
    cannot mirror `mlx_text.worker`, and `load` below explains why.
    """
    try:
        from mlx_embeddings.utils import load as mlx_embed_load
    except ImportError as e:
        raise RuntimeError(
            f"mlx-embeddings could not be imported from the runner environment "
            f"at {sys.prefix} ({e.__class__.__name__}: {e}). That is an "
            "environment failure rather than a problem with this model."
        ) from e
    return mlx_embed_load(repo_id)


def _read_json(path):
    """One small JSON file out of the snapshot, or `{}` when it is not there.

    `{}` rather than a raise: the callers below state their own defaults for
    everything they read, and a snapshot with an unreadable `config.json` is a
    broken download whose failure belongs to the library's own load rather than
    to this two-line reader.
    """
    import json

    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _family(config, model_id):
    """`_DUAL` or `_TEXT`, off the cached `config.json`'s `model_type`.

    Asked ONCE, in `load()`, and stored — `generate()` reads `_loaded["family"]`
    rather than re-deriving it, so there is exactly one place a checkpoint's
    kind is decided.

    `formats.py`'s own sets, never a second reading of the same field: the AI
    Models page decides which engines get a Load button from those constants,
    and a runner that classified the same config differently would accept a repo
    the page never offered or refuse one it did.
    """
    family = formats.embed_model_type(config)
    if family is None:
        raise RuntimeError(
            f"{model_id}'s config declares "
            f"model_type={config.get('model_type')!r}, which is not an "
            f"embedding family this runner reads "
            f"({', '.join(sorted(formats.MLX_EMBED_MODEL_TYPES))}).")
    return _DUAL if family in formats.DUAL_EMBED_MODEL_TYPES else _TEXT


def _text_length(config):
    """Where a text is cut off, from the CHECKPOINT rather than a constant.

    **PR #780 used a flat `_MAX_LENGTH = 512` and its own docstring conceded the
    cost**: correct for the BERT-family encoders that trained at 512, and LOSSY
    for anything longer — a ModernBERT trains at 8192, so a long passage would
    be silently cut at a sixteenth of what the model can read, with the vectors
    still coming back unit length and describing the first paragraph. That was a
    deliberate compromise made from a machine that could not run the code; the
    config states the real number and this reads it.

    Bounded above by `_MAX_TEXT_LENGTH` because `max_position_embeddings`
    carries a sentinel on some exports, and below by `_DEFAULT_TEXT_LENGTH`
    because a config that says nothing is a BERT until proven otherwise. A
    number SMALLER than the model's maximum is a shorter read; a LARGER one
    indexes past its position embeddings, which is why the ceiling is a clamp
    and not a warning.
    """
    for value in (config.get("max_position_embeddings"),
                  (config.get("text_config") or {}).get("max_position_embeddings")
                  if isinstance(config.get("text_config"), dict) else None):
        if isinstance(value, int) and 0 < value <= _MAX_TEXT_LENGTH:
            return value
    return _DEFAULT_TEXT_LENGTH


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory.

    **`model_id` is what the library gets, and `path` is read only for the
    CONFIG.** Every other
    runner here hands its library the snapshot directory; mlx-embeddings 0.1.x
    cannot take one for a SigLIP checkpoint, because it reads the vision tower's
    geometry out of the REPO NAME rather than out of the config beside the
    weights — `re.search(r"patch\\d+-(\\d+)", path_to_repo).group(1)` in its
    `load_model`. A snapshot directory is content-addressed
    (`…/snapshots/f775b65a…`), so that regex finds nothing and the load dies on
    `AttributeError: 'NoneType' object has no attribute 'group'` — a message
    with no hint that the fix is to pass a name instead of a path.

    Passing the id is safe rather than a second download: `download` above has
    already fetched the whole snapshot into the Hub cache, which is where the
    library's own resolution finds it. It costs one cache lookup and buys the
    only spelling of this call that works.

    **What the library hands back differs by family, which is why the second
    value is stored under two different names.** `mlx_embeddings.utils.load`
    picks `AutoProcessor` for a config carrying a `vision_config` (a SigLIP) and
    its own `load_tokenizer` otherwise — so a dual checkpoint yields a PROCESSOR
    that also knows how to turn a PIL image into `pixel_values`, and a prose one
    yields a plain tokenizer that has no idea images exist. Storing them under
    one key would leave `_image_vectors` able to reach a tokenizer and fail
    inside it.
    """
    # BEFORE the weights exist, and after the import guard — see
    # `mlx_text.worker.load`'s identical ordering and its own comment on why.
    _pin_stream()

    config = _read_json(os.path.join(path, "config.json"))
    family = _family(config, model_id)
    model, second = _mlx_load(model_id)
    _loaded.clear()
    _loaded["model"] = model
    _loaded["family"] = family
    _loaded["model_id"] = model_id
    # The prompt convention travels with the loaded model, exactly as it does in
    # `onnx_embed.load` and for the same two reasons: it is a property of these
    # weights, and `generate` is on the hot path of a 64-item batch. `"none"`
    # for a dual encoder, which has no query/passage convention — so
    # `prompted()` returns the texts unchanged and `kind` is a parameter with
    # nothing to do, which is what `ai_runtime` refuses it on.
    _loaded["scheme"] = formats.text_embed_scheme(model_id)
    if family == _DUAL:
        _loaded["processor"] = second
    else:
        _loaded["tokenizer"] = second
        _loaded["length"] = _text_length(config)


def memory():
    """What MLX itself says it is holding. Same probe as `mlx_text.worker.memory`
    and for the same reason: mmap'd, lazy arrays make RSS alone report the
    interpreter rather than the model."""
    import mlx.core as mx

    for probe in (getattr(mx, "get_active_memory", None),
                  getattr(getattr(mx, "metal", None), "get_active_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


# ------------------------------------------------------------------ embedding


def _to_lists(array):
    """An mx array's rows as plain Python floats — `embed_common`'s currency.

    `.tolist()` on an mx array already returns plain Python numbers (mlx has
    no numpy dependency to round-trip through), so this is a name for the
    conversion rather than a computation of its own — kept as a function so
    both call sites below read the same way `runners/onnx_embed.py`'s
    two do.
    """
    import mlx.core as mx

    if not isinstance(array, mx.array):
        array = mx.array(array)
    return array.astype(mx.float32).tolist()


def _text_vectors(model, processor, texts):
    """One vector per string in `texts`, unnormalized, as a plain nested list."""
    import mlx.core as mx

    inputs = processor(text=texts, padding=_TEXT_PADDING, truncation=True,
                       return_tensors="np")
    input_ids = mx.array(inputs["input_ids"])
    attention_mask = mx.array(inputs["attention_mask"]) if "attention_mask" in inputs else None
    features = model.get_text_features(input_ids=input_ids, attention_mask=attention_mask)
    return _to_lists(features)


def _image_vectors(model, processor, paths):
    """One vector per path in `paths`, unnormalized, as a plain nested list.

    Opened one at a time through `embed_common.open_image`, exactly
    `runners/onnx_embed.py`'s `_image_vectors` — see that function's docstring
    for why a bad path in the middle of a batch must be opened here rather than
    handed to the processor as a filename.
    """
    import mlx.core as mx

    images = [embed_common.open_image(path) for path in paths]
    inputs = processor(images=images, return_tensors="np")
    pixel_values = mx.array(inputs["pixel_values"])
    features = model.get_image_features(pixel_values=pixel_values)
    return _to_lists(features)


def _prose_vectors(model, tokenizer, texts, kind):
    """One vector per string in `texts` from a PROSE encoder, as a plain nested
    list of floats.

    **The whole of the encode/pool seam, in five lines, deliberately.** Upstream
    does the pooling: every `Model.__call__` in `mlx_embeddings/models/` that
    this runner can reach returns a `BaseModelOutput` whose `text_embeds` is
    already pooled by whichever strategy that architecture was trained for —
    mean for BERT and XLM-RoBERTa, config-driven for ModernBERT. So there is no
    pooling BRANCH here, and adding one would be actively wrong: it would
    second-guess the library about a choice the checkpoint's own config records.
    (`runners/onnx_embed.py` DOES pool, and has to: an ONNX graph is a graph, so
    the pooling module was never compiled into it and `1_Pooling/config.json` is
    the only record of the mode. The asymmetry between the two runners is real
    and is a property of the two formats, not an inconsistency.)

    The vectors come back L2-normalized too (`base.normalize_embeddings`, called
    inside each of those `__call__`s), and `embed_common.unit_normalize` still
    runs over them afterwards in `generate` — re-normalizing an already-unit
    vector is a no-op to within float error, and paying for that no-op is much
    cheaper than a future upstream quietly stopping.

    `batch_encode_plus` with `padding=True` is upstream's own documented call,
    copied from the "Multiple Texts Comparison" example in the wheel's
    METADATA — padding to the batch's LONGEST, which is correct here for the
    reason `onnx_embed._tokenizer` states at length: a BERT-family model takes
    an attention mask and ignores its pads.
    """
    import mlx.core as mx

    inputs = tokenizer.batch_encode_plus(
        embed_common.prompted(texts, kind, _loaded["scheme"]),
        return_tensors="mlx", padding=True, truncation=True,
        max_length=_loaded["length"])
    output = model(mx.array(inputs["input_ids"]),
                   attention_mask=mx.array(inputs["attention_mask"]))
    embeds = getattr(output, _TEXT_EMBEDS_FIELD, None)
    if embeds is None:
        # Named rather than left as an AttributeError on a dataclass the reader
        # cannot see — see `_TEXT_EMBEDS_FIELD`. Reachable only if upstream
        # renames the field inside the manifest's `<0.2` ceiling.
        raise RuntimeError(
            f"this model's output carries no {_TEXT_EMBEDS_FIELD!r} — "
            f"mlx-embeddings changed the field this runner pools through "
            f"(got {type(output).__name__})")
    return _to_lists(embeds)


def generate(body):
    """One embedding call. Returns `{vectors, dim}` — see `embed_common.py`."""
    _pin_stream()

    model = _loaded.get("model")
    family = _loaded.get("family")
    # A prose load stores a `tokenizer` and a dual load a `processor` (see
    # `load`), so the readiness check asks for whichever this family needs
    # rather than for one name both would have had to share.
    second = _loaded.get("processor" if family == _DUAL else "tokenizer")
    if model is None or family is None or second is None:
        raise RuntimeError("no model is loaded")

    source, items, kind = embed_common.request_kind(body)
    if source == "paths" and family != _DUAL:
        # **Refused by NAME, never attempted.** A text encoder has no vision
        # tower, and the tokenizer this family loaded has no idea images exist —
        # so the alternative is an `AttributeError` from inside
        # `mlx-embeddings`, which tells a page author nothing. The MODEL is
        # named because the fix is to pick a different one. The route refuses
        # the same request earlier for the same reason
        # (`ai_runtime._accepts_paths`); this is the second half of that pair,
        # for a caller reaching `/generate` directly.
        raise ValueError(
            f"{_loaded['model_id']} is a text encoder — it has no vision tower, "
            f"so 'paths' is not something it can read. Pass 'texts' instead, or "
            f"pick a dual encoder (a SigLIP checkpoint) to embed images.")
    if source == "paths":
        vectors = _image_vectors(model, second, items)
    elif family == _DUAL:
        # `kind` reaches nothing here: SigLIP has no retrieval convention, so
        # its scheme is `"none"` and there is no prefix to apply.
        vectors = _text_vectors(model, second, items)
    else:
        vectors = _prose_vectors(model, second, items, kind)
    vectors = embed_common.unit_normalize(vectors)
    dim = len(vectors[0]) if vectors else 0
    return {"vectors": vectors, "dim": dim}


def main():
    """Serve, forever. This file's own `__main__` calls it directly — no shell
    folder, unlike text and image generation, because this is the only folder
    this engine installs — unlike `onnx_embed`, which has four, one per
    execution provider."""
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)


if __name__ == "__main__":
    main()
