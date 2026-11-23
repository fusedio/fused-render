"""Shared fixtures for tests/test_experts_*.py: each does `from _experts_conftest import *`.

* `app_home`, `client` (from `_bots_conftest`): a per-test FUSED_RENDER_HOME and workspace (so
  `<workspace>/experts/` is fresh per test), and a real server with `create_app()` (lifespan off: no seed, no
  scheduler unless a test calls them).
* `experts_on` (autouse): the flag on, a git identity, and the path caches reset.
* `needs_git`: skip a test when `git` is not on PATH.
* `make_expert(name, about, folder)`: one through `store.create`.
"""
from _bots_conftest import *  # noqa: F401,F403 — app_home, client, Client
import shutil

import pytest


@pytest.fixture(autouse=True)
def experts_on(app_home, monkeypatch):
    from fused_render.experts import paths

    monkeypatch.setenv("FUSED_RENDER_EXPERTS", "1")
    # The per-test HOME has no ~/.gitconfig.
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "test")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "test@example.invalid")
    paths._reset()
    yield
    paths._reset()


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def make_expert(name="Canvas", about="Fused canvases", folder=""):
    from fused_render.experts import store

    return store.create(name, about, folder)
