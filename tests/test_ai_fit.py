"""Tests for the fit verdict (SPEC AI-16, AI-16b, AI-16c, D497).

`ai/fit.py` computes {verdict, basis, footprintBytes} over the best footprint
available for a model, on the precedence ladder measured > declared >
download, judged against headroom thresholds rather than a fraction of total
RAM, with an Apple-Silicon wired-memory hard ceiling.

`machine_ram_gb` is cached forever (`functools.lru_cache`), so every test
here monkeypatches `fit._wired_limit_mb` directly and drives `fit.verdict`
with an explicit `ram_gb` path by monkeypatching `fit.machine_ram_gb` itself
— never depends on the real host's RAM, which would make the suite fail
differently on every machine it runs on.
"""
import pytest

from fused_render.ai import fit, footprints, hw_detect


@pytest.fixture(autouse=True)
def _no_real_platform(monkeypatch):
    """Every test controls RAM and the wired limit explicitly — never the
    real host's. Defaults: 32GB RAM, no Apple-Silicon ceiling in play."""
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 32.0)
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: None)


@pytest.fixture(autouse=True)
def _isolated_footprints(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


# -- public surface for SPEC AI-21 (speed.py) --------------------------------------


def test_is_apple_silicon_reads_the_wired_limit_probe(monkeypatch):
    """SPEC AI-21: `speed.py`'s backend inference needs the identical "is
    this Apple Silicon" fact `_select_pool`'s own `is_apple_unified`
    parameter already encodes inside `verdict()` — exposed as its own public
    function so a second module does not re-derive it from a fresh sysctl
    probe or platform-name guesswork that could drift from this one."""
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: None)
    assert fit.is_apple_silicon() is False
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: 0)
    assert fit.is_apple_silicon() is True
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: 16_000)
    assert fit.is_apple_silicon() is True


def test_weight_bytes_is_the_download_rungs_own_weight_size_figure():
    """A public wrapper over `_weight_bytes` — the same weight-size figure
    the `download` rung's own arithmetic already uses, exposed so `speed.py`
    (SPEC AI-21) can turn it into a tok/s estimate without depending on a
    private name or recomputing the precedence rule (recognized-quant
    `params x bpp` vs. a real `size_gb`) a second time."""
    # A recognized quant with parseable params: params x bpp wins.
    assert fit.weight_bytes(4.0, "1.2B", "MLX 4-bit") == pytest.approx(
        1.2e9 * fit.QUANT_BYTES_PER_PARAM["mlx_4bit"])
    # An unrecognized quant with a real size_gb: size_gb wins (D521's rule).
    assert fit.weight_bytes(6.1, "27B", "Ternary 2-bit") == 6.1 * fit.GB_BYTES
    # Nothing at all: None, same as `_weight_bytes` itself.
    assert fit.weight_bytes(None, None, None) is None


# -- the precedence ladder ---------------------------------------------------------


def test_download_is_the_floor_when_nothing_else_is_known():
    """SPEC AI-19 item 3: the flat runtime-overhead constant now lands on
    every `download`-rung estimate, so this is `size_gb` plus
    `RUNTIME_OVERHEAD_BYTES` rather than `size_gb` alone."""
    result = fit.verdict("text-generation", "org/m", size_gb=4.0)
    assert result is not None
    assert result["basis"] == "download"
    assert result["footprintBytes"] == 4.0 * 1e9 + fit.RUNTIME_OVERHEAD_BYTES


def test_declared_wins_over_download():
    result = fit.verdict("text-generation", "org/m", size_gb=4.0, resident_gb=6.0)
    assert result is not None
    assert result["basis"] == "declared"
    assert result["footprintBytes"] == 6.0 * 1e9


def test_measured_wins_over_declared_and_download():
    footprints.record("text-generation", "org/m", 5_000_000_000)
    result = fit.verdict("text-generation", "org/m", size_gb=4.0, resident_gb=6.0)
    assert result is not None
    assert result["basis"] == "measured"
    assert result["footprintBytes"] == 5_000_000_000


def test_none_when_nothing_is_known_at_all():
    """AI-11a's rule that an unknown size is a dash and never a guess governs
    the verdict too."""
    assert fit.verdict("text-generation", "org/m") is None


def test_a_measurement_for_a_DIFFERENT_capability_does_not_leak_in():
    """SPEC AI-16a: the same checkpoint can serve two capabilities with two
    different footprints since AI-11j."""
    footprints.record("image-to-text", "org/m", 9_000_000_000)
    result = fit.verdict("text-generation", "org/m", size_gb=4.0)
    assert result is not None
    assert result["basis"] == "download"


# -- headroom thresholds (AI-16b) ---------------------------------------------------


def test_easy_is_within_60_percent_of_the_usable_budget():
    # 32GB RAM, 8GB reserve -> 24GB usable, 60% of that is 14.4GB.
    result = fit.verdict("text-generation", "org/m", size_gb=14.0)
    assert result is not None
    assert result["verdict"] == "easy"


