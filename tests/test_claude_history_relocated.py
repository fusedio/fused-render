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


def _timed(users, start, tag="r"):
    """Conversation rows with distinct uuids and timestamps `start`+i minutes."""
    out = []
    for i, u in enumerate(users):
        for kind, role in (("u", "user"), ("a", "assistant")):
            minute = start + len(out)
            row = {"type": role, "uuid": f"{tag}{kind}{minute}",
                   "timestamp": f"2026-11-05T16:{minute:02d}:00.000Z",
                   "message": {"role": role, "content": u if role == "user" else [
                       {"type": "text", "text": f"re {u}"}]}}
            out.append(row)
    return out


def test_helper_returns_the_only_copy_and_the_original_when_none(agent, tmp_path):
    _t, work, munged = _setup(agent, tmp_path)
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    assert agent._session_transcript_path(work, "sid") == orig  # missing: unchanged
    old = os.path.join(agent.PROJECTS, munged + "--claude-worktrees-a", "sid.jsonl")
    _write(old, _rows("one"))
    assert agent._session_transcript_path(work, "sid") == old


def test_exit_bookkeeping_newer_than_the_worktree_copy_does_not_win(agent, tmp_path):
    # The CLI appends cost-state/last-prompt to the ORIGINAL path after the
    # move, so it is the newest by mtime and holds the pre-move turns. The
    # worktree copy has the later turns: history shows both, in order.
    target, work, munged = _setup(agent, tmp_path)
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    moved = os.path.join(agent.PROJECTS, munged + "--claude-worktrees-wt", "sid.jsonl")
    _write(orig, _timed(["first", "second"], 0, "o") + _bookkeeping())
    _write(moved, _timed(["third", "fourth"], 10, "m"))
    os.utime(moved, (time.time() - 100, time.time() - 100))
    assert _users(agent._history(target, "sid")) == ["first", "second", "third", "fourth"]


def test_copies_sharing_rows_merge_without_duplicates(agent, tmp_path):
    target, work, munged = _setup(agent, tmp_path)
    shared = _timed(["first", "second"], 0, "s")
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    moved = os.path.join(agent.PROJECTS, "-other-wt", "sid.jsonl")
    _write(orig, shared)
    _write(moved, shared + _timed(["third"], 10, "m"))
    assert _users(agent._history(target, "sid")) == ["first", "second", "third"]


def test_merge_follows_the_sources_when_they_grow(agent, tmp_path):
    target, work, munged = _setup(agent, tmp_path)
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    moved = os.path.join(agent.PROJECTS, "-other-wt", "sid.jsonl")
    _write(orig, _timed(["first"], 0, "o"))
    _write(moved, _timed(["second"], 10, "m"))
    assert _users(agent._history(target, "sid")) == ["first", "second"]
    _write(moved, _timed(["second", "third"], 10, "m"))
    assert _users(agent._history(target, "sid")) == ["first", "second", "third"]


def test_same_session_under_any_slug_is_found(agent, tmp_path):
    # A session id is a uuid: the file under a slug that is no worktree of the
    # workdir is still this session (the agent cd'd somewhere unrelated).
    _t, work, munged = _setup(agent, tmp_path)
    other = os.path.join(agent.PROJECTS, "-elsewhere-repo", "sid.jsonl")
    _write(other, _rows("x"))
    assert agent._session_transcript_path(work, "sid") == other


def test_stale_original_loses_to_newer_copy_with_all_turns(agent, tmp_path):
    # The first turn was written under the folder's slug; the agent then
    # moved into a worktree and the rest of the conversation landed there.
    target, work, munged = _setup(agent, tmp_path)
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    moved = os.path.join(agent.PROJECTS, "-other-wt", "sid.jsonl")
    _write(orig, _rows("first"))
    os.utime(orig, (time.time() - 100, time.time() - 100))
    _write(moved, _rows("first", "second", "third"))
    assert _users(agent._history(target, "sid")) == ["first", "second", "third"]


def test_history_reads_the_relocated_transcript(agent, tmp_path):
    target, work, munged = _setup(agent, tmp_path)
    _write(os.path.join(agent.PROJECTS, munged + "--claude-worktrees-wt", "sid.jsonl"),
           _rows("first", "second", "third"))
    hist = agent._history(target, "sid")
    assert _users(hist) == ["first", "second", "third"]


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


def _bookkeeping(sid="sid"):
    # What the CLI writes on exit, with no conversation row among them.
    return [
        {"type": "bridge-session", "sessionId": sid, "bridgeSessionId": "cse_x"},
        {"type": "cost-state", "sessionId": sid, "totalCostUSD": 1.0},
        {"type": "last-prompt", "lastPrompt": "hi", "leafUuid": "a0", "sessionId": sid},
        {"type": "ai-title", "aiTitle": "t", "sessionId": sid},
        {"type": "mode", "mode": "normal", "sessionId": sid},
        {"type": "atis-latch", "atis": "", "sessionId": sid},
    ]


def test_bookkeeping_only_original_loses_to_relocated(agent, tmp_path):
    # EnterWorktree moved the conversation; the CLI's exit then re-created the
    # original path with bookkeeping rows only, newer than nothing it replaced.
    target, work, munged = _setup(agent, tmp_path)
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    moved = os.path.join(agent.PROJECTS, munged + "--claude-worktrees-wt", "sid.jsonl")
    _write(moved, _rows("first", "second", "is it done?"))
    _write(orig, _bookkeeping())
    assert agent._session_transcript_path(work, "sid") == moved
    assert _users(agent._history(target, "sid")) == ["first", "second", "is it done?"]


def test_bookkeeping_only_original_kept_when_nothing_relocated(agent, tmp_path):
    _t, work, munged = _setup(agent, tmp_path)
    orig = os.path.join(agent.PROJECTS, munged, "sid.jsonl")
    _write(orig, _bookkeeping())
    assert agent._session_transcript_path(work, "sid") == orig


def test_slash_command_row_is_its_own_user_turn(agent, tmp_path):
    target, work, munged = _setup(agent, tmp_path)
    slash = ("<command-message>development:building-with-subagent</command-message>"
             "\n<command-name>/development:building-with-subagent</command-name>"
             "\n<command-args>build the thing</command-args>")
    rows = _rows("first")
    rows.append({"type": "user", "uuid": "s1", "message": {"role": "user", "content": slash}})
    rows.append({"type": "assistant", "uuid": "a9", "message": {
        "role": "assistant", "content": [{"type": "text", "text": "on it"}]}})
    _write(os.path.join(agent.PROJECTS, munged, "sid.jsonl"), rows)
    turns = agent._history(target, "sid")["turns"]
    assert [(t["role"], t["text"]) for t in turns] == [
        ("user", "first"), ("assistant", "re first"),
        ("user", "/development:building-with-subagent build the thing"),
        ("assistant", "on it")]


def test_bare_slash_command_has_no_trailing_space(agent):
    assert agent._slash_turn_text(
        "<command-message>x</command-message>\n<command-name>/x</command-name>") == "/x"
    assert agent._slash_turn_text("<task-notification>d</task-notification>") is None
