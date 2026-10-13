"""SPEC T-2 on reload: `_history` nests each subagent's PERSISTED transcript
under its Agent tool segment.

Claude Code 2.1.296 keeps the main session transcript free of subagent rows
and writes each subagent beside it:
`<projects>/<munged cwd>/<session>/subagents/agent-<id>.jsonl` plus
`agent-<id>.meta.json` whose `toolUseId` is the parent call's id.
"""
import importlib.util
import json
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


@pytest.fixture
def agent():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_hist_sub_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _asst(content, **extra):
    row = {"type": "assistant", "message": {"role": "assistant", "content": content}}
    row.update(extra)
    return row


def _res(tool_id, content, **extra):
    row = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": content}]}}
    row.update(extra)
    return row


def _side(row, agent_id="a1", ts=None):
    row = dict(row, isSidechain=True, agentId=agent_id)
    if ts:
        row["timestamp"] = ts
    return row


MAIN = [_user("look at greet"),
        _asst([{"type": "tool_use", "id": "toolu_P", "name": "Agent",
                "input": {"description": "Read greet", "subagent_type": "general-purpose"}}]),
        _res("toolu_P", "it says hello"),
        _asst([{"type": "text", "text": "It says hello."}])]


def _setup(agent, tmp_path, monkeypatch, main=MAIN):
    target = tmp_path / "proj"
    target.mkdir()
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    agent.RUNS = str(tmp_path / "runs")
    d = projects / agent._munge(str(target))
    d.mkdir(parents=True)
    (d / "sess1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in main))
    sub = d / "sess1" / "subagents"
    sub.mkdir(parents=True)
    return target, sub


def _write_agent(sub, agent_id, tool_use_id, rows, agent_type="general-purpose"):
    (sub / f"agent-{agent_id}.meta.json").write_text(json.dumps(
        {"agentType": agent_type, "description": "d", "toolUseId": tool_use_id,
         "spawnDepth": 1, "requestShape": "background"}))
    (sub / f"agent-{agent_id}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def _agent_seg(agent, target):
    turns = agent._history(str(target), "sess1")["turns"]
    return next(s for s in turns[-1]["segments"] if s.get("id") == "toolu_P")


def test_a_persisted_subagent_nests_under_its_agent_chip(agent, tmp_path, monkeypatch):
    target, sub = _setup(agent, tmp_path, monkeypatch)
    _write_agent(sub, "a1", "toolu_P", [
        _side(_user("Read src/app.py and report")),
        {"isSidechain": True, "agentId": "a1",
         "attachment": {"type": "deferred_tools_delta"}, "type": "attachment"},
        _side(_asst([{"type": "tool_use", "id": "toolu_R", "name": "Read",
                      "input": {"file_path": "app.py"}}])),
        _side(_res("toolu_R", "def greet(): ...")),
        _side(_asst([{"type": "text", "text": "greet returns hello"}])),
    ])
    seg = _agent_seg(agent, target)
    assert seg["subagent_type"] == "general-purpose"
    kids = seg["children"]
    assert [k["kind"] for k in kids] == ["tool", "text"]
    assert kids[0]["name"] == "Read" and kids[0]["output"] == "def greet(): ..."
    assert kids[1]["text"] == "greet returns hello"


def test_no_subagents_folder_means_no_children(agent, tmp_path, monkeypatch):
    target, sub = _setup(agent, tmp_path, monkeypatch)
    sub.rmdir()
    seg = _agent_seg(agent, target)
    assert "children" not in seg and "subagent_type" not in seg


def test_a_nested_subagent_is_flattened_into_its_parent(agent, tmp_path, monkeypatch):
    target, sub = _setup(agent, tmp_path, monkeypatch)
    _write_agent(sub, "a1", "toolu_P", [
        _side(_asst([{"type": "tool_use", "id": "toolu_N", "name": "Agent",
                      "input": {"description": "inner"}}]), ts="2026-10-13T10:00:01Z"),
        _side(_res("toolu_N", "inner done"), ts="2026-10-13T10:00:05Z"),
    ])
    _write_agent(sub, "a2", "toolu_N", [
        _side(_asst([{"type": "text", "text": "inner speaks"}]), "a2",
              ts="2026-10-13T10:00:03Z"),
    ], agent_type="Explore")
    seg = _agent_seg(agent, target)
    kids = seg["children"]
    assert [k["kind"] for k in kids] == ["tool", "text"]
    assert kids[0]["id"] == "toolu_N" and kids[0]["status"] == "ok"
    assert "children" not in kids[0]
    assert kids[1]["text"] == "inner speaks"


def test_unreadable_or_oversized_files_are_skipped(agent, tmp_path, monkeypatch):
    target, sub = _setup(agent, tmp_path, monkeypatch)
    (sub / "agent-bad.meta.json").write_text("{not json")
    _write_agent(sub, "big", "toolu_P", [
        _side(_asst([{"type": "text", "text": "x" * 200}]))])
    monkeypatch.setattr(agent, "_SUBAGENT_BYTES_CAP", 50)
    seg = _agent_seg(agent, target)
    assert "children" not in seg
    (sub / "agent-orphan.meta.json").write_text(json.dumps({"toolUseId": "toolu_P"}))
    assert "children" not in _agent_seg(agent, target)   # no jsonl beside it


def test_the_file_cap_bounds_the_read(agent, tmp_path, monkeypatch):
    target, sub = _setup(agent, tmp_path, monkeypatch)
    for i in range(3):
        _write_agent(sub, f"x{i}", f"toolu_other{i}", [
            _side(_asst([{"type": "text", "text": "no"}]))])
    _write_agent(sub, "z", "toolu_P", [
        _side(_asst([{"type": "text", "text": "late"}]))])
    # The cap bounds the transcripts PARSED, not which files sort first: the
    # one referenced by the chip on screen is parsed on demand, so three
    # unrelated subagents ahead of it alphabetically change nothing (Bugbot,
    # PR #1635).
    monkeypatch.setattr(agent, "_SUBAGENT_FILE_CAP", 1)
    assert _agent_seg(agent, target)["children"][0]["text"] == "late"
    monkeypatch.setattr(agent, "_SUBAGENT_FILE_CAP", 0)
    assert "children" not in _agent_seg(agent, target)


def test_a_fifo_meta_is_skipped_not_opened(agent, tmp_path, monkeypatch):
    """A FIFO (or a symlink to one) named like a meta file would block the
    pool thread on open; only regular files are read (Bugbot, PR #1635)."""
    import os as _os
    if not hasattr(_os, "mkfifo"):
        import pytest
        pytest.skip("no FIFOs on this platform")
    target, sub = _setup(agent, tmp_path, monkeypatch)
    _write_agent(sub, "z", "toolu_P", [
        _side(_asst([{"type": "text", "text": "late"}]))])
    _os.mkfifo(str(sub / "agent-fifo.meta.json"))
    assert _agent_seg(agent, target)["children"][0]["text"] == "late"


def test_grandchild_subagents_fold_into_the_chip(agent, tmp_path, monkeypatch):
    """A subagent's own subagent lives in its own file; the chip shows one
    flat group, transitively (Bugbot, PR #1635)."""
    target, sub = _setup(agent, tmp_path, monkeypatch)
    _write_agent(sub, "child", "toolu_P", [
        _side(_asst([{"type": "tool_use", "id": "toolu_C", "name": "Agent",
                      "input": {"prompt": "go deeper"}}])),
    ])
    _write_agent(sub, "grandchild", "toolu_C", [
        _side(_asst([{"type": "text", "text": "from the grandchild"}]))])
    seg = _agent_seg(agent, target)
    texts = [c.get("text") for c in seg["children"] if c.get("kind") == "text"]
    assert "from the grandchild" in texts


def test_a_second_meta_for_one_call_falls_through_to_the_readable_one(
        agent, tmp_path, monkeypatch):
    """Two metas naming the same call: the unreadable one (its transcript
    missing) must not hide the readable one (Bugbot, PR #1635)."""
    target, sub = _setup(agent, tmp_path, monkeypatch)
    (sub / "agent-aaa.meta.json").write_text(
        '{"agentType": "x", "toolUseId": "toolu_P"}')  # no .jsonl beside it
    _write_agent(sub, "bbb", "toolu_P", [
        _side(_asst([{"type": "text", "text": "readable"}]))])
    assert _agent_seg(agent, target)["children"][0]["text"] == "readable"