def test_tight_is_between_the_easy_fraction_and_the_usable_budget():
    # 24GB usable; 20GB is past 60% (14.4GB) but within the full 24GB.
    result = fit.verdict("text-generation", "org/m", size_gb=20.0)
    assert result is not None
    assert result["verdict"] == "tight"


def test_no_is_past_the_usable_budget():
    # 24GB usable; 30GB exceeds it even before any wired-limit gate.
    result = fit.verdict("text-generation", "org/m", size_gb=30.0)
    assert result is not None
    assert result["verdict"] == "no"


def test_thresholds_scale_with_ram_not_a_flat_fraction(monkeypatch):
    """The whole point of moving off 25%/50%-of-total: a 64GB machine must
    not leave 32GB unusable for no stated reason."""
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 64.0)
    # 64GB - 8GB reserve = 56GB usable; 40GB is within it but past 60% (33.6GB).
    result = fit.verdict("text-generation", "org/m", size_gb=40.0)
    assert result is not None
    assert result["verdict"] == "tight"


def test_a_measured_no_is_reachable_and_not_a_contradiction():
    """AI-16c: the footprint store only ever holds models that ran — a
    measured 'no' means it ran while nothing else was competing for memory,
    not that the number is wrong."""
    footprints.record("text-generation", "org/m", 30_000_000_000)
    result = fit.verdict("text-generation", "org/m")
    assert result is not None
    assert result["basis"] == "measured"
    assert result["verdict"] == "no"


# -- the Apple-Silicon wired-limit ceiling (AI-16b) ---------------------------------


def test_a_footprint_past_the_wired_limit_is_no_even_with_headroom_to_spare(monkeypatch):
    """MLX cannot exceed `iogpu.wired_limit_mb` no matter how much of the
    reserve-adjusted budget the arithmetic found free."""
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: 10_000)  # 10 GB (MiB-ish) ceiling
    # 12GB is comfortably "easy" by headroom (24GB usable, 60% = 14.4GB) but
    # past a 10,000 MiB (~10.5GB) wired ceiling.
    result = fit.verdict("text-generation", "org/m", size_gb=12.0)
    assert result is not None
    assert result["verdict"] == "no"


def test_wired_limit_zero_means_the_apple_default_not_unset(monkeypatch):
    """Apple's own documented meaning of 0: no explicit limit, so the kernel
    enforces its default (~75% of RAM) — not 'no ceiling at all'."""
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: 0)
    # 32GB * 0.75 = 24GB default ceiling. 26GB clears headroom's usable-budget
    # gate too (past 24GB usable) so this exercises the wired branch is at
    # least as strict, not that it alone decided "no".
    result = fit.verdict("text-generation", "org/m", size_gb=26.0)
    assert result is not None
    assert result["verdict"] == "no"


def test_an_unreadable_wired_limit_costs_the_gate_never_the_verdict(monkeypatch):
    """None (off Darwin, or a failed read) must not manufacture a 'no' —
    only the headroom arithmetic decides in that case."""
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: None)
    result = fit.verdict("text-generation", "org/m", size_gb=14.0)
    assert result is not None
    assert result["verdict"] == "easy"


# -- footprint_bytes as its own unit ------------------------------------------------


def test_footprint_bytes_ignores_a_zero_or_negative_resident_gb():
    bytes_, basis = fit.footprint_bytes("text-generation", "org/m", size_gb=4.0,
                                        resident_gb=0)
    assert basis == "download" and bytes_ == 4.0 * 1e9 + fit.RUNTIME_OVERHEAD_BYTES


def test_footprint_bytes_ignores_a_bool_masquerading_as_a_number():
    """`isinstance(True, int)` is True in Python — a stray `resident_gb: true`
    from a malformed catalog entry must not be read as `resident_gb: 1`."""
    bytes_, basis = fit.footprint_bytes("text-generation", "org/m", size_gb=4.0,
                                        resident_gb=True)
    assert basis == "download"


# -- SPEC AI-19 item 3: flat runtime overhead --------------------------------------


def test_measured_and_declared_do_not_get_a_second_helping_of_overhead():
    """Only the `download` rung is a GUESS this module is making — `measured`
    and `declared` are already real, observed numbers that include whatever
    overhead the model actually used, so adding the flat constant on top of
    either would double count it."""
    footprints.record("text-generation", "org/m", 5_000_000_000)
    measured = fit.footprint_bytes("text-generation", "org/m", size_gb=4.0)
    assert measured == (5_000_000_000, "measured")

    declared = fit.footprint_bytes("text-generation", "other/m", size_gb=4.0, resident_gb=6.0)
    assert declared == (6.0 * 1e9, "declared")


# -- SPEC AI-19 item 4: quantization-aware weight sizing ---------------------------


