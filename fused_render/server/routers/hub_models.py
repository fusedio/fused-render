"""/api/ai-models/hub/* — models on the Hugging Face Hub that this machine can
actually run, told apart from the ones already on this disk.

The AI Models page (§37) answers "what did I already download". This answers the
other half — "what is there" — and the two are only useful *together*: the Hub
does not know your disk, and a browser tab open on huggingface.co cannot tell
you that the model you are looking at is already sitting in your cache, was last
read three weeks ago, and would cost nothing to open. Every result here is
cross-referenced against the local scan before it is returned, so a card can say
**downloaded**, **partly downloaded**, or **not downloaded, ~7.3 GB**.

**Every result is RUNNABLE HERE, and that constraint is the feature** (D313,
narrowed by D316). This search used to return whatever the Hub returned —
`sentence-transformers`, `bert-base-uncased`, a fill-mask model — over a page
that could load none of them, and it said so in a caption under the box:
"Search results are read-only". A browsing surface over tens of thousands of
repos in front of an app that runs about four kinds of model is not a feature
with a rough edge. So the constraint moved into the module: a row survives only
if

* its `pipeline_tag` maps, through the SAME table that decides whether a
  downloaded model gets a Load button (`registry.capability_for_task`), to a
  capability some registered runner serves. A repo with no pipeline tag at all
  is dropped too — we cannot promise something we cannot classify.
* it is not `private`. There is no step an ordinary account can take to reach
  one, so a card for it could never be actioned by the person reading it.
* its `library_name`, when it has one, is not a framework this app has no
  runner for at all. The tag above says what a model is FOR and is blind to
  what it is MADE OF, so a `text-to-image` repo of `.tflite` graphs passed it
  and arrived with a Download button in front of a load that could only fail.
  This is a NARROW test and stays narrow — see `_UNRUNNABLE_LIBRARIES`: it
  names formats with no path through any runner under any circumstances, and
  it is a denylist because the formats we DO read are open and
  community-labelled (one FLUX.2 klein loads from four different values of
  this field).

**The constraint is "an engine here can run it", not "nothing further is asked
of the user"** — that is D316's correction. Gated repos come BACK, carrying
their gate (`gated`: "auto" | "manual" | None), because a licence you accept by
signing in and clicking is a step the user can take, and several of the
best-known models on the Hub sit behind exactly one. The card says what is
needed instead of offering a button that 403s.

Every row therefore carries a non-null `capability`, which is exactly what the
page needs to hand to `POST /api/ai/runtime/download`. **The filter is by
CAPABILITY EXISTENCE, not by what resolves on this machine**: the Engines tab
lists all three capabilities on every platform, a runner that cannot run here
is a fact the download refuses with its own sentence, and making search results
depend on the host would mean the same query answered differently on two
machines for reasons neither user could see.

**Search is nevertheless a guarded POST, and the reason is worth stating.** The
app's rule is that reads are unguarded GETs (WF-5), because D36's protection is
the browser's own: a foreign page can fire a request but cannot read the reply.
That reasoning is about the RESPONSE. It says nothing about the REQUEST, and
search is the one read here that leaves the machine — it calls the Hub with the
user's token attached. Unguarded, a blind cross-origin GET could spend someone's
credential and their rate limit while learning nothing, which is a cost the
same-origin policy does not prevent. Rather than bolt a guard onto a GET and
leave a shape that contradicts the rule, the route takes the shape its effect
deserves: outward effect → POST → `X-Fused` (D36). `hub/tasks` remains a GET
beside it, because it is a static glossary and touches nothing.

**Why the server fetches, and not the page.** One place to hold the token, one
place to bound the timeout, one place to cache — and one place to audit what
this app sends to a third party. The page never talks to huggingface.co.

Three rules the outbound call follows:

* **The host is fixed.** Only the query string varies, so no request body can
  point this at another server. `HF_ENDPOINT` (the standard mirror override,
  which `huggingface_hub` honours) is the one exception: it comes from the
  user's own environment, and it is still checked to be an http(s) URL before it
  is used.
* **The token is hf's, not ours.** `hf_auth.token()` is `get_token()`, so this
  request carries whatever a `hf auth login` or the Preferences login button put
  in hf's own store — and nothing this app persists, because it persists none
  (D402).
* **The query is ENCODED, never concatenated.** `urlencode` builds it, so a
  search for `a&b=c` is a search, not a second parameter.
* **Every field is optional.** The Hub's list endpoint returns what it returns,
  and `expand[]` fields may be absent, renamed, or refused by an older
  deployment. Nothing here indexes blindly: a missing field is a field the card
  leaves out, never a 500.

**Sizes are estimates and say so.** `safetensors.parameters` is a dtype ->
count map, so the bytes are recovered by summing `count * bits / 8` — the same
arithmetic the model card does locally (HF-17), and the same `≈`. A repo
without safetensors metadata reports no size rather than a guessed one.

**And when there is no safetensors metadata, `hub/size` is the second ask —
one repo at a time, on purpose.** A GGUF, mflux or LoRA-only repo carries no
dtype map, so the arithmetic above has nothing to work from and the card shows
a dash, even though huggingface.co shows a real total on the model's own page.
That total is `usedStorage`, and it exists ONLY on the per-repo detail endpoint:
the list endpoint refuses `expand[]=usedStorage` with a 400 naming the fields it
does accept, so there is no way to fold it into the search. Getting it for a
page of results would therefore be one HTTP round trip PER ROW, on every
debounced keystroke — exactly the traffic the cache above exists to avoid. So it
is a separate route, and the page calls it lazily: only for a row that has no
estimate, and only once that card has actually scrolled into view. It is also a
DIFFERENT NUMBER and the page says so — the total of everything in the repo,
not the weights — because a tooltip claiming "computed from parameter counts"
over a figure that includes the tokenizer and three quantised copies would be a
sentence about work that never happened.

The TTL cache exists to be a good citizen: search-as-you-type would otherwise
put one request per keystroke on a public API. Identical queries inside the
window are answered from memory.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Body, Header

from fused_render._view_url_codec import canonical_fs_path
from fused_render.ai import fit, footprints, hw_detect, speed
from fused_render.ai import tasks as ai_tasks
from fused_render.ai.registry import TEXT_GENERATION, for_capability
from fused_render.ai.runners import formats
from fused_render.server.common import _error, _require_fused
from fused_render.ai.hub_cache import (
    _entry_is_dir,
    _scan_repo,
    _unfinished_fetch,
    hub_cache_dir,
)

router = APIRouter()

_DEFAULT_ENDPOINT = "https://huggingface.co"

# Long enough that a burst of typing costs one request, short enough that a
# model published this morning shows up this morning.
_CACHE_TTL_S = 90.0
_CACHE_MAX = 64

# The Hub is a third party on the far side of someone's home connection. A
# search that has not answered in this long is a search the page should be told
# about, not one it should keep waiting on.
_TIMEOUT_S = 12.0

# What a card can show. Anything the deployment does not know how to expand is
# simply absent from the reply (see the module docstring).
#
# **`siblings` (D412) is the one entry that is not for the card at all — it
# is for `_model_row`'s own GGUF resolution.** Confirmed directly against the
# live API rather than assumed: the bare list endpoint returns NO `siblings`
# at all, passing `full` at ANY value (including `full=false`) turns them on
# as a side effect, and `expand[]=siblings` composed with the rest of this
# tuple returns the repo's COMPLETE file list in the LIST response itself —
# no per-repo follow-up request. That is what makes resolving a GGUF search
# result at ROW-CONSTRUCTION time (`formats.pick_gguf_file`) cheap: the data
# this needs is already in the payload this module was fetching anyway.
_EXPAND = (
    "pipeline_tag", "downloads", "likes", "lastModified", "createdAt",
    "library_name", "gated", "private", "tags", "safetensors", "siblings",
)

# Sorts the page offers. Keyed so a client cannot pass an arbitrary sort field
# through to the Hub. `trending` -> `trendingScore` is verified live against the
# API. `fit` is deliberately NOT a key here — it is not a field the Hub has, so
# there is no wire value to map it to; `api_hub_search` special-cases it below,
# the same honesty `hubSearchView.ts` documents for its own page-only "size".
_SORTS = {
    "downloads": ("downloads", -1),
    "likes": ("likes", -1),
    "updated": ("lastModified", -1),
    "created": ("createdAt", -1),
    "trending": ("trendingScore", -1),
}

# The one sort value this endpoint accepts that is not in `_SORTS`: "fit" asks
# for a candidate set the Hub CAN rank (downloads — the same honest default
# `size` uses on the frontend) and reorders it here, over `fit.verdict`'s own
# `score`, after the per-request join. See `api_hub_search`.
_FIT_SORT = "fit"

_MAX_LIMIT = 60

# Longer than any real `org/name` and short enough that nothing hand-written
# turns into a long URL this server goes and fetches.
_MAX_ID_LEN = 200

# An unfiltered query is filtered HERE, so the Hub has to be asked for more rows
# than the page will show or a search for a common word would come back nearly
# empty after the supported-tag pass. Bounded, because this is somebody's home
# connection and a public API: four pages' worth, never more than _MAX_FETCH.
_OVERFETCH = 4
_MAX_FETCH = 200

# The tags a filter menu could offer are `ai/tasks.py`'s table, in its order —
# every `pipeline_tag` the Hub serves, vendored from `@huggingface/tasks`. This
# module used to keep its own hand-picked subset beside that; two lists of tags
# with different edit histories is exactly the drift `supported_tags` exists to
# prevent, and the local subset had already gone stale (it still offered
# `text2text-generation`, a tag the Hub retired and now returns nothing for).

# Bits per safetensors dtype, for turning a parameter count back into bytes.
# Deliberately the same table the model card uses on local files (HF-17): one
# model must not be 16GB on the Hub tab and 8GB on its card.
_DTYPE_BITS = {
    "U8": 8, "I8": 8, "F8_E4M3": 8, "F8_E5M2": 8,
    "U16": 16, "I16": 16, "F16": 16, "BF16": 16,
    "U32": 32, "I32": 32, "F32": 32,
    "U64": 64, "I64": 64, "F64": 64,
}

# Hub `library_name` values NOTHING here can ever open, and the reason this is a
# DENYLIST rather than an allowlist.
#
# The tag filter above asks "is this KIND of model runnable"; it cannot see the
# FORMAT, so `litert-community/FLUX.2-klein-4B-LiteRT` — a `text-to-image` repo
# of `.tflite` graphs — arrived with a Download button in front of a load that
# could only fail. `grep -rn litert fused_render/` finds nothing, and no
# quantization, platform or wheel would change that.
#
# An allowlist keyed to the libraries the runners are BUILT on (diffusers,
# transformers, mlx) is the tempting version and it is wrong, because it is not
# what the repos we load actually report. Read off the Hub, not guessed: the
# FLUX.2 klein family alone loads today from `diffusers`
# (black-forest-labs/FLUX.2-klein-4B), `ggml`
# (unsloth/FLUX.2-klein-4B-GGUF, the recipe's quantized transformer),
# `diffusion-single-file` (mlx-community/FLUX.2-Klein-4B-4bit, the one repo
# `MFLUX_VARIANTS` names) and `mflux` (Runpod/FLUX.2-klein-4B-mflux-4bit) —
# four values for one model — while Whisper adds `ctranslate2` and `mlx`. A
# three-name allowlist would hide most of what works. The set of formats we
# read is open and community-labelled; the set we provably cannot is small and
# nameable, so the small one is the one written down.
#
# **What this claims and what it does not.** It claims only that the named
# framework has no runner in this app AT ALL — not that a repo passing it will
# load. It deliberately does NOT try to predict quantization, file layout or
# anything host-specific: that is D316's line, and a diffusers-tagged repo with
# a bespoke quantization still gets through and still fails, which is a
# different and harder problem. When in doubt a value is LEFT OUT: a card that
# should not be there is the status quo, and hiding a repo that would have
# loaded is a regression this filter must not introduce.
_UNRUNNABLE_LIBRARIES = frozenset({
    # Graph formats for other runtimes entirely. No runner imports any of them.
    "litert", "tflite", "coreml", "onnx", "openvino", "unity-sentis",
    "keras", "tf-keras",
    # Speech stacks that are not the two this app has. `nemo` used to need a
    # caveat here — `parakeet-mlx` read the MLX CONVERSION of a NeMo export
    # (`library_name: "mlx"`), never the archive itself — but D406 withdrew
    # that runner, so a raw `.nemo` archive (`library_name: "nemo"`) is
    # unloadable with no exception now.
    #
    # **Known, accepted gap left by that withdrawal (D406):** an MLX
    # CONVERSION of a Parakeet checkpoint reports `library_name: "mlx"`, which
    # is a format this app DOES run other things on (MLX LM, MLX FLUX, MLX
    # Whisper) — so it is not in this denylist and cannot be, without also
    # hiding every repo those runners actually load. Such a repo still passes
    # `supported_tags()` and appears in Hub search with a Download button;
    # `formats.py`'s trap catches it only AFTER download, once `config.json`
    # is on disk to read `target` from — the card then shows no engine tag and
    # no Load button, same as any other unloadable cached repo, but the
    # Download button upstream of that is honest-looking and wrong. Fixing it
    # would need a per-repo config fetch inside search results, which this
    # filter deliberately does not do (see "What this claims" above).
    "nemo", "espnet", "speechbrain", "k2",
    # Classical NLP toolkits, which publish under supported pipeline tags.
    "spacy", "fasttext", "flair", "stanza", "allennlp", "sklearn", "paddlenlp",
})

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def hub_endpoint() -> str:
    """The Hub base URL. `HF_ENDPOINT` is the standard mirror override and is
    honoured, but only when it parses as an http(s) URL — an unset or malformed
    value falls back to the real Hub rather than being used."""
    raw = (os.environ.get("HF_ENDPOINT") or "").strip().rstrip("/")
    if not raw:
        return _DEFAULT_ENDPOINT
    parts = urlsplit(raw)
    if parts.scheme in ("http", "https") and parts.netloc:
        return raw
    return _DEFAULT_ENDPOINT


def _token() -> str | None:
    """The user's Hub token, if they have one, for gated repos and a higher rate
    limit. NEVER returned to the client, and never logged.

    `hf_auth.token()` — i.e. `huggingface_hub.get_token()` — rather than a
    resolution of its own (D402). The same credential decides this search and
    every model download, and a download resolves it by calling hf inside the
    worker; a second copy of the order here is how a page comes to report itself
    authenticated while the download beside it goes out anonymous. Read per
    request, so a login applies to the next search with no restart — and so that
    an OAuth token hf refreshed in place is the one that gets sent."""
    from fused_render.server.routers import hf_auth

    return hf_auth.token()


def supported_tags() -> tuple[str, ...]:
    """The Hub pipeline tags this app can download AND run, in menu order.

    Asked of `ai/tasks.py` rather than listed here, and that is the whole point
    of the split: it is the SAME table that decides whether a repo already on
    this disk gets a Load button, so a search result and a downloaded card
    cannot disagree about whether a kind of model is runnable — which they would
    the moment two hand-maintained lists drifted, and the drift would be
    invisible until a user downloaded 8GB of something that then refused to
    load.

    It follows that adding a runner for, say, text-to-speech makes that filter
    appear here with no edit to this module, and that removing one makes it
    vanish. `tests/test_hub_models.py` pins both directions.
    """
    return ai_tasks.supported_tags()


def _estimated_bytes(safetensors) -> int | None:
    """Bytes on disk, recovered from the dtype -> parameter-count map. None when
    the repo carries no safetensors metadata: a size we cannot compute is left
    out, never guessed from the parameter count alone."""
    if not isinstance(safetensors, dict):
        return None
    by_dtype = safetensors.get("parameters")
    if not isinstance(by_dtype, dict):
        return None
    total = 0
    for dtype, count in by_dtype.items():
        bits = _DTYPE_BITS.get(str(dtype).upper())
        if bits and isinstance(count, int) and count >= 0:
            total += count * bits // 8
    return total or None


def _params(safetensors) -> int | None:
    if not isinstance(safetensors, dict):
        return None
    total = safetensors.get("total")
    if isinstance(total, int) and total > 0:
        return total
    by_dtype = safetensors.get("parameters")
    if isinstance(by_dtype, dict):
        counted = sum(v for v in by_dtype.values() if isinstance(v, int) and v >= 0)
        return counted or None
    return None


def _cached_dirs() -> dict[str, str]:
    """Repo id (`org/name`) -> cache folder name, for MODEL repos.

    One `scandir` of the cache root and nothing else — no `stat`, no walk, no
    reading of anyone's metadata. This runs on every search (HS-5: what is on
    this disk is never served stale), so it has to cost about nothing on a cache
    holding hundreds of repos.
    """
    dirs = {}
    try:
        entries = list(os.scandir(hub_cache_dir()))
    except OSError:
        return dirs
    for entry in entries:
        if not entry.name.startswith("models--"):
            continue  # datasets/spaces/.locks — this search is models only
        if not _entry_is_dir(entry):
            continue
        dirs["/".join(entry.name.split("--")[1:])] = entry.name
    return dirs


def _local_state(cache_dir: str, dirname: str | None) -> dict:
    """How ONE Hub result stands on this disk.

    Measuring is per RESULT, not per cached repo: a page shows at most a couple
    of dozen rows, and of those only the ones actually present cost a walk. The
    AI Models listing would answer this too, but it also reads every repo's
    model card, config and safetensors headers to say what each model is FOR —
    work no row here needs, and work a debounced keystroke must not pay for
    across an entire cache.

    `partial` is a real state, not a rounding of `downloaded`: an interrupted
    pull leaves bytes behind, and calling those "downloaded" would send someone
    to a model that cannot load.

    **The line is `_unfinished_fetch`, not "has at least one snapshot" (D424).**
    A snapshot directory is materialised FILE BY FILE — our own fetcher links
    each blob as it lands — so a repo whose first small file arrived before the
    user pressed ✕ had a revision, had no weights, and read as "downloaded"
    here. What answers honestly is the residue of the stopped fetch itself, and
    that reading lives in `ai_models` beside the listing's own, so this tab and
    that page cannot disagree about one folder.
    """
    if dirname is None:
        return {"state": "none"}
    repo_dir = os.path.join(cache_dir, dirname)
    scan = _scan_repo(repo_dir)
    return {
        "state": "partial" if _unfinished_fetch(repo_dir) else "downloaded",
        "size": scan.size,
        "files": scan.files,
        # Newest atime — "last read", the same measure the cached tab shows.
        "lastUsed": scan.atime or None,
        # Canonicalized like every other fs path the frontend gets, so it can go
        # straight to navigate(path, {isDir: true}).
        "path": canonical_fs_path(repo_dir),
        "dir": dirname,
    }


#: `base_model:<relation>:<id>` — the Hub's own tag naming what a repo was
#: derived from. `relation` is free text on the Hub's side, but the four
#: values every republish here actually uses are `quantized`, `finetune`,
#: `merge` and `adapter`; the parse itself does not narrow to that set — a
#: value the Hub adds later still groups, it would just group under a
#: relation label the frontend has not written a name for yet.
_BASE_MODEL_TAG_PREFIX = "base_model:"


def _base_model(tags) -> tuple[str | None, str | None]:
    """`(baseModel, relation)` parsed off a repo's own `tags`, or `(None,
    None)` when none of them says what this was derived from — a row
    standing alone, or a repo whose tags this server could not read at all
    (missing, not a list, or entries that are not strings).

    **Parsing only. The grouping RULE is the frontend's** (`hubFamilies.ts`)
    — this function's whole job is turning the Hub's own colon-delimited tag
    into two fields, never deciding which rows share a family or which one
    leads it. Mirrors `_gate`'s own shape: never absent from the row, so "no
    base" and "the Hub did not say" would be one field if this ever had a
    reason to conflate them — it does not, both read as `None` today, but the
    shape is deliberate rather than incidental.

    The FIRST matching tag wins where more than one exists (a repo cannot
    have two base models this table would agree on, and the Hub does not
    document what a second one would mean), and the base model id itself may
    contain colons in principle (an org or repo name never does on today's
    Hub, but nothing here assumes otherwise) — `partition`, not `split`, so
    only the first two colons are consumed and the id is whatever remains.
    """
    if not isinstance(tags, list):
        return None, None
    for tag in tags:
        if not isinstance(tag, str) or not tag.startswith(_BASE_MODEL_TAG_PREFIX):
            continue
        rest = tag[len(_BASE_MODEL_TAG_PREFIX):]
        relation, sep, base_id = rest.partition(":")
        if not sep or not relation or not base_id:
            continue
        return base_id, relation
    return None, None


def _gate(raw) -> str | None:
    """The Hub's `gated` field as one of None / "auto" / "manual".

    The Hub sends `False` for an open repo and the two strings for a gated one.
    Anything else truthy is read as "manual": that is the stricter of the two
    gates, and telling somebody a gate opens by signing in when it does not is
    worse than telling them to go and look.
    """
    if not raw:
        return None
    return "auto" if raw == "auto" else "manual"


def _model_row(raw: dict, cache_dir: str, dirs: dict[str, str],
               footprint_store: dict | None, hardware) -> dict | None:
    """One Hub result, joined to the local cache — or None for a row this app
    has no business offering.

    **Five ways to be dropped, and they are the search's whole contract**
    (D313, narrowed by D316, widened by D412). A row that reaches the page
    comes with a Download button or with the one sentence that says what to
    do first, so every one of these is the difference between an actionable
    card and one that apologises:

    * no id — a row the page could not act on at all.
    * a `pipeline_tag` no registered runner serves, or none at all. The tag is
      classified by `capability_for_task`, the same function the Local tab's
      Load button asks, so "searchable" and "loadable" cannot come apart.
    * `private` — visible only because this machine happens to hold a token
      that can see it. There is no step an ordinary account can take to reach
      one: no licence to accept, no queue to join, so a card for it could never
      be actioned by the person reading it.
    * a `library_name` in `_UNRUNNABLE_LIBRARIES` — the right KIND of model in
      a format nothing here reads. The tag says what a repo is FOR and cannot
      see what it is MADE OF, which is how a `.tflite` FLUX got a Download
      button; see that set for why it is a denylist and, importantly, for the
      much smaller thing it claims. A MISSING or non-string `library_name` is
      not a drop: the Hub often does not set it, and silence about the format
      is not evidence against it — only an explicit unrunnable value counts,
      the same way `_gate` reads only what the Hub actually said.
    * (D412) the capability's ACTIVE runner here declares a format tag
      (`Runner.hub_filter_tags`) and `formats.pick_gguf_file` finds nothing
      loadable among the repo's own `siblings` — see below for why this is
      the one drop reason that depends on the resolved runner rather than on
      capability existence alone, and is therefore the one exception to this
      module's "search does not depend on the host" rule.

    **`gated` is NOT a drop, and the distinction is the point** (D316). It was
    one, on the rule that every card must be downloadable — a rule drawn one
    step too tight. A gate you open by signing in and accepting a licence is
    not a repo nobody can have; several of the best-known models on the Hub sit
    behind exactly that, and a search that silently omitted them was answering
    a question nobody asked. The gate TRAVELS instead (`gated`: "auto",
    "manual" or None), so the card can say what is needed rather than offering
    a button that 403s. `manual` — the owner grants access by hand — is the one
    case that needs more than logging in, and the Hub does tell us, so it stays
    its own value; a truthy gate we do not recognise is read as `manual`, the
    stricter reading, because guessing "just sign in" about an unknown gate is
    the guess that wastes someone's afternoon.

    **Why the fifth drop is allowed to depend on the resolved runner, when
    every other check here is capability-only (D412).** `llamacpp-text`'s
    GGUF format and `mlx-text`'s safetensors are the first time one capability
    has had two genuinely different on-disk formats behind it — every earlier
    multi-runner capability's variants
    share a format, so `_UNRUNNABLE_LIBRARIES` never had to choose between
    them. A search result this machine's ACTIVE text-generation engine
    cannot resolve at all (a safetensors repo, while llamacpp is the engine
    in force) is not actionable HERE regardless of what a different engine
    elsewhere could do with it — the identical argument
    `_UNRUNNABLE_LIBRARIES` already makes about FORMAT, only now decided per
    machine because the format itself varies per machine. Two people running
    the same query see different results only after making different,
    VISIBLE choices in Preferences, never for a reason neither could see —
    which is what keeps this inside the spirit of "not by what resolves on
    this machine", even though it reads the resolved runner to answer it.
    """
    model_id = raw.get("id") or raw.get("modelId")
    if not isinstance(model_id, str) or not model_id:
        return None
    if raw.get("private"):
        return None
    reading = ai_tasks.classify(raw.get("pipeline_tag"))
    capability = reading.capability
    if capability is None:
        # HS-0: everything on this tab is runnable HERE. A ruled-out task and an
        # unrecognised one are both dropped, and now for stateable reasons —
        # `reading.support` says which, for a future face that wants to show
        # rather than hide them.
        return None
    task = reading.label
    library = raw.get("library_name") if isinstance(raw.get("library_name"), str) else None
    if library and library.lower() in _UNRUNNABLE_LIBRARIES:
        return None
    file = None
    runner = for_capability(capability)
    if runner is not None and "gguf" in runner.hub_filter_tags:
        # The one runner-specific branch in this module (see the docstring's
        # last section) — `pick_gguf_file` is a GGUF-specific function, and
        # `hub_filter_tags` names the FILTER TAG generically but not the
        # picker that goes with it, since llama.cpp's is the only format
        # that needs one today. A future second format-specific runner would
        # need its own branch here, not a new item in `hub_filter_tags`.
        siblings = raw.get("siblings")
        names = ([s.get("rfilename") for s in siblings if isinstance(s, dict)]
                 if isinstance(siblings, list) else [])
        file = formats.pick_gguf_file(names)
        if file is None:
            return None
    safetensors = raw.get("safetensors")
    params = _params(safetensors)
    estimated_size = _estimated_bytes(safetensors)
    # A real, safetensors-derived byte total is strictly better evidence than
    # `fit`'s own `params x bytes-per-param` guess — that guess is what
    # `fit._weight_bytes` falls back to only when `quantization` is a
    # recognized display string, which a Hub search row never carries (that
    # field is catalog-only). So `quantization` stays None here always, and
    # `size_gb` carries the computed total whenever one exists.
    size_gb = (estimated_size / fit.GB_BYTES) if estimated_size else None
    fit_verdict = fit.verdict(capability, model_id, size_gb, params=params,
                              footprint_store=footprint_store, hardware=hardware)
    speed_estimate = (
        speed.estimate_tok_s(size_gb, params=params, hardware=hardware)
        if capability == TEXT_GENERATION else None)
    created = raw.get("createdAt") if isinstance(raw.get("createdAt"), str) else None
    base_model, relation = _base_model(raw.get("tags"))
    return {
        "id": model_id,
        # What this repo was derived from, and how — parsed off the Hub's own
        # `base_model:<relation>:<id>` tag, or (None, None) for a row standing
        # alone or one whose tags this server could not read. The GROUPING
        # rule that turns this into one row per family is the frontend's
        # (`hubFamilies.ts`) — this is the raw fact, not the judgement.
        "baseModel": base_model,
        "relation": relation,
        # {verdict, basis, footprintBytes, score, runMode} or None — the same
        # judgement `ai_runtime.describe_catalog` computes for a downloaded
        # model, over the SAME `fit.verdict` this app already trusts, so a
        # Hub row and a local card cannot disagree about what "fits" means.
        "fit": fit_verdict,
        # {tokensPerSecond, method, backend, bandwidthGbS, contextTokens,
        # calibrated, calibrationFactor} or None — text-generation only, the
        # same restriction `ai_runtime.describe_catalog` applies, because the
        # unit means nothing for the other three capabilities.
        "speedEstimate": speed_estimate,
        # ISO8601 or None. Already in `_EXPAND` and thrown away before this —
        # the field the "New" sort orders by but the page never drew.
        "created": created,
        "task": task,
        # The same sentence the local cards show on hover, so a task means the
        # same thing on both tabs or it means nothing.
        "taskHelp": ai_tasks.help_for(reading.tag),
        "pipelineTag": raw.get("pipeline_tag"),
        # Never null, by the drop rule above — it is what the page hands to
        # `POST /api/ai/runtime/download`, which needs to know which runner is
        # being asked for.
        "capability": capability,
        # The ONE file `pick_gguf_file` chose, for a GGUF row — None for
        # every other row. Carried so `POST /api/ai/runtime/download` can act
        # without re-deriving the pick from `siblings` a second time; the id
        # stays a bare repo id regardless (no `repo:file` grammar — see
        # `llama_text.py`'s own docstring for why one was rejected).
        "file": file,
        # None, "auto" or "manual" — never absent and never False, so the page
        # tests one field for "is there a gate and what kind". A missing key
        # would make "no gate" and "the Hub did not say" the same answer.
        "gated": _gate(raw.get("gated")),
        # Whatever the Hub said, minus the values dropped above — so the badge
        # on a card and the reason a card exists read off the same field.
        "library": library,
        "downloads": raw.get("downloads") if isinstance(raw.get("downloads"), int) else None,
        "likes": raw.get("likes") if isinstance(raw.get("likes"), int) else None,
        "updated": raw.get("lastModified") if isinstance(raw.get("lastModified"), str) else None,
        "params": params,
        "estimatedSize": estimated_size,
        "local": _local_state(cache_dir, dirs.get(model_id)),
        "url": f"{hub_endpoint()}/{model_id}",
    }


def _get(url: str) -> tuple[httpx.Response | None, str | None]:
    """One authenticated GET at the Hub: (response, error). Never raises — an
    unreachable Hub is a sentence on the page, not a 500 from this server.

    Shared by the two outbound calls this module makes, so a rate limit or a
    refused token reads the same whether the page was searching or asking one
    repo how big it is.
    """
    headers = {"Accept": "application/json"}
    token = _token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(url, headers=headers, timeout=_TIMEOUT_S,
                             follow_redirects=True)
    except httpx.HTTPError as e:
        # Offline, DNS, TLS, timeout — all the same to the person looking at the
        # page, and all worth naming rather than spinning forever.
        return None, f"Could not reach {urlsplit(hub_endpoint()).netloc}: {e.__class__.__name__}"
    if response.status_code == 401 or response.status_code == 403:
        return None, ("The Hub refused this request. A private or gated repo needs a token — "
                      "sign in to Hugging Face in Preferences → AI, or set HF_TOKEN.")
    if response.status_code == 429:
        return None, "The Hub is rate-limiting this machine. Try again in a minute."
    if response.status_code >= 400:
        return None, f"The Hub answered {response.status_code}."
    return response, None


def _fetch(params: dict) -> tuple[list, str | None]:
    """The Hub's model list for one query: (rows, error)."""
    response, error = _get(f"{hub_endpoint()}/api/models?{urlencode(params, doseq=True)}")
    if error or response is None:
        return [], error
    try:
        payload = response.json()
    except ValueError:
        return [], "The Hub sent something that is not JSON."
    if not isinstance(payload, list):
        return [], "The Hub sent an unexpected reply."
    return payload, None


def _fetch_used_storage(model_id: str) -> tuple[int | None, str | None]:
    """One repo's total bytes on the Hub: (usedStorage, error).

    The DETAIL endpoint, which answers with an object rather than the list
    `_fetch` reads — and it is the only endpoint that will expand this field at
    all (see the module docstring). The id is quoted into the PATH with its
    slash intact and nothing else surviving, so a repo name carrying a `?`
    cannot become a second query parameter.

    Anything that is not a plain non-negative int is no answer: a string of
    digits, a float, a `True`. This route's only job is the total, so a repo the
    Hub does not measure reports None rather than falling back to the dtype map
    the search already tried.
    """
    url = (f"{hub_endpoint()}/api/models/{quote(model_id, safe='/')}"
           f"?{urlencode({'expand[]': 'usedStorage'})}")
    response, error = _get(url)
    if error or response is None:
        return None, error
    try:
        payload = response.json()
    except ValueError:
        return None, "The Hub sent something that is not JSON."
    if not isinstance(payload, dict):
        return None, "The Hub sent an unexpected reply."
    used = payload.get("usedStorage")
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        return None, None
    return used, None


def _cached(key: tuple):
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return hit[1]
    return None


def _store(key: tuple, value: dict) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            # Oldest first — this is a politeness cache, not a correctness one,
            # so the cheapest eviction that bounds memory is the right one.
            for stale, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[: _CACHE_MAX // 2]:
                _cache.pop(stale, None)
        _cache[key] = (time.monotonic(), value)


@router.post("/api/ai-models/hub/search")
def api_hub_search(body: dict = Body(default={}), x_fused: str | None = Header(default=None)):
    """Hub models matching a query, each told apart from the local cache.

    **A POST, and guarded, even though it reads nothing on this machine.** Every
    other read in the app is an unguarded GET (WF-5), because D36's protection
    is the browser's: a foreign page can fire the request but cannot read the
    reply. That argument covers the RESPONSE and says nothing about the REQUEST,
    and this is the one read that leaves the machine — it makes an outbound call
    to the Hub carrying the user's token. A blind cross-origin GET could
    therefore spend someone's credential and their rate limit without ever
    seeing an answer.

    So the shape follows the rule rather than the rule acquiring an exception:
    a request with an outward effect is a POST, and POSTs carry `X-Fused` (D36).
    `hub/tasks` stays a GET beside it — it is a static glossary and touches
    nothing. Still NOT authentication (D3 stands); it only forces a preflight
    that a cross-origin caller cannot satisfy.

    Sync `def` on purpose: it makes one bounded outbound request and one cache
    walk, so FastAPI runs it in the threadpool rather than parking the event
    loop behind someone else's network.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    q, task = body.get("q"), body.get("task")
    sort = body.get("sort") or "downloads"
    limit = body.get("limit")
    query = (q or "").strip()[:120] if isinstance(q, str) else ""
    task_filter = (task or "").strip()[:60] if isinstance(task, str) else ""
    include_unfit = bool(body.get("includeUnfit"))
    if sort not in _SORTS and sort != _FIT_SORT:
        return _error(f"unknown sort {sort!r}", status=400)
    # A task nothing here can run is refused rather than searched for. The menu
    # only offers supported tags, so reaching this is either a stale page or a
    # hand-written request — and answering it with an empty grid would look
    # like "the Hub has no summarization models" rather than "this app does not
    # run them".
    if task_filter and task_filter not in supported_tags():
        return _error(f"nothing here runs {task_filter!r}", status=400)
    try:
        count = 24 if limit is None else max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        return _error("limit must be a number", status=400)

    # "fit" is not a Hub field: the candidate set the Hub is asked for is the
    # same honest default `size` uses on the frontend — most-downloaded — and
    # this route reorders it below, over `fit.verdict`'s own score, once every
    # row's fit is known.
    sort_field, direction = _SORTS[sort] if sort in _SORTS else _SORTS["downloads"]
    # With a task filter the Hub already returns only rows we keep, so asking
    # for `count` is asking for what will be shown. WITHOUT one, the
    # supported-tag pass runs here and throws most of a page away — a search for
    # "small" sorted by downloads is mostly embedding models — so the request
    # over-fetches and the reply is truncated after filtering.
    fetch = count if task_filter else min(count * _OVERFETCH, _MAX_FETCH)
    params: dict[str, object] = {
        "sort": sort_field,
        "direction": direction,
        "limit": fetch,
        "expand[]": list(_EXPAND),
    }
    if query:
        params["search"] = query
    # (D412) When a task filter is set AND the runner actually serving that
    # capability HERE declares a format tag (`Runner.hub_filter_tags`), the
    # Hub is asked to AND it onto the pipeline-tag filter already sent —
    # confirmed live that multiple `filter=` values are ANDed, and the Hub
    # request already uses `urlencode(..., doseq=True)` below, so a list
    # value here is not a new parameter shape. Without a task filter there is
    # no single capability to resolve a runner for (a bare keyword search
    # spans every supported tag at once), so this narrowing is skipped and
    # `_model_row`'s own per-row check is the only gate — the same
    # two-layer shape the pipeline-tag filter itself already has.
    extra_tags: tuple[str, ...] = ()
    if task_filter:
        filter_capability = ai_tasks.capability_for_tag(task_filter)
        filter_runner = for_capability(filter_capability) if filter_capability else None
        if filter_runner is not None:
            extra_tags = filter_runner.hub_filter_tags
        params["filter"] = [task_filter, *extra_tags] if extra_tags else task_filter

    # `extra_tags` is part of the cache key because it is part of the
    # ANSWER: a preference switched live (CT-5, no restart needed) changes
    # which runner serves the capability and therefore what this narrows to,
    # so the SAME query/task/sort/count must not be served from a cache
    # entry built under a different engine choice.
    key = (hub_endpoint(), query, task_filter, sort, count, bool(_token()), extra_tags)
    payload = _cached(key)
    if payload is None:
        rows, error = _fetch(params)
        if error:
            # Not a 5xx: the request was fine, the far side was not, and the
            # page has a sentence to show for it.
            return {"models": [], "error": error, "query": {
                "q": query, "task": task_filter, "sort": sort, "limit": count}}
        payload = {"raw": rows}
        _store(key, payload)

    # The JOIN is deliberately outside the cache: the Hub's answer is stable for
    # the TTL, but what is on this disk changes the moment someone deletes a
    # model, and a stale "downloaded" badge would send them to a folder that is
    # no longer there. It can afford to run every time because it is scoped to
    # the rows being returned — one scandir of the cache root, then a measure of
    # only the handful of repos that turned out to be present.
    cache_dir = hub_cache_dir()
    dirs = _cached_dirs()
    # Read ONCE per request, exactly like `ai_runtime.py:906-923` — both are a
    # `storage.read_json` open plus a parse (`hw_detect.cached_hardware()` also
    # re-checks machine identity), and this join answers as many rows as the
    # Hub sent back before truncation. `_model_row` threads both straight
    # through to `fit.verdict`/`speed.estimate_tok_s` rather than letting
    # either call resolve its own reading per row.
    footprint_store = footprints.load_store()
    hardware = hw_detect.cached_hardware()
    # `_model_row` is also the supported-tag filter (see its docstring): a row
    # this app could not download and run comes back None and never reaches the
    # page. Both the "cannot run here" drop below and the fit reorder run
    # BEFORE truncation, so `limit` keeps meaning "rows you will be shown"
    # rather than "rows the Hub was asked for".
    models = [row
              for row in (_model_row(r, cache_dir, dirs, footprint_store, hardware)
                          for r in payload["raw"] if isinstance(r, dict))
              if row is not None]

    # A `verdict: "no"` row is a fact about THIS MACHINE's memory, not about
    # how popular or well-classified the model is — dropped by default so a
    # search does not fill a page with models nothing here could hold, but
    # never silently: `hiddenUnfit` says how many, and `includeUnfit` asks for
    # them back.
    if include_unfit:
        hidden_unfit = 0
    else:
        kept, hidden = [], 0
        for row in models:
            if (row.get("fit") or {}).get("verdict") == "no":
                hidden += 1
            else:
                kept.append(row)
        models, hidden_unfit = kept, hidden

    if sort == _FIT_SORT:
        # Descending score, nulls (nothing to judge) sorted last — `sort` is
        # Python's own stable sort, so ties (including every null-fit row
        # among themselves) keep the Hub's own most-downloaded ordering as
        # the tie-break, the same guarantee `bySizeAscending` documents for
        # the frontend's own page-side sort.
        models.sort(key=lambda row: (row.get("fit") or {}).get("score", -1.0),
                    reverse=True)

    models = models[:count]
    return {
        "models": models,
        "query": {"q": query, "task": task_filter, "sort": sort, "limit": count},
        "endpoint": hub_endpoint(),
        "authenticated": bool(_token()),
        "hiddenUnfit": hidden_unfit,
    }


@router.post("/api/ai-models/hub/size")
def api_hub_size(body: dict = Body(default={}), x_fused: str | None = Header(default=None)):
    """One repo's TOTAL size on the Hub, for a card the dtype map could not
    measure.

    **Guarded POST for exactly the reason search is** — see `api_hub_search`:
    the cost of this request is in the REQUEST, not the reply, because it leaves
    the machine carrying the user's Hub token. Nothing about it being a "read"
    changes that, so it takes the same shape rather than the rule acquiring a
    second exception.

    **One repo per call, and the page asks lazily.** The Hub only expands
    `usedStorage` on the per-repo detail endpoint, so a page of two dozen
    results is two dozen round trips — which is why this is not folded into
    search and why the frontend calls it only for a row with no estimate whose
    card has actually scrolled into view (see the module docstring).

    The number is NOT the search's `estimatedSize` and must not be presented as
    one: it is everything in the repo — tokenizer, configs, every quantised copy
    the author published — rather than the weights a load would read.
    """
    guard = _require_fused(x_fused)
    if guard is not None:
        return guard
    model_id = body.get("id")
    if not isinstance(model_id, str):
        return _error("id must be a string", status=400)
    model_id = model_id.strip()
    # `org/name`, and nothing else becomes a request. The Hub's own shape, so a
    # malformed id is a client bug rather than a URL this server goes and fetches.
    parts = model_id.split("/")
    if len(parts) != 2 or not all(parts) or len(model_id) > _MAX_ID_LEN:
        # Echoed back so a caller can see WHICH id was refused, truncated so a
        # megabyte of junk in the body is not a megabyte of error message.
        return _error(f"{model_id[:80]!r} is not a repo id of the form org/name", status=400)

    key = ("size", hub_endpoint(), model_id, bool(_token()))
    payload = _cached(key)
    if payload is None:
        used, error = _fetch_used_storage(model_id)
        if error:
            # Not a 5xx, and not cached: the request was fine, the far side was
            # not, and the next card into view should find out for itself.
            return {"id": model_id, "usedStorage": None, "error": error}
        payload = {"usedStorage": used}
        _store(key, payload)
    return {"id": model_id, "usedStorage": payload["usedStorage"], "error": None}


@router.get("/api/ai-models/hub/tasks")
def api_hub_tasks():
    """The task filters the page offers: the Hub's tag, our label for it, and
    the sentence explaining what it means.

    **Only the ones something here can run** (D313). The menu used to list
    every tag the Hub recognises — twenty-six of them, of which this app could
    load four — so the filter that looked most like the point of the feature
    was mostly a list of ways to get results with no working button.

    The candidate TAGS are listed in this module because they are Hub
    vocabulary and this is the module that talks to the Hub — a filter is only
    useful if the far side recognises it. Which of them survives is
    `supported_tags`, i.e. the registry's answer. The LABEL and the sentence
    come from the shared glossary, so a filter named "text generation" here
    means exactly what a downloaded model labelled "text generation" means on
    the other tab.

    Deriving the tags by reversing the glossary is the tempting version and it
    is wrong: several labels there ("image generation", "video generation",
    "audio generation") are our READING of a diffusers pipeline or an
    architecture suffix, not tags anyone publishes under, and a filter built
    from one would quietly return nothing.
    """
    rows = []
    for tag in supported_tags():
        rows.append({"tag": tag,
                     "label": ai_tasks.label_for(tag),
                     "help": ai_tasks.help_for(tag)})
    return {"tasks": rows}
