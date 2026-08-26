"""Tests for the measured-footprint store (SPEC AI-16a, D497).

`ai/footprints.py` at ~/.fused-render/ai_footprints.json — the shape
`bench_store.py` establishes, mirrored: a corrupt or absent file reads as no
observations, keyed by `<capability>/<model_id>`, bounded at MAX_MODELS, and
discarded WHOLESALE the moment the recorded machine identity no longer
matches this one.

FUSED_RENDER_HOME is redirected exactly as tests/test_ai_benchmark_store.py
does it, so no test reads or writes a developer's real store.
"""
import json

from fused_render.ai import benchmark, footprints


def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


def _pin_machine(monkeypatch, **overrides):
    """Freeze `benchmark.machine()` to a known identity, so a test can control
    exactly what "the same machine" means without depending on the real host."""
    identity = {"platform": "Darwin", "arch": "arm64", "cpuCount": 8,
                "totalMemoryBytes": 34_000_000_000, **overrides}
    monkeypatch.setattr(benchmark, "machine", lambda: dict(identity))
    return identity


# -- reading an empty or absent store ---------------------------------------------


def test_empty_store_reads_as_no_observation(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    assert footprints.read("text-generation", "org/m") is None


def test_a_corrupt_file_reads_as_no_observation(tmp_path, monkeypatch):
    """Same contract `storage.read_json` already gives — absent OR corrupt
    reads as None — restated because `fit.py`'s ladder depends on silence
    rather than a 500 on the AI Models page."""
    home = _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    home.mkdir(parents=True)
    (home / "ai_footprints.json").write_text("{not json", encoding="utf-8")
    assert footprints.read("text-generation", "org/m") is None
    # And a record over the corruption still lands, rather than raising.
    footprints.record("text-generation", "org/m", 6_000_000_000)
    assert footprints.read("text-generation", "org/m") == 6_000_000_000


def test_a_wrong_shaped_file_reads_as_no_observation(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    home.mkdir(parents=True)
    (home / "ai_footprints.json").write_text(json.dumps(["not", "a", "dict"]),
                                             encoding="utf-8")
    assert footprints.read("text-generation", "org/m") is None


# -- record/read round trip --------------------------------------------------------


def test_record_then_read_round_trips(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    footprints.record("text-generation", "org/m", 6_000_000_000)
    assert footprints.read("text-generation", "org/m") == 6_000_000_000


def test_keyed_by_capability_AND_model_not_by_repo_alone(tmp_path, monkeypatch):
    """SPEC AI-16a: since AI-11j the same checkpoint can serve two
    capabilities with two different footprints — a vision-tower load is not
    the load that skips it."""
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    footprints.record("text-generation", "org/m", 4_000_000_000)
    footprints.record("image-to-text", "org/m", 9_000_000_000)
    assert footprints.read("text-generation", "org/m") == 4_000_000_000
    assert footprints.read("image-to-text", "org/m") == 9_000_000_000


def test_a_non_positive_or_non_int_reading_is_ignored(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    footprints.record("text-generation", "org/m", 0)
    footprints.record("text-generation", "org/m", -5)
    footprints.record("text-generation", "org/m", 4.5)  # not an int
    assert footprints.read("text-generation", "org/m") is None


# -- the high-water rule -----------------------------------------------------------


def test_a_reading_that_grows_the_peak_is_recorded(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    footprints.record("text-generation", "org/m", 4_000_000_000)
    footprints.record("text-generation", "org/m", 8_000_000_000)
    assert footprints.read("text-generation", "org/m") == 8_000_000_000


def test_a_reading_smaller_than_the_peak_does_not_lower_it(tmp_path, monkeypatch):
    """A high-water mark, not a last-seen value: `low_memory=True` frees a
    stage between renders, and a smaller LATER reading must not erase what
    the model actually cost at its worst."""
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    footprints.record("text-generation", "org/m", 8_000_000_000)
    footprints.record("text-generation", "org/m", 1_000_000_000)
    assert footprints.read("text-generation", "org/m") == 8_000_000_000


def test_jitter_within_tolerance_does_not_rewrite_the_file(tmp_path, monkeypatch):
    """A reading that does not clear `_GROWTH_TOLERANCE` past the existing
    peak is not written — otherwise a resident figure jittering by a few
    bytes across polls would rewrite the file on every `/health` read."""
    home = _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    footprints.record("text-generation", "org/m", 10_000_000_000)
    path = home / "ai_footprints.json"
    written_at = path.stat().st_mtime_ns

    footprints.record("text-generation", "org/m", 10_050_000_000)  # +0.5%, within tolerance

    assert path.stat().st_mtime_ns == written_at
    assert footprints.read("text-generation", "org/m") == 10_000_000_000


# -- the machine identity gate ------------------------------------------------------


def test_a_footprint_from_a_DIFFERENT_machine_is_not_read(tmp_path, monkeypatch):
    """AI-16a: 'a home directory gets restored onto a new laptop' — every
    number in the file was measured on the recorded machine, so a mismatch
    discards it wholesale rather than trying to reconcile row by row."""
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch, totalMemoryBytes=34_000_000_000)
    footprints.record("text-generation", "org/m", 6_000_000_000)
    assert footprints.read("text-generation", "org/m") == 6_000_000_000

    _pin_machine(monkeypatch, totalMemoryBytes=16_000_000_000)  # a different machine now
    assert footprints.read("text-generation", "org/m") is None


def test_writing_on_a_new_machine_starts_fresh_rather_than_raising(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch, totalMemoryBytes=34_000_000_000)
    footprints.record("text-generation", "org/m", 6_000_000_000)

    _pin_machine(monkeypatch, totalMemoryBytes=16_000_000_000)
    footprints.record("text-generation", "org/n", 2_000_000_000)
    assert footprints.read("text-generation", "org/n") == 2_000_000_000
    # The OLD machine's row is gone, not merely unreadable through this identity.
    assert footprints.read("text-generation", "org/m") is None


def test_cpu_count_alone_is_not_part_of_the_identity(tmp_path, monkeypatch):
    """A VM reconfigured with a different core count, same RAM and arch, is
    still the same memory budget this file is about — see the module
    docstring for why `cpuCount` is excluded from `_IDENTITY_KEYS`."""
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch, cpuCount=8)
    footprints.record("text-generation", "org/m", 6_000_000_000)

    _pin_machine(monkeypatch, cpuCount=4)
    assert footprints.read("text-generation", "org/m") == 6_000_000_000


# -- bounded by construction ---------------------------------------------------------


def test_the_store_is_capped_and_drops_the_oldest_first(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    monkeypatch.setattr(footprints, "MAX_MODELS", 3)
    for i in range(5):
        footprints.record("text-generation", f"org/m{i}", 1_000_000_000 + i)
    kept = [f"org/m{i}" for i in range(5)
           if footprints.read("text-generation", f"org/m{i}") is not None]
    assert kept == ["org/m2", "org/m3", "org/m4"]


def test_a_malformed_row_does_not_crash_bounding_an_over_cap_store(tmp_path, monkeypatch):
    """Code review on AI-16a: `_bounded` sorts on `kv[1].get("observedAt", 0)`,
    which assumes every row is a dict — but `_load` never validates row
    SHAPE, only the envelope (`data`, `machine`, `models`). A hand-edited or
    partially-written file with one non-dict row must still be boundable
    once the store is past `MAX_MODELS`, the same way `peak_from_store`
    already tolerates a non-dict row by returning None for it rather than
    raising."""
    home = _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    monkeypatch.setattr(footprints, "MAX_MODELS", 3)
    home.mkdir(parents=True)
    machine = benchmark.machine()
    path = home / "ai_footprints.json"
    path.write_text(json.dumps({
        "version": footprints.VERSION,
        "machine": machine,
        "models": {
            "text-generation/org/malformed": "not a dict",
            "text-generation/org/m0": {"peakBytes": 1_000_000_000, "observedAt": 1},
            "text-generation/org/m1": {"peakBytes": 2_000_000_000, "observedAt": 2},
            "text-generation/org/m2": {"peakBytes": 3_000_000_000, "observedAt": 3},
        },
    }), encoding="utf-8")
    # Recording one more reading forces `_bounded` to run over a
    # store that already has a malformed row in it.
    footprints.record("text-generation", "org/m3", 4_000_000_000)
    assert footprints.read("text-generation", "org/m3") == 4_000_000_000
    assert footprints.read("text-generation", "org/malformed") is None


# -- clear -----------------------------------------------------------------------------


def test_clear_forgets_every_measurement(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    _pin_machine(monkeypatch)
    footprints.record("text-generation", "org/m", 6_000_000_000)
    footprints.clear()
    assert footprints.read("text-generation", "org/m") is None
