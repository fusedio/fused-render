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
from fused_render.ai.runners import formats


DUAL_MLX_IDS = {"google/siglip2-base-patch16-384",
                "google/siglip2-so400m-patch14-384"}
# The MLX prose row is an `mlx-community` CONVERSION where the dual rows are
# upstream safetensors, and `catalog.py`'s block header carries the reason: it is
# where a single-format MLX build of a prose encoder exists.
PROSE_MLX_IDS = {"mlx-community/nomicai-modernbert-embed-base-bf16"}
MLX_IDS = DUAL_MLX_IDS | PROSE_MLX_IDS


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


def test_BOTH_engines_curate_a_prose_row():
    """**There is no Mac prose gap any more, and this is the assertion that
    keeps it closed.** For a while the MLX list was dual-encoders-only, so a Mac
    default was a 64-token caption encoder while every other machine had a
    paragraph encoder — an asymmetry about the DOWNLOADS rather than the engine,
    since `mlx_embed` serves a text encoder perfectly well.

    Asserted per engine rather than in total, because a list is what ONE machine
    sees: a total would let the Mac list go proseless behind a well-stocked ONNX
    one, which is exactly the state this replaced.
    """
    for code, prose in (("mlx-embed", PROSE_MLX_IDS),
                        ("onnx-embed", PROSE_ONNX_IDS)):
        ids = {entry["id"] for entry in catalog.SUGGESTIONS[code]}
        assert ids & prose, code


def test_the_mlx_prose_row_leads_its_list_and_is_the_recommended_one():
    """Position 0 IS the default (`default_for`), and smallest-first is what puts
    it there rather than a preference: the conversion is 0.30 GB against the
    SigLIP2 base's 1.5."""
    entries = catalog.SUGGESTIONS["mlx-embed"]
    assert entries[0]["id"] in PROSE_MLX_IDS
    assert entries[0]["recommended"] is True
    assert {e["id"] for e in entries[1:]} == DUAL_MLX_IDS


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


def test_the_default_moves_OFF_siglip2_on_BOTH_engines(monkeypatch):
    """**The user-visible change on this branch**, and the reason the risk is
    worth an assertion rather than a comment: a bare `fused.ai.embed({texts})`
    used to load a 64-token caption encoder and now loads a 2048-token paragraph
    encoder. The two models' vectors are not comparable, so anyone who indexed a
    corpus with the old default has to re-index — and nothing downstream can
    detect that they did not.

    `default_for` takes the CAPABILITY and resolves the runner itself
    (`_runner_for`), so both platforms are checked rather than one being assumed
    from the other's list.

**Both** engines answer with a prose encoder now, and both answers are nomic
    models — deliberately, since the two produce vectors in the same space and
    defaults from one family make that promise easier to believe.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")
    assert catalog.default_for(registry.EMBEDDINGS) == (
        "mlx-community/nomicai-modernbert-embed-base-bf16")
    monkeypatch.setattr(registry.platform, "system", lambda: "Windows")
    monkeypatch.setattr(registry.platform, "machine", lambda: "AMD64")
    assert catalog.default_for(registry.EMBEDDINGS) == (
        "nomic-ai/nomic-embed-text-v1.5")


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


DUAL_ONNX_IDS = {"onnx-community/siglip2-base-patch16-384-ONNX",
                 "onnx-community/siglip2-so400m-patch14-384-ONNX"}
# ONE curated prose row, and the singleton is load-bearing rather than
# incidental: `intfloat/multilingual-e5-small` was here and was removed as a
# scope decision (see `catalog.py`'s own note), and it was the only curated
# prose model `formats.MLX_EMBED_MODEL_TYPES` admitted.
PROSE_ONNX_IDS = {"nomic-ai/nomic-embed-text-v1.5"}
ONNX_IDS = DUAL_ONNX_IDS | PROSE_ONNX_IDS


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


def test_every_curated_embedding_id_resolves_to_a_PROMPT_SCHEME():
    """**A curation rule, not a coincidence.** A retrieval encoder instructs a
    question and a passage differently, and a model whose convention this app
    does not know embeds both verbatim — still unit-length vectors of the right
    dimension, just worse ones, with nothing downstream able to tell. So
    recommending a model we cannot prompt correctly would be recommending a
    silent accuracy loss.

    "Resolves" means `text_embed_scheme` answers, which it always does — the
    real assertion is that every PROSE row resolves to a scheme that is not
    `"none"`, and that it comes from the curated table rather than from the id
    heuristic. A dual encoder correctly answers `"none"`: it has no query/passage
    convention, and that is what the route refuses `kind` on.
    """
    prose = PROSE_ONNX_IDS | PROSE_MLX_IDS
    for code in ("mlx-embed", "onnx-embed"):
        for entry in catalog.SUGGESTIONS[code]:
            scheme = formats.text_embed_scheme(entry["id"])
            assert scheme in formats.TEXT_EMBED_PROMPTS, entry["id"]
            if entry["id"] in prose:
                assert scheme != "none", entry["id"]
                # Named OUTRIGHT, never left to the id heuristic. The MLX row is
                # why this half of the assertion earns its keep: its id spells
                # the account `nomicai-`, so no hint matches it and it resolved
                # `"none"` until it was curated here — a curated model embedding
                # every query with no prefix, which nothing downstream could
                # have detected.
                assert entry["id"] in formats.TEXT_EMBED_SCHEMES, entry["id"]
            else:
                assert scheme == "none", entry["id"]


def test_all_MiniLM_is_deliberately_not_curated():
    """`catalog.py`'s own comment gives the reason, and it is the smallest-first
    rule that makes it decisive rather than a preference: the ONNX export is
    90 MB, so it would take position 0 and BE the default — and it is not
    retrieval-trained, has no query/passage convention to prompt with, and is
    the one candidate here that is bad at the job the capability exists for."""
    for code in ("mlx-embed", "onnx-embed"):
        ids = {entry["id"] for entry in catalog.SUGGESTIONS[code]}
        assert not any("minilm" in repo_id.lower() for repo_id in ids)


def test_the_prose_row_leads_the_onnx_list():
    """Position 0 IS the default (`default_for`), so this is the same fact as
    the default test above, asserted structurally — a re-order is what would
    change it, and a re-order is invisible in a diff of two dicts.

    The dual encoders follow it, and that ordering is what the smallest-first
    rule produces rather than a preference: the prose fetch is 0.55 GB against
    the base export's 1.54 GB.
    """
    ids = [entry["id"] for entry in catalog.SUGGESTIONS["onnx-embed"]]
    assert ids[0] == "nomic-ai/nomic-embed-text-v1.5"
    assert set(ids[1:]) == DUAL_ONNX_IDS


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
    # The prose row deviates further: its repo ships a full safetensors copy
    # beside the eight quantizations, so the whole snapshot is four times the
    # fetch.
    assert by_id["nomic-ai/nomic-embed-text-v1.5"]["size_gb"] == 0.5


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

    Only the DUAL rows carry the `-ONNX` suffix: the prose entries are the
    publishers' own repos, which ship an `onnx/` folder beside their safetensors
    rather than being re-exported under a separate account. Same rule either
    way — the id names a repo holding a graph this engine can open.
    """
    assert not (ONNX_IDS & MLX_IDS)
    for repo_id in DUAL_ONNX_IDS:
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
