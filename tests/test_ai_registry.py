"""The embeddings capability's own registration (SPEC §40) — the parts of
`registry.py` not already exercised through `ai_models.py`/`ai_runtime.py`'s
HTTP surface.

Platform gating is driven the same way `test_ai_models_api.py` drives it:
`monkeypatch.setattr(registry.platform, "system"/"machine", ...)` rather than
running on whatever machine CI happens to be.
"""
import os
import re

from fused_render.ai import registry, tasks


def _mac_arm(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")


def _windows(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")


def _linux(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")


def _runner(code):
    """`registry.by_code(code)`, asserted present.

    `by_code` returns `Runner | None` — real when a code is misspelled, but
    every code this file names is a row this test suite itself registers, so
    a `None` here is a real regression (a runner renamed or removed) and
    should fail LOUDLY on the missing code rather than as an opaque
    `AttributeError` two lines later on whatever attribute was read first.
    """
    runner = registry.by_code(code)
    assert runner is not None, code
    return runner


def test_embeddings_is_a_registered_capability():
    assert registry.EMBEDDINGS == "embeddings"
    assert registry.EMBEDDINGS in registry.capabilities()


def test_four_embedding_runners_are_registered():
    """Renamed from "...both_embedding_runners...": there are four rows now,
    not two — `transformers-embed` gained CUDA and ROCm siblings (see
    `test_the_cuda_and_rocm_embed_variants_are_registered_and_ordered_correctly`
    below for the decision this reverses)."""
    codes = {r.code for r in registry.all_runners() if r.capability == registry.EMBEDDINGS}
    assert codes == {"mlx-embed", "transformers-embed", "transformers-embed-cuda",
                     "transformers-embed-rocm"}


def test_mlx_embed_is_registered_before_every_torch_embed_row():
    """First-match-wins is the whole mechanism (see `registry.py`'s comment on
    the table): MLX must come first so an Apple Silicon machine resolves there
    by default, exactly like text generation and image generation. Widened
    from asserting the two-row list verbatim (it now has four rows) to the
    property this test actually cares about: MLX is first, ahead of every
    torch build.
    """
    codes = [r.code for r in registry.all_runners() if r.capability == registry.EMBEDDINGS]
    assert codes[0] == "mlx-embed"
    assert codes.index("mlx-embed") < codes.index("transformers-embed")


def test_mlx_embed_is_gated_to_apple_silicon(monkeypatch):
    _windows(monkeypatch)
    assert not _runner("mlx-embed").available().ok
    _linux(monkeypatch)
    assert not _runner("mlx-embed").available().ok
    _mac_arm(monkeypatch)
    assert _runner("mlx-embed").available().ok


def test_transformers_embed_runs_everywhere(monkeypatch):
    """The platform-agnostic row — `_torch_platform`, the gate the withdrawn
    `transformers-text` family used (D416), so wherever that CPU fallback ran,
    embeddings does too."""
    for setter in (_mac_arm, _windows, _linux):
        setter(monkeypatch)
        assert _runner("transformers-embed").available().ok


def test_apple_silicon_resolves_to_mlx_embed(monkeypatch):
    _mac_arm(monkeypatch)
    resolved = registry.for_capability(registry.EMBEDDINGS)
    assert resolved is not None and resolved.code == "mlx-embed"


def test_windows_resolves_to_transformers_embed(monkeypatch):
    _windows(monkeypatch)
    resolved = registry.for_capability(registry.EMBEDDINGS)
    assert resolved is not None and resolved.code == "transformers-embed"


def test_the_cuda_and_rocm_embed_variants_are_registered_and_ordered_correctly():
    """The decision this test used to pin — no accelerated embed variant,
    because a dual encoder is "too cheap to justify a second or third wheel"
    — was DELIBERATELY REVERSED on this branch, not overlooked. The speed
    argument for the TEXT tower still holds (one short sequence, milliseconds
    on a CPU), but an image tower run at `embed_common.MAX_ITEMS` (64) items
    per call is real work a GPU meaningfully speeds up, and a machine that
    already has a working NVIDIA or fully ROCm-capable AMD card was otherwise
    stuck running every one of those batches in fp32 on the CPU with no way to
    opt out. `transformers-embed-cuda`/`-rocm` are that opt-in, gated on the
    same `_cuda`/`_rocm` probes `diffusers-image-cuda`/`-rocm` use.

    Both sit BELOW `transformers-embed`, CUDA before ROCm — the same order the
    diffusers image family uses — so `auto` still resolves to the CPU/MPS row
    on every platform (`test_the_embeddings_capability_orders_mlx_then_cpu_then_cuda_then_rocm`
    in `test_ai_runtime.py` pins that through platform mocks; this test pins
    only the static ordering).
    """
    codes = [r.code for r in registry.all_runners() if r.capability == registry.EMBEDDINGS]
    assert codes == [
        "mlx-embed", "transformers-embed", "transformers-embed-cuda",
        "transformers-embed-rocm",
    ]


# The task-vocabulary tests that used to live here (the embeddings dual-encoder
# tag, the ruled-out-names trap, the no-label-in-both-tables completeness rule)
# moved to `test_ai_tasks.py` along with `_TASK_CAPABILITIES`/`NO_RUNNER_YET`
# themselves (D433) — this file keeps only what is actually about `registry.py`.


def test_video_generation_is_a_registered_capability():
    assert registry.VIDEO_GENERATION == "text-to-video"
    assert registry.VIDEO_GENERATION in registry.capabilities()


def test_ltx_video_is_the_only_registered_video_runner():
    """D468 dropped `h3-video`, the second row. Pinned as an exact set rather
    than a membership check because registry ORDER is this table's opt-in
    mechanism — a stray video row added above this one would silently take
    over `for_capability`."""
    codes = [r.code for r in registry.all_runners() if r.capability == registry.VIDEO_GENERATION]
    assert codes == ["ltx-video"]


def test_ltx_video_row_present_on_apple_silicon(monkeypatch):
    _mac_arm(monkeypatch)
    assert _runner("ltx-video")._available().ok


def test_ltx_video_row_absent_off_apple_silicon(monkeypatch):
    _windows(monkeypatch)
    status = _runner("ltx-video")._available()
    assert not status.ok
    assert "Apple Silicon" in status.reason

    _linux(monkeypatch)
    status = _runner("ltx-video")._available()
    assert not status.ok
    assert "Apple Silicon" in status.reason


def test_no_video_runner_is_available_off_apple_silicon(monkeypatch):
    """The capability's one row is gated on `_apple_silicon`, so a machine
    that is not one has no video capability at all — the property that makes
    video the first capability with no "everywhere" row."""
    _windows(monkeypatch)
    assert registry.for_capability(registry.VIDEO_GENERATION) is None


def test_catalog_defaults_to_the_ltx_entry_on_apple_silicon(monkeypatch):
    """`ltx-video` is the capability's one row and has its own curated
    shortlist, so a bare `fused.ai.video()` on Apple Silicon defaults to the
    smallest LTX-2.3 tier."""
    _mac_arm(monkeypatch)
    from fused_render.ai import catalog

    assert registry.for_capability(registry.VIDEO_GENERATION).code == "ltx-video"
    rows = {row["capability"]: row for row in catalog.describe()}
    video = rows[registry.VIDEO_GENERATION]
    assert video["available"]
    assert video["default"] == "dgrauet/ltx-2.3-mlx-q4"


def test_catalog_video_traits_follow_the_resolved_engine(monkeypatch):
    """The payload the Playground's frame/canvas/step sliders read
    (`catalog.py`'s `videoTraits`, the fix for Task 5 leaving the CLIENT
    hardcoded to the server's grid) — present only for video generation, and
    only the resolved engine's own numbers."""
    from fused_render.ai import catalog

    _mac_arm(monkeypatch)
    rows = {row["capability"]: row for row in catalog.describe()}
    assert rows[registry.TEXT_GENERATION]["videoTraits"] is None
    video = rows[registry.VIDEO_GENERATION]["videoTraits"]
    assert video == {
        "framesBase": 1, "framesStep": 8, "minFrames": 9, "maxFrames": 169,
        "defaultFrames": 97, "defaultWidth": 704, "defaultHeight": 480,
        "defaultSteps": 8,
    }


def test_video_frame_bounds_matches_the_apps_own_n_window():
    ltx = registry.video_traits_for("ltx-video")
    assert registry.video_frame_bounds(ltx) == (9, 169)  # 1+8*1, 1+8*21


def test_ltx_video_suggestions_name_their_own_8_step_default():
    """`DistilledPipeline` runs a fixed 8-step stage-1 schedule regardless of
    quantization tier — not the app's generic image-route default (28) — so
    the Playground's step slider (`VideoStage.tsx`) must
    not fall back to either. `entry.defaults.steps` is the per-model hint
    that keeps it from doing so even if a future change moved `registry.
    VIDEO_TRAITS["ltx-video"].default_steps` off 8 for some other reason."""
    from fused_render.ai import catalog

    for entry in catalog.SUGGESTIONS["ltx-video"]:
        assert entry["defaults"] == {"steps": 8}, entry["id"]


def test_catalog_default_is_null_when_unavailable(monkeypatch):
    _windows(monkeypatch)
    from fused_render.ai import catalog

    rows = {row["capability"]: row for row in catalog.describe()}
    video = rows[registry.VIDEO_GENERATION]
    assert not video["available"]
    assert video["default"] is None


def test_video_generation_is_classified_as_supported_not_ruled_out():
    """The task-vocabulary half of this (D433 moved the table to `ai/tasks.py`,
    keyed by the Hub's own `text-to-video` tag rather than a prose label)."""
    reading = tasks.classify("text-to-video")
    assert reading.support == tasks.SUPPORTED
    assert reading.capability == registry.VIDEO_GENERATION


def test_hub_repo_the_engine_cannot_read_hits_the_wrong_format_refusal():
    """`text-to-video` being SUPPORTED gives every video repo on the Hub a Load
    button — including ones the shipping engine cannot read (a diffusers
    text-to-video pipeline, say). This is not a new mechanism: it is the same
    "unknown checkpoint, refuse with a sentence" pattern `mflux_image`'s
    worker already uses for a repo it cannot classify
    (`test_ai_mflux_worker.py::test_a_model_with_no_variant_is_named_as_the_
    cause`); the video worker's own version is tested in
    `test_ai_ltx_video_worker.py` rather than duplicated here.
    """
    assert tasks.classify("text-to-video").capability == registry.VIDEO_GENERATION


def test_video_traits_names_every_registered_video_runner():
    assert set(registry.VIDEO_TRAITS) == {"ltx-video"}


def test_video_traits_for_falls_back_to_the_shipping_runner_for_an_unknown_code():
    """A runner under test (`fake_video_runner`'s `code="fake-video"`), or one
    written before this table existed, must get the shipping engine's own
    shape rather than an arbitrary new default or a `KeyError`. Was H3's row
    until D468 dropped that runner."""
    assert registry.video_traits_for("fake-video") == registry.VIDEO_TRAITS["ltx-video"]
    assert registry.video_traits_for(None) == registry.VIDEO_TRAITS["ltx-video"]


def test_video_traits_for_ltx_video():
    traits = registry.video_traits_for("ltx-video")
    assert (traits.frames_base, traits.frames_step) == (1, 8)
    assert traits.default_frames_n == 12  # 1 + 8*12 = 97, upstream's own default
    assert (traits.default_width, traits.default_height) == (704, 480)
    assert traits.default_steps == 8


#: `VideoStage.tsx`'s own copy of the fallback request shape. Kept in sync by
#: the test below rather than by the comment above each literal, which is how
#: the two DID drift: D468 moved `video_traits_for`'s fallback from the
#: dropped `h3-video` row to `ltx-video`'s and left the client drawing a
#: `5 + 17n` slider at 864x480/20 steps for a payload-less row (caught in
#: review, not by a test). A client that guesses a different grid than the
#: server snaps to draws a control whose every value the render then moves —
#: the exact failure the `videoTraits` payload exists to close.
_VIDEO_STAGE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend", "src", "apps", "ai_models", "playground", "VideoStage.tsx")


def test_the_playgrounds_fallback_traits_match_the_servers_own_fallback():
    """Checked on the SOURCE, like `test_ai_runtime.py`'s bridge-whitelist
    guards: nothing at runtime ever compares these two, because the client
    only reaches its literal when the server sent no payload at all — so a
    mismatch is invisible in every test that has a real catalog row.
    """
    with open(_VIDEO_STAGE, encoding="utf-8") as f:
        src = f.read()
    block = re.search(
        r"const FALLBACK_TRAITS[^{]*\{(.*?)\n\};", src, re.DOTALL)
    assert block, f"no FALLBACK_TRAITS object literal in {_VIDEO_STAGE}"
    client = {k: int(v) for k, v in re.findall(r"(\w+):\s*(\d+)", block.group(1))}

    traits = registry.video_traits_for("no-such-runner")   # the fallback itself
    low, high = registry.video_frame_bounds(traits)
    assert client == {
        "framesBase": traits.frames_base,
        "framesStep": traits.frames_step,
        "minFrames": low,
        "maxFrames": high,
        "defaultFrames": traits.frames_base + traits.frames_step * traits.default_frames_n,
        "defaultWidth": traits.default_width,
        "defaultHeight": traits.default_height,
        "defaultSteps": traits.default_steps,
    }, (
        "VideoStage.tsx's FALLBACK_TRAITS has drifted from "
        "`registry.video_traits_for`'s own fallback row — see this test's "
        "docstring for why that is invisible at runtime")
