"""Typed decisions on MLX: one resident Laya agent (SPEC §40, D887).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. The HTTP contract, the download reporting and the
state machine are `worker_base`'s; what lives here is only what is true of
`laya-mlx` in particular.

**Not a text model.** Laya is a bidirectional encoder (ModernBERT-large or
mmBERT-base) with three decision heads. A request is a STATE (a string, a dict
or a list of messages) plus a dict of typed QUESTIONS — `choice` over named
options, `score` over an ordered rubric, `noul` for the probability a
proposition holds — and the reply is calibrated probabilities per question.
Zero output tokens, one encoder pass per question, ~13 ms each on an M3 Max.
So this worker is `streaming=False` and answers inside the request, the way
`mlx_embed/worker.py` does and `mlx_text/worker.py` does not.

**`laya_mlx.load()` gets the snapshot DIRECTORY, never the repo id.** Its
`resolve_model` takes a local branch when the path exists and otherwise calls
`huggingface_hub.snapshot_download` itself — which would work, and would
silently bypass this app's fetcher (`worker_base.download_snapshot`: the
mirror, the on-disk progress rows, cancel). `download` below fetches through
the app's path and `load` hands the result over, so the library's own
downloader is never reached.

**FP16, and one dtype only.** The published checkpoints are FP16; the port's
own validation matrix covers `float16` and `float32` and matches upstream's
selected answer on every question at both. `float32` is not tried
automatically on a `FloatingPointError` (non-finite logits): that is a fact
about the checkpoint the caller should hear, not a doubled-latency retry
hidden here.
"""

import os
import sys

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import warnings  # noqa: E402

import worker_base  # noqa: E402 - the path insert above is what makes it importable

log = logging.getLogger("fused_render.ai.laya")

#: The loaded agent and the id it came from. One per process.
_loaded = {}

#: The three question types Laya answers, verbatim from `laya_mlx.agent`.
#: `choice` and `score` need `criteria` (the options / the rubric levels);
#: `noul` — "No / Unknown / Yes-ish", P(true) for a proposition — takes none.
QUESTION_TYPES = ("choice", "score", "noul")
_CRITERIA_TYPES = ("choice", "score")

#: The dtype every published checkpoint ships in, and the one the port
#: validated — see the module docstring.
_DTYPE = "float16"

#: The MLX streams every thread in this process works on, keyed by device
#: name. Exactly `mlx_text/worker.py`'s `_STREAMS`/`_pin_stream` — this worker
#: is threaded the same way (`worker_base.serve`'s bring-up thread loads, and
#: its one persistent generate thread — `run_on_generate_thread` — predicts),
#: so an unevaluated array built on one and forced from the other is the same
#: abort that module's docstring documents at length. Not shared as an import: a per-process
#: module-level dict cannot cross the separate interpreters these runners
#: run in.
_STREAMS = {}
_STREAMS_LOCK = threading.Lock()


