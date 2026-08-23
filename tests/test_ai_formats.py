"""What each backend's weights look like on disk (`ai/runners/formats.py`).

The module exists because two very different readers ask the same question: a
worker's `load()`, deciding whether to raise before it imports anything, and
the AI Models page, deciding which engine tag a cached repo gets. The value of
the tag rests entirely on those two answers being the same one — so what is
pinned here is the SHARED-ness, not the individual filenames.
"""
import pytest

from fused_render.ai import registry
from fused_render.ai.runners import formats


def _codes():
    return {r.code for r in registry.all_runners()}


#: The runners a plain directory of safetensors belongs to, and the ones a
#: `model_index.json` belongs to — spelled once here because the per-hardware
#: split made the image side three codes, and a test that listed them by hand in
#: every case would be the same drift `loaders()` itself avoids by extending a
#: tuple. `_IMAGE` is read from the module under test on purpose: what these
#: tests pin is the BRANCHES (which format reaches which family), and the
#: membership of a family is pinned by
#: `test_every_registered_runner_appears_in_loaders` below.
#:
#: `_TEXT` is a LITERAL rather than a tuple read from `formats`, and only became
#: one at D416: it was `set(formats.TRANSFORMERS_RUNNERS) | {"mlx-text"}` while
#: four codes read a directory of safetensors, and with the transformers family
#: removed there is exactly one left. A one-element family tuple in `formats`
#: would be a name with nothing to hold together, so the code is written here —
#: which makes these assertions say `{"mlx-text"}` out loud, where a tuple would
#: have let the safetensors branch silently empty out and every case below still
#: pass.
_TEXT = {"mlx-text"}
_IMAGE = set(formats.DIFFUSERS_RUNNERS)


def test_every_runner_code_named_here_is_a_registered_runner():
    """`formats` says "faster-whisper" as a bare string, because it is imported
    by interpreters that have no `fused_render` on their path (see its module
    docstring). This is the check that buys back what the import would have
    given: a runner renamed in the registry and not here would silently stop
    matching, and every card of that format would go quiet."""
    named = set(formats.DECISIVE)
    for repo_id in formats.MFLUX_VARIANTS:
        named |= set(formats.loaders(
            repo_id=repo_id, names=set(), dirnames=set(formats.MFLUX_COMPONENTS),
            config={}, torch_weights=True))
    named |= set(formats.loaders(
        repo_id="x/y", names={formats.CT2_WEIGHTS, formats.MLX_WHISPER_WEIGHTS[0],
                              formats.DIFFUSERS_INDEX},
        dirnames=set(), config={}, torch_weights=True))
    # A NeMo ASR snapshot deliberately names no code (D406 withdrew the
    # runner that used to claim it) — included anyway so a future change that
    # made it name one would be caught by this same assertion.
    named |= set(formats.loaders(
        repo_id="x/y", names={formats.PARAKEET_WEIGHTS}, dirnames=set(),
        config={"target": formats.NEMO_ASR_TARGET + "rnnt_bpe_models.X"},
        torch_weights=True))
    assert named <= _codes(), sorted(named - _codes())


def test_every_registered_runner_appears_in_loaders():
    """The DRIFT IN THE OTHER DIRECTION, and it is the silent one.

    The test above pins that every code named in `formats` is registered — a
    rename in the registry breaks it. Nothing pinned the converse, and the
    converse is what a new runner gets wrong: `ai_models.py` builds a cached
    repo's engine row by filtering `r.code in meta.loaders`, and AI-11e's
    cached-model injection admits a repo to `models[]` only if the resolved
    runner is among that repo's loaders. So a registered runner missing from
    every branch here has NO engine tag, NO Load button and NO cached repos
    offered — precisely on the machines that chose it — while every format test
    in this file still passes, because `loaders()` is internally consistent and
    only the registry knows the code exists.

    That is exactly what the four per-hardware torch variants would have done,
    and it is what the next variant would do too, which is why this is a rule
    rather than four assertions.

    A runner is exercised by throwing the union of every format signal at
    `loaders()` — the same trick the test above uses — plus the return that
    short-circuits (an MLX whisper snapshot), because a code reachable only
    from a branch below it would otherwise look absent. The NeMo ASR branch
    also short-circuits but, since D406, names no code — so it contributes
    nothing to `seen` and is exercised by the dedicated tests below instead.
    """
    seen = set()
    for repo_id in formats.MFLUX_VARIANTS:
        seen |= set(formats.loaders(
            repo_id=repo_id, names=set(), dirnames=set(formats.MFLUX_COMPONENTS),
            config={}, torch_weights=True))
    seen |= set(formats.loaders(
        repo_id="x/y", names={formats.CT2_WEIGHTS, formats.DIFFUSERS_INDEX},
        dirnames=set(), config={}, torch_weights=True))
    seen |= set(formats.loaders(
        repo_id="x/y", names={formats.MLX_WHISPER_WEIGHTS[0]}, dirnames=set(),
        config={}, torch_weights=False))
    seen |= set(formats.loaders(
        repo_id="x/y", names=set(), dirnames=set(),
        config={"quantization": {"group_size": 64, "bits": 4}}, torch_weights=True))
    seen |= set(formats.loaders(
        repo_id="x/y", names={"model.gguf"}, dirnames=set(), config={},
        torch_weights=False, gguf_architecture="qwen35"))
    seen |= set(formats.loaders(
        repo_id="x/y", names=set(), dirnames={formats.H3_COMPONENT},
        config={}, torch_weights=False))
    seen |= set(formats.loaders(
        repo_id="x/y",
        names={formats.LTX_SPLIT_MANIFEST, "transformer-distilled.safetensors"},
        dirnames=set(), config={}, torch_weights=True))
    # `mlx-embed`/`transformers-embed` short-circuit too (see `loaders()`'s own
    # comment on the branch), so — like the MLX whisper and Parakeet cases
    # above — a code reachable only from below it would otherwise look absent.
    seen |= set(formats.loaders(
        repo_id="x/y", names=set(), dirnames=set(),
        config={"model_type": "siglip"}, torch_weights=True))
    missing = _codes() - seen
    assert not missing, (
        f"{sorted(missing)} are registered runners that `loaders()` never "
        f"names, so a cached repo they can load gets no engine tag and no Load "
        f"button on the machines that resolve to them (see this test's "
        f"docstring). Add the code to the branch for the format it reads.")


