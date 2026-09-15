"""Tests for `fused_render.ai.hub_loadable` — the runner-aware admission
facts behind item 2 (scope-corrected, see DECISIONS.md D1274/D1275) and item
3 of the architecture-detection brief (D1287+): never a reason to DROP a Hub
search row, only to flag one that EVERY runner AVAILABLE for its capability
would refuse once downloaded.
"""
import pytest

from fused_render.ai import hub_loadable
from fused_render.ai.hub_architecture import Architecture
from fused_render.ai.runners import formats

#: Captured before the autouse `_force_mflux_registry` fixture below ever
#: monkeypatches `hub_loadable.mflux_loadable_repos` — a reference to the
#: REAL implementation, for the one test that exercises it directly
#: (`test_mflux_loadable_repos_a_repo_id_shared_by_two_registry_entries_
#: dedupes_cleanly`). `monkeypatch.setattr` rebinds the module attribute,
#: not this already-bound reference, so calling it here always runs the
#: real function regardless of what the autouse fixture did to the name.
_real_mflux_loadable_repos = hub_loadable.mflux_loadable_repos


@pytest.fixture(autouse=True)
def _clear_cache():
    hub_loadable.reset_cache()
    yield
    hub_loadable.reset_cache()


#: A forced stand-in for `mflux_loadable_repos()`'s real return — the two
#: CANONICAL repo ids the old hand-typed `MFLUX_VARIANTS` allowlist's two
#: entries actually resolve to under mflux's own `AVAILABLE_MODELS`
#: (`black-forest-labs/FLUX.2-klein-4B` / `-9B`, not the `mlx-community`
#: quantized ids `MFLUX_VARIANTS` was keyed by — see
#: `test_flux2_klein_mlx_community_ids_stay_loadable_via_their_base_model_tag`
#: below for why those need their `base_model:` tag instead).
_DEFAULT_MFLUX_REPOS = frozenset({
    "black-forest-labs/FLUX.2-klein-4B",
    "black-forest-labs/FLUX.2-klein-9B",
})


@pytest.fixture(autouse=True)
def _force_mflux_registry(monkeypatch):
    """Per `tests-inherit-dev-machine-runner-resolution`: every test below
    that touches `mflux-image` must judge admission against an EXPLICITLY
    forced reading of mflux's registry, never this dev machine's real
    installed one. Without this, a CI box with no mflux venv installed
    would get `mflux_loadable_repos() is None` (the venv-absent "admit
    everything" reading) and silently invert every refusal assertion in
    this file. A test that needs the venv-absent behavior itself, or a
    different registry, overrides this with its own `monkeypatch.setattr`
    call."""
    monkeypatch.setattr(hub_loadable, "mflux_loadable_repos", lambda: _DEFAULT_MFLUX_REPOS)


def test_mflux_image_is_a_mflux_kind_backed_by_the_derived_registry(monkeypatch):
    """Round "derive mflux admission from the installed engine": `mflux-
    image` no longer reports the old two-entry hand-typed allowlist —
    `loadable_kind` now delegates to `mflux_loadable_repos()`, whatever it
    returns."""
    monkeypatch.setattr(hub_loadable, "mflux_loadable_repos",
                         lambda: frozenset({"org/base-model"}))
    assert hub_loadable.loadable_kind("mflux-image") == ("mflux", frozenset({"org/base-model"}))


def test_mlx_text_is_a_model_types_kind():
    kind, _data = hub_loadable.loadable_kind("mlx-text")
    assert kind == "model_types"


def test_ltx_video_is_a_file_layout_kind():
    assert hub_loadable.loadable_kind("ltx-video") == ("file_layout", None)


def test_any_other_runner_is_the_any_kind_with_no_data():
    assert hub_loadable.loadable_kind("llamacpp-text") == ("any", None)


def test_diffusers_runners_are_the_diffusers_kind():
    """Round: "Diffusers admission is unconditional" — the three Diffusers
    runner codes get a real admission rule now, not the `"any"` default
    that used to rubber-stamp every row (D1302)."""
    assert hub_loadable.loadable_kind("diffusers-image") == ("diffusers", None)
    assert hub_loadable.loadable_kind("diffusers-image-cuda") == ("diffusers", None)
    assert hub_loadable.loadable_kind("diffusers-image-rocm") == ("diffusers", None)


