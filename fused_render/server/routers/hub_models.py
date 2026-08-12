"""POST /api/ai-models/hub/search — models on the Hugging Face Hub, told apart
from the ones already on this disk.

The AI Models page (§37) answers "what did I already download". This answers the
other half — "what is there" — and the two are only useful *together*: the Hub
does not know your disk, and a browser tab open on huggingface.co cannot tell
you that the model you are looking at is already sitting in your cache, was last
read three weeks ago, and would cost nothing to open. Every result here is
cross-referenced against the local scan before it is returned, so a card can say
**downloaded**, **partly downloaded**, or **not downloaded, ~7.3 GB**.

**Read-only, and that is the whole feature.** Nothing here downloads a model,
writes to the cache, or mutates anything. Downloading is a separate decision with
a separate cost (gigabytes of someone's disk) and is deliberately not part of
this module.

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

The TTL cache exists to be a good citizen: search-as-you-type would otherwise
put one request per keystroke on a public API. Identical queries inside the
window are answered from memory.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Body, Header

from fused_render._view_url_codec import canonical_fs_path
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

# The task filters the page offers. These are the Hub's OWN `pipeline_tag`
# values — the far side has to recognise a filter for it to return anything —
# ordered the way someone scanning a menu would read them: text, then vision,
# then audio. Every one of them resolves through the shared glossary to a label
# with a sentence, which `tests/test_hub_models.py` pins.
_FILTER_TAGS = (
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
    limit. Read from the same places `huggingface_hub` reads it; NEVER returned
    to the client, and never logged."""
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    home = os.environ.get("HF_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface")
    try:
        with open(os.path.join(home, "token"), encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError:
        return None


def _friendly_task(tag) -> str | None:
    """The Hub's `pipeline_tag` in the words the rest of the app uses. Same
    table the local scan uses, so one model reads the same on both tabs."""
    if not isinstance(tag, str) or not tag:
        return None
    return _FRIENDLIER_TAGS.get(tag, tag.replace("-", " "))


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


def _model_row(raw: dict, cache_dir: str, dirs: dict[str, str]) -> dict | None:
    """One Hub result, joined to the local cache. None for anything without an
    id — a row the page could not act on is a row it should not be given."""
    model_id = raw.get("id") or raw.get("modelId")
    if not isinstance(model_id, str) or not model_id:
        return None
    task = _friendly_task(raw.get("pipeline_tag"))
    tags = raw.get("tags")
    safetensors = raw.get("safetensors")
    return {
        "id": model_id,
        "task": task,
        # The same sentence the local cards show on hover, so a task means the
        # same thing on both tabs or it means nothing.
        "taskHelp": _TASK_HELP.get(task) if task else None,
        "pipelineTag": raw.get("pipeline_tag") if isinstance(raw.get("pipeline_tag"), str) else None,
        "library": raw.get("library_name") if isinstance(raw.get("library_name"), str) else None,
        "downloads": raw.get("downloads") if isinstance(raw.get("downloads"), int) else None,
        "likes": raw.get("likes") if isinstance(raw.get("likes"), int) else None,
        "updated": raw.get("lastModified") if isinstance(raw.get("lastModified"), str) else None,
        # `gated` is "auto"/"manual"/False on the Hub — anything truthy means
        # the licence has to be accepted before a download would work, which is
        # worth saying BEFORE someone tries.
        "gated": bool(raw.get("gated")),
        "private": bool(raw.get("private")),
        "tags": [t for t in tags if isinstance(t, str)][:12] if isinstance(tags, list) else [],
        "params": _params(safetensors),
        "estimatedSize": _estimated_bytes(safetensors),
        "local": _local_state(cache_dir, dirs.get(model_id)),
        "url": f"{hub_endpoint()}/{model_id}",
    }


def _fetch(params: dict) -> tuple[list, str | None]:
    """The Hub's model list for one query: (rows, error). Never raises — an
    unreachable Hub is a sentence on the page, not a 500 from this server."""
    url = f"{hub_endpoint()}/api/models?{urlencode(params, doseq=True)}"
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
        return [], f"Could not reach {urlsplit(hub_endpoint()).netloc}: {e.__class__.__name__}"
    if response.status_code == 401 or response.status_code == 403:
        return [], ("The Hub refused this request. A private or gated search needs a token — "
                    "set HF_TOKEN, or log in with the Hugging Face CLI.")
    if response.status_code == 429:
        return [], "The Hub is rate-limiting this machine. Try again in a minute."
    if response.status_code >= 400:
        return [], f"The Hub answered {response.status_code}."
    try:
        payload = response.json()
    except ValueError:
        return [], "The Hub sent something that is not JSON."
    if not isinstance(payload, list):
        return [], "The Hub sent an unexpected reply."
    return payload, None


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
    try:
        count = 24 if limit is None else max(1, min(int(limit), _MAX_LIMIT))
    except (TypeError, ValueError):
        return _error("limit must be a number", status=400)

    sort_field, direction = _SORTS[sort]
    params: dict[str, object] = {
        "sort": sort_field,
        "direction": direction,
        "limit": count,
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
    models = [row
              for row in (_model_row(r, cache_dir, dirs)
                          for r in payload["raw"] if isinstance(r, dict))
              if row is not None]
    return {
        "models": models,
        "query": {"q": query, "task": task_filter, "sort": sort, "limit": count},
        "endpoint": hub_endpoint(),
        "authenticated": bool(_token()),
    }


@router.get("/api/ai-models/hub/tasks")
def api_hub_tasks():
    """The task filters the page offers: the Hub's tag, our label for it, and
    the sentence explaining what it means.

    The TAGS are listed here because they are Hub vocabulary and this is the
    module that talks to the Hub — a filter is only useful if the far side
    recognises it. The LABEL and the sentence come from the shared glossary, so
    a filter named "text generation" here means exactly what a downloaded model
    labelled "text generation" means on the other tab.

    Deriving the tags by reversing the glossary is the tempting version and it
    is wrong: several labels there ("image generation", "video generation",
    "audio generation") are our READING of a diffusers pipeline or an
    architecture suffix, not tags anyone publishes under, and a filter built
    from one would quietly return nothing.
    """
    tasks = []
    for tag in _FILTER_TAGS:
        label = _friendly_task(tag)
        tasks.append({"tag": tag, "label": label, "help": _TASK_HELP.get(label)})
    return {"tasks": tasks}
