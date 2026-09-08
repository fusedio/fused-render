"""Tests for fused_render/server/routers/github.py at the router level.

Patterned on tests/test_server_git_repo.py: real git repos built with the one
git fixture helper in this tree (tests/_git_repo.py), driven directly rather
than through the app's full router table. Unlike that module's pure-function
style, every endpoint here is a FastAPI route, so a bare TestClient carrying
just this router (mirroring test_github_setup.py's own `_client()`) stands in
for the browser.

Two things every mutating endpoint on this router must get right:

  * a POST with no X-Fused header is refused before it does anything (the D3
    guard against a blind cross-origin form submission — see
    server/common.py::_require_fused);
  * `/api/github/publish`'s `root` — the one field on this router that names
    a path arrived at from the page — is resolved and containment-checked
    before any git or gh command touches it, so a root outside any repository
    (the "allowed scope" for a publish) is refused rather than acted on.
"""
import os

import pytest
from starlette.testclient import TestClient

from _git_repo import git, git_available

from fused_render import github_setup


def _client():
    from fastapi import FastAPI

    from fused_render.server.routers.github import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_publish():
    # Every test starts from `idle` and leaves it there — the same discipline
    # test_github_setup.py's own `_clean_publish` fixture uses, so a publish
    # left `running` by one test can never bleed into the next.
    github_setup.publish_reset()
    yield
    github_setup.publish_reset()


def _repo_with_a_commit(tmp_path):
    if not git_available():
        pytest.skip("git is not available")
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.txt").write_text("hello\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-q", "-m", "first")
    return str(root)


# ------------------------------------------------ every POST needs X-Fused

@pytest.mark.parametrize("path", [
    "/api/github/status/refresh",
    "/api/github/install",
    "/api/github/login",
    "/api/github/login/cancel",
    "/api/github/publish",
])
def test_every_post_rejects_a_missing_x_fused_header(path):
    resp = _client().post(path, json={})
    assert resp.status_code == 403
    assert "X-Fused" in resp.json()["error"]


@pytest.mark.parametrize("path", [
    "/api/github/status/refresh",
    "/api/github/install",
    "/api/github/login",
    "/api/github/login/cancel",
    "/api/github/publish",
])
def test_every_post_rejects_a_wrong_x_fused_header(path):
    resp = _client().post(path, json={}, headers={"X-Fused": "0"})
    assert resp.status_code == 403


# ------------------------------------------------ publish's root containment

def test_publish_refuses_a_root_outside_any_repository(tmp_path, monkeypatch):
    # A plain folder — never `git init`'d — is not inside any repository, so
    # `_resolve_repo_root` refuses it before `_has_commits`/`_has_remote` (let
    # alone the `gh` spawn) ever run against it.
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    resp = _client().post("/api/github/publish", headers={"X-Fused": "1"},
                          json={"root": str(plain), "name": "my-repo",
                                "visibility": "private"})
    assert resp.status_code == 409
    assert "not inside a git repository" in resp.json()["error"]


def test_publish_refuses_a_relative_root(monkeypatch):
    # The page always sends an absolute folder path; a relative one is not
    # merely "outside scope" but not even a locatable filesystem path, so it
    # is refused the same way `_resolve_repo_root` refuses any non-absolute
    # string, before a git command is ever run against it.
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    resp = _client().post("/api/github/publish", headers={"X-Fused": "1"},
                          json={"root": "relative/dir", "name": "my-repo",
                                "visibility": "private"})
    assert resp.status_code == 409
    assert resp.json()["error"]


def test_publish_refuses_a_root_that_does_not_exist(monkeypatch):
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    resp = _client().post("/api/github/publish", headers={"X-Fused": "1"},
                          json={"root": "/no/such/path/at/all", "name": "my-repo",
                                "visibility": "private"})
    assert resp.status_code == 409
    assert resp.json()["error"]


def test_publish_refuses_a_mount_backed_root(tmp_path, monkeypatch):
    from fused_render.shell import mounts as shell_mounts

    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    monkeypatch.setattr(shell_mounts, "is_mount_backed", lambda p: True)
    resp = _client().post("/api/github/publish", headers={"X-Fused": "1"},
                          json={"root": str(tmp_path), "name": "my-repo",
                                "visibility": "private"})
    assert resp.status_code == 409
    assert "remote mounts" in resp.json()["error"]


def test_publish_with_a_root_inside_the_repository_starts(tmp_path, monkeypatch):
    # A root that IS inside a repository — the allowed scope — is accepted:
    # `_resolve_repo_root` walks it up to the work-tree root via `git
    # rev-parse --show-toplevel`, exactly like the page handing over the repo
    # folder it has open.
    root = _repo_with_a_commit(tmp_path)
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/usr/bin/gh", "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)
    monkeypatch.setattr(github_setup, "_run_publish", lambda *a, **k: None)
    resp = _client().post("/api/github/publish", headers={"X-Fused": "1"},
                          json={"root": root, "name": "my-repo",
                                "visibility": "private"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "running"


def test_publish_status_is_an_unguarded_read():
    resp = _client().get("/api/github/publish")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"