def test_admission_admits_when_any_available_runner_would_load_it():
    """Two available runners, and only the non-active one admits — no
    chip. `admission()` widened from the single ACTIVE runner to every
    AVAILABLE one specifically so this stops being flagged. No
    `active_runner_code` given here, so `runs_on` stays `None` too (see the
    dedicated `runs_on` tests below for that field's own behaviour)."""
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image", "diffusers-image"),
        model_id="some-org/unrelated-flux-repo", model_type=None)
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_flags_when_the_sole_available_runner_refuses():
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="some-org/unrelated-flux-repo", model_type=None)
    assert loadable is False
    assert reason == "no engine here loads this"
    assert runs_on is None


def test_admission_mflux_kind_admits_when_the_rows_own_id_is_in_the_registry():
    """mflux's own `exact_match` rule: the row's own id names a registry
    entry directly (a straight browse of the canonical, un-quantized
    repo)."""
    variant_id = next(iter(_DEFAULT_MFLUX_REPOS))
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",), model_id=variant_id, model_type=None)
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_mflux_kind_admits_via_base_model_tag_explicit_base_rule():
    """mflux's own `explicit_base` rule, and the case the brief calls out as
    the one that matters in practice: the row's OWN id is a quantized
    conversion the registry has never heard of, but its `base_model:` tag
    names the canonical repo the registry DOES carry."""
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="mlx-community/FLUX.2-Klein-4B-4bit", model_type=None,
        base_model="black-forest-labs/FLUX.2-klein-4B")
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_mflux_kind_refuses_when_neither_id_nor_base_model_resolves():
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="some-org/unrelated-repo", model_type=None,
        base_model="some-org/also-unrelated")
    assert loadable is False
    assert reason == "no engine here loads this"
    assert runs_on is None


def test_admission_mflux_kind_venv_absent_admits_everything(monkeypatch):
    """The critical case from the brief: when the mflux venv is not
    installed yet, `mflux-image` must NOT refuse every row — that would
    flag the entire image capability on a machine that has simply not
    installed the engine yet, mirroring `mlx_vlm_model_types`'s own
    venv-absent convention exactly."""
    monkeypatch.setattr(hub_loadable, "mflux_loadable_repos", lambda: None)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="literally-anything/at-all", model_type=None)
    assert (loadable, reason, runs_on) == (True, None, None)


def test_mflux_loadable_repos_a_repo_id_shared_by_two_registry_entries_dedupes_cleanly(
        tmp_path, monkeypatch):
    """`resolve_key`'s own docstring in mflux's `config_resolution.py` warns
    that several registry entries SHARE a repo id (the FLUX.1-dev
    ControlNets; z-image-turbo and its ControlNet) and are matched by
    object IDENTITY, not by name — a flat `repo_id -> variant` dict cannot
    represent that. This module never builds one: `mflux_loadable_repos`
    only collects a SET of ids, so two entries sharing `model_name` simply
    de-duplicate into one set entry, never crash, and admission never has
    to pick which registry entry "won" — there is nothing to pick."""
    import types as _types

    from fused_render import envinstall
    from fused_render.ai import registry

    venv_dir = tmp_path / "venv"
    config_dir = (venv_dir / "lib" / "python3.12" / "site-packages" / "mflux"
                  / "models" / "common" / "config")
    config_dir.mkdir(parents=True)
    (config_dir / "model_config.py").write_text('''
AVAILABLE_MODELS = {
    "dev": ModelConfig(
        priority=0,
        aliases=["dev"],
        model_name="black-forest-labs/FLUX.1-dev",
        base_model=None,
    ),
    "dev-controlnet-canny": ModelConfig(
        priority=6,
        aliases=["dev-controlnet-canny"],
        model_name="black-forest-labs/FLUX.1-dev",
        base_model=None,
        controlnet_model="InstantX/FLUX.1-dev-Controlnet-Canny",
    ),
    "flux2-klein-4b": ModelConfig(
        priority=11,
        aliases=["flux2-klein-4b", "klein-4b"],
        model_name="black-forest-labs/FLUX.2-klein-4B",
        base_model=None,
    ),
}
''')
    runner = _types.SimpleNamespace(folder=str(tmp_path / "project"))
    monkeypatch.setattr(registry, "by_code", lambda code: runner)
    monkeypatch.setattr(envinstall, "venv_dir_for", lambda folder: str(venv_dir))

    result = _real_mflux_loadable_repos()
    assert result == frozenset({
        "black-forest-labs/FLUX.1-dev",
        "black-forest-labs/FLUX.2-klein-4B",
    })

    # And feeding that derived set through admission() for either of the two
    # entries sharing "black-forest-labs/FLUX.1-dev" never raises and always
    # admits — membership, not identity, is all admission ever asks.
    monkeypatch.setattr(hub_loadable, "mflux_loadable_repos", lambda: result)
    for model_id in ("black-forest-labs/FLUX.1-dev", "black-forest-labs/FLUX.2-klein-4B"):
        loadable, reason, runs_on = hub_loadable.admission(
            runner_codes=("mflux-image",), model_id=model_id, model_type=None)
        assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_names_the_architecture_that_would_load_it():
    """D1287 item 3: when nothing available admits, the reason NAMES what
    would — replacing the old bespoke "switch to Diffusers to run this"
    (D1278) with the generic architecture-shaped vocabulary."""
    architecture = Architecture(name="MiniMaxH3Pipeline", engine="Diffusers",
                                 shipped=True)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="some-org/unrelated-flux-repo", model_type=None,
        architecture=architecture)
    assert loadable is False
    assert reason == "needs Diffusers (MiniMaxH3Pipeline)"
    assert runs_on is None


