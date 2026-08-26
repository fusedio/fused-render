"""SPEC AI-22, D525: `resident_gb` populated for the two `ltx-video` rows.

`resident_gb` is the `declared` rung of `fit.py`'s precedence ladder — an
optional, curator-supplied resident-footprint estimate that outranks the
`download` rung (`size_gb`, or a `params x bpp` guess). It was plumbed
end-to-end (`fit.py`, `ai_runtime.describe_catalog`) since SPEC AI-16 and set
by NOTHING: the ladder's second rung was dead.

**Why only these two rows, and why THESE are safe to seed.** `fit.py`'s own
module docstring names the exact case `resident_gb` exists to fix: `ltx-video`
runs `DistilledPipeline(low_memory=True)`, which frees the transformer and the
Gemma-3 text encoder between stages, so the model's true resident PEAK is one
stage — the larger of the two components, never their sum — while `size_gb`
(correctly, per this file's own rule) reports every byte BOTH downloads fetch.
The two component byte counts are not a guess: they are the exact,
Hub-measured figures `catalog.py`'s own comment above these two entries
already states (2026-08-23), so `resident_gb` here is `max(component bytes) /
1e9`, rounded to the same one decimal `size_gb` uses — real evidence, derived
from numbers already curated in this file, not a fresh estimate invented for
this rung. That is the bar SPEC AI-22 sets: a `resident_gb` value only
belongs on a row when it is BETTER evidence than `size_gb`, and every other
row in this file is a single-stage pipeline (a Diffusers/mflux/GGUF/mlx text,
image or embedding load, all of whose components are resident together
through the whole run) where `size_gb` and the true resident footprint are
already the same figure — seeding `resident_gb` there would add no
information, only a second number to drift out of sync with the first. See
this test file's own assertions for why NEITHER of those two things is being
skipped by accident.
"""
from fused_render.ai import catalog, fit


def _entry(model_id: str) -> dict:
    for entry in catalog.SUGGESTIONS["ltx-video"]:
        if entry["id"] == model_id:
            return entry
    raise AssertionError(f"{model_id!r} not found in the ltx-video suggestions")


def test_the_int4_tier_declares_the_larger_single_stage_not_the_download_sum():
    entry = _entry("dgrauet/ltx-2.3-mlx-q4")
    # 20,479,309,067 B weights vs. 8,068,021,302 B Gemma-3 — the weights stage
    # is the larger of the two, and `DistilledPipeline` never holds both at
    # once, so the true peak is the weights figure alone.
    assert entry["resident_gb"] == 20.5
    assert entry["size_gb"] == 28.5
    assert entry["resident_gb"] < entry["size_gb"]


def test_the_int8_tier_declares_the_larger_single_stage_not_the_download_sum():
    entry = _entry("dgrauet/ltx-2.3-mlx-q8")
    # 29,754,496,331 B weights vs. the SAME 8,068,021,302 B Gemma-3 repo —
    # weights again the larger stage.
    assert entry["resident_gb"] == 29.8
    assert entry["size_gb"] == 37.8
    assert entry["resident_gb"] < entry["size_gb"]


def test_no_single_stage_pipeline_entry_declares_a_resident_gb():
    """Every OTHER curated row is a pipeline whose components are resident
    TOGETHER for the whole run — `size_gb` already IS the true resident
    figure there, so a `resident_gb` on any of them would be a second copy
    of the same number with no better evidence behind it than the one
    `fit.py`'s `download` rung already reads. This is the "don't seed a row
    you cannot justify as better evidence" half of SPEC AI-22 — pinned as a
    test so a future edit has to make the same case in words, not add a
    plausible-looking number by habit."""
    for capability_entries in catalog.SUGGESTIONS.values():
        for entry in capability_entries:
            if entry["id"] in ("dgrauet/ltx-2.3-mlx-q4", "dgrauet/ltx-2.3-mlx-q8"):
                continue
            assert "resident_gb" not in entry, (
                f"{entry['id']!r} declares a resident_gb with no documented "
                "evidentiary basis stronger than its own size_gb"
            )


def test_the_declared_rung_actually_reads_the_seeded_value():
    """Integration-shaped, over `fit.verdict` directly rather than through
    the router: proves the plumbing `ai_runtime.describe_catalog` already
    has (`entry.get("resident_gb")` passed straight to `fit.verdict`) turns
    this catalog data into a `declared`-basis verdict, not just a field that
    sits in the catalog unread."""
    entry = _entry("dgrauet/ltx-2.3-mlx-q4")
    footprint, basis = fit.footprint_bytes(
        "text-to-video", entry["id"], entry.get("size_gb"), entry.get("resident_gb"))
    assert basis == "declared"
    assert footprint == entry["resident_gb"] * fit.GB_BYTES
