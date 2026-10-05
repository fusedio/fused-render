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


# Follow-up review findings 2, 4, 5 -------------------------------------------


def test_diffusers_manifest_wins_over_a_gguf_sibling_in_the_same_repo():
    """Finding 2: a Diffusers repo that ALSO ships a `.gguf` quant under a
    component subfolder (a common FLUX/SD publishing pattern) must resolve to
    Diffusers, not llama.cpp — `.gguf` is checked only when no Diffusers
    manifest/library signal is present at all."""
    raw = {
        "tags": ["diffusers:FluxPipeline"],
        "library_name": "diffusers",
        "siblings": _siblings("model_index.json", "transformer/diffusion_pytorch_model-Q4.gguf"),
    }
    arch = hub_architecture.resolve(raw)
    assert arch.name == "FluxPipeline"
    assert arch.engine == "Diffusers"


def test_gguf_still_resolves_when_no_diffusers_signal_present():
    """The reorder in the test above must not swallow the plain GGUF case —
    a repo with no Diffusers manifest/library signal still resolves off its
    `.gguf` sibling exactly as before."""
    raw = {"siblings": _siblings("model.Q4_K_M.gguf"), "library_name": "gguf"}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "llama.cpp"


def test_is_shipped_gated_on_capability_a_video_repo_is_not_shipped_by_diffusers_image():
    """Finding 4: `_ENGINE_RUNNER_CODES["Diffusers"]` holds only IMAGE runner
    codes. A text-to-video Diffusers repo judged against the VIDEO
    capability's runner codes must read as NOT shipped — advising "needs
    Diffusers" for an engine with no video runner is a bug; the honest
    reading is "recognised, but no runner for THIS capability"."""
    raw = {"library_name": "diffusers", "tags": ["diffusers:SomeVideoPipeline"]}
    arch_video = hub_architecture.resolve(raw, capability="text-to-video")
    assert arch_video.engine == "Diffusers"
    assert arch_video.shipped is False

    arch_image = hub_architecture.resolve(raw, capability="text-to-image")
    assert arch_image.engine == "Diffusers"
    assert arch_image.shipped is True


def test_is_shipped_with_no_capability_given_falls_back_to_any_runner():
    """Backward-compatible default: a caller that does not (yet) know the
    row's capability gets the old, capability-blind reading."""
    raw = {"library_name": "diffusers"}
    arch = hub_architecture.resolve(raw)
    assert arch.engine == "Diffusers"
    assert arch.shipped is True


def test_bare_library_fallback_name_is_flagged_as_stuttering_with_engine():
    """Finding 5: the MiniMax-H3-shaped row — no `diffusers:` tag, no
    `config` at all — falls back to the bare `library_name` for `name`,
    which is the EXACT signal that also produced `engine="Diffusers"`.
    `name_is_bare_library` names that stutter so a consumer (the chip's
    parenthetical, the drawer's engine suffix) can suppress it."""
    raw = {"library_name": "diffusers",
           "siblings": _siblings("modular_model_index.json")}
    arch = hub_architecture.resolve(raw)
    assert arch.name == "diffusers"
    assert arch.engine == "Diffusers"
    assert arch.name_is_bare_library is True


def test_bare_library_fallback_not_flagged_when_it_does_not_match_engine():
    """A bare `library_name` fallback that does NOT read the same as the
    engine name (`"onnx"` vs. `"ONNX Runtime"`) is still an honest, distinct
    fact worth showing beside the engine — not suppressed."""
    raw = {"library_name": "onnx", "siblings": _siblings("model.onnx")}
    arch = hub_architecture.resolve(raw)
    assert arch.name == "onnx"
    assert arch.engine == "ONNX Runtime"
    assert arch.name_is_bare_library is False


