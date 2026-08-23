"""Text embeddings on MLX: one resident text encoder, one route (SPEC §40).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. The HTTP contract, the download reporting and the
state machine are `worker_base`'s; what lives here is only what is true of
`mlx-embeddings`' text encoders in particular.

**HOW MUCH OF THIS WAS ACTUALLY RUN, stated up front because a docstring that
implies more than was tested is worse than one that admits less.** This file
was written on Windows, where `mlx` does not install: **no code path below has
been executed against a real model.** What was verified, and it is more than
nothing:

* `mlx-embeddings` 0.1.0's published wheel was downloaded from PyPI and its
  contents read directly (2026-08-23). It ships `models/bert.py`,
  `models/modernbert.py`, `models/xlm_roberta.py`, `models/qwen3.py`,
  `models/gemma3_text.py`, `models/lfm2.py` and `models/llama_bidirec.py`
  beside its `models/siglip.py`. **So this is a real implementation against a
  real API, not a scaffold around a package that only does SigLIP** — which
  was the open question this runner was written to answer.
* Every one of those modules' `Model.__call__` was read. Each returns a
  `models/base.py` `BaseModelOutput` whose `text_embeds` field is the pooled,
  L2-normalized sentence vector — mean-pooled for `bert`/`xlm_roberta`,
  config-driven for `modernbert` (defaulting to mean), and last-token-pooled
  for `qwen3`. That uniformity is the ONE seam this file depends on, and it is
  why `_text_vectors` below has no per-architecture branch.
* The call shape is upstream's own documented one, copied from the "Multiple
  Texts Comparison" example in the wheel's `METADATA`:
  `tokenizer.batch_encode_plus(texts, return_tensors="mlx", padding=True,
  truncation=True, max_length=...)`, then `model(input_ids,
  attention_mask=...)`, then `.text_embeds`.

**What someone on a Mac still has to check**, in the order it will break:
`_load` resolving a repo (mlx-embeddings' `load()` returns `(model,
tokenizer)` where the tokenizer is a `TokenizerWrapper`, so
`batch_encode_plus` is reached through `__getattr__` — read, not run);
whether `return_tensors="mlx"` yields something `mx.array` accepts unchanged
for BOTH keys; and `_MAX_LENGTH`'s interaction with a model whose own
`max_position_embeddings` is smaller. None of those is a design question — the
seams are marked below — but all three are the kind of thing that is obvious
in one minute on the right machine and unknowable from here.

-------------------------------------------------------------------------------

**This engine reads SAFETENSORS, and the llama.cpp rows read GGUF.** So
unlike the `embeddings` capability — whose two runners share one catalog
because SigLIP publishes one format both read — this capability's MLX list and
its llama.cpp list are DIFFERENT repos, exactly as `mlx-text` and
`llamacpp-text` have different lists for chat. `catalog.py` is keyed by runner
for precisely this case.

**One folder, unlike the llama.cpp pair beside it.** There is no accelerated
variant to install: MLX is the Apple Silicon GPU path already, and there is no
second wheel index to differ in.

`text_embed_common.py` (one directory up) holds the request validation, the
`kind`/prompt rules and the normalization, shared with `llama_embed.py`
because the two engines must refuse and shape a request identically even
though they read different files.
"""

import os
import sys

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading  # noqa: E402

import formats  # noqa: E402 - the shared prompt schemes; see formats.py
import text_embed_common  # noqa: E402 - the shared request shape
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded (model, tokenizer) and the prompt scheme these weights want.
#: One per process.
_loaded = {}

#: The MLX streams every thread in this process works on, keyed by device
#: name. Exactly `mlx_embed/worker.py`'s and `mlx_text/worker.py`'s
#: `_STREAMS`/`_pin_stream` — this worker is threaded the same way
#: (`worker_base.serve`'s bring-up thread loads, a `ThreadingTCPServer`
#: request thread generates), so an unevaluated array built on one stream and
#: forced from another is the same abort those modules document at length.
#: Not shared as an import: a per-process module-level dict cannot cross the
#: separate interpreters these runners run in.
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()

