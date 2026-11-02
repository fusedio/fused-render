"""Which commits leave a tracked `.fused/` out, and where a Claude turn's
commit reads the copy's kind (review findings, upgrade + security).

- `.fused/` is left out of fused's commits only in a copy the contribution
  loop records (the copies that also get its skip-worktree protection). An
  unrecorded repo that tracks `.fused/` commits it as before; leaving it
  out there left the edit uncommitted for good, which then blocked Update.
- The Claude-turn sweep (agent.py `_commit_turn`) takes the copy's kind from
  the record, outside the folder — never from `fused.kind` in the repo's own
  `.git/config`, which the app can delete to get its hooks run again.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import subprocess

import pytest

from fused_render import app_git, git_upstream
from fused_render.contrib import classify, store


def git(cwd, *args):
    res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


@pytest.fixture
def app(tmp_path, monkeypatch):
    home, ws = tmp_path / "home", tmp_path / "Fused"
    home.mkdir()
    ws.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(ws))
    monkeypatch.setenv("FUSED_RENDER_WORKSPACE_DIR", str(ws))
    monkeypatch.setattr(git_upstream, "schedule_sync", lambda *a, **k: None)
    d = ws / "app" / "x"
    d.mkdir(parents=True)
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.name", "Author")
    git(d, "config", "user.email", "author@example.com")
    git(d, "remote", "add", "origin", "https://github.com/alice/x.git")
    write(os.path.join(d, "index.html"), '<meta name="fused-app"><p>hi</p>\n')
    write(os.path.join(d, ".fused", "meta.json"), '{"v": 1}\n')
    git(d, "add", "-A", "-f")
    git(d, "commit", "-q", "-m", "author commits .fused")
    return str(d)


def _changed_in_head(d):
    return sorted(git(d, "show", "--name-only", "--format=", "HEAD").split())


def test_an_unrecorded_repo_commits_its_tracked_fused_dir(app):
    assert classify.kind_of(app) is None
    write(os.path.join(app, ".fused", "meta.json"), '{"v": 2}\n')
    write(os.path.join(app, "index.html"), '<meta name="fused-app"><p>edit</p>\n')
    assert app_git.commit(os.path.join(app, "index.html"), "Edit")
    assert _changed_in_head(app) == [".fused/meta.json", "index.html"]
    assert git(app, "status", "--porcelain") == ""


def test_a_recorded_copy_leaves_fused_out(app):
    store.put(app, store.new_record("own", repo="alice/x", branch="main"))
    assert classify.kind_of(app) == "own"
    write(os.path.join(app, ".fused", "meta.json"), '{"machine": "bob"}\n')
    write(os.path.join(app, "index.html"), '<meta name="fused-app"><p>edit</p>\n')
    assert app_git.commit(os.path.join(app, "index.html"), "Edit")
    assert _changed_in_head(app) == ["index.html"]


def _agent(monkeypatch):
    from fused_render.claude_agent import AGENT_PATH

    spec = importlib.util.spec_from_file_location("test_fused_scope_agent", AGENT_PATH)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "_request_sync", lambda app_dir: None)
    return agent


def _hook(app, marker):
    hook = os.path.join(app, ".git", "hooks", "pre-commit")
    write(hook, f"#!/bin/sh\ntouch '{marker}'\n")
    os.chmod(hook, os.stat(hook).st_mode | stat.S_IXUSR)


def test_a_claude_turn_reads_the_kind_from_the_record(app, tmp_path, monkeypatch):
    """A GitHub-link copy whose `.git/config` lost `fused.kind` (the app
    deleted it): the record still says github, so no hook runs and
    `.fused/` stays out."""
    store.put(app, store.new_record("github", repo="alice/x", branch="main",
                                    classified_by="install"))
    assert classify.kind_of(app) == "github"
    assert subprocess.run(["git", "-C", app, "config", "--local", "--get", "fused.kind"],
                          capture_output=True).returncode != 0
    marker = tmp_path / "hook-ran"
    _hook(app, marker)
    agent = _agent(monkeypatch)
    write(os.path.join(app, ".fused", "meta.json"), '{"machine": "bob"}\n')
    write(os.path.join(app, "index.html"), '<meta name="fused-app"><p>claude</p>\n')
    agent._commit_turn(os.path.join(app, "index.html"), "make it pretty")
    assert _changed_in_head(app) == ["index.html"]
    assert not marker.exists()
    assert "Co-authored-by: Claude" in git(app, "log", "-1", "--format=%B")


def test_a_claude_turn_in_an_unrecorded_repo_commits_fused_and_keeps_hooks(
        app, tmp_path, monkeypatch):
    """Even with a `fused.kind` marker planted in its config, an unrecorded
    repo is the author's own: hooks run, `.fused/` is committed."""
    git(app, "config", "fused.kind", "github")
    marker = tmp_path / "hook-ran"
    _hook(app, marker)
    agent = _agent(monkeypatch)
    write(os.path.join(app, ".fused", "meta.json"), '{"v": 3}\n')
    write(os.path.join(app, "index.html"), '<meta name="fused-app"><p>claude</p>\n')
    agent._commit_turn(os.path.join(app, "index.html"), "edit")
    assert _changed_in_head(app) == [".fused/meta.json", "index.html"]
    assert marker.exists()