@pytest.mark.parametrize("label,key", [
    ("F32", "f32"), ("fp32", "f32"),
    ("BF16", "bf16"), ("F16", "f16"), ("fp16", "f16"),
    ("Q8_0", "q8_0"), ("GGUF Q8_0", "q8_0"),
    ("Q6_K", "q6_k"), ("Q5_K_M", "q5_k_m"),
    ("GGUF Q4_K_M", "q4_k_m"), ("Q4_0", "q4_0"),
    ("Q3_K_M", "q3_k_m"), ("Q2_K", "q2_k"),
    ("MLX 8-bit", "mlx_8bit"), ("MLX 4-bit", "mlx_4bit"),
    ("AWQ 4-bit", "awq_4bit"), ("GPTQ 4-bit", "awq_4bit"),
    ("AWQ 8-bit", "awq_8bit"), ("GPTQ 8-bit", "awq_8bit"),
])
def test_quant_key_recognizes_every_table_entry(label, key):
    assert fit._quant_key(label) == key
    assert fit.quant_bytes_per_param(label) == fit.QUANT_BYTES_PER_PARAM[key]


def test_quant_key_is_none_for_an_unrecognised_or_absent_string():
    """`"OptiQ 4-bit"`, `"Ternary 2-bit"` and `"int8 (torchao)"` are real
    `catalog.py` quantization labels this table has no row for — the parser
    must not fabricate a match, and the caller falls back to
    `DEFAULT_BYTES_PER_PARAM`."""
    for label in ("OptiQ 4-bit", "Ternary 2-bit", "int8 (torchao)", None, ""):
        assert fit._quant_key(label) is None
        assert fit.quant_bytes_per_param(label) == fit.DEFAULT_BYTES_PER_PARAM


def test_download_tier_derives_weight_size_from_params_and_quant():
    """SPEC item 4: `params x bytes-per-param`, not the hand-curated
    `size_gb`, once a real parameter count is known — a wildly wrong
    `size_gb` (the catalog.py:52-58 bug class) must not leak through when
    `params` is available to compute a better number."""
    result = fit.verdict("text-generation", "org/m", size_gb=999.0,
                        params=7_000_000_000, quantization="Q4_K_M")
    assert result is not None
    weight = 7_000_000_000 * 0.58
    assert result["footprintBytes"] == pytest.approx(weight + fit.RUNTIME_OVERHEAD_BYTES)


def test_download_tier_falls_back_to_size_gb_when_params_are_absent():
    """SPEC item 4's closing line: `size_gb` keeps working as an override/
    fallback — an entry that has never supplied `params` must not regress."""
    result = fit.verdict("text-generation", "org/m", size_gb=4.0, quantization="Q4_K_M")
    assert result is not None
    assert result["footprintBytes"] == pytest.approx(4.0 * 1e9 + fit.RUNTIME_OVERHEAD_BYTES)


# -- SPEC AI-19 item 4b: parsing catalog.py's free-text `params` field -------------


def test_parse_params_accepts_an_already_numeric_count():
    """A caller that already has a real count in hand (a future non-string
    source) must not have to stringify it first."""
    assert fit.parse_params(7_000_000_000) == 7_000_000_000
    assert fit.parse_params(7e9) == 7e9
    assert fit.parse_params(0) is None
    assert fit.parse_params(-1) is None
    assert fit.parse_params(True) is None  # bool masquerading as a number


@pytest.mark.parametrize("label,expected", [
    # Every plain `<number><unit>` form actually present in catalog.py
    # (`grep -oP '"params": "[^"]*"' fused_render/ai/catalog.py | sort -u`).
    ("39M", 39_000_000), ("137M", 137_000_000), ("149M", 149_000_000),
    ("244M", 244_000_000), ("375M", 375_000_000), ("809M", 809_000_000),
    ("1.1B", 1_100_000_000), ("1.2B", 1_200_000_000), ("1.5B", 1_500_000_000),
    ("4B", 4_000_000_000), ("9B", 9_000_000_000), ("22B", 22_000_000_000),
    ("27B", 27_000_000_000),
])
def test_parse_params_covers_the_real_spread_of_catalog_forms(label, expected):
    assert fit.parse_params(label) == pytest.approx(expected)


def test_parse_params_moe_form_uses_the_total_not_the_active_count():
    """`"8B (~1B active)"` (`LiquidAI/LFM2.5-8B-A1B-MLX-4bit`): the LEADING
    figure is total resident parameters, which is what a memory footprint
    scales with — inactive experts are ordinary tensors on disk and in
    memory, per that row's own catalog.py note. The parenthetical active
    count is a compute/bandwidth figure, deliberately dropped here."""
    assert fit.parse_params("8B (~1B active)") == pytest.approx(8_000_000_000)


