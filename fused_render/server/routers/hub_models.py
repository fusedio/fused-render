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
  (D401).
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
from fused_render.ai.registry import capability_for_task
from fused_render.server.common import _error, _require_fused
from fused_render.server.routers.ai_models import (
    _FRIENDLIER_TAGS,
    _TASK_HELP,
    _entry_is_dir,
    _revisions,
    _scan_repo,
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
_EXPAND = (
    "pipeline_tag", "downloads", "likes", "lastModified", "createdAt",
    "library_name", "gated", "private", "tags", "safetensors",
)

# Sorts the page offers. Keyed so a client cannot pass an arbitrary sort field
# through to the Hub.
_SORTS = {
    "downloads": ("downloads", -1),
    "likes": ("likes", -1),
    "updated": ("lastModified", -1),
    "created": ("createdAt", -1),
}

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

# The tags a filter menu COULD offer: the Hub's own `pipeline_tag` values —
# the far side has to recognise a filter for it to return anything — ordered the
# way someone scanning a menu would read them: text, then vision, then audio.
#
# Which of them the page actually offers is not decided here (see
# `supported_tags`). This list is the vocabulary; the registry is the authority
# on what can be run, and keeping the two apart is what stops a new runner
# needing an edit in this module to become searchable.
_CANDIDATE_TAGS = (
    "text-generation",
    "text2text-generation",
    "image-text-to-text",
    "summarization",
    "translation",
    "question-answering",
    "text-classification",
    "token-classification",
    "zero-shot-classification",
    "fill-mask",
    "feature-extraction",
    "sentence-similarity",
    "text-to-image",
    "image-to-image",
    "image-to-text",
    "image-classification",
    "zero-shot-image-classification",
    "image-segmentation",
    "object-detection",
    "depth-estimation",
    "text-to-video",
    "automatic-speech-recognition",
    "text-to-speech",
    "text-to-audio",
    "audio-classification",
    "any-to-any",
)

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
    # Speech stacks that are not the three this app has: a `.nemo` archive is
    # the case worth naming, since `parakeet-mlx` reads the MLX CONVERSION of
    # one (`library_name: "mlx"`) and never the archive itself.
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
    resolution of its own (D401). The same credential decides this search and
    every model download, and a download resolves it by calling hf inside the
    worker; a second copy of the order here is how a page comes to report itself
    authenticated while the download beside it goes out anonymous. Read per
    request, so a login applies to the next search with no restart — and so that
    an OAuth token hf refreshed in place is the one that gets sent."""
    from fused_render.server.routers import hf_auth

    return hf_auth.token()


def _friendly_task(tag) -> str | None:
    """The Hub's `pipeline_tag` in the words the rest of the app uses. Same
    table the local scan uses, so one model reads the same on both tabs."""
    if not isinstance(tag, str) or not tag:
        return None
    return _FRIENDLIER_TAGS.get(tag, tag.replace("-", " "))


def supported_tags() -> tuple[str, ...]:
    """The Hub pipeline tags this app can download AND run, in menu order.

    Asked of the registry rather than listed here, and that is the whole point
    of the split above. `capability_for_task` is the SAME function that decides
    whether a repo already on this disk gets a Load button, so a search result
    and a downloaded card cannot disagree about whether a kind of model is
    runnable — which they would the moment two hand-maintained lists drifted,
    and the drift would be invisible until a user downloaded 8GB of something
    that then refused to load.

    It follows that adding a runner for, say, text-to-speech makes that filter
    appear here with no edit to this module, and that removing one makes it
    vanish. `tests/test_hub_models.py` pins both directions.
    """
    return tuple(t for t in _CANDIDATE_TAGS if capability_for_task(_friendly_task(t)))


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
    pull leaves a repo folder holding blobs and no materialised snapshot, and
    calling that "downloaded" would send someone to a model that cannot load.
    A repo with at least one snapshot has a revision something can open, which
    is the line this draws.
    """
    if dirname is None:
        return {"state": "none"}
    repo_dir = os.path.join(cache_dir, dirname)
    scan = _scan_repo(repo_dir)
    return {
        "state": "downloaded" if _revisions(repo_dir) else "partial",
        "size": scan.size,
        "files": scan.files,
        # Newest atime — "last read", the same measure the cached tab shows.
        "lastUsed": scan.atime or None,
        # Canonicalized like every other fs path the frontend gets, so it can go
        # straight to navigate(path, {isDir: true}).
        "path": canonical_fs_path(repo_dir),
        "dir": dirname,
    }


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


def _model_row(raw: dict, cache_dir: str, dirs: dict[str, str]) -> dict | None:
    """One Hub result, joined to the local cache — or None for a row this app
    has no business offering.

    **Four ways to be dropped, and they are the search's whole contract**
    (D313, narrowed by D316). A row that reaches the page comes with a Download
    button or with the one sentence that says what to do first, so every one of
    these is the difference between an actionable card and one that apologises:

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
    """
    model_id = raw.get("id") or raw.get("modelId")
    if not isinstance(model_id, str) or not model_id:
        return None
    if raw.get("private"):
        return None
    task = _friendly_task(raw.get("pipeline_tag"))
    capability = capability_for_task(task)
    if capability is None:
        return None
    library = raw.get("library_name") if isinstance(raw.get("library_name"), str) else None
    if library and library.lower() in _UNRUNNABLE_LIBRARIES:
        return None
    safetensors = raw.get("safetensors")
    return {
        "id": model_id,
        "task": task,
        # The same sentence the local cards show on hover, so a task means the
        # same thing on both tabs or it means nothing.
        "taskHelp": _TASK_HELP.get(task),
        "pipelineTag": raw.get("pipeline_tag"),
        # Never null, by the drop rule above — it is what the page hands to
        # `POST /api/ai/runtime/download`, which needs to know which runner is
        # being asked for.
        "capability": capability,
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
        "params": _params(safetensors),
        "estimatedSize": _estimated_bytes(safetensors),
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
    if sort not in _SORTS:
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

    sort_field, direction = _SORTS[sort]
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
    if task_filter:
        params["filter"] = task_filter

    key = (hub_endpoint(), query, task_filter, sort, count, bool(_token()))
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
    # `_model_row` is also the supported-tag filter (see its docstring): a row
    # this app could not download and run comes back None and never reaches the
    # page. Truncation is AFTER that pass, so `limit` means "rows you will be
    # shown" rather than "rows the Hub was asked for".
    models = [row
              for row in (_model_row(r, cache_dir, dirs)
                          for r in payload["raw"] if isinstance(r, dict))
              if row is not None][:count]
    return {
        "models": models,
        "query": {"q": query, "task": task_filter, "sort": sort, "limit": count},
        "endpoint": hub_endpoint(),
        "authenticated": bool(_token()),
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
    tasks = []
    for tag in supported_tags():
        label = _friendly_task(tag)
        tasks.append({"tag": tag, "label": label, "help": _TASK_HELP.get(label)})
    return {"tasks": tasks}
