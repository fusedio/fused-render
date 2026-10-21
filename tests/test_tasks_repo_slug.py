"""The `repo` a task row carries: the GitHub slug of its folder's remote.

Read straight off `.git/config` (no git subprocess), through a worktree's
`.git` file and `commondir`, "" outside a repo or off GitHub.
"""

import os

from fused_render.server.routers import tasks as tasks_mod


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def test_slug_from_main_checkout_in_every_url_spelling(tmp_path):
    for url in (
        "git@github.com:fusedio/fused-render.git",
        "https://github.com/fusedio/fused-render",
        "https://github.com/fusedio/fused-render.git",
        "ssh://git@github.com/fusedio/fused-render.git",
    ):
        repo = tmp_path / url.replace("/", "_").replace(":", "_")
        _write(str(repo / ".git" / "config"), f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n')
        assert tasks_mod._repo_slug_uncached(str(repo)) == "fusedio/fused-render", url
        # ...and from a folder inside the checkout.
        _write(str(repo / "frontend" / "src" / "x.ts"), "")
        assert tasks_mod._repo_slug_uncached(str(repo / "frontend" / "src")) == "fusedio/fused-render"


def test_slug_through_a_worktree(tmp_path):
    main = tmp_path / "main"
    _write(str(main / ".git" / "config"), '[remote "origin"]\n\turl = git@github.com:fusedio/fused-render.git\n')
    wt_git = main / ".git" / "worktrees" / "feature"
    _write(str(wt_git / "commondir"), "../..\n")
    wt = tmp_path / "wt" / "feature"
    _write(str(wt / ".git"), f"gitdir: {wt_git}\n")
    assert tasks_mod._repo_slug_uncached(str(wt)) == "fusedio/fused-render"


def test_no_slug_outside_a_repo_or_off_github(tmp_path):
    assert tasks_mod._repo_slug_uncached(str(tmp_path / "plain")) == ""
    other = tmp_path / "gitlab"
    _write(str(other / ".git" / "config"), '[remote "origin"]\n\turl = git@gitlab.com:a/b.git\n')
    assert tasks_mod._repo_slug_uncached(str(other)) == ""
    # No origin: the first remote stands in.
    fork = tmp_path / "fork"
    _write(str(fork / ".git" / "config"), '[remote "upstream"]\n\turl = https://github.com/a/b\n')
    assert tasks_mod._repo_slug_uncached(str(fork)) == "a/b"
    assert tasks_mod._repo_slug("") == ""


def test_slug_is_cached_per_folder(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    _write(str(repo / ".git" / "config"), '[remote "origin"]\n\turl = https://github.com/a/b\n')
    tasks_mod._REPO_CACHE.clear()
    assert tasks_mod._repo_slug(str(repo)) == "a/b"
    calls = []
    monkeypatch.setattr(tasks_mod, "_repo_slug_uncached", lambda f: calls.append(f) or "x/y")
    assert tasks_mod._repo_slug(str(repo)) == "a/b"
    assert calls == []
