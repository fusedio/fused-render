"""Known-quantizer GGUF counterpart resolution, cached at
`~/.fused-render/ai_gguf_sources.json` (SPEC AI-23, D526).

**Why this exists, and what it replaces.** A model this app curates for one
engine sometimes has no GGUF conversion on any curated list at all — the user
found it in Discover, or it is one of the handful of repos a llama.cpp-only
machine (Windows, Linux, D416) cannot read in its original safetensors form.
`catalog.py` used to carry `COUNTERPART_IDS`, but that table answers a
DIFFERENT question by construction — it is two written-by-hand rows mapping a
curated id to the SAME checkpoint in a different EXPORT FORMAT (ONNX), for
exactly the two ids the torch-runner removal orphaned, and its own docstring
disclaims any broader use ("two lines of data cannot [invent ids for every
other repo]"). It has never held a GGUF row and, per its own text, was never
meant to generalize to one — the audit that named it as this item's
replacement target was wrong about what it contains, not about the gap this
module fills. `gguf_sources` is new capability, not a migration: it answers
"which GGUF quantization of this repo, if any, does a known quantizer
publish" for ANY repo id, curated or not, which `COUNTERPART_IDS`'s
two-hand-written-rows shape could never scale to.

**Probing, not guessing.** Five accounts publish the overwhelming majority of
community GGUF conversions (`QUANTIZER_NAMESPACES`, most-active-first, per
this build's own spec): `unsloth`, `bartowski`, `ggml-org`, `TheBloke`,
`mradermacher`. For a source repo `org/name`, this module asks the Hub's
model-info API for `{provider}/name-GGUF` under each namespace in turn and
KEEPS every one that can be verified — never assumes a same-named repo under
a quantizer's account is actually a conversion of THIS model; two different
upstreams routinely publish same-named checkpoints.

**Verification, in priority order:**

1. The candidate's own `cardData.base_model` (or `base_model:quantized:` —
   HF's model-card schema uses the plain `base_model` key for both a
   fine-tune's base and a quantization's source, so this module checks one
   key) names the source repo, exactly — a string or a list, either form
   the Hub itself accepts.
2. Absent that tag, a PARAMETER-COUNT check: the candidate's own
   `safetensors.total` (the Hub's own parameter tally, when it can compute
   one for a GGUF repo) within **+/-30%** of the source's — passed in by the
   caller, since this module has no config.json reader of its own and
   `hub_metadata.get(repo_id)`'s harvest already exists for that.
3. **Neither present on either side is not a match.** Absence of evidence is
   not evidence of a match — an existence check alone (a 200 back from the
   model-info endpoint) would happily pair `unsloth/Llama-3-8B-GGUF` with
   some UNRELATED repo that also happens to be named `Llama-3-8B`, which is
   exactly the failure `catalog.py`'s own `COUNTERPART_IDS` docstring warns
   against for its own (different) table.

**Cached with a TTL, in the `bench_store.py`/`hub_metadata.py` store idiom**:
a private `_path()` over `storage.home_dir()`, `storage.read_json`/
`storage.write_json`, a corrupt or missing file reads as empty and never
raises, a bounded row count. Not machine-scoped for the same reason
`hub_metadata.py` is not — which GGUF conversions exist for a repo is a fact
about the MODEL, not the machine asking.

**Network failure degrades silently to "no sources", never raises into a
route** — the same contract `hub_metadata.get` and `mirror.fetch_json`
already keep, enforced here at the very outer edge of `sources_for` so a
`_fetch_model_info` override that raises (a monkeypatched test, or a future
caller wiring in a stricter HTTP client) cannot turn a probe of five
namespaces into a broken page.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from fused_render.shell import storage

#: `huggingface.co/api/models/{repo}` — the Hub's own model-info endpoint,
#: which carries `cardData` (the parsed YAML front-matter, including
#: `base_model`) and, for many repos, a `safetensors.total` parameter tally —
#: everything this module's two verification tiers need, in one request.
_MODEL_INFO_URL = "https://huggingface.co/api/models/{repo}"

#: Five accounts that publish the overwhelming majority of community GGUF
#: conversions, most-active-first — probed in this order so the FIRST
#: verified hit (the common case: exactly one quantizer has converted a given
#: model) is also the one most likely to be current and well-maintained.
#: Order is priority, not exhaustiveness: every verified candidate is kept
#: and returned, this only decides which is tried first.
QUANTIZER_NAMESPACES: tuple[str, ...] = (
    "unsloth", "bartowski", "ggml-org", "TheBloke", "mradermacher",
)

#: A candidate's parameter count must land within this fraction of the
#: source's to be accepted on param-similarity alone (SPEC AI-23's own
#: figure) — wide enough that ordinary rounding between a safetensors count
#: and a GGUF repo's own tally does not false-negative, narrow enough that an
#: unrelated same-named repo of a wildly different size cannot false-match.
PARAM_SIMILARITY_TOLERANCE = 0.30

#: 7 days — GGUF conversions of a popular model appear within days of a
#: release and rarely churn after that; short enough that a just-published
#: conversion is found on the next few searches, long enough that a search
#: page does not re-probe five namespaces per row on every load.
TTL_SECONDS = 7 * 24 * 60 * 60

#: A model-info response is a few KB of JSON; refusing to read past this is
#: the same defence `hub_metadata.MAX_BYTES` states for the same reason — an
#: oversized response is evidence the URL served something else.
MAX_BYTES = 1024 * 1024

_TIMEOUT_S = 8.0

#: Mirrors `hub_metadata.MAX_REPOS` — a row is a few dozen bytes, bounded so
#: a machine that has searched thousands of repos does not grow this file
#: without limit.
MAX_REPOS = 500

VERSION = 1

_UNREACHABLE = (urllib.error.URLError, OSError, ValueError, TimeoutError)


def _path() -> str:
    return os.path.join(storage.home_dir(), "ai_gguf_sources.json")


def _load() -> dict:
    data = storage.read_json(_path())
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return {"repos": {}}
    return {"repos": data["repos"]}


def _write(store: dict) -> None:
    storage.write_json(_path(), {"version": VERSION, "repos": store["repos"]})


def _fetched_at(entry) -> float:
    """`entry["fetchedAt"]` as a `float`, or `0.0` (the oldest possible
    reading) for anything that is not one — a non-dict row, a missing key,
    or a HAND-EDITED/truncated write that leaves a string or `None` where a
    timestamp belongs. `isinstance` rather than a bare `.get(..., 0)`
    (code review): the bare form let a corrupt `fetchedAt` reach
    `time.time() - ...` and raise `TypeError` straight out of `sources_for`,
    which this module's own docstring promises never happens. Booleans are
    excluded explicitly — `isinstance(True, int)` is `True` in Python, and a
    stray `"fetchedAt": true` must not be read as `1`."""
    if not isinstance(entry, dict):
        return 0.0
    value = entry.get("fetchedAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _bounded(repos: dict) -> dict:
    if len(repos) <= MAX_REPOS:
        return repos

    ordered = sorted(repos.items(), key=lambda kv: _fetched_at(kv[1]))
    return dict(ordered[len(ordered) - MAX_REPOS:])


def _fetch_model_info(repo_id: str) -> dict | None:
    """`{"cardData": {...}, "safetensors": {"total": N}, ...}` for `repo_id`,
    or None on ANY failure (including a 404 for a namespace/basename combo
    that does not exist) — the module's one network seam, monkeypatched by
    every test."""
    url = _MODEL_INFO_URL.format(repo=urllib.parse.quote(repo_id, safe="/"))
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            raw = response.read(MAX_BYTES + 1)
    except _UNREACHABLE:
        return None
    if len(raw) > MAX_BYTES:
        return None
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def _basename(repo_id: str) -> str:
    return repo_id.split("/")[-1] if "/" in repo_id else repo_id


def _base_model_names(info: dict) -> tuple[str, ...]:
    card = info.get("cardData")
    if not isinstance(card, dict):
        return ()
    value = card.get("base_model")
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(v for v in value if isinstance(v, str))
    return ()


def _param_total(info: dict) -> float | None:
    safetensors = info.get("safetensors")
    if not isinstance(safetensors, dict):
        return None
    total = safetensors.get("total")
    return float(total) if isinstance(total, (int, float)) and not isinstance(total, bool) else None


def _verified(source_repo: str, source_params: float | None, info: dict) -> bool:
    tags = _base_model_names(info)
    if tags:
        # An explicit `base_model` tag is the candidate's OWN claim about
        # its source, so it is decisive either way: naming a repo that is
        # not `source_repo` disqualifies the candidate outright, and must
        # not be rescued by a coincidental param-count match — the tag
        # override this branch would otherwise allow is exactly the
        # same-named-different-checkpoint failure this module exists to
        # avoid.
        return source_repo in tags
    candidate_params = _param_total(info)
    if source_params is None or candidate_params is None or source_params <= 0:
        return False
    return abs(candidate_params - source_params) / source_params <= PARAM_SIMILARITY_TOLERANCE


def _probe(repo_id: str, params: float | None) -> tuple[str, ...]:
    basename = _basename(repo_id)
    found = []
    for namespace in QUANTIZER_NAMESPACES:
        candidate = f"{namespace}/{basename}-GGUF"
        info = _fetch_model_info(candidate)
        if info is not None and _verified(repo_id, params, info):
            found.append(candidate)
    return tuple(found)


def sources_for(repo_id: str, *, params: float | None = None,
                force: bool = False) -> tuple[str, ...]:
    """Every verified GGUF-quantizer counterpart repo for `repo_id`, in
    `QUANTIZER_NAMESPACES` priority order — empty when none is found or the
    probe could not run at all.

    `params`, when the caller has it (a curated `params` field parsed by
    `fit.parse_params`, or a `hub_metadata.get(repo_id)["numHiddenLayers"]`-
    adjacent figure some future caller derives), backs the param-similarity
    fallback tier; omitted, only the `base_model`-tag tier can confirm a
    candidate.

    `force=True` bypasses the TTL, mirroring `hub_metadata.get`'s own flag.
    """
    store = _load()
    entry = store["repos"].get(repo_id)
    fresh = (isinstance(entry, dict) and not force
             and time.time() - _fetched_at(entry) < TTL_SECONDS)
    if fresh:
        sources = entry.get("sources")
        return tuple(sources) if isinstance(sources, list) else ()

    try:
        sources = _probe(repo_id, params)
    except Exception:  # noqa: BLE001 - the probe must never raise into a
        # caller: a monkeypatched `_fetch_model_info` misbehaving in a test,
        # or a future stricter HTTP client, degrades to "no sources" exactly
        # like a plain network failure would.
        sources = ()

    store["repos"][repo_id] = {"sources": list(sources), "fetchedAt": time.time()}
    store["repos"] = _bounded(store["repos"])
    _write(store)
    return sources


def clear() -> None:
    """Forget every resolved repo — mirrors `hub_metadata.clear`'s shape."""
    _write({"repos": {}})
