"""The embeddings capability's own registration (SPEC §40) — the parts of
`registry.py` not already exercised through `ai_models.py`/`ai_runtime.py`'s
HTTP surface.

Platform gating is driven the same way `test_ai_models_api.py` drives it:
`monkeypatch.setattr(registry.platform, "system"/"machine", ...)` rather than
running on whatever machine CI happens to be.
"""
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


def test_both_embedding_runners_are_registered():
    codes = {r.code for r in registry.all_runners() if r.capability == registry.EMBEDDINGS}
    assert codes == {"mlx-embed", "transformers-embed"}


def test_mlx_embed_is_registered_before_transformers_embed():
    """First-match-wins is the whole mechanism (see `registry.py`'s comment on
    the table): MLX must come first so an Apple Silicon machine resolves there
    by default, exactly like text generation and image generation."""
    codes = [r.code for r in registry.all_runners() if r.capability == registry.EMBEDDINGS]
    assert codes == ["mlx-embed", "transformers-embed"]


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


def test_no_cuda_or_rocm_embed_variant_exists():
    """Deliberate (see `registry.py`'s comment on the `transformers-embed`
    row): a dual encoder is one forward pass, too cheap to justify a second or
    third wheel the way text and image generation's accelerated rows are."""
    codes = {r.code for r in registry.all_runners() if r.capability == registry.EMBEDDINGS}
    assert not any("cuda" in code or "rocm" in code for code in codes)


# The task-vocabulary tests that used to live here (the embeddings dual-encoder
# tag, the ruled-out-names trap, the no-label-in-both-tables completeness rule)
# moved to `test_ai_tasks.py` along with `_TASK_CAPABILITIES`/`NO_RUNNER_YET`
# themselves (D433) — this file keeps only what is actually about `registry.py`.


def _with_h3_binary(monkeypatch, tmp_path):
    fake = tmp_path / "h3"
    fake.write_text("#!/bin/sh\necho fake h3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_H3_BIN", str(fake))
    return fake


