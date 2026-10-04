"""Tests for the orthogonal capability tags (SPEC AI-28, D532).

`registry.py`'s five capability constants are load-bearing RUNNER DISPATCH —
`text-generation`, `text-to-image`, `automatic-speech-recognition`,
`embeddings`, `text-to-video` — and item 18 is explicit that they are not
reshaped into a use-case enum. `tool-use` and `vision` are tags ON TOP of
that: orthogonal boolean facts about a TEXT_GENERATION checkpoint, not new
capabilities and not a replacement for anything above.

`supports_tool_use` is a KNOWN-FAMILY allowlist, not a regex over an
arbitrary repo id — `registry.py` stays dependency-light (no filesystem or
network reads), so it takes whatever family evidence the caller already has
(the repo id itself, plus `hub_metadata`'s `modelType`/`architecture` when
available) rather than fetching anything of its own.
"""
from fused_render.ai import registry


def test_qwen3_matches_by_repo_id_alone():
    assert registry.supports_tool_use("Qwen/Qwen3-8B-Instruct") is True


def test_qwen2_5_matches():
    assert registry.supports_tool_use("Qwen/Qwen2.5-7B-Instruct") is True


def test_command_r_matches():
    assert registry.supports_tool_use("CohereForAI/c4ai-command-r-08-2024") is True


def test_hermes_matches():
    assert registry.supports_tool_use("NousResearch/Hermes-3-Llama-3.1-8B") is True


def test_llama_3_requires_instruct_qualifier():
    assert registry.supports_tool_use("meta-llama/Llama-3-8B-Instruct") is True
    assert registry.supports_tool_use("meta-llama/Llama-3-8B") is False


def test_mistral_requires_instruct_qualifier():
    assert registry.supports_tool_use("mistralai/Mistral-7B-Instruct-v0.3") is True
    assert registry.supports_tool_use("mistralai/Mistral-7B-v0.3") is False


def test_gemma_requires_it_qualifier():
    assert registry.supports_tool_use("google/gemma-3-4b-it") is True
    assert registry.supports_tool_use("google/gemma-4-12b-it") is True
    assert registry.supports_tool_use("google/gemma-3-4b") is False


def test_an_unlisted_family_is_false():
    assert registry.supports_tool_use("org/some-random-checkpoint") is False


def test_underscore_and_hyphen_variants_both_match():
    """A repo id spelled `llama_3_8b_instruct` (underscores) must match the
    same as the hyphenated form — this is a KNOWN-FAMILY check, not a strict
    literal-string one, so normalization has to bridge the Hub's own
    inconsistency between the two separators."""
    assert registry.supports_tool_use("org/llama_3_8b_instruct") is True


def test_model_type_and_architecture_evidence_is_also_consulted():
    """A repo id alone can be uninformative (`org/my-finetune`) while
    `hub_metadata`'s harvested `model_type`/`architecture` names the real
    family — evidence this function takes as keyword arguments rather than
    fetching itself, keeping `registry.py` dependency-light."""
    assert registry.supports_tool_use(
        "org/my-finetune", model_type="qwen3", architecture=None) is True


def test_capability_tags_composes_tool_use_and_vision():
    tags = registry.capability_tags("Qwen/Qwen3-8B-Instruct", has_vision=True)
    assert set(tags) == {"tool-use", "vision"}


def test_capability_tags_is_empty_when_neither_applies():
    assert registry.capability_tags("org/plain-model", has_vision=False) == ()


def test_the_capability_constants_are_unchanged():
    """The load-bearing dispatch vocabulary item 18 must not touch — six
    since D887 added typed decisions."""
    assert registry.TEXT_GENERATION == "text-generation"
    assert registry.IMAGE_GENERATION == "text-to-image"
    assert registry.SPEECH_TO_TEXT == "automatic-speech-recognition"
    assert registry.EMBEDDINGS == "embeddings"
    assert registry.VIDEO_GENERATION == "text-to-video"
    assert registry.DECISIONS == "text-classification"


# -- use cases (SPEC AI-28b) --------------------------------
# A USE CASE is a tag-shaped fact about a text-generation entry, not a sixth
# capability: `writing` / `coding` / `reasoning`. Curated entries carry theirs
# by hand; everything else (Hub search hits, cached repos) gets a heuristic
# over the repo id plus the Hub pipeline tag, llmfit-style.


def test_use_cases_are_the_three_documented_ones():
    assert registry.USE_CASES == ("writing", "coding", "reasoning")


def test_use_case_coding_from_the_repo_id():
    for rid in ("Qwen/Qwen3-Coder-30B-A3B-Instruct", "bigcode/starcoder2-15b",
                "deepseek-ai/deepseek-coder-6.7b-instruct", "codellama/CodeLlama-7b-hf",
                "mistralai/Codestral-22B-v0.1", "org/some-code-model"):
        assert registry.use_cases(rid) == ("coding",), rid


def test_use_case_reasoning_from_the_repo_id():
    for rid in ("deepseek-ai/DeepSeek-R1-Distill-Qwen-7B", "Qwen/QwQ-32B",
                "org/Foo-Reasoning-8B", "Qwen/Qwen3-4B-Thinking-2507"):
        assert registry.use_cases(rid) == ("reasoning",), rid


def test_use_case_defaults_to_writing():
    assert registry.use_cases("mistralai/Mistral-7B-Instruct-v0.3") == ("writing",)
    assert registry.use_cases("org/some-random-checkpoint") == ("writing",)


def test_use_case_does_not_match_inside_unrelated_words():
    # `decode`, `barcode` and `r10`/`mr1x` must not read as code / R1.
    assert registry.use_cases("org/decoder-chat-7b") == ("writing",)
    assert registry.use_cases("org/barcode-chat") == ("writing",)
    assert registry.use_cases("org/Mistral-R10-chat") == ("writing",)


def test_use_case_takes_hub_tags_as_evidence():
    assert registry.use_cases("org/plain-name", hub_tags=("code",)) == ("coding",)
    assert registry.use_cases("org/plain-name", pipeline_tag="text-generation") == ("writing",)


def test_use_case_coding_beats_reasoning():
    assert registry.use_cases("org/Coder-R1-7B") == ("coding",)