@pytest.mark.parametrize("names,dirnames,config,torch,expected", [
    # One filename each, and each is the check the runner itself makes.
    ({"model.bin"}, set(), {}, False, {"faster-whisper"}),
    ({"weights.npz"}, set(), {}, False, {"mlx-whisper"}),
    ({"model_index.json"}, set(), {}, False, _IMAGE),
    # A directory of plain safetensors WITH a config is the text runner's —
    # which runner of that capability gets it is the registry's question, not
    # the format's.
    (set(), set(), {"model_type": "qwen3"}, True, _TEXT),
    # …and the checkpoint MLX packed itself, likewise.
    (set(), set(), {"quantization": {"group_size": 64, "bits": 4}}, True, {"mlx-text"}),
    # **Weights with NO config are nobody's**, and that is the SymphonyGen case:
    # `mlx_lm.load` resolves a checkpoint through `config.json`, so a directory
    # of `.pt` files whose extensions happen to match is not something this
    # engine can open. Claiming it was how a symbolic-music policy came to be
    # filed under text generation with a Load button.
    (set(), set(), {}, True, set()),
    # A quantization this build ships no package for is nobody's.
    (set(), set(), {"quantization_config": {"quant_method": "awq"}}, True, set()),
    # Nothing readable at all — the answer the page most needs to be able to give.
    ({"README.md"}, set(), {}, False, set()),
])
def test_loaders_reads_the_format_and_nothing_else(names, dirnames, config, torch, expected):
    assert set(formats.loaders(repo_id="org/m", names=names, dirnames=dirnames,
                               config=config, torch_weights=torch)) == expected


def test_a_root_level_gguf_needs_a_recognised_text_architecture_too():
    """A `.gguf` at the snapshot root is llama.cpp's format (SPEC AI-11) —
    but presence alone is a container fact, not a modality one
    (`has_gguf_weights`'s docstring), so `loaders()` only calls it decisively
    `llamacpp-text` when the file's OWN `general.architecture` metadata is a
    known causal-text one. Passed in by the caller rather than read from a
    real path here, since `loaders()` never does file I/O of its own."""
    names = {"model.Q4_K_M.gguf"}
    assert formats.loaders(
        repo_id="org/m", names=names, dirnames=set(), config={},
        torch_weights=False, gguf_architecture="qwen35") == formats.LLAMACPP_RUNNERS
    # No architecture read (a truncated peek, or the caller never asked) —
    # fails toward NOT decisive, never toward a guess.
    assert formats.loaders(
        repo_id="org/m", names=names, dirnames=set(), config={},
        torch_weights=False, gguf_architecture=None) == ()
    # A REAL architecture, just not a text one — `city96/FLUX.1-dev-gguf`'s
    # own metadata reads exactly this way (verified 2026-08-21).
    assert formats.loaders(
        repo_id="org/m", names=names, dirnames=set(), config={},
        torch_weights=False, gguf_architecture="flux") == ()


#: The config the newer quantized mlx-community whisper re-uploads carry:
#: OpenAI's own `ModelDimensions` fields, which neither a transformers nor a
#: NeMo checkpoint spells this way.
_MLX_WHISPER_CONFIG = {"n_mels": 80, "n_audio_ctx": 1500, "n_vocab": 51864,
                       "quantization": {"group_size": 64, "bits": 8}}


