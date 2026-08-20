"""Text generation on llama.cpp / GGUF: one resident model, four routes (SPEC §40).

**This module is the whole of the runner and it sits at the runners ROOT**,
beside `worker_base.py`, `formats.py` and `torch_text.py`, for the reason
`torch_text.py` states about itself: `llamacpp_text/` holds only a
`pyproject.toml` and a five-line `worker.py` shell around `main()` below.

**Opt-in, and registered BELOW every `transformers-text` row** (`registry.py`)
— `auto` resolution never reaches this runner, on any platform. That is not a
benchmark verdict, it is a packaging one: AI-11's amendment records that the
maintainer's wheel index is a coin-flip per release on macOS arm64 (roughly
4 of 16 sampled releases are intact), so a capability this fragile to install
must never be what a machine gets without asking for it. `llamacpp_text/pyproject.toml`
carries the version this was audited against and the audit itself; bumping the
pin without repeating it is the one thing that folder's comment forbids.

**The model-id problem, and why there is no `repo:Q4_K_M` grammar.** A GGUF
repo commonly publishes 25-30 quantizations of one model — `unsloth/Qwen3.5-9B-GGUF`
alone is 147.81GB across every file it holds — so a MODEL here is really a
`(repo, filename)` pair, while the rest of this app addresses a model by one
string id. Inventing an id syntax to encode that pair would touch every page,
preference and cache tag that currently treats a model id as a Hub repo id
verbatim. `_GGUF_RECIPES` solves it the way `torch_image._GGUF_RECIPES` solves
the same shape of problem for FLUX's quantized transformer: the id is an
opaque, curated key (here, simply the GGUF's own filename — already unique,
already meaningful to a reader, and never parsed for structure) mapped to the
`(repo, file)` it actually downloads. **The consequence is deliberate and
documented rather than fixed**: Hub search on the Discover tab cannot offer a
model this runner would load, because typing a bare repo id into that search
box supplies no filename and this runner has no rule for picking one out of
thirty — only the ids in `_GGUF_RECIPES` (the ones `catalog.SUGGESTIONS` also
lists) ever load. That mirrors `formats.COMPONENT_REPOS`, whose repos are
likewise not addressable by a user typing on the Discover tab.

**No external tokenizer/config download, and that is a fact about THESE
repos, not a general rule.** unsloth's GGUF conversions — the ones
`_GGUF_RECIPES` curates — ship no `tokenizer_config.json` or `config.json` at
their root: checked directly against the Hub API on 2026-08-21, the only
non-GGUF files in `unsloth/Qwen3.5-4B-GGUF` and `unsloth/Qwen3.5-9B-GGUF` are
`.gitattributes`, `README.md` and an imatrix calibration file, none of them a
tokenizer. That is not an oversight on unsloth's part — it is the point of the
GGUF format: the vocabulary, the architecture and (since llama.cpp's chat
template support landed) the chat template all live inside the ONE file's own
key-value metadata, which is exactly what `llama_cpp.Llama` reads at load time
into `.metadata`. So `download()` fetches exactly one file
(`worker_base.download_file`) and nothing else — there is no
`download_snapshot(..., allow_patterns=…)` call here, because there is nothing
at those repos' roots for it to fetch.

Three things differ from `torch_text.py`, and all three are llama.cpp's doing:

* **CPU only, and that is the whole of the device story.** No CUDA/ROCm/Metal
  variant of this runner exists in this change (the pyproject header states
  why), so `worker_base.set_state(device="cpu")` is unconditional rather than
  probed — unlike torch, there is no second device this process could have
  landed on.
* **The chat template is rendered by hand, from the GGUF's own embedded jinja2
  source, because `create_completion(stream=True)` — not
  `create_chat_completion` — is what keeps the streaming contract identical to
  `torch_text.generate`'s NDJSON shape.** `create_chat_completion`'s streaming
  reply is OpenAI-delta-shaped and would need reshaping back into this app's
  `{"type": "chunk"}` frames anyway, so rendering the prompt ourselves and
  calling the low-level completion API keeps one code path instead of two.
  `enable_thinking=False` is passed into the render context unconditionally,
  the same default `torch_text._apply_template` chooses and for the same
  reason (AI-11d): three of this runner's curated models are Qwen3.5 GGUFs,
  whose upstream template defaults reasoning ON. Jinja simply ignores a
  context variable a template never references, so — unlike transformers'
  `apply_chat_template`, which can raise on an unexpected keyword — no retry
  is needed here.
* **Cancelling needs no thread.** `model.generate` in `torch_text` owns its own
  loop, so a `StoppingCriteria` callback is the only interruption point and a
  producer thread is required to let `TextIteratorStreamer` hand tokens back
  to this process while generation runs. `Llama.create_completion(stream=True)`
  is an ordinary Python generator that computes one token per `next()` — this
  loop IS the token loop — so checking `worker_base.CANCEL` between iterations
  is the whole of cancellation, and a `write()` that raises on a client
  disconnect simply propagates out of `generate()` with nothing left running
  and nothing to join.

Deliberately llama-cpp-python + jinja2 + huggingface_hub only. No FastAPI, no
requests — this process must start fast, and its dependency list is a thing
users download.
"""

