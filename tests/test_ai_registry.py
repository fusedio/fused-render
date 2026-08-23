"""The embeddings capability's own registration (SPEC §40) — the parts of
`registry.py` not already exercised through `ai_models.py`/`ai_runtime.py`'s
HTTP surface.

Platform gating is driven the same way `test_ai_models_api.py` drives it:
`monkeypatch.setattr(registry.platform, "system"/"machine", ...)` rather than
running on whatever machine CI happens to be.
"""
from fused_render.ai import registry


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


def test_the_task_label_that_routes_to_embeddings_is_the_dual_encoder_one():
    """`zero-shot-image-classification`, which is the tag a SigLIP or CLIP repo
    actually carries — a dual encoder, described by one thing you can do with
    its two towers."""
    assert registry._TASK_CAPABILITIES["zero-shot image classification"] == registry.EMBEDDINGS


def test_the_capabilitys_own_names_deliberately_stay_unclassified():
    """The trap this capability sets for itself: "embeddings" (the Hub's
    `feature-extraction`) and "sentence embeddings" (`sentence-similarity`) read
    like the obvious labels for it, and are not.

    What wears them is a sentence-transformers checkpoint — a text encoder plus
    a pooling config, with no vision tower and no `get_text_features`/
    `get_image_features` for either embedding runner to call. Mapping them would
    put a Load button on `sentence-transformers/all-MiniLM-L6-v2`, a download
    that then refuses; `test_hub_models.py::test_a_result_is_never_something_
    this_app_cannot_run` pins that by the repo id itself.
    """
    for label in ("embeddings", "sentence embeddings"):
        assert label in registry.NO_RUNNER_YET, label
        assert label not in registry._TASK_CAPABILITIES, label


def test_every_task_label_this_module_names_is_classified_exactly_once():
    """The completeness rule `test_ai_models_api.py::test_every_task_label_is_
    classified` checks from the listing side, restated here from the registry
    side: no label is in both tables, which would make one of them a dead
    entry nobody can reach."""
    overlap = set(registry._TASK_CAPABILITIES) & registry.NO_RUNNER_YET
    assert not overlap


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
    smallest LTX-2.3 tier rather than H3's 144GB checkpoint. Naming
    `MiniMaxAI/MiniMax-H3` explicitly still reaches H3 unchanged (see
    `test_ai_runtime.py`'s per-runner request-shaping tests, Task 5, and
    `test_a_video_off_apple_silicon_says_so`-style tests that pin `_RUNNERS`
    to h3-video directly)."""
    _mac_arm(monkeypatch)
    _with_h3_binary(monkeypatch, tmp_path)
    from fused_render.ai import catalog

    assert registry.for_capability(registry.VIDEO_GENERATION).code == "ltx-video"
    rows = {row["capability"]: row for row in catalog.describe()}
    video = rows[registry.VIDEO_GENERATION]
    assert video["available"]
    assert video["default"] == "dgrauet/ltx-2.3-mlx-q4"


def test_catalog_default_is_null_when_unavailable(monkeypatch):
    _windows(monkeypatch)
    from fused_render.ai import catalog

    rows = {row["capability"]: row for row in catalog.describe()}
    video = rows[registry.VIDEO_GENERATION]
    assert not video["available"]
    assert video["default"] is None


def test_video_generation_removed_from_ruled_out_tasks():
    assert "video generation" not in registry.NO_RUNNER_YET
    assert registry._TASK_CAPABILITIES["video generation"] == registry.VIDEO_GENERATION


def test_hub_repo_h3_cannot_read_hits_the_existing_wrong_format_refusal():
    """Removing "video generation" from the ruled-out list gives every video
    repo on the Hub a Load button — including ones h3.c cannot read (a
    diffusers text-to-video pipeline, say). This is not a new mechanism: it
    is the same "unknown checkpoint, refuse with a sentence" pattern
    `mflux_image`'s worker already uses for a repo it cannot classify
    (`test_ai_mflux_worker.py::test_a_model_with_no_variant_is_named_as_the_
    cause`); the h3_video worker gets its own version of that refusal, tested
    directly in `test_ai_h3_worker.py` rather than duplicated here.
    """
    assert registry.capability_for_task("video generation") == registry.VIDEO_GENERATION


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
