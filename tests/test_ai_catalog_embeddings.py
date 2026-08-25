"""The two embeddings blocks of `catalog.py` — and the invariant that makes them
two rather than one.

`mlx-embed` used to be ALIASED onto the withdrawn `transformers-embed`'s list
(`_SHARED_SUGGESTIONS`), correctly: `google/siglip2-*` publishes a single format
that transformers and mlx-embeddings both read, so one list served both engines
by construction. `onnx-embed` reads an `onnx-community` graph export instead —
a different file out of a different repo — so the alias would now offer every Mac
a download its engine has no reader for. This file pins the split, and pins that
the alias is GONE rather than repointed.
"""
from fused_render.ai import catalog, registry


MLX_IDS = {"google/siglip2-base-patch16-384",
           "google/siglip2-so400m-patch14-384"}


def test_the_withdrawn_torch_rows_are_gone_from_the_catalog():
    """Not just unregistered — uncurated. A `SUGGESTIONS` key for a runner
    nobody registers is a dead card on the page, and
    `test_every_suggested_model_names_a_runner_that_exists` catches it; this
    names the codes so the failure reads as "the removal was incomplete" rather
    than as a generic invariant break."""
    for code in ("transformers-embed", "transformers-embed-cuda",
                 "transformers-embed-rocm"):
        assert code not in catalog.SUGGESTIONS
        assert code not in catalog._SHARED_SUGGESTIONS


def test_mlx_embed_has_its_own_list_and_is_no_longer_aliased():
    """The alias was the ONE cross-RUNNER entry in `_SHARED_SUGGESTIONS`, and
    removing it is the change most easily got wrong: repointing it at
    `"onnx-embed"` would pass a naive "the alias resolves" check and offer Macs
    an ONNX export mlx-embeddings cannot open."""
    assert catalog._SHARED_SUGGESTIONS.get("mlx-embed") is None
    ids = {entry["id"] for entry in catalog.SUGGESTIONS["mlx-embed"]}
    assert ids == MLX_IDS


def test_the_mlx_list_holds_safetensors_repos_and_the_onnx_list_does_not():
    """The two lists must not overlap, and the reason is the FILES: MLX reads
    `model.safetensors` out of `google/siglip2-*`, `onnxruntime` reads
    `onnx/text_model.onnx` out of the `-ONNX` re-export. A repo in both lists
    would break the every-id-in-one-list invariant `capability_of` reads."""
    onnx_ids = {entry["id"] for entry in catalog.SUGGESTIONS["onnx-embed"]}
    assert not (MLX_IDS & onnx_ids)
    for repo_id in MLX_IDS:
        assert not repo_id.endswith("-ONNX")


def test_the_returned_lists_are_independent_copies():
    """`for_runner` promises a copy callers may mutate (its own docstring) —
    proven here rather than assumed, since the aliased hardware variants make it
    easy to accidentally hand back the same list object for two runners."""
    a = catalog.for_runner("onnx-embed")
    b = catalog.for_runner("onnx-embed-cuda")
    a.append({"id": "not-a-real-model"})
    assert b != a


def test_siglip2_base_is_the_default_on_both_engines(monkeypatch):
    """`default_for` takes the CAPABILITY and resolves the runner itself
    (`_runner_for`), so the default is checked on both platforms rather than
    assumed from one list. Two DIFFERENT repo ids for the same checkpoint now,
    which is the visible consequence of the split."""
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    assert catalog.default_for(registry.EMBEDDINGS) == "google/siglip2-base-patch16-384"
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    assert catalog.default_for(registry.EMBEDDINGS) == (
        "onnx-community/siglip2-base-patch16-384-ONNX")


def test_the_mlx_list_is_smallest_first():
    sizes = [entry["size_gb"] for entry in catalog.SUGGESTIONS["mlx-embed"]]
    assert sizes == sorted(sizes)


def test_clip_is_deliberately_not_curated_on_either_engine():
    """`catalog.py`'s own comment on the ONNX block explains why the argument
    survived the move to exports: mlx-embeddings has no CLIP module, so a
    curated CLIP is an entry that vanishes the moment a Mac switches engines.
    (The torch-era half of the argument — that `openai/clip-vit-base-patch32`
    ships TensorFlow and Flax copies, making the pull 3.6 GB — no longer
    applies to an `onnx-community` export, which is why the note had to be
    rewritten rather than deleted.)"""
    for code in ("mlx-embed", "onnx-embed"):
        ids = {entry["id"] for entry in catalog.SUGGESTIONS[code]}
        assert not any("clip" in repo_id.lower() for repo_id in ids)


# -- the ONNX block -------------------------------------------------------------


ONNX_IDS = {"onnx-community/siglip2-base-patch16-384-ONNX",
            "onnx-community/siglip2-so400m-patch14-384-ONNX"}


def test_onnx_embed_has_its_own_curated_list():
    """A SEPARATE list, never an alias onto the MLX one — which is the whole
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


def test_the_onnx_repos_are_exports_and_not_the_safetensors_repos():
    """Distinct repo ids for the same weights, which is what makes two lists
    correct rather than redundant: `onnx-community/*-ONNX` and `google/siglip2-*`
    are different downloads, and a machine holding one does not hold the other.
    """
    assert not (ONNX_IDS & MLX_IDS)
    for repo_id in ONNX_IDS:
        assert repo_id.endswith("-ONNX")


def test_every_embedding_suggestion_is_loadable_by_its_runner():
    """The same rule `test_every_suggested_model_could_be_loaded_by_the_page`
    (in `test_ai_models_api.py`) checks for every runner in the app — restated
    here for the two new codes so this file does not depend on that one."""
    for code in ("mlx-embed", "onnx-embed", "onnx-embed-directml",
                 "onnx-embed-cuda", "onnx-embed-rocm"):
        runner = registry.by_code(code)
        assert runner is not None
        for entry in catalog.for_runner(code):
            assert entry["id"]
            assert entry["size_gb"] and entry["size_gb"] > 0