def _pin_stream():
    """Put this thread's MLX work on the process's shared streams.

    Identical to `mlx_text.worker._pin_stream`, copied rather than imported:
    the workers run in separate interpreters built from separate
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
    """The whole repo. A Laya export is `model.safetensors` beside three small
    configs and a tokenizer folder — under a gigabyte, and nothing to pick
    out of it — so it downloads whole like every other MLX runner here."""
    return worker_base.download_snapshot(model_id)


def _laya_load():
    """`laya_mlx.load`, or an error that names the ENVIRONMENT rather than a
    module. Same shape as `mlx_text.worker._mlx_load`, for the same reason:
    an ImportError out of a library this deep loses the fact that it is an
    environment problem by the time it reaches the AI Models page."""
    try:
        from laya_mlx import load as laya_load
    except ImportError as e:
        raise RuntimeError(
            f"laya-mlx could not be imported from the runner environment at "
            f"{sys.prefix} ({e.__class__.__name__}: {e}). That is an "
            "environment failure rather than a problem with this model."
        ) from e
    return laya_load


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory, and what
    the library gets (see the module docstring for why not the id).

    **The calibration-clamp warning is logged, not forwarded.** `laya_mlx`
    clamps its fitted calibration temperatures to `[0.5, 5.0]` at load and
    raises one `RuntimeWarning` per clamped bucket (the shipped `choice:11+`
    bucket is 0.1006). That is a fact about the checkpoint, the same on every
    request, and not a D633 `warnings[]` entry — those report an OPTION the
    caller passed and this tier dropped. So it goes to the worker log once,
    where whoever is comparing raw and clamped temperatures can find it.
    """
    laya_load = _laya_load()
    # AFTER the import guard (an environment where `mlx` itself cannot import
    # must fail with `_laya_load`'s named-environment error, not a raw
    # `ModuleNotFoundError` out of here) and BEFORE the weights exist — see
    # `_pin_stream`.
    _pin_stream()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        agent = laya_load(path, dtype=_DTYPE)
    for w in caught:
        log.info("%s: %s", model_id, w.message)
    _loaded.clear()
    _loaded["agent"] = agent
    _loaded["model_id"] = model_id


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


def peak_memory():
    """The HIGH-WATER mark MLX's allocator has reached over this process's
    whole life, in bytes — SPEC AI-8c, D497; `mlx_embed.worker.peak_memory`'s
    own probe, verbatim."""
    import mlx.core as mx

    for probe in (getattr(mx, "get_peak_memory", None),
                  getattr(getattr(mx, "metal", None), "get_peak_memory", None)):
        if probe is None:
            continue
        value = probe()
        if isinstance(value, int) and value > 0:
            return value
    return None


def release():
    """Hand MLX's allocator pool back to the OS after an idle stretch —
    `mlx_embed.worker.release`'s probe, verbatim, and armed by
    `worker_base.serve(release=...)` the same way. Small model, but the
    supervisor keeps one resident worker per capability, and this pool sits
    beside whatever text or image worker is also loaded.

    `getattr` because a real but older mlx wheel, or this repo's stubbed
    `mlx.core` in tests, may not have `clear_cache` at all."""
    import mlx.core as mx

    clear = getattr(mx, "clear_cache", None)
    if clear is not None:
        clear()


# ---------------------------------------------------------------- predicting


def validate_request(body):
    """The `(state, questions)` pair out of a request body, or a `ValueError`
    that names the question at fault.

    Mirrors what `laya_mlx.agent.Agent._to_internal` will accept, checked
    here first so the caller reads a sentence about THEIR request rather than
    a `KeyError` three frames inside the library. The route validates the
    same shape before the request reaches this process (D633's closed
    envelope); this is the second half of that pair, for a caller reaching
    `/generate` directly.
    """
    if not isinstance(body, dict):
        raise ValueError("the request body must be an object")
    state = body.get("state")
    if not isinstance(state, (str, dict, list)):
        raise ValueError(
            "'state' must be a string, an object, or a list of messages")
    if isinstance(state, str) and not state.strip():
        raise ValueError("'state' must not be empty")
    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise ValueError(
            "'questions' must be a non-empty object keyed by question id")
    for qid, q in questions.items():
        if not isinstance(q, dict):
            raise ValueError(f"question {qid!r} must be an object")
        qtype = q.get("type")
        if qtype not in QUESTION_TYPES:
            raise ValueError(
                f"question {qid!r}: 'type' must be one of "
                f"{', '.join(QUESTION_TYPES)}")
        instructions = q.get("instructions")
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError(
                f"question {qid!r}: 'instructions' must be a non-empty string")
        criteria = q.get("criteria")
        if qtype in _CRITERIA_TYPES:
            # Same rule the route enforces (`ai_runtime._validate_questions`):
            # a choice takes labels or label: description; a score is an
            # ORDERED rubric, so only a list says which level is worst.
            if isinstance(criteria, dict) and qtype == "choice":
                labels = list(criteria)
            elif isinstance(criteria, list):
                labels = criteria
            else:
                raise ValueError(
                    f"question {qid!r}: a {qtype!r} question needs 'criteria' — "
                    + ("a list of labels or an object of label: description"
                       if qtype == "choice" else "an ordered list of levels, worst first"))
            if len(labels) < 2:
                raise ValueError(
                    f"question {qid!r}: 'criteria' needs at least two entries")
            if not all(isinstance(label, str) and label.strip() for label in labels):
                raise ValueError(
                    f"question {qid!r}: every criteria label must be a "
                    f"non-empty string")
            if len(set(labels)) != len(labels):
                raise ValueError(
                    f"question {qid!r}: criteria labels must be unique")
        elif criteria not in (None, [], {}):
            raise ValueError(
                f"question {qid!r}: a 'noul' question takes no 'criteria'")
    return state, questions