def test_parse_params_effective_form_is_deliberately_unparseable():
    """`"4B effective"` (Gemma's MatFormer "E4B" naming,
    `mlx-community/gemma-4-e4b-it-4bit` / `gemma-4-E4B-it-Q4_K_M.gguf`) gives
    exactly one number, and that number is a compute-quality-parity figure,
    not a parameter count — there is no second (total) figure in the string
    to fall back to the way the MoE form has one. Parsing it as a literal 4B
    would be the WRONG figure (see the verification against real catalog
    rows below); `None` is the honest answer, and the caller falls back to
    the curated `size_gb`."""
    assert fit.parse_params("4B effective") is None
    assert fit.parse_params("Effective 4B") is None  # case- and order-insensitive


def test_parse_params_is_none_for_garbage_or_absent_input():
    for value in (None, "", "unknown", "N/A"):
        assert fit.parse_params(value) is None


# -- SPEC AI-19 item 4c: verified against real catalog.py rows ---------------------


#: Two curated rows land WAY outside a sane `params x bpp` vs. `size_gb`
#: band, for two DIFFERENT documented reasons — neither is "the parser is
#: wrong" or "size_gb was wrong the catalog.py:52-58 way"; both are real,
#: Two curated rows had a `quantization` string the table has no row for
#: ("Ternary 2-bit", "int8 (torchao)") — real, known gaps in
#: `QUANT_BYTES_PER_PARAM` (see `_weight_bytes`'s own docstring for why
#: neither gets a made-up bpp figure). `_weight_bytes` treats "quantization
#: unrecognized" as a GENERAL property, not two special cases: an
#: unrecognized row with a real `size_gb` uses it exactly rather than an
#: unconditional (and, for these two, wildly wrong — 2.57x over and 0.28x
#: under) `params x bpp` guess. The two ids below are referenced only to
#: assert this build's own investigation stays true against a moving
#: catalog, never to special-case them out of a check.
_INVESTIGATED_UNRECOGNIZED_QUANT_ROW_IDS = frozenset({
    "prism-ml/Ternary-Bonsai-27B-mlx-2bit",
    "tonera/FLUX.2-klein-4B-int8-diffusers",
})


def _catalog_rows_with_params_quant_and_size():
    from fused_render.ai import catalog

    for models in catalog.SUGGESTIONS.values():
        for entry in models:
            params_str = entry.get("params")
            quant = entry.get("quantization")
            size_gb = entry.get("size_gb")
            if params_str and quant and size_gb:
                yield entry, params_str, quant, size_gb


def test_a_recognized_quant_row_diverging_badly_fails_loudly():
    """Not a synthetic fixture: every curated `catalog.py` row whose
    `quantization` string IS RECOGNISED in `QUANT_BYTES_PER_PARAM` (and
    whose `params` parses — the "effective" rows are excluded on purpose,
    see `parse_params`'s own docstring) gets an INDEPENDENT `params x bpp`
    estimate from `_weight_bytes`, checked here against a generous 60%
    relative band of the curated `size_gb` — loose enough to allow for
    architecture overhead (embeddings/lm_head/router — this table has no
    per-architecture term) while still catching a WRONG bpp key or a badly
    wrong curated number, which is what this test exists to catch, per the
    coordinator's request that a large divergence be reported rather than
    smoothed over.

    An UNRECOGNIZED-quant row is deliberately not checked here: per
    `_weight_bytes`'s own rule, such a row's estimate now IS `size_gb`
    (see `test_an_unrecognized_quant_row_uses_size_gb_exactly` below), so
    checking it against `size_gb` would trivially always pass and prove
    nothing about the table."""
    divergent = []
    checked = 0
    for entry, params_str, quant, size_gb in _catalog_rows_with_params_quant_and_size():
        if fit._quant_key(quant) is None:
            continue
        parsed = fit.parse_params(params_str)
        if parsed is None:
            continue
        checked += 1
        weight_bytes = fit._weight_bytes(size_gb, params_str, quant)
        assert weight_bytes is not None
        ratio = (weight_bytes / fit.GB_BYTES) / size_gb
        if not (0.4 <= ratio <= 1.6):
            divergent.append((entry["id"], params_str, quant, size_gb, ratio))
    assert checked >= 10, "expected the real catalog to have at least this many recognized-quant rows"
    assert not divergent, f"recognized-quant rows outside a sane band: {divergent}"


