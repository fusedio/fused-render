"""Tests for the GGUF quantizer-namespace resolver (SPEC AI-23, D526).

`gguf_sources.sources_for(repo_id)` replaces `catalog.COUNTERPART_IDS`'s old
job of naming a llama.cpp-readable counterpart for a model this app curates
for another engine, but generalized: rather than two hand-written rows, it
probes the known quantizer namespaces (`unsloth`, `bartowski`, `ggml-org`,
`TheBloke`, `mradermacher`) for `{provider}/{basename}-GGUF` and verifies a
hit with the candidate's own `base_model` card metadata, falling back to
param-count similarity when the card carries no such tag.

Network is reached through exactly one seam, `_fetch_model_info` — every test
drives that, never `urllib`, the same discipline `test_ai_hub_metadata.py`
already keeps for `hub_metadata._fetch_raw`.
"""
import pytest

from fused_render.ai import gguf_sources


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


def _info(base_model=None, params=None):
    card = {}
    if base_model is not None:
        card["base_model"] = base_model
    info = {"cardData": card}
    if params is not None:
        info["safetensors"] = {"total": params}
    return info


def test_a_namespace_confirmed_by_base_model_tag_is_returned(monkeypatch):
    def fetch(repo_id):
        if repo_id == "unsloth/Qwen3-8B-GGUF":
            return _info(base_model="org/Qwen3-8B")
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    assert gguf_sources.sources_for("org/Qwen3-8B") == ("unsloth/Qwen3-8B-GGUF",)


def test_base_model_tag_can_be_a_list(monkeypatch):
    def fetch(repo_id):
        if repo_id == "bartowski/Qwen3-8B-GGUF":
            return _info(base_model=["other/thing", "org/Qwen3-8B"])
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    assert "bartowski/Qwen3-8B-GGUF" in gguf_sources.sources_for("org/Qwen3-8B")


def test_namespaces_are_probed_in_priority_order(monkeypatch):
    seen = []

    def fetch(repo_id):
        seen.append(repo_id)
        if repo_id == "TheBloke/Qwen3-8B-GGUF":
            return _info(base_model="org/Qwen3-8B")
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    result = gguf_sources.sources_for("org/Qwen3-8B")
    assert result == ("TheBloke/Qwen3-8B-GGUF",)
    # Every namespace ahead of the hit in priority order was tried first.
    assert seen.index("TheBloke/Qwen3-8B-GGUF") == len(gguf_sources.QUANTIZER_NAMESPACES) - 2


def test_a_candidate_with_no_base_model_tag_falls_back_to_param_similarity(monkeypatch):
    def fetch(repo_id):
        if repo_id == "unsloth/Qwen3-8B-GGUF":
            return _info(params=8_100_000_000)
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    result = gguf_sources.sources_for("org/Qwen3-8B", params=8_000_000_000)
    assert result == ("unsloth/Qwen3-8B-GGUF",)


def test_param_similarity_outside_30_percent_is_rejected(monkeypatch):
    def fetch(repo_id):
        if repo_id == "unsloth/Qwen3-8B-GGUF":
            return _info(params=20_000_000_000)
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    assert gguf_sources.sources_for("org/Qwen3-8B", params=8_000_000_000) == ()


def test_a_wrong_base_model_tag_is_rejected_even_within_param_similarity(monkeypatch):
    def fetch(repo_id):
        if repo_id == "unsloth/Qwen3-8B-GGUF":
            return _info(base_model="someone/else-entirely", params=8_000_000_000)
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    assert gguf_sources.sources_for("org/Qwen3-8B", params=8_000_000_000) == ()


def test_no_evidence_at_all_is_never_a_match(monkeypatch):
    """No `base_model` tag and no params on either side: nothing to compare,
    so this must never guess a match out of a bare 404-free existence check."""
    def fetch(repo_id):
        if repo_id == "unsloth/Qwen3-8B-GGUF":
            return _info()
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    assert gguf_sources.sources_for("org/Qwen3-8B") == ()


def test_a_repo_no_namespace_publishes_returns_empty(monkeypatch):
    monkeypatch.setattr(gguf_sources, "_fetch_model_info", lambda repo_id: None)
    assert gguf_sources.sources_for("org/some-obscure-model") == ()


def test_network_failure_degrades_silently_never_raises(monkeypatch):
    def fetch(repo_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    assert gguf_sources.sources_for("org/Qwen3-8B") == ()


def test_a_fresh_result_is_cached_and_not_refetched(monkeypatch):
    calls = []

    def fetch(repo_id):
        calls.append(repo_id)
        if repo_id == "unsloth/Qwen3-8B-GGUF":
            return _info(base_model="org/Qwen3-8B")
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    first = gguf_sources.sources_for("org/Qwen3-8B")
    n_calls = len(calls)
    second = gguf_sources.sources_for("org/Qwen3-8B")
    assert second == first
    assert len(calls) == n_calls


def test_force_bypasses_the_ttl_cache(monkeypatch):
    def fetch(repo_id):
        if repo_id == "unsloth/Qwen3-8B-GGUF":
            return _info(base_model="org/Qwen3-8B")
        return None

    monkeypatch.setattr(gguf_sources, "_fetch_model_info", fetch)
    gguf_sources.sources_for("org/Qwen3-8B")
    calls = []
    monkeypatch.setattr(gguf_sources, "_fetch_model_info",
                        lambda repo_id: calls.append(repo_id) or fetch(repo_id))
    gguf_sources.sources_for("org/Qwen3-8B", force=True)
    assert calls


def test_a_corrupt_fetchedat_never_raises_into_the_route(monkeypatch):
    """`entry.get("fetchedAt", 0)` used to be handed straight to `time.time()
    - ...`, which raises `TypeError` when the stored value is not a number —
    a hand-edited or truncated write. This module's own docstring promises it
    never raises into a route; a corrupt timestamp must read as "stale,
    refetch" rather than crash the caller (code review)."""
    monkeypatch.setattr(gguf_sources, "_fetch_model_info",
                        lambda repo_id: _info(base_model="org/Qwen3-8B")
                        if repo_id == "unsloth/Qwen3-8B-GGUF" else None)
    store = gguf_sources._load()
    store["repos"]["org/Qwen3-8B"] = {"sources": ["unsloth/Qwen3-8B-GGUF"],
                                      "fetchedAt": "not-a-timestamp"}
    gguf_sources._write(store)
    assert gguf_sources.sources_for("org/Qwen3-8B") == ("unsloth/Qwen3-8B-GGUF",)