def test_admission_names_engine_alone_when_architecture_has_no_name():
    architecture = Architecture(name=None, engine="Diffusers", shipped=True)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="some-org/unrelated-flux-repo", model_type=None,
        architecture=architecture)
    assert loadable is False
    assert reason == "needs Diffusers"
    assert runs_on is None


def test_admission_suppresses_name_that_merely_stutters_the_engine():
    """Finding 5: `name_is_bare_library` says `name` is nothing more than
    the same word as `engine` ("diffusers" / "Diffusers") — the parenthetical
    must not repeat it."""
    architecture = Architecture(name="diffusers", engine="Diffusers", shipped=True,
                                 name_is_bare_library=True)
    loadable, reason, _runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="some-org/unrelated-flux-repo", model_type=None,
        architecture=architecture)
    assert loadable is False
    assert reason == "needs Diffusers"


def test_admission_says_not_supported_yet_when_engine_is_not_shipped():
    architecture = Architecture(name=None, engine="Sentence Transformers",
                                 shipped=False)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="some-org/unrelated-flux-repo", model_type=None,
        architecture=architecture)
    assert loadable is False
    assert reason == "needs Sentence Transformers — not supported yet"
    assert runs_on is None


def test_admission_leads_with_architecture_name_when_engine_is_not_shipped():
    """Defect fix: a video row whose `engine` resolves to "Diffusers" (an
    IMAGE-only engine in this app) or "MLX" (a TEXT-only engine) must not
    tell the user to get that engine — it must name the actual model
    family. `MiniMax-H3` is the name recovered from the row's own
    `base_model:` tag (see `test_ai_hub_architecture.py`)."""
    architecture = Architecture(name="MiniMax-H3", engine="Diffusers",
                                 shipped=False)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("ltx-video",),
        model_id="OzzyGT/MiniMax_H3_sdnq_8bit_pruned", model_type=None,
        architecture=architecture)
    assert loadable is False
    assert reason == "MiniMax-H3 not supported yet"
    assert runs_on is None

    mlx_architecture = Architecture(name="MiniMax-H3", engine="MLX", shipped=False)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("ltx-video",),
        model_id="pipenetwork/MiniMax-H3-MLX-4bit", model_type=None,
        architecture=mlx_architecture)
    assert loadable is False
    assert reason == "MiniMax-H3 not supported yet"


def test_admission_says_no_engine_loads_name_when_architecture_has_no_engine():
    architecture = Architecture(name="SomeWeirdArch", engine=None, shipped=False)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id="some-org/unrelated-flux-repo", model_type=None,
        architecture=architecture)
    assert loadable is False
    assert reason == "no engine loads SomeWeirdArch yet"
    assert runs_on is None


