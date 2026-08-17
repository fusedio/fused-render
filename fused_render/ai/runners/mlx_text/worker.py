"""Text generation on MLX: one resident model, four routes (SPEC §40).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. Nothing in fused-render imports this file — it runs in
a different interpreter, and the only contract between them is HTTP, which
`worker_base` implements once for every runner:

    GET  /health    {state, model, detail, error, resident_bytes, loaded_at}
    POST /generate  {messages|prompt, max_tokens, temperature, top_p} -> NDJSON
    POST /cancel    stop the generation in flight
    POST /quit      release the weights and exit

What is left HERE is only what is true of MLX in particular: how a chat prompt
is built, and how tokens come out. Downloading, reporting, the port handshake,
the auth check and the state machine are the base's, because they are the
supervisor's contract rather than this backend's behaviour.

Deliberately mlx-lm only. No FastAPI, no requests — this process must start fast
and its dependency list is a thing users download.
"""

import os
import sys
import time

# The base sits one directory up, in `runners/`. Added to the path explicitly
# because this file is run as a SCRIPT (`python .../mlx_text/worker.py`), so
# sys.path[0] is this folder and there is no package to import through.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded (model, tokenizer). One per process — see the module docstring.
_loaded = {}


# --------------------------------------------------------------- model loading


def download(model_id):
    """The whole repo. MLX reads a directory of safetensors plus the tokenizer,
    so there is no single file to pick out — unlike the image runner, which
    swaps one quantized checkpoint into an otherwise-normal pipeline."""
    return worker_base.download_snapshot(model_id)


def _mlx_load():
    """`mlx_lm.load`, or an error that names the ENVIRONMENT rather than a module.

    An ImportError out of mlx-lm is never about the model being loaded, and by
    the time it reaches the AI Models page it has lost every trace of that: what
    the user read was `Could not import module 'AutoTokenizer'`, printed beside
    the name of a Qwen repo that was downloaded correctly, while the real
    exception — three frames down, wrapped by transformers' lazy-module
    machinery — was `ModuleNotFoundError: No module named 'filecmp'` out of the
    bundled interpreter's incomplete stdlib.

    So this says what is TRUE at this level and no more: mlx-lm could not be
    imported, here is the environment, and here is the original error. What that
    error MEANS is `worker_base.describe_failure`'s job, because it is not
    specific to MLX — it walks the chain to the root and, for a stdlib module,
    says that rebuilding the environment cannot help. Chaining with `from e` is
    what makes that possible, so it is load-bearing rather than good manners.

    Deliberately no longer claims an interrupted install. That was a guess, it
    was wrong in the case that motivated this, and a confident wrong cause is
    worse than none: it sent the user to delete an environment that was
    installed perfectly.
    """
    try:
        from mlx_lm import load as mlx_load
    except ImportError as e:
        raise RuntimeError(
            f"mlx-lm could not be imported from the runner environment at "
            f"{sys.prefix} ({e.__class__.__name__}: {e}). That is an environment "
            "failure rather than a problem with this model — mlx-lm imports "
            "transformers for the tokenizer, so the import that fails is rarely "
            "the one named first."
        ) from e
    return mlx_load


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory."""
    mlx_load = _mlx_load()

    model, tokenizer = mlx_load(path)
    _loaded["model"] = model
    _loaded["tokenizer"] = tokenizer


def memory():
    """What MLX itself says it is holding, in bytes.

    RSS alone is wrong here, and this is the backend that proves it: MLX
    memory-maps the weight files and its arrays are lazy, so immediately after a
    load the process has touched almost none of them — RSS reported 379 MB for a
    6.1 GB model. The allocator knows what it reserved whether or not the pages
    have faulted in, so it is the honest figure to offer; `worker_base` takes
    the larger of this and RSS (neither is a superset of the other).

    `get_active_memory` moved out of `mlx.core.metal` into `mlx.core` and the
    old spelling is deprecated, so both are tried — a version skew should cost
    the better number, not raise inside `/health`.
    """
    import mlx.core as mx

    for probe in (getattr(mx, "get_active_memory", None),
                  getattr(getattr(mx, "metal", None), "get_active_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


# ------------------------------------------------------------------ generation


def _messages_to_prompt(tokenizer, messages, prompt):
    """The model's own chat template, never a hand-rolled one.

    Every instruct model has its own turn markers, and getting them wrong
    produces output that looks almost right — which is worse than an error.
    `apply_chat_template` is the tokenizer's own answer; a model without one
    falls back to the raw prompt.
    """
    if prompt:
        return prompt
    template = getattr(tokenizer, "apply_chat_template", None)
    if template and getattr(tokenizer, "chat_template", None):
        return template(messages, tokenize=False, add_generation_prompt=True)
    return "\n\n".join(m.get("content", "") for m in messages if isinstance(m, dict))


def generate(body, write):
    """Stream one completion as NDJSON: {chunk} lines, then {done}."""
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler

    model = _loaded.get("model")
    tokenizer = _loaded.get("tokenizer")
    if model is None or tokenizer is None:
        write({"type": "done", "ok": False, "error": "no model is loaded"})
        return

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    text = _messages_to_prompt(tokenizer, messages, body.get("prompt") or "")
    max_tokens = int(body.get("max_tokens") or 1024)
    sampler = make_sampler(
        temp=float(body.get("temperature", 0.7)),
        top_p=float(body.get("top_p", 0.95)),
    )

    count = 0
    started = time.time()
    for response in stream_generate(model, tokenizer, text, max_tokens=max_tokens,
                                    sampler=sampler):
        if worker_base.CANCEL.is_set():
            write({"type": "done", "ok": True, "cancelled": True, "tokens": count})
            return
        count += 1
        write({"type": "chunk", "text": response.text})
    write({
        "type": "done", "ok": True, "tokens": count,
        "seconds": round(time.time() - started, 2),
    })


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=True, memory=memory)
