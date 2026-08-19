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

import threading

import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded (model, tokenizer). One per process — see the module docstring.
_loaded = {}

#: The MLX streams every thread in this process works on, keyed by device name —
#: ONE PER DEVICE, which is the whole point. See `_pin_stream`.
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


def _pin_stream():
    """Put this thread's MLX work on the process's shared streams — EVERY device.

    **MLX default streams are per-thread from mlx 0.32 on, and this worker is
    threaded.** `load` runs on `worker_base.serve`'s bring-up thread, which then
    exits; `generate` arrives on a `ThreadingTCPServer` request thread. An
    UNEVALUATED array is a graph pinned to the stream it was built on, and
    forcing it from another thread throws
    `std::runtime_error("There is no Stream(gpu, 1) in current thread")` out of
    `metal::get_command_encoder`, which is an UNCAUGHT C++ exception and aborts
    the process — see `mlx_whisper/worker.py::_pin_stream` for the same mechanism
    written out at length, and `mflux_image/worker.py::_pin_stream` for why both
    devices are pinned rather than the GPU alone.

    Every model switch respawns this worker as a fresh process (`supervisor.
    _start_resident`), so the FIRST message after a switch is, by construction,
    the first time any thread besides the bring-up thread touches that model's
    weights — exactly the trigger condition this pins against.

    `new_thread_unsafe_stream` is mlx's own answer: a stream not owned by the
    thread that made it. The sharing is the mechanism — one stream per device
    for the whole process, so a graph built on any thread is forceable on any
    other. "Unsafe" means it must not be driven by two threads AT ONCE, which
    this worker already guarantees: `worker_base.GENERATE_LOCK` serializes
    generations and the load completes before the server accepts a request.

    A no-op on an mlx too old to have the call, which is the right answer:
    streams were process-wide there and there was nothing to pin.
    """
    import mlx.core as mx

    make = getattr(mx, "new_thread_unsafe_stream", None)
    pin = getattr(mx, "set_default_stream", None)
    if make is None or pin is None:
        return None
    # `default_device()` rather than `mx.gpu`, and CPU FIRST: on a build with no
    # Metal the two are the same device and this dedupes to one stream, rather
    # than naming a device this mlx may not have.
    devices = [mx.cpu, mx.default_device()]
    with _STREAMS_LOCK:
        streams = []
        for device in devices:
            key = str(device)
            # `if key not in`, NOT `setdefault(key, make(device))`: the latter
            # evaluates `make` on every call and would mint a fresh stream per
            # thread while keeping the first — the shared stream is the whole
            # mechanism, so quietly making unshared ones is the bug this guards.
            if key not in _STREAMS:
                _STREAMS[key] = make(device)
            if _STREAMS[key] not in streams:
                streams.append(_STREAMS[key])
    for stream in streams:
        pin(stream)
    return streams


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
    # AFTER the import guard above (an environment where `mlx` itself cannot
    # import must fail with `_mlx_load`'s named-environment error, not a raw
    # `ModuleNotFoundError` out of here) and BEFORE the weights exist, because
    # an array remembers the stream it was made on and this thread is not the
    # one that will generate. See `_pin_stream`.
    _pin_stream()

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


def _prompt_tokens(tokenizer, text):
    """How long the prompt is, in the model's OWN tokens — or None.

    The number the API reports as `input_tokens` (SPEC AI-3). Counted here
    rather than estimated upstream because this process is the only one holding
    the tokenizer that decides the answer: characters/4 would be a plausible
    number that disagrees with the model.

    Fail-soft by design. This is a METRIC, and a tokenizer that spells `encode`
    differently, or refuses this string, must cost the count and not the
    completion — `None` means "not reported", which every reader already
    handles (a worker that predates this said nothing at all).
    """
    encode = getattr(tokenizer, "encode", None)
    if encode is None:
        return None
    try:
        return len(encode(text))
    except Exception:  # noqa: BLE001 - a count may not break a generation
        return None


def generate(body, write):
    """Stream one completion as NDJSON: {chunk} lines, then {done}."""
    _pin_stream()
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

    # Counted BEFORE the first token, so a cancelled generation reports it too:
    # the prompt was read whether or not the answer was wanted by the end.
    prompt_tokens = _prompt_tokens(tokenizer, text)

    count = 0
    started = time.time()
    for response in stream_generate(model, tokenizer, text, max_tokens=max_tokens,
                                    sampler=sampler):
        if worker_base.CANCEL.is_set():
            write({"type": "done", "ok": True, "cancelled": True, "tokens": count,
                   "input_tokens": prompt_tokens})
            return
        count += 1
        write({"type": "chunk", "text": response.text})
    write({
        "type": "done", "ok": True, "tokens": count,
        "input_tokens": prompt_tokens,
        "seconds": round(time.time() - started, 2),
    })


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=True, memory=memory)
