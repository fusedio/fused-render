"""Hub `config.json` harvest, cached in the hub-catalog store's own parquet
table (`hub_catalog_config.HubCatalogConfig.metadata_dir`), keyed by repo id
(SPEC AI-17, D518; migrated off a single JSON file per docs/HUB_CATALOG_SPEC.md
item 5).

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

**One HTTP GET, cached in a small repo-keyed parquet table + append log**
(item 5): `_table_path()`/`_log_path()` under the catalog's `metadata_dir`,
read with a fresh DuckDB connection per call (the same "never a long-lived
connection" rule `hub_catalog.py` documents) and written under that module's
`store_lock`. A single repo's `get()` writes one row to the LOG rather than
rewriting the whole table — `_upsert` — and the log is folded back into the
table (`_compact`) once it grows past `_LOG_COMPACT_THRESHOLD` rows, so a
store with thousands of harvested repos still costs one small append per
write instead of a full rewrite. Unlike `bench_store.py`/`footprints.py` this
store is NOT machine-scoped — a repo's `config.json` describes the MODEL, not
the machine that asked for it, so there is no `_same_machine`-style identity
check and a home directory carried onto a new laptop keeps a warm cache
rather than discarding it. A corrupt or missing table/log file reads as
"nothing cached" and never raises. **One-time import**: if the old
`~/.fused-render/ai_hub_metadata.json` (D518's original store) is still
present, its `repos` are folded into the new table on first access and the
old file is renamed `.imported` so this only happens once.

**13-day TTL, now a FALLBACK rather than the only freshness signal**
(item 5). The primary freshness signal is `invalidate(repo_id)`, called by
`hub_catalog_builder.refresh_capability_pool_delta` (item 4) for every repo
its daily `lastModified` delta actually saw change — a repo tracked by some
capability's pool is re-harvested the day its Hub metadata actually moves,
not up to 13 days later. `TTL_SECONDS` still matters for a repo NO pool
tracks at all (an id the on-device catalog has simply never indexed), where
there is no delta signal to lean on; 13 days matches the analogous cache in
the comparative study this build was derived from (`llmfit`, read-only
reference, not vendored) — a `config.json` changes when a repo is
re-published under the same id, which happens on the order of weeks, not
minutes.

**A stale entry is served rather than discarded when the refetch itself
fails.** Network failure "degrades silently to no metadata" per spec, but that
rule is about a REPO WE HAVE NEVER SEEN — for one we have a two-week-old
reading of, going back to nothing on a transient DNS hiccup would regress a
page that used to answer into one that renders blank, which is worse than
serving a slightly-stale-but-still-correct answer. Only a repo with NO
prior SUCCESSFUL reading and a failed fetch reads as None.

**A failed fetch with nothing to fall back on is cached too, as a NEGATIVE
entry, under a much shorter `NEGATIVE_TTL_SECONDS`.** GGUF repos routinely
have no `config.json` at all; without this, `get()` re-issued the HTTP GET
for such a repo on every single call — a route this feeds is polled, so
that meant hammering the Hub, forever, for a repo that will never answer
differently. See `NEGATIVE_TTL_SECONDS`'s own docstring.

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

from fused_render.ai import hub_catalog
from fused_render.shell import storage

#: `huggingface.co/{repo}/resolve/main/config.json` — the same URL shape a
#: browser's "raw file" link uses, and (per AI-17's own text) a few KB, no
#: weights: fetching this for every row a Hub search returns is cheap in a way
#: fetching even one weight file would not be.
_CONFIG_URL = "https://huggingface.co/{repo}/resolve/main/config.json"

#: 13 days — see the module docstring for why this specific figure, not a
#: shorter or longer one.
TTL_SECONDS = 13 * 24 * 60 * 60

#: How long a NEGATIVE result — `_fetch_raw` could not turn `repo_id` into
#: bytes, for ANY reason, and there was no prior successful reading to fall
#: back on — is trusted before the next call retries (code review, finding
#: C). Far shorter than `TTL_SECONDS`, and deliberately ONE ttl for every
#: failure reason rather than trying to tell "this repo genuinely has no
#: `config.json`" (a GGUF repo — durable, could stand a longer TTL) apart
#: from "the network is down right now" (transient, wants a short one):
#: distinguishing those would need `_fetch_raw` to surface the HTTP status
#: rather than collapsing every failure to `None`, a bigger seam change for
#: a distinction the caller does not need — either way, `get()` today has
#: nothing better to do than wait and retry.
#:
#: **Why this exists at all**: before it did, a fetch that came back empty
#: wrote NOTHING to the store, so a repo with no `config.json` (GGUF repos
#: routinely have none) was re-fetched on every single call — a polled
#: catalog route reached the Hub, `_TIMEOUT_S` seconds at a time, on every
#: poll, forever; offline, that made a request stall for up to
#: `_TIMEOUT_S` seconds per repo it asked about in one page load.
NEGATIVE_TTL_SECONDS = 60 * 60

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

#: How many rows the append log may hold before `_upsert` folds it back into
#: the table (`_compact`). A parquet-per-repo-row read is cheap even at a few
#: hundred rows, so this is sized for "rewrite the table now and then", not
#: for correctness — a log left uncompacted is still fully readable, just
#: progressively more rows to merge on every read.
_LOG_COMPACT_THRESHOLD = 50

VERSION = 1

_UNREACHABLE = (urllib.error.URLError, OSError, ValueError, TimeoutError)

#: The subset of `config.json` this module harvests, and the key each lands
#: under in the returned dict — SPEC AI-17's own list. `head_dim` is read
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
#: A future `fit.py` KV-cache term needs this list to count only the
#: attention layers — a flat `num_hidden_layers` overcounts a hybrid model's
#: KV cache by however many Mamba layers it has, which carry no KV cache at
#: all. Harvested here even though nothing reads it yet, for the same reason
#: `bench_store.py`'s `VERSION` field exists ahead of its first migration:
#: cheaper to capture alongside the rest of this fetch than to add a second
#: harvest pass later.
_LAYER_TYPE_KEYS = ("layer_types", "layers_block_type")


def _path() -> str:
    """The OLD (pre-item-5) single-JSON-file store location — kept only as
    the one-time import source, `_maybe_import_legacy`'s read target."""
    return os.path.join(storage.home_dir(), "ai_hub_metadata.json")


