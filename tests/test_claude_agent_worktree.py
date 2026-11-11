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


def test_enter_worktree_name_resolves_under_repo_root(agent, tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    sub = root / "src" / "pkg"
    sub.mkdir(parents=True)
    got = agent._worktree_hint("EnterWorktree", {"name": "wt1"}, str(sub))
    assert got == {"path": str(root / ".claude" / "worktrees" / "wt1"),
                   "branch": "", "via": "enter"}
    # .git as a FILE (a linked worktree) counts too
    wt = tmp_path / "linked"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: x")
    assert agent._worktree_hint("EnterWorktree", {"name": "a"}, str(wt))["path"] == str(
        wt / ".claude" / "worktrees" / "a")
    # an explicit relative path resolves against cwd
    assert agent._worktree_hint("EnterWorktree", {"path": "../w"}, str(sub))["path"] == str(
        root / "src" / "w")
    # no repo found / no cwd: the bare name, as before
    assert agent._worktree_hint("EnterWorktree", {"name": "n"}, "/nonexistent-zz/q")["path"] == "n"


def test_agent_hint_carries_the_standing_worktree_as_prev(agent):
    bash = {"path": "/r/wt", "branch": "b", "via": "bash"}
    ag = {"path": "", "branch": "", "via": "agent", "label": "x"}
    got = agent._next_worktree_hint(bash, ag)
    assert got == {**ag, "prev": bash}
    # a second agent hint carries the same prev, never nested
    got2 = agent._next_worktree_hint(got, {**ag, "label": "y"})
    assert got2["prev"] == bash and "prev" not in got2["prev"]
    # enter with an absolute path counts; relative / none does not
    enter = {"path": "/r/e", "branch": "", "via": "enter"}
    assert agent._next_worktree_hint(enter, ag)["prev"] == enter
    assert agent._next_worktree_hint({"path": "e", "branch": "", "via": "enter"}, ag) == ag
    assert agent._next_worktree_hint(None, ag) == ag
    # exit clears; remove of another path stands (remove of prev: see below)
    assert agent._next_worktree_hint(got, dict(agent._WT_EXIT)) is None
    assert agent._next_worktree_hint(got, {**agent._WT_EXIT, "removed": "/r/other"}) is got


def test_remove_of_prev_under_an_agent_hint_keeps_the_agent_hint(agent):
    bash = {"path": "/r/wt", "branch": "b", "via": "bash"}
    ag = {"path": "", "branch": "", "via": "agent", "label": "x"}
    cur = agent._next_worktree_hint(bash, ag)
    assert cur["prev"] == bash
    # removing prev's checkout drops only prev; the isolated task's label stays
    assert agent._next_worktree_hint(cur, {**agent._WT_EXIT, "removed": "/r/wt"}) == ag
    # an exit with no `removed` still clears, a remove of another path stands
    assert agent._next_worktree_hint(cur, dict(agent._WT_EXIT)) is None
    assert agent._next_worktree_hint(cur, {**agent._WT_EXIT, "removed": "/r/other"}) is cur
    # a bash hint whose own path is removed still clears
    assert agent._next_worktree_hint(bash, {**agent._WT_EXIT, "removed": "/r/wt"}) is None


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


# ------------------------------------------- failed calls never become hints

def _tool_result(tid, is_error=False):
    return {"type": "user", "session_id": "s", "message": {
        "role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": is_error,
             "content": "refused" if is_error else "ok"}]}}


_BASH_ADD = {"command": "git worktree add -q -b wt-x /r/.claude/worktrees/x origin/main"}


def _call_rows(name, inp, tid, is_error):
    return [_assistant_tool(name, inp, tid), _tool_result(tid, is_error)]


@pytest.mark.parametrize("name,inp", [
    ("Bash", _BASH_ADD),
    ("EnterWorktree", {"path": "/r/.claude/worktrees/x"}),
])
def test_errored_worktree_call_leaves_no_hint(agent, run_dir, name, inp):
    rows = _call_rows(name, inp, "t1", True)
    assert _poll(agent, run_dir, rows)["worktree_hint"] is None


@pytest.mark.parametrize("name,inp", [
    ("Bash", _BASH_ADD),
    ("EnterWorktree", {"path": "/r/.claude/worktrees/x"}),
])
def test_errored_worktree_call_leaves_no_prev_under_an_agent_hint(agent, run_dir, name, inp):
    rows = (_call_rows(name, inp, "t1", True)
            + [_assistant_tool("Agent", {"isolation": "worktree", "description": "D"}, "t2")])
    hint = _poll(agent, run_dir, rows)["worktree_hint"]
    assert hint["via"] == "agent" and "prev" not in hint


def test_errored_worktree_call_restores_the_previous_standing_hint(agent, run_dir):
    rows = (_call_rows("Bash", {"command": "git worktree add /r/a -b a"}, "t1", False)
            + _call_rows("Bash", _BASH_ADD, "t2", True)
            + [_assistant_tool("Agent", {"isolation": "worktree"}, "t3")])
    hint = _poll(agent, run_dir, rows)["worktree_hint"]
    assert hint["prev"]["path"] == "/r/a"


