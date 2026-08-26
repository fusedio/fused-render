"""Tests for budget-aware GGUF quant selection (SPEC AI-24, D527).

`formats.select_gguf_recipe` is the "which quant fits" half of item 14 —
"best quality that still fits" against a machine's budget, over a repo's OWN
file listing (real sizes, not `catalog.py`'s hand-curated `size_gb`). It does
NOT touch `pick_gguf_file`'s deterministic, hardware-blind picker (D412) —
that function answers a different question ("what does a bare repo id mean,
identically on every machine") and stays exactly as it is; this one answers
"what should THIS machine's download be" for a caller that already knows its
own budget (`fit.py`/`hw_detect.py`), and is used by callers that opt into a
hardware-aware choice rather than replacing the id-resolution default.
"""
from fused_render.ai.runners import formats


GB = 1024 ** 3


def test_the_best_quality_candidate_within_budget_is_picked():
    files = {
        "model-Q8_0.gguf": 9 * GB,
        "model-Q6_K.gguf": 7 * GB,
        "model-Q4_K_M.gguf": 5 * GB,
    }
    name, total = formats.select_gguf_recipe(files, budget_bytes=8 * GB)
    assert name == "model-Q6_K.gguf"
    assert total == 7 * GB


def test_no_candidate_fits_the_budget_returns_none():
    files = {"model-Q8_0.gguf": 20 * GB, "model-Q6_K.gguf": 18 * GB}
    assert formats.select_gguf_recipe(files, budget_bytes=1 * GB) is None


def test_none_budget_picks_the_best_quality_available():
    files = {"model-Q4_K_M.gguf": 5 * GB, "model-Q6_K.gguf": 7 * GB}
    name, total = formats.select_gguf_recipe(files, budget_bytes=None)
    assert name == "model-Q6_K.gguf"
    assert total == 7 * GB


def test_a_three_way_shard_set_collapses_to_one_candidate_at_the_summed_size():
    files = {
        "model-Q4_K_M-00001-of-00003.gguf": 4 * GB,
        "model-Q4_K_M-00002-of-00003.gguf": 4 * GB,
        "model-Q4_K_M-00003-of-00003.gguf": 2 * GB,
        "model-Q2_K.gguf": 3 * GB,
    }
    # 10GB summed Q4_K_M shard set must be judged as ONE 10GB candidate, not
    # three 4GB/4GB/2GB candidates any of which alone would look like it fits
    # an 8GB budget.
    result = formats.select_gguf_recipe(files, budget_bytes=8 * GB)
    assert result == ("model-Q2_K.gguf", 3 * GB)


def test_the_shard_set_is_offered_whole_when_the_budget_covers_its_sum():
    files = {
        "model-Q4_K_M-00001-of-00002.gguf": 4 * GB,
        "model-Q4_K_M-00002-of-00002.gguf": 4 * GB,
    }
    name, total = formats.select_gguf_recipe(files, budget_bytes=9 * GB)
    assert total == 8 * GB
    assert "00001" in name


def test_auxiliary_files_are_excluded_from_candidacy():
    files = {
        "model-Q4_K_M.gguf": 5 * GB,
        "mmproj-model-Q4_K_M.gguf": 1 * GB,
    }
    name, total = formats.select_gguf_recipe(files, budget_bytes=None)
    assert name == "model-Q4_K_M.gguf"
    assert total == 5 * GB


def test_a_filename_with_no_recognised_quant_token_is_not_a_candidate():
    files = {"model.gguf": 5 * GB, "model-Q4_K_M.gguf": 5 * GB}
    name, total = formats.select_gguf_recipe(files, budget_bytes=None)
    assert name == "model-Q4_K_M.gguf"


def test_an_empty_listing_returns_none():
    assert formats.select_gguf_recipe({}, budget_bytes=100 * GB) is None


def test_a_higher_bit_iq_variant_beats_a_lower_bit_plain_quant_when_both_fit(monkeypatch):
    """Code review finding 5: `IQ4_XS` (a 4-bit i-quant) sat AFTER `Q2_K`
    (a 2-bit quant) in the old ladder, so a repo offering both, on a budget
    both fit, returned the LOWER-quality `Q2_K` — contradicting the
    function's own docstring ("best quality that still fits"). `IQ4_XS`
    must rank ahead of `Q2_K` now that the ladder is bit-width-ordered."""
    files = {"model-Q2_K.gguf": 3 * GB, "model-IQ4_XS.gguf": 5 * GB}
    name, total = formats.select_gguf_recipe(files, budget_bytes=6 * GB)
    assert name == "model-IQ4_XS.gguf"
    assert total == 5 * GB