def test_admission_model_types_kind_unknown_set_never_flags(monkeypatch):
    """The venv-not-installed-yet case: `mlx_vlm_model_types()` returns
    `None` ("unknown"), and an unknown set must never read as "loads
    nothing" — every row stays loadable until the venv exists to ask."""
    monkeypatch.setattr(hub_loadable, "mlx_vlm_model_types", lambda: None)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mlx-text",),
        model_id="org/whatever", model_type="totally_unknown_arch")
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_model_types_kind_flags_an_unsupported_architecture(monkeypatch):
    monkeypatch.setattr(hub_loadable, "mlx_vlm_model_types",
                         lambda: frozenset({"llama", "qwen2"}))
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mlx-text",),
        model_id="org/some-repo", model_type="neo_chat")
    assert loadable is False
    assert reason == "neo_chat not supported by mlx-vlm"
    assert runs_on is None

    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mlx-text",),
        model_id="org/some-repo", model_type="llama")
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_model_types_kind_with_no_model_type_at_all_stays_loadable(monkeypatch):
    """A row whose `config.model_type` this server could not read (no
    `config` expand on an old server, or a malformed one) must not be
    flagged just because there is nothing to check it against — an unknown
    reading of the ROW is the same "don't flag" verdict as an unknown
    reading of the RUNNER's set."""
    monkeypatch.setattr(hub_loadable, "mlx_vlm_model_types",
                         lambda: frozenset({"llama"}))
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mlx-text",),
        model_id="org/some-repo", model_type=None)
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_two_available_runners_both_model_types_kind_still_flags_generically(monkeypatch):
    """The `"<model_type> not supported by mlx-vlm"` wording is preserved
    ONLY for the single-available-runner case — with more than one
    refusing runner it falls back to the generic, architecture-shaped
    reason, since naming just one of several refusals by name would be
    misleading."""
    monkeypatch.setattr(hub_loadable, "mlx_vlm_model_types",
                         lambda: frozenset({"llama"}))
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mlx-text", "mflux-image"),
        model_id="org/some-repo", model_type="neo_chat")
    assert loadable is False
    assert reason == "no engine here loads this"
    assert runs_on is None


def test_admission_file_layout_kind_admits_the_curated_ltx_layout():
    names = frozenset({formats.LTX_SPLIT_MANIFEST,
                        "transformer-distilled.safetensors"})
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("ltx-video",), model_id="org/repo", model_type=None,
        names=names)
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_file_layout_kind_flags_a_repo_missing_the_layout():
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("ltx-video",), model_id="org/repo", model_type=None,
        names=frozenset({"model_index.json"}),
        architecture=Architecture(name="LTXPipeline", engine="Diffusers",
                                   shipped=True))
    assert loadable is False
    assert reason == "needs Diffusers (LTXPipeline)"
    assert runs_on is None


# Round: "Diffusers admission is unconditional" (D1302) ---------------------


def test_lance_3b_is_not_loadable_by_diffusers_and_names_the_architecture():
    """The proof case: `mlx-community/Lance-3B-bf16`'s real shape — `mlx`
    library, no `model_index.json`/`modular_model_index.json`, runs on
    `lance-mlx` (not shipped, not on PyPI). Reaches the image capability via
    the deliberate `image-to-image` widening (D1235, untouched here). Every
    available runner must refuse: `mflux-image`'s allowlist (this id is not
    one of the two Klein repos) AND `diffusers-image`'s new rule (no
    Diffusers signal in `library_name` or `names`)."""
    names = frozenset({"config.json", "llm_config.json", "generation_config.json",
                        "model.safetensors", "vae.safetensors", "vit.safetensors",
                        "tokenizer.json"})
    architecture = Architecture(name="Qwen2_5_VLForConditionalGeneration",
                                 engine=None, shipped=False)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image", "diffusers-image"),
        model_id="mlx-community/Lance-3B-bf16", model_type="qwen2_5_vl",
        names=names, library_name="mlx", architecture=architecture)
    assert loadable is False
    assert reason == "no engine loads Qwen2_5_VLForConditionalGeneration yet"
    assert runs_on is None


