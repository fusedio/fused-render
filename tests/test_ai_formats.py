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
    assert named <= _codes(), sorted(named - _codes())


@pytest.mark.parametrize("names,dirnames,config,torch,expected", [
    # One filename each, and each is the check the runner itself makes.
    ({"model.bin"}, set(), {}, False, {"faster-whisper"}),
    ({"weights.npz"}, set(), {}, False, {"mlx-whisper"}),
    ({"model_index.json"}, set(), {}, False, {"diffusers-image"}),
    # A directory of plain safetensors is BOTH text runners' — which of them
    # gets it is the registry's question, not the format's.
    (set(), set(), {}, True, {"mlx-text", "transformers-text"}),
    # …unless the checkpoint is MLX's own, which torch cannot read at all.
    (set(), set(), {"quantization": {"group_size": 64, "bits": 4}}, True, {"mlx-text"}),
    # A quantization this build ships no package for is nobody's.
    (set(), set(), {"quantization_config": {"quant_method": "awq"}}, True, set()),
    # Nothing readable at all — the answer the page most needs to be able to give.
    ({"model.Q4_K_M.gguf"}, set(), {}, False, set()),
])
def test_loaders_reads_the_format_and_nothing_else(names, dirnames, config, torch, expected):
    assert set(formats.loaders(repo_id="org/m", names=names, dirnames=dirnames,
                               config=config, torch_weights=torch)) == expected


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