def _table_path(cfg: hub_catalog.HubCatalogConfig) -> str:
    return os.path.join(cfg.metadata_dir, "repos.parquet")


def _log_path(cfg: hub_catalog.HubCatalogConfig) -> str:
    return os.path.join(cfg.metadata_dir, "repos_log.parquet")


def _read_repo_rows(path: str) -> dict[str, dict]:
    """`{repoId: entry}` from a `(repoId, payload)` parquet file, or `{}` for
    a missing or unreadable one — a truncated/corrupt file must degrade to
    "nothing cached" exactly like the old JSON store's corrupt-file case,
    never raise into `get()`/`cached()`."""
    if not os.path.exists(path):
        return {}
    import duckdb

    try:
        con = duckdb.connect()
        try:
            rows = con.execute(
                f"SELECT repoId, payload FROM read_parquet('{path}')").fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - any duckdb read failure over a
        # corrupt/truncated file reads as "nothing cached", not a raise.
        return {}
    result: dict[str, dict] = {}
    for repo_id, payload in rows:
        try:
            entry = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            result[repo_id] = entry
    return result


def _read_log_rows(path: str) -> list[tuple[str, str, int]]:
    """`[(repoId, payload, seq)]`, oldest first — `seq` (not file row order,
    which DuckDB does not promise to preserve) is what makes a later write to
    the same `repoId` win when the log is folded."""
    if not os.path.exists(path):
        return []
    import duckdb

    try:
        con = duckdb.connect()
        try:
            rows = con.execute(
                f"SELECT repoId, payload, seq FROM read_parquet('{path}') "
                "ORDER BY seq").fetchall()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - same tolerance as `_read_repo_rows`.
        return []
    return [(r[0], r[1], int(r[2])) for r in rows]