def test_a_SHARED_weight_name_needs_the_whisper_config_beside_it():
    """`model.safetensors` is every transformers repo's filename, so it claims
    mlx-whisper only together with the native whisper config — the layout
    whisper-tiny.en-8bit ships, which the filename test alone refused."""
    assert set(formats.loaders(
        repo_id="mlx-community/whisper-tiny.en-8bit",
        names={"model.safetensors", "config.json"}, dirnames=set(),
        config=_MLX_WHISPER_CONFIG, torch_weights=True)) == {"mlx-whisper"}
    # A transformers whisper config beside the same filename is the text
    # runners' business, exactly as before.
    assert set(formats.loaders(
        repo_id="openai/whisper-tiny.en",
        names={"model.safetensors", "config.json"}, dirnames=set(),
        config={"model_type": "whisper", "num_mel_bins": 80, "d_model": 384},
        torch_weights=True)) == _TEXT


def test_an_mlx_whisper_snapshot_is_NOT_offered_to_the_text_runners():
    """The quantized re-uploads carry an MLX `quantization` block, so without
    the exclusion the text branch would offer to load a speech model as a chat
    model — the Parakeet trap, arriving by a new route. The older
    `weights.safetensors` era had the same leak (`.safetensors` counts as torch
    weights), so that spelling is asserted too."""
    for names in ({"model.safetensors"}, {"weights.safetensors"}):
        codes = formats.loaders(
            repo_id="mlx-community/whisper-large-v3-turbo", names=names,
            dirnames=set(), config=_MLX_WHISPER_CONFIG, torch_weights=True)
        assert not (_TEXT & set(codes)), codes
        assert "mlx-whisper" in codes


#: What a Parakeet MLX snapshot looks like: transformers-shaped safetensors
#: beside a config that is NeMo's rather than transformers'.
_PARAKEET_CONFIG = {"target": "nemo.collections.asr.models.rnnt_bpe_models."
                              "EncDecRNNTBPEModel"}


def test_a_parakeet_snapshot_is_recognised_by_its_NEMO_config_and_matches_no_runner():
    """`model.safetensors` alone says nothing — it is the file every
    transformers repo carries. The `target` in config.json names the NeMo
    class the weights were exported from, which no text checkpoint has —
    and since D406 withdrew the `parakeet-mlx` runner that used to claim
    this format, recognising it now means matching NO runner at all rather
    than claiming a runner. THE TRAP (see the module's docstring and
    `loaders()`'s early return): a directory of safetensors is otherwise
    every text runner's format, so without the early return this snapshot
    would fall through and the AI Models page would offer to load a speech
    model as a chat model."""
    codes = formats.loaders(
        repo_id="mlx-community/parakeet-tdt-0.6b-v3",
        names={formats.PARAKEET_WEIGHTS, "config.json"}, dirnames=set(),
        config=_PARAKEET_CONFIG, torch_weights=True)
    assert codes == ()
    assert not (_TEXT & set(codes)), codes


def test_a_nemo_config_with_no_weights_beside_it_loads_nowhere():
    """Evidence in both halves, like every other format here: a config alone is
    a repo somebody uploaded the metadata of."""
    assert formats.loaders(repo_id="org/m", names={"config.json"}, dirnames=set(),
                           config=_PARAKEET_CONFIG, torch_weights=False) == ()


def test_a_NON_asr_nemo_target_falls_through_to_the_text_runners():
    """NeMo covers TTS and LLMs too, and no runner here loads either — the ASR
    prefix is what the check is on, not the word "nemo". Unlike an ASR target,
    a non-ASR one does NOT trip the early return, so a directory of
    safetensors beside it is business as usual: every text runner's."""
    codes = formats.loaders(
        repo_id="org/m", names={formats.PARAKEET_WEIGHTS}, dirnames=set(),
        config={"target": "nemo.collections.tts.models.FastPitchModel"},
        torch_weights=True)
    assert set(codes) == _TEXT


def test_an_ltx_split_snapshot_claims_ltx_video_and_nothing_else():
    """An mlx-forge split conversion of LTX-2.3 (`split_model.json` beside a
    `transformer-*.safetensors`) is a directory of plain safetensors — the
    SAME shape `mlx-text` reads — so without `loaders()`'s early return the
    fallthrough at the bottom would ALSO claim it, offering an LTX checkpoint
    as a chat model."""
    codes = formats.loaders(
        repo_id="dgrauet/ltx-2.3-mlx-q4",
        names={formats.LTX_SPLIT_MANIFEST, "transformer-distilled.safetensors",
              "transformer-dev.safetensors", "connector.safetensors"},
        dirnames=set(), config={}, torch_weights=True)
    assert set(codes) == {"ltx-video"}


