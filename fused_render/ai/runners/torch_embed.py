"""Embeddings on transformers: one resident dual encoder, two towers (SPEC §40).

**This module is the whole of the runner and it sits at the runners ROOT**,
beside `worker_base.py`, `embed_common.py` and `torch_image.py` — which is the
rule `preview.py` states about itself, applied to embeddings. THREE folders
serve this engine — `transformers_embed/`, `transformers_embed_cuda/` and
`transformers_embed_rocm/` — and they differ only in which index their
`pyproject.toml` takes torch from: PyPI's `whl/cpu` build for the first,
PyPI's default (the CUDA build on Linux, `download.pytorch.org/whl/cu130` on
Windows) for the second, `download.pytorch.org/whl/rocm7.1` for the third.
Each folder's `worker.py` is a five-line shell around `torch_embed.main()`,
the same shape `diffusers_image_cuda/worker.py` documents at length for the
image family; a second copy of `_pooled`, the padding rule or the placement
logic under any of them would fail no test, because each copy would pass its
own.

The HTTP contract, the download reporting and the state machine are
`worker_base`'s; what lives here is only what is true of a SigLIP/CLIP
checkpoint loaded through transformers in particular.

**Until this file, embeddings had exactly one torch folder** (D416's argument,
still true of the CPU/MPS row below): a dual encoder is a SINGLE forward pass
over a short sequence or one image, milliseconds already on a CPU, so there was
nothing an accelerated wheel would meaningfully speed up. The CUDA and ROCm
folders do not contradict that — they exist for a machine that already has a
working NVIDIA or fully ROCm-capable AMD card and would rather spend a request
on it than fall back to CPU fp32 by default, which is what happens on that
hardware without them. Both are opt-in from the Engines tab for exactly that
reason: the speed argument is a wash, so nobody should be moved onto either
who did not ask. See `registry.py`'s comment on the
`transformers-embed-cuda`/`-rocm` rows for the same argument from the
registry's side.

`_placement()` needs no branch for either accelerated folder: CUDA torch and
ROCm torch both report through `torch.cuda.is_available()` — ROCm because HIP
presents the CUDA API surface — so the existing `"cuda"` case already serves
both, precisely as `diffusers_image_cuda/pyproject.toml` and
`diffusers_image_rocm/pyproject.toml` document for the image family.

`get_text_features` / `get_image_features` is the API every dual encoder this
runner loads publishes (`formats.EMBED_MODEL_TYPES`) — SigLIP and CLIP both,
under transformers' own `AutoModel`. `embed_common.py` (one directory up) is
where the request validation and the unit-normalization live, shared with
`mlx_embed/worker.py` because the two engines answer for the SAME repos and
must refuse and shape a request identically.
"""

import os
import sys

# Each `worker.py` shell has already inserted `runners/` on the way in (it is
# one directory up from the shell — see mlx_text/worker.py); repeated here
# because a module may not assume something was done before it was imported,
# and this is the same self-directory insert `partial.py` falls back to.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

    Unchanged for the CUDA and ROCm folders on purpose — see the module
    docstring. Both wheels report through `torch.cuda.is_available()` and want
    the device string `"cuda"`, so the first branch already serves either one,
    and a `_placement()` that tried to tell the folders apart would be a
    difference between variants no test could see.
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


def _pooled(features):
    """The embedding vector out of what `get_*_features` returned.

    **transformers 5 returns the tower's whole output, not the vector.** Through
    the 4.x series `get_text_features`/`get_image_features` returned the pooled
    tensor itself; in 5.x both hand back a `BaseModelOutputWithPooling` carrying
    `last_hidden_state` (per-token/per-patch, `(batch, 64, dim)` for SigLIP's
    text tower) beside `pooler_output` (the vector, `(batch, dim)`). Calling
    `.to()` on that object is the `AttributeError:
    'BaseModelOutputWithPooling' object has no attribute 'to'` this function
    exists to have never allowed — and picking the field is not optional
    tidying, because `last_hidden_state` is a different rank and would not have
    raised, it would have returned nonsense.

    **`pooler_output` is right for BOTH model types in
    `formats.EMBED_MODEL_TYPES`, and for CLIP that is not a coincidence.** A
    CLIP feature is the pooled output PROJECTED into the joint space, and 5.x
    keeps `get_*_features` honest about that by overwriting the field on the way
    out (`text_outputs.pooler_output = self.text_projection(pooled_output)` in
    `modeling_clip`). So the projection is not skipped by reading the same
    attribute SigLIP's unprojected tower publishes — one field, both formats,
    checked in the installed source rather than assumed from the name.

    No fallback for a plain tensor: `pyproject.toml` pins
    `transformers>=5.15,<6`, so the 4.x shape is not a version this runner can
    be installed against, and a branch for it would be untestable code
    asserting a dependency range that does not exist.
    """
    pooled = getattr(features, "pooler_output", None)
    if pooled is None:
        raise RuntimeError(
            "the model's feature call returned "
            f"{type(features).__name__} with no `pooler_output` — this runner "
            "reads that field for both SigLIP and CLIP (see _pooled)")
    return pooled


def _text_vectors(model, processor, device, texts):
    """One vector per string in `texts`, unnormalized, as a plain nested list."""
    import torch

    inputs = processor(text=texts, padding=_TEXT_PADDING, truncation=True,
                       return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.inference_mode():
        features = model.get_text_features(**inputs)
    return _pooled(features).to("cpu", dtype=torch.float32).tolist()


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
    return _pooled(features).to("cpu", dtype=torch.float32).tolist()


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
    """Serve, forever. The entry point each variant's `worker.py` shell calls.

    A function rather than a `__main__` block because this file is imported,
    not run: the process the supervisor spawns is `<variant>/worker.py`, whose
    whole body is a path insert and a call to this — see `torch_image.main()`.
    """
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory)
