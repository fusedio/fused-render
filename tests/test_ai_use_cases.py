"""Use cases on the curated text lists and the per-machine reasoning pick
(SPEC AI-28b).

`useCases` is hand-written on every curated text entry; `pickFor` marks the
hand-chosen recommended pick per use case (writing, coding). Reasoning's pick is
NOT hand-marked — it is the largest reasoning-tagged entry that fits the machine,
computed by `catalog.reasoning_pick` from the fit verdicts `describe_catalog`
already attaches.
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


@pytest.mark.parametrize("code", TEXT_LISTS)
def test_one_hand_picked_entry_each_for_writing_and_coding(code):
    for case in ("writing", "coding"):
        picks = [e["id"] for e in catalog.SUGGESTIONS[code] if case in e.get("pickFor", ())]
        assert len(picks) == 1, (code, case, picks)
    # a pick must belong to the use case it is the pick for
    for entry in catalog.SUGGESTIONS[code]:
        assert set(entry.get("pickFor", ())) <= set(entry["useCases"]), entry["id"]


@pytest.mark.parametrize("code", TEXT_LISTS)
def test_reasoning_is_never_hand_picked(code):
    assert all("reasoning" not in e.get("pickFor", ()) for e in catalog.SUGGESTIONS[code])


def test_qwen3_coder_is_the_coding_pick_on_both_engines():
    mlx = {e["id"]: e for e in catalog.SUGGESTIONS["mlx-text"]}
    gguf = {e["id"]: e for e in catalog.SUGGESTIONS["llamacpp-text"]}
    assert mlx[CODER_MLX]["pickFor"] == ["coding"]
    assert gguf[CODER_GGUF]["pickFor"] == ["coding"]
    assert mlx[CODER_MLX]["useCases"] == ["coding"]


def test_qwen3_coder_gguf_has_a_recipe_naming_the_real_repo():
    # the id is a FILENAME; the repo comes from GGUF_RECIPES (D422's trap)
    recipe = formats.GGUF_RECIPES[CODER_GGUF]
    assert recipe == {"repo": "unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF", "file": CODER_GGUF}
    assert catalog.mirror_id(CODER_GGUF) == recipe["repo"]


@pytest.mark.parametrize("code", TEXT_LISTS)
def test_lists_stay_sorted_smallest_first(code):
    sizes = [e["size_gb"] for e in catalog.SUGGESTIONS[code]]
    assert sizes == sorted(sizes)


def _entry(id_, size, verdict, cases=("reasoning",), source="curated"):
    return {"id": id_, "size_gb": size, "useCases": list(cases), "source": source,
            "fit": None if verdict is None else {"verdict": verdict}}


def test_reasoning_pick_is_the_largest_that_fits():
    rows = [_entry("a", 4, "easy"), _entry("b", 8, "easy"), _entry("c", 20, "no"),
            _entry("d", 30, "easy", cases=("coding",))]
    assert catalog.reasoning_pick(rows) == "b"


def test_reasoning_pick_accepts_tight_over_a_smaller_easy():
    rows = [_entry("a", 4, "easy"), _entry("b", 20, "tight")]
    assert catalog.reasoning_pick(rows) == "b"


def test_reasoning_pick_ignores_non_curated_and_unfit_everything():
    assert catalog.reasoning_pick([_entry("x", 4, "easy", source="cached")]) is None
    # nothing fits: fall back to the smallest, so a small machine still has a star
    rows = [_entry("a", 10, "no"), _entry("b", 20, "no")]
    assert catalog.reasoning_pick(rows) == "a"


def test_reasoning_pick_without_any_fit_data_takes_the_smallest():
    rows = [_entry("a", 4, None), _entry("b", 8, None)]
    assert catalog.reasoning_pick(rows) == "a"