def _without_h3_binary(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_H3_BIN", raising=False)
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    monkeypatch.setattr(registry.sys, "frozen", None, raising=False)


def test_video_generation_is_a_registered_capability():
    assert registry.VIDEO_GENERATION == "text-to-video"
    assert registry.VIDEO_GENERATION in registry.capabilities()


def test_ltx_video_and_h3_video_are_the_registered_video_runners():
    codes = {r.code for r in registry.all_runners() if r.capability == registry.VIDEO_GENERATION}
    assert codes == {"ltx-video", "h3-video"}


def test_ltx_video_is_registered_before_h3_video():
    """First-match-wins is the whole mechanism (see `registry.py`'s comment on
    the table, and `test_mlx_embed_is_registered_before_transformers_embed`
    above): `ltx-video` needs only Apple Silicon (16 GB+), while `h3-video`
    additionally needs the staged h3.c binary, so an Apple Silicon machine
    with no opinion resolves to the accessible ~30 GB engine rather than the
    144 GB one."""
    codes = [r.code for r in registry.all_runners() if r.capability == registry.VIDEO_GENERATION]
    assert codes == ["ltx-video", "h3-video"]


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


def test_neither_video_runner_is_available_off_apple_silicon(monkeypatch, tmp_path):
    """Both rows share the same underlying gate off a Mac — `ltx-video`
    directly via `_apple_silicon`, `h3-video` because `_h3_available` checks
    it first — so a machine with neither is left with no video capability at
    all, unchanged from before this runner existed."""
    _windows(monkeypatch)
    _with_h3_binary(monkeypatch, tmp_path)
    assert not _runner("ltx-video")._available().ok
    assert not _runner("h3-video")._available().ok


def test_h3_video_row_present_on_apple_silicon_with_a_binary(monkeypatch, tmp_path):
    """The gate itself (`_h3_available`, wired via `_available`), independent
    of whether the `h3_video` folder has a `worker.py` yet — `Runner.available`
    also requires the folder to be built, which is Task 2's concern and not
    this one's."""
    _mac_arm(monkeypatch)
    _with_h3_binary(monkeypatch, tmp_path)
    assert _runner("h3-video")._available().ok


def test_h3_video_row_absent_off_apple_silicon(monkeypatch, tmp_path):
    _windows(monkeypatch)
    _with_h3_binary(monkeypatch, tmp_path)
    status = _runner("h3-video")._available()
    assert not status.ok
    assert "Apple Silicon" in status.reason

    _linux(monkeypatch)
    status = _runner("h3-video")._available()
    assert not status.ok
    assert "Apple Silicon" in status.reason


def test_h3_video_row_absent_with_no_binary(monkeypatch):
    _mac_arm(monkeypatch)
    _without_h3_binary(monkeypatch)
    status = _runner("h3-video")._available()
    assert not status.ok
    assert "h3" in status.reason.lower()


def test_h3_bin_resolves_the_env_override(monkeypatch, tmp_path):
    fake = _with_h3_binary(monkeypatch, tmp_path)
    assert registry.h3_bin() == str(fake)


def test_h3_bin_ignores_a_stale_override_that_is_not_a_file(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_H3_BIN", "/no/such/file")
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    monkeypatch.setattr(registry.sys, "frozen", None, raising=False)
    assert registry.h3_bin() is None


def test_h3_bin_falls_back_to_path(monkeypatch):
    _without_h3_binary(monkeypatch)
    monkeypatch.setattr(
        registry.shutil, "which",
        lambda name: "/usr/local/bin/h3" if name == "h3" else None)
    assert registry.h3_bin() == "/usr/local/bin/h3"


def test_h3_bin_resolves_the_packaged_app_bundle(monkeypatch, tmp_path):
    monkeypatch.delenv("FUSED_RENDER_H3_BIN", raising=False)
    monkeypatch.setattr(registry.shutil, "which", lambda name: None)
    monkeypatch.setattr(registry.sys, "frozen", "macosx_app", raising=False)
    contents = tmp_path / "FusedRender.app" / "Contents"
    bin_dir = contents / "Resources" / "bin"
    bin_dir.mkdir(parents=True)
    bundled = bin_dir / "h3"
    bundled.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        registry.sys, "executable", str(contents / "MacOS" / "FusedRender"), raising=False)
    assert registry.h3_bin() == str(bundled)


def test_catalog_defaults_to_the_ltx_entry_on_apple_silicon(monkeypatch, tmp_path):
    """`ltx-video` resolves ahead of `h3-video` on Apple Silicon (Task 1's
    ordering, buildable since Task 3) and now has its own curated shortlist
    (Task 6), so a bare `fused.ai.video()` on such a machine defaults to the
    smallest LTX-2.3 tier rather than H3's 144GB checkpoint. Reaching H3
    itself needs the ENGINE PREFERENCE (the Engines tab) — naming
    `MiniMaxAI/MiniMax-H3` explicitly is refused instead
    (`test_ai_runtime.py::test_naming_the_OTHER_video_engines_cached_model_
    is_refused_not_started`), since resolution is by capability plus stored
    preference and never by `model`."""
    _mac_arm(monkeypatch)
    _with_h3_binary(monkeypatch, tmp_path)
    from fused_render.ai import catalog

    assert registry.for_capability(registry.VIDEO_GENERATION).code == "ltx-video"
    rows = {row["capability"]: row for row in catalog.describe()}
    video = rows[registry.VIDEO_GENERATION]
    assert video["available"]
    assert video["default"] == "dgrauet/ltx-2.3-mlx-q4"


def test_catalog_video_traits_follow_the_resolved_engine(monkeypatch, tmp_path):
    """The payload the Playground's frame/canvas/step sliders read
    (`catalog.py`'s `videoTraits`, the fix for Task 5 leaving the CLIENT
    hardcoded to H3's grid) — present only for video generation, and only
    the resolved engine's own numbers, never a mix of the two rows."""
    from fused_render.ai import catalog

    _mac_arm(monkeypatch)
    _with_h3_binary(monkeypatch, tmp_path)
    rows = {row["capability"]: row for row in catalog.describe()}
    assert rows[registry.TEXT_GENERATION]["videoTraits"] is None
    video = rows[registry.VIDEO_GENERATION]["videoTraits"]
    assert video == {
        "framesBase": 1, "framesStep": 8, "minFrames": 9, "maxFrames": 169,
        "defaultFrames": 97, "defaultWidth": 704, "defaultHeight": 480,
        "defaultSteps": 8,
    }

    # Pin `_RUNNERS` to h3-video alone (the same trick `test_ai_runtime.py`'s
    # own video fixtures use) to check the OTHER engine's numbers reach the
    # same field — proving this is read off whichever runner resolves, not
    # hardcoded to ltx-video now that ltx-video is the common case.
    monkeypatch.setattr(registry, "_RUNNERS", (registry.by_code("h3-video"),))
    rows = {row["capability"]: row for row in catalog.describe()}
    video = rows[registry.VIDEO_GENERATION]["videoTraits"]
    assert video == {
        "framesBase": 5, "framesStep": 17, "minFrames": 22, "maxFrames": 362,
        "defaultFrames": 90, "defaultWidth": 864, "defaultHeight": 480,
        "defaultSteps": 20,
    }


def test_video_frame_bounds_matches_the_apps_own_n_window():
    ltx = registry.video_traits_for("ltx-video")
    assert registry.video_frame_bounds(ltx) == (9, 169)  # 1+8*1, 1+8*21
    h3 = registry.video_traits_for("h3-video")
    assert registry.video_frame_bounds(h3) == (22, 362)  # 5+17*1, 5+17*21


def test_ltx_video_suggestions_name_their_own_8_step_default():
    """`DistilledPipeline` runs a fixed 8-step stage-1 schedule regardless of
    quantization tier — not the app's generic image-route default (28) or
    H3's own 20 — so the Playground's step slider (`VideoStage.tsx`) must
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


def test_hub_repo_h3_cannot_read_hits_the_existing_wrong_format_refusal():
    """`text-to-video` being SUPPORTED gives every video repo on the Hub a Load
    button — including ones h3.c cannot read (a diffusers text-to-video
    pipeline, say). This is not a new mechanism: it is the same "unknown
    checkpoint, refuse with a sentence" pattern `mflux_image`'s worker already
    uses for a repo it cannot classify (`test_ai_mflux_worker.py::test_a_model_
    with_no_variant_is_named_as_the_cause`); the h3_video worker gets its own
    version of that refusal, tested directly in `test_ai_h3_worker.py` rather
    than duplicated here.
    """
    assert tasks.classify("text-to-video").capability == registry.VIDEO_GENERATION


def test_video_traits_names_both_registered_video_runners():
    assert set(registry.VIDEO_TRAITS) == {"ltx-video", "h3-video"}


def test_video_traits_for_falls_back_to_h3_for_an_unknown_code():
    """The exact request shape every video call got before `ltx-video`
    existed — a runner under test (`fake_video_runner`'s `code="fake-
    video"`), or one written before this table existed, must not get an
    arbitrary new default or a `KeyError`."""
    assert registry.video_traits_for("fake-video") == registry.VIDEO_TRAITS["h3-video"]
    assert registry.video_traits_for(None) == registry.VIDEO_TRAITS["h3-video"]


def test_video_traits_for_ltx_video():
    traits = registry.video_traits_for("ltx-video")
    assert (traits.frames_base, traits.frames_step) == (1, 8)
    assert traits.default_frames_n == 12  # 1 + 8*12 = 97, upstream's own default
    assert (traits.default_width, traits.default_height) == (704, 480)
    assert traits.default_steps == 8