def test_genuine_diffusers_repo_with_model_index_stays_loadable():
    """A real Diffusers repo (`model_index.json` sibling) — `diffusers-image`
    must still admit it, and `runs_on` still names it when the ACTIVE
    runner is `mflux-image` (which refuses everything but its 2-entry
    allowlist)."""
    names = frozenset({formats.DIFFUSERS_INDEX, "model_index.json_meta"})
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image", "diffusers-image"),
        model_id="stabilityai/sd-xl", model_type=None,
        names=names, active_runner_code="mflux-image")
    assert loadable is True
    assert reason is None
    assert runs_on == "Diffusers"


def test_modular_diffusers_repo_with_only_the_modular_index_stays_loadable():
    """A modular Diffusers pipeline ships `modular_model_index.json` with NO
    flat `model_index.json` — must still admit via `diffusers-image`."""
    names = frozenset({formats.DIFFUSERS_MODULAR_INDEX})
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("diffusers-image",),
        model_id="OzzyGT/MiniMax_H3_sdnq_4bit_pruned", model_type=None,
        names=names)
    assert (loadable, reason, runs_on) == (True, None, None)


def test_library_name_diffusers_with_no_index_file_stays_loadable():
    """`library_name: "diffusers"` alone (no `names` at all, or a `names`
    set with no index file) is a real, independent Diffusers signal —
    `is_diffusers_repo` honors it even without a manifest sibling."""
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("diffusers-image",),
        model_id="some-org/diffusers-lib-repo", model_type=None,
        names=frozenset(), library_name="diffusers")
    assert (loadable, reason, runs_on) == (True, None, None)


def test_caller_with_neither_names_nor_library_name_never_flags_a_diffusers_row():
    """The never-drops-a-row guarantee: a caller that has not read either
    signal (both left at their defaults) must not have that absence read as
    "not Diffusers" — it must stay loadable, exactly like every other kind's
    unknown-reading convention in this module."""
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("diffusers-image",),
        model_id="some-org/unknown-shape-repo", model_type=None)
    assert (loadable, reason, runs_on) == (True, None, None)


def test_flux2_klein_mlx_community_ids_stay_loadable_via_their_base_model_tag():
    """The two `mlx-community` ids the OLD hand-typed `MFLUX_VARIANTS`
    allowlist named directly are themselves quantized conversions — neither
    is a `model_name` mflux's own registry carries (that's
    `black-forest-labs/FLUX.2-klein-4B`/`-9B`). They stay loadable, no chip,
    via their own `base_model:` tag instead — the same fact `hub_models.py`
    already parses to show "from black-forest-labs/FLUX.2-klein-*B" under
    each row, now also threaded into admission."""
    cases = (
        ("mlx-community/FLUX.2-Klein-4B-4bit", "black-forest-labs/FLUX.2-klein-4B"),
        ("mlx-community/flux2-klein-9b-4bit", "black-forest-labs/FLUX.2-klein-9B"),
    )
    for variant_id, canonical_base in cases:
        loadable, reason, runs_on = hub_loadable.admission(
            runner_codes=("mflux-image", "diffusers-image"),
            model_id=variant_id, model_type=None, base_model=canonical_base,
            active_runner_code="mflux-image")
        assert (loadable, reason, runs_on) == (True, None, None)


def test_any_kind_never_flags_regardless_of_model_type_or_id():
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("llamacpp-text",),
        model_id="org/anything", model_type="anything")
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_with_no_available_runners_at_all_stays_loadable():
    """Should not happen in production (`_model_row` only calls this when
    `for_capability` resolved a runner), but an empty set must read the
    same "unknown = loadable" way every other absent fact does here."""
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=(), model_id="org/anything", model_type=None)
    assert (loadable, reason, runs_on) == (True, None, None)


# Finding 1 of the follow-up review: a third, neutral row state -------------


def test_admission_silent_when_the_active_runner_itself_admits():
    """Active runner admits -> nothing new: no reason, no `runs_on` info,
    exactly like today."""
    variant_id = next(iter(_DEFAULT_MFLUX_REPOS))
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image", "diffusers-image"),
        model_id=variant_id, model_type=None,
        active_runner_code="mflux-image")
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_names_the_engine_when_active_refuses_but_another_admits():
    """The exact bug report: `stabilityai/sd-xl` (a plain Diffusers repo,
    not an mflux-curated variant) with `mflux-image` ACTIVE and
    `diffusers-image` merely available. `loadable` stays True (unchanged
    ranking/sorting), `reason` stays None (no warning chip), but `runs_on`
    now names the engine that WOULD load it, for a neutral, non-warning
    chip."""
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image", "diffusers-image"),
        model_id="stabilityai/sd-xl", model_type=None,
        active_runner_code="mflux-image")
    assert loadable is True
    assert reason is None
    assert runs_on == "Diffusers"


