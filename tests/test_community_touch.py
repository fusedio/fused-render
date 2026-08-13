"""The community marketplace's `touch` action and its catalog merge.

`fused_render/community.py` records "this app was opened" per slug in
~/.fused-render/community/opened.json (action `touch`), and `catalog` folds
the timestamp into every app entry as `opened_at` — what the /apps hub's
community tab sorts by. Pinned here: the write is recorded and re-read, an
invalid slug is refused, and apps never opened report opened_at None.
"""
import json
import os

import pytest


@pytest.fixture()
def community_mod(tmp_path, monkeypatch):
    """fused_render.community with its state + cache pointed at tmp."""
    from fused_render import community as mod

    state = tmp_path / "state"
    monkeypatch.setattr(mod, "STATE_DIR", str(state))
    monkeypatch.setattr(mod, "SHOWCASE_DIR", str(tmp_path / "workspace" / "showcase"))
    monkeypatch.setattr(mod, "INSTALLS_JSON", str(state / "installs.json"))
    monkeypatch.setattr(mod, "OPENED_JSON", str(state / "opened.json"))
    return mod


def _fake_cache(mod, apps):
    os.makedirs(os.path.join(mod.SHOWCASE_DIR, ".git"), exist_ok=True)
    for app in apps:
        folder = os.path.join(mod.SHOWCASE_DIR, app["slug"])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "metadata.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in app.items() if k != "slug"}, f)


def test_touch_records_and_catalog_merges(community_mod):
    mod = community_mod
    _fake_cache(mod, [{"slug": "sine-wave", "name": "Sine wave"},
                      {"slug": "calc", "name": "Calculator"}])

    res = mod.main(action="touch", slug="sine-wave")
    assert res["status"] == "ok"
    assert isinstance(res["opened_at"], float)

    catalog = mod.main(action="catalog")
    assert catalog["status"] == "ok"
    by_slug = {a["slug"]: a for a in catalog["apps"]}
    assert by_slug["sine-wave"]["opened_at"] == res["opened_at"]
    assert by_slug["calc"]["opened_at"] is None


def test_touch_updates_existing_timestamp(community_mod):
    mod = community_mod
    first = mod.main(action="touch", slug="calc")["opened_at"]
    second = mod.main(action="touch", slug="calc")["opened_at"]
    assert second >= first
    with open(mod.OPENED_JSON, encoding="utf-8") as f:
        assert set(json.load(f)["opened"]) == {"calc"}


def test_touch_rejects_bad_slug(community_mod):
    res = community_mod.main(action="touch", slug="../etc")
    assert res["status"] == "error"
    assert "invalid app slug" in res["message"]
