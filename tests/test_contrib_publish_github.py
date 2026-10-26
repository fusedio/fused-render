"""Put it on GitHub in one call (docs/CONTRIB_LOOP.md §19.2, §19.7) —
`contrib/ladder.py publish_github` and `POST /api/contrib/publish-github`,
against a FakeGitHub (real bare repos) and a fake `gh repo create` spawn
that creates the repo there, adds `origin` and pushes, as gh does.

What a wrong answer would cost: the whole workspace pushed under one app's
name (a nested folder handed to `gh repo create --source`), `.venv/` or
`.fused/` in the first commit, a commit under no identity, a record left
`repo: null` after the publish (the Marketplace strip never showed — the
bug §19 fixes), a Git-panel publish that skips the rebind.
"""
from __future__ import annotations

import os
import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from tests._contrib_gh_world import git, write_prefs

H = {"X-Fused": "1"}
PAGE = "<!doctype html><html><head><meta name=\"fused-app\" /><title>T</title></head></html>\n"


@pytest.fixture
def w(tmp_path, monkeypatch):
    from fused_render import github_setup
    from fused_render.contrib import gh, ladder, refresh

    home, ws = tmp_path / "home", tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    monkeypatch.setenv(gh.FAKE_ENV, str(tmp_path / "github"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    fake = gh.fake()
    for k, v in fake.git_env().items():
        monkeypatch.setenv(k, v)
    write_prefs(marketplace_enabled=True)
    gh.forget_identity()
    ladder.forget_caches()
    monkeypatch.setattr(refresh, "_INLINE", True)
    refresh._reset()
    github_setup.publish_reset()
    monkeypatch.setattr(github_setup.jobs, "upsert", lambda *a, **k: {})
    monkeypatch.setattr(github_setup, "cached_summary",
                        lambda: {"found": True, "signed_in": True, "account": "alice",
                                 "stale": False})
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    fake.add_user("alice", "Alice Liddell")
    fake.add_user("bob", "Bob")
    fake.sign_in("alice")

    spawned = []

    def fake_gh_repo_create(cmd):
        # [gh, "repo", "create", name, "--source", root, "--remote", "origin",
        #  "--push", "--public"|"--private"]
        spawned.append(cmd)
        name, root = cmd[3], cmd[5]
        full = fake.create_repo(name, description="", private="--private" in cmd)
        git(root, "remote", "add", "origin", f"https://github.com/{full}")
        branch = git(root, "symbolic-ref", "--short", "HEAD")
        git(root, "push", "-q", "-u", "origin", f"HEAD:refs/heads/{branch}")
        fake.after_push(full, branch)
        return type("R", (), {"returncode": 0, "stdout": f"https://github.com/{full}\n",
                              "stderr": ""})()

    monkeypatch.setattr(github_setup, "_spawn_gh_repo_create", fake_gh_repo_create)

    api = FastAPI()
    from fused_render.server.routers.ladder import router
    api.include_router(router)
    client = TestClient(api)
    yield type("W", (), {"fake": fake, "ws": ws, "client": client, "spawned": spawned})
    github_setup.publish_reset()


def _fresh(ws, tag="app", name="My Fresh App"):
    d = ws / tag / name
    d.mkdir(parents=True)
    (d / "index.html").write_text(PAGE)
    (d / ".fused").mkdir()
    (d / ".fused" / "state.json").write_text("{}\n")
    (d / ".venv").mkdir()
    (d / ".venv" / "pyvenv.cfg").write_text("home = /x\n")
    return str(d)


def _wait_done(timeout=20.0) -> dict:
    from fused_render import github_setup

    end = time.time() + timeout
    while time.time() < end:
        rec = github_setup.publish_status()
        if rec["state"] in ("done", "error"):
            return rec
        time.sleep(0.05)
    raise AssertionError("the publish never finished")


def _post(w, app, **body):
    return w.client.post("/api/contrib/publish-github", json={"dir": app, **body}, headers=H)


def test_a_scratch_folder_goes_to_github_in_one_call(w):
    from fused_render.contrib import follow, store

    app = _fresh(w.ws)
    r = _post(w, app)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["state"] == "running"
    rec = _wait_done()
    assert rec["state"] == "done", rec
    assert rec["detail"] == "https://github.com/alice/my-fresh-app"
    real = os.path.realpath(app)
    assert follow.is_repo_root(app) == real
    assert git(app, "log", "-1", "--format=%s") == "Initial commit"
    # identity from the gh account: its name and <login>@users.noreply
    assert git(app, "log", "-1", "--format=%an <%ae>") == \
        "Alice Liddell <alice@users.noreply.github.com>"
    tracked = git(app, "ls-files").split()
    assert "index.html" in tracked
    assert not any(p.startswith((".fused/", ".venv/")) for p in tracked)
    assert w.fake.tip("alice/my-fresh-app") == git(app, "rev-parse", "HEAD")
    assert "--public" in w.spawned[0]  # the default (§19.2)
    # done → the record is rebound: own, with the repo
    got = store.get(app)
    assert got["kind"] == "own" and got["repo"] == "alice/my-fresh-app"
    assert got["classified_by"] == "rebind"
    # the next ladder read says rung 2, visibility known
    lad = w.client.get("/api/contrib/ladder", params={"dir": app}).json()
    assert lad["rung"] == 2 and lad["github"]["repo"] == "alice/my-fresh-app"
    assert lad["github"]["visibility"] == "public"


def test_private_and_a_chosen_name(w):
    app = _fresh(w.ws, name="thing")
    r = _post(w, app, name="Other Name", visibility="private")
    assert r.status_code == 200, r.text
    assert _wait_done()["state"] == "done"
    assert "--private" in w.spawned[0] and w.spawned[0][3] == "other-name"
    from fused_render.contrib import gh
    assert gh.repo("alice/other-name")["private"] is True


def test_an_existing_global_identity_is_kept(w, tmp_path):
    (tmp_path / "gitconfig").write_text("[user]\n\tname = Ally\n\temail = ally@example.com\n")
    app = _fresh(w.ws, name="kept")
    assert _post(w, app).status_code == 200
    assert _wait_done()["state"] == "done"
    assert git(app, "log", "-1", "--format=%an <%ae>") == "Ally <ally@example.com>"
    assert git(app, "config", "--local", "--get", "user.name", check=False) == ""


def test_nested_inside_another_project_is_refused(w):
    from fused_render.contrib import ladder

    proj = w.ws / "proj"
    proj.mkdir(parents=True)
    git(proj, "init", "-q")
    app = _fresh(w.ws, tag="proj", name="inner")
    r = _post(w, app)
    assert r.status_code == 409
    assert r.json() == {"ok": False, "error": ladder.NESTED, "message": ladder.NESTED,
                        "reason": "nested"}
    assert not os.path.exists(os.path.join(app, ".git"))
    assert w.spawned == []


def test_an_app_in_the_shared_local_repo_gets_a_repository_of_its_own(w):
    from fused_render.contrib import store

    local = w.ws / "local"
    app = _fresh(w.ws, tag="local", name="mine")
    other = w.ws / "local" / "other"
    other.mkdir()
    (other / "o.txt").write_text("o\n")
    git(local, "init", "-q")
    git(local, "add", "-A", "--", "mine/index.html", "other")
    git(local, "-c", "user.name=F", "-c", "user.email=f@x", "commit", "-q", "-m", "init")
    (other / "o.txt").write_text("o2\n")
    git(local, "add", "--", "other")  # another app's staged edit must not ride along
    before = git(local, "rev-parse", "HEAD")

    r = _post(w, app)
    assert r.status_code == 200, r.text
    assert _wait_done()["state"] == "done"
    assert git(app, "rev-parse", "--show-toplevel") == os.path.realpath(app)
    assert git(local, "log", "-1", "--format=%s") == "Move mine to its own repository"
    assert git(local, "rev-parse", "HEAD~1") == before
    assert git(local, "ls-tree", "--name-only", "HEAD", "--", "mine") == ""
    assert git(local, "ls-tree", "--name-only", "HEAD", "--", "other") == "other"
    assert "M  other/o.txt" in git(local, "status", "--porcelain")
    assert store.get(app)["repo"] == "alice/mine"


def test_a_remote_already_there_is_refused(w):
    from fused_render.contrib import ladder

    app = _fresh(w.ws, name="remote")
    git(app, "init", "-q")
    git(app, "remote", "add", "origin", "https://gitlab.com/a/b")
    r = _post(w, app)
    assert r.status_code == 409 and r.json()["reason"] == "has-remote"
    assert r.json()["error"] == ladder.HAS_REMOTE
    assert w.spawned == []


def test_signed_out_and_no_gh_are_refused(w, monkeypatch):
    from fused_render import github_setup
    from fused_render.contrib import gh, ladder

    app = _fresh(w.ws, name="nope")
    w.fake.sign_out()
    gh.forget_identity()
    r = _post(w, app)
    assert r.status_code == 409 and r.json()["reason"] == "signed-out"
    assert r.json()["error"] == ladder.SIGNED_OUT
    monkeypatch.setattr(github_setup, "cached_summary",
                        lambda: {"found": False, "signed_in": False, "stale": False})
    r = _post(w, app)
    assert r.status_code == 409 and r.json()["reason"] == "no-gh"
    assert not os.path.exists(os.path.join(app, ".git"))


def test_bad_visibility_and_the_guard(w):
    app = _fresh(w.ws, name="vis")
    r = _post(w, app, visibility="internal")
    assert r.status_code == 409 and r.json()["reason"] == "invalid"
    r = w.client.post("/api/contrib/publish-github", json={"dir": app})
    assert r.status_code != 200 and w.spawned == []


def test_the_git_panels_publish_rebinds_too(w):
    """The other door: `github_setup.publish_start` straight (the Git
    panel's modal) on an own app with no repo — its done path rebinds."""
    from fused_render import github_setup
    from fused_render.contrib import classify, store

    app = _fresh(w.ws, name="panel")
    git(app, "init", "-q")
    git(app, "add", "index.html")
    git(app, "-c", "user.name=A", "-c", "user.email=a@x", "commit", "-q", "-m", "one")
    rec = classify.ensure(app)
    assert rec["kind"] == "own" and rec["repo"] is None
    github_setup.publish_start(os.path.realpath(app), "panel", "public")
    assert _wait_done()["state"] == "done"
    got = store.get(app)
    assert got["kind"] == "own" and got["repo"] == "alice/panel"
    assert classify.kind_of(app) == "own"


def test_a_failing_rebind_never_fails_the_publish(w, monkeypatch):
    from fused_render.contrib import classify

    def boom(*_a, **_k):
        raise RuntimeError("rebind broke")
    monkeypatch.setattr(classify, "rebind", boom)
    app = _fresh(w.ws, name="sturdy")
    assert _post(w, app).status_code == 200
    rec = _wait_done()
    assert rec["state"] == "done" and rec["error"] is None