def test_an_unrecognized_quant_row_uses_size_gb_exactly():
    """The general rule: a defaulted (unrecognized) `quantization` string
    carries no evidentiary basis for a bytes-per-param figure, so
    `_weight_bytes` must not let `params x DEFAULT_BYTES_PER_PARAM`
    override a real, curated `size_gb` — this is a property of EVERY such
    row, not two by-id special cases. Checked against every real curated
    row with an unrecognized quant string and a `size_gb`, and asserted to
    include (not just allow) the two rows this build's own investigation
    found diverging under the old, unconditional rule."""
    seen_investigated_ids = set()
    checked = 0
    for entry, params_str, quant, size_gb in _catalog_rows_with_params_quant_and_size():
        if fit._quant_key(quant) is not None:
            continue
        checked += 1
        weight_bytes = fit._weight_bytes(size_gb, params_str, quant)
        assert weight_bytes == pytest.approx(size_gb * fit.GB_BYTES), (
            f"{entry['id']}: an unrecognized quant string must fall back to "
            f"size_gb exactly, not a defaulted params x bpp guess")
        if entry["id"] in _INVESTIGATED_UNRECOGNIZED_QUANT_ROW_IDS:
            seen_investigated_ids.add(entry["id"])
    assert checked >= 5, "expected the real catalog to have at least this many unrecognized-quant rows"
    assert seen_investigated_ids == _INVESTIGATED_UNRECOGNIZED_QUANT_ROW_IDS, (
        "the two rows this build's investigation found diverging must still "
        "be present in the catalog and still have an unrecognized quant "
        "string — if this fails, the finding needs re-reading, not deleting")


def test_the_two_investigated_rows_snap_back_to_their_curated_size():
    """Regression lock, by NAME, on the two concrete before/after figures
    the coordinator asked to see reported: `parse_params(params) x
    quant_bytes_per_param(quantization)` (the OLD, unconditional formula)
    still computes the wildly-wrong numbers this investigation found —
    proving the gap is real and not an artifact of this test's own
    arithmetic — while `_weight_bytes` (what `fit.verdict` actually calls)
    now answers `size_gb` exactly for both."""
    from fused_render.ai import catalog

    by_id = {entry["id"]: entry
            for models in catalog.SUGGESTIONS.values() for entry in models}

    ternary = by_id["prism-ml/Ternary-Bonsai-27B-mlx-2bit"]
    ternary_params = fit.parse_params(ternary["params"])
    assert ternary_params is not None
    old_formula_gb = (ternary_params * fit.quant_bytes_per_param(ternary["quantization"])
                      / fit.GB_BYTES)
    assert old_formula_gb == pytest.approx(15.66, abs=0.01)  # ~2.57x over size_gb
    fixed = fit._weight_bytes(ternary["size_gb"], ternary["params"], ternary["quantization"])
    assert fixed == pytest.approx(6.1 * fit.GB_BYTES)  # == size_gb, not 15.66GB

    flux = by_id["tonera/FLUX.2-klein-4B-int8-diffusers"]
    flux_params = fit.parse_params(flux["params"])
    assert flux_params is not None
    old_formula_gb = (flux_params * fit.quant_bytes_per_param(flux["quantization"])
                      / fit.GB_BYTES)
    assert old_formula_gb == pytest.approx(2.32, abs=0.01)  # ~0.28x under size_gb
    fixed = fit._weight_bytes(flux["size_gb"], flux["params"], flux["quantization"])
    assert fixed == pytest.approx(8.2 * fit.GB_BYTES)  # == size_gb, not 2.32GB


# -- SPEC AI-19 item 5: KV cache term -----------------------------------------------


def test_kv_cache_matches_the_hand_computed_formula():
    kv = fit._kv_cache_bytes(num_hidden_layers=32, num_key_value_heads=8,
                             head_dim=128, context_tokens=8192, kv_dtype="fp16")
    expected = 2 * 8 * 128 * 8192 * 2.0 * 32
    assert kv == pytest.approx(expected)


def test_kv_cache_head_dim_derives_from_hidden_size_and_attention_heads():
    """`head_dim` absent (the Hub did not publish it — `hub_metadata.py`'s
    own docstring assigns this derivation to `fit.py`): `hidden_size /
    num_attention_heads` stands in."""
    with_head_dim = fit._kv_cache_bytes(num_hidden_layers=4, num_key_value_heads=4,
                                        head_dim=64, context_tokens=1024)
    derived = fit._kv_cache_bytes(num_hidden_layers=4, num_key_value_heads=4,
                                  hidden_size=512, num_attention_heads=8,
                                  context_tokens=1024)
    assert derived == pytest.approx(with_head_dim)


def test_kv_cache_num_key_value_heads_falls_back_to_attention_heads_then_eight():
    via_attention_heads = fit._kv_cache_bytes(num_hidden_layers=4, head_dim=64,
                                              num_attention_heads=6, context_tokens=1024)
    assert via_attention_heads == pytest.approx(2 * 6 * 64 * 1024 * 2.0 * 4)

    via_flat_eight = fit._kv_cache_bytes(num_hidden_layers=4, head_dim=64, context_tokens=1024)
    assert via_flat_eight == pytest.approx(2 * 8 * 64 * 1024 * 2.0 * 4)


def test_kv_cache_is_zero_without_the_geometry_to_compute_it():
    """No `head_dim` and nothing to derive it from — 0.0, never a guess."""
    assert fit._kv_cache_bytes(num_hidden_layers=32) == 0.0
    assert fit._kv_cache_bytes(head_dim=128) == 0.0