def test_an_ordinary_mlx_text_snapshot_is_untouched_by_the_ltx_check():
    """The negative case `has_ltx_split_layout` exists to keep safe: a
    perfectly ordinary MLX text checkpoint (no `split_model.json`, no
    `transformer-*` naming) must still resolve to `mlx-text` alone.

    `config` is non-empty (D433's guard: `mlx-text` now REQUIRES a readable
    `config.json`, not merely its presence in `names`)."""
    codes = formats.loaders(
        repo_id="org/some-mlx-model", names={"model.safetensors", "config.json"},
        dirnames=set(), config={"model_type": "llama"}, torch_weights=True)
    assert set(codes) == _TEXT


def test_has_ltx_split_layout_needs_both_signals():
    """Neither the manifest alone nor a `transformer-*` name alone is
    enough — see `has_ltx_split_layout`'s own docstring for why a single
    signal is a thinner claim than this codebase accepts here."""
    assert not formats.has_ltx_split_layout({formats.LTX_SPLIT_MANIFEST})
    assert not formats.has_ltx_split_layout({"transformer-dev.safetensors"})
    assert formats.has_ltx_split_layout(
        {formats.LTX_SPLIT_MANIFEST, "transformer-distilled.safetensors"})


def test_a_parakeet_repo_is_no_longer_in_DECISIVE():
    """`parakeet-mlx` was withdrawn (D406) and was never re-added to
    `DECISIVE` — there is no code left for that table to name. The NeMo ASR
    format is still decisive about matching nothing, but `loaders()`'s early
    return enforces that directly rather than through this table."""
    assert "parakeet-mlx" not in formats.DECISIVE


def test_DECISIVE_follows_the_FORMAT_and_not_the_hardware():
    """Membership in `DECISIVE` is a claim about the format, so every hardware
    variant of a decisive runner is decisive.

    A `model_index.json` is a diffusion pipeline whichever wheel opens it, and
    `ai_models.py` infers a cached repo's capability from the first decisive
    runner among its loaders — so listing only the CPU row would make that
    inference depend on which builds happen to be registered. The safetensors
    text runner is the counter-case in the same assertion: a directory of
    safetensors says nothing about the modality on any wheel.
    """
    assert set(formats.DIFFUSERS_RUNNERS) <= set(formats.DECISIVE)
    assert not _TEXT & set(formats.DECISIVE)
    assert set(formats.LLAMACPP_RUNNERS) <= set(formats.DECISIVE)


def test_mflux_needs_the_variant_table_as_well_as_the_layout():
    """Both halves, because a snapshot can be perfect MLX and still be a model
    this build cannot name a variant class for — which is what the runner's own
    `load()` raises about before it looks at the folder."""
    components = set(formats.MFLUX_COMPONENTS)
    known = next(iter(formats.MFLUX_VARIANTS))
    assert "mflux-image" in formats.loaders(
        repo_id=known, names=set(), dirnames=components, config={}, torch_weights=False)
    assert "mflux-image" not in formats.loaders(
        repo_id="someone/else-mlx", names=set(), dirnames=components, config={},
        torch_weights=False)


def test_mflux_edit_recipe_DERIVES_config_and_vae_rather_than_duplicating():
    """`MFLUX_EDIT_VARIANTS` carries only `variant`/`module` — the two facts
    that actually differ between editing and plain generation. `config` and
    `vae` come out of `MFLUX_VARIANTS`'s row for the SAME id, so the two
    tables cannot independently say something different about a checkpoint's
    architecture or latent space."""
    known = next(iter(formats.MFLUX_VARIANTS))
    plain = formats.MFLUX_VARIANTS[known]
    recipe = formats.mflux_edit_recipe(known)
    assert recipe["config"] == plain["config"]
    assert recipe["vae"] == plain["vae"]
    assert recipe["variant"] == formats.MFLUX_EDIT_VARIANTS[known]["variant"]
    assert recipe["module"] == formats.MFLUX_EDIT_VARIANTS[known]["module"]
    # The two facts this table exists to hold are NOT duplicated onto the
    # edit row — proving the derivation is real rather than a coincidence
    # between two copies that happen to still agree.
    assert "config" not in formats.MFLUX_EDIT_VARIANTS[known]
    assert "vae" not in formats.MFLUX_EDIT_VARIANTS[known]


def test_mflux_edit_recipe_TRACKS_a_change_to_the_plain_rows_config(monkeypatch):
    """The drift this derivation makes structurally impossible: editing the
    plain row's `config`/`vae` — the kind of edit a re-fitted preview matrix
    or a renamed `ModelConfig` method would require — must reach the edit
    recipe with NO second edit anywhere, because there is no second copy to
    forget."""
    known = next(iter(formats.MFLUX_VARIANTS))
    changed = dict(formats.MFLUX_VARIANTS[known])
    changed["config"] = "some_other_config_method"
    changed["vae"] = "SomeOtherVAE"
    monkeypatch.setitem(formats.MFLUX_VARIANTS, known, changed)
    recipe = formats.mflux_edit_recipe(known)
    assert recipe["config"] == "some_other_config_method"
    assert recipe["vae"] == "SomeOtherVAE"


