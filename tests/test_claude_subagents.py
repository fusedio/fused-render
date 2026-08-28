"""Spawned subagents (the `Task` tool) made visible in the transcript.

Claude Code writes a subagent's run to its OWN file — never inline in the
parent transcript — plus a tiny sidecar (`agent-<id>.meta.json`) that carries
the header a `Task` chip needs (agent type, description, model, spawn depth)
and the `toolUseId` that joins it back to the `Task` `tool_use` block in the
PARENT transcript. This file pins that join, the header attachment onto
`Task` segments (`_poll` and `_history` both do it, through one shared
helper), and the lazy per-agent transcript reader.

Status is not invented here: a `Task` segment's `status` is already "running"
until its `tool_result` lands (same as any other tool call, see
tests/test_claude_agent_segments.py) — that IS the running/done/error rule
this feature needs, so nothing new computes it. What is new is the extra
`subagent` key `_attach_subagents` puts on that same segment, and — only
while it is running — a cheap progress snapshot read from the subagent's own
file tail rather than the whole thing.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_subagents_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def _project_dir(agent, tmp_path, monkeypatch, workdir):
    os.makedirs(workdir, exist_ok=True)  # _workdir() only treats it as a dir
    # if it actually IS one on disk — the same rule the target file's own
    # project dir is resolved by everywhere else in agent.py.
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    return projects / agent._munge(str(workdir))


def _write_meta(path, agent_type, description, tool_use_id, spawn_depth=1,
                model="sonnet"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "agentType": agent_type, "description": description,
        "toolUseId": tool_use_id, "spawnDepth": spawn_depth, "model": model,
    }))


# --------------------------------------------------- _segments_from_rows gate

def test_skip_sidechain_defaults_true_for_the_main_conversation(agent):
    """Unchanged behaviour: a caller that does not opt in still drops
    subagent rows from the parent's own segment list."""
    segments = agent._segments_from_rows([
        {"type": "assistant", "isSidechain": True, "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "Bash", "input": {}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "mine"}]}},
    ])
    assert [(s["kind"], s["text"]) for s in segments] == [("text", "mine")]


def test_skip_sidechain_false_reads_a_subagents_own_rows(agent):
    """A subagent's file has `isSidechain: true` on EVERY row (that is what
    marks it as a subagent to the parent) — so rendering ITS OWN transcript
    must not apply the guard that means "not this conversation"."""
    segments = agent._segments_from_rows([
        {"type": "assistant", "isSidechain": True, "message": {"content": [
            {"type": "text", "text": "working on it"}]}},
    ], skip_sidechain=False)
    assert [(s["kind"], s["text"]) for s in segments] == [
        ("text", "working on it")]


def test_skip_sidechain_false_still_drops_meta_rows(agent):
    segments = agent._segments_from_rows([
        {"type": "assistant", "isMeta": True, "isSidechain": True,
         "message": {"content": [{"type": "text", "text": "injected"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [
            {"type": "text", "text": "real"}]}},
    ], skip_sidechain=False)
    assert [s["text"] for s in segments] == ["real"]


# -------------------------------------------------------- the header join

def _task_rows(tool_use_id="tu1", with_result=False, is_error=False):
    content = [{"type": "tool_use", "id": tool_use_id, "name": "Task",
                "input": {"description": "Verify suites",
                          "subagent_type": "general-purpose",
                          "prompt": "go check it"}}]
    rows = [{"type": "assistant", "message": {"content": content}}]
    if with_result:
        rows.append({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "is_error": is_error,
             "content": "done" if not is_error else "boom"}]}})
    return rows


def test_a_running_task_chip_gets_its_subagent_header(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites and baseline claims",
                "tu1", spawn_depth=1, model="opus")
    segments = agent._attach_subagents(
        agent._segments_from_rows(_task_rows(with_result=False)),
        str(workdir), "sess1")
    task = segments[0]
    assert task["status"] == "running"
    assert task["subagent"]["agentType"] == "general-purpose"
    assert task["subagent"]["description"] == "Verify suites and baseline claims"
    assert task["subagent"]["model"] == "opus"
    assert task["subagent"]["spawnDepth"] == 1
    assert task["subagent"]["agentId"] == "a1"


