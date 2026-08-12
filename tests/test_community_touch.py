"""The community marketplace's `touch` action and its catalog merge.

`core_apps/community/community.py` records "this app was opened" per slug in
~/.fused-render/community/opened.json (action `touch`), and `catalog` folds
the timestamp into every app entry as `opened_at` — what the /apps hub's
community tab sorts by. Pinned here: the write is recorded and re-read, an
invalid slug is refused, and apps never opened report opened_at None.
"""
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.join(os.path.dirname(_HERE), "core_apps", "community")


@pytest.fixture()
def community_mod(tmp_path, monkeypatch):
    """community.py with its state + cache pointed at tmp."""
    monkeypatch.syspath_prepend(_APP)
    sys.modules.pop("community", None)
    import community as mod

    state = tmp_path / "state"
    cache = state / "repo"
    monkeypatch.setattr(mod, "STATE_DIR", str(state))
    monkeypatch.setattr(mod, "CACHE_REPO", str(cache))
    monkeypatch.setattr(mod, "INSTALLS_JSON", str(state / "installs.json"))
    monkeypatch.setattr(mod, "OPENED_JSON", str(state / "opened.json"))
    return mod


def _fake_cache(mod, apps):
    os.makedirs(os.path.join(mod.CACHE_REPO, ".git"), exist_ok=True)
    with open(os.path.join(mod.CACHE_REPO, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"apps": apps}, f)


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