def test_diffusers_tag_name_is_never_flagged_as_bare_library():
    """A `diffusers:<Class>` tag name is a real, specific fact — never the
    bare-library fallback, even though `engine` also reads "Diffusers"."""
    raw = {"tags": ["diffusers:FluxPipeline"], "library_name": "diffusers",
           "siblings": _siblings("model_index.json")}
    arch = hub_architecture.resolve(raw)
    assert arch.name == "FluxPipeline"
    assert arch.name_is_bare_library is False


# Architecture-name recovery from `base_model:` when `config` is empty ------
# (the user complaint: a video row with `config: {}` fell all the way
# through to the bare `library_name`, so the chip read "needs Diffusers"
# instead of naming the actual model family.)


def test_base_model_tag_recovers_a_family_name_when_config_is_empty():
    """`OzzyGT/MiniMax_H3_sdnq_8bit_pruned`-shaped row: `library_name:
    diffusers`, `config: {}`, but a `base_model:` tag naming
    `MiniMaxAI/MiniMax-H3` — the same tag `hub_models._base_model` already
    parses to show "from MiniMaxAI/MiniMax-H3" under the chip. The last
    path segment is the wanted display name, and it is NOT the bare-library
    fallback."""
    raw = {
        "library_name": "diffusers",
        "config": {},
        "tags": ["minimax-h3", "text-to-audio-video", "video-generation",
                 "base_model:quantized:MiniMaxAI/MiniMax-H3"],
    }
    arch = hub_architecture.resolve(raw, capability="text-to-video")
    assert arch.name == "MiniMax-H3"
    assert arch.name_is_bare_library is False
    assert arch.engine == "Diffusers"
    assert arch.shipped is False


def test_base_model_tag_recovers_a_family_name_for_an_mlx_format_video_repo():
    """`pipenetwork/MiniMax-H3-MLX-4bit`-shaped row: `library_name: mlx`,
    empty `config`, a `base_model:` tag. `engine` resolves to "MLX" (a
    text-generation-only runner in this app), but the recovered `name`
    still leads: the chip must not say "needs MLX" for a video repo."""
    raw = {
        "library_name": "mlx",
        "config": {},
        "tags": ["base_model:quantized:MiniMaxAI/MiniMax-H3"],
    }
    arch = hub_architecture.resolve(raw, capability="text-to-video")
    assert arch.name == "MiniMax-H3"
    assert arch.name_is_bare_library is False
    assert arch.engine == "MLX"
    assert arch.shipped is False


def test_base_model_tag_outranks_bare_library_but_not_config_signals():
    """The new signal sits ABOVE the bare `library_name` fallback but below
    every `config`-derived signal — a repo with a real `config.model_type`
    still prefers that over a `base_model:` family name."""
    raw = {
        "library_name": "diffusers",
        "config": {"model_type": "some_real_type"},
        "tags": ["base_model:quantized:MiniMaxAI/MiniMax-H3"],
    }
    arch = hub_architecture.resolve(raw)
    assert arch.name == "some_real_type"


def test_no_base_model_tag_and_empty_config_falls_back_to_bare_library():
    """No recoverable name anywhere: falls back to the bare `library_name`,
    flagged as such, exactly as before this change."""
    raw = {"library_name": "diffusers", "config": {}}
    arch = hub_architecture.resolve(raw, capability="text-to-video")
    assert arch.name == "diffusers"
    assert arch.name_is_bare_library is True
    assert arch.engine == "Diffusers"
    assert arch.shipped is False


def test_parse_base_model_tag_matches_hub_models_own_parse():
    """`hub_architecture.parse_base_model_tag` is the SAME function
    `hub_models._base_model` is now aliased to — one parse, not two."""
    from fused_render.server.routers import hub_models

    tags = ["base_model:quantized:MiniMaxAI/MiniMax-H3"]
    assert hub_architecture.parse_base_model_tag(tags) == hub_models._base_model(tags)
    assert hub_models._base_model(tags) == ("MiniMaxAI/MiniMax-H3", "quantized")