def generate(body):
    """One prediction. Returns `{answers, usage}` — Laya's own reply minus its
    fixed `"model": "laya-rl-agent"` string, which `response.modelId` in the
    D632 frame replaces with the catalog id."""
    _pin_stream()

    agent = _loaded.get("agent")
    if agent is None:
        raise RuntimeError("no model is loaded")

    state, questions = validate_request(body)
    started = time.monotonic()
    try:
        result = agent.predict(state, questions)
    except FloatingPointError:
        # Laya's own message says "retry with dtype='float32'"; this runner
        # pins float16 and does not retry (module docstring), so the sentence
        # the caller reads names the model and the dtype rather than
        # suggesting a knob that is not theirs to turn. `from None`, because
        # `worker_base.describe_failure` walks the cause chain and would
        # print the library's retry advice right after this sentence.
        raise RuntimeError(
            f"{_loaded.get('model_id')} produced non-finite outputs in "
            f"{_DTYPE} for this request. Shorten the state or the criteria "
            f"and try again.") from None
    return {
        "answers": result.get("answers") or {},
        "usage": result.get("usage") or {"input_tokens": 0, "output_tokens": 0},
        "warnings": _truncation_warnings(agent, state, questions),
        # Model time only (tokenise + forward passes), the same `seconds` the
        # text runner reports, so the page can say how long the model took
        # apart from the request's own round trip.
        "seconds": round(time.monotonic() - started, 4),
    }


def _truncation_warnings(agent, state, questions):
    """Say so when the STATE did not fit.

    Laya's `build_sequence` fits instructions and options first and then keeps
    only as much of the state as is left — cut from the TAIL, silently, no
    error (`laya_mlx/common.py`). Only too many options raises. A page that
    sends a long conversation would otherwise lose its newest turns and read
    a confident answer about the wrong text, so this re-runs the CPU-side
    `prepare` (tokenisation only, milliseconds) and reports every question
    whose sequence hit the ceiling as a D633 warning in the frame's
    `warnings[]`, the slot that already means "we dropped part of your input".
    """
    try:
        items, _ = agent.prepare(state, questions)
    except Exception:  # noqa: BLE001 — the prediction already succeeded; never fail on the check
        return []
    max_len = int(agent.cfg.get("max_len", 512))
    cut = [qid for qid, item in zip(questions, items) if len(item["ids"]) >= max_len]
    if not cut:
        return []
    return [{
        "type": "other",
        "message": (
            f"The state was cut to fit this model's {max_len}-token window for "
            f"question(s) {', '.join(repr(q) for q in cut)}; the end of the "
            f"text was not read. Shorten the state or the options, or use a "
            f"checkpoint with a longer window."),
    }]


def main():
    """Serve, forever. This file's own `__main__` calls it directly, like
    `mlx_embed` — the only folder this engine installs."""
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=False, memory=memory, peak_memory=peak_memory,
                      release=release)


if __name__ == "__main__":
    main()
