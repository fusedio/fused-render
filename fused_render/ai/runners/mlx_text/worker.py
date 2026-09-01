"""Text generation on MLX: one resident model, four routes (SPEC §40).

Started by `fused_render.ai.supervisor` on the interpreter built from this
folder's `pyproject.toml`. Nothing in fused-render imports this file — it runs in
a different interpreter, and the only contract between them is HTTP, which
`worker_base` implements once for every runner:

    GET  /health    {state, model, detail, error, resident_bytes, loaded_at}
    POST /generate  {messages|prompt, images, max_tokens, temperature, top_p}
                    -> NDJSON
    POST /cancel    stop the generation in flight
    POST /quit      release the weights and exit

What is left HERE is only what is true of MLX in particular: how a chat prompt
is built, and how tokens come out. Downloading, reporting, the port handshake,
the auth check and the state machine are the base's, because they are the
supervisor's contract rather than this backend's behaviour.

Deliberately mlx-vlm only. No FastAPI, no requests — this process must start
fast and its dependency list is a thing users download.
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
        seen = set()
        for device in devices:
            key = str(device)
            # `if key not in`, NOT `setdefault(key, make(device))`: the latter
            # evaluates `make` on every call and would mint a fresh stream per
            # thread while keeping the first — the shared stream is the whole
            # mechanism, so quietly making unshared ones is the bug this guards.
            if key not in _STREAMS:
                _STREAMS[key] = make(device)
            stream = _STREAMS[key]
            # `id(stream)`, never `stream not in streams` (which is what this
            # said until the crash this comment now documents): `in` calls
            # `Stream.__eq__`, and a `new_thread_unsafe_stream` is — per its own
            # name — not safe to touch from a thread other than the one that
            # made it, EQUALITY INCLUDED. A worker with more than one request
            # thread calls this from a NEW thread every time (a fresh
            # `ThreadingTCPServer` thread per request), so the second request
            # onward compared a stream this thread never made and segfaulted
            # inside MLX's C++ equality — reproduced directly by calling this
            # from two different threads in a row, no HTTP layer involved.
            # `id()` is a plain Python pointer comparison, never reaching into
            # MLX at all, so it carries none of that risk.
            if id(stream) not in seen:
                seen.add(id(stream))
                streams.append(stream)
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
    """`mlx_vlm.load`, or an error that names the ENVIRONMENT rather than a module.

    An ImportError out of mlx-vlm is never about the model being loaded, and by
    the time it reaches the AI Models page it has lost every trace of that: what
    the user read was `Could not import module 'AutoTokenizer'`, printed beside
    the name of a Qwen repo that was downloaded correctly, while the real
    exception — three frames down, wrapped by transformers' lazy-module
    machinery — was `ModuleNotFoundError: No module named 'filecmp'` out of the
    bundled interpreter's incomplete stdlib. That transformers-lazy-module point
    survives the mlx-lm -> mlx-vlm switch unchanged: mlx-vlm resolves a
    checkpoint's processor through the same lazy-import machinery, so the same
    misdirection is possible from the same place.

    So this says what is TRUE at this level and no more: mlx-vlm could not be
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
        from mlx_vlm import load as mlx_load
    except ImportError as e:
        raise RuntimeError(
            f"mlx-vlm could not be imported from the runner environment at "
            f"{sys.prefix} ({e.__class__.__name__}: {e}). That is an environment "
            "failure rather than a problem with this model — mlx-vlm imports "
            "transformers for the tokenizer, so the import that fails is rarely "
            "the one named first."
        ) from e
    return mlx_load


