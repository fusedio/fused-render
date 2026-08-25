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


# -- the ONNX block -------------------------------------------------------------


ONNX_IDS = {"onnx-community/siglip2-base-patch16-384-ONNX",
            "onnx-community/siglip2-so400m-patch14-384-ONNX"}


def test_onnx_embed_has_its_own_curated_list():
    """A SEPARATE list, never an alias onto the torch one — which is the whole
    keying rule of this file. The two engines read DIFFERENT FILES out of the
    same checkpoints (`onnx/text_model.onnx` against `model.safetensors`), so a
    shared list would offer each engine a repo it cannot open."""
    ids = {entry["id"] for entry in catalog.SUGGESTIONS["onnx-embed"]}
    assert ids == ONNX_IDS
    assert catalog._SHARED_SUGGESTIONS.get("onnx-embed") is None


def test_the_three_onnx_hardware_variants_are_aliased_not_duplicated():
    """Same repos, same graphs, a different execution provider — the
    `diffusers-image-cuda` argument exactly. Only the alias table proves there
    is one list; three copied literals would be equal until somebody edited
    one, and that failure is silent on the page."""
    for code in ("onnx-embed-directml", "onnx-embed-cuda", "onnx-embed-rocm"):
        assert catalog._SHARED_SUGGESTIONS.get(code) == "onnx-embed"
        assert code not in catalog.SUGGESTIONS
        assert catalog.for_runner(code) == catalog.for_runner("onnx-embed")


def test_the_onnx_list_is_smallest_first():
    sizes = [entry["size_gb"] for entry in catalog.SUGGESTIONS["onnx-embed"]]
    assert sizes == sorted(sizes)


def test_the_onnx_sizes_are_the_FETCHED_set_not_the_whole_snapshot():
    """The deliberate exception to this file's whole-snapshot convention, and
    the reason it is documented in the block's own comment.

    These repos publish eight quantizations of each tower side by side: the
    whole base snapshot is 11.42 GB and the so400m one 29.5 GB. Neither is what
    this app downloads — `runners/onnx_embed.py`'s `download()` pins
    `allow_patterns` to the fp32 graphs — so a whole-snapshot figure here would
    price a download nobody performs, and would put an 11 GB "no" fit verdict on
    a 1.5 GB model. The exact figures are asserted rather than merely bounded
    because `tests/test_ai_onnx_embed_real_weights.py` checks the FETCHED bytes
    against them, and the two must not drift.
    """
    by_id = {entry["id"]: entry for entry in catalog.SUGGESTIONS["onnx-embed"]}
    assert by_id["onnx-community/siglip2-base-patch16-384-ONNX"]["size_gb"] == 1.5
    assert by_id["onnx-community/siglip2-so400m-patch14-384-ONNX"]["size_gb"] == 4.6


def test_every_id_still_appears_in_exactly_one_list():
    """The invariant `all_suggested_ids()` and `capability_of` both read. Adding
    a second embeddings block is the first change that could break it by
    accident — the ONNX repos are re-exports of the torch ones and share their
    labels, so a copy-paste that reused an `id` would be easy to miss."""
    seen = []
    for entries in catalog.SUGGESTIONS.values():
        seen.extend(entry["id"] for entry in entries)
    assert len(seen) == len(set(seen)), sorted(
        repo_id for repo_id in set(seen) if seen.count(repo_id) > 1)
    assert ONNX_IDS <= catalog.all_suggested_ids()


def test_the_onnx_repos_are_the_exports_of_the_torch_ones_and_not_the_same_repos():
    """Distinct repo ids for the same weights, which is what makes two lists
    correct rather than redundant: `onnx-community/*-ONNX` and `google/siglip2-*`
    are different downloads, and a machine holding one does not hold the other.
    """
    torch_ids = {entry["id"] for entry in catalog.SUGGESTIONS["transformers-embed"]}
    assert not (ONNX_IDS & torch_ids)
    for repo_id in ONNX_IDS:
        assert repo_id.endswith("-ONNX")


def test_every_embedding_suggestion_is_loadable_by_its_runner():
    """The same rule `test_every_suggested_model_could_be_loaded_by_the_page`
    (in `test_ai_models_api.py`) checks for every runner in the app — restated
    here for the two new codes so this file does not depend on that one."""
    for code in ("mlx-embed", "transformers-embed", "onnx-embed",
                 "onnx-embed-directml", "onnx-embed-cuda", "onnx-embed-rocm"):
        runner = registry.by_code(code)
        assert runner is not None
        for entry in catalog.for_runner(code):
            assert entry["id"]
            assert entry["size_gb"] and entry["size_gb"] > 0
