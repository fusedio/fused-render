"""Tests for `ai/speed.py` — a tok/s speed estimate with a recorded basis,
and this machine's own local calibration of it (SPEC AI-21 items 9 and 11 of
the fit/categorization/download overhaul).

`hw_detect.cached_hardware` and `fit.is_apple_silicon` are monkeypatched
directly on the `speed` module's own imported references, the same style
`test_ai_fit.py` already uses for `fit._wired_limit_mb` — no test depends on
the real host's hardware or platform. `FUSED_RENDER_HOME` is redirected so no
test reads or writes a developer's real calibration store.
"""
import pytest

from fused_render.ai import bench_store, hw_detect, registry, speed


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def _no_real_hardware(monkeypatch):
    """Default: no cached hardware probe yet, not Apple Silicon — every test
    that cares overrides one or both explicitly."""
    monkeypatch.setattr(speed.hw_detect, "cached_hardware", lambda: None)
    monkeypatch.setattr(speed.fit, "is_apple_silicon", lambda: False)


def _hardware(name, vram_gb=24.0, bandwidth_gb_s=None, unified=False):
    return hw_detect.HardwareInfo(
        gpus=[hw_detect.GpuDevice(name=name, vram_gb=vram_gb, unified_memory=unified)],
        total_vram_gb=vram_gb, bandwidth_gb_s=bandwidth_gb_s, detected_at=0.0)


# -- backend_bucket -----------------------------------------------------------------


def test_backend_bucket_is_metal_mlx_on_apple_unified_regardless_of_hardware():
    assert speed.backend_bucket(None, is_apple_unified=True) == "metal-mlx"
    assert speed.backend_bucket(
        _hardware("Apple M3 Max"), is_apple_unified=True) == "metal-mlx"


def test_backend_bucket_detects_nvidia_by_name():
    hw = _hardware("NVIDIA GeForce RTX 4090")
    assert speed.backend_bucket(hw, is_apple_unified=False) == "cuda"


def test_backend_bucket_detects_amd_by_name():
    hw = _hardware("AMD Radeon RX 7900 XTX")
    assert speed.backend_bucket(hw, is_apple_unified=False) == "rocm"


def test_backend_bucket_falls_back_to_cpu_arch_with_no_gpu(monkeypatch):
    monkeypatch.setattr(speed.platform, "machine", lambda: "arm64")
    assert speed.backend_bucket(None, is_apple_unified=False) == "cpu-arm"
    monkeypatch.setattr(speed.platform, "machine", lambda: "x86_64")
    assert speed.backend_bucket(None, is_apple_unified=False) == "cpu-x86"


def test_backend_bucket_falls_back_to_cpu_arch_when_gpu_name_is_unrecognized(
        monkeypatch):
    monkeypatch.setattr(speed.platform, "machine", lambda: "x86_64")
    hw = _hardware("Some Future Card 9000")
    assert speed.backend_bucket(hw, is_apple_unified=False) == "cpu-x86"


# -- estimate_tok_s -------------------------------------------------------------------


def test_estimate_returns_none_when_nothing_is_known_about_the_weight_size():
    assert speed.estimate_tok_s(None, params=None, quantization=None) is None


def test_estimate_uses_bandwidth_when_hardware_reports_it(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA GeForce RTX 4090", bandwidth_gb_s=400.0))
    result = speed.estimate_tok_s(8.0)  # size_gb=8, no params/quantization
    assert result is not None
    assert result["method"] == "bandwidth"
    assert result["backend"] == "cuda"
    assert result["bandwidthGbS"] == 400.0
    assert result["tokensPerSecond"] == pytest.approx(400.0 / 8.0 * speed.BANDWIDTH_FRACTION)
    assert result["contextTokens"] == speed.fit.KV_CACHE_CONTEXT_TOKENS
    assert result["calibrated"] is False
    assert result["calibrationFactor"] is None


def test_estimate_falls_back_to_a_backend_constant_when_bandwidth_is_unknown(
        monkeypatch):
    # A GPU is present but its name is not in hw_detect's bandwidth table.
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("Some Future Card 9000", bandwidth_gb_s=None))
    monkeypatch.setattr(speed.platform, "machine", lambda: "x86_64")
    result = speed.estimate_tok_s(8.0)
    assert result is not None
    assert result["method"] == "backend-constant"
    assert result["backend"] == "cpu-x86"
    assert result["bandwidthGbS"] is None
    assert result["tokensPerSecond"] == speed.PER_BACKEND_TOK_S["cpu-x86"]


