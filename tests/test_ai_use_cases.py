"""Use cases on the curated text lists (SPEC AI-28b).

`useCases` is hand-written on every curated text entry.
"""
import pytest

from fused_render.ai import catalog, registry
from fused_render.ai.runners import formats

TEXT_LISTS = ("mlx-text", "llamacpp-text")
CODER_MLX = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
CODER_GGUF = "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf"


@pytest.mark.parametrize("code", TEXT_LISTS)
def test_every_curated_text_entry_declares_known_use_cases(code):
    for entry in catalog.SUGGESTIONS[code]:
        cases = entry.get("useCases")
        assert cases, entry["id"]
        assert set(cases) <= set(registry.USE_CASES), entry["id"]


def test_qwen3_coder_is_tagged_coding_on_both_engines():
    mlx = {e["id"]: e for e in catalog.SUGGESTIONS["mlx-text"]}
    gguf = {e["id"]: e for e in catalog.SUGGESTIONS["llamacpp-text"]}
    assert mlx[CODER_MLX]["useCases"] == ["coding"]
    assert gguf[CODER_GGUF]["useCases"] == ["coding"]


def test_qwen3_coder_gguf_has_a_recipe_naming_the_real_repo():
    # the id is a FILENAME; the repo comes from GGUF_RECIPES (D422's trap)
    recipe = formats.GGUF_RECIPES[CODER_GGUF]
    assert recipe == {"repo": "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", "file": CODER_GGUF}
    assert catalog.mirror_id(CODER_GGUF) == recipe["repo"]


@pytest.mark.parametrize("code", TEXT_LISTS)
def test_lists_stay_sorted_smallest_first(code):
    sizes = [e["size_gb"] for e in catalog.SUGGESTIONS[code]]
    assert sizes == sorted(sizes)



@pytest.mark.parametrize("code", TEXT_LISTS)
def test_every_section_has_a_curated_row_leading_with_it(code):
    # a row sits in the section of its FIRST use case; a section with no
    # row leading with it stays hidden on a typical install
    firsts = {e["useCases"][0] for e in catalog.SUGGESTIONS[code]}
    assert firsts >= set(registry.USE_CASES)