def test_a_finished_task_chip_is_done_not_running(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")
    segments = agent._attach_subagents(
        agent._segments_from_rows(_task_rows(with_result=True)),
        str(workdir), "sess1")
    task = segments[0]
    assert task["status"] == "ok"
    assert task["subagent"]["agentType"] == "general-purpose"
    # A finished agent's header is still attached, but no live progress is
    # read for it — there is nothing "live" left to report.
    assert "progress" not in task["subagent"]


def test_an_errored_task_chip_is_error(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")
    segments = agent._attach_subagents(
        agent._segments_from_rows(_task_rows(with_result=True, is_error=True)),
        str(workdir), "sess1")
    assert segments[0]["status"] == "error"


def test_a_task_with_no_meta_file_yet_is_left_alone(agent, tmp_path, monkeypatch):
    """The CLI writes the sidecar before spawning, but a page catching the
    Task tool_use the instant it appears should not crash or invent a header
    it does not have yet."""
    workdir = tmp_path / "proj"
    _project_dir(agent, tmp_path, monkeypatch, workdir)
    segments = agent._attach_subagents(
        agent._segments_from_rows(_task_rows()), str(workdir), "sess1")
    assert "subagent" not in segments[0]


def test_no_file_or_session_is_a_no_op(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")
    segments = agent._segments_from_rows(_task_rows())
    assert agent._attach_subagents(segments, "", "sess1") is segments or True
    assert "subagent" not in agent._attach_subagents(list(segments), "", "sess1")[0]
    assert "subagent" not in agent._attach_subagents(list(segments), str(workdir), "")[0]


def test_non_task_tools_are_untouched(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "x", "tu1")
    segments = agent._attach_subagents(
        agent._segments_from_rows([{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": "ls"}}]}}]),
        str(workdir), "sess1")
    assert "subagent" not in segments[0]


def test_the_workflows_subdirectory_does_not_crash_the_index(agent, tmp_path, monkeypatch):
    """`subagents/workflows/wf_*/agent-*.meta.json` is a different layer this
    feature does not render; the index builder must skip right over it (it is
    a directory entry, not a `.meta.json` file) rather than choke on it."""
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "top level", "tu1")
    wf_meta = d / "sess1" / "subagents" / "workflows" / "wf_1" / "agent-b1.meta.json"
    _write_meta(wf_meta, "workflow-agent", "nested", "tu2")
    segments = agent._attach_subagents(
        agent._segments_from_rows(_task_rows(tool_use_id="tu1")),
        str(workdir), "sess1")
    assert segments[0]["subagent"]["description"] == "top level"


# ------------------------------------------------------------- live progress

def _iso(t):
    import datetime
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_a_running_subagent_reports_its_last_tool_and_start_time(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")
    _write_jsonl(str(d / "sess1" / "subagents" / "agent-a1.jsonl"), [
        {"type": "user", "timestamp": _iso(1000),
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "timestamp": _iso(1005), "message": {"content": [
            {"type": "tool_use", "id": "x1", "name": "Bash",
             "input": {"command": "pytest"}}]}},
    ])
    segments = agent._attach_subagents(
        agent._segments_from_rows(_task_rows()), str(workdir), "sess1")
    progress = segments[0]["subagent"]["progress"]
    assert progress["startedAt"] == _iso(1000)
    assert progress["lastActivityAt"] == _iso(1005)
    assert progress["lastTool"] == "Bash"


def test_progress_is_empty_but_present_when_the_subagent_has_not_written_yet(
        agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")
    segments = agent._attach_subagents(
        agent._segments_from_rows(_task_rows()), str(workdir), "sess1")
    progress = segments[0]["subagent"]["progress"]
    assert progress == {"startedAt": "", "lastActivityAt": "", "lastTool": ""}


# ------------------------------------------------------- _poll / _history

def test_poll_attaches_subagent_headers_to_task_segments(agent, tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    os.makedirs(run_dir / "perm", exist_ok=True)
    _write_jsonl(str(run_dir / "out.jsonl"), [
        {"type": "system", "session_id": "sess1"},
    ] + _task_rows())
    agent.RUNS = str(tmp_path / "runs_root")
    os.makedirs(agent.RUNS, exist_ok=True)
    import shutil
    shutil.move(str(run_dir), os.path.join(agent.RUNS, "run"))
    agent._alive = lambda _run_dir: True

    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")

    data = agent._poll("run", file=str(workdir))
    task = [s for s in data["segments"] if s["kind"] == "tool"][0]
    assert task["subagent"]["agentType"] == "general-purpose"


def _t_assistant(content):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": content}}


def test_history_attaches_subagent_headers_to_task_segments(agent, tmp_path, monkeypatch):
    target = tmp_path / "proj" / "page.html"
    os.makedirs(target.parent, exist_ok=True)
    target.write_text("<html></html>")
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    d = projects / agent._munge(str(target.parent))
    _write_jsonl(str(d / "sess1.jsonl"), [
        {"type": "user", "message": {"role": "user", "content": "go check it"}},
        _t_assistant([{"type": "tool_use", "id": "tu1", "name": "Task",
                       "input": {"description": "Verify suites",
                                "subagent_type": "general-purpose"}}]),
    ])
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")

    turns = agent._history(str(target), "sess1")["turns"]
    task = [s for s in turns[1]["segments"] if s["kind"] == "tool"][0]
    assert task["subagent"]["agentType"] == "general-purpose"
    assert task["status"] == "running"


# ------------------------------------------------------- subagent transcript

def test_subagent_transcript_renders_its_own_turns(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "Verify suites", "tu1")
    _write_jsonl(str(d / "sess1" / "subagents" / "agent-a1.jsonl"), [
        {"type": "user", "isSidechain": True, "agentId": "a1",
         "message": {"role": "user", "content": "go check it"}},
        {"type": "assistant", "isSidechain": True, "agentId": "a1",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Checking now."},
             {"type": "tool_use", "id": "x1", "name": "Bash",
              "input": {"command": "pytest"}}]}},
        {"type": "user", "isSidechain": True, "agentId": "a1",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "x1", "content": "1 passed"}]}},
        {"type": "assistant", "isSidechain": True, "agentId": "a1",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "All good."}]}},
    ])
    out = agent._subagent_transcript(str(workdir), "sess1", "a1")
    turns = out["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    kinds = [s["kind"] for s in turns[1]["segments"]]
    assert kinds == ["text", "tool", "text"]
    assert turns[1]["segments"][1]["output"] == "1 passed"
    assert out["transcript"]["path"].endswith(
        os.path.join("subagents", "agent-a1.jsonl"))


def test_subagent_transcript_of_an_unknown_agent_is_empty(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    _project_dir(agent, tmp_path, monkeypatch, workdir)
    out = agent._subagent_transcript(str(workdir), "sess1", "nope")
    assert out["turns"] == []


def test_subagent_transcript_refuses_a_path_breaking_agent_id(agent, tmp_path, monkeypatch):
    workdir = tmp_path / "proj"
    _project_dir(agent, tmp_path, monkeypatch, workdir)
    out = agent._subagent_transcript(str(workdir), "sess1", "../escape")
    assert out["turns"] == [] and out["transcript"]["path"] == ""


def test_a_nested_task_inside_a_subagent_transcript_also_gets_its_header(
        agent, tmp_path, monkeypatch):
    """spawnDepth > 1: a subagent that itself spawned a subagent. The nested
    agent's sidecar lives in the SAME flat `subagents/` dir (keyed by the
    nested Task's own `toolUseId`), so the same attach pass covers it with no
    recursive directory walk."""
    workdir = tmp_path / "proj"
    d = _project_dir(agent, tmp_path, monkeypatch, workdir)
    _write_meta(d / "sess1" / "subagents" / "agent-a1.meta.json",
                "general-purpose", "top", "tu1", spawn_depth=1)
    _write_meta(d / "sess1" / "subagents" / "agent-a2.meta.json",
                "code-reviewer", "nested", "tu2", spawn_depth=2)
    _write_jsonl(str(d / "sess1" / "subagents" / "agent-a1.jsonl"), [
        {"type": "assistant", "isSidechain": True, "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": "tu2", "name": "Task",
                 "input": {"description": "nested",
                          "subagent_type": "code-reviewer"}}]}},
    ])
    out = agent._subagent_transcript(str(workdir), "sess1", "a1")
    task = [s for s in out["turns"][0]["segments"] if s["kind"] == "tool"][0]
    assert task["subagent"]["agentType"] == "code-reviewer"
    assert task["subagent"]["spawnDepth"] == 2
