"""Review follow-ups for the worktree-visibility branch: the Agent `streaming`
flag, the bounded brief scanner, hint clearing (exit marker), command-word
matching for `git`, and the hint on a history reload."""
import importlib.util
import json
import os

import pytest

AGENT = os.path.join("fused_render", "claude_agent", "agent.py")


@pytest.fixture
def agent(tmp_path):
    spec = importlib.util.spec_from_file_location("claude_agent_wtf", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PROJECTS = str(tmp_path / "projects")
    os.makedirs(mod.PROJECTS)
    return mod


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _poll(agent, run_dir, rows):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: True
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


def test_agent_streaming_flag_lives_until_the_finalized_input(agent, run_dir):
    rows = [_tool_start("Agent"),
            _input_json('{"description": "D", "subagent_type": "Explore", "prompt": "'
                        + "x" * 50)]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool["streaming"] is True and tool["detail"] == "D"
    rows.append(_assistant_tool("Agent", {"description": "D", "subagent_type": "Explore",
                                          "prompt": "p"}))
    assert _poll(agent, run_dir, rows)["activity"]["tool"]["streaming"] is False


def test_long_description_over_many_small_fragments_is_found(agent, run_dir):
    desc = "d" * 2048
    text = '{"prompt": "' + "p" * 3000 + '", "description": "' + desc + '"}'
    rows = [_tool_start("Agent")] + [_input_json(text[i:i + 7])
                                     for i in range(0, len(text), 7)]
    detail = _poll(agent, run_dir, rows)["activity"]["tool"]["detail"]
    assert detail == desc[:77] + "…"


def test_scan_buffer_stays_bounded_without_a_pending_opener(agent):
    found = {}
    buf = ""
    for _ in range(200):
        frag = "z" * 40
        buf = agent._scan_agent_fields(buf, frag, found)
        assert len(buf) <= agent._AGENT_SCAN_OVERLAP + len(frag)
    for c in '"description": "split ':
        buf = agent._scan_agent_fields(buf, c, found)
    assert not found and buf.startswith('"description"')
    buf = agent._scan_agent_fields(buf, 'value"', found)
    assert found == {"description": "split value"}


def test_scan_finds_key_split_across_chunks(agent):
    found = {}
    buf = agent._scan_agent_fields("", "q" * 500 + '{"subagent_ty', found)
    buf = agent._scan_agent_fields(buf, 'pe": "Plan"', found)
    assert found == {"subagent_type": "Plan"}


@pytest.mark.parametrize("name,inp", [
    ("ExitWorktree", {"action": "keep"}),
    ("Bash", {"command": "git worktree remove ../wt"}),
])
def test_worktree_hint_exit_marker(agent, name, inp):
    assert agent._worktree_hint(name, inp) == {"via": "exit", "path": "", "branch": ""}


@pytest.mark.parametrize("command", ["echo git worktree add foo",
                                     "grep -r git worktree add docs/"])
def test_worktree_hint_git_must_be_the_command_word(agent, command):
    assert agent._worktree_hint("Bash", {"command": command}) is None


@pytest.mark.parametrize("command", [
    "GIT_DIR=x git worktree add ../wt -b f", "sudo git worktree add ../wt -b f",
    "env FOO=1 git worktree add ../wt -b f", "command git worktree add ../wt -b f"])
def test_worktree_hint_prefixed_git_still_matches(agent, command):
    assert agent._worktree_hint("Bash", {"command": command}) == {
        "path": "../wt", "branch": "f", "via": "bash"}


def test_poll_hint_cleared_by_exit(agent, run_dir):
    rows = [_assistant_tool("EnterWorktree", {"name": "w"}, "t1"),
            _assistant_tool("ExitWorktree", {}, "t2")]
    assert _poll(agent, run_dir, rows)["worktree_hint"]["via"] == "exit"


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_history_returns_the_final_worktree_hint(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    work = tmp_path / "repo"
    work.mkdir()
    target = work / "x.py"
    target.write_text("")
    path = os.path.join(agent.PROJECTS, agent._munge(str(work)), "sid.jsonl")

    def tool(name, inp, i):
        return {"type": "assistant", "uuid": f"t{i}", "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp}]}}
    base = [{"type": "user", "uuid": "u0", "message": {"role": "user", "content": "go"}}]
    _write(path, base + [tool("EnterWorktree", {"name": "w1"}, 1)])
    hint = agent._history(str(target), "sid")["worktree_hint"]
    assert hint["via"] == "enter" and hint["path"] == "w1"
    _write(path, base + [tool("EnterWorktree", {"name": "w1"}, 1),
                         tool("ExitWorktree", {}, 2)])
    assert agent._history(str(target), "sid")["worktree_hint"] is None
    _write(path, base)
    assert agent._history(str(target), "sid")["worktree_hint"] is None


def test_streaming_agent_captures_model_and_effort(agent, run_dir):
    rows = [_tool_start("Agent"),
            _input_json(
                '{"description": "D", "subagent_type": "Explore", "prompt": "'
                + "x" * 600 + '", "model": "sonnet", "eff'),
            _input_json('ort": "medium"')]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool["model"] == "sonnet" and tool["effort"] == "medium"


def test_finalized_agent_row_carries_model_and_effort(agent, run_dir):
    rows = [_tool_start("Agent"), _input_json('{"description": "D"'),
            _assistant_tool("Agent", {"description": "D", "subagent_type": "Plan",
                                      "model": "haiku", "effort": "low"})]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert tool["model"] == "haiku" and tool["effort"] == "low"
    assert tool["streaming"] is False


def test_scan_stops_once_streaming_ends(agent, run_dir):
    rows = [_tool_start("Agent"),
            _assistant_tool("Agent", {"description": "D"}),
            _input_json('{"model": "opus"}')]
    tool = _poll(agent, run_dir, rows)["activity"]["tool"]
    assert "model" not in tool


def test_worktree_prune_is_not_an_exit(agent):
    assert agent._worktree_hint("Bash", {"command": "cd x && git worktree prune"}) is None


def test_worktree_paths_resolve_against_cwd_and_dash_c(agent):
    hint = agent._worktree_hint
    assert hint("Bash", {"command": "git -C /other/repo worktree add ../wt"},
                "/x/proj")["path"] == "/other/wt"
    assert hint("Bash", {"command": "git worktree add ../p"}, "/x/proj")["path"] == "/x/p"
    assert hint("Bash", {"command": "git -C sub worktree add ../p"},
                "/x/proj")["path"] == "/x/proj/p"
    assert hint("Bash", {"command": "git worktree add /abs/../p"}, "/x/proj")["path"] == "/p"
    # No cwd: a relative path stays as typed.
    assert hint("Bash", {"command": "git worktree add ../p"})["path"] == "../p"


def test_worktree_quoted_separator_does_not_split(agent):
    got = agent._worktree_hint(
        "Bash", {"command": 'echo "a;b" && git worktree add ../p'}, "/x/proj")
    assert got == {"path": "/x/p", "branch": "", "via": "bash"}
    got = agent._worktree_hint(
        "Bash", {"command": "ls\ngit worktree add ../p -b f"}, "/x/proj")
    assert got["path"] == "/x/p" and got["branch"] == "f"


def test_worktree_remove_marker_carries_the_resolved_path(agent):
    got = agent._worktree_hint("Bash", {"command": "git worktree remove ../wt"}, "/x/proj")
    assert got == {"via": "exit", "path": "", "branch": "", "removed": "/x/wt"}


def _hint_after(agent, run_dir, commands):
    (run_dir / "meta.json").write_text(json.dumps({"file": "/x/proj/a.py"}))
    rows = [_assistant_tool("Bash", {"command": c}, f"t{i}")
            for i, c in enumerate(commands)]
    return _poll(agent, run_dir, rows)["worktree_hint"]


def test_poll_prune_keeps_the_hint(agent, run_dir):
    hint = _hint_after(agent, run_dir, ["git worktree add ../p", "git worktree prune"])
    assert hint["via"] == "bash" and hint["path"] == "/x/p"


def test_poll_removing_another_path_keeps_the_hint(agent, run_dir):
    hint = _hint_after(agent, run_dir, ["git worktree add ../p", "git worktree remove ../q"])
    assert hint["via"] == "bash" and hint["path"] == "/x/p"


def test_poll_removing_the_hinted_path_clears_it(agent, run_dir):
    hint = _hint_after(agent, run_dir, ["git worktree add ../p", "git worktree remove ../p"])
    assert hint["via"] == "exit"
