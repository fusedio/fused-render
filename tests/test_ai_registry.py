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
    assert not registry.by_code("mlx-embed").available().ok
    _linux(monkeypatch)
    assert not registry.by_code("mlx-embed").available().ok
    _mac_arm(monkeypatch)
    assert registry.by_code("mlx-embed").available().ok


def test_transformers_embed_runs_everywhere(monkeypatch):
    """The platform-agnostic row — `_torch_platform`, the gate the withdrawn
    `transformers-text` family used (D416), so wherever that CPU fallback ran,
    embeddings does too."""
    for setter in (_mac_arm, _windows, _linux):
        setter(monkeypatch)
        assert registry.by_code("transformers-embed").available().ok


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
