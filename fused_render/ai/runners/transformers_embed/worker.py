"""Embeddings on transformers: one resident dual encoder, two towers (SPEC §40).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. The HTTP contract, the download reporting and the
state machine are `worker_base`'s; what lives here is only what is true of a
SigLIP/CLIP checkpoint loaded through transformers in particular.

**One folder, unlike text and image generation.** Both of those have CUDA and
ROCm siblings that install the same code against a different torch wheel
(`diffusers_image_cuda/`, `diffusers_image_rocm/`) — an accelerated variant
earns its keep there because a token loop or a denoiser is seconds of GPU work
per request. A dual encoder is a SINGLE forward pass over a short sequence or
one image, which is milliseconds on a CPU already, so there is nothing an
accelerated wheel would meaningfully speed up and nothing here for a variant
folder to differ in. See `registry.py`'s comment on the `transformers-embed`
row for the same argument from the registry's side.

**Not the withdrawn `torch_text.py`'s shape** (D416): that module sat at
the runners ROOT because three folders share it. This runner has exactly one
folder, so its whole body lives here — the same reason `faster_whisper/worker.py`
is not split either.

`get_text_features` / `get_image_features` is the API every dual encoder this
runner loads publishes (`formats.EMBED_MODEL_TYPES`) — SigLIP and CLIP both,
under transformers' own `AutoModel`. `embed_common.py` (one directory up) is
where the request validation and the unit-normalization live, shared with
`mlx_embed/worker.py` because the two engines answer for the SAME repos and
must refuse and shape a request identically.
"""

import os
import sys

# The shared runner sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import embed_common  # noqa: E402 - the shared request shape; see embed_common.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded (model, processor) and the device they are on. One per process.
_loaded = {}

#: SigLIP's own padding rule (also correct for CLIP, which simply ignores the
#: extra pad tokens under its causal mask). SigLIP was trained with every
#: sequence padded to its tokenizer's max length rather than to the batch's
#: longest — an ordinary `padding=True` call still runs, but it changes what
#: the text tower saw relative to training and measurably shifts the vectors
#: it produces. Named here rather than left to a default so a reader does not
#: have to know that convention to trust this call.
_TEXT_PADDING = "max_length"


# --------------------------------------------------------------- model loading


def download(model_id):
    """The whole repo: transformers reads a directory of safetensors plus the
    processor's own config, so — like the text and image runners — there is no
    single file to pick out."""
    return worker_base.download_snapshot(model_id)


def _placement():
    """`device` — where this model runs.

    Same three candidates the withdrawn text runner's `_placement` picked
    from (D416), minus the dtype
    question: a dual encoder here runs in float32, at millisecond cost, so
    there is no CPU-memory argument for a narrower width the way an 8B chat
    model has one. CUDA when available, then MPS, then CPU — the same order,
    for the same reason (MPS is the Apple Silicon FALLBACK, behind `mlx-embed`
    in the registry).
    """
    import torch

    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory."""
    import torch  # noqa: F401 - imported for its side effect on the ones below
    from transformers import AutoModel, AutoProcessor

    device = _placement()
    model = AutoModel.from_pretrained(path)
    model.to(device)
    model.eval()
    # `AutoProcessor`, not `AutoTokenizer`: a dual encoder's processor also
    # knows how to turn a PIL image into `pixel_values`, which the text-only
    # tokenizer class has no idea exists. Both SigLIP and CLIP publish one.
    processor = AutoProcessor.from_pretrained(path)

    _loaded["model"] = model
    _loaded["processor"] = processor
    _loaded["device"] = device
    # Published on `/health` — the field the AI Models page reads to say
    # which device is serving.
    worker_base.set_state(device=device)


def memory():
    """What torch says it is holding, in bytes: on CUDA and MPS the weights
    live in an allocator's pool RSS cannot see."""
    import torch

    total = 0
    mps = getattr(torch, "mps", None)
    if mps is not None and hasattr(mps, "current_allocated_memory"):
        try:
            total += int(mps.current_allocated_memory())
        except (RuntimeError, OSError):
            pass
    if torch.cuda.is_available():
        try:
            total += int(torch.cuda.memory_allocated())
        except (RuntimeError, OSError):
            pass
    return total or None


# ------------------------------------------------------------------ embedding


def _text_vectors(model, processor, device, texts):
    """One vector per string in `texts`, unnormalized, as a plain nested list."""
    import torch

    inputs = processor(text=texts, padding=_TEXT_PADDING, truncation=True,
                       return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.inference_mode():
        features = model.get_text_features(**inputs)
    return features.to("cpu", dtype=torch.float32).tolist()


def _image_vectors(model, processor, device, paths):
    """One vector per path in `paths`, unnormalized, as a plain nested list.

    Opened one at a time through `embed_common.open_image` rather than handed
    to the processor as a batch of paths — a processor takes images, not
    filenames, and opening here (instead of inside a comprehension the
    processor call builds) is what lets a bad path in the middle of a batch
    name itself instead of surfacing as a PIL error with no filename attached.
    """
    import torch

    images = [embed_common.open_image(path) for path in paths]
    inputs = processor(images=images, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.inference_mode():
        features = model.get_image_features(**inputs)
    return features.to("cpu", dtype=torch.float32).tolist()


def generate(body):
    """One embedding call. Returns `{vectors, dim}` — see `embed_common.py`.

    Not job-backed and not streaming: a batch of at most
    `embed_common.MAX_ITEMS` items is one forward pass through a small tower,
    over before a progress row would ever have drawn.
    """
    model = _loaded.get("model")
    processor = _loaded.get("processor")
    device = _loaded.get("device")
    if model is None or processor is None:
        raise RuntimeError("no model is loaded")

    kind, items = embed_common.request_kind(body)
    vectors = (_text_vectors(model, processor, device, items) if kind == "texts"
              else _image_vectors(model, processor, device, items))
    vectors = embed_common.unit_normalize(vectors)
    dim = len(vectors[0]) if vectors else 0
    return {"vectors": vectors, "dim": dim}


def main():
    """Serve, forever. The entry point this file's own `__main__` calls — no
    shell folder here, unlike text and image generation, for the reason the
    module docstring gives."""
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)


if __name__ == "__main__":
    main()