#: Where a text is cut off. 512 is upstream's own example value and the
#: trained length of the BERT-family encoders that make up most of this
#: engine's catalog.
#:
#: **A ceiling rather than the model's own maximum, and unlike
#: `llama_embed.load` this does NOT read the checkpoint to size itself.** That
#: runner must: llama.cpp pools only within one batch, so `n_ctx` there is a
#: hard per-item limit that has to be allocated up front. Here the tokenizer
#: truncates and the encoder runs on whatever is left, so a smaller number is
#: a shorter read rather than a failure — and a LARGER one on a
#: 512-position BERT would index past its position embeddings.
#:
#: **Correct for the encoders, LOSSY for the decoders, and that is the honest
#: description.** `formats.MLX_TEXT_ENCODER_MODEL_TYPES` — bert, modernbert,
#: xlm-roberta — trained at 512, so this is their own maximum and nothing is
#: lost. `formats.MLX_TEXT_EMBED_DECODER_MODEL_TYPES` — qwen3, gemma3_text,
#: lfm2 — support far more, so a long passage is TRUNCATED here where the
#: llama.cpp runner would read it whole (that one sizes `n_ctx` off the
#: checkpoint, `llama_embed._MAX_N_CTX`). Sizing this from the config the
#: same way is the obvious improvement and is deliberately not attempted from
#: a machine that cannot run the code; 512 is the value that is correct or
#: conservative for every architecture rather than wrong for some.
_MAX_LENGTH = 512

#: The field on `mlx_embeddings`' `BaseModelOutput` that carries the pooled,
#: normalized sentence vector — the single seam this whole runner rests on.
#:
#: Named as a constant rather than written inline because it is the ONE thing
#: an upstream minor could rename out from under this file, and if it does,
#: `_text_vectors` raises with this name in the message instead of an
#: `AttributeError` on a dataclass nobody here can see. (The manifest's
#: `<0.2` ceiling exists so that cannot arrive unannounced.)
_TEXT_EMBEDS_FIELD = "text_embeds"


def _pin_stream():
    """Put this thread's MLX work on the process's shared streams.

    Identical to `mlx_embed.worker._pin_stream` and
    `mlx_text.worker._pin_stream`, copied rather than imported: these workers
    run in separate interpreters built from separate `pyproject.toml`s, so
    there is no module either can import from the other's folder. See
    `mlx_text.worker._pin_stream` for the mechanism this guards against.
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
    the tokenizer's own files — the same shape every other safetensors runner
    here downloads whole."""
    return worker_base.download_snapshot(model_id)


def _mlx_load(repo_id):
    """`mlx_embeddings.load`, or an error that names the ENVIRONMENT rather
    than a module. Same shape as `mlx_embed.worker._mlx_load` and
    `mlx_text.worker._mlx_load`, for the same reason: an ImportError out of a
    library this deep loses the fact that it is an environment problem by the
    time it reaches the AI Models page.

    **Takes the REPO ID, not the snapshot path**, matching `mlx_embed`'s
    choice — see `load` below.
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

    **`model_id` is what the library gets, and `path` goes unused**, the same
    call `mlx_embed/worker.py` makes and for a related reason. That folder had
    a forcing one (mlx-embeddings reads a SigLIP's patch geometry out of the
    repo NAME, so a content-addressed snapshot path breaks it); here the
    library would accept either, because `get_model_path` returns a `Path`
    unchanged when it exists on disk. Passing the id anyway keeps the two MLX
    embedding runners' loads spelled identically, so a reader comparing them
    is not left wondering which difference is meaningful.

    Not a second download: `download` above has already fetched the snapshot
    into the Hub cache, which is where the library's own resolution finds it.

    **SEAM FOR SOMEONE ON A MAC.** This is the first line that will fail if
    anything in this file is wrong — `mlx_embeddings.load()` resolving a
    text-encoder repo and returning `(Model, TokenizerWrapper)`. Read out of
    0.1.0's `utils.load`, which picks `load_tokenizer` for a config with no
    `vision_config` (which a text encoder has none of) and `AutoProcessor`
    otherwise. Never executed.
    """
    # BEFORE the weights exist, and after the import guard — see
    # `mlx_text.worker.load`'s identical ordering and its own comment on why.
    _pin_stream()

    model, tokenizer = _mlx_load(model_id)
    _loaded["model"] = model
    _loaded["tokenizer"] = tokenizer
    # The prompt convention travels with the loaded model, exactly as it does
    # in `llama_embed.load` and for the same two reasons: it is a property of
    # these weights, and `generate` is on the hot path of a 64-item batch.
    # Keyed off the REPO ID here rather than a filename, since this engine's
    # catalog is repo-shaped — `formats.text_embed_scheme` takes both and
    # falls back to `"none"` for anything it does not recognise.
    _loaded["scheme"] = formats.text_embed_scheme(model_id)

    # MLX only ever runs on Apple Silicon's GPU here, so this is a constant
    # rather than a probe — but it is still published through the same field
    # every other runner reports through, because a page reading it must not
    # need a special case for this engine.
    worker_base.set_state(device="gpu")


