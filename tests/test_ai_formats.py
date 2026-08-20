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


#: The runners a plain directory of torch safetensors belongs to, and the ones a
#: `model_index.json` belongs to — spelled once here because the per-hardware
#: split made them three codes apiece, and a test that listed them by hand in
#: every case would be the same drift `loaders()` itself avoids by extending a
#: tuple. Read from the module under test on purpose: what these tests pin is
#: the BRANCHES (which format reaches which family), and the membership of a
#: family is pinned by `test_every_registered_runner_appears_in_loaders` below.
_TEXT = set(formats.TRANSFORMERS_RUNNERS) | {"mlx-text"}
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
    `loaders()` — the same trick the test above uses — plus the two returns that
    short-circuit (an MLX whisper snapshot and a Parakeet one), because a code
    reachable only from a branch below one of those would otherwise look absent.
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
        repo_id="x/y", names={formats.PARAKEET_WEIGHTS}, dirnames=set(),
        config={"target": formats.NEMO_ASR_TARGET + "rnnt_bpe_models.X"},
        torch_weights=True))
    seen |= set(formats.loaders(
        repo_id="x/y", names=set(), dirnames=set(),
        config={"quantization": {"group_size": 64, "bits": 4}}, torch_weights=True))
    seen |= set(formats.loaders(
        repo_id="x/y", names={"model.gguf"}, dirnames=set(), config={},
        torch_weights=False, gguf_architecture="qwen35"))
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
    # A directory of plain safetensors is EVERY text runner's — which of them
    # gets it is the registry's question, not the format's.
    (set(), set(), {}, True, _TEXT),
    # …unless the checkpoint is MLX's own, which torch cannot read at all.
    (set(), set(), {"quantization": {"group_size": 64, "bits": 4}}, True, {"mlx-text"}),
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
        torch_weights=False, gguf_architecture="qwen35") == ("llamacpp-text",)
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


def test_a_parakeet_snapshot_is_recognised_by_its_NEMO_config():
    """`model.safetensors` alone says nothing — it is the file every
    transformers repo carries. What settles it is the `target` in config.json,
    which names the NeMo class the weights were exported from and which no text
    checkpoint has."""
    assert set(formats.loaders(
        repo_id="mlx-community/parakeet-tdt-0.6b-v3",
        names={formats.PARAKEET_WEIGHTS, "config.json"}, dirnames=set(),
        config=_PARAKEET_CONFIG, torch_weights=True)) == {"parakeet-mlx"}


def test_a_parakeet_snapshot_is_NOT_offered_to_the_text_runners():
    """The trap this guards: a directory of safetensors is normally both text
    runners', so without the exclusion the AI Models page would put a Load
    button on Parakeet for `mlx-text`, which would try to read a speech model
    as a chat model and fail several frames inside mlx-lm."""
    codes = formats.loaders(
        repo_id="mlx-community/parakeet-tdt-0.6b-v3",
        names={formats.PARAKEET_WEIGHTS}, dirnames=set(),
        config=_PARAKEET_CONFIG, torch_weights=True)
    assert not (_TEXT & set(codes)), codes


def test_a_nemo_config_with_no_weights_beside_it_loads_nowhere():
    """Evidence in both halves, like every other format here: a config alone is
    a repo somebody uploaded the metadata of."""
    assert formats.loaders(repo_id="org/m", names={"config.json"}, dirnames=set(),
                           config=_PARAKEET_CONFIG, torch_weights=False) == ()


def test_a_NON_asr_nemo_target_is_not_a_parakeet_repo():
    """NeMo covers TTS and LLMs too, and this runner loads neither — the ASR
    prefix is what the check is on, not the word "nemo"."""
    codes = formats.loaders(
        repo_id="org/m", names={formats.PARAKEET_WEIGHTS}, dirnames=set(),
        config={"target": "nemo.collections.tts.models.FastPitchModel"},
        torch_weights=True)
    assert "parakeet-mlx" not in codes


def test_a_parakeet_repo_SETTLES_what_the_model_is():
    """`DECISIVE` is the list of formats whose evidence also names the
    modality, and a NeMo ASR config does: it cannot be anything but speech
    recognition, so the page's tag does not have to hedge."""
    assert "parakeet-mlx" in formats.DECISIVE


def test_DECISIVE_follows_the_FORMAT_and_not_the_hardware():
    """Membership in `DECISIVE` is a claim about the format, so every hardware
    variant of a decisive runner is decisive.

    A `model_index.json` is a diffusion pipeline whichever wheel opens it, and
    `ai_models.py` infers a cached repo's capability from the first decisive
    runner among its loaders — so listing only the CPU row would make that
    inference depend on which builds happen to be registered. The text runners
    are the counter-case in the same assertion: a directory of safetensors says
    nothing about the modality on any wheel.
    """
    assert set(formats.DIFFUSERS_RUNNERS) <= set(formats.DECISIVE)
    assert not set(formats.TRANSFORMERS_RUNNERS) & set(formats.DECISIVE)


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
    assert codes == ("llamacpp-text",)


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
    assert codes == ("llamacpp-text",)


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
