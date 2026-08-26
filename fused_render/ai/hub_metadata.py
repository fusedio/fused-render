"""Hub `config.json` harvest, cached at ~/.fused-render/ai_hub_metadata.json
(SPEC AI-17d, D498).

**Why this exists.** `hub_cache.has_vision_tower` and every KV-cache/fit
computation in `fit.py` need a model's architecture facts — layer count,
attention-head geometry, whether it carries a vision tower — but the ONLY
place those facts live before a download is `config.json` on the Hub, and
`hub_cache.py`'s equivalent reader (`_read_json` over a cached snapshot) can
only see a repo that is already ON DISK. A Hub *search* result never has that:
the AI Models page has to answer "will this fit, does it take images" for a
repo the user has not pulled a single byte of yet. `config.json` is a few KB —
no weights — so fetching it ahead of a decision to download is cheap enough to
do for every row a search returns, unlike a `HEAD` on the weight files
themselves.

**One HTTP GET, cached with a TTL, following the `bench_store.py` /
`footprints.py` idiom**: a private `_path()` over `storage.home_dir()`,
`storage.read_json`/`storage.write_json` and nothing else, a corrupt or
missing file reads as "nothing cached" and never raises. Unlike those two
modules this store is NOT machine-scoped — a repo's `config.json` describes
the MODEL, not the machine that asked for it, so there is no
`_same_machine`-style identity check and a home directory carried onto a new
laptop keeps a warm cache rather than discarding it.

**13-day TTL**, matching the analogous cache in the study this module is
modelled on (see the module-level docstring in `AI-FIT-OVERHAUL-SPEC.md`) — a
`config.json` changes when a repo is re-published under the same id, which
happens on the order of weeks, not minutes; a shorter TTL would re-fetch on
every page load for no benefit, and a much longer one risks serving a stale
architecture across a repo's occasional in-place edit.

**A stale entry is served rather than discarded when the refetch itself
fails.** Network failure "degrades silently to no metadata" per spec, but that
rule is about a REPO WE HAVE NEVER SEEN — for one we have a two-week-old
reading of, going back to nothing on a transient DNS hiccup would regress a
page that used to answer into one that renders blank, which is worse than
serving a slightly-stale-but-still-correct answer. Only a repo with NO cached
entry and a failed first fetch reads as None.

**Never raises into a route.** Every network or parse failure — DNS, timeout,
a non-200, a truncated or non-JSON body, a repo id `urllib.parse.quote` cannot
encode — is caught here and answered as "no metadata", the same contract
`mirror.fetch_json` and `footprints.read` already keep for their own callers.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from fused_render.shell import storage

#: `huggingface.co/{repo}/resolve/main/config.json` — the same URL shape a
#: browser's "raw file" link uses, and (per AI-1's own text) a few KB, no
#: weights: fetching this for every row a Hub search returns is cheap in a way
#: fetching even one weight file would not be.
_CONFIG_URL = "https://huggingface.co/{repo}/resolve/main/config.json"

#: 13 days — see the module docstring for why this specific figure, not a
#: shorter or longer one.
TTL_SECONDS = 13 * 24 * 60 * 60

#: A `config.json` is a few KB. Anything wildly larger than this is not one —
#: refusing to read past it is the same defence `mirror.py`'s
#: `MAX_MANIFEST_BYTES` states for the same reason: an oversized response is
#: not "a bigger version of the thing we asked for", it is evidence the URL
#: served something else.
MAX_BYTES = 1024 * 1024

#: One request round trip. Slow enough to allow for the Hub under load, short
#: enough that a hung read cannot stall the catalog/search route it feeds —
#: mirrors `mirror.MANIFEST_TIMEOUT_S`'s reasoning for the same kind of call.
_TIMEOUT_S = 8.0

#: How many repos' harvested metadata are kept — the same reasoning
#: `footprints.MAX_MODELS` gives: a row is a few dozen bytes, and the bound
#: exists so a machine that has searched thousands of repos over its lifetime
#: does not grow this file without limit.
MAX_REPOS = 500

VERSION = 1

_UNREACHABLE = (urllib.error.URLError, OSError, ValueError, TimeoutError)

#: The subset of `config.json` this module harvests, and the key each lands
#: under in the returned dict — SPEC AI-1's own list. `head_dim` is read
#: verbatim when present; `fit.py`'s KV-cache term derives it from
#: `hidden_size / num_attention_heads` when it is absent, not this module —
#: harvesting stays a straight read of what the Hub published, never a guess.
_FIELDS = {
    "model_type": "modelType",
    "num_hidden_layers": "numHiddenLayers",
    "num_key_value_heads": "numKeyValueHeads",
    "num_attention_heads": "numAttentionHeads",
    "head_dim": "headDim",
    "hidden_size": "hiddenSize",
    "max_position_embeddings": "maxPositionEmbeddings",
}

#: Hybrid/Mamba configs (Jamba, Zamba, Nemotron-H, ...) expose which layers
#: are real attention vs. a state-space/Mamba block under one of these keys.
#: `fit.py`'s KV-cache term (AI-5) needs this list to count only the
#: attention layers — a flat `num_hidden_layers` overcounts a hybrid model's
#: KV cache by however many Mamba layers it has, which carry no KV cache at
#: all.
_LAYER_TYPE_KEYS = ("layer_types", "layers_block_type")


def _path() -> str:
    return os.path.join(storage.home_dir(), "ai_hub_metadata.json")


def _load() -> dict:
    """The store, always a usable shape — `{"repos": {...}}` — never raising
    and never `None`: unlike `footprints`, a missing file and a corrupt one
    are not meaningfully different outcomes for THIS store (there is no
    machine-identity question to fail), so both simply start empty."""
    data = storage.read_json(_path())
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return {"repos": {}}
    return {"repos": data["repos"]}


def _write(store: dict) -> None:
    storage.write_json(_path(), {"version": VERSION, "repos": store["repos"]})


def _bounded(repos: dict) -> dict:
    if len(repos) <= MAX_REPOS:
        return repos

    def _fetched_at(kv):
        value = kv[1]
        return value.get("fetchedAt", 0) if isinstance(value, dict) else 0

    ordered = sorted(repos.items(), key=_fetched_at)
    return dict(ordered[len(ordered) - MAX_REPOS:])


def _fetch_raw(repo_id: str) -> bytes | None:
    """The `config.json` body for `repo_id`, or None on ANY failure — the
    module's one network seam, monkeypatched by every test so the rest of
    this module is exercised without a socket."""
    url = _CONFIG_URL.format(repo=urllib.parse.quote(repo_id, safe="/"))
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            raw = response.read(MAX_BYTES + 1)
    except _UNREACHABLE:
        return None
    if len(raw) > MAX_BYTES:
        return None
    return raw


def _harvest(config: dict) -> dict:
    """`config.json`'s body, narrowed to the fields this module promises —
    every one of them present in the returned dict, `None` where the source
    config did not declare it. Never raises: a value of the wrong shape
    (a string where a number was expected) reads as absent rather than
    propagating a `TypeError` into the route this feeds."""
    def _get(key):
        value = config.get(key)
        return value if isinstance(value, (int, float, str)) and not isinstance(value, bool) else None

    architectures = config.get("architectures")
    architecture = architectures[0] if isinstance(architectures, list) and architectures \
        and isinstance(architectures[0], str) else None

    quant_config = config.get("quantization_config")
    quant_method = quant_config.get("quant_method") if isinstance(quant_config, dict) else None
    if not isinstance(quant_method, str):
        quant_method = None

    layer_types = None
    for key in _LAYER_TYPE_KEYS:
        value = config.get(key)
        if isinstance(value, list) and value:
            layer_types = value
            break

    harvested = {v: _get(k) for k, v in _FIELDS.items()}
    harvested["architecture"] = architecture
    harvested["quantMethod"] = quant_method
    harvested["hasVisionTower"] = "vision_config" in config or "image_token_id" in config
    harvested["layerTypes"] = layer_types
    return harvested


def get(repo_id: str, *, force: bool = False) -> dict | None:
    """The harvested metadata for `repo_id` — from cache when fresh, fetched
    and cached when stale or absent, or None when nothing is known and a
    fetch could not answer either.

    `force=True` bypasses the TTL (not the cache-on-failure fallback) — for a
    future "re-check this model" action; nothing in this build calls it yet.
    """
    store = _load()
    entry = store["repos"].get(repo_id)
    fresh = (isinstance(entry, dict) and not force
              and time.time() - entry.get("fetchedAt", 0) < TTL_SECONDS)
    if fresh:
        return entry.get("meta")

    try:
        raw = _fetch_raw(repo_id)
    except Exception:  # noqa: BLE001 - `_fetch_raw` is the mockable seam; a
        # test (or a future caller) raising OUT of it, rather than returning
        # None, must still degrade silently — this route must never 500 off
        # a network failure, regardless of which layer the failure surfaces at.
        raw = None
    if raw is None:
        # Fetch failed (or was never attempted because this repo id cannot be
        # encoded into a URL) — fall back to a stale-but-real prior reading
        # rather than manufacturing "no metadata" out of a transient failure.
        # See the module docstring.
        return entry.get("meta") if isinstance(entry, dict) else None

    try:
        config = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return entry.get("meta") if isinstance(entry, dict) else None
    if not isinstance(config, dict):
        return entry.get("meta") if isinstance(entry, dict) else None

    meta = _harvest(config)
    store["repos"][repo_id] = {"meta": meta, "fetchedAt": time.time()}
    store["repos"] = _bounded(store["repos"])
    _write(store)
    return meta


def clear() -> None:
    """Forget every harvested repo — mirrors `footprints.clear`/`bench_store.
    clear`'s shape for the same reason: a caller wiping AI state should not
    have to enumerate this store's keys first."""
    _write({"repos": {}})
