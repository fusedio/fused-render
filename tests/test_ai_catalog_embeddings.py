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


# The base row is upstream and the so400m row is an `mlx-community` bf16
# conversion — half the bytes at the same capability. `catalog.py`'s block header
# carries the rule: take the conversion where it is the same model in a cheaper
# build, stay upstream where the only conversion on offer is a weaker model.
DUAL_MLX_IDS = {"google/siglip2-base-patch16-384",
                "mlx-community/siglip2-so400m-patch16-384"}
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


# -- the stranded MODEL, and the engine gap (PR #830 regression) ----------------
#
# Task 7 removed the `mlx-embed` -> `transformers-embed` alias, which was right
# — but it made embeddings the FIRST capability where a curated id belongs to one
# engine and not another. The offer path was never built for that: a partly
# downloaded `google/siglip2-so400m-patch14-384` on Linux showed no engine, so
# the Local tab offered a resume and the download died inside `onnx_embed`.
#
# The shape is NOT embeddings-specific and the fix is deliberately generic — see
# `catalog.engine_gap`. `test_every_multi_runner_capability_has_the_same_shape`
# below is what pins that.


def _linux(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Linux")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")


def _mac(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "arm64")


def test_a_machine_on_onnx_is_not_offered_the_mlx_only_ids(monkeypatch):
    """**The offer side of the bug.** `for_capability` is what every picker and
    the catalog payload read, and on Linux it must contain no id whose only
    curated home is an engine that cannot run here."""
    _linux(monkeypatch)
    offered = {entry["id"] for entry in catalog.for_capability(registry.EMBEDDINGS)}
    assert offered == {e["id"] for e in catalog.SUGGESTIONS["onnx-embed"]}
    assert not (offered & MLX_IDS)
    # …and every one of those omitted ids reports a gap, so the omission is a
    # decision this machine can explain rather than a silence.
    for repo_id in MLX_IDS:
        assert catalog.engine_gap(repo_id) is not None, repo_id


def test_the_same_ids_are_offered_and_ungapped_on_a_MAC(monkeypatch):
    """The other half, and the one a Linux-only fix would have broken: on Apple
    Silicon `mlx-embed` serves, so these ids are exactly what SHOULD be offered
    and nothing about them is a gap."""
    _mac(monkeypatch)
    offered = {entry["id"] for entry in catalog.for_capability(registry.EMBEDDINGS)}
    assert offered == MLX_IDS
    for repo_id in MLX_IDS:
        assert catalog.engine_gap(repo_id) is None, repo_id
    # The ONNX ids are not a gap on a Mac either: `onnx-embed` is AVAILABLE
    # there, just not selected, and switching engines is a real remedy — which
    # is `hub_cache._engine`'s existing "switch it on the Engines tab" sentence
    # rather than this one.
    for repo_id in ONNX_IDS:
        assert catalog.engine_gap(repo_id) is None, repo_id


def test_the_gap_names_the_engine_the_reason_and_the_counterpart(monkeypatch):
    """What the sentence has to contain to be actionable, asserted by part
    rather than verbatim — the wording may improve, the four facts may not go
    missing.

    The BASE row, because it is the one with a true counterpart: it and
    `onnx-community/siglip2-base-patch16-384-ONNX` are patch16 both, one export
    of one checkpoint. The so400m rows are no longer a pair — see
    `COUNTERPART_IDS` — and the test below covers that.
    """
    _linux(monkeypatch)
    gap = catalog.engine_gap("google/siglip2-base-patch16-384")
    assert gap is not None
    reason = gap["reason"]
    assert "google/siglip2-base-patch16-384" in reason         # which model
    assert "MLX Embeddings" in reason                          # which engine
    assert "Apple Silicon" in reason                           # why not here
    assert "onnx-community/siglip2-base-patch16-384-ONNX" in reason  # what to do
    # And it says the snapshot is not being thrown away, because the honest
    # answer to "then why is it on my disk" is "it still works on a Mac".
    assert "stays on disk" in reason
    assert gap["counterpart"] == "onnx-community/siglip2-base-patch16-384-ONNX"
    assert gap["engines"] == ("mlx-embed",)
    assert gap["serving"] == "onnx-embed"


def test_the_so400m_rows_are_NOT_offered_as_each_others_counterpart(monkeypatch):
    """**The MLX so400m row is patch16 and the ONNX one is patch14.**

    A different checkpoint, not the same weights in another format, so
    recommending one as the other's replacement would break the exact promise
    the sentence makes ("the same model in the format this machine's engine does
    read"). The stranded snapshot still gets a gap — it just gets the
    no-counterpart sentence, which names what serves embeddings here and
    recommends nothing.
    """
    _linux(monkeypatch)
    gap = catalog.engine_gap("mlx-community/siglip2-so400m-patch16-384")
    assert gap is not None
    assert gap["counterpart"] is None
    assert "ONNX Embeddings" in gap["reason"]
    assert "patch14" not in gap["reason"]


def test_a_gap_with_no_counterpart_still_says_what_serves_here(monkeypatch):
    """The curated MLX prose row has no ONNX equivalent curated for it, so there
    is nothing to recommend — and the sentence must not trail off. It names the
    engine that DOES serve the capability here instead. (The so400m row is the
    other case, for a different reason — see the counterpart test above.)"""
    _linux(monkeypatch)
    gap = catalog.engine_gap("mlx-community/nomicai-modernbert-embed-base-bf16")
    assert gap is not None and gap["counterpart"] is None
    assert "ONNX Embeddings" in gap["reason"]
    assert "does not read this model's files" in gap["reason"]


