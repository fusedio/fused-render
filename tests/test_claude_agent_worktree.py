"""Agent-brief streaming detail (unit A), `_worktree_hint` and task isolation
(unit B) — backend half of the chat's "work moved into a worktree" visibility.

Row builders mirror tests/test_claude_working_line.py by shape, not import.
"""
import importlib.util
import json
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


@pytest.fixture
def agent():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_wt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _poll(agent, run_dir, rows, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")
    return agent._poll("run")


def _ev(event):
    return {"type": "stream_event", "event": event, "session_id": "s"}


def _tool_start(name, id="toolu_1"):
    return _ev({"type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "id": id, "name": name,
                                  "input": {}}})


def _input_json(fragment):
    return _ev({"type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": fragment}})


def _assistant_tool(name, inp, id="toolu_1"):
    return {"type": "assistant", "session_id": "s", "message": {
        "role": "assistant", "content": [
            {"type": "tool_use", "id": id, "name": name, "input": inp}]}}


# ---------------------------------------------------------------- unit A

def test_agent_description_and_type_surface_while_the_brief_streams(agent, run_dir):
    rows = [_tool_start("Agent"), _input_json('{"description": "Fix the fl'),
            _input_json('aky test", "subagent_type": "gener')]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool["detail"] == "Fix the flaky test"
    assert not tool.get("subagent_type")
    rows.append(_input_json('al-purpose", "prompt": "' + "x" * 500))
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool["detail"] == "Fix the flaky test"
    assert tool["subagent_type"] == "general-purpose"


def test_agent_fields_with_escapes_are_decoded(agent, run_dir):
    rows = [_tool_start("Task"), _input_json('{"description": "say \\"hi\\" now"')]
    assert _poll(agent, run_dir, rows)["activity"]["tool"]["detail"] == 'say "hi" now'


def test_a_value_split_across_many_tiny_chunks_is_found(agent, run_dir):
    text = '{"subagent_type": "Explore", "description": "Map it"}'
    rows = [_tool_start("Agent")] + [_input_json(c) for c in text]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool["detail"] == "Map it" and tool["subagent_type"] == "Explore"


def test_non_agent_tools_are_not_scanned(agent, run_dir):
    rows = [_tool_start("Write"), _input_json('{"description": "nope", "x": 1}')]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool == {"id": "toolu_1", "name": "Write", "detail": ""}


def test_finalized_agent_row_carries_subagent_type(agent, run_dir):
    rows = [_tool_start("Agent"),
            _assistant_tool("Agent", {"description": "d", "subagent_type": "Explore",
                                      "prompt": "p"})]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool["detail"] == "d" and tool["subagent_type"] == "Explore"


# ---------------------------------------------------------------- unit B

@pytest.mark.parametrize("command,expected", [
    ("cd x && git worktree add -q .claude/worktrees/foo -b feat/foo origin/main",
     (".claude/worktrees/foo", "feat/foo")),
    ("git -C /r worktree add ../p", ("/p", "")),
    ("git worktree add --detach p", ("p", "")),
    ("git worktree add -B hot /tmp/hot HEAD", ("/tmp/hot", "hot")),
    ("git worktree add --orphan nb ../nb", ("../nb", "nb")),
    ("git worktree add --lock --track ../t", ("../t", "")),
    ("echo hi; git worktree add 'my dir' -b b1", ("my dir", "b1")),
])
def test_worktree_hint_bash(agent, command, expected):
    hint = agent._worktree_hint("Bash", {"command": command})
    assert hint == {"path": expected[0], "branch": expected[1], "via": "bash"}


@pytest.mark.parametrize("command", [
    "git worktree list", "git status", "git worktree add", "git worktree add 'unterminated",
    "", "ls && git worktree move x y",
])
def test_worktree_hint_bash_none(agent, command):
    assert agent._worktree_hint("Bash", {"command": command}) is None


def test_worktree_hint_enter_and_agent(agent):
    assert agent._worktree_hint("EnterWorktree", {"name": "wt1"}) == {
        "path": "wt1", "branch": "", "via": "enter"}
    assert agent._worktree_hint("EnterWorktree", {"path": "/p", "name": "n"})["path"] == "/p"
    assert agent._worktree_hint("Agent", {"isolation": "worktree", "description": "Fix",
                                          "subagent_type": "Explore"}) == {
        "path": "", "branch": "", "via": "agent", "label": "Fix"}
    assert agent._worktree_hint("Task", {"isolation": "worktree",
                                         "subagent_type": "Explore"})["label"] == "Explore"
    assert agent._worktree_hint("Agent", {"description": "x"}) is None
    assert agent._worktree_hint("Read", {"file_path": "x"}) is None


def test_poll_exposes_the_latest_worktree_hint(agent, run_dir):
    assert _poll(agent, run_dir, [_tool_start("Bash")])["worktree_hint"] is None
    rows = [_assistant_tool("Bash", {"command": "git worktree add ../a -b a"}, "t1"),
            _assistant_tool("Agent", {"isolation": "worktree", "description": "D"}, "t2"),
            _assistant_tool("Bash", {"command": "ls"}, "t3")]
    hint = _poll(agent, run_dir, rows)["worktree_hint"]
    assert hint["via"] == "agent" and hint["label"] == "D"


def test_task_record_carries_isolation(agent, run_dir):
    assert agent._new_task_record("t")["isolation"] == ""
    reg = {}
    agent._apply_task_row(
        reg, {"subtype": "task_started", "task_id": "k", "tool_use_id": "tu"},
        {"tu": "Agent"}, {"tu": {"subagent_type": "Explore", "isolation": "worktree"}}, 1.0)
    assert reg["k"]["isolation"] == "worktree"
    reg2 = {}
    agent._apply_task_row(
        reg2, {"subtype": "task_started", "task_id": "k", "tool_use_id": "tu"},
        {"tu": "Bash"}, {"tu": {"isolation": "worktree"}}, 1.0)
    assert reg2["k"]["isolation"] == ""
    assert agent._upgrade_task_record("k", {"isolation": "worktree"})["isolation"] == "worktree"
