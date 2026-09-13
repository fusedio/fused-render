"""Tests for `fused_render.ai.hub_architecture` — a pure, no-I/O reading of a
Hub search row's architecture/engine, off the SAME `raw` dict `_EXPAND`
already fetches (item 1 of the architecture-detection brief; see
DECISIONS.md D1287+).

The MiniMax H3 fixture below is the repo that motivated this module:
`OzzyGT/MiniMax_H3_sdnq_4bit_pruned` — a Diffusers *modular* pipeline (no
flat `model_index.json`, only `modular_model_index.json` plus per-component
folders) that used to fall through `hub_loadable.loadable_kind` to
`("any", None)` and get offered as loadable by every runner, including ones
that cannot open it at all.
"""
import pytest

from fused_render.ai import hub_architecture
from fused_render.ai.runners import formats


@pytest.fixture(autouse=True)
def _clear_cache():
    hub_architecture.reset_cache()
    yield
    hub_architecture.reset_cache()


def _siblings(*names):
    return [{"rfilename": n} for n in names]


def test_minimax_h3_modular_pipeline_resolves_to_diffusers():
    """The exact repo shape that motivated this module: 21 files, no
    `model_index.json`, no `transformer-distilled*.safetensors` — only the
    modular manifest plus component subfolders."""
    raw = {
        "id": "OzzyGT/MiniMax_H3_sdnq_4bit_pruned",
        "pipeline_tag": "text-to-video",
        "library_name": "diffusers",
        "tags": ["diffusers:MiniMaxH3Pipeline"],
        "siblings": _siblings(
            "modular_model_index.json", "scheduler/scheduler_config.json",
            "audio_scheduler/scheduler_config.json",
            "transformer/config.json", "transformer_ref/config.json",
            "vae/config.json"),
        "config": {},
    }
    arch = hub_architecture.resolve(raw)
    assert arch.name == "MiniMaxH3Pipeline"
    assert arch.engine == "Diffusers"
    assert arch.shipped is True


def test_diffusers_tag_wins_over_config_signals():
    raw = {
        "tags": ["diffusers:SomePipeline"],
        "config": {"diffusers": {"_class_name": "OtherClass"},
                    "architectures": ["OtherArch"], "model_type": "other"},
        "siblings": _siblings("model_index.json"),
    }
    arch = hub_architecture.resolve(raw)
    assert arch.name == "SomePipeline"
    assert arch.engine == "Diffusers"


def test_config_diffusers_class_name_is_the_second_signal():
    raw = {
        "config": {"diffusers": {"_class_name": "FluxPipeline"},
                    "architectures": ["OtherArch"]},
        "siblings": _siblings("model_index.json"),
    }
    arch = hub_architecture.resolve(raw)
    assert arch.name == "FluxPipeline"
    assert arch.engine == "Diffusers"


def test_config_architectures_is_the_third_signal():
    raw = {"config": {"architectures": ["Qwen3VLForConditionalGeneration"],
                       "model_type": "qwen3_vl"}}
    arch = hub_architecture.resolve(raw)
    assert arch.name == "Qwen3VLForConditionalGeneration"


def test_config_model_type_is_the_fourth_signal():
    raw = {"config": {"model_type": "gemma3"}}
    arch = hub_architecture.resolve(raw)
    assert arch.name == "gemma3"


def test_library_name_alone_is_the_final_name_signal():
    raw = {"library_name": "diffusers"}
    arch = hub_architecture.resolve(raw)
    assert arch.name == "diffusers"


def test_nothing_at_all_resolves_to_no_name_and_no_engine():
    arch = hub_architecture.resolve({})
    assert arch.name is None
    assert arch.engine is None
    assert arch.shipped is False


def test_gguf_sibling_resolves_to_llama_cpp():
    raw = {"siblings": _siblings("model.Q4_K_M.gguf"), "library_name": "gguf"}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "llama.cpp"
    assert arch.shipped is True


def test_ltx_split_layout_resolves_to_ltx_2_mlx_engine():
    names = ("split_model.json", "transformer-distilled.safetensors",
             "vae_encoder.safetensors")
    assert formats.has_ltx_split_layout(frozenset(names))
    raw = {"siblings": _siblings(*names)}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "ltx-2-mlx"
    assert arch.shipped is True


def test_ltx_split_layout_wins_over_a_stray_gguf_sibling():
    names = ("split_model.json", "transformer-distilled.safetensors",
             "quantize_config.gguf")
    raw = {"siblings": _siblings(*names)}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "ltx-2-mlx"


def test_onnx_sibling_resolves_to_onnx_runtime():
    raw = {"siblings": _siblings("onnx/model.onnx")}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "ONNX Runtime"
    assert arch.shipped is True


def test_mlx_library_resolves_to_mlx_engine():
    raw = {"library_name": "mlx", "config": {"model_type": "llama"}}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "MLX"
    assert arch.shipped is True


def test_sentence_transformers_library_recognised_but_not_shipped():
    """No runner in this app is keyed by the `sentence-transformers` library
    alone — the embedding runners gate on `model_type`, not library — so
    this engine is a recognised fact with no runner behind it (yet)."""
    raw = {"library_name": "sentence-transformers"}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "Sentence Transformers"
    assert arch.shipped is False


def test_unrecognised_architecture_with_no_engine_stays_unshipped():
    raw = {"config": {"model_type": "some_unheard_of_arch"}}
    arch = hub_architecture.resolve(raw)
    assert arch.name == "some_unheard_of_arch"
    assert arch.engine is None
    assert arch.shipped is False


def test_shipped_cache_is_stable_across_calls():
    raw = {"siblings": _siblings("model_index.json"), "library_name": "diffusers"}
    first = hub_architecture.resolve(raw)
    second = hub_architecture.resolve(raw)
    assert first.shipped is True
    assert second.shipped is True
