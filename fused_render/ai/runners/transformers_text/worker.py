"""Text generation on transformers: one resident model, four routes (SPEC §40).

The cross-platform counterpart of `mlx_text/worker.py`, and the same shape: the
HTTP contract, the download reporting and the state machine are `worker_base`'s,
and what lives here is only what is true of torch and transformers in particular
— which dtype to load at, which device to put it on, how a token loop reports
itself, and what to say when a checkpoint is in a format this cannot read.

It exists because `mlx_text` is Metal-only, which made the app's flagship local
capability something a Windows or Linux user could read about but not use — the
complaint AI-10 answers for transcription and this answers for chat. The
registry lists it AFTER the MLX runner, so first-match-wins leaves Apple Silicon
on MLX when available (faster there, and its 4-bit catalog is sized for a laptop),
hands this to Windows and Linux, and retains this runner as the Apple Silicon
fallback.

Three things differ from the MLX runner, and all three are torch's doing:

* **The device is a choice, not a given.** MLX is Metal or nothing; torch runs on
  CUDA, MPS or CPU, and which one it got is the single most useful thing this
  process can tell the user — a model answering at three tokens a second is
  working perfectly, and looks broken. So `load()` records it and `/health`
  reports it, which is why `worker_base.STATE` grew a `device` field.
* **CPU is a first-class case, so the dtype is bfloat16 rather than float32.**
  See `_placement`.
* **Cancelling is a StoppingCriteria**, because `model.generate` owns the loop
  here — where mlx-lm hands back a generator this runner drives itself, so the
  only interruption point is the callback transformers offers.

Deliberately torch + transformers only. No FastAPI, no requests — this process
must start fast, and its dependency list is a thing users download.
"""

from collections.abc import Mapping
import json
import os
import sys
import time

# The base sits one directory up, in `runners/` — see mlx_text/worker.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import formats  # noqa: E402 - the shared format checks; see formats.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded (model, tokenizer) and the device they are on. One per process.
_loaded = {}


# --------------------------------------------------------------- model loading


def download(model_id):
    """The whole repo. transformers reads a directory of safetensors plus the
    tokenizer, so there is no single file to pick out — the same shape MLX has,
    and unlike the image runner, which swaps one quantized checkpoint into an
    otherwise-normal pipeline."""
    return worker_base.download_snapshot(model_id)


#: Quantization schemes a checkpoint can declare that this runner cannot load,
#: and what the user should do about each — named rather than left to the
#: loader's own error, for the reason `faster_whisper/worker.py` spells out
#: about CTranslate2.
#:
#: In `formats` with the other backends' format checks, because the AI Models
#: page reads the same table: a repo this runner refuses by name must not be
#: tagged as one it loads.
_UNLOADABLE_QUANT = formats.UNLOADABLE_QUANT

#: What to suggest instead, once we have refused. One sentence, and a repo that
#: really does load, because a user who picked the wrong format should learn the
#: right one from the error rather than from a web search.
_TRY_INSTEAD = (
    "Try an unquantized checkpoint such as Qwen/Qwen3-4B-Instruct-2507 or "
    "microsoft/Phi-4-mini-instruct."
)