def test_mflux_edit_recipe_is_None_without_a_row_in_EITHER_table(monkeypatch):
    known = next(iter(formats.MFLUX_VARIANTS))
    assert formats.mflux_edit_recipe("no/such-model") is None
    monkeypatch.delitem(formats.MFLUX_EDIT_VARIANTS, known)
    assert formats.mflux_edit_recipe(known) is None


def test_mflux_edit_recipe_tolerates_a_plain_row_with_NO_vae(monkeypatch):
    """`MFLUX_VARIANTS`'s own docstring declares `vae` optional — "a variant
    with no `vae` simply gets no preview" — and `_build_variant` reads it
    with `.get()`. The derivation in `mflux_edit_recipe` must not turn that
    into a `KeyError` a plain row itself would never raise."""
    known = next(iter(formats.MFLUX_VARIANTS))
    no_vae = {k: v for k, v in formats.MFLUX_VARIANTS[known].items() if k != "vae"}
    monkeypatch.setitem(formats.MFLUX_VARIANTS, known, no_vae)
    recipe = formats.mflux_edit_recipe(known)
    assert recipe is not None
    assert recipe.get("vae") is None


def test_a_root_level_gguf_is_llamacpp_texts_and_nothing_elses():
    """A `.gguf` at the snapshot root is llama.cpp's format (SPEC AI-11) —
    checked case-insensitively, and decisive against a stray safetensors file
    that would otherwise also make this a text-runner repo — PROVIDED the
    file's own architecture is a recognised text one (`is_text_gguf`); see
    `test_a_root_level_gguf_needs_a_recognised_text_architecture_too` for the
    negative case this test does not cover."""
    assert formats.has_gguf_weights({"Qwen3.5-9B-Q4_K_M.GGUF"})
    assert not formats.has_gguf_weights({"config.json", "README.md"})
    codes = formats.loaders(
        repo_id="unsloth/Qwen3.5-9B-GGUF",
        names={"Qwen3.5-9B-Q4_K_M.gguf", "model.safetensors"}, dirnames=set(),
        config={}, torch_weights=True, gguf_architecture="qwen35")
    assert codes == formats.LLAMACPP_RUNNERS


def test_gguf_architecture_is_read_from_a_real_gguf_header(tmp_path):
    """`gguf_architecture`/`is_text_gguf` end to end, against bytes shaped
    exactly like a real file's header (`general.architecture` as KV index 0,
    a length-prefixed string) — verified against a real downloaded
    `unsloth/Qwen3.5-4B-GGUF` file (byte offset 70) and a real
    `city96/FLUX.1-dev-gguf` file (`general.architecture = "flux"`, 3 KV
    pairs total), both checked 2026-08-21."""
    import struct

    def make_gguf(pairs):
        buf = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + \
            struct.pack("<Q", len(pairs))
        for key, value in pairs:
            buf += struct.pack("<Q", len(key.encode())) + key.encode()
            buf += struct.pack("<I", 8)  # GGUF string type
            buf += struct.pack("<Q", len(value.encode())) + value.encode()
        return buf

    text_path = tmp_path / "text.gguf"
    text_path.write_bytes(
        make_gguf([("general.architecture", "qwen35"), ("general.name", "x")]))
    image_path = tmp_path / "image.gguf"
    image_path.write_bytes(make_gguf([("general.architecture", "flux")]))
    bad_path = tmp_path / "bad.gguf"
    bad_path.write_bytes(b"NOTGGUF")
    missing_path = str(tmp_path / "does-not-exist.gguf")

    assert formats.gguf_architecture(str(text_path)) == "qwen35"
    assert formats.is_text_gguf(str(text_path)) is True
    assert formats.gguf_architecture(str(image_path)) == "flux"
    assert formats.is_text_gguf(str(image_path)) is False
    assert formats.gguf_architecture(str(bad_path)) is None
    assert formats.gguf_architecture(missing_path) is None