def test_kv_cache_counts_only_full_attention_layers_in_a_hybrid_config():
    """Jamba/Zamba-style hybrids interleave Mamba (no KV cache at all) with
    real attention layers — only the latter scale with context."""
    layer_types = ["attention", "mamba", "mamba", "attention", "mamba", "mamba"]
    kv = fit._kv_cache_bytes(num_hidden_layers=6, layer_types=layer_types,
                             num_key_value_heads=8, head_dim=64, context_tokens=1024)
    expected = 2 * 8 * 64 * 1024 * 2.0 * 2  # only the two "attention" entries
    assert kv == pytest.approx(expected)


def test_kv_cache_sliding_attention_layers_still_count():
    """A sliding-window attention layer still caches real K/V — it must not
    be excluded the way a Mamba/SSM layer with no cache at all is."""
    kv = fit._kv_cache_bytes(num_hidden_layers=2, layer_types=["sliding_attention", "full_attention"],
                             num_key_value_heads=8, head_dim=64, context_tokens=1024)
    expected = 2 * 8 * 64 * 1024 * 2.0 * 2
    assert kv == pytest.approx(expected)


def test_verdict_includes_the_kv_cache_term_in_the_download_footprint():
    result = fit.verdict("text-generation", "org/m", size_gb=4.0,
                        num_hidden_layers=32, num_key_value_heads=8,
                        head_dim=128, num_attention_heads=32)
    assert result is not None
    kv = 2 * 8 * 128 * 8192 * 2.0 * 32
    assert result["footprintBytes"] == pytest.approx(4.0 * 1e9 + kv + fit.RUNTIME_OVERHEAD_BYTES)


# -- SPEC AI-19 item 6: VRAM-vs-RAM pool selection + run mode ----------------------


def test_run_mode_is_cpu_only_when_no_hardware_cache_exists():
    """No `hw_detect` cache yet (a fresh install — the common case per that
    module's own docstring) must not be mistaken for a confirmed GPU-less
    machine forever, but the safe default is the same "judge against RAM"
    behaviour this module has always had."""
    result = fit.verdict("text-generation", "org/m", size_gb=4.0)
    assert result is not None
    assert result["runMode"] == "cpu-only"


def test_run_mode_is_gpu_when_the_footprint_fits_in_vram(monkeypatch):
    info = hw_detect.HardwareInfo(
        gpus=[hw_detect.GpuDevice(name="NVIDIA GeForce RTX 4090", vram_gb=24.0)],
        total_vram_gb=24.0, bandwidth_gb_s=1008.0, detected_at=0.0)
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: info)
    result = fit.verdict("text-generation", "org/m", size_gb=10.0)
    assert result is not None
    assert result["runMode"] == "gpu"


def test_run_mode_is_cpu_offload_when_the_footprint_exceeds_vram_but_fits_combined(monkeypatch):
    info = hw_detect.HardwareInfo(
        gpus=[hw_detect.GpuDevice(name="NVIDIA GeForce RTX 3060", vram_gb=8.0)],
        total_vram_gb=8.0, bandwidth_gb_s=360.0, detected_at=0.0)
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: info)
    # 32GB RAM - 8GB reserve = 24GB usable; 8GB VRAM + 24GB usable = 32GB combined.
    # 20GB exceeds the 8GB VRAM alone but fits the combined pool.
    result = fit.verdict("text-generation", "org/m", size_gb=19.5)
    assert result is not None
    assert result["runMode"] == "cpu-offload"
    assert result["verdict"] in ("easy", "tight")


def test_run_mode_is_gpu_for_a_non_apple_unified_memory_device(monkeypatch):
    """A Strix Halo / Grace-class APU draws from system RAM exactly like
    Apple Silicon — `hw_detect._apply_unified_override`'s own case — so the
    pool stays RAM and the mode is still `gpu`, not `cpu-offload`."""
    info = hw_detect.HardwareInfo(
        gpus=[hw_detect.GpuDevice(name="AMD Ryzen AI Max+ 395", vram_gb=32.0, unified_memory=True)],
        total_vram_gb=32.0, bandwidth_gb_s=256.0, detected_at=0.0)
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: info)
    result = fit.verdict("text-generation", "org/m", size_gb=14.0)
    assert result is not None
    assert result["runMode"] == "gpu"