def _write_table(cfg: hub_catalog.HubCatalogConfig, repos: dict) -> None:
    """Replace the WHOLE table with `repos` and clear the log — the
    full-replace path used by `_write`/`clear()`/the legacy import, always
    called with `store_lock` already held."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    os.makedirs(cfg.metadata_dir, exist_ok=True)
    ids = [repo_id for repo_id, entry in repos.items() if isinstance(entry, dict)]
    table = pa.table({
        "repoId": ids,
        "payload": [json.dumps(repos[i]) for i in ids],
    })
    tmp = _table_path(cfg) + ".new"
    pq.write_table(table, tmp)
    os.replace(tmp, _table_path(cfg))
    try:
        os.unlink(_log_path(cfg))
    except OSError:
        pass


def _write_log_rows(cfg: hub_catalog.HubCatalogConfig,
                     rows: list[tuple[str, str, int]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    os.makedirs(cfg.metadata_dir, exist_ok=True)
    table = pa.table({
        "repoId": [r[0] for r in rows],
        "payload": [r[1] for r in rows],
        "seq": [r[2] for r in rows],
    })
    tmp = _log_path(cfg) + ".new"
    pq.write_table(table, tmp)
    os.replace(tmp, _log_path(cfg))


def _merge_all_nolegacy(cfg: hub_catalog.HubCatalogConfig) -> dict:
    repos = _read_repo_rows(_table_path(cfg))
    for repo_id, payload, _seq in _read_log_rows(_log_path(cfg)):
        try:
            entry = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(entry, dict):
            repos[repo_id] = entry
    return repos


def _maybe_import_legacy(cfg: hub_catalog.HubCatalogConfig) -> None:
    """Fold the old single-JSON store into the new table, once. Cheap no-op
    on every call after the first: the legacy file is renamed away as soon
    as it's imported, so the `os.path.exists` check below is the entire cost
    for the rest of this process's life (and any other process's)."""
    legacy = _path()
    if not os.path.exists(legacy):
        return
    with hub_catalog.store_lock(cfg):
        if not os.path.exists(legacy):
            return  # another writer imported it first
        data = storage.read_json(legacy)
        legacy_repos = data.get("repos") if isinstance(data, dict) else None
        if isinstance(legacy_repos, dict):
            current = _merge_all_nolegacy(cfg)
            current.update({k: v for k, v in legacy_repos.items() if isinstance(v, dict)})
            _write_table(cfg, current)
        try:
            os.replace(legacy, legacy + ".imported")
        except OSError:
            pass


def _merge_all(cfg: hub_catalog.HubCatalogConfig) -> dict:
    _maybe_import_legacy(cfg)
    return _merge_all_nolegacy(cfg)


def _load() -> dict:
    """The store, always a usable shape — `{"repos": {...}}` — never raising
    and never `None`: unlike `footprints`, a missing table and a corrupt one
    are not meaningfully different outcomes for THIS store (there is no
    machine-identity question to fail), so both simply start empty."""
    cfg = hub_catalog.load_config()
    return {"repos": _merge_all(cfg)}


def _write(store: dict) -> None:
    """Full replace: `store["repos"]` becomes the WHOLE store, table and log
    both — the shape every existing test already assumes (`_load()`, mutate
    one key, `_write(store)` back). Production's hot path (`get()`'s own
    single-repo write) uses `_upsert` instead, which appends to the log
    rather than paying this full rewrite on every call."""
    cfg = hub_catalog.load_config()
    with hub_catalog.store_lock(cfg):
        _write_table(cfg, store["repos"])


def _upsert(repo_id: str, entry: dict) -> None:
    """Append one row to the log for `repo_id` — the efficient single-repo
    write path `get()` uses instead of `_write`'s full-table rewrite.
    Compacts the log into the table once it passes `_LOG_COMPACT_THRESHOLD`
    rows, so a long-running process doing many individual harvests never
    grows the log without bound."""
    cfg = hub_catalog.load_config()
    with hub_catalog.store_lock(cfg):
        rows = _read_log_rows(_log_path(cfg))
        next_seq = (rows[-1][2] + 1) if rows else 0
        rows.append((repo_id, json.dumps(entry), next_seq))
        _write_log_rows(cfg, rows)
        if len(rows) >= _LOG_COMPACT_THRESHOLD:
            merged = _read_repo_rows(_table_path(cfg))
            for rid, payload, _seq in rows:
                try:
                    merged_entry = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if isinstance(merged_entry, dict):
                    merged[rid] = merged_entry
            _write_table(cfg, merged)


def invalidate(repo_id: str) -> None:
    """Force the next `get()` call for `repo_id` to refetch, regardless of
    how much of `TTL_SECONDS` remains — the item 4/5 delta-refresh hook:
    `hub_catalog_builder.refresh_capability_pool_delta` calls this for every
    repo its daily `lastModified` delta actually saw change, so a tracked
    repo's harvested metadata goes stale on the day the Hub says it changed,
    not up to 13 days later.

    Resets `fetchedAt` to `0.0` rather than deleting the entry, so `get()`'s
    existing stale-cache-fallback behaviour (a failed refetch still serves
    the last known-good `meta`) is unaffected — this is a freshness signal,
    not a forget. A repo with no entry yet is a no-op: there is nothing to
    invalidate, and `get()` will fetch it fresh on first ask regardless."""
    cfg = hub_catalog.load_config()
    entry = _merge_all(cfg).get(repo_id)
    if not isinstance(entry, dict):
        return
    updated = dict(entry)
    updated["fetchedAt"] = 0.0
    _upsert(repo_id, updated)


def _fetched_at(entry) -> float:
    """`entry["fetchedAt"]` as a `float`, or `0.0` (the oldest possible
    reading, i.e. "stale") for anything that is not one — a non-dict row, a
    missing key, or a HAND-EDITED/truncated write that leaves a string or
    `None` where a timestamp belongs (code review, finding D). A bare
    `entry.get("fetchedAt", 0)` let a corrupt value reach `time.time() -
    ...` and raise `TypeError` straight out of `get()`, which this module's
    own docstring promises never happens — `isinstance` here is the guard
    every other field in this store already gets. Booleans are excluded
    explicitly: `isinstance(True, int)` is `True` in Python, and a stray
    `"fetchedAt": true` must not be read as `1`."""
    if not isinstance(entry, dict):
        return 0.0
    value = entry.get("fetchedAt")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


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


def _harvest(config: dict) -> dict[str, object]:
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

    harvested: dict[str, object] = {v: _get(k) for k, v in _FIELDS.items()}
    harvested["architecture"] = architecture
    harvested["quantMethod"] = quant_method
    harvested["hasVisionTower"] = "vision_config" in config or "image_token_id" in config
    harvested["layerTypes"] = layer_types
    return harvested


def get(repo_id: str, *, force: bool = False) -> dict[str, object] | None:
    """The harvested metadata for `repo_id` — from cache when fresh, fetched
    and cached when stale or absent, or None when nothing is known and a
    fetch could not answer either.

    `force=True` bypasses the TTL (not the cache-on-failure fallback) — for a
    future "re-check this model" action; nothing in this build calls it yet.

    **A failed fetch is cached too, as a NEGATIVE entry** (code review,
    finding C) — `{"meta": None, "fetchedAt": ..., "negative": True}` — under
    `NEGATIVE_TTL_SECONDS` rather than `TTL_SECONDS`. Before this, a failed
    fetch wrote nothing at all, so a repo whose `config.json` genuinely does
    not exist (every GGUF repo) was re-fetched on EVERY call — see
    `NEGATIVE_TTL_SECONDS`'s own docstring for the route-level cost that had.
    A negative entry is distinguishable from "never asked" by its mere
    presence in `store["repos"]` (`get()`'s own return value is `None`
    either way, by design — a caller does not need to tell the two apart,
    only this module's internal TTL logic does).

    **A negative NEVER overwrites a prior SUCCESSFUL reading.** If `entry`
    already holds real `meta` (not itself a negative), a failed refetch
    falls back to serving that stale-but-real reading, exactly as before —
    the module docstring's rule that a transient failure must not regress an
    already-answered repo to blank. Only a repo with no positive reading to
    fall back on (never asked, or already negative) gets a negative written.
    """
    store = _load()
    entry = store["repos"].get(repo_id)
    if isinstance(entry, dict) and not force:
        ttl = NEGATIVE_TTL_SECONDS if entry.get("negative") else TTL_SECONDS
        if time.time() - _fetched_at(entry) < ttl:
            return entry.get("meta")

    try:
        raw = _fetch_raw(repo_id)
    except Exception:  # noqa: BLE001 - `_fetch_raw` is the mockable seam; a
        # test (or a future caller) raising OUT of it, rather than returning
        # None, must still degrade silently — this route must never 500 off
        # a network failure, regardless of which layer the failure surfaces at.
        raw = None

    config = None
    if raw is not None:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            parsed = None
        config = parsed if isinstance(parsed, dict) else None

    if config is not None:
        meta = _harvest(config)
        _upsert(repo_id, {"meta": meta, "fetchedAt": time.time()})
        return meta

    # The fetch failed, or the body was not a JSON object.
    if isinstance(entry, dict) and not entry.get("negative"):
        # A prior SUCCESSFUL reading exists — serve it (module docstring's
        # stale-cache-fallback rule) rather than clobbering real data with a
        # negative over what may be a transient blip.
        return entry.get("meta")

    _upsert(repo_id, {"meta": None, "fetchedAt": time.time(), "negative": True})
    return None


def cached(repo_id: str) -> dict[str, object] | None:
    """The harvested metadata for `repo_id` if this store already holds ANY
    entry for it — fresh, stale, or negative — with NO network access,
    period. `get()`'s own TTL question is not asked here at all.

    **The request-path half of the same split `hw_detect.py` already
    draws** (code review finding 1): `hw_detect.cached_hardware()` is a
    plain `storage.read_json` and the only function `fit.py`/`speed.py` may
    call, because `detect_hardware()`/`refresh_hardware()` are a slow
    subprocess probe that must never sit on a route the picker polls. This
    module's own `get()` is the equivalent slow path here — a synchronous
    `urllib` GET with an 8-second timeout — and until this function existed,
    `ai_runtime._accepts_image`/`_capability_tags` called `get()` directly
    from `describe_catalog`, which is exactly a route the picker polls. A
    `llamacpp-text` catalog (five curated GGUF repos, none of which
    publishes a `config.json`) turned one catalog request into up to five
    back-to-back 8-second-timeout fetches when offline or behind a captive
    portal, plus an outbound huggingface.co request per uncached row for a
    model the user never asked to download.

    `cached()` is that route's ONLY legal way to read this store now — a
    background refresh (`supervisor.start_hub_metadata_refresh`, mirroring
    `start_hardware_refresh`'s shape exactly) is the sole caller of `get()`
    outside this module's own tests, so the network fetch happens on a
    ticking thread and the request path only ever reads what that thread
    already wrote.

    A NEGATIVE entry (`get()`'s own `{"meta": None, "negative": True}`) is
    indistinguishable here from "never asked" — both answer `None` — which
    is correct: a caller of `cached()` only ever wants the harvested meta or
    nothing, the same contract `get()` itself already keeps for its return
    value (the `negative` flag is `get()`'s own internal TTL bookkeeping,
    never part of its public answer either).
    """
    entry = _load()["repos"].get(repo_id)
    return entry.get("meta") if isinstance(entry, dict) else None


def clear() -> None:
    """Forget every harvested repo — mirrors `footprints.clear`/`bench_store.
    clear`'s shape for the same reason: a caller wiping AI state should not
    have to enumerate this store's keys first."""
    _write({"repos": {}})
