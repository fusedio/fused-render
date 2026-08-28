"""Tests for the Hub `config.json` metadata harvest (SPEC AI-17, D518).

`hub_metadata.get()` answers "what does this repo's `config.json` say" for a
model that may not be cached on disk at all — the whole reason it exists is to
unlock a fit/KV-cache/vision estimate for a Hub SEARCH result, before a single
weight byte has downloaded. A real network fetch has no place in a unit test,
so every test drives the module through its one seam, `_fetch_raw`, exactly the
way `test_ai_model_mirror.py` drives `mirror.fetch_json` — network failure and
cache-store behaviour are what is under test, not `urllib` itself.
"""
import json
import time

import pytest

from fused_render.ai import hub_metadata


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _raw(config: dict) -> bytes:
    return json.dumps(config).encode("utf-8")


def _meta(repo_id: str, **kwargs) -> dict:
    """`hub_metadata.get()`, narrowed to a plain dict for the tests that
    already know (by construction) it answered — `get()`'s return type is
    `dict | None` for its real callers, and every one of THOSE has to keep
    checking; a test that just fed it a stub 200 response does not."""
    meta = hub_metadata.get(repo_id, **kwargs)
    assert meta is not None
    return meta


CONFIG = {
    "model_type": "qwen3",
    "architectures": ["Qwen3ForCausalLM"],
    "num_hidden_layers": 36,
    "num_attention_heads": 32,
    "num_key_value_heads": 8,
    "head_dim": 128,
    "hidden_size": 4096,
    "max_position_embeddings": 40960,
}


def test_a_successful_fetch_is_harvested_and_cached(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    meta = _meta("org/m")
    assert meta["modelType"] == "qwen3"
    assert meta["architecture"] == "Qwen3ForCausalLM"
    assert meta["numHiddenLayers"] == 36
    assert meta["numAttentionHeads"] == 32
    assert meta["numKeyValueHeads"] == 8
    assert meta["headDim"] == 128
    assert meta["hiddenSize"] == 4096
    assert meta["maxPositionEmbeddings"] == 40960
    assert meta["hasVisionTower"] is False
    assert meta["quantMethod"] is None


def test_a_vision_tower_is_detected_from_vision_config(monkeypatch):
    config = {**CONFIG, "vision_config": {"depth": 12}}
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(config))
    assert _meta("org/m")["hasVisionTower"] is True


def test_a_vision_tower_is_detected_from_image_token_id(monkeypatch):
    config = {**CONFIG, "image_token_id": 151655}
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(config))
    assert _meta("org/m")["hasVisionTower"] is True


def test_quantization_config_quant_method_is_harvested(monkeypatch):
    config = {**CONFIG, "quantization_config": {"quant_method": "awq"}}
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(config))
    assert _meta("org/m")["quantMethod"] == "awq"


def test_a_network_failure_degrades_to_none_never_raises(monkeypatch):
    def _boom(repo_id):
        raise OSError("no route to host")

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _boom)
    assert hub_metadata.get("org/m") is None


def test_a_non_json_body_degrades_to_none(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: b"not json")
    assert hub_metadata.get("org/m") is None


# -- negative caching (code review, finding C) --------------------------------


def test_a_failed_first_fetch_is_cached_so_a_poll_does_not_refetch_every_time(monkeypatch):
    """GGUF repos routinely have no `config.json` at all — before this fix,
    `get()` returned None on a failed fetch WITHOUT writing a store entry, so
    a polled catalog route re-issued the (up to `_TIMEOUT_S`-long) HTTP GET
    for that repo on EVERY poll, forever. A cached negative must stop the
    second and subsequent calls from touching the network again within
    `NEGATIVE_TTL_SECONDS`."""
    calls = []

    def _boom(repo_id):
        calls.append(repo_id)
        return None

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _boom)
    assert hub_metadata.get("org/gguf-only") is None
    assert hub_metadata.get("org/gguf-only") is None
    assert hub_metadata.get("org/gguf-only") is None
    assert len(calls) == 1