def test_the_gap_reason_names_the_blocker_for_THIS_machine_not_offering_zero(monkeypatch):
    """PR review finding: `runners_offering()` walks `_RUNNERS` in registry
    order, and since the GPU-first reorder that order is
    `onnx-embed-directml, onnx-embed-cuda, onnx-embed-rocm, onnx-embed` —
    accelerator rows for OTHER operating systems ahead of the cross-platform
    base row. Picking `offering[0]`'s reason unconditionally therefore named
    `_directml`'s "needs Windows" on an Intel Mac, which is true of that row
    and useless to the reader: the actual reason nothing here can read
    `nomic-ai/nomic-embed-text-v1.5` is `onnx-embed`'s own gate — no macOS
    x86_64 `onnxruntime` wheel — and that is the sentence this machine needs.

    `nomic-ai/nomic-embed-text-v1.5` is curated on `onnx-embed` (see
    `catalog.py`'s suggestion list), so it is offered by all four hardware
    variants via `_SHARED_SUGGESTIONS` — the same shape the brief's example
    used.
    """
    monkeypatch.setattr(registry.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(registry.platform, "machine", lambda: "x86_64")
    assert catalog.runners_offering("nomic-ai/nomic-embed-text-v1.5") == (
        "onnx-embed-directml", "onnx-embed-cuda", "onnx-embed-rocm", "onnx-embed",
    )
    gap = catalog.engine_gap("nomic-ai/nomic-embed-text-v1.5")
    assert gap is not None
    reason = gap["reason"]
    assert "Apple Silicon" in reason, reason
    assert "Windows" not in reason, reason


def test_an_UNCURATED_id_is_never_a_gap(monkeypatch):
    """"No information" is not "no", and this is the assertion that keeps the fix
    from becoming a wall. Nobody here has an opinion about a repo the user found
    in Discover; `formats.loaders()` and the runner's own format check are the
    judges for those, exactly as before."""
    _linux(monkeypatch)
    assert catalog.runners_offering("someone/found-this-myself") == ()
    assert catalog.engine_gap("someone/found-this-myself") is None


def test_counterpart_for_is_checked_against_the_curation_not_trusted(monkeypatch):
    """The table proposes and the curation decides: a counterpart pointing at a
    row nobody curates must not be recommended."""
    _linux(monkeypatch)
    assert catalog.counterpart_for("google/siglip2-base-patch16-384", "onnx-embed") == (
        "onnx-community/siglip2-base-patch16-384-ONNX")
    # The so400m row has no table entry at all now, patch16 against patch14.
    assert catalog.counterpart_for(
        "mlx-community/siglip2-so400m-patch16-384", "onnx-embed") is None
    # Not curated for the MLX engine, so not offered to it.
    assert catalog.counterpart_for("google/siglip2-base-patch16-384", "mlx-embed") is None
    # An id with no table row at all.
    assert catalog.counterpart_for("someone/whatever", "onnx-embed") is None


def test_runners_offering_is_the_narrow_companion_to_all_suggested_ids():
    """The two exist together and answer opposite questions — the docstrings say
    so, and this is the assertion behind them. `all_suggested_ids` keeps its
    cross-runner breadth (the mirror's privacy gate reads it); `runners_offering`
    is what says WHICH engine, which is what an offer needs."""
    every = catalog.all_suggested_ids()
    assert "mlx-community/siglip2-so400m-patch16-384" in every
    assert "nomic-ai/nomic-embed-text-v1.5" in every
    assert catalog.runners_offering(
        "mlx-community/siglip2-so400m-patch16-384") == ("mlx-embed",)
    # First in REGISTRY order, which is `onnx-embed-directml` now that the
    # GPU-first policy decision (`registry.py`'s block comment above
    # `_RUNNERS`) leads the ONNX family with its accelerated rows —
    # `runners_offering` walks `registry.all_runners()` in order, so this
    # follows the reorder without a code change here.
    assert catalog.runners_offering(
        "nomic-ai/nomic-embed-text-v1.5")[0] == "onnx-embed-directml"
    # Hardware variants report as offering their family's list, the same
    # resolution `for_runner` does.
    assert "onnx-embed-cuda" in catalog.runners_offering("nomic-ai/nomic-embed-text-v1.5")
    assert "onnx-embed" in catalog.runners_offering("nomic-ai/nomic-embed-text-v1.5")


def test_every_multi_runner_capability_has_the_same_shape():
    """**Why the fix is capability-agnostic**, pinned so nobody narrows it to
    embeddings later.

    Embeddings is where this was HIT — the torch removal orphaned two ids that
    every platform used to be able to read — but it is not where it is possible.
    Every capability with two curated runners has ids belonging to only one of
    them, because the two engines read different formats: `mlx-text`'s
    `mlx-community/*` against `llamacpp-text`'s GGUF filenames, MLX Whisper's
    conversions against CTranslate2's, MLX FLUX's against Diffusers'. A
    half-downloaded `mlx-community` chat model on Linux is the identical hole.
    """
    per_capability = {}
    for code, entries in catalog.SUGGESTIONS.items():
        runner = registry.by_code(code)
        if runner is None:
            continue
        per_capability.setdefault(runner.capability, {})[code] = {
            entry["id"] for entry in entries}

    multi = {cap: per for cap, per in per_capability.items() if len(per) > 1}
    # If this is ever empty the test has stopped testing anything.
    assert len(multi) >= 3, sorted(per_capability)
    for cap, per in multi.items():
        for code, ids in per.items():
            others = set()
            for other, other_ids in per.items():
                if other != code:
                    others |= other_ids
            assert ids - others, (
                f"{cap}/{code} shares every id with its siblings — if that is "
                f"now true, `engine_gap`'s reason for being generic has changed")