from __future__ import annotations

import os
import sys
import time

# The base sits in THIS directory, and so does everything else this imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded `Llama` instance. One per process.
_loaded = {}

#: How much context this runner asks llama.cpp to allocate. 8192 rather than a
#: GGUF's own trained maximum (Qwen3.5 supports far more): this runner exists
#: to serve ordinary chat turns on a CPU, where a larger KV cache is memory and
#: prompt-processing time spent on headroom nobody asked for. Raising it is a
#: one-line change if a curated model's use case needs it.
_N_CTX = 8192

#: Curated `(repo, file)` pairs, keyed by an OPAQUE id — see the module
#: docstring for why this is not a `repo:quant` grammar. The key is the GGUF's
#: own filename, which is already unique within its repo and already
#: meaningful to read; nothing here parses it for structure. Every entry here
#: should also appear in `catalog.SUGGESTIONS["llamacpp-text"]` — the two
#: tables answer different questions (this one "how do I fetch it", that one
#: "should I suggest it") but a model this runner can load and the catalog
#: never mentions, or the reverse, is the drift `formats.COMPONENT_REPOS`
#: warns about one level up.
_GGUF_RECIPES = {
    "Qwen3.5-4B-Q5_K_M.gguf": {
        "repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q5_K_M.gguf",
    },
    "Qwen3.5-4B-Q8_0.gguf": {
        "repo": "unsloth/Qwen3.5-4B-GGUF",
        "file": "Qwen3.5-4B-Q8_0.gguf",
    },
    "Qwen3.5-9B-Q4_K_M.gguf": {
        "repo": "unsloth/Qwen3.5-9B-GGUF",
        "file": "Qwen3.5-9B-Q4_K_M.gguf",
    },
    "Qwen3.5-9B-Q8_0.gguf": {
        "repo": "unsloth/Qwen3.5-9B-GGUF",
        "file": "Qwen3.5-9B-Q8_0.gguf",
    },
    "Qwen3.8-27B-UD-Q3_K_XL.gguf": {
        "repo": "unsloth/Qwen3.8-27B-GGUF",
        "file": "Qwen3.8-27B-UD-Q3_K_XL.gguf",
    },
}

#: What to say when a caller asks for a model id this runner has no recipe
#: for. Named rather than left to a bare `hf_hub_download` 404, because "no
#: recipe" and "the file does not exist" are different facts a user can act on
#: differently — the first means "pick from this engine's own list", the
#: second means "you mistyped a repo id".
_NOT_CURATED = (
    "{model_id!r} is not one of this engine's curated models. A GGUF repo "
    "commonly publishes two dozen quantizations of one model, so llamacpp-text "
    "loads only the (repo, file) pairs in its own catalog rather than "
    "guessing which file a bare repo id means — pick a model from the AI "
    "Models page's suggestions for this engine."
)


def _recipe(model_id):
    return _GGUF_RECIPES.get(model_id)


# --------------------------------------------------------------- model loading


def download(model_id):
    """The one GGUF file this model means — never the whole repo.

    A repo in `_GGUF_RECIPES` publishes many more files than the one curated
    here (`unsloth/Qwen3.5-9B-GGUF` alone is 147.81GB whole), so the ordinary
    "download the repo" a snapshot-based runner uses would be catastrophic
    here. `worker_base.download_file` is the one-file counterpart
    `torch_image.py` uses for its own quantized-transformer swap, and it is
    already progress-instrumented against that ONE file's size rather than the
    repo's.
    """
    recipe = _recipe(model_id)
    if not recipe:
        raise RuntimeError(_NOT_CURATED.format(model_id=model_id))
    filename = recipe["file"]
    return worker_base.download_file(
        recipe["repo"], filename, detail=f"Fetching {filename}…")


def load(model_id, gguf_path):
    """`gguf_path` is what `download` returned — the one `.gguf` file's path."""
    # The curation check comes first, before the heavy import, for the reason
    # `torch_text.load` gives about its own format check: a model this runner
    # was never going to serve is a fact about the request, and importing
    # first would replace a clear refusal with whatever llama.cpp raises on a
    # path that was never fetched.
    if not _recipe(model_id):
        raise RuntimeError(_NOT_CURATED.format(model_id=model_id))

    from llama_cpp import Llama

    llm = Llama(
        model_path=gguf_path,
        n_ctx=_N_CTX,
        n_threads=os.cpu_count() or 4,
        verbose=False,
    )
    _loaded["llm"] = llm
    # Always "cpu" — see the module docstring. Still set through the same
    # field every other runner reports through (`worker_base.STATE["device"]`),
    # because a page reading that field must not need a special case for the
    # one engine that never varies.
    worker_base.set_state(device="cpu")


