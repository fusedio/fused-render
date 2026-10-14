"""History finds a session the CLI relocated into a worktree.

The CLI's EnterWorktree tool MOVES the session transcript from
~/.claude/projects/<munge(workdir)>/<sid>.jsonl to
~/.claude/projects/<munge(worktree)>/<sid>.jsonl (the old file is gone). The
history read looked only at the original folder, found nothing, and a reload
showed the first message plus one merged blob.
"""
import importlib.util
import json
import os
import time

import pytest

AGENT = os.path.join("fused_render", "claude_agent", "agent.py")


@pytest.fixture
def agent(tmp_path):
    spec = importlib.util.spec_from_file_location("claude_agent_reloc", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PROJECTS = str(tmp_path / "projects")
    mod.RUNS = str(tmp_path / "runs")
    os.makedirs(mod.PROJECTS)
    os.makedirs(mod.RUNS)
    return mod


def _rows(*users):
    out = []
    for i, u in enumerate(users):
        out.append({"type": "user", "uuid": f"u{i}", "message": {"role": "user", "content": u}})
        out.append({"type": "assistant", "uuid": f"a{i}", "message": {
            "role": "assistant", "content": [{"type": "text", "text": f"re {u}"}]}})
    return out


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _setup(agent, tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    target = work / "x.py"
    target.write_text("")
    munged = agent._munge(str(work))
    return str(target), str(work), munged


def _users(hist):
    return [t["text"] for t in hist["turns"] if t["role"] == "user"]


def test_helper_prefers_original_then_newest_relocated(agent, tmp_path):
    _t, work, munged = _setup(agent, tmp_path)
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    assert agent._session_transcript_path(work, "sid") == orig  # missing: unchanged
    old = os.path.join(agent.PROJECTS, munged + "--claude-worktrees-a", "sid.jsonl")
    new = os.path.join(agent.PROJECTS, munged + "--claude-worktrees-b", "sid.jsonl")
    _write(old, _rows("one"))
    _write(new, _rows("one"))
    os.utime(old, (time.time() - 100, time.time() - 100))
    assert agent._session_transcript_path(work, "sid") == new
    _write(orig, _rows("one"))
    assert agent._session_transcript_path(work, "sid") == orig  # original wins


def test_other_folders_are_not_mistaken_for_worktrees(agent, tmp_path):
    _t, work, munged = _setup(agent, tmp_path)
    _write(os.path.join(agent.PROJECTS, munged + "-sibling", "sid.jsonl"), _rows("x"))
    assert agent._session_transcript_path(work, "sid") == os.path.join(
        agent.PROJECTS, munged, "sid.jsonl")


def test_history_reads_the_relocated_transcript(agent, tmp_path):
    target, work, munged = _setup(agent, tmp_path)
    _write(os.path.join(agent.PROJECTS, munged + "--claude-worktrees-wt", "sid.jsonl"),
           _rows("first", "second", "third"))
    hist = agent._history(target, "sid")
    assert _users(hist) == ["first", "second", "third"]


def test_history_original_location_still_wins(agent, tmp_path):
    target, work, munged = _setup(agent, tmp_path)
    _write(os.path.join(agent.PROJECTS, munged, "sid.jsonl"), _rows("here"))
    _write(os.path.join(agent.PROJECTS, munged + "--claude-worktrees-wt", "sid.jsonl"),
           _rows("there", "more"))
    assert _users(agent._history(target, "sid")) == ["here"]


def test_missing_id_keeps_the_empty_shape(agent, tmp_path):
    target, work, munged = _setup(agent, tmp_path)
    hist = agent._history(target, "nope")
    assert hist["turns"] == []
    assert hist["tasks"] == []


def test_relocated_found_from_nested_folder_workdir(agent, tmp_path):
    # EnterWorktree puts the worktree under the REPO ROOT, so a chat in a
    # subfolder must look for `<munge(root)>--claude-worktrees-*`.
    root = tmp_path / "repo"
    sub = root / "sub" / "dir"
    sub.mkdir(parents=True)
    cand = os.path.join(agent.PROJECTS, agent._munge(str(root)) + "--claude-worktrees-foo", "sid.jsonl")
    _write(cand, _rows("x"))
    assert agent._session_transcript_path(str(sub), "sid") == cand


def test_relocated_found_from_file_workdir(agent, tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "x.py"
    f.write_text("")
    cand = os.path.join(agent.PROJECTS, agent._munge(str(root)) + "--claude-worktrees-foo", "sid.jsonl")
    _write(cand, _rows("x"))
    assert agent._session_transcript_path(str(f), "sid") == cand