def test_apple_pool_stays_system_ram_regardless_of_a_stale_hw_detect_cache(monkeypatch):
    """SPEC AI-19: the Apple wired-limit hard ceiling and its RAM pool must
    not regress even if `hw_detect` has a (stale, or simply wrong for this
    unified-memory machine) discrete-GPU-shaped cache entry."""
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: None)
    info = hw_detect.HardwareInfo(
        gpus=[hw_detect.GpuDevice(name="NVIDIA GeForce RTX 4090", vram_gb=1.0)],
        total_vram_gb=1.0, bandwidth_gb_s=1008.0, detected_at=0.0)
    monkeypatch.setattr(hw_detect, "cached_hardware", lambda: info)
    # Simulate being on Apple Silicon: a real (non-None) wired limit.
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: 0)  # Apple default, 0.75 * 32GB = 24GB
    result = fit.verdict("text-generation", "org/m", size_gb=14.0)
    assert result is not None
    # Judged against the 24GB RAM-derived ceiling, NOT the bogus 1GB VRAM
    # figure a stray hw_detect cache reports.
    assert result["verdict"] == "easy"
    assert result["runMode"] == "gpu"


# -- SPEC AI-19 item 6b: the hardware reading is threaded, not re-read ------------


def test_verdict_reads_the_hardware_cache_itself_when_none_is_threaded_through(monkeypatch):
    """A lone caller (no `hardware=` passed, `footprint_bytes`'s own
    `footprint_store` default-fetch shape) still gets a correct verdict —
    `verdict()` reads `hw_detect.cached_hardware()` itself exactly once per
    call, the same "do your own single read" fallback `footprint_store`
    already has."""
    calls = []
    real_cached_hardware = hw_detect.cached_hardware

    def _counting_cached_hardware():
        calls.append(1)
        return real_cached_hardware()

    monkeypatch.setattr(hw_detect, "cached_hardware", _counting_cached_hardware)
    result = fit.verdict("text-generation", "org/m", size_gb=4.0)
    assert result is not None
    assert len(calls) == 1


def test_verdict_does_not_read_the_hardware_cache_again_when_one_is_threaded_through(monkeypatch):
    """Code review: `_select_pool` used to call `hw_detect.cached_hardware()`
    itself on every `verdict()` invocation — a fresh `storage.read_json`
    open and JSON parse per catalog ROW on a route the model picker polls,
    the exact cost `machine_ram_gb` is `lru_cache`d to avoid and
    `footprint_store` is threaded through to avoid. A caller that already
    has a reading in hand (the shape `ai_runtime.describe_catalog` now
    uses) must not trigger a second read — `hardware=` passed explicitly
    is used AS-IS, with zero further `hw_detect.cached_hardware()` calls."""
    calls = []
    real_cached_hardware = hw_detect.cached_hardware

    def _counting_cached_hardware():
        calls.append(1)
        return real_cached_hardware()

    monkeypatch.setattr(hw_detect, "cached_hardware", _counting_cached_hardware)
    info = hw_detect.HardwareInfo(
        gpus=[hw_detect.GpuDevice(name="NVIDIA GeForce RTX 4090", vram_gb=24.0)],
        total_vram_gb=24.0, bandwidth_gb_s=1008.0, detected_at=0.0)
    result = fit.verdict("text-generation", "org/m", size_gb=10.0, hardware=info)
    assert result is not None
    assert result["runMode"] == "gpu"  # the threaded reading was actually used
    assert len(calls) == 0


def test_a_multi_row_catalog_request_reads_hardware_ONCE_not_per_row(monkeypatch):
    """Pins the improvement itself, not just unchanged behaviour: a caller
    answering MANY catalog entries in one request (`ai_runtime.
    describe_catalog`'s own shape, mirroring how `footprint_store` is
    loaded once and threaded through every `fit.verdict` call) reads
    `hw_detect.cached_hardware()` exactly ONCE regardless of how many
    entries it answers for — a test that only checked the verdict came out
    right on each row would pass equally well against the N-reads-per-row
    bug this test exists to catch."""
    calls = []
    real_cached_hardware = hw_detect.cached_hardware

    def _counting_cached_hardware():
        calls.append(1)
        return real_cached_hardware()

    monkeypatch.setattr(hw_detect, "cached_hardware", _counting_cached_hardware)

    # The router's own shape: one read up front, threaded through every
    # per-entry `fit.verdict` call — see `ai_runtime.describe_catalog`.
    hardware = hw_detect.cached_hardware()
    assert len(calls) == 1

    for i in range(5):
        result = fit.verdict("text-generation", f"org/model-{i}", size_gb=4.0 + i,
                             hardware=hardware)
        assert result is not None

    assert len(calls) == 1, f"expected exactly one read, got {len(calls)}"


# -- SPEC AI-19 item 7: Gaussian fit score ------------------------------------------


def test_score_is_100_at_or_under_the_comfort_ratio():
    assert fit._fit_score(0.70 * 24e9, 24e9) == 100.0
    assert fit._fit_score(0.30 * 24e9, 24e9) == 100.0


def test_score_is_zero_once_the_footprint_exceeds_the_pool():
    assert fit._fit_score(25e9, 24e9) == 0.0