def test_gguf_block_count_is_read_from_a_real_gguf_header(tmp_path):
    """`gguf_block_count`, the layer count `llama_text.py`'s offload backoff
    sizes itself against — verified against real downloaded headers rather
    than assumed: `unsloth/Qwen3.5-4B-GGUF` and `unsloth/Qwen3.5-9B-GGUF`
    both carry `qwen35.block_count = 32`, and `unsloth/Qwen3.8-27B-GGUF`
    carries `qwen35.block_count = 65`, all checked 2026-08-21. Matched by
    SUFFIX (`.block_count`), not by requiring the caller to already know the
    architecture prefix — see the function's own docstring."""
    import struct

    def make_gguf(pairs):
        buf = b"GGUF" + struct.pack("<I", 3) + struct.pack("<Q", 0) + \
            struct.pack("<Q", len(pairs))
        for key, value_type, value in pairs:
            buf += struct.pack("<Q", len(key.encode())) + key.encode()
            buf += struct.pack("<I", value_type)
            if value_type == 8:  # GGUF string type
                buf += struct.pack("<Q", len(value.encode())) + value.encode()
            else:  # uint32
                buf += struct.pack("<I", value)
        return buf

    with_layers = tmp_path / "with_layers.gguf"
    with_layers.write_bytes(make_gguf([
        ("general.architecture", 8, "qwen35"),
        ("qwen35.block_count", 4, 32),
        ("qwen35.context_length", 4, 262144),
    ]))
    no_layers = tmp_path / "no_layers.gguf"
    no_layers.write_bytes(make_gguf([("general.architecture", 8, "qwen35")]))
    bad_path = tmp_path / "bad.gguf"
    bad_path.write_bytes(b"NOTGGUF")

    assert formats.gguf_block_count(str(with_layers)) == 32
    assert formats.gguf_block_count(str(no_layers)) is None
    assert formats.gguf_block_count(str(bad_path)) is None
    assert formats.gguf_block_count(str(tmp_path / "does-not-exist.gguf")) is None


# -- pick_gguf_file (D412, Piece 1) ------------------------------------------
#
# Deterministic ranking over a repo's own file listing, so a bare Hub repo id
# means the same bytes on every machine (see the module note above
# `pick_gguf_file`'s definition for why hardware never enters this).


def test_pick_gguf_file_prefers_q4_k_m_over_every_other_named_suffix():
    """The community's own floor for "still reliable" and the branch's own
    curated table's cheapest tier — never the true smallest quant a repo
    might publish, because a pick that is too small downloads exactly as
    many bytes and then answers worse, which nothing here can fix
    afterward."""
    names = ["m-Q8_0.gguf", "m-Q6_K.gguf", "m-Q5_K_M.gguf", "m-IQ4_XS.gguf",
             "m-Q4_K_S.gguf", "m-Q4_K_M.gguf", "m-Q4_0.gguf"]
    assert formats.pick_gguf_file(names) == "m-Q4_K_M.gguf"


def test_pick_gguf_file_ranks_named_families_smallest_reasonable_first():
    assert formats.pick_gguf_file(["m-Q8_0.gguf", "m-Q5_K_M.gguf"]) == "m-Q5_K_M.gguf"
    assert formats.pick_gguf_file(["m-Q6_K.gguf", "m-Q8_0.gguf"]) == "m-Q6_K.gguf"


def test_pick_gguf_file_excludes_multi_part_shards():
    """`download()` fetches exactly one file — a split quantization can never
    be assembled by this runner's own download path, so it must never be the
    answer even when nothing else is on offer."""
    names = ["m-BF16-00001-of-00002.gguf", "m-BF16-00002-of-00002.gguf"]
    assert formats.pick_gguf_file(names) is None


def test_pick_gguf_file_excludes_subdirectories():
    """`BF16/` (unquantized) and `MTP/` (speculative-decoding auxiliary) —
    observed live on real repos — plus every split shard in the sample,
    which happened to live under one of these two, for free."""
    names = ["BF16/m-BF16.gguf", "MTP/mtp-m-Q4_0.gguf", "m-Q4_K_M.gguf"]
    assert formats.pick_gguf_file(names) == "m-Q4_K_M.gguf"


def test_pick_gguf_file_excludes_auxiliary_weights_by_name():
    """`mmproj`/`mtp`/`draft`/`projector` — the widened scan's finding: BARE
    substrings, not anchored ones (`RVN-Q4_K_M-mtp.gguf`'s dash comes BEFORE
    "mtp", which an anchored `mtp[-_]` pattern would have missed)."""
    names = [
        "m-Q4_K_M.gguf", "m-mmproj-Q8_0.gguf", "RVN-Q4_K_M-mtp.gguf",
        "m-draft-Q8_0.gguf", "vision_f16_projector.gguf",
    ]
    assert formats.pick_gguf_file(names) == "m-Q4_K_M.gguf"
    # And with the chat model itself removed, every remaining file is
    # auxiliary and NONE of them may be offered as a fallback.
    names.remove("m-Q4_K_M.gguf")
    assert formats.pick_gguf_file(names) is None


def test_pick_gguf_file_ranks_unsloth_dynamic_quants_below_plain_quants():
    """Eligible, per the branch's own curated `UD-Q3_K_XL` entry — but ranked
    below every plain quant of a named family, since a plain quant needs no
    per-layer engineering to stay usable at that width."""
    names = ["m-UD-Q4_K_XL.gguf", "m-Q5_K_M.gguf"]
    assert formats.pick_gguf_file(names) == "m-Q5_K_M.gguf"