def test_estimate_prefers_a_recognized_quant_params_x_bpp_weight_size(monkeypatch):
    """The weight size behind the estimate is `fit.weight_bytes`'s own
    figure, not a bare `size_gb` reading — proven by giving it BOTH a
    recognized quantization and a divergent `size_gb`, and checking the
    tok/s number reflects the SMALLER, `params x bpp`-derived weight."""
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    result = speed.estimate_tok_s(999.0, params="4B", quantization="MLX 4-bit")
    assert result is not None
    weight_gb = (4e9 * speed.fit.QUANT_BYTES_PER_PARAM["mlx_4bit"]) / speed.fit.GB_BYTES
    assert result["tokensPerSecond"] == pytest.approx(
        1555.0 / weight_gb * speed.BANDWIDTH_FRACTION)


# -- hardware threading (code review: don't re-read per catalog row) ---------------


def test_estimate_reads_the_hardware_cache_itself_when_none_is_threaded_through(
        monkeypatch):
    """A lone caller (no `hardware=` passed) still gets a correct estimate —
    `estimate_tok_s` reads `hw_detect.cached_hardware()` itself exactly once
    per call, the same "do your own single read" fallback `fit.verdict`'s
    `hardware=` parameter already has (and the identical shape
    `footprint_store` established first)."""
    calls = []
    real = speed.hw_detect.cached_hardware

    def _counting():
        calls.append(1)
        return real()

    # `_no_real_hardware` (autouse) already stubbed `cached_hardware` to a
    # constant `None` lambda — replace THAT with a counting wrapper around
    # the SAME stub, not the real probe, so this stays isolated from the
    # actual host's hardware.
    monkeypatch.setattr(speed.hw_detect, "cached_hardware", _counting)
    result = speed.estimate_tok_s(8.0)
    assert result is not None
    assert len(calls) == 1


def test_estimate_does_not_read_the_hardware_cache_again_when_one_is_threaded_through(
        monkeypatch):
    """Code review: `estimate_tok_s` used to call `hw_detect.cached_hardware()`
    itself on every invocation — a fresh `storage.read_json` open and JSON
    parse per catalog ROW, on the exact route `fit.verdict`'s own
    `hardware=` parameter was already threaded through to fix. A caller that
    already has a reading in hand (`ai_runtime.describe_catalog`'s shape)
    must not trigger a second read — `hardware=` passed explicitly is used
    AS-IS, with zero further `hw_detect.cached_hardware()` calls."""
    calls = []
    real = speed.hw_detect.cached_hardware

    def _counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(speed.hw_detect, "cached_hardware", _counting)
    info = _hardware("NVIDIA A100", bandwidth_gb_s=1555.0)
    result = speed.estimate_tok_s(8.0, hardware=info)
    assert result is not None
    assert result["bandwidthGbS"] == 1555.0  # the threaded reading was actually used
    assert len(calls) == 0


def test_many_estimates_in_one_request_read_hardware_ONCE_not_per_call(monkeypatch):
    """Pins the improvement itself, not just unchanged behaviour — mirrors
    `test_ai_fit.py::test_a_multi_row_catalog_request_reads_hardware_ONCE_
    not_per_row` for the identical bug in `speed.py`: a caller answering
    MANY catalog entries in one request reads `hw_detect.cached_hardware()`
    exactly once regardless of how many `estimate_tok_s` calls it makes — a
    test that only checked each estimate came out right would pass equally
    well against the N-reads-per-row bug this test exists to catch."""
    calls = []
    real = speed.hw_detect.cached_hardware

    def _counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(speed.hw_detect, "cached_hardware", _counting)

    # The router's own shape: one read up front, threaded through every
    # per-entry `estimate_tok_s` call.
    hardware = speed.hw_detect.cached_hardware()
    assert len(calls) == 1

    for i in range(5):
        result = speed.estimate_tok_s(4.0 + i, hardware=hardware)
        assert result is not None

    assert len(calls) == 1, f"expected exactly one read, got {len(calls)}"


# -- calibration (SPEC item 11) -------------------------------------------------------


def _run(model="org/big-model", tok_s=42.0, ok=True):
    return {"capability": registry.TEXT_GENERATION, "model": model, "ok": ok,
           "metrics": {"tokensPerSecond": tok_s}}


def _catalog_entry(size_gb=8.0, params="8B", quantization="GGUF Q4_K_M"):
    return {"size_gb": size_gb, "params": params, "quantization": quantization}


def test_recalibrate_computes_the_clamped_median_ratio_and_persists_it(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    weight_bytes = speed.fit.weight_bytes(
        entry["size_gb"], entry["params"], entry["quantization"])
    assert weight_bytes is not None
    weight_gb = weight_bytes / speed.fit.GB_BYTES
    base = 1555.0 / weight_gb * speed.BANDWIDTH_FRACTION
    # Two runs of the SAME model at different measured speeds — the median
    # of two ratios is their mean here, chosen so the expected factor is easy
    # to hand-verify.
    runs = [_run(tok_s=base * 0.5), _run(tok_s=base * 0.7)]
    factor = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: entry)
    assert factor == pytest.approx(0.6)
    assert speed.stored_calibration_factor() == pytest.approx(0.6)