def test_a_plain_quant_still_beats_its_own_iq_variant_at_the_same_width():
    """Within one bit-width tier, a PLAIN quant still outranks its `IQ`
    sibling — the same "no engineering needed at that width" precedence
    `_gguf_rank`'s dynamic-quant tiers already establish for `pick_gguf_
    file`, restated here for the budget-aware ladder."""
    files = {"model-Q4_K_M.gguf": 5 * GB, "model-IQ4_XS.gguf": 4 * GB}
    # Both fit easily — the higher-ranked one (Q4_K_M, plain) must win even
    # though IQ4_XS is smaller, since "best quality that still fits" ranks
    # by quality first, not by size.
    name, _total = formats.select_gguf_recipe(files, budget_bytes=100 * GB)
    assert name == "model-Q4_K_M.gguf"


def test_ordering_matches_the_quality_ladder_best_first():
    # Every named token in the ladder ranks strictly ahead of the next.
    assert formats.GGUF_QUALITY_ORDER.index("Q8_0") < formats.GGUF_QUALITY_ORDER.index("Q6_K")
    assert formats.GGUF_QUALITY_ORDER.index("Q6_K") < formats.GGUF_QUALITY_ORDER.index("Q6_K_L")
    assert formats.GGUF_QUALITY_ORDER.index("Q4_K_M") < formats.GGUF_QUALITY_ORDER.index("Q4_0")
    assert formats.GGUF_QUALITY_ORDER.index("IQ4_XS") < formats.GGUF_QUALITY_ORDER.index("IQ1_M")
    # Code review finding 5: the IQ rows are interleaved at their REAL
    # bit-width tier, not dumped after every named quant regardless of
    # width — a 4-bit i-quant must outrank every 3-bit and 2-bit entry, a
    # 3-bit i-quant every 2-bit entry, and so on down to IQ1_M at the
    # bottom (no plain 1-bit quant exists in this ladder to rank below).
    assert formats.GGUF_QUALITY_ORDER.index("IQ4_XS") < formats.GGUF_QUALITY_ORDER.index("Q3_K_M")
    assert formats.GGUF_QUALITY_ORDER.index("IQ4_XS") < formats.GGUF_QUALITY_ORDER.index("Q2_K")
    assert formats.GGUF_QUALITY_ORDER.index("IQ3_M") < formats.GGUF_QUALITY_ORDER.index("Q2_K")
    assert formats.GGUF_QUALITY_ORDER.index("IQ2_M") < formats.GGUF_QUALITY_ORDER.index("IQ1_M")
    # Within one bit-width tier, the plain quant still outranks its IQ
    # sibling (same precedence `_gguf_rank`'s dynamic-quant tiers use).
    assert formats.GGUF_QUALITY_ORDER.index("Q4_0") < formats.GGUF_QUALITY_ORDER.index("IQ4_XS")
    assert formats.GGUF_QUALITY_ORDER.index("Q3_K_S") < formats.GGUF_QUALITY_ORDER.index("IQ3_M")
    assert formats.GGUF_QUALITY_ORDER.index("Q2_K") < formats.GGUF_QUALITY_ORDER.index("IQ2_M")


def test_an_unknown_size_estimates_via_the_shared_bpp_table_when_params_given():
    # No real size for the Q4_K_M file — estimated from params x bpp, reusing
    # fit.QUANT_BYTES_PER_PARAM rather than a duplicate table.
    from fused_render.ai import fit

    files = {"model-Q4_K_M.gguf": None}
    params = 8_000_000_000
    name, total = formats.select_gguf_recipe(files, budget_bytes=None, params=params)
    assert name == "model-Q4_K_M.gguf"
    assert total == params * fit.quant_bytes_per_param("Q4_K_M")


def test_an_unknown_size_with_no_params_is_excluded():
    files = {"model-Q4_K_M.gguf": None, "model-Q2_K.gguf": 3 * GB}
    name, total = formats.select_gguf_recipe(files, budget_bytes=None)
    assert name == "model-Q2_K.gguf"