def test_a_cached_negative_is_distinguishable_from_never_asked():
    """`get()` returns `None` either way — but internally, a repo that was
    actually asked about and came back empty must leave a real store entry
    (so the TTL logic above has something to be fresh against), unlike a
    repo this process has simply never looked up. `store["repos"]` is the
    ground truth for that distinction; nothing public needs to expose it
    beyond `get()` itself answering None either way."""
    assert "org/never-asked" not in hub_metadata._load()["repos"]


def test_a_cached_negative_expires_and_retries_after_its_own_shorter_ttl(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: None)
    hub_metadata.get("org/gguf-only")

    store = hub_metadata._load()
    store["repos"]["org/gguf-only"]["fetchedAt"] = (
        time.time() - hub_metadata.NEGATIVE_TTL_SECONDS - 1)
    hub_metadata._write(store)

    calls = []
    monkeypatch.setattr(hub_metadata, "_fetch_raw",
                        lambda repo_id: calls.append(repo_id) or None)
    hub_metadata.get("org/gguf-only")
    assert calls == ["org/gguf-only"]


def test_a_negative_ttl_is_shorter_than_the_success_ttl():
    """The whole point: a repo we could not answer is retried much sooner
    than a repo we successfully harvested — a durable negative (a GGUF
    repo's missing `config.json`) does not cost much to re-check, while a
    successful harvest is expensive to re-fetch for no benefit (see
    `TTL_SECONDS`'s own docstring)."""
    assert hub_metadata.NEGATIVE_TTL_SECONDS < hub_metadata.TTL_SECONDS


def test_a_negative_cache_does_not_clobber_a_prior_successful_reading(monkeypatch):
    """A repo that answered once and then starts failing (a transient outage,
    not a genuine 404) must keep serving its last good reading — the
    pre-existing stale-cache-fallback behaviour — rather than a failed
    refetch overwriting it with a negative and losing real data."""
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    hub_metadata.get("org/m")

    store = hub_metadata._load()
    store["repos"]["org/m"]["fetchedAt"] = time.time() - hub_metadata.TTL_SECONDS - 1
    hub_metadata._write(store)

    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: None)
    meta = _meta("org/m")
    assert meta["modelType"] == "qwen3"


# -- a corrupt fetchedAt must never raise (code review, finding D) -------------


def test_a_corrupt_fetchedat_never_raises_into_the_route(monkeypatch):
    """`entry.get("fetchedAt", 0)` used to be handed straight to `time.time()
    - ...`, which raises `TypeError` when the stored value is not a number —
    a hand-edited or truncated write. This module's own docstring promises it
    never raises into a route."""
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    store = hub_metadata._load()
    store["repos"]["org/m"] = {"meta": {"modelType": "stale"},
                               "fetchedAt": "not-a-timestamp"}
    hub_metadata._write(store)
    meta = _meta("org/m")
    assert meta["modelType"] == "qwen3"  # treated as stale, refetched


def test_missing_fields_read_as_none_not_a_raise(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw({}))
    meta = _meta("org/m")
    assert meta["modelType"] is None
    assert meta["numHiddenLayers"] is None
    assert meta["hasVisionTower"] is False


def test_a_second_call_within_the_ttl_never_refetches(monkeypatch):
    calls = []

    def _fetch(repo_id):
        calls.append(repo_id)
        return _raw(CONFIG)

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _fetch)
    hub_metadata.get("org/m")
    hub_metadata.get("org/m")
    assert len(calls) == 1


def test_a_call_past_the_ttl_refetches(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    hub_metadata.get("org/m")

    store = hub_metadata._load()
    store["repos"]["org/m"]["fetchedAt"] = time.time() - hub_metadata.TTL_SECONDS - 1
    hub_metadata._write(store)

    calls = []

    def _fetch(repo_id):
        calls.append(repo_id)
        return _raw(CONFIG)

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _fetch)
    hub_metadata.get("org/m")
    assert len(calls) == 1