def _config(path):
    """The checkpoint's `config.json`, or an empty dict.

    Read rather than inferred from the repo name: `mlx-community/…-4bit` is a
    naming convention, and a refusal this file prints must rest on what the
    checkpoint SAYS about itself — the same rule `ai_models._quantization`
    follows for the number it puts on a card.
    """
    try:
        with open(os.path.join(path, "config.json"), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _weights_here(path):
    """Does this snapshot hold weights transformers can open?

    A repo of nothing but `.gguf` is llama.cpp's format. transformers can read
    one, but it DEQUANTIZES to float32 on the way in — a 4GB quantized file
    becomes ~16GB of RAM — so a load that "worked" would OOM the machine or swap
    it to a standstill, which is a worse answer than a refusal.
    """
    for _dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            if name.endswith((".safetensors", ".bin", ".pt")):
                return True
    return False


def _refuse_unloadable(model_id, path):
    """Raise with the CAUSE named, for a checkpoint this runner cannot read.

    Ordered so the most specific answer wins: an MLX repo is also a repo with a
    `quantization` block, and "this is built for Apple's MLX" is a great deal
    more useful than "this declares a quantization I don't know".
    """
    config = _config(path)

    # MLX's own quantization, which is bit-packed for Metal kernels and means
    # nothing to torch. Worth catching first and by name: the catalog's Apple
    # Silicon suggestions are all `mlx-community/…`, they sit one tab away on
    # the same page, and this is the single likeliest wrong turn a user takes.
    quantization = config.get("quantization")
    if isinstance(quantization, dict) and "group_size" in quantization:
        raise RuntimeError(
            f"{model_id} is an MLX checkpoint — its weights are packed for "
            "Apple's Metal kernels and torch cannot read them. MLX models only "
            f"load on Apple Silicon. {_TRY_INSTEAD}")

    block = config.get("quantization_config")
    method = block.get("quant_method") if isinstance(block, dict) else None
    if isinstance(method, str) and method.lower() in _UNLOADABLE_QUANT:
        raise RuntimeError(
            f"{model_id} is {_UNLOADABLE_QUANT[method.lower()]}. {_TRY_INSTEAD}")

    if not _weights_here(path):
        raise RuntimeError(
            f"{model_id} has no safetensors weights — a repo of GGUF files is "
            "llama.cpp's format, and transformers can only read one by "
            f"expanding it to float32, which costs four times the memory. "
            f"{_TRY_INSTEAD}")


def _placement():
    """`(device, dtype)` — where this model runs and at what width.

    Three devices and one dtype rule, and the dtype is the part worth reading.

    **CPU loads at bfloat16, never float32.** torch's default for a checkpoint
    with no dtype is float32, which doubles a model that was published at 16
    bits: a 4B model is 8GB at bfloat16 and 16GB at float32, so the default
    turns the one configuration this runner exists to serve — a Windows laptop
    with no GPU — from tight into impossible. The cost is that a CPU without
    native bf16 emulates it, which is slower than float32 on paper; it is still
    the right trade, because a model that swaps is not slow, it is unusable.

    **MPS takes float16 rather than bfloat16.** This runner only reaches an
    Apple machine when the MLX one is unavailable, and float16 is the width
    Metal has supported longest — a fallback path is the wrong place to want a
    recent macOS.

    CUDA takes bfloat16 where the card supports it, which every card new enough
    to matter does, and float16 otherwise.
    """
    import torch

    if torch.cuda.is_available():
        supported = getattr(torch.cuda, "is_bf16_supported", None)
        try:
            bf16 = bool(supported()) if supported else False
        except (RuntimeError, AssertionError):
            bf16 = False
        return "cuda", (torch.bfloat16 if bf16 else torch.float16)
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.bfloat16


def _dtype_kwargs(dtype):
    """`{"dtype": …}` or `{"torch_dtype": …}`, whichever this transformers takes.

    `torch_dtype` was renamed to `dtype` in 4.56 and the old spelling deprecated
    behind a warning; it is scheduled to go in v5. This runner declares
    `transformers>=4.46` with no ceiling, so BOTH spellings are live options on
    a machine somewhere, and the wrong one is not a loud failure: unknown
    keyword arguments to `from_pretrained` are forwarded into the config rather
    than rejected, so a stale spelling would silently load a 4B model at float32
    and OOM a laptop that had every right to run it.

    Parsed tolerantly — a dev build is `4.57.0.dev0` — and an unreadable version
    takes the modern spelling, since that is the one that keeps working.
    """
    import transformers

    parts = []
    for piece in str(getattr(transformers, "__version__", "")).split(".")[:2]:
        digits = "".join(c for c in piece if c.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    if len(parts) == 2 and (parts[0], parts[1]) < (4, 56):
        return {"torch_dtype": dtype}
    return {"dtype": dtype}


def load(model_id, path):
    """`path` is what `download` returned — the snapshot directory."""
    # The format check comes FIRST, before the heavy imports, for the reason
    # `faster_whisper/worker.py` gives: a repo in the wrong format is a fact
    # about the download rather than about this environment, and importing
    # first would replace the explanation with whichever error torch happened
    # to raise on the way past.
    _refuse_unloadable(model_id, path)

    import torch  # noqa: F401 - imported for its side effect on the ones below
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device, dtype = _placement()
    tokenizer = AutoTokenizer.from_pretrained(path)

    kwargs = dict(_dtype_kwargs(dtype), low_cpu_mem_usage=True)
    if device == "cuda":
        # `device_map` is accelerate's, and on CUDA it earns its place twice: it
        # streams the shards onto the card as they load rather than building the
        # whole model in RAM first, and it offloads whatever does not fit
        # instead of raising. On CPU and MPS it buys nothing — there is one
        # device and nowhere to offload to — so those take a plain `.to()`.
        kwargs["device_map"] = "auto"
    model = AutoModelForCausalLM.from_pretrained(path, **kwargs)
    if device != "cuda":
        model.to(device)
    model.eval()

    _loaded["model"] = model
    _loaded["tokenizer"] = tokenizer
    _loaded["device"] = device
    # Published on `/health`, which is where the AI Models page reads it. The
    # supervisor never computes this: only the process holding the weights knows
    # where they actually landed, which is the same argument AI-8 makes about
    # resident bytes.
    worker_base.set_state(device=device)


def memory():
    """What torch says it is holding, in bytes.

    The same probe the image runner uses, and for the same reason: on CUDA and
    MPS the weights live in an allocator's pool that the process's resident set
    does not count, so RSS alone reports the interpreter. `worker_base` takes
    the larger of this and RSS, which is what keeps the CPU case — where the
    tensors ARE in RSS and these probes read zero — honest.
    """
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


# ------------------------------------------------------------------ generation


def _apply_template(tokenizer, messages):
    """The model's own chat template, with REASONING OFF by default.

    **Qwen3's template defaults `enable_thinking` to true**, and three of the
    four models this runner's catalog suggests are Qwen3. Left on, an ordinary
    "what does this file do?" emits a `<think>` block first — hundreds of tokens
    of hidden reasoning that the caller has no way to tell apart from the answer,
    since `/generate` streams whatever the model produces. On the CPU path this
    runner exists to serve, at a few tokens a second, that is minutes of silence
    before the first useful word, on a machine already suspected of being slow.
    So the default is the one that makes a chat box behave like a chat box.

    It is passed to EVERY model, which is safe and deliberate: `kwargs` here
    land in the Jinja render context, and a template that never mentions
    `enable_thinking` — Phi-4's, Llama's — simply does not read it. The retry
    exists for the other kind of skew, a tokenizer whose `apply_chat_template`
    rejects the keyword outright: a model that will not take the hint should
    still answer, just verbosely.
    """
    try:
        return tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt",
            enable_thinking=False)
    except TypeError:
        return tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt")


def _encode(tokenizer, messages, prompt, device):
    """The prompt as token ids on the model's device.

    **Two paths, because tokenizing a rendered chat template is not the same as
    tokenizing text.** A template emits the model's own turn markers INCLUDING
    its leading BOS, and `tokenizer(text)` adds another one on top — two BOS
    tokens, which no model saw in training and which degrade the answer without
    breaking it. That is the "looks almost right" failure the MLX runner's
    `_messages_to_prompt` warns about, one layer down. Asking
    `apply_chat_template` for the ids directly is what avoids it, rather than
    remembering to pass `add_special_tokens=False` at the one call site that
    needs it.

    A raw `prompt` (the API's `raw` option) is the other path and gets the
    ordinary treatment, special tokens and all — that is what asking for no chat
    template means.
    """
    import torch

    if prompt:
        encoded = tokenizer(prompt, return_tensors="pt")
        ids, mask = encoded["input_ids"], encoded.get("attention_mask")
    elif getattr(tokenizer, "chat_template", None):
        encoded = _apply_template(tokenizer, messages)
        # transformers v4 returned the input-id tensor directly, while v5
        # consistently returns a BatchEncoding like an ordinary tokenizer call.
        # Accept both: this runner deliberately supports both sides of that
        # dependency boundary, and handing the v5 mapping to `torch.ones_like`
        # produces "input must be Tensor, not BatchEncoding" before generation
        # can start.
        if isinstance(encoded, Mapping):
            ids, mask = encoded["input_ids"], encoded.get("attention_mask")
        else:
            ids, mask = encoded, None
    else:
        # No template to apply. The same fallback MLX takes — better a plain
        # concatenation than turn markers invented here.
        encoded = tokenizer(
            "\n\n".join(m.get("content", "") for m in messages if isinstance(m, dict)),
            return_tensors="pt")
        ids, mask = encoded["input_ids"], encoded.get("attention_mask")
    if mask is None:
        # One un-padded sequence, so every position is real. Passed explicitly
        # because transformers warns (and on some models silently mis-attends)
        # when it has to infer one from a pad token the model may also use as
        # its EOS.
        mask = torch.ones_like(ids)
    return ids.to(device), mask.to(device)


def _prompt_tokens(ids):
    """How many tokens the encoded prompt is, or None if this cannot say.

    `ids` is one un-padded sequence (`_encode`), so its last dimension is the
    length — via `.shape` for a real tensor, `len()` for anything sequence-like.
    """
    shape = getattr(ids, "shape", None)
    try:
        if shape is not None:
            return int(shape[-1])
        return len(ids)
    except Exception:  # noqa: BLE001 - a count may not break a generation
        return None


def _stopping_criteria(local_stop=None):
    """Stop when `/cancel` was pressed or this stream went away.

    `model.generate` owns the token loop here — unlike mlx-lm, which hands back
    a generator this runner steps itself — so its own callback is the only place
    a stop can be honoured. Without it the ✕ would be read only after generation
    finished, which is to say never, since finishing is what it was trying to
    avoid. The request-local event covers the other way a token loop ends early:
    a disconnected client makes its `write` raise. That producer must stop and
    join before the worker releases its generation lock, or the next request can
    enter `model.generate` while the abandoned one is still using the model.
    """
    from transformers import StoppingCriteria, StoppingCriteriaList

    class Cancelled(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs):
            return worker_base.CANCEL.is_set() or (
                local_stop is not None and local_stop.is_set())

    return StoppingCriteriaList([Cancelled()])


def generate(body, write):
    """Stream one completion as NDJSON: {chunk} lines, then {done}."""
    import threading

    import torch
    from transformers import TextIteratorStreamer

    model = _loaded.get("model")
    tokenizer = _loaded.get("tokenizer")
    if model is None or tokenizer is None:
        write({"type": "done", "ok": False, "error": "no model is loaded"})
        return

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    ids, mask = _encode(tokenizer, messages, body.get("prompt") or "", model.device)
    # What the model READ, reported as `input_tokens` (SPEC AI-3). Free here —
    # the prompt has just been tokenized, and its length is the answer — where
    # anything upstream would have to estimate it and disagree with the model.
    # Fail-soft like the count itself: a tensor shape this cannot read costs the
    # metric, never the generation.
    prompt_tokens = _prompt_tokens(ids)
    max_tokens = int(body.get("max_tokens") or 1024)
    temperature = float(body.get("temperature", 0.7))
    top_p = float(body.get("top_p", 0.95))

    local_stop = threading.Event()
    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    kwargs = {
        "input_ids": ids,
        "attention_mask": mask,
        "max_new_tokens": max_tokens,
        "streamer": streamer,
        "stopping_criteria": _stopping_criteria(local_stop),
        # A model whose tokenizer has no pad token pads with EOS, which is
        # transformers' own advice and silences a warning that would otherwise
        # reach the supervisor's log on every single generation.
        "pad_token_id": tokenizer.pad_token_id if tokenizer.pad_token_id is not None
                        else tokenizer.eos_token_id,
    }
    if temperature > 0:
        kwargs.update(do_sample=True, temperature=temperature, top_p=top_p)
    else:
        # Not `temperature=0`: transformers divides the logits by it. Greedy
        # decoding is the thing a caller asking for zero actually wants, and
        # sampling params are refused alongside it rather than ignored, since a
        # `top_p` that silently did nothing is the failure this app avoids
        # everywhere else.
        kwargs["do_sample"] = False

    result = {}

    def run():
        try:
            with torch.inference_mode():
                model.generate(**kwargs)
        except BaseException as e:  # noqa: BLE001 - carried out to the streaming thread
            result["error"] = e
        finally:
            # `TextIteratorStreamer` ends its iteration when generate() calls
            # `end()`, and a generate() that RAISED never does — so without
            # this the loop below blocks forever on a model that failed in its
            # first forward pass, and the request never answers.
            streamer.end()

    # Generation runs on a thread and the tokens are consumed HERE, which is the
    # shape TextIteratorStreamer requires: it is a blocking queue with a producer
    # on one side and a consumer on the other, and both cannot be this thread.
    thread = threading.Thread(target=run, name="generate", daemon=True)
    thread.start()

    count = 0
    started = time.time()
    try:
        for text in streamer:
            if not text:
                continue
            count += 1
            write({"type": "chunk", "text": text})
    finally:
        # `write` raises when the client disconnects. Signal the producer and
        # wait for it here so `worker_base` cannot release GENERATE_LOCK while
        # this model is still generating for a request nobody can read.
        local_stop.set()
        thread.join()

    if "error" in result:
        raise result["error"]
    if worker_base.CANCEL.is_set():
        write({"type": "done", "ok": True, "cancelled": True, "tokens": count,
               "input_tokens": prompt_tokens})
        return
    write({
        "type": "done", "ok": True, "tokens": count,
        "input_tokens": prompt_tokens,
        "seconds": round(time.time() - started, 2),
    })


if __name__ == "__main__":
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=True, memory=memory)
