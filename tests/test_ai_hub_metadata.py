"""Tests for the Hub `config.json` metadata harvest (SPEC AI-17, D517).

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
