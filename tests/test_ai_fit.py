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

from fused_render.ai import fit, footprints


@pytest.fixture(autouse=True)
def _no_real_platform(monkeypatch):
    """Every test controls RAM and the wired limit explicitly — never the
    real host's. Defaults: 32GB RAM, no Apple-Silicon ceiling in play."""
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 32.0)
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: None)


@pytest.fixture(autouse=True)
def _isolated_footprints(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


# -- the precedence ladder ---------------------------------------------------------


def test_download_is_the_floor_when_nothing_else_is_known():
    result = fit.verdict("text-generation", "org/m", size_gb=4.0)
    assert result["basis"] == "download"
    assert result["footprintBytes"] == 4.0 * 1e9


def test_declared_wins_over_download():
    result = fit.verdict("text-generation", "org/m", size_gb=4.0, resident_gb=6.0)
    assert result["basis"] == "declared"
    assert result["footprintBytes"] == 6.0 * 1e9


def test_measured_wins_over_declared_and_download():
    footprints.record("text-generation", "org/m", 5_000_000_000)
    result = fit.verdict("text-generation", "org/m", size_gb=4.0, resident_gb=6.0)
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
    assert result["basis"] == "download"


# -- headroom thresholds (AI-16b) ---------------------------------------------------


def test_easy_is_within_60_percent_of_the_usable_budget():
    # 32GB RAM, 8GB reserve -> 24GB usable, 60% of that is 14.4GB.
    result = fit.verdict("text-generation", "org/m", size_gb=14.0)
    assert result["verdict"] == "easy"


def test_tight_is_between_the_easy_fraction_and_the_usable_budget():
    # 24GB usable; 20GB is past 60% (14.4GB) but within the full 24GB.
    result = fit.verdict("text-generation", "org/m", size_gb=20.0)
    assert result["verdict"] == "tight"


def test_no_is_past_the_usable_budget():
    # 24GB usable; 30GB exceeds it even before any wired-limit gate.
    result = fit.verdict("text-generation", "org/m", size_gb=30.0)
    assert result["verdict"] == "no"


def test_thresholds_scale_with_ram_not_a_flat_fraction(monkeypatch):
    """The whole point of moving off 25%/50%-of-total: a 64GB machine must
    not leave 32GB unusable for no stated reason."""
    monkeypatch.setattr(fit, "machine_ram_gb", lambda: 64.0)
    # 64GB - 8GB reserve = 56GB usable; 40GB is within it but past 60% (33.6GB).
    result = fit.verdict("text-generation", "org/m", size_gb=40.0)
    assert result["verdict"] == "tight"


def test_a_measured_no_is_reachable_and_not_a_contradiction():
    """AI-16c: the footprint store only ever holds models that ran — a
    measured 'no' means it ran while nothing else was competing for memory,
    not that the number is wrong."""
    footprints.record("text-generation", "org/m", 30_000_000_000)
    result = fit.verdict("text-generation", "org/m")
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
    assert result["verdict"] == "no"


def test_wired_limit_zero_means_the_apple_default_not_unset(monkeypatch):
    """Apple's own documented meaning of 0: no explicit limit, so the kernel
    enforces its default (~75% of RAM) — not 'no ceiling at all'."""
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: 0)
    # 32GB * 0.75 = 24GB default ceiling. 26GB clears headroom's usable-budget
    # gate too (past 24GB usable) so this exercises the wired branch is at
    # least as strict, not that it alone decided "no".
    result = fit.verdict("text-generation", "org/m", size_gb=26.0)
    assert result["verdict"] == "no"


def test_an_unreadable_wired_limit_costs_the_gate_never_the_verdict(monkeypatch):
    """None (off Darwin, or a failed read) must not manufacture a 'no' —
    only the headroom arithmetic decides in that case."""
    monkeypatch.setattr(fit, "_wired_limit_mb", lambda: None)
    result = fit.verdict("text-generation", "org/m", size_gb=14.0)
    assert result["verdict"] == "easy"


# -- footprint_bytes as its own unit ------------------------------------------------


def test_footprint_bytes_ignores_a_zero_or_negative_resident_gb():
    bytes_, basis = fit.footprint_bytes("text-generation", "org/m", size_gb=4.0,
                                        resident_gb=0)
    assert basis == "download" and bytes_ == 4.0 * 1e9


def test_footprint_bytes_ignores_a_bool_masquerading_as_a_number():
    """`isinstance(True, int)` is True in Python — a stray `resident_gb: true`
    from a malformed catalog entry must not be read as `resident_gb: 1`."""
    bytes_, basis = fit.footprint_bytes("text-generation", "org/m", size_gb=4.0,
                                        resident_gb=True)
    assert basis == "download"
