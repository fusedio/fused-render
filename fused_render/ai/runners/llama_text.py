"""Text generation on llama.cpp / GGUF: one resident model, four routes (SPEC §40).

**This module is the whole of the runner and it sits at the runners ROOT**,
beside `worker_base.py`, `formats.py` and `torch_image.py`: TWO folders serve
this one engine — `llamacpp_text/` and `llamacpp_text_vulkan/` — and each holds
only a `pyproject.toml` and a five-line `worker.py` shell around `main()`
below. They differ in which wheel index their manifest takes
`llama-cpp-python` from, and the hardware that names is a fact about the wheel,
never about the code. (The pattern is `torch_image.py`'s and was the removed
`torch_text.py`'s, which served three such folders — D416.)

**The DEFAULT text engine on Windows and Linux since D416, and it was designed
not to be.** This module shipped registered below three `transformers-text*`
rows so that `auto` could never reach it, because `llamacpp_text/pyproject.toml`
records that the maintainer's wheel index is a coin-flip per release on macOS
arm64 (4 of 16 sampled releases fail an integrity check) and a capability that
fragile to INSTALL is a poor thing to hand a machine that did not ask for it.
D416 removed those rows on a benchmark this engine won on every axis at once
(4.2x transformers' throughput on a Radeon GPU, 2.4x on CPU, a third of the
download, a third of the peak RSS), so the packaging argument lost to a
performance one and the default moved. What kept it affordable: the pinned
`0.3.29` Linux and Windows wheels were verified intact, macOS arm64 still
resolves to `mlx-text` ahead of this row, and a corrupt wheel fails LOUDLY at
`uv sync` rather than answering wrongly later. `llamacpp_text/pyproject.toml`
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

**No longer true as of D412: a bare repo id `formats.GGUF_RECIPES` has never
heard of now resolves too**, through `_resolve_uncurated_repo` — the id still
supplies no filename, but this runner now HAS a rule for picking one out of
thirty (`formats.pick_gguf_file`, ranked by quantization suffix, small and
reliable first), rather than no rule at all. `GGUF_RECIPES` keeps its
original job — a hand-picked, `size_gb`-promised suggestion list, not the
only thing this engine can load — and `hub_models.py`'s search runs the SAME
picker over a result's own `siblings` before ever offering it, so a repo
that would load here is also one Hub search will surface (`Runner.hub_filter_tags`,
`registry.py`). `formats.COMPONENT_REPOS`'s repos remain the one thing this
paragraph used to describe that is STILL true of a different table: they
name a component swapped into an otherwise ordinary pipeline, not a whole
model a bare id could mean, so no picker generalizes them the way this one
generalizes `GGUF_RECIPES`.

**No external tokenizer/config download, and the reason is the FORMAT rather
than the repos.** The vocabulary, the architecture and (since llama.cpp's
chat template support landed) the chat template all live inside the ONE
file's own key-value metadata, which is exactly what `llama_cpp.Llama` reads
at load time into `.metadata`. So `download()` fetches exactly one file
(`worker_base.download_file`) and nothing else — there is no
`download_snapshot(..., allow_patterns=…)` call here, because a GGUF needs no
companion.

**This used to be argued the other way round — "those repos happen to ship
nothing but GGUFs" — and that argument expired.** It was true of the three
unsloth Qwen repos the table curated on 2026-08-21, whose only non-GGUF files
were `.gitattributes`, `README.md` and an imatrix calibration file. The
shortlist has since gained repos where it is plainly false:
`unsloth/gemma-4-E4B-it-GGUF` carries a root `config.json` and an `MTP/`
folder, and `LiquidAI/LFM2.5-1.2B-Instruct-GGUF` a `leap/` directory of
runtime manifests. None of it is fetched and none of it is missed, which is
the proof that the format was always doing the work. Stating it as a property
of the repos would have made a correct implementation look like a lucky one,
and would have argued against curating either of them.

Five things are true of this runner and of no other text runner here, and all
five are llama.cpp's doing. Three of them are stated as contrasts with the
transformers runner this app shipped until D416 (`torch_text.py`), because that
is the shape the difference has and the reasoning does not become wrong when
the other side of the comparison is deleted — only unvisitable:

* **GPU offload is decided by the LINKED BUILD, never by this module knowing
  which folder imported it.** `llamacpp_text/` and `llamacpp_text_vulkan/`
  install the SAME `llama-cpp-python==0.3.29` pin against different wheel
  indexes, and this module must not branch on which one — a Vulkan-specific
  `if` here would be a difference between the two folders no test could see,
  the same rule the module docstring states about growing a second line of
  behaviour anywhere else. `llama_cpp.llama_supports_gpu_offload()` answers
  the question honestly instead: it is a real llama.cpp C API (not a
  `verbose`-log inference), and reading its implementation at the vendored
  commit (`src/llama.cpp`) shows it asks ggml's OWN backend registry for a
  real `GPU` or `IGPU` device — `ggml_backend_dev_by_type(...) != nullptr` —
  which is false on a CPU-only build (no GPU backend `.so` even linked),
  false on a Vulkan build with the loader present but no ICD registered (the
  backend registers zero devices), and true on Apple Silicon's Metal-linked
  wheel and a Vulkan build with a working driver alike. So the SAME check
  gets Metal right on macOS for free, with no Apple-specific code, which is
  the whole reason this is one shared module and not three.
  `n_gpu_layers` defaults to `0` in `llama-cpp-python` — verified against
  0.3.29's own `Llama.__init__` signature — so leaving it unset was silently
  CPU-only even on a Vulkan install; `load()` below now asks first.
* **Offload is SIZED BY TRYING, because nothing in this binding can size it
  by CALCULATING.** llama.cpp does not check available VRAM before
  allocating a layer's GPU buffer — `llama-model.cpp` only clamps the
  requested count to the model's own total layer count
  (`n_gpu = std::min(n_gpu_layers, n_layer_all)`), never to what the device
  has free — and `llama_cpp.py`'s ctypes surface has no binding for
  `ggml_backend_dev_memory` or any other free-VRAM query, confirmed by
  reading the installed package. A buffer allocation that does not fit
  raises a catchable Python exception rather than aborting the process
  (`llama_model_load`'s own `try`/`catch` in `src/llama.cpp` converts it to a
  clean load failure, read at the vendored commit) — so `load()` exploits
  that: it reads the model's own layer count off its GGUF header
  (`formats.gguf_block_count`) and tries a shrinking sequence of offload
  counts, catching each failed attempt and trying fewer layers, down to `0`
  (pure CPU) as the guaranteed-to-work floor. A hard OOM that kills the Load
  button outright would be the worse failure mode for a 4-8GB laptop GPU
  asked to hold a model sized for a bigger one; a slower partial load is not.
* **The reported device now reflects what actually happened, not what this
  runner assumed.** `worker_base.set_state(device=...)` is `"cpu"`, `"gpu"`
  (every layer offloaded), or `"gpu (partial)"` (the backoff above landed on
  fewer than the model's own total) — a MEASUREMENT of which attempt
  succeeded, the same principle AI-11b already states about reporting a probed
  device rather than an assumed one. It cannot say "Vulkan" or "Metal" by name: nothing in the
  bound API reports which backend actually served the request, only whether
  a GPU-shaped device existed at all.
* **The chat template is rendered by hand, from the GGUF's own embedded jinja2
  source, because `create_completion(stream=True)` — not
  `create_chat_completion` — is what keeps the streaming contract identical to
  every other runner's NDJSON shape (`worker_base`).** `create_chat_completion`'s streaming
  reply is OpenAI-delta-shaped and would need reshaping back into this app's
  `{"type": "chunk"}` frames anyway, so rendering the prompt ourselves and
  calling the low-level completion API keeps one code path instead of two.
  `enable_thinking=False` is passed into the render context unconditionally,
  the same default the removed `torch_text._apply_template` chose and for the
  same reason (AI-11d): three of this runner's curated models are Qwen3.5
  GGUFs, whose upstream template defaults reasoning ON. Jinja simply ignores a
  context variable a template never references, so — unlike transformers'
  `apply_chat_template`, which can raise on an unexpected keyword — no retry
  is needed here.
* **Cancelling needs no thread.** transformers' `model.generate` owned its own
  loop, so a `StoppingCriteria` callback was the only interruption point and a
  producer thread was required to let `TextIteratorStreamer` hand tokens back
  to that process while generation ran. `Llama.create_completion(stream=True)`
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

#: What to say when a repo id curates MORE THAN ONE quantization and none of
#: them is on disk yet — the one case a bare repo id is genuinely ambiguous
#: rather than merely uncurated.
_AMBIGUOUS_REPO = (
    "{model_id!r} curates more than one quantization here ({ids}) and none of "
    "them is on this machine yet, so which one 'load' means is ambiguous — "
    "pick one of those ids instead of the bare repo id."
)

#: What to say when an uncurated repo's own file listing could not be read —
#: named apart from `_NO_GGUF_MATCH` because the two are different facts a
#: user can act on differently: this one means "try again", that one means
#: "this repo will never resolve".
_LOOKUP_FAILED = (
    "Could not read {model_id!r}'s file listing on the Hub ({error}) — an "
    "uncurated repo id resolves by reading which GGUF files it actually "
    "publishes, so a network or Hub problem here means the pick cannot be "
    "made right now."
)

#: What to say when an uncurated repo's listing WAS read but
#: `formats.pick_gguf_file` found nothing to choose — either no `.gguf` at
#: all (this was never a GGUF repo), or every candidate was excluded by
#: shape or format (see that function's docstring for exactly which).
_NO_GGUF_MATCH = (
    "{model_id!r} has no GGUF file this engine can pick as a chat model "
    "({count} file(s) checked) — files in subdirectories, multi-part shards, "
    "auxiliary weights (a projector, a speculative-decoding draft, or "
    "similar) and quantizations below Q4 are excluded, and nothing else "
    "matched a recognised quantization suffix."
)


def _recipes_for_repo(repo_id):
    """Every curated recipe whose repo is `repo_id`, keyed by their filename ids."""
    return {key: recipe for key, recipe in _GGUF_RECIPES.items()
            if recipe["repo"] == repo_id}


def _locally_cached_gguf_files(repo_id):
    """Root-level `.gguf` filenames `repo_id` already has ON DISK, with no
    network call — the local-cache-first fast path (D412) for resolving an
    UNCURATED repo, the same "answer the disk before asking the Hub" rule
    `worker_base._cached_file` already gives a curated recipe.

    Every snapshot directory hf's cache holds for this repo is scanned
    (usually one, `main`) rather than resolving a specific revision first,
    because this function's ONLY job is "what filenames exist", and a second
    hf call to resolve a ref before answering that would defeat the point of
    a local-only fast path. `entry.is_file()` follows the symlink hf's cache
    writes for a materialised file — a snapshot entry for a download still in
    flight is a symlink to a blob that does not exist yet, which this
    correctly reads as absent.

    Returns an empty list rather than raising for anything this cannot read;
    a fast path that fails is a fast path this function simply does not
    offer, not a reason to break resolution — `_resolve_uncurated_repo` falls
    through to the networked listing exactly as if the cache were empty.
    """
    folder = worker_base.repo_folder(repo_id)
    if not folder:
        return []
    names = set()
    try:
        with os.scandir(os.path.join(folder, "snapshots")) as entries:
            snapshot_dirs = [entry.path for entry in entries if entry.is_dir()]
    except OSError:
        return []
    for snapshot_dir in snapshot_dirs:
        try:
            with os.scandir(snapshot_dir) as entries:
                for entry in entries:
                    if (entry.name.lower().endswith(formats.GGUF_EXTENSION)
                            and entry.is_file()):
                        names.add(entry.name)
        except OSError:
            continue
    return sorted(names)


def _resolve_uncurated_repo(model_id):
    """`(key, recipe)` for a bare repo id `formats.GGUF_RECIPES` has never
    heard of — Piece 1 (D412): any Hub repo carrying a root-level GGUF is
    something `llama_cpp.Llama` can load, and the only reason this used to be
    refused outright is that this app had no rule for choosing WHICH of a
    repo's own quantizations a bare id should mean. `formats.pick_gguf_file`
    is that rule; this function is only the two ways of getting it a file
    list to run over, cheapest first.

    Local cache checked FIRST — see `_locally_cached_gguf_files` — so
    reloading a model already fully downloaded through this engine costs
    nothing and needs no network, exactly like a curated recipe's own
    cache-first check three lines up in `_resolve_model_id`. Only when the
    cache has nothing does this reach the Hub, and `list_repo_files` is
    everything this needs: filenames only, no per-file size metadata this
    picker never uses.

    The two ways this refuses are named apart because they are different
    facts about the id: `_LOOKUP_FAILED` means "ask again", `_NO_GGUF_MATCH`
    means "this repo will not resolve to anything, GGUF or otherwise".
    """
    local_files = _locally_cached_gguf_files(model_id)
    if local_files:
        chosen = formats.pick_gguf_file(local_files)
        if chosen:
            return model_id, {"repo": model_id, "file": chosen}

    import huggingface_hub

    try:
        filenames = huggingface_hub.list_repo_files(model_id)
    except Exception as error:  # noqa: BLE001 - a Hub lookup failure is a fact
                                 # about the id/network, not a bug in this runner
        raise RuntimeError(
            _LOOKUP_FAILED.format(model_id=model_id, error=error)) from error

    chosen = formats.pick_gguf_file(filenames)
    if chosen is None:
        raise RuntimeError(
            _NO_GGUF_MATCH.format(model_id=model_id, count=len(filenames)))
    return model_id, {"repo": model_id, "file": chosen}


def _resolve_model_id(model_id):
    """`(key, recipe)` for whatever `model_id` actually means, or raise.

    Three shapes reach here, because the page and this table disagree about
    what a model's ID is (see the module docstring): a curated FILENAME key,
    used unchanged; a bare REPO id this table already curates one or more
    recipes for — the shape the AI Models page's local cache scan hands back
    for a repo this runner already downloaded, since that scan is keyed by
    repo folder and knows nothing of this table's own keys; and, since D412,
    a bare repo id this table has NEVER heard of, resolved generically by
    `_resolve_uncurated_repo` rather than refused by name — the whole point
    of Piece 1: a curated recipe was never a limit llama.cpp itself imposed.

    A CURATED repo id resolves to whichever of ITS curated recipes is
    already on disk (`worker_base._cached_file` is a read-only lookup — it
    cannot start a download, so asking it here speculatively costs nothing
    and starts nothing), which is what makes the exact model a user just
    downloaded through this engine loadable again under the id the cache
    scan offers it by. A repo with exactly one curated recipe resolves to it
    even cold, since there is nothing to disambiguate. A repo with more than
    one and nothing cached yet is refused BY NAME by `_AMBIGUOUS_REPO`,
    rather than guessed at — a wrong guess here is not a `FileNotFoundError`,
    it is a multi-gigabyte download of the WRONG quantization.
    """
    if model_id in _GGUF_RECIPES:
        return model_id, _GGUF_RECIPES[model_id]

    candidates = _recipes_for_repo(model_id)
    if not candidates:
        return _resolve_uncurated_repo(model_id)

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


def _offload_schedule(total_layers):
    """Layer counts to try offloading, largest first, always ending in `0`.

    See the module docstring's "sized by trying, not calculating" note for
    why this exists at all. `total_layers` is the model's own layer count,
    read off its GGUF header by the caller (`formats.gguf_block_count`) —
    passed in rather than read here so `load()` reads the file's header
    exactly once. When it is known, the schedule steps down through roughly
    thirds of it (a shrinking sequence that reaches `0` in a bounded number
    of attempts regardless of how many layers the model has, rather than
    decrementing by ones), deduplicated and sorted so a small model's
    rounding never repeats a step. When it is `None` (the header could not be
    read), there is nothing to fraction against, so the schedule is the
    two-tier `(-1, 0)` — llama.cpp's own "all layers" sentinel, then pure CPU
    — which still gets a working fallback, just without an intermediate
    partial-offload step.
    """
    if not total_layers or total_layers <= 0:
        return (-1, 0)
    steps = sorted({total_layers, (total_layers * 2) // 3, total_layers // 3, 0},
                   reverse=True)
    return tuple(steps)


def load(model_id, gguf_path):
    """`gguf_path` is what `download` returned — the one `.gguf` file's path."""
    # The curation check comes first, before the heavy import: a model this
    # runner was never going to serve is a fact about the REQUEST, and importing
    # first would replace a clear refusal with whatever llama.cpp raises on a
    # path that was never fetched. (The rule was `torch_text.load`'s, which
    # checked its own format before importing transformers for the same
    # reason — removed at D416, the rule kept.)
    _resolve_model_id(model_id)

    import llama_cpp
    from llama_cpp import Llama

    # A real llama.cpp API asked at call time, not inferred from which folder
    # imported this module — see the module docstring's "decided by the
    # linked build" note. False on a CPU-only build (no GPU backend even
    # linked) and on a Vulkan build with no usable driver; true on Apple
    # Silicon's Metal-linked wheel and a Vulkan build with a working one.
    gpu_capable = bool(llama_cpp.llama_supports_gpu_offload())
    total_layers = formats.gguf_block_count(gguf_path) if gpu_capable else None
    schedule = _offload_schedule(total_layers) if gpu_capable else (0,)

    llm = None
    n_layers = 0
    for index, candidate in enumerate(schedule):
        is_last = index == len(schedule) - 1
        try:
            llm = Llama(
                model_path=gguf_path,
                n_ctx=_N_CTX,
                n_threads=os.cpu_count() or 4,
                n_gpu_layers=candidate,
                verbose=False,
            )
            n_layers = candidate
            break
        except Exception:  # noqa: BLE001 - this loop IS the VRAM-sizing probe
            if is_last:
                # No smaller candidate is left to try — including plain CPU
                # (`0`), which llama.cpp can always satisfy if the file and
                # its metadata are valid — so this is a REAL failure (a
                # corrupt download, an unreadable file) and must not be
                # swallowed the way a too-large GPU request is above.
                raise
            print(f"llamacpp-text: {candidate} GPU layers did not fit, "
                  f"retrying with fewer", file=sys.stderr)
    _loaded["llm"] = llm

    if n_layers == 0:
        device = "cpu"
    elif n_layers == -1 or (total_layers and n_layers >= total_layers):
        device = "gpu"
    else:
        device = "gpu (partial)"
    # Still set through the same field every other runner reports through
    # (`worker_base.STATE["device"]`), because a page reading that field must
    # not need a special case for this engine — only the VALUE is new, per
    # the module docstring's "reported device now reflects what actually
    # happened" note.
    worker_base.set_state(device=device)


def memory():
    """None — RSS alone is the honest answer here.

    llama.cpp `mmap`s the GGUF by default (`use_mmap=True`), and unlike a CUDA
    or MPS allocator's pool there is no second accounting system to ask: pages
    that are actually touched during inference are counted in this process's
    resident set already, the way any CPU-resident allocation is — there is no
    second allocator to interrogate the way `torch_image` must interrogate
    torch's Metal pool. Returning None rather than 0 tells `worker_base` there
    is nothing beyond RSS to add, not that the answer is zero.

    WIRED, not dead code: `main()` passes this to `worker_base.serve`, the
    same way `torch_image.main` passes its own — an unwired
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
    here, where the removed `torch_text._apply_template` needed one: a Jinja
    template that never reads the variable simply never sees it, where
    transformers' `apply_chat_template` can raise on an unexpected keyword.
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

    Three paths, in the order the removed `torch_text._encode` tried them
    (kept because the ORDER is the app's contract, not that runner's): an explicit
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
    `worker_base.CANCEL` between tokens — the same flag every other runner
    checks, read from this thread rather than from a producer one.
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
