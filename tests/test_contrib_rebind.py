"""`classify.rebind` (docs/CONTRIB_LOOP.md §19.2, §19.7): a record made
before its folder had a GitHub `origin` is reclassified in place once it has
one — and the records that must never move don't.

What a wrong answer would cost: an own app whose record says `repo: null`
forever (no Marketplace strip, no install link — the bug §19 fixes); a
GitHub-link copy whose re-pointed origin is adopted as its source (I2: the
contributor's changes would go wherever an app re-pointed them); a
Marketplace copy turned into a plain copy (M4); a Try copy treated as an app.
"""
from __future__ import annotations

import os
import time

import pytest

from tests._contrib_gh_world import git, write_prefs

PAGE = "<!doctype html><html><head><meta name=\"fused-app\" /></head></html>\n"


@pytest.fixture
def w(tmp_path, monkeypatch):
    from fused_render.contrib import classify, gh, store

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv(gh.FAKE_ENV, str(tmp_path / "github"))
    fake = gh.fake()
    for k, v in fake.git_env().items():
        monkeypatch.setenv(k, v)
    write_prefs()
    gh.forget_identity()
    with classify._role_lock:
        classify._role_cache.clear()
    fake.add_user("alice", "Alice")
    fake.add_user("bob", "Bob")
    fake.create_repo_as("alice", "sine", {"index.html": PAGE})
    fake.create_repo_as("bob", "tool", {"index.html": PAGE})
    fake.sign_in("alice")

    def app(name, *, kind="own", repo=None, origin=None, stamp=True, **extra):
        d = tmp_path / "ws" / "app" / name
        d.mkdir(parents=True)
        (d / "index.html").write_text(PAGE)
        git(d, "init", "-q")
        git(d, "add", "-A")
        git(d, "-c", "user.name=A", "-c", "user.email=a@x", "commit", "-q", "-m", "one")
        rec = store.new_record(kind, repo=repo, classified_by="test")
        rec.update(extra)
        store.put(str(d), rec)
        if stamp:
            git(d, "config", "--local", "fused.copyid", rec["copy_id"])
        if origin:
            git(d, "remote", "add", "origin", f"https://github.com/{origin}")
            git(d, "fetch", "-q", "origin")
        return store.key(str(d))

    return type("W", (), {"fake": fake, "app": staticmethod(app), "store": store,
                          "classify": classify})


def test_an_own_record_with_no_repo_is_rebound(w):
    d = w.app("mine", origin="alice/sine")
    out = w.classify.rebind(d)
    assert out["kind"] == "own" and out["repo"] == "alice/sine"
    assert out["owner_checked"] is True and out["classified_by"] == "rebind"
    assert out["branch"] == "main"
    assert any(a["kind"] == "rebind" for a in w.store.activity(d))
    assert w.classify.kind_of(d) == "own"
    assert w.classify.rebind(d) is None  # nothing left to do


def test_a_scratch_record_with_an_origin_is_rebound(w):
    d = w.app("scr", kind="scratch", origin="alice/sine")
    out = w.classify.rebind(d)
    assert out["kind"] == "own" and out["repo"] == "alice/sine"


def test_someone_elses_origin_reads_github(w):
    d = w.app("theirs", origin="bob/tool")
    out = w.classify.rebind(d)
    assert out["kind"] == "github" and out["repo"] == "bob/tool"
    assert out["owner_checked"] is True


def test_unknown_ownership_is_the_safe_direction(w):
    d = w.app("unknown", origin="alice/sine")
    w.fake.sign_out()
    w.classify._gh().forget_identity()
    out = w.classify.rebind(d)
    assert out["kind"] == "github" and out["owner_checked"] is False


def test_a_try_copy_is_never_rebound(w):
    d = w.app("try", kind="scratch", origin="alice/sine",
              try_of={"app": "/x", "number": 1, "head_sha": "a" * 40})
    assert w.classify.rebind(d) is None
    assert w.store.get(d)["kind"] == "scratch"


def test_a_marketplace_record_is_never_touched(w):
    d = w.app("market", kind="marketplace", repo="bob/tool", origin="alice/sine")
    assert w.classify.rebind(d) is None
    raw = w.store.get_raw(d)
    assert raw["kind"] == "marketplace" and raw["repo"] == "bob/tool"


def test_a_github_copys_repointed_origin_is_not_adopted(w):
    """I2: the record's repo stands; `_point_back` undoes the origin."""
    d = w.app("copy", kind="github", repo="bob/tool", origin="alice/sine")
    assert w.classify.rebind(d) is None
    assert w.store.get_raw(d)["repo"] == "bob/tool"
    assert not w.classify._needs_rebind(d, w.store.get_raw(d))


def test_an_own_record_that_names_a_repo_keeps_it(w):
    """§17.6: an own copy's origin is its author's to change (a rename);
    the record's repo stands."""
    d = w.app("renamed", repo="alice/old-name", origin="alice/sine")
    assert w.classify.rebind(d) is None
    assert w.store.get_raw(d)["repo"] == "alice/old-name"


def test_a_folder_without_a_github_origin_is_left_alone(w):
    d = w.app("local")
    assert w.classify.rebind(d) is None
    assert w.store.get(d)["repo"] is None


def test_an_unstamped_record_is_still_found(w):
    """A folder git-init'ed after its record was made has no copy id: the
    store's binding would drop it on `get` — rebind reads the raw row."""
    d = w.app("bare", stamp=False, origin="alice/sine")
    out = w.classify.rebind(d)
    assert out is not None and out["repo"] == "alice/sine"


def test_kick_rebinds_in_the_background(w):
    d = w.app("kicked", origin="alice/sine")
    assert w.classify.kind_of(d, kick=True) == "own"
    end = time.time() + 10
    while time.time() < end and (w.store.get_raw(d) or {}).get("repo") is None:
        time.sleep(0.05)
    assert w.store.get_raw(d)["repo"] == "alice/sine"
    assert w.classify.kind_of(d) == "own"


def test_ensure_rebinds_too(w):
    d = w.app("ensured", origin="alice/sine")
    out = w.classify.ensure(d)
    assert out["repo"] == "alice/sine"


def test_rebind_on_a_folder_with_no_record_does_nothing(w, tmp_path):
    d = tmp_path / "ws" / "app" / "none"
    d.mkdir(parents=True)
    assert w.classify.rebind(str(d)) is None
    assert not os.path.exists(os.path.join(d, ".git"))