def test_successful_worktree_call_still_yields_the_hint(agent, run_dir):
    hint = _poll(agent, run_dir, _call_rows("Bash", _BASH_ADD, "t1", False))["worktree_hint"]
    assert hint["via"] == "bash" and hint["path"] == "/r/.claude/worktrees/x"
    # in flight (no result yet) it already stands
    hint = _poll(agent, run_dir, [_assistant_tool("Bash", _BASH_ADD, "t1")])["worktree_hint"]
    assert hint["path"] == "/r/.claude/worktrees/x"


def test_history_drops_errored_worktree_hints(agent, tmp_path):
    agent.RUNS = str(tmp_path / "runs")
    os.makedirs(agent.RUNS)
    work = tmp_path / "repo"
    work.mkdir()
    target = work / "x.py"
    target.write_text("")
    path = os.path.join(agent.PROJECTS, agent._munge(str(work)), "sid.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def rows(is_error):
        out = [{"type": "user", "uuid": "u0", "message": {"role": "user", "content": "go"}}]
        for i, (name, inp) in enumerate([("Bash", _BASH_ADD),
                                         ("Agent", {"isolation": "worktree"})]):
            out.append({"type": "assistant", "uuid": f"a{i}", "message": {
                "role": "assistant", "content": [
                    {"type": "tool_use", "id": f"tu{i}", "name": name, "input": inp}]}})
            out.append({"type": "user", "uuid": f"r{i}", "message": {
                "role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": f"tu{i}",
                     "is_error": is_error and i == 0, "content": "x"}]}})
        return out

    def write(is_error):
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows(is_error):
                fh.write(json.dumps(r) + "\n")

    write(True)
    hint = agent._history(str(target), "sid")["worktree_hint"]
    assert hint["via"] == "agent" and "prev" not in hint
    write(False)
    assert agent._history(str(target), "sid")["worktree_hint"]["prev"]["path"] == \
        "/r/.claude/worktrees/x"


def test_poll_reports_the_path_of_an_errored_worktree_call(agent, run_dir):
    out = _poll(agent, run_dir, _call_rows("Bash", _BASH_ADD, "t1", True))
    assert out["worktree_hint"] is None
    assert out["worktree_failed"] == ["/r/.claude/worktrees/x"]
    assert _poll(agent, run_dir, _call_rows("Bash", _BASH_ADD, "t1", False))["worktree_failed"] == []


def _append(run_dir, rows):
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_poll_names_the_path_when_the_call_went_out_in_an_earlier_window(agent, run_dir):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _d: True
    _append(run_dir, [_assistant_tool("Bash", _BASH_ADD, "t1")])
    first = agent._poll("run")
    assert first["worktree_hint"]["path"] == "/r/.claude/worktrees/x"
    assert first["worktree_failed"] == []
    # The cursor has moved past the call; only its errored result is new.
    _append(run_dir, [_tool_result("t1", True)])
    second = agent._poll("run")
    assert second["worktree_failed"] == ["/r/.claude/worktrees/x"]


def test_a_later_window_error_for_an_existing_folder_withdraws_nothing(agent, run_dir, tmp_path):
    made = tmp_path / "wt"
    made.mkdir()
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _d: True
    _append(run_dir, [_assistant_tool("Bash", {"command": f"git worktree add {made} -b wt"}, "t1")])
    agent._poll("run")
    _append(run_dir, [_tool_result("t1", True)])
    assert agent._poll("run")["worktree_failed"] == []


def test_errored_compound_command_whose_add_succeeded_keeps_the_hint(agent, run_dir, tmp_path):
    made = tmp_path / "wt"
    made.mkdir()
    inp = {"command": f"git worktree add {made} -b wt && cd {made} && bun install"}
    out = _poll(agent, run_dir, _call_rows("Bash", inp, "t1", True))
    assert out["worktree_hint"]["path"] == str(made)
    assert out["worktree_failed"] == []


def test_repeated_add_of_an_existing_folder_keeps_the_hint(agent, run_dir, tmp_path):
    made = tmp_path / "wt"
    made.mkdir()
    inp = {"command": f"git worktree add {made} -b wt"}
    out = _poll(agent, run_dir, _call_rows("Bash", inp, "t1", True))
    assert out["worktree_hint"]["path"] == str(made)
    assert out["worktree_failed"] == []


def test_history_keeps_an_errored_add_whose_folder_exists(agent, tmp_path):
    scan = agent._WorktreeScan()
    made = tmp_path / "wt"
    made.mkdir()
    scan.add("t1", {"path": str(made), "branch": "wt", "via": "bash"})
    assert scan.fail("t1") is None
    assert scan.state["path"] == str(made)
    made.rmdir()
    assert scan.fail("t1")["path"] == str(made)
    assert scan.state is None


def test_an_errored_result_without_an_id_retracts_nothing(agent):
    scan = agent._WorktreeScan()
    scan.add(None, {"path": "/r/a", "branch": "a", "via": "bash"})
    scan.add("t2", {"path": "/r/b", "branch": "b", "via": "bash"})
    assert scan.fail(None) is None and scan.fail("") is None
    assert scan.state["path"] == "/r/b"
    assert scan.fail("t2")["path"] == "/r/b"
    assert scan.state["path"] == "/r/a"
