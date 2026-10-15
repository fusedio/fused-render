"""Claude Code's `api-request-blob` records are not turns.

CLI 2.1.300 writes, beside the ordinary `user`/`assistant` records, one
`api-request-blob` record per block it put on the API wire — the system prompt,
the folded `<local-command-caveat>` + `/mcp` envelope, assistant replies — each
carrying a `message` with a `role` and no `uuid`. `_history` used to key a turn
on `message.role` alone, so the user-role copy of the slash-command fold painted
as a user bubble full of XML the user never typed (Akshil, 2026-10-15).

The regression fixture is a cut-down copy of the real transcript that showed it.
"""
import importlib.util
import json
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")
SESSION = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

FOLDED = ("<local-command-caveat>The command below was run directly in Claude "
          "Code, not sent to you as a request.</local-command-caveat>\n\n"
          "<command-name>/mcp</command-name>\n<command-message>mcp</command-message>"
          "\n<command-args></command-args>\n\n"
          "<local-command-stdout>27 MCP server(s): 3 connected</local-command-stdout>")


@pytest.fixture
def agent():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_api_rows", path)
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

    write.target = str(target)
    return write


@pytest.fixture
def history(agent, transcript):
    """The whole `_history` payload for what `transcript` last wrote."""
    return lambda: agent._history(transcript.target, SESSION)


def _turn(kind, text, uuid):
    return {"type": kind, "uuid": uuid,
            "message": {"role": kind,
                        "content": [{"type": "text", "text": text}]}}


def _blob(role, text):
    """The 2.1.300 record shape: a `message` with a role, no `uuid`, and a
    `type` that is neither `user` nor `assistant`."""
    return {"type": "api-request-blob", "sessionId": SESSION, "hash": "abc",
            "message": {"role": role,
                        "content": [{"type": "text", "text": text}]}}


def test_a_user_role_blob_is_not_a_user_bubble(transcript):
    turns = transcript([
        _blob("system", "You are Claude Code."),
        _turn("user", "what is this tool loaded", "u1"),
        _blob("user", FOLDED),
        _turn("assistant", "It is the Claude Docs connector.", "a1"),
    ])
    assert [(t["role"], t.get("uuid")) for t in turns] == [
        ("user", "u1"), ("assistant", "a1")]
    assert all("local-command" not in t["text"] for t in turns)


def test_an_assistant_role_blob_does_not_merge_into_the_reply(transcript):
    turns = transcript([
        _turn("user", "hi", "u1"),
        _turn("assistant", "hello", "a1"),
        _blob("assistant", "hello"),
        _blob("assistant", "a stale copy of some earlier reply"),
    ])
    assert [t["text"] for t in turns] == ["hi", "hello"]


def test_api_request_shape_rows_are_skipped_too(transcript):
    turns = transcript([
        {"type": "api-request-shape", "sessionId": SESSION,
         "shape": {"system": [{"type": "text", "text": "x"}]},
         "message": {"role": "system", "content": "ignored"}},
        _turn("user", "hi", "u1"),
    ])
    assert [(t["role"], t["text"]) for t in turns] == [("user", "hi")]


def test_a_system_row_with_a_user_role_message_is_not_a_turn(transcript):
    """The row kind decides, not the role it carries."""
    turns = transcript([
        {"type": "system", "subtype": "local_command", "uuid": "s1",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "not typed"}]}},
        _turn("user", "hi", "u1"),
    ])
    assert [(t["role"], t["text"]) for t in turns] == [("user", "hi")]


def test_a_typeless_record_is_not_a_turn_either(transcript):
    """No `type` at all is not a licence: the CLI names every record's kind."""
    turns = transcript([
        {"message": {"role": "user",
                     "content": [{"type": "text", "text": FOLDED}]}},
        _turn("user", "hi", "u1"),
    ])
    assert [(t["role"], t["text"]) for t in turns] == [("user", "hi")]


def test_a_real_turn_without_a_uuid_still_renders(transcript):
    """The gate is the row kind alone — a `uuid` is not what makes a turn."""
    turns = transcript([
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "text", "text": "hi"}]}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "yo"}]}},
    ])
    assert [(t["role"], t["text"]) for t in turns] == [("user", "hi"), ("assistant", "yo")]


def test_a_compact_boundary_still_reports_through_the_gate(transcript, history):
    """The `system` branch beside the new gate is untouched by it: a boundary
    with no API reading after it still leaves the compacted estimate."""
    transcript([
        _turn("user", "hi", "u1"),
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 900,
                             "postTokens": 123}},
    ])
    out = history()
    assert out["context"]["compacted"] is True
    assert out["context"]["input_tokens"] == 123
