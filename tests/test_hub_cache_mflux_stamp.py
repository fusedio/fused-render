"""`hub_cache._repo_meta` re-derives `loaders` when the installed mflux registry
changes, without the snapshot being touched."""
from fused_render.ai import hub_cache, hub_loadable
from fused_render.ai.runners import formats

REPO = "black-forest-labs/flux-3-image-klein"


def test_loaders_gain_mflux_once_the_registry_appears(tmp_path, monkeypatch):
    repo_dir = tmp_path / "models--black-forest-labs--flux-3-image-klein"
    snap = repo_dir / "snapshots" / "c1"
    for d in ("transformer", "text_encoder", "vae"):
        (snap / d).mkdir(parents=True)
    (snap / "model_index.json").write_text("{}")
    (repo_dir / "refs").mkdir()
    (repo_dir / "refs" / "main").write_text("c1")
    hub_cache._META_CACHE.clear()

    state = {"registry": None, "stamp": None}
    monkeypatch.setattr(hub_loadable, "mflux_available_models", lambda: state["registry"])
    monkeypatch.setattr(hub_loadable, "mflux_registry_stamp", lambda: state["stamp"])
    formats.set_mflux_registry_reader(None)

    assert "mflux-image" not in hub_cache._repo_meta(str(repo_dir)).loaders
    state["registry"] = {REPO: "flux3-image-klein"}
    state["stamp"] = 123
    assert "mflux-image" in hub_cache._repo_meta(str(repo_dir)).loaders
    hub_cache._META_CACHE.clear()
