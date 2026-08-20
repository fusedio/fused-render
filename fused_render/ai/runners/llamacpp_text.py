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
verbatim. `formats.GGUF_RECIPES` solves it the way `torch_image._GGUF_RECIPES`
solves the same shape of problem for FLUX's quantized transformer: the id is
an opaque, curated key (here, simply the GGUF's own filename — already
unique, already meaningful to a reader, and never parsed for structure)
mapped to the `(repo, file)` it actually downloads.

**The table lives in `formats.py`, not here — a second reader needs it.** The
AI Models page enumerates the local Hub cache by REPO id (that is what a
`models--org--repo` folder is keyed by), so a repo this runner already
downloaded through one of its curated ids is discoverable and offered a Load
button under its BARE REPO ID, never under the filename this table's own
entries are keyed by. Both the page (deciding whether a curated entry is
already "downloaded", and refusing to show it a second time as an
undifferentiated cached row) and this worker (resolving a repo id BACK to the
one recipe that fetched it, `_resolve_model_id` below) need the same mapping,
and the page runs in a process that cannot import this venv — the identical
reason `formats.COMPONENT_REPOS` lives there rather than inside the runner
that reads it.

**The consequence is deliberate and documented rather than fixed**: Hub
search on the Discover tab cannot offer a model this runner would load,
because typing a bare repo id into that search box supplies no filename and
this runner has no rule for picking one out of thirty — only the ids in
`formats.GGUF_RECIPES` (the ones `catalog.SUGGESTIONS` also lists) ever load,
UNLESS the repo is already cached under one of them, which `_resolve_model_id`
below is what makes a bare repo id resolve anyway once that is true. That
mirrors `formats.COMPONENT_REPOS`, whose repos are likewise not addressable by
a user typing on the Discover tab.