def memory():
    """None — RSS alone is the honest answer here.

    llama.cpp `mmap`s the GGUF by default (`use_mmap=True`), and unlike a CUDA
    or MPS allocator's pool there is no second accounting system to ask: pages
    that are actually touched during inference are counted in this process's
    resident set the same way `torch_text`'s CPU case is (see that module's
    `memory` docstring). Returning None rather than 0 tells `worker_base` there
    is nothing beyond RSS to add, not that the answer is zero.
    """
    return None


# ------------------------------------------------------------------ generation


def _chat_template(llm):
    """The GGUF's own embedded jinja2 chat template, or None.

    Read from `Llama.metadata` — populated straight from the model file's
    key-value store at load time — rather than from any file on disk, because
    for the repos this runner curates there IS no file on disk beside the
    GGUF: see the module docstring's note on that.
    """
    metadata = getattr(llm, "metadata", None) or {}
    template = metadata.get("tokenizer.chat_template")
    return template if isinstance(template, str) else None


def _render_chat(template_str, llm, messages):
    """The model's own chat template, with reasoning OFF by default.

    See the module docstring for why `enable_thinking=False` needs no retry
    here the way `torch_text._apply_template` does: a template that never
    reads the variable simply never sees it, where transformers'
    `apply_chat_template` can raise on an unexpected keyword.
    """
    from jinja2 import Environment

    # `trim_blocks`/`lstrip_blocks`: the same whitespace convention
    # transformers' own Jinja sandbox uses for chat templates, so a template
    # written against that convention (which is all of them — this is the
    # convention the ecosystem settled on) renders identically here.
    env = Environment(trim_blocks=True, lstrip_blocks=True)
    template = env.from_string(template_str)
    bos_id = llm.token_bos()
    eos_id = llm.token_eos()
    bos_token = llm.token_get_text(bos_id) if bos_id != -1 else ""
    eos_token = llm.token_get_text(eos_id) if eos_id != -1 else ""
    return template.render(
        messages=messages, add_generation_prompt=True,
        bos_token=bos_token, eos_token=eos_token, enable_thinking=False)


def _prompt_text(llm, messages, raw_prompt):
    """The text to hand `create_completion`: raw, templated, or a plain join.

    Three paths, same order `torch_text._encode` tries them in: an explicit
    `raw` prompt always wins, a model that carries a chat template renders
    through it, and a model with neither falls back to a plain concatenation
    of message bodies rather than inventing turn markers the model never saw
    in training.
    """
    if raw_prompt:
        return raw_prompt
    template_str = _chat_template(llm)
    if template_str:
        try:
            return _render_chat(template_str, llm, messages)
        except Exception:  # noqa: BLE001 - a bad template must not break the reply
            pass
    return "\n\n".join(
        m.get("content", "") for m in messages if isinstance(m, dict))


def _prompt_tokens(llm, prompt):
    """How many tokens the encoded prompt is, or None if this cannot say.

    Fail-soft like every other runner's count (SPEC AI-3): a tokenizer call
    that raises costs the metric, never the generation.
    """
    try:
        return len(llm.tokenize(prompt.encode("utf-8"), add_bos=True))
    except Exception:  # noqa: BLE001 - a count may not break a generation
        return None


def generate(body, write):
    """Stream one completion as NDJSON: {chunk} lines, then {done}.

    No producer thread — see the module docstring. `create_completion`'s own
    generator IS the token loop, so this function reads it directly and checks
    `worker_base.CANCEL` between tokens, the same flag `torch_text`'s
    `StoppingCriteria` reads from a different thread.
    """
    llm = _loaded.get("llm")
    if llm is None:
        write({"type": "done", "ok": False, "error": "no model is loaded"})
        return

    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    prompt = _prompt_text(llm, messages, body.get("prompt") or "")
    # What the model READ, reported as `input_tokens` (SPEC AI-3) — counted
    # before the first token, so a cancelled generation still reports it.
    prompt_tokens = _prompt_tokens(llm, prompt)
    max_tokens = int(body.get("max_tokens") or 1024)
    temperature = float(body.get("temperature", 0.7))
    top_p = float(body.get("top_p", 0.95))

    completion = llm.create_completion(
        prompt, max_tokens=max_tokens, temperature=temperature, top_p=top_p,
        stream=True)

    count = 0
    started = time.time()
    for chunk in completion:
        if worker_base.CANCEL.is_set():
            break
        text = chunk["choices"][0]["text"]
        if not text:
            continue
        count += 1
        write({"type": "chunk", "text": text})

    if worker_base.CANCEL.is_set():
        write({"type": "done", "ok": True, "cancelled": True, "tokens": count,
               "input_tokens": prompt_tokens})
        return
    write({
        "type": "done", "ok": True, "tokens": count,
        "input_tokens": prompt_tokens,
        "seconds": round(time.time() - started, 2),
    })


def main():
    """Serve, forever. The entry point `llamacpp_text/worker.py` calls."""
    worker_base.serve(download=download, load=load, generate=generate,
                      streaming=True)