def test_pick_gguf_file_prefers_a_named_family_over_a_plain_sub_4bit_quant():
    """A PLAIN sub-4-bit quant (no dynamic per-layer allocation behind it) is
    never PREFERRED — ranking excludes it whenever something better
    competes — but a `UD-` dynamic quant of the identical bit width is
    eligible and ranks ABOVE it once both are candidates, since the dynamic
    allocation is specifically engineered to stay usable at that width and a
    uniform quant at the same width is not."""
    assert formats.pick_gguf_file(["m-Q3_K_M.gguf", "m-UD-Q3_K_XL.gguf"]) == "m-UD-Q3_K_XL.gguf"
    assert formats.pick_gguf_file(["m-UD-Q3_K_XL.gguf"]) == "m-UD-Q3_K_XL.gguf"
    # A named family always outranks an unranked-family dynamic quant too.
    names = ["m-UD-Q3_K_XL.gguf", "m-Q8_0.gguf"]
    assert formats.pick_gguf_file(names) == "m-Q8_0.gguf"


def test_pick_gguf_file_still_falls_back_to_a_lone_plain_sub_4bit_quant():
    """Excluded from RANKING, not from candidacy outright — with nothing
    else to compete against, the single-candidate fallback still applies:
    the same "no ambiguity, nothing to guess between" reasoning that lets a
    lone unsuffixed file resolve also covers a repo that published exactly
    one (aggressive) quantization."""
    assert formats.pick_gguf_file(["m-Q3_K_M.gguf"]) == "m-Q3_K_M.gguf"


def test_pick_gguf_file_falls_back_to_the_one_unranked_candidate():
    """No clear Q4_K_M (or any recognised suffix) did not occur in the
    plan's own 5-repo sample, but must not be assumed impossible — a lone
    candidate with nothing to disambiguate against is the one case this
    picker resolves anyway, the same "no ambiguity" rule
    `llama_text._resolve_model_id` already uses for a single curated
    recipe."""
    assert formats.pick_gguf_file(["model.gguf"]) == "model.gguf"
    assert formats.pick_gguf_file(["model-BF16.gguf"]) == "model-BF16.gguf"


def test_pick_gguf_file_refuses_when_multiple_candidates_have_no_signal():
    """More than one unranked file and nothing to break the tie with — do
    NOT silently pick the smallest, since a `mmproj` or a draft model is
    also small and is not a chat model (the exact failure mode this whole
    picker exists to avoid)."""
    assert formats.pick_gguf_file(["model-a.gguf", "model-b.gguf"]) is None


def test_pick_gguf_file_returns_none_for_an_empty_or_non_gguf_listing():
    assert formats.pick_gguf_file([]) is None
    assert formats.pick_gguf_file(["README.md", "config.json"]) is None


def test_a_gguf_inside_a_subfolder_is_not_a_root_level_snapshot():
    """`names` is the snapshot's TOP-LEVEL listing only (`ai_models.py` builds
    it with `os.listdir`, never a recursive walk) — a GGUF one directory down,
    the shape the diffusers FLUX recipe's component repo uses
    (`COMPONENT_REPOS`), must not make an unrelated snapshot read as this
    engine's. Simulated here by simply leaving the file out of `names`, since
    the function has no path information beyond that set."""
    assert formats.loaders(
        repo_id="org/m", names={"model_index.json"}, dirnames={"transformer"},
        config={}, torch_weights=False) == tuple(formats.DIFFUSERS_RUNNERS)


def test_a_gguf_repo_SETTLES_what_the_model_is():
    """`DECISIVE` is the list of formats whose evidence also names the
    modality — a `.gguf` is llama.cpp's and nothing else in this app reads one
    for text, so the page's tag does not have to hedge."""
    assert "llamacpp-text" in formats.DECISIVE


def test_the_gguf_branch_is_genuinely_exclusive_even_against_a_diffusers_index():
    """Code review finding 5: the GGUF branch used to be appended AFTER the
    unconditional `DIFFUSERS_INDEX` check rather than checked ahead of it, so
    a (contrived, but not impossible — nothing stops a Hub repo bundling
    both) snapshot carrying BOTH `model_index.json` and a root `.gguf` came
    back `(*DIFFUSERS_RUNNERS, "llamacpp-text")`. Because `llamacpp-text` is
    registered ahead of the diffusers rows, `ai_models._engine`'s
    `decisive[0]` (picked in REGISTRY order, not in `loaders()`'s append
    order) would have labelled a diffusion pipeline as text generation. The
    branch now runs first and returns unconditionally, so this must come
    back GGUF-only."""
    codes = formats.loaders(
        repo_id="org/both", names={formats.DIFFUSERS_INDEX, "model.gguf"},
        dirnames=set(), config={}, torch_weights=False,
        gguf_architecture="qwen35")
    assert codes == formats.LLAMACPP_RUNNERS


