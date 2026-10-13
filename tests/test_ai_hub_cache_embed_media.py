"""`hub_cache.embed_media` — which kinds of file `paths` may name on a cached
embedding checkpoint, read off the same `config.json` `embed_family` reads."""
import json

import pytest

from fused_render.ai import hub_cache


@pytest.fixture()
def snapshot(monkeypatch, tmp_path):
    """Point both readers at `tmp_path` as the model's snapshot, so the test
    writes a `config.json` and nothing touches the real Hub cache."""
    monkeypatch.setattr(hub_cache, "_embed_snapshot_dir",
                        lambda repo_id: str(tmp_path) if repo_id != "cold/model" else None)

    def write(config):
        (tmp_path / "config.json").write_text(json.dumps(config))
    return write


def test_a_cold_model_cannot_say(snapshot):
    assert hub_cache.embed_media("cold/model") is None
    assert hub_cache.embed_family("cold/model") is None


def test_a_snapshot_with_no_config_cannot_say_either(snapshot):
    assert hub_cache.embed_media("x/y") is None


def test_embeddinggemma2_reads_all_three(snapshot):
    snapshot({"model_type": "embedding_gemma2", "architectures": ["EmbeddingGemma2Model"]})
    assert hub_cache.embed_media("mlx-community/embeddinggemma-2-bf16") == \
        ("image", "audio", "video")
    assert hub_cache.embed_family("mlx-community/embeddinggemma-2-bf16") == "dual"


def test_a_siglip_reads_pictures_only(snapshot):
    snapshot({"model_type": "siglip", "architectures": ["SiglipModel"]})
    assert hub_cache.embed_media("google/siglip2-base-patch16-384") == ("image",)


def test_a_prose_encoder_reads_nothing_by_path(snapshot):
    snapshot({"model_type": "modernbert", "architectures": ["ModernBertModel"]})
    assert hub_cache.embed_media("nomic-ai/x") == ()
    assert hub_cache.embed_family("nomic-ai/x") == "text"


def test_a_non_embedding_checkpoint_is_none_not_empty(snapshot):
    snapshot({"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]})
    assert hub_cache.embed_media("Qwen/x") is None
