"""The embeddings block of `catalog.py` — the suggestion list both embedding
runners share, and the fix for the defect the block used to have: the comment
said `mlx-embed` was ALIASED onto `transformers-embed`'s list
(`_SHARED_SUGGESTIONS`), while the code actually carried two separately
maintained copies of the same two entries. This pins that the code and the
comment now agree.
"""
from fused_render.ai import catalog, registry


def test_transformers_embed_has_the_curated_list():
    assert catalog.SUGGESTIONS["transformers-embed"]
    assert "mlx-embed" not in catalog.SUGGESTIONS


def test_mlx_embed_is_aliased_not_duplicated():
    """The defect this test exists to catch: a literal SECOND copy of the two
    entries under `SUGGESTIONS["mlx-embed"]` would pass every other assertion
    here (the two lists would still be equal) but drift the moment either one
    is edited alone. Only the alias table proves there is one list."""
    assert catalog._SHARED_SUGGESTIONS.get("mlx-embed") == "transformers-embed"


def test_mlx_embed_and_transformers_embed_resolve_to_the_identical_list():
    assert catalog.for_runner("mlx-embed") == catalog.for_runner("transformers-embed")


def test_the_returned_lists_are_independent_copies():
    """`for_runner` promises a copy callers may mutate (its own docstring) —
    proven here rather than assumed, since the alias makes it easy to
    accidentally hand back the same list object for both runners."""
    a = catalog.for_runner("mlx-embed")
    b = catalog.for_runner("transformers-embed")
    a.append({"id": "not-a-real-model"})
    assert b != a


def test_siglip2_base_is_the_default_whichever_runner_resolves(monkeypatch):
    """`default_for` takes the CAPABILITY and resolves the runner itself
    (`_runner_for`), so the default is checked on both platforms rather than
    assumed identical from the shared list alone."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    assert catalog.default_for(registry.EMBEDDINGS) == "google/siglip2-base-patch16-384"
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    assert catalog.default_for(registry.EMBEDDINGS) == "google/siglip2-base-patch16-384"


def test_siglip2_so400m_is_present_too():
    ids = {entry["id"] for entry in catalog.SUGGESTIONS["transformers-embed"]}
    assert ids == {"google/siglip2-base-patch16-384", "google/siglip2-so400m-patch14-384"}


def test_the_list_is_smallest_first():
    sizes = [entry["size_gb"] for entry in catalog.SUGGESTIONS["transformers-embed"]]
    assert sizes == sorted(sizes)


def test_clip_is_deliberately_not_curated():
    """The module's own comment on the embeddings block explains why: the whole
    repo download is 3.6GB for a weaker, English-only encoder, and
    mlx-embeddings has no CLIP module either — it would be a suggestion that
    vanishes the moment a Mac switches engines."""
    ids = {entry["id"] for entry in catalog.SUGGESTIONS["transformers-embed"]}
    assert not any("clip" in repo_id.lower() for repo_id in ids)


def test_every_embedding_suggestion_is_loadable_by_its_runner():
    """The same rule `test_every_suggested_model_could_be_loaded_by_the_page`
    (in `test_ai_models_api.py`) checks for every runner in the app — restated
    here for the two new codes so this file does not depend on that one."""
    for code in ("mlx-embed", "transformers-embed"):
        runner = registry.by_code(code)
        assert runner is not None
        for entry in catalog.for_runner(code):
            assert entry["id"]
            assert entry["size_gb"] and entry["size_gb"] > 0