def test_a_ct2_whisper_repo_needs_more_than_the_filename():
    """`model.bin` is what the loader checks, and it is not enough to put a
    TASK on a card: the page says "speech recognition" off this, and a stray
    pickle called model.bin is not a Whisper model."""
    assert formats.is_ct2_whisper({"model.bin", "preprocessor_config.json"}, {})
    assert formats.is_ct2_whisper({"model.bin"}, {"alignment_heads": [[1, 2]]})
    assert not formats.is_ct2_whisper({"model.bin"}, {})


# -- the repos this app downloads on its own behalf ------------------------------


def test_every_component_repo_says_what_it_is_a_part_of():
    """The registry the AI Models page reads to explain a row nobody chose.

    Each entry must carry the four things a card needs — the file we fetch, the
    thing it belongs to, the noun, and the sentence — because a half-filled
    entry renders as the mystery row this table exists to abolish."""
    assert formats.COMPONENT_REPOS, "the registry cannot be empty"
    for repo_id, entry in formats.COMPONENT_REPOS.items():
        assert "/" in repo_id, repo_id
        assert entry["file"] and not entry["file"].startswith("/"), repo_id
        assert entry["owner"] and entry["part"] and entry["what"], repo_id
        # `of` is a repo id or None (a component of an ENGINE, like the VAD).
        assert entry["of"] is None or "/" in entry["of"], repo_id
        assert formats.component(repo_id) is entry
    assert formats.component("mlx-community/whisper-small-mlx") is None


def test_the_vad_reads_its_file_from_the_registry():
    """`vad.py` names the detector, and the page names it too. One copy: the
    shared module at the runners root reads `formats`, so a moved repo cannot
    quietly become an unexplained row."""
    import importlib.util
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fused_render", "ai", "runners", "vad.py",
    )
    spec = importlib.util.spec_from_file_location("vad_for_formats", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.REPO in formats.COMPONENT_REPOS
    assert formats.COMPONENT_REPOS[module.REPO]["file"] == module.FILE


# -- embeddings ------------------------------------------------------------------


def _siglip_config():
    return {"model_type": "siglip"}


def _clip_config():
    return {"model_type": "clip"}


def test_a_siglip_snapshot_resolves_to_both_embedding_runners():
    codes = formats.loaders(
        repo_id="google/siglip2-base-patch16-384", names={"model.safetensors"},
        dirnames=set(), config=_siglip_config(), torch_weights=True)
    assert set(codes) == {"mlx-embed", "transformers-embed"}


def test_a_clip_snapshot_resolves_to_transformers_embed_only():
    """mlx-embeddings 0.1.x has a `siglip` module and no `clip` one
    (`MLX_EMBED_MODEL_TYPES`) — the one difference between the two families
    this app treats identically everywhere else."""
    codes = formats.loaders(
        repo_id="openai/clip-vit-base-patch32", names={"model.safetensors"},
        dirnames=set(), config=_clip_config(), torch_weights=True)
    assert codes == ("transformers-embed",)


def test_an_embed_config_with_no_torch_weights_loads_nowhere():
    """A `model_type: siglip` config with nothing but a README is not a
    loadable snapshot — `torch_weights` is what tells the two apart, the same
    guard the text branch at the bottom of `loaders()` has."""
    codes = formats.loaders(
        repo_id="x/y", names=set(), dirnames=set(),
        config=_siglip_config(), torch_weights=False)
    assert codes == ()


def test_a_siglip_snapshot_is_NOT_offered_to_the_text_runners():
    """The `DECISIVE`/short-circuit rule `is_parakeet_checkpoint` and
    `is_mlx_whisper_snapshot` both rely on: a dual encoder is a directory of
    safetensors, so without the early `return` the text branch below would
    claim it too and the page would offer a Load button for a chat model that
    can never generate a token."""
    codes = formats.loaders(
        repo_id="google/siglip2-base-patch16-384", names={"model.safetensors"},
        dirnames=set(), config=_siglip_config(), torch_weights=True)
    # `mlx-text` spelled literally, per `_TEXT`'s own note above: since D416 it
    # is the one runner left that reads a bare directory of safetensors, so a
    # family tuple would have nothing to hold together.
    assert "mlx-text" not in codes
    assert set(codes) == {"mlx-embed", "transformers-embed"}


def test_embed_model_type_is_case_and_whitespace_tolerant():
    assert formats.embed_model_type({"model_type": " SigLIP "}) == "siglip"
    assert formats.embed_model_type({"model_type": "CLIP"}) == "clip"


def test_embed_model_type_rejects_anything_else():
    assert formats.embed_model_type({"model_type": "llama"}) is None
    assert formats.embed_model_type({}) is None
    assert formats.embed_model_type({"model_type": 123}) is None


def test_the_embed_codes_are_decisive():
    """A directory of safetensors says nothing about the modality on its own
    (`DECISIVE`'s own comment) — a `siglip`/`clip` config is what settles it,
    exactly like a Parakeet NeMo target or an MLX whisper weights file."""
    assert "mlx-embed" in formats.DECISIVE
    assert "transformers-embed" in formats.DECISIVE
