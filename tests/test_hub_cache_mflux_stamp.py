"""`hub_cache._repo_meta` re-derives `loaders` when the installed mflux registry
changes, without the snapshot being touched. Uses a real tmp venv file, so the
real `mflux_available_models` and `mflux_registry_stamp` are what must agree."""
import os
import types

import pytest

from fused_render import envinstall
from fused_render.ai import hub_cache, hub_loadable, registry
from fused_render.ai.runners import formats

REPO = "black-forest-labs/flux-3-image-klein"
SOURCE = '''
AVAILABLE_MODELS = {
    "flux3-image-klein": ModelConfig(
        priority=11, aliases=["flux3"],
        model_name="black-forest-labs/flux-3-image-klein", base_model=None),
}
'''


@pytest.fixture(autouse=True)
def _clean():
    hub_loadable.reset_cache()
    hub_cache._META_CACHE.clear()
    formats.set_mflux_registry_reader(None)
    yield
    hub_loadable.reset_cache()
    hub_cache._META_CACHE.clear()
    formats.set_mflux_registry_reader(None)


def _repo(tmp_path):
    repo_dir = tmp_path / "models--black-forest-labs--flux-3-image-klein"
    snap = repo_dir / "snapshots" / "c1"
    for d in ("transformer", "text_encoder", "vae"):
        (snap / d).mkdir(parents=True)
    (snap / "model_index.json").write_text("{}")
    (repo_dir / "refs").mkdir()
    (repo_dir / "refs" / "main").write_text("c1")
    return repo_dir


def _venv(tmp_path, monkeypatch):
    venv_dir = tmp_path / "venv"
    runner = types.SimpleNamespace(folder=str(tmp_path / "project"))
    monkeypatch.setattr(registry, "by_code", lambda code: runner)
    monkeypatch.setattr(envinstall, "venv_dir_for", lambda folder: str(venv_dir))
    return venv_dir


def _install(venv_dir):
    cfg = (venv_dir / "lib" / "python3.12" / "site-packages" / "mflux"
           / "models" / "common" / "config")
    cfg.mkdir(parents=True)
    (cfg / "model_config.py").write_text(SOURCE)


def test_loaders_gain_mflux_once_the_registry_appears(tmp_path, monkeypatch):
    repo_dir = _repo(tmp_path)
    venv_dir = _venv(tmp_path, monkeypatch)

    assert "mflux-image" not in hub_cache._repo_meta(str(repo_dir)).loaders
    _install(venv_dir)
    assert "mflux-image" in hub_cache._repo_meta(str(repo_dir)).loaders


def test_forty_repo_meta_calls_stat_the_registry_once(tmp_path, monkeypatch):
    repo_dir = _repo(tmp_path)
    venv_dir = _venv(tmp_path, monkeypatch)
    _install(venv_dir)
    real_stat = os.stat
    calls = []

    def counting_stat(path, *a, **kw):
        if str(path).endswith("model_config.py"):
            calls.append(path)
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(hub_loadable.os, "stat", counting_stat)
    for _ in range(40):
        hub_cache._repo_meta(str(repo_dir))
    assert len(calls) == 1