def _unsupported_architecture(config):
    """`(model_type, exc)` if mlx-vlm cannot open `config`'s architecture,
    else `None` — the exception is carried out so the caller can chain `from`
    it and keep the original message in the log.

    Asked of mlx-vlm's OWN resolution (`get_model_and_args`) — never a table
    of architectures maintained here, which would go stale the moment the
    installed package's model zoo grew past it. `get_model_and_args` is
    exactly what `mlx_load` below calls internally to pick a model class; the
    only thing asking it again here buys is catching its failure BEFORE it is
    wrapped in mlx-vlm's own words, so `load` can say something clearer than
    what those words say on their own — see `load`'s own comment for why that
    matters.
    """
    model_type = config.get("model_type") if isinstance(config, dict) else None
    if not model_type:
        return None
    try:
        from mlx_vlm.utils import get_model_and_args

        get_model_and_args(config)
    except ValueError as e:
        # mlx-vlm's own "no such architecture" shape — see `load`'s docstring.
        return model_type, e
    except Exception:  # noqa: BLE001 - anything else is not THIS question's to answer
        return None
    return None


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory."""
    mlx_load = _mlx_load()
    # AFTER the import guard above (an environment where `mlx` itself cannot
    # import must fail with `_mlx_load`'s named-environment error, not a raw
    # `ModuleNotFoundError` out of here) and BEFORE the weights exist, because
    # an array remembers the stream it was made on and this thread is not the
    # one that will generate. See `_pin_stream`.
    _pin_stream()

    # Read once, here, and reused below rather than re-read per request (see
    # the comment where it lands in `_loaded`) — and read BEFORE the load
    # call for a second reason: it is what lets `_unsupported_architecture`
    # ask mlx-vlm's own resolver whether this checkpoint's `model_type` opens
    # at all, before `mlx_load` gets a chance to fail on it in mlx-vlm's own
    # words instead of ours.
    from mlx_vlm.utils import load_config

    config = load_config(path)

    # **AI-11j widened `visual-question-answering` to a real TEXT_GENERATION
    # capability, but the vocabulary tag says nothing about whether mlx-vlm's
    # model ZOO actually opens the architecture behind it** — the canonical
    # repo for that tag, `dandelin/vilt-b32-finetuned-vqa`, ships no
    # `mlx_vlm.models.vilt`, and calling `mlx_load` on it raises
    # `ValueError: Model type vilt not supported. Error: …` straight out of
    # mlx-vlm's `get_model_and_args`. That message is not wrong, but it reads
    # as this app's own confusion by the time `describe_failure` reports it —
    # a bare `ValueError` with no cause chain to walk, phrased as an internal
    # import failure inside a third-party utility rather than a fact about
    # the checkpoint. So this is caught here and re-raised in OUR words,
    # naming the architecture plainly, the same discipline `_mlx_load`
    # follows for an unimportable environment: say what is actually true (an
    # architecture this runner cannot open) rather than let a technically
    # correct but confusingly phrased library message reach the page
    # unexplained. `from e` keeps the original message in the log for
    # whoever reads it.
    unsupported = _unsupported_architecture(config)
    if unsupported is not None:
        model_type, cause = unsupported
        raise RuntimeError(
            f"{model_type!r} is not an architecture mlx-vlm can open — "
            f"{model_id} cannot be loaded by this runner, which is a fact "
            "about this checkpoint rather than about the environment."
        ) from cause

    # `lazy=True` is the measurement this switch is built on: eager loading
    # materialises the vision tower too — +0.67GB on Qwen3.5-4B-OptiQ-4bit,
    # resident at 3.937GB against mlx-lm's 3.270GB — even though a text-only
    # chat never touches it. `lazy=True` defers EVERY array, vision and
    # language both, until something actually reads it.
    model, processor = mlx_load(path, lazy=True)

    # …but "defer everything" is one array too many. Left fully lazy, the
    # LANGUAGE tower's own weights are not forced into memory until the first
    # generation, which breaks two things `mlx-lm` never did: `memory()`
    # below reads `mx.get_active_memory()`, so `/health`'s `resident_bytes`
    # would report a freshly loaded, multi-gigabyte model as ~0 bytes right up
    # until the first reply — and a corrupt checkpoint or an OOM, which used
    # to fail the LOAD, now fails the first GENERATION instead, because
    # `worker_base` marks this model "ready" the moment `load()` returns
    # without having touched a single weight.
    #
    # So the language tower is evaluated NOW — exactly what mlx-lm always did
    # for the one tower it ever loaded, and exactly what mlx-vlm's own
    # `lazy=False` does for BOTH towers (`mlx_vlm.utils.load_model`'s
    # `if not lazy: mx.eval(model.parameters())`) — while the vision tower
    # stays lazy, deferred to the image path's first use (commit 3).
    #
    # Verified by hand: evaluating only `model.language_model` on
    # `mlx-community/Qwen3.5-4B-OptiQ-4bit` (this runner's own catalog entry)
    # lands at 3269.96 MB resident — matching mlx-lm's own 3.270GB almost
    # exactly — against 3936.99 MB for evaluating both towers eagerly. The
    # ~0.67GB gap between the two IS the vision tower, and it stays deferred.
    import mlx.core as mx

    language_model = getattr(model, "language_model", None)
    if language_model is not None:
        mx.eval(language_model.parameters())
    else:
        # Defensive, not the expected path: every architecture mlx-vlm ships
        # builds its `Model` around a `self.language_model` attribute —
        # verified across qwen3, qwen3_5, gemma3, gemma4, llama and others,
        # the whole reason `Model.get_input_embeddings` can call
        # `self.language_model.model.embed_tokens` unconditionally in every
        # one of them. A future architecture that broke that convention must
        # not silently keep the honest-reporting bug this fix exists for; it
        # costs the full eager load instead, same as mlx-lm always did.
        mx.eval(model.parameters())

    _loaded["model"] = model
    _loaded["processor"] = processor
    # Stashed so `generate`'s image-path refusal can NAME the model rather
    # than say "this checkpoint" about nothing in particular — the same
    # reason `_unreadable_image` names the path instead of just failing.
    _loaded["model_id"] = model_id
    # The SAME `config` read above (and already spent checking architecture
    # support with it) — stashed here rather than re-read per request:
    # `generate`'s image path needs it for `mlx_vlm.prompt_utils.
    # apply_chat_template` (`num_images` is not enough on its own — the
    # helper also reads the model's own `model_type` off this dict to pick
    # its message format), and a request is not the place to be doing
    # filesystem reads this load already paid for.
    _loaded["config"] = config
    # Any KV prompt cache from a PREVIOUS `load()` in this same process belongs
    # to different weights and a different token vocabulary — reusing it here
    # would feed this model hidden states it never produced. `load()` is only
    # ever called once per production process (a model switch respawns this
    # worker fresh — see `_pin_stream`'s docstring), so this is a no-op there;
    # it exists for the case a test, or a future reload path, calls `load()`
    # twice in one process. See `generate`'s own comment, just above the
    # `stream_generate` call, for what this slot holds and why.
    _loaded.pop("prompt_cache_state", None)


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


def peak_memory():
    """The HIGH-WATER mark MLX's allocator has reached over this process's
    whole life, in bytes — SPEC AI-8c, D497, `ltx_video.worker.peak_memory`'s
    own probe, verbatim. `memory()` above answers "right now"; `fit` (AI-16)
    needs "at its worst" instead, and `mx.get_peak_memory()` already tracks
    that without this process sampling anything — the same defensive getattr
    pair `memory()` uses, in case a wheel still spells it
    `mx.metal.get_peak_memory`.
    """
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
    """Hand MLX's allocator pool back to the OS — `mflux_image.release`'s own
    probe, verbatim. `worker_base.serve(release=...)` fires this
    `worker_base._RELEASE_IDLE_S` seconds after this worker's LAST generation
    if nothing new started in the meantime, not per call: a chat exchanged in
    a burst of turns, or a page issuing many short completions in a row, keeps
    the allocator at full speed throughout — only a run of `_RELEASE_IDLE_S`
    seconds with no new `/generate` at all actually clears it.

    Matters for the same stacking reason as `mlx_whisper.worker.release` and
    `mlx_embed.worker.release`: the supervisor keeps one resident worker PER
    CAPABILITY, so a text model sits in its own process next to whatever
    image, speech or embed worker is also loaded, each with its own idle
    pool — this is the runner most likely to be resident continuously through
    a whole session, which is exactly the case an idle-only release exists
    for, as against the per-call design this change's first cut used and
    would have thrashed a fast chat loop.

    `getattr` because a real but older mlx wheel, or this repo's stubbed
    `mlx.core` in tests, may not have `clear_cache` at all — absence is a
    no-op, matching every other MLX runner's guard.

    **Also where the retained prompt-KV-cache (`generate`'s own comment, at
    the `stream_generate` call, explains what it is) gets dropped.** That
    cache is live MLX arrays this process holds a Python reference to — real
    unified-memory bytes `clear_cache()` alone cannot reclaim, because nothing
    has told the allocator they are free. So the reference is dropped FIRST,
    here, in the same idle window that already exists for the weights'
    allocator pool; `clear_cache()` right after is what actually hands the
    now-unreferenced bytes back to the OS. A chat that resumes after the idle
    window pays one full prompt reprocess for its next turn — exactly the
    ordinary cold-start cost, and cheap next to leaking a growing KV cache for
    every resident-but-idle chat this process ever sees.
    """
    _loaded.pop("prompt_cache_state", None)

    import mlx.core as mx

    clear = getattr(mx, "clear_cache", None)
    if clear is not None:
        clear()


# ------------------------------------------------------------------ generation


def _messages_to_prompt(processor, messages, prompt):
    """The model's own chat template, never a hand-rolled one.

    Every instruct model has its own turn markers, and getting them wrong
    produces output that looks almost right — which is worse than an error.
    `apply_chat_template` is the tokenizer's own answer; a model without one
    falls back to the raw prompt.

    **Deliberately still the tokenizer's/processor's OWN `apply_chat_template`,
    never `mlx_vlm.prompt_utils.apply_chat_template`** — this is the one place
    the mlx-lm -> mlx-vlm switch must NOT reach for the new library's helper.
    Verified by hand: `mlx_vlm.prompt_utils.apply_chat_template` emits
    `<think>\\n\\n</think>\\n\\n` on a reasoning model — a CLOSED, empty think
    block, which is thinking turned OFF — where the tokenizer's own template
    leaves `<think>\\n` open. Calling mlx-vlm's helper here would silently flip
    every reasoning model in the catalog into non-thinking mode: no error, and
    `playground/think.ts` would simply stop finding a think block to render.
    mlx-vlm's own helper is reached for only on the path that actually carries
    an image (`generate`, commit 3), where the image placeholder tokens it
    inserts are the point of using it at all.

    `processor` rather than `tokenizer` in the parameter name only — the
    getattr dance below is unchanged, and it keeps working because a
    transformers `ProcessorMixin` exposes `apply_chat_template` and
    `chat_template` itself (forwarding to the tokenizer it wraps), the same
    shape mlx-lm's plain tokenizer had.
    """
    if prompt:
        return prompt
    template = getattr(processor, "apply_chat_template", None)
    if template and getattr(processor, "chat_template", None):
        return template(messages, tokenize=False, add_generation_prompt=True)
    return "\n\n".join(m.get("content", "") for m in messages if isinstance(m, dict))


def _prompt_tokens(processor, text):
    """How long the prompt is, in the model's OWN tokens — or None.

    The number the API reports as `input_tokens` (SPEC AI-3). Counted here
    rather than estimated upstream because this process is the only one holding
    the tokenizer that decides the answer: characters/4 would be a plausible
    number that disagrees with the model.

    Fail-soft by design. This is a METRIC, and a tokenizer that spells `encode`
    differently, or refuses this string, must cost the count and not the
    completion — `None` means "not reported", which every reader already
    handles (a worker that predates this said nothing at all).

    **`processor` may not have `.encode` itself.** mlx-vlm's `load()` returns a
    transformers `ProcessorMixin`, which usually wraps the actual tokenizer at
    `.tokenizer` rather than exposing `encode` directly — so this reaches for
    the wrapped tokenizer only when the processor itself has nothing to offer,
    preferring the processor's own `encode` where one exists.
    """
    encode = getattr(processor, "encode", None)
    if encode is None:
        encode = getattr(getattr(processor, "tokenizer", None), "encode", None)
    if encode is None:
        return None
    try:
        return len(encode(text))
    except Exception:  # noqa: BLE001 - a count may not break a generation
        return None


def _unreadable_image(path):
    """Why `path` cannot be sent to the model, or None.

    Checked BEFORE anything below touches the model: mlx-vlm's own answer to a
    missing file is a tensor-shape error raised deep inside the vision tower,
    which names no path at all — exactly the kind of failure a caller cannot
    act on. This names the path instead, the same discipline `/api/fs/pick-file`
    and every route that reads a caller-supplied path already follows.
    """
    if not os.path.isfile(path):
        return f"image not found: {path}"
    if not os.access(path, os.R_OK):
        return f"image not readable: {path}"
    return None


def generate(body, write):
    """Stream one completion as NDJSON: {chunk} lines, then {done}."""
    _pin_stream()
    from mlx_vlm import stream_generate
    from mlx_vlm.sample_utils import make_sampler

    model = _loaded.get("model")
    processor = _loaded.get("processor")
    if model is None or processor is None:
        write({"type": "done", "ok": False, "error": "no model is loaded"})
        return

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    # A list of absolute paths (`server/ai.py`'s `_images_problem` validates the
    # shape before this ever runs) — a LIST rather than the single `image` the
    # `/api/ai/image` route takes, because a VLM's chat template is told
    # `num_images` and asking about two pictures at once is the ordinary case
    # for this capability (unlike an edit, which has exactly one base image).
    #
    # **Images ride the CURRENT turn only.** `history` (server/ai.py) stays
    # text-only by design — this worker never re-attaches an image to a prior
    # turn — so a follow-up question about a picture already sent will not
    # re-see it. That is a deliberate v1 boundary: multi-turn image memory would
    # mean re-sending the placeholder tokens (and the pixels) on every turn of
    # a growing history, which is a real design question this build does not
    # answer, not an oversight in this one.
    #
    # `isinstance(raw_images, list)`, not a bare `or []`: the server validates
    # this shape today (`_images_problem`), but this worker's OWN contract
    # should not depend on that — a caller that sent a single string instead
    # of a list would otherwise have this comprehension iterate its
    # CHARACTERS, and report a mystifying "image not found: /" for whichever
    # character `_unreadable_image` reached first.
    raw_images = body.get("images")
    images = ([p for p in raw_images if isinstance(p, str) and p]
             if isinstance(raw_images, list) else [])
    if images:
        # `config` is the dict `load()` stashed at load time — needed both
        # for the MODEL-axis check right below and later to build the
        # image-aware prompt via `apply_chat_template`. Checked for real
        # content up front, not lazily: an empty dict would otherwise reach
        # either use as a confusing failure with no path, no model, nothing
        # a caller could act on. `load()` always stashes a real config for
        # anything that got far enough to generate, so this is defensive
        # rather than an expected failure.
        config = _loaded.get("config")
        if not config:
            write({"type": "done", "ok": False,
                   "error": "no model configuration was found to build an "
                            "image-aware prompt from"})
            return

        # **The MODEL axis, checked BEFORE a single path is even looked
        # at.** `server/ai.py`'s `_images_problem`/`_accepts_image` already
        # try to stop this at the door, but that gate reads a CACHED
        # `config.json` — a prediction that can disagree with what is
        # actually resident: the model on disk could have been swapped
        # after the server last read it, an older server may carry no such
        # gate at all, and nothing stops a caller reaching this worker
        # directly. This process is the one place that KNOWS what it
        # loaded, so it asks its OWN copy of the config again here.
        #
        # **Answered from CONFIG EVIDENCE, never from a `getattr` on the
        # loaded model object.** A first cut of this check read
        # `model.vision_tower`, which is wrong: that attribute's name is NOT
        # standardised across mlx-vlm's model zoo — verified against every
        # `class Model(...)` in the installed 0.6.15 package — `vision_tower`
        # is only the most common spelling. At least 18 real architectures
        # name the SAME thing `vision_model` (idefics2, idefics3,
        # internvl_chat, llama4, deepseekocr, molmo_point, and others),
        # `vision` (deepseek_vl_v2, moondream2, moondream3, rt_detr_v2), or
        # `visual` (paddleocr_vl). A getattr keyed on one name refuses every
        # architecture spelled another way — genuine vision-language models,
        # rejected as "no vision tower" — which is the exact opposite of the
        # intended behaviour, and invisible to a test that only fakes the
        # common attribute name.
        #
        # So this asks the same question `hub_cache.has_vision_tower` and
        # `_architecture_task` already ask of a checkpoint's OWN config: a
        # `vision_config` block and/or an `image_token_id` is the
        # architecture-NAME-INDEPENDENT evidence that this checkpoint has a
        # tower — one definition of "has a tower" for the whole codebase,
        # not a second one invented here from whichever attribute this file
        # happens to recognise.
        #
        # **The rule is refuse-on-POSITIVE-evidence, not refuse-on-absence:**
        # only a config with NEITHER key is text-only. An architecture this
        # file has never heard of, but whose config declares a tower, must
        # be let through to mlx-vlm — the config already answered the
        # question, and mlx-vlm is the authority on whether IT can build
        # that tower, not this worker guessing from an attribute name it
        # happens to know. Silently answering about a picture the model
        # cannot even see is still the worst failure this app has — this
        # check exists for the genuinely text-only case, not to second-guess
        # every architecture mlx-vlm ships.
        if "vision_config" not in config and "image_token_id" not in config:
            write({"type": "done", "ok": False,
                   "error": f"{_loaded.get('model_id') or 'this model'} has no "
                            "vision tower to read an image with"})
            return

        for path in images:
            problem = _unreadable_image(path)
            if problem:
                write({"type": "done", "ok": False, "error": problem})
                return
        # The ONE place mlx-vlm's own `apply_chat_template` is correct rather
        # than a hazard (see `_messages_to_prompt`'s docstring): the image
        # placeholder tokens it inserts into the prompt are what the model
        # needs to know a picture is coming, and the tokenizer's own template
        # has no notion of that at all. `model_type` out of `config` is how
        # the helper picks this checkpoint's own message format
        # (`prompt_utils.MODEL_CONFIG`).
        #
        # **`enable_thinking=True`, explicitly** — mlx-vlm's helper (verified
        # against the installed 0.6.15 source) defaults this to `False` on any
        # template that accepts the kwarg, which closes the think block the
        # same way `_messages_to_prompt`'s docstring says the OTHER helper
        # must never be reached for: a reasoning model (Qwen3.5 and friends)
        # would silently drop visible thinking the moment an image is
        # attached, with no error and nothing for `playground/think.ts` to
        # render. Passing it here keeps the image path's thinking behaviour
        # identical to the text path's (an unset kwarg is simply unused by a
        # template that never asks for it, so this is a no-op there).
        from mlx_vlm.prompt_utils import apply_chat_template

        text = apply_chat_template(processor, config, messages,
                                   num_images=len(images), enable_thinking=True)
    else:
        text = _messages_to_prompt(processor, messages, body.get("prompt") or "")
    max_tokens = int(body.get("max_tokens") or 1024)
    sampler = make_sampler(
        temp=float(body.get("temperature", 0.7)),
        top_p=float(body.get("top_p", 0.95)),
    )

    # Counted BEFORE the first token, so a cancelled generation reports it too:
    # the prompt was read whether or not the answer was wanted by the end.
    #
    # **`None` on the image path, deliberately, rather than a wrong number.**
    # `_prompt_tokens` counts the TEMPLATED TEXT — the string `apply_chat_
    # template` returned, with one placeholder TOKEN standing in for a
    # picture — but that placeholder later expands, deep inside the vision
    # tower, into anywhere from dozens to thousands of real vision tokens the
    # text encoder never sees as characters. Counting the templated string
    # would report a number smaller than what the model actually read by
    # roughly the whole cost of every attached image, under a label
    # (`input_tokens`, SPEC AI-3) that promises the model's own count. This is
    # a METRIC (fail-soft is the rule, per `_prompt_tokens`'s own docstring),
    # and an under-report presented as a count is a worse metric than an
    # honest `None` — so the image path reports nothing rather than reports
    # wrong.
    prompt_tokens = None if images else _prompt_tokens(processor, text)

    # **Prompt-cache reuse: the whole reason this exists.** Without it, every
    # turn of a chat hands `stream_generate` the FULL templated conversation —
    # `text`, built above from the WHOLE `messages` history — and it reprocesses
    # every earlier turn's tokens from scratch before producing a single new
    # one. On a long conversation that reprocessing, not the new turn's actual
    # content, is what time-to-first-token is mostly paying for.
    #
    # mlx-vlm 0.6.15 (the floor of this folder's `mlx-vlm>=0.6.15,<0.7` pin —
    # verified present at both that floor and the 0.6.17 ceiling-adjacent tag)
    # ships exactly the mechanism this needs, built INTO `stream_generate`
    # itself, not bolted on here:
    # https://raw.githubusercontent.com/Blaizzy/mlx-vlm/v0.6.15/mlx_vlm/generate/dispatch.py
    # https://raw.githubusercontent.com/Blaizzy/mlx-vlm/v0.6.15/mlx_vlm/generate/common.py
    # A `PromptCacheState` (`mlx_vlm.PromptCacheState`, re-exported at the
    # package root per `mlx_vlm/__init__.py`) is a tiny object holding the
    # FULL TOKEN-ID LIST of the last processed prompt plus its live KV cache.
    # Handed to `stream_generate` as `prompt_cache_state=`, `dispatch.py`'s own
    # `find_prefix_length` walks the new turn's tokens against the stored ones
    # and returns how many agree from position 0 — the EXACT prefix identity
    # this needs, never a message count or a hash of a summary: any edit,
    # regeneration, branch, or system-prompt change changes a token somewhere
    # in that walk and the match stops exactly there, at worst returning 0.
    # `stream_generate` then trims its retained KV cache back to that many
    # tokens and prefills ONLY the new suffix — the old prefix's KV states are
    # reused rather than recomputed. **The safe path is the automatic
    # fallback, not a mode this file has to remember to choose**: a zero-length
    # (or otherwise unusable — a wrapped rotating-window cache, e.g.) match
    # falls straight through to `dispatch.py`'s own "ensure we have a
    # prompt_cache" branch, which builds a fresh one and cold-prefills the
    # whole prompt — precisely what happened before this change. Getting the
    # match wrong in the OTHER direction — reusing a prefix that is not
    # actually a match — is the failure this cannot have (text conditioned on
    # the wrong conversation, silently), and it cannot happen here because the
    # comparison is a literal token-by-token walk, not an approximation.
    #
    # **One `PromptCacheState`, kept across turns in `_loaded` for exactly as
    # long as this process keeps its model** — same lifetime as `model` and
    # `processor`, dropped in `release()` (this process's only mechanism for
    # giving retained memory back between chat bursts) and cleared again at
    # the top of `load()` in case a process ever loads a second model. Created
    # lazily, the first time a text-only turn asks for it, rather than in
    # `load()`: a model that only ever answers image turns should not pay for
    # a `PromptCacheState` it will never populate.
    #
    # **TEXT-ONLY TURNS ONLY — never on the branch that built `text` with an
    # attached image, above.** `dispatch.py` DOES carry its own image-turn
    # guard for this (`_apc_suffix_is_text_only`, sourced from
    # `mlx_vlm.apc.media_safe_prefix_min`: it refuses to reuse a cache whose
    # trimmed suffix would cut through a media-placeholder token), but reading
    # that guard's own source (`mlx_vlm/apc.py`) shows it only recognises
    # placeholder ids named `image_token_id`/`image_token_index`/
    # `video_token_id`/`video_token_index` in the checkpoint's config — and
    # this file's own vision-tower gate, just above, already had to learn the
    # hard way (its docstring: at least 18 real architectures) that mlx-vlm's
    # naming is NOT standardised across its model zoo. A checkpoint that
    # passes this file's OWN gate on `vision_config` alone, with none of those
    # four id keys, would make `dispatch.py`'s guard a silent no-op — the
    # exact "reusing a cache across turns with different attached images"
    # correctness bug this task exists to rule out, on an architecture this
    # file cannot enumerate in advance. Excluding the whole image branch is
    # the same discipline `prompt_tokens = None if images` already applies a
    # few lines up: refuse to guess about the image path rather than trust a
    # check whose coverage this file cannot fully verify, and report/behave
    # honestly (a full reprocess) instead. This costs nothing on an ordinary
    # text turn threaded between image turns, either — the stored state is
    # simply left untouched by an image turn (never handed to
    # `stream_generate` there, so `dispatch.py` never calls `.update()` on
    # it), and the NEXT text-only turn still finds its own last text-only
    # prefix waiting, since conversation history is text-only to begin with
    # (this worker never re-attaches an image to a prior turn — see the
    # `images` comment above).
    cache_kwargs = {}
    if not images:
        from mlx_vlm import PromptCacheState

        cache_state = _loaded.get("prompt_cache_state")
        if cache_state is None:
            cache_state = PromptCacheState()
            _loaded["prompt_cache_state"] = cache_state
        cache_kwargs["prompt_cache_state"] = cache_state

    count = 0
    started = time.time()
    for response in stream_generate(model, processor, text, image=images or None,
                                    max_tokens=max_tokens, sampler=sampler,
                                    **cache_kwargs):
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
                      streaming=True, memory=memory, peak_memory=peak_memory,
                      release=release)
