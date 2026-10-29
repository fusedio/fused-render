"""The Community tab's ladder read (docs/CONTRIB_LOOP.md §19.2, §19.7) —
`contrib/ladder.py status` and `GET /api/contrib/ladder`, against a
FakeGitHub holding Alice's app repo and the Marketplace index (real bare
repos; every git process maps https://github.com/ to them).

What a wrong answer would cost: a rung the buttons then refuse (rung 3 with
the Marketplace switch off), a ladder offered on someone else's app, an
install link shown with its switch off, a page read that waits on GitHub.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from tests._contrib_gh_world import git, write_prefs

INDEX = "fusedio/apps-index"
REPO = "alice/sine-starter"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8" /><meta name="fused-app" />
<meta name="description" content="A sine wave to start from." />
<title>Sine Starter</title></head><body><p>sine</p></body></html>
"""


@pytest.fixture
def w(tmp_path, monkeypatch):
    from fused_render import github_setup
    from fused_render.contrib import gh, ladder, listing, refresh, store

    home, ws = tmp_path / "home", tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    monkeypatch.setenv(gh.FAKE_ENV, str(tmp_path / "github"))
    monkeypatch.setenv("FUSED_RENDER_MARKETPLACE_INDEX", INDEX)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    fake = gh.fake()
    for k, v in fake.git_env().items():
        monkeypatch.setenv(k, v)
    write_prefs(marketplace_enabled=True, contrib_enabled=True)
    gh.forget_identity()
    ladder.forget_caches()
    monkeypatch.setattr(refresh, "_INLINE", True)
    refresh._reset()
    monkeypatch.setattr(github_setup, "cached_summary",
                        lambda: {"found": True, "signed_in": True, "account": "alice",
                                 "stale": False})
    # share_app reads `remote get-url`, which answers the fake's insteadOf
    # rewrite (a local bare path) — its own tests cover the link rules; here
    # only the wiring and the switch are under test.
    from fused_render import share_app
    monkeypatch.setattr(share_app, "github_link_state",
                        lambda path, **_: {"ok": True, "repo": REPO, "code": None,
                                           "url": f"https://fused.io/install?repo={REPO}"})

    fake.add_user("alice", "Alice")
    fake.add_user("bob", "Bob")
    fake.add_user("fusedio")
    fake.create_repo_as("fusedio", "apps-index", {"README.md": "The index.\n"})
    fake.create_repo_as("alice", "sine-starter", {
        "index.html": PAGE, "README.md": "# Sine Starter\n",
        "icon.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>\n"})
    fake.sign_in("alice")

    (ws / "app").mkdir(parents=True)
    app = str(ws / "app" / "sine-starter")
    git(ws / "app", "clone", "-q", f"https://github.com/{REPO}", app)
    git(app, "config", "user.name", "Alice")
    git(app, "config", "user.email", "alice@example.com")
    rec = store.new_record("own", repo=REPO, branch="main", classified_by="install")
    store.put(app, rec)
    git(app, "config", "--local", "fused.copyid", rec["copy_id"])
    listing._index_branch_memo.clear()

    api = FastAPI()
    from fused_render.server.routers.ladder import router
    api.include_router(router)
    client = TestClient(api)

    def read(path, **extra):
        r = client.get("/api/contrib/ladder", params={"dir": path, **extra})
        assert r.status_code == 200
        return r.json()

    return type("W", (), {"fake": fake, "app": store.key(app), "ws": ws, "gh": gh,
                          "store": store, "read": staticmethod(read), "listing": listing})


def _fresh_app(ws, tag="app", name="fresh"):
    d = ws / tag / name
    d.mkdir(parents=True)
    (d / "index.html").write_text(PAGE)
    return str(d)


def test_a_scratch_folder_stands_on_rung_one(w):
    app = _fresh_app(w.ws)
    out = w.read(app)
    assert out["ok"] is True
    assert out["rung"] == 1
    assert out["mine"] is True and out["kind"] == "own"
    assert out["github"]["repo"] is None
    assert out["github"]["installed"] is True and out["github"]["signed_in"] is True
    assert out["github"]["login"] == "alice"
    assert out["github"]["blocked"] is None
    assert out["listing"] is None
    assert out["doctor"] == {"clear": None, "failing": None}
    assert out["flags"] == {"sources": True, "marketplace": True}


def test_rung_one_says_when_the_door_is_shut(w):
    """Nested in a project that is not the shared workspace repo: the
    checklist can say so before the button is pressed."""
    proj = w.ws / "proj"
    proj.mkdir()
    git(proj, "init", "-q")
    app = _fresh_app(w.ws, tag="proj", name="thing")
    out = w.read(app)
    assert out["rung"] == 1
    from fused_render.contrib import ladder
    assert out["github"]["blocked"] == ladder.NESTED


def test_an_app_in_the_shared_local_repo_is_not_blocked(w):
    local = w.ws / "local"
    app = _fresh_app(w.ws, tag="local", name="mine")
    git(local, "init", "-q")
    git(local, "add", "-A")
    git(local, "-c", "user.name=F", "-c", "user.email=f@x", "commit", "-q", "-m", "init")
    out = w.read(app)
    assert out["rung"] == 1 and out["github"]["blocked"] is None