def test_score_eases_down_smoothly_past_comfort_with_no_cliff():
    """The old `EASY_FRACTION = 0.6` step function scored every ratio on one
    side of 60% identically and every ratio on the other side identically —
    a discontinuity a UI bar cannot render honestly. The Gaussian replacement
    must not reproduce that: two ratios a hair apart score a hair apart."""
    just_under = fit._fit_score(0.80 * 24e9, 24e9)
    just_over = fit._fit_score(0.81 * 24e9, 24e9)
    assert 0 < just_over < just_under < 100
    assert (just_under - just_over) < 5.0  # no cliff


def test_score_decreases_monotonically_with_utilization_past_comfort():
    ratios = [0.70, 0.80, 0.90, 1.00]
    scores = [fit._fit_score(r * 24e9, 24e9) for r in ratios]
    assert scores == sorted(scores, reverse=True)


def test_verdict_is_easy_iff_score_is_100():
    # 32GB RAM, 8GB reserve -> 24GB usable; 70% of that is 16.8GB.
    result = fit.verdict("text-generation", "org/m", size_gb=16.3)  # +0.5GB overhead = 16.8GB
    assert result is not None
    assert result["score"] == 100.0
    assert result["verdict"] == "easy"


def test_verdict_is_tight_when_score_is_between_zero_and_100():
    result = fit.verdict("text-generation", "org/m", size_gb=19.5)  # +0.5 = 20GB, ratio 0.833
    assert result is not None
    assert 0 < result["score"] < 100
    assert result["verdict"] == "tight"


def test_verdict_is_no_when_score_is_zero():
    result = fit.verdict("text-generation", "org/m", size_gb=30.0)
    assert result is not None
    assert result["score"] == 0.0
    assert result["verdict"] == "no"


# -- available_budget_bytes: the reusable ceiling for a download-time picker
# (SPEC AI-24 item 14 wiring) -------------------------------------------------
#
# The ONE memory-budget notion this codebase computes -- RESERVE_BYTES, the
# Apple wired-limit ceiling, and VRAM-vs-RAM pool selection via _select_pool
# -- exposed as a plain number a caller with no specific footprint yet (a
# GGUF file picker, deciding WHICH file to fetch in the first place) can
# reuse, rather than inventing a second notion of "how much memory do I
# have" that could disagree with fit.verdict's own answer.


def test_available_budget_bytes_is_ram_minus_reserve_with_no_gpu(monkeypatch):
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 32.0)
    monkeypatch.setattr(fit, "_wired_limit_bytes", lambda ram_gb: None)
    budget = fit.available_budget_bytes(hardware=None)
    assert budget == 32.0 * fit.GB_BYTES - fit.RESERVE_BYTES


def test_available_budget_bytes_is_the_combined_offload_ceiling_with_a_discrete_gpu(monkeypatch):
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 32.0)
    monkeypatch.setattr(fit, "_wired_limit_bytes", lambda ram_gb: None)
    info = hw_detect.HardwareInfo(
        gpus=[hw_detect.GpuDevice(name="NVIDIA GeForce RTX 4090", vram_gb=24.0)],
        total_vram_gb=24.0, bandwidth_gb_s=1008.0, detected_at=0.0)
    budget = fit.available_budget_bytes(hardware=info)
    # The COMBINED VRAM+RAM offload ceiling -- the true maximum this machine
    # could ever hold via _offload_schedule's CPU-offload backoff, not just
    # the VRAM-alone figure a specific small footprint might fit inside.
    expected = 24.0 * fit.GB_BYTES + (32.0 * fit.GB_BYTES - fit.RESERVE_BYTES)
    assert budget == expected


def test_available_budget_bytes_respects_the_apple_wired_limit_ceiling(monkeypatch):
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 32.0)
    usable = 32.0 * fit.GB_BYTES - fit.RESERVE_BYTES
    monkeypatch.setattr(fit, "_wired_limit_bytes", lambda ram_gb: usable / 2)
    budget = fit.available_budget_bytes(hardware=None)
    assert budget == usable / 2


def test_available_budget_bytes_is_none_when_ram_is_unreadable(monkeypatch):
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: None)
    assert fit.available_budget_bytes(hardware=None) is None


def test_available_budget_bytes_reads_the_hardware_cache_itself_by_default(monkeypatch):
    calls = []
    real_cached_hardware = hw_detect.cached_hardware

    def _counting(*a, **k):
        calls.append(1)
        return real_cached_hardware()

    monkeypatch.setattr(hw_detect, "cached_hardware", _counting)
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 16.0)
    fit.available_budget_bytes()
    assert len(calls) == 1


def test_available_budget_bytes_does_not_read_the_cache_again_when_threaded_through(monkeypatch):
    calls = []
    real_cached_hardware = hw_detect.cached_hardware

    def _counting(*a, **k):
        calls.append(1)
        return real_cached_hardware()

    monkeypatch.setattr(hw_detect, "cached_hardware", _counting)
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 16.0)
    fit.available_budget_bytes(hardware=None)
    assert len(calls) == 0