def test_admission_no_runs_on_when_active_runner_code_is_unknown():
    """`active_runner_code=None` (the default — a caller that has not
    resolved one) must not fabricate a `runs_on` chip out of "I don't know
    which one is active" — preserves every pre-existing call site's
    behaviour untouched."""
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image", "diffusers-image"),
        model_id="stabilityai/sd-xl", model_type=None)
    assert loadable is True
    assert reason is None
    assert runs_on is None


def test_admission_no_runs_on_when_only_one_runner_available_at_all(monkeypatch):
    """A single available runner that itself is active and admits: no
    `runs_on` — there is no "another" runner to name."""
    variant_id = next(iter(_DEFAULT_MFLUX_REPOS))
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",),
        model_id=variant_id, model_type=None,
        active_runner_code="mflux-image")
    assert (loadable, reason, runs_on) == (True, None, None)


def test_admission_runs_on_falls_back_to_none_when_admitting_runner_has_no_family_label(monkeypatch):
    """`_engine_name` reads `Runner.family_label` off the registry — a code
    the registry does not recognise (should not happen in production, but
    this module never assumes) must not raise, just read as "nothing to
    name"."""
    monkeypatch.setattr(hub_loadable, "_engine_name", lambda code: None)
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image", "diffusers-image"),
        model_id="stabilityai/sd-xl", model_type=None,
        active_runner_code="mflux-image")
    assert loadable is True
    assert reason is None
    assert runs_on is None


def test_mlx_vlm_model_types_returns_none_when_the_venv_is_not_installed(monkeypatch):
    import types as _types

    from fused_render import envinstall
    from fused_render.ai import registry

    runner = _types.SimpleNamespace(folder="/does/not/exist")
    monkeypatch.setattr(registry, "by_code", lambda code: runner)
    monkeypatch.setattr(envinstall, "venv_dir_for", lambda folder: "/does/not/exist/.venv")
    assert hub_loadable.mlx_vlm_model_types() is None


def test_mlx_vlm_model_types_introspects_the_installed_venvs_models_package(tmp_path, monkeypatch):
    import types as _types

    from fused_render import envinstall
    from fused_render.ai import registry

    venv_dir = tmp_path / "venv"
    models_dir = venv_dir / "lib" / "python3.12" / "site-packages" / "mlx_vlm" / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "llama.py").write_text("")
    (models_dir / "__init__.py").write_text("")
    (models_dir / "_helper.py").write_text("")
    pkg_arch = models_dir / "qwen2_vl"
    pkg_arch.mkdir()
    (pkg_arch / "__init__.py").write_text("")

    runner = _types.SimpleNamespace(folder=str(tmp_path / "project"))
    monkeypatch.setattr(registry, "by_code", lambda code: runner)
    monkeypatch.setattr(envinstall, "venv_dir_for", lambda folder: str(venv_dir))

    result = hub_loadable.mlx_vlm_model_types()
    assert result == frozenset({"llama", "qwen2_vl"})


def test_mlx_vlm_model_types_caches_by_venv_dir(tmp_path, monkeypatch):
    import types as _types

    from fused_render import envinstall
    from fused_render.ai import registry

    calls = []

    def fake_venv_dir_for(folder):
        calls.append(folder)
        return str(tmp_path / "venv")

    runner = _types.SimpleNamespace(folder=str(tmp_path / "project"))
    monkeypatch.setattr(registry, "by_code", lambda code: runner)
    monkeypatch.setattr(envinstall, "venv_dir_for", fake_venv_dir_for)

    first = hub_loadable.mlx_vlm_model_types()
    second = hub_loadable.mlx_vlm_model_types()
    # A cache HIT still calls `venv_dir_for` (cheap, needed as the cache key)
    # but must not re-glob/re-listdir a filesystem path that does not exist
    # — both calls return the identical (cached) `None` with no error.
    assert first is None
    assert second is None
    assert len(calls) == 2