def memory():
    """What MLX itself says it is holding. Same probe as
    `mlx_embed.worker.memory` and for the same reason: mmap'd, lazy arrays
    make RSS alone report the interpreter rather than the model."""
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


def _text_vectors(model, tokenizer, texts):
    """One vector per string in `texts`, as a plain nested list of floats.

    **The whole of the load/encode/pool seam, in five lines, deliberately.**
    Upstream does the pooling: every `Model.__call__` in
    `mlx_embeddings/models/` that this runner can reach returns
    `BaseModelOutput.text_embeds`, already pooled by whichever strategy that
    architecture was trained for — mean for BERT and XLM-RoBERTa,
    config-driven for ModernBERT, last-token for Qwen3. So there is no
    pooling BRANCH here, and adding one would be actively wrong: it would
    second-guess the library about a choice the checkpoint's own config
    records.

    The vectors come back L2-normalized too (`base.normalize_embeddings`,
    called inside each of those `__call__`s), and
    `text_embed_common.unit_normalize` still runs over them afterwards — see
    that function's docstring for why re-normalizing an already-unit vector is
    the right trade rather than a wasted pass.

    **SEAM FOR SOMEONE ON A MAC**, and the second thing that will break if
    anything does: whether `return_tensors="mlx"` hands back values `mx.array`
    takes unchanged for both `input_ids` and `attention_mask`. Upstream's own
    README example passes the two straight through positionally, which is what
    is copied here; if a version yields numpy instead, `mx.array` still
    accepts it and this is unaffected.
    """
    import mlx.core as mx

    inputs = tokenizer.batch_encode_plus(
        texts, return_tensors="mlx", padding=True, truncation=True,
        max_length=_MAX_LENGTH)
    output = model(mx.array(inputs["input_ids"]),
                   attention_mask=mx.array(inputs["attention_mask"]))
    embeds = getattr(output, _TEXT_EMBEDS_FIELD, None)
    if embeds is None:
        # Named rather than left as an AttributeError on a dataclass the
        # reader cannot see — see `_TEXT_EMBEDS_FIELD`. Reachable only if
        # upstream renames the field inside the manifest's `<0.2` ceiling.
        raise RuntimeError(
            f"this model's output carries no {_TEXT_EMBEDS_FIELD!r} — "
            f"mlx-embeddings changed the field this runner pools through "
            f"(got {type(output).__name__} with {dir(output)!r})")
    return mx.array(embeds).astype(mx.float32).tolist()


def generate(body):
    """One embedding call. Returns `{vectors, dim, kind, promptScheme}` — the
    identical reply shape `llama_embed.generate` returns, including the two
    reporting fields and for the reasons its docstring gives."""
    _pin_stream()

    model = _loaded.get("model")
    tokenizer = _loaded.get("tokenizer")
    if model is None or tokenizer is None:
        raise RuntimeError("no model is loaded")

    texts, kind = text_embed_common.request_texts(body)
    scheme = _loaded.get("scheme") or "none"
    prompted = text_embed_common.prompted(texts, kind, scheme)

    vectors = text_embed_common.unit_normalize(
        _text_vectors(model, tokenizer, prompted))
    return {
        "vectors": vectors,
        "dim": len(vectors[0]) if vectors else 0,
        "kind": kind,
        "promptScheme": scheme,
    }


def main():
    """Serve, forever. This file's own `__main__` calls it directly — no shell
    folder, unlike the llama.cpp pair, because this is the only folder this
    engine installs (see the module docstring)."""
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)


if __name__ == "__main__":
    main()