def test_an_own_repo_stands_on_rung_two(w):
    w.listing.refresh(w.app)  # a read of the index (not listed)
    out = w.read(w.app)
    assert out["rung"] == 2
    assert out["mine"] is True and out["kind"] == "own"
    gh_ = out["github"]
    assert gh_["repo"] == REPO
    assert gh_["visibility"] == "public"  # looked up on the (inline) lane, cached
    assert gh_["unpushed"] == 0
    assert gh_["link"] and REPO in gh_["link"]
    assert out["listing"]["state"] == "not-listed"
    assert isinstance(out["doctor"]["clear"], bool)
    assert isinstance(out["doctor"]["failing"], int)

    with open(os.path.join(w.app, "app.py"), "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")
    git(w.app, "add", "-A")
    git(w.app, "commit", "-q", "-m", "local edit")
    out = w.read(w.app)
    assert out["rung"] == 2
    assert out["github"]["unpushed"] == 1


def test_a_private_repo_reads_private(w):
    w.fake.create_repo_as("alice", "secret", {"index.html": PAGE}, private=True)
    from fused_render.contrib import ladder
    assert ladder._fetch_visibility("alice/secret") == "private"
    assert ladder.visibility("alice/secret") == "private"


def test_a_listed_app_stands_on_rung_three(w):
    head = git(w.app, "rev-parse", "HEAD")
    row = {"slug": "sine-starter", "repo": REPO, "branch": "main", "commit": head,
           "name": "Sine Starter", "description": "", "category": "starters", "tags": [],
           "icon": None, "preview": None, "remix_of": None, "version": "1.0.0"}
    w.fake.commit(INDEX, "main", {"apps/sine-starter.json": json.dumps(row) + "\n"},
                  "List sine-starter", author="fusedio")
    w.listing.refresh(w.app)
    out = w.read(w.app)
    assert out["listing"]["listing"]["listed"] is True
    assert out["listing"]["state"] == "live"
    assert out["rung"] == 3
    assert out["listing"]["ok"] is True  # the §18.7 ListingStrip answer
    assert out["listing"]["version"] == "1.0.0"
    assert out["listing"]["listing"]["approved"]["version"] == "1.0.0"


def test_a_github_copy_is_not_mine(w):
    rec = w.store.get(w.app)
    rec["kind"] = "github"
    w.store.put(w.app, rec)
    out = w.read(w.app)
    assert out["mine"] is False and out["kind"] == "github"
    assert out["rung"] is None
    assert out["listing"] is None
    assert out["github"]["repo"] == REPO
    assert out["github"]["link"] is None


def test_a_marketplace_copy_is_not_mine(w):
    rec = w.store.get(w.app)
    rec["kind"] = "marketplace"
    w.store.put(w.app, rec)
    out = w.read(w.app)
    assert out["mine"] is False and out["kind"] == "marketplace" and out["rung"] is None


def test_an_unrecorded_clone_of_someone_elses_repo_is_not_mine(w):
    """No record and the switches off: the ownership rule from what is known
    without asking GitHub — Bob's repo is not Alice's."""
    w.fake.create_repo_as("bob", "tool", {"index.html": PAGE})
    git(w.ws / "app", "clone", "-q", "https://github.com/bob/tool", str(w.ws / "app" / "tool"))
    out = w.read(str(w.ws / "app" / "tool"))
    assert out["mine"] is False and out["rung"] is None


def test_not_an_app_is_refused(w, tmp_path):
    out = w.read(str(tmp_path))
    assert out["ok"] is False and out["reason"] == "not-an-app"


def test_a_legacy_github_link_switch_is_dropped_once(w):
    """Contract 3: the old Share-sheet switch has no Preferences row, so a
    stored value is deleted once — never left as a switch the UI cannot
    turn off. (It used to fold into `contrib_enabled`; the loop is always
    on since 2026-10-29, and a stored `contrib_enabled` is ignored.)"""
    from fused_render.shell import prefs, storage

    path = prefs._path()
    storage.write_json(path, {"marketplace_enabled": False, "github_link_enabled": True})
    assert prefs.read_prefs() == {"marketplace_enabled": False}
    assert "github_link_enabled" not in storage.read_json(path)  # written back once
    assert w.read(w.app)["flags"] == {"sources": True, "marketplace": True}
    storage.write_json(path, {"contrib_enabled": False, "github_link_enabled": False})
    assert prefs.read_prefs() == {"contrib_enabled": False}
    assert w.read(w.app)["flags"]["sources"] is True


def test_fresh_re_asks_what_is_cached(w):
    from fused_render.contrib import ladder

    w.listing.refresh(w.app)
    assert w.read(w.app)["github"]["visibility"] == "public"
    ladder.note_visibility(REPO, "private")  # a stale cached answer
    assert w.read(w.app)["github"]["visibility"] == "private"
    assert w.read(w.app, fresh=1)["github"]["visibility"] == "public"