def test_recalibrate_clamps_an_extreme_ratio(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    # A wildly fast measured run relative to the base estimate — the formula
    # is a rough proxy and a real anomalous run must not blow the factor past
    # SPEC item 11's own [0.05, 3.0] clamp.
    runs = [_run(tok_s=1_000_000.0)]
    factor = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: entry)
    assert factor == speed.CALIBRATION_MAX


def test_recalibrate_is_idempotent_across_repeated_calls(monkeypatch):
    """SPEC item 11's own required property: applying calibration repeatedly
    over the SAME evidence must stop moving, not compound. Satisfied by
    construction here — every ratio is computed against the UNCALIBRATED
    base estimate, never against `estimate_tok_s`'s own (possibly already
    calibrated) output — so the stored factor never feeds into the
    computation of its own replacement, and there is nothing to "divide
    out": it was never let in."""
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    runs = [_run(tok_s=30.0), _run(tok_s=35.0)]
    first = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: entry)
    second = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: entry)
    third = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: entry)
    assert first == second == third
    # And the stored value itself is not drifting between calls either.
    assert speed.stored_calibration_factor() == first


def test_recalibrate_excludes_moe_rows(monkeypatch):
    """llmfit's own anchor rule (SPEC item 11): `params_b >= 1.0 and not
    is_moe`. An MoE row's catalog `params` carries the "(~Xb active)"
    qualifier — the same string `fit.parse_params` itself special-cases."""
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    moe_entry = _catalog_entry(params="8B (~1B active)")
    runs = [_run(model="org/moe-model", tok_s=999.0)]
    factor = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: moe_entry)
    # No anchor survives the MoE exclusion, so nothing was stored — the
    # function answers whatever was already there (nothing, here).
    assert factor is None
    assert speed.stored_calibration_factor() is None


def test_recalibrate_excludes_models_under_the_billion_parameter_floor(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    tiny_entry = _catalog_entry(params="244M")
    runs = [_run(model="org/tiny-model", tok_s=999.0)]
    factor = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: tiny_entry)
    assert factor is None


def test_recalibrate_ignores_a_failed_run(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    runs = [_run(tok_s=999.0, ok=False)]
    factor = speed.recalibrate(runs=runs, catalog_lookup=lambda _id: entry)
    assert factor is None


def test_recalibrate_ignores_a_non_text_generation_capability(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    run = _run(tok_s=999.0)
    run["capability"] = registry.EMBEDDINGS
    factor = speed.recalibrate(runs=[run], catalog_lookup=lambda _id: entry)
    assert factor is None


def test_recalibrate_with_no_anchors_leaves_the_stored_value_untouched(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    speed.recalibrate(runs=[_run(tok_s=30.0)], catalog_lookup=lambda _id: entry)
    stored_before = speed.stored_calibration_factor()
    assert stored_before is not None
    # A second recalibration with nothing to anchor on (no runs at all) must
    # not clobber the earlier real one with None/1.0.
    result = speed.recalibrate(runs=[], catalog_lookup=lambda _id: entry)
    assert result == stored_before
    assert speed.stored_calibration_factor() == stored_before


def test_estimate_applies_the_stored_calibration_factor(monkeypatch):
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    speed.recalibrate(runs=[_run(tok_s=30.0)], catalog_lookup=lambda _id: entry)
    factor = speed.stored_calibration_factor()
    assert factor is not None
    uncalibrated = speed.estimate_tok_s(
        entry["size_gb"], params=entry["params"], quantization=entry["quantization"])
    assert uncalibrated is not None
    # Read back with no factor at all, for comparison.
    import fused_render.shell.storage as storage_module
    storage_module.write_json(speed._path(), {"version": speed.VERSION, "factor": None})
    assert speed.stored_calibration_factor() is None
    baseline = speed.estimate_tok_s(
        entry["size_gb"], params=entry["params"], quantization=entry["quantization"])
    assert baseline is not None
    assert uncalibrated["calibrated"] is True
    assert uncalibrated["calibrationFactor"] == factor
    assert baseline["calibrated"] is False
    assert uncalibrated["tokensPerSecond"] == pytest.approx(
        baseline["tokensPerSecond"] * factor)


def test_recalibrate_reads_bench_store_by_default(monkeypatch):
    """No `runs=` override: the real production path reads `bench_store.read()`."""
    monkeypatch.setattr(speed.hw_detect, "cached_hardware",
                        lambda: _hardware("NVIDIA A100", bandwidth_gb_s=1555.0))
    entry = _catalog_entry()
    bench_store.append({
        "id": "run1", "capability": registry.TEXT_GENERATION, "model": "org/big-model",
        "ok": True, "metrics": {"tokensPerSecond": 30.0}, "workload": {},
    })
    factor = speed.recalibrate(catalog_lookup=lambda _id: entry)
    assert factor is not None
