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


@pytest.fixture(autouse=True)
def _clear_cache():
    hub_loadable.reset_cache()
    yield
    hub_loadable.reset_cache()


def test_mflux_image_is_an_allowlist_of_its_two_klein_repos():
    kind, data = hub_loadable.loadable_kind("mflux-image")
    assert kind == "allowlist"
    assert data == frozenset(formats.MFLUX_VARIANTS)


def test_mlx_text_is_a_model_types_kind():
    kind, _data = hub_loadable.loadable_kind("mlx-text")
    assert kind == "model_types"


def test_ltx_video_is_a_file_layout_kind():
    assert hub_loadable.loadable_kind("ltx-video") == ("file_layout", None)


def test_any_other_runner_is_the_any_kind_with_no_data():
    assert hub_loadable.loadable_kind("llamacpp-text") == ("any", None)
    assert hub_loadable.loadable_kind("diffusers-image") == ("any", None)


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


def test_admission_allowlist_kind_admits_a_listed_variant():
    variant_id = next(iter(formats.MFLUX_VARIANTS))
    loadable, reason, runs_on = hub_loadable.admission(
        runner_codes=("mflux-image",), model_id=variant_id, model_type=None)
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
    variant_id = next(iter(formats.MFLUX_VARIANTS))
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
    variant_id = next(iter(formats.MFLUX_VARIANTS))
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
