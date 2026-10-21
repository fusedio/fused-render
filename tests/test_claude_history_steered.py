"""A message typed into a RUNNING tool call survives a reload.

Claude Code does not open a turn for a line that arrives while a tool is
running: it injects the words into the turn in flight, beside the next tool
result, and persists them to the session JSONL as an `attachment` of type
`queued_command` — never as a `user` row (measured 2026-10-20, CLI 2.1.302).
The live stream had echoed the same line as an ordinary user row, so the page
drew the bubble; `_history` skipped the attachment, so the first transcript
refresh took the reader's own words away and every later follow-up listed
them as a ghost (Akshil, 2026-10-19).

`_history` now reads that record as the user turn it is, in its place in the
conversation, keyed by the uuid the live echo carried (`source_uuid`).
"""
import importlib.util
import json
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")
SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STEER_UUID = "80186871-6496-46ac-adb3-57a6e9a249ab"


@pytest.fixture
def agent():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_steered", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def transcript(agent, tmp_path, monkeypatch):
    target = tmp_path / "proj"
    target.mkdir()
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    proj_dir = projects / agent._munge(str(target))
    proj_dir.mkdir(parents=True)

    def write(rows):
        (proj_dir / (SESSION + ".jsonl")).write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return agent._history(str(target), SESSION)["turns"]

    return write


def _user(text, uuid="u-1"):
    return {"type": "user", "uuid": uuid, "timestamp": "2026-10-20T08:28:14.478Z",
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _assistant(*blocks):
    return {"type": "assistant", "uuid": "a-1",
            "message": {"role": "assistant", "content": list(blocks)}}


def _tool_result(tool_id="toolu_1"):
    return {"type": "user", "uuid": "tr-1",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": "done"}]}}


def _steered(text, uuid=STEER_UUID, **attachment_extra):
    """The real shape, trimmed: `rendered` is the <system-reminder> the model
    was shown and is not the reader's words."""
    att = {"type": "queued_command",
           "prompt": [{"type": "text", "text": text}],
           "source_uuid": uuid, "delivery_id": "d-1", "commandMode": "prompt",
           "timestamp": "2026-10-20T08:28:19.129Z"}
    att.update(attachment_extra)
    return {"type": "attachment", "uuid": "att-row-1",
            "timestamp": "2026-10-20T08:28:19.129Z", "attachment": att,
            "renderedRole": "system",
            "rendered": [{"content": [{"type": "text",
                                       "text": "<system-reminder>…</system-reminder>"}]}]}


def test_a_steered_line_is_a_user_turn_in_its_place(transcript):
    turns = transcript([
        _user("run the long job"),
        _assistant({"type": "tool_use", "id": "toolu_1", "name": "Bash",
                    "input": {"command": "sleep 20"}}),
        _tool_result(),
        _steered("what colour is the sky?"),
        _assistant({"type": "text", "text": "OK\n\nBlue"}),
    ])
    roles = [t["role"] for t in turns]
    assert roles == ["user", "assistant", "user", "assistant"], roles
    steered = turns[2]
    assert steered["text"] == "what colour is the sky?"
    # The uuid the live echo carried — the key the page's bubble already has.
    assert steered["uuid"] == STEER_UUID
    assert steered["ts"] == pytest.approx(1792484899.129, abs=0.001)
    # The tool rows before it stay on the reply above; the prose lands below.
    assert turns[1]["segments"] and turns[1]["segments"][0]["kind"] == "tool"
    assert turns[3]["text"] == "OK\n\nBlue"


def test_a_steered_line_sheds_the_blocks_the_page_composed_onto_it(transcript):
    """The transcript holds what the CLI was SENT, app-state block included —
    the same strip the ordinary user branch applies."""
    turns = transcript([
        _user("hello"),
        _steered("<live-app-state>\n{\"pane\": 1}\n</live-app-state>\n\nand this"),
        _assistant({"type": "text", "text": "yes"}),
    ])
    assert [t["text"] for t in turns if t["role"] == "user"] == ["hello", "and this"]


def test_other_attachments_are_still_bookkeeping(transcript):
    """`environment`, `model`, `hook_success`… ride the same record type and
    are nobody's words."""
    turns = transcript([
        _user("hello"),
        {"type": "attachment", "uuid": "x", "attachment": {
            "type": "environment", "snapshot": {"workingDirectory": "/tmp"}}},
        {"type": "attachment", "uuid": "y", "attachment": {
            "type": "queued_command", "prompt": [], "source_uuid": "z"}},
        _assistant({"type": "text", "text": "hi"}),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]


def test_a_steered_line_with_no_source_uuid_falls_back_to_the_row(transcript):
    turns = transcript([
        _user("hello"),
        _steered("again", uuid=""),
        _assistant({"type": "text", "text": "hi"}),
    ])
    assert turns[1] == {"role": "user", "text": "again", "uuid": "att-row-1",
                        "ts": pytest.approx(1792484899.129, abs=0.001)}


@pytest.mark.parametrize("extra", [
    {"origin": {"kind": "peer", "agentId": "a9df564ece27386ec"}, "isMeta": True},
    {"origin": {"kind": "coordinator"}, "isMeta": True},
    {"commandMode": "task-notification", "origin": {"kind": "task-notification"}},
])
def test_the_clis_own_machinery_on_the_same_record_is_not_a_turn(transcript, extra):
    """The same record carries a subagent's hand-back, a coordinator's note,
    a task notification (2,590 local rows measured 2026-10-20). None of it is
    something the reader typed."""
    turns = transcript([
        _user("hello"),
        _steered("<agent-message from=\"a9df564ece27386ec\"> [Subagent hand-back]"
                 " the report follows", **extra),
        _steered("Akshil: keep looping until green", **extra),
        _assistant({"type": "text", "text": "hi"}),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]


def test_a_human_line_with_an_explicit_origin_is_still_a_turn(transcript):
    turns = transcript([
        _user("hello"),
        _steered("and this", origin={"kind": "human"}, humanTurn=True),
        _assistant({"type": "text", "text": "hi"}),
    ])
    assert [t["text"] for t in turns if t["role"] == "user"] == ["hello", "and this"]