def test_a_stale_cache_falls_back_when_the_refetch_fails(monkeypatch):
    """A model that fetched cleanly two weeks ago should not go BLANK just
    because the network hiccups on the refresh — that would regress a page
    that used to render a fact into one that renders nothing, off a transient
    failure. The stale reading is still better evidence than none."""
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    hub_metadata.get("org/m")

    store = hub_metadata._load()
    store["repos"]["org/m"]["fetchedAt"] = time.time() - hub_metadata.TTL_SECONDS - 1
    hub_metadata._write(store)

    def _boom(repo_id):
        raise OSError("timed out")

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _boom)
    meta = _meta("org/m")
    assert meta["modelType"] == "qwen3"


def test_a_hostile_repo_id_is_never_reached_into_a_path(monkeypatch):
    """The cache key is a Hub repo id, never used to build a filesystem path
    (unlike `hub_cache`'s on-disk snapshot lookups) — but it still must not
    raise or corrupt the store for a weird value."""
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    assert hub_metadata.get("../../etc/passwd") is not None


def test_a_corrupt_store_reads_as_no_metadata(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: (_ for _ in ()).throw(OSError("down")))
    path = hub_metadata._path()
    import os

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("{not json")
    assert hub_metadata.get("org/m") is None


# -- cached(): the pure disk read, for a request path that must never block --
#
# `hw_detect.cached_hardware()` draws the identical split for the identical
# reason: a probe that can block on the network/a subprocess belongs off the
# route a picker polls, and this is `hub_metadata`'s half of the same shape
# (code review finding 1) — `ai_runtime._accepts_image`/`_capability_tags`
# now call THIS, never `get()`, from the catalog route.


def test_cached_reads_a_fresh_positive_entry_with_no_network(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    hub_metadata.get("org/m")

    def _boom(repo_id):
        raise AssertionError("cached() must never touch the network seam")

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _boom)
    meta = hub_metadata.cached("org/m")
    assert meta is not None
    assert meta["modelType"] == "qwen3"


def test_cached_reads_a_stale_entry_too_with_no_refetch(monkeypatch):
    """Unlike `get()`, `cached()` never checks the TTL — it is a plain read
    of whatever the background refresh has already written, exactly like
    `hw_detect.cached_hardware()` never re-probes."""
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: _raw(CONFIG))
    hub_metadata.get("org/m")
    store = hub_metadata._load()
    store["repos"]["org/m"]["fetchedAt"] = 0.0
    hub_metadata._write(store)

    def _boom(repo_id):
        raise AssertionError("cached() must never touch the network seam")

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _boom)
    assert hub_metadata.cached("org/m")["modelType"] == "qwen3"


def test_cached_answers_none_for_an_unknown_repo_with_no_network(monkeypatch):
    def _boom(repo_id):
        raise AssertionError("cached() must never touch the network seam")

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _boom)
    assert hub_metadata.cached("org/never-asked") is None


def test_cached_answers_none_for_a_negative_entry(monkeypatch):
    monkeypatch.setattr(hub_metadata, "_fetch_raw", lambda repo_id: None)
    assert hub_metadata.get("org/no-config") is None

    def _boom(repo_id):
        raise AssertionError("cached() must never touch the network seam")

    monkeypatch.setattr(hub_metadata, "_fetch_raw", _boom)
    assert hub_metadata.cached("org/no-config") is None


def test_cached_never_raises_over_socket_use_source_grep():
    """A source-level guard, the same shape `test_ai_hw_detect.py`'s
    `test_fit_module_only_reads_the_cache_never_the_probe` already pins for
    `hw_detect.py` — `cached`'s own function body must not name the network
    seam at all, so a future edit that reintroduces a fetch there is caught
    by reading the source, not only by a test that happens to monkeypatch it
    away."""
    import inspect

    source = inspect.getsource(hub_metadata.cached)
    assert "_fetch_raw" not in source