**No external tokenizer/config download, and that is a fact about THESE
repos, not a general rule.** unsloth's GGUF conversions — the ones
`formats.GGUF_RECIPES` curates — ship no `tokenizer_config.json` or
`config.json` at their root: checked directly against the Hub API on
2026-08-21, the only non-GGUF files in `unsloth/Qwen3.5-4B-GGUF` and
`unsloth/Qwen3.5-9B-GGUF` are `.gitattributes`, `README.md` and an imatrix
calibration file, none of them a tokenizer. That is not an oversight on
unsloth's part — it is the point of the GGUF format: the vocabulary, the
architecture and (since llama.cpp's chat template support landed) the chat
template all live inside the ONE file's own key-value metadata, which is
exactly what `llama_cpp.Llama` reads at load time into `.metadata`. So
`download()` fetches exactly one file (`worker_base.download_file`) and
nothing else — there is no `download_snapshot(..., allow_patterns=…)` call
here, because there is nothing at those repos' roots for it to fetch.

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

**`llm._model.token_get_text` and `llm._model.add_bos_token`, not the public
`Llama` surface — verified against the installed 0.3.29, not assumed.**
`Llama` itself has `token_bos`/`token_eos`/`tokenize`/`detokenize` and no
`token_get_text` at all; the vocabulary lookup and the "does this model
auto-add BOS" flag live on the internal `LlamaModel` at `Llama._model`, which
is exactly where `Llama.__init__` itself reads them when it builds its OWN
`bos_token`/`eos_token` strings for `create_chat_completion`'s chat-format
table (`llama_cpp/llama.py`, the block that populates `self._chat_handlers`).
Leading underscore or not, this is upstream's own canonical path for this
exact question, not a private implementation detail this module is guessing
its way into.

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

import formats  # noqa: E402 - the shared format checks and GGUF_RECIPES; see formats.py
import worker_base  # noqa: E402 - the path insert above is what makes it importable

#: The loaded `Llama` instance. One per process.
_loaded = {}

#: How much context this runner asks llama.cpp to allocate. 8192 rather than a
#: GGUF's own trained maximum (Qwen3.5 supports far more): this runner exists
#: to serve ordinary chat turns on a CPU, where a larger KV cache is memory and
#: prompt-processing time spent on headroom nobody asked for. Raising it is a
#: one-line change if a curated model's use case needs it.
_N_CTX = 8192

#: Curated `(repo, file)` pairs — see the module docstring for why this lives
#: in `formats.py` rather than here, and why the key is a filename rather than
#: a `repo:quant` grammar.
_GGUF_RECIPES = formats.GGUF_RECIPES

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

#: What to say when a repo id curates MORE THAN ONE quantization and none of
#: them is on disk yet — the one case a bare repo id is genuinely ambiguous
#: rather than merely uncurated.
_AMBIGUOUS_REPO = (
    "{model_id!r} curates more than one quantization here ({ids}) and none of "
    "them is on this machine yet, so which one 'load' means is ambiguous — "
    "pick one of those ids instead of the bare repo id."
)


def _recipes_for_repo(repo_id):
    """Every curated recipe whose repo is `repo_id`, keyed by their filename ids."""
    return {key: recipe for key, recipe in _GGUF_RECIPES.items()
            if recipe["repo"] == repo_id}


def _resolve_model_id(model_id):
    """`(key, recipe)` for whatever `model_id` actually means, or raise.

    Two shapes reach here, because the page and this table disagree about
    what a model's ID is (see the module docstring): a curated FILENAME key,
    used unchanged, and a bare REPO id — the shape the AI Models page's local
    cache scan hands back for a repo this runner already downloaded, since
    that scan is keyed by repo folder and knows nothing of this table's own
    keys.

    A repo id resolves to whichever of ITS curated recipes is already on
    disk (`worker_base._cached_file` is a read-only lookup — it cannot start
    a download, so asking it here speculatively costs nothing and starts
    nothing), which is what makes the exact model a user just downloaded
    through this engine loadable again under the id the cache scan offers it
    by. A repo with exactly one curated recipe resolves to it even cold,
    since there is nothing to disambiguate. A repo with more than one and
    nothing cached yet is refused BY NAME by `_AMBIGUOUS_REPO`, rather than
    guessed at — a wrong guess here is not a `FileNotFoundError`, it is a
    multi-gigabyte download of the WRONG quantization.
    """
    if model_id in _GGUF_RECIPES:
        return model_id, _GGUF_RECIPES[model_id]

    candidates = _recipes_for_repo(model_id)
    if not candidates:
        raise RuntimeError(_NOT_CURATED.format(model_id=model_id))

    for key, recipe in candidates.items():
        if worker_base._cached_file(recipe["repo"], recipe["file"]):
            return key, recipe

    if len(candidates) == 1:
        (key, recipe), = candidates.items()
        return key, recipe

    raise RuntimeError(_AMBIGUOUS_REPO.format(
        model_id=model_id, ids=", ".join(repr(k) for k in sorted(candidates))))


# --------------------------------------------------------------- model loading


def download(model_id):
    """The one GGUF file this model means — never the whole repo.

    A repo in `formats.GGUF_RECIPES` publishes many more files than the one
    curated here (`unsloth/Qwen3.5-9B-GGUF` alone is 147.81GB whole), so the
    ordinary "download the repo" a snapshot-based runner uses would be
    catastrophic here. `worker_base.download_file` is the one-file
    counterpart `torch_image.py` uses for its own quantized-transformer swap,
    and it is already progress-instrumented against that ONE file's size
    rather than the repo's.
    """
    _key, recipe = _resolve_model_id(model_id)
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
    _resolve_model_id(model_id)

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

    WIRED, not dead code: `main()` passes this to `worker_base.serve`, the
    same way `torch_text.main`/`torch_image.main` pass theirs — an unwired
    `memory()` would be silently ignored forever, including the day someone
    adds a real probe (llama.cpp's own KV-cache size, say) to this body.
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


def _token_text(llm, token_id):
    """A token id's own vocabulary text, or "" for -1 (no such token).

    `Llama._model.token_get_text`, not `Llama.token_get_text` — the public
    class has no such method (verified against the installed 0.3.29; see the
    module docstring). `!= -1` guard copied from `Llama.__init__` itself,
    which asks this exact question to build its OWN bos/eos strings.
    """
    return llm._model.token_get_text(token_id) if token_id != -1 else ""


def _bos_token_for_template(llm):
    """The `bos_token` string to hand the chat template, or "" to omit it.

    **Empty whenever `create_completion` will add BOS itself.**
    `Llama._create_completion` decides independently, every call, whether to
    prepend the real BOS token — keyed on `Llama._model.add_bos_token()` —
    regardless of what text this function hands it. A template that ALSO
    renders the literal `bos_token` string (many do, at the very start of the
    prompt: `{{ bos_token }}...`) would then put two BOS tokens in the
    sequence: one from the rendered text, one `create_completion` adds on
    top. Only a model that does NOT auto-add BOS needs the template to spell
    it out, so this asks the same flag `create_completion` will act on rather
    than assuming a policy.

    Fails toward OMITTING it: a model whose `add_bos_token` this cannot read
    is assumed to add its own, since a missing BOS the tokenizer would have
    supplied is one token of context lost, while a doubled one is a corrupted
    prompt — the two failure modes are not symmetric.
    """
    try:
        auto_added = bool(llm._model.add_bos_token())
    except Exception:  # noqa: BLE001 - see docstring: omit rather than double
        auto_added = True
    if auto_added:
        return ""
    return _token_text(llm, llm.token_bos())


def _eos_token_for_template(llm):
    """The `eos_token` string to hand the chat template — the same reasoning
    `_bos_token_for_template` applies, for the rarer model that auto-appends
    EOS the way BERT-shaped models do (`add_eos_token`)."""
    try:
        auto_added = bool(llm._model.add_eos_token())
    except Exception:  # noqa: BLE001
        auto_added = False
    if auto_added:
        return ""
    return _token_text(llm, llm.token_eos())


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
    return template.render(
        messages=messages, add_generation_prompt=True,
        bos_token=_bos_token_for_template(llm),
        eos_token=_eos_token_for_template(llm), enable_thinking=False)


def _content_text(content):
    """A message's `content` as plain text, for the no-template fallback join.

    `content` is USUALLY a string, but the wire format also admits `None`
    (an assistant turn with only a tool call, say) and a multimodal PARTS
    list (`[{"type": "text", "text": "…"}, {"type": "image_url", ...}]`) —
    `"\\n\\n".join(...)` raising `TypeError` on either was this fallback's
    only path once `_render_chat` stopped being the silent no-op finding 1
    made it, which made it the HOT path rather than a rare corner.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _prompt_text(llm, messages, raw_prompt):
    """The text to hand `create_completion`: raw, templated, or a plain join.

    Three paths, same order `torch_text._encode` tries them in: an explicit
    `raw` prompt always wins, a model that carries a chat template renders
    through it, and a model with neither falls back to a plain concatenation
    of message bodies rather than inventing turn markers the model never saw
    in training.

    **Only a template's OWN failure is caught.** `jinja2.exceptions.TemplateError`
    is a fact about the MODEL's template — malformed Jinja, a construct this
    sandbox refuses — and falling back rather than failing the whole reply is
    the right call for that. Anything else (an `AttributeError` from this
    module's own code reaching for a method that does not exist, say) is a
    BUG, and swallowing it here is exactly what let one ship: every call
    silently produced the plain-join fallback with no error anywhere, which
    read as "this model has no chat template" rather than "this file has a
    defect". Logged before falling through even for the caught case, because
    a template that stops applying is worth knowing about even when the
    reply still goes out.
    """
    if raw_prompt:
        return raw_prompt
    template_str = _chat_template(llm)
    if template_str:
        import jinja2.exceptions

        try:
            return _render_chat(template_str, llm, messages)
        except jinja2.exceptions.TemplateError as error:
            print(f"llamacpp-text: chat template failed to render, falling back "
                  f"to a plain join: {error}", file=sys.stderr)
    return "\n\n".join(_content_text(m.get("content")) for m in messages
                       if isinstance(m, dict))


def _prompt_tokens(llm, prompt):
    """How many tokens the encoded prompt is, or None if this cannot say.

    `add_bos` follows the SAME policy `create_completion` itself uses
    (`Llama._model.add_bos_token()`) rather than being hardcoded True — a
    fixed `add_bos=True` counted a token `create_completion` might not
    actually add, which is metric drift on every model whose GGUF turns its
    own auto-BOS off (see `_bos_token_for_template`, the same flag, read for
    the same reason).

    Fail-soft like every other runner's count (SPEC AI-3): a tokenizer call
    that raises costs the metric, never the generation.
    """
    try:
        add_bos = bool(llm._model.add_bos_token())
    except Exception:  # noqa: BLE001 - the common case; see _bos_token_for_template
        add_bos = True
    try:
        return len(llm.tokenize(prompt.encode("utf-8"), add_bos=add_bos))
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
                      streaming=True, memory=memory)
