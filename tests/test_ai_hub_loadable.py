"""Tests for `fused_render.ai.hub_loadable` — the runner-aware admission
facts behind item 2 (scope-corrected, see DECISIONS.md D1274/D1275): never a
reason to DROP a Hub search row, only to flag one the runner ACTIVE for its
capability will refuse once downloaded.
"""
import pytest

from fused_render.ai import hub_loadable
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


def test_any_other_runner_is_the_any_kind_with_no_data():
    assert hub_loadable.loadable_kind("llamacpp-text") == ("any", None)
    assert hub_loadable.loadable_kind("diffusers-image") == ("any", None)


def test_admission_allowlist_kind_flags_a_repo_outside_the_two_variants():
    variant_id = next(iter(formats.MFLUX_VARIANTS))
    loadable, reason = hub_loadable.admission(
        runner_code="mflux-image", runner_short="mflux",
        model_id=variant_id, model_type=None)
    assert (loadable, reason) == (True, None)

    loadable, reason = hub_loadable.admission(
        runner_code="mflux-image", runner_short="mflux",
        model_id="some-org/unrelated-flux-repo", model_type=None)
    assert loadable is False
    assert reason == "mflux only loads FLUX.2 Klein"


def test_admission_model_types_kind_unknown_set_never_flags(monkeypatch):
    """The venv-not-installed-yet case: `mlx_vlm_model_types()` returns
    `None` ("unknown"), and an unknown set must never read as "loads
    nothing" — every row stays loadable until the venv exists to ask."""
    monkeypatch.setattr(hub_loadable, "mlx_vlm_model_types", lambda: None)
    loadable, reason = hub_loadable.admission(
        runner_code="mlx-text", runner_short="mlx-vlm",
        model_id="org/whatever", model_type="totally_unknown_arch")
    assert (loadable, reason) == (True, None)


def test_admission_model_types_kind_flags_an_unsupported_architecture(monkeypatch):
    monkeypatch.setattr(hub_loadable, "mlx_vlm_model_types",
                         lambda: frozenset({"llama", "qwen2"}))
    loadable, reason = hub_loadable.admission(
        runner_code="mlx-text", runner_short="mlx-vlm",
        model_id="org/some-repo", model_type="neo_chat")
    assert loadable is False
    assert reason == "neo_chat not supported by mlx-vlm"

    loadable, reason = hub_loadable.admission(
        runner_code="mlx-text", runner_short="mlx-vlm",
        model_id="org/some-repo", model_type="llama")
    assert (loadable, reason) == (True, None)


def test_admission_model_types_kind_with_no_model_type_at_all_stays_loadable(monkeypatch):
    """A row whose `config.model_type` this server could not read (no
    `config` expand on an old server, or a malformed one) must not be
    flagged just because there is nothing to check it against — an unknown
    reading of the ROW is the same "don't flag" verdict as an unknown
    reading of the RUNNER's set."""
    monkeypatch.setattr(hub_loadable, "mlx_vlm_model_types",
                         lambda: frozenset({"llama"}))
    loadable, reason = hub_loadable.admission(
        runner_code="mlx-text", runner_short="mlx-vlm",
        model_id="org/some-repo", model_type=None)
    assert (loadable, reason) == (True, None)


def test_any_kind_never_flags_regardless_of_model_type_or_id():
    loadable, reason = hub_loadable.admission(
        runner_code="llamacpp-text", runner_short="llama.cpp",
        model_id="org/anything", model_type="anything")
    assert (loadable, reason) == (True, None)


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
