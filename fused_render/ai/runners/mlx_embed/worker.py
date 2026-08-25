"""Embeddings on MLX: one resident dual encoder, two towers (SPEC §40).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. The HTTP contract, the download reporting and the
state machine are `worker_base`'s; what lives here is only what is true of
`mlx-embeddings`' SigLIP port in particular.

**Only SigLIP, never CLIP.** `mlx-embeddings` 0.1.x ships a `siglip` module and
no `clip` one, so a CLIP checkpoint in safetensors resolves to NOTHING at all
here (`formats.MLX_EMBED_MODEL_TYPES`, and `registry`'s comment on this row) —
`onnx_embed` reads a CLIP graph export, but that is a different file. This file
never sees one and has no branch for it.

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
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded (model, processor). One per process — see the module docstring.
_loaded = {}

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


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory.

    **`model_id` is what the library gets, and `path` goes unused.** Every other
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
    """
    # BEFORE the weights exist, and after the import guard — see
    # `mlx_text.worker.load`'s identical ordering and its own comment on why.
    _pin_stream()

    model, processor = _mlx_load(model_id)
    _loaded["model"] = model
    _loaded["processor"] = processor


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


def generate(body):
    """One embedding call. Returns `{vectors, dim}` — see `embed_common.py`."""
    _pin_stream()

    model = _loaded.get("model")
    processor = _loaded.get("processor")
    if model is None or processor is None:
        raise RuntimeError("no model is loaded")

    # `retrieval` is unpacked and deliberately unused HERE: this runner serves
    # SigLIP only until the prose path lands, and SigLIP has no prompt scheme to
    # apply. Unpacked rather than dropped so the day it starts mattering is a
    # one-line change and not a search for the call site.
    source, items, _retrieval = embed_common.request_kind(body)
    vectors = (_text_vectors(model, processor, items) if source == "texts"
              else _image_vectors(model, processor, items))
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
