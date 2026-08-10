"""The chat template's SEGMENTED transcript: `_segments_from_rows`, and the two
callers that must not disagree about it.

The page used to receive one flat string per turn, so everything that was not
prose — thinking, a tool call, what the tool answered — either vanished or was
concatenated into the reply as if Claude had typed it. Segments are the ordered
record instead: text / thinking / tool, in the order they happened, with each
`tool_result` joined back to the `tool_use` that asked for it.

Three properties are worth pinning here, and none of them is visible in a test
of anything else:

* **`data["text"]` did not move.** The segment list is additive. `_poll`'s text
  accumulation is byte-for-byte what it was, separators included, and the text
  segments joined back together reproduce it — asserted against a SECOND,
  independent copy of the old accumulation rule (`_old_text` below) rather than
  against a golden string, because a golden string would have to be regenerated
  by the very code it is supposed to be checking.
* **One reader, two row sources.** `_poll` reads the live `out.jsonl` (which
  carries `stream_event` rows, so text arrives as deltas) and `_history` reads
  the persisted session transcript (no stream rows, so text arrives as finalized
  blocks). Both go through `_segments_from_rows`, so both are exercised here.
* **The wire is messier than the happy path.** A `tool_result`'s `content` is a
  plain string for most tools and a list of typed blocks for the ones returning
  images; a run can die with half a line written; a result can name a
  `tool_use_id` nothing matches. Each of those has a test below because each of
  them is on the real wire, not because it is a hypothetical.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_seg_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


def _poll_rows(agent, tmp_path, rows, tail=""):
    """`_poll` over a run whose `out.jsonl` is exactly `rows` (+ raw `tail`).

    `_alive` is forced True because these fixtures write no pid file and a run
    with no `result` row would otherwise be reported as a crash — which is a
    different test's subject.
    """
    run_dir = tmp_path / "run"
    os.makedirs(run_dir / "perm", exist_ok=True)
    with open(run_dir / "out.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
        fh.write(tail)
    agent.RUNS = str(tmp_path)
    agent._alive = lambda _run_dir: True
    return agent._poll("run")


def _delta(kind, value):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": kind, kind.split("_")[0]: value}}}


_STOP = {"type": "stream_event", "event": {"type": "message_stop"}}


def _rows():
    """One tool-using turn, in the shapes the CLI actually emits: thinking and
    text as deltas, the tool call only on the FINALIZED `assistant` row, and its
    result on a `user` row."""
    return [
        {"type": "system", "session_id": "s1"},
        _delta("thinking_delta", "hmm"),
        _delta("text_delta", "Let me edit."),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Let me edit."},
            {"type": "tool_use", "id": "tu1", "name": "Edit",
             "input": {"file_path": "/a.py", "old_string": "x=1",
                       "new_string": "x=2"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": [{"type": "text", "text": "ok"}]}]}},
        _STOP,
        {"type": "result", "result": "Let me edit.", "session_id": "s1"},
    ]


# ------------------------------------------------------------ order and joins

def test_segments_order_and_tool_join(agent, tmp_path):
    data = _poll_rows(agent, tmp_path, _rows())
    assert [s["kind"] for s in data["segments"]] == ["thinking", "text", "tool"]
    assert data["segments"][0]["text"] == "hmm"
    assert data["segments"][1]["text"] == "Let me edit."
    tool = data["segments"][2]
    assert tool["id"] == "tu1" and tool["name"] == "Edit"
    assert tool["status"] == "ok" and tool["output"] == "ok"
    assert tool["images"] == []
    # The input rides through verbatim: the page renders a diff from it, so a
    # normalised or re-serialised copy is a rendering bug waiting to happen.
    assert tool["input"] == {"file_path": "/a.py", "old_string": "x=1",
                             "new_string": "x=2"}


def test_a_tool_still_waiting_for_its_result_is_running(agent, tmp_path):
    rows = [r for r in _rows() if r.get("type") != "user"]
    data = _poll_rows(agent, tmp_path, rows)
    tool = data["segments"][2]
    assert tool["status"] == "running"
    assert tool["output"] is None, "an absent result must not read as empty output"


def test_the_finalized_assistant_row_is_the_only_source_of_a_tool_call(agent, tmp_path):
    """The streamed `content_block_start` for the same call arrives with
    `input: {}` and its arguments only as `input_json_delta` fragments, so the
    finalized row is read and that one is ignored — otherwise every tool call is
    reported twice, once of them blank."""
    rows = _rows()
    rows.insert(3, {"type": "stream_event", "event": {
        "type": "content_block_start",
        "content_block": {"type": "tool_use", "id": "tu1", "name": "Edit",
                          "input": {}}}})
    data = _poll_rows(agent, tmp_path, rows)
    tools = [s for s in data["segments"] if s["kind"] == "tool"]
    assert len(tools) == 1 and tools[0]["input"]["new_string"] == "x=2"


def test_a_replayed_assistant_row_does_not_report_the_tool_twice(agent, tmp_path):
    rows = _rows()
    rows.insert(4, rows[3])  # the same finalized message, written twice
    data = _poll_rows(agent, tmp_path, rows)
    assert [s["kind"] for s in data["segments"]] == ["thinking", "text", "tool"]


def test_several_tool_calls_in_one_message_keep_their_own_results(agent, tmp_path):
    data = _poll_rows(agent, tmp_path, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "/1"}},
            {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "/2"}}]}},
        # Answered out of call order, which parallel tools do routinely.
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "b", "content": "two"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "a", "content": "one"}]}},
    ])
    assert [(s["id"], s["output"]) for s in data["segments"]] == [
        ("a", "one"), ("b", "two")]


# ------------------------------------------------------- the result's payload

def test_a_string_content_tool_result_is_read_too(agent, tmp_path):
    """Most tools return `content` as a plain STRING; only the image-returning
    ones use the block list. Reading one shape reports the other as empty."""
    rows = _rows()
    rows[4]["message"]["content"][0]["content"] = "plain string output"
    data = _poll_rows(agent, tmp_path, rows)
    assert data["segments"][2]["output"] == "plain string output"


def test_an_errored_tool_result_says_so(agent, tmp_path):
    rows = _rows()
    rows[4]["message"]["content"][0].update(
        {"is_error": True, "content": "File does not exist."})
    data = _poll_rows(agent, tmp_path, rows)
    tool = data["segments"][2]
    assert tool["status"] == "error" and tool["output"] == "File does not exist."


def test_image_blocks_in_a_tool_result_are_captured(agent, tmp_path):
    rows = _rows()
    rows[4]["message"]["content"][0]["content"] = [
        {"type": "text", "text": "shot taken"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": "AAAA"}}]
    tool = _poll_rows(agent, tmp_path, rows)["segments"][2]
    assert tool["output"] == "shot taken"
    assert tool["images"] == [{"media_type": "image/png", "data": "AAAA"}]


def test_an_oversized_image_is_dropped_and_the_output_says_it_was(agent, tmp_path):
    """Every poll re-sends the whole segment list, so a multi-megabyte base64
    blob would be re-encoded and re-parsed for the rest of the turn. Dropped
    SILENTLY it would look like a tool that returned nothing, hence the note."""
    rows = _rows()
    big = "A" * (agent.SEGMENT_IMAGE_CAP + 1)
    rows[4]["message"]["content"][0]["content"] = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": big}}]
    tool = _poll_rows(agent, tmp_path, rows)["segments"][2]
    assert tool["images"] == []
    assert "image dropped" in tool["output"]
    assert big not in tool["output"], "the blob came back through the note"


def test_output_is_capped_with_a_tail_saying_how_much_is_missing(agent, tmp_path):
    rows = _rows()
    rows[4]["message"]["content"][0]["content"] = "x" * 4500
    tool = _poll_rows(agent, tmp_path, rows)["segments"][2]
    assert tool["output"] == "x" * 4000 + "… (+500 chars)"
    assert agent.SEGMENT_OUTPUT_CAP == 4000


def test_output_exactly_at_the_cap_keeps_no_tail(agent, tmp_path):
    rows = _rows()
    rows[4]["message"]["content"][0]["content"] = "x" * 4000
    assert _poll_rows(agent, tmp_path, rows)["segments"][2]["output"] == "x" * 4000


def test_a_result_naming_an_unknown_tool_use_id_is_ignored(agent, tmp_path):
    rows = _rows()
    rows[4]["message"]["content"][0]["tool_use_id"] = "nope"
    data = _poll_rows(agent, tmp_path, rows)
    tool = data["segments"][2]
    assert tool["status"] == "running" and tool["output"] is None
    assert len([s for s in data["segments"] if s["kind"] == "tool"]) == 1, (
        "an unmatched result invented a segment of its own")


def test_a_result_that_lands_before_its_tool_use_still_joins(agent, tmp_path):
    """Rows are read in file order, and nothing guarantees the result cannot be
    flushed first. Dropping it would leave a finished tool stuck on "running"
    for the rest of the session."""
    rows = _rows()
    result_row = rows.pop(4)
    rows.insert(3, result_row)
    tool = _poll_rows(agent, tmp_path, rows)["segments"][2]
    assert tool["status"] == "ok" and tool["output"] == "ok"


# -------------------------------------------------------------- what is shown

def test_the_app_state_plumbing_tool_is_not_a_segment(agent, tmp_path):
    """`app_state` is this template's own bridge asking the page what it is
    showing — nobody asked for it and it has no output worth reading. Only that
    EXACT tool is stripped: every other MCP tool is a real call the user is
    entitled to see."""
    plumbing = "mcp__%s__%s" % (agent.PERMISSION_SERVER, agent.APP_STATE_TOOL)
    assert plumbing == "mcp__fused_approvals__app_state"
    data = _poll_rows(agent, tmp_path, [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "p1", "name": plumbing, "input": {}},
            {"type": "tool_use", "id": "o1", "name": "mcp__other__thing",
             "input": {"q": 1}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "p1", "content": "{...}"},
            {"type": "tool_result", "tool_use_id": "o1", "content": "answer"}]}},
    ])
    assert [(s["name"], s["output"]) for s in data["segments"]] == [
        ("mcp__other__thing", "answer")]


def test_meta_and_sidechain_rows_are_skipped(agent, tmp_path):
    """Synthetic rows (`isMeta`) and subagent rows (`isSidechain`) are not this
    conversation — the same guard `_history` has always applied to turns."""
    segments = agent._segments_from_rows([
        {"type": "assistant", "isMeta": True, "message": {"content": [
            {"type": "text", "text": "injected"}]}},
        {"type": "assistant", "isSidechain": True, "message": {"content": [
            {"type": "tool_use", "id": "s1", "name": "Bash", "input": {}}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "mine"}]}},
    ])
    assert [(s["kind"], s["text"]) for s in segments] == [("text", "mine")]


# ---------------------------------------------------------- text did not move

def _old_text(rows):
    """The text accumulation EXACTLY as `_poll` did it before segments existed.

    A second implementation on purpose (D146): asserting the new payload against
    a value produced by the new code would pass for any accumulation at all.
    """
    text_parts, pending_sep = [], False
    for row in rows:
        if row.get("type") != "stream_event":
            continue
        ev = row.get("event", {})
        if ev.get("type") == "content_block_delta":
            if ev.get("delta", {}).get("type") == "text_delta":
                if pending_sep:
                    text_parts.append("\n\n")
                    pending_sep = False
                text_parts.append(ev["delta"].get("text", ""))
        elif ev.get("type") == "message_stop":
            pending_sep = bool(text_parts)
    return "".join(text_parts)


def _multi_message_rows():
    """A tool-using turn: TWO assistant messages, which is what makes the
    separator matter — without it their texts concatenate mid-word."""
    return [
        {"type": "system", "session_id": "s1"},
        _delta("thinking_delta", "plan it"),
        _delta("text_delta", "First."),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "First."},
            {"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": "ls"}}]}},
        _STOP,
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "a\nb"}]}},
        _delta("text_delta", "Done."),
        _STOP,
        {"type": "result", "result": "Done.", "session_id": "s1"},
    ]


def test_text_is_byte_identical_to_the_pre_segments_accumulation(agent, tmp_path):
    rows = _multi_message_rows()
    data = _poll_rows(agent, tmp_path, rows)
    assert data["text"] == _old_text(rows) == "First.\n\nDone."


def test_the_text_segments_join_back_into_the_text_field(agent, tmp_path):
    """Two copies of one rule (the accumulator and the segment builder) need a
    test that they agree, not a comment saying they should. The message
    separator therefore lives INSIDE the segment that follows it."""
    rows = _multi_message_rows()
    data = _poll_rows(agent, tmp_path, rows)
    joined = "".join(s["text"] for s in data["segments"] if s["kind"] == "text")
    assert joined == data["text"]
    assert [s["kind"] for s in data["segments"]] == [
        "thinking", "text", "tool", "text"]


def test_a_streamed_message_does_not_also_adopt_its_finalized_text(agent, tmp_path):
    """The finalized `assistant` row repeats the text that already streamed as
    deltas. Reading both says everything twice."""
    data = _poll_rows(agent, tmp_path, _rows())
    assert [s["text"] for s in data["segments"] if s["kind"] == "text"] == [
        "Let me edit."]


def test_the_finalized_row_landing_after_message_stop_still_says_it_once(
        agent, tmp_path):
    """The de-duplication must not hinge on where the `assistant` row sits
    relative to its own `message_stop` — nothing in the format pins that, and a
    per-message flag reset at the stop would double every reply the moment the
    CLI reordered the two."""
    data = _poll_rows(agent, tmp_path, [
        _delta("text_delta", "Once."),
        _STOP,
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Once."}]}},
        {"type": "result", "result": "Once.", "session_id": "s1"},
    ])
    assert [(s["kind"], s["text"]) for s in data["segments"]] == [("text", "Once.")]
    assert data["text"] == "Once."


def test_a_transcript_without_stream_rows_still_yields_text_segments(agent, tmp_path):
    """An older CLI (or the persisted transcript) carries no `stream_event`
    rows at all, so the finalized blocks are the only text there is. `text`
    keeps its own documented fallback — the `result` row — and is untouched."""
    data = _poll_rows(agent, tmp_path, [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "one"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "two"}]}},
        {"type": "result", "result": "two", "session_id": "s1"},
    ])
    assert data["text"] == "two"  # the pre-existing fallback, unchanged
    assert [(s["kind"], s["text"]) for s in data["segments"]] == [
        ("text", "one\n\ntwo")]


def test_a_half_written_last_line_is_skipped(agent, tmp_path):
    """A run killed mid-write leaves a partial line. The existing loop skips it
    and the next poll gets it; the accumulator must not be the thing that turns
    that into a traceback."""
    data = _poll_rows(agent, tmp_path, _rows()[:4],
                      tail='{"type": "user", "message": {"cont')
    assert [s["kind"] for s in data["segments"]] == ["thinking", "text", "tool"]
    assert data["segments"][2]["status"] == "running"


def test_segments_do_not_disturb_the_rest_of_the_poll_payload(agent, tmp_path):
    """Additive means additive: every other field is what it was."""
    rows = _multi_message_rows()
    rows.insert(1, {"type": "stream_event", "event": {
        "type": "message_delta", "usage": {"output_tokens": 7}}})
    rows.insert(2, {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "sk", "name": "Skill",
         "input": {"skill": "brainstorming"}}]}})
    data = _poll_rows(agent, tmp_path, rows)
    assert data["done"] and data["session_id"] == "s1" and not data["error"]
    assert data["tokens"] == 7 and data["phase"] == "composing"
    assert data["skills"] == [{"id": "sk", "skill": "brainstorming"}]
    assert data["retry"] is None and data["retry_total"] == 0
    assert data["permissions"] == [] and data["app_state"] == []


def test_an_empty_run_has_an_empty_segment_list(agent, tmp_path):
    data = _poll_rows(agent, tmp_path, [])
    assert data["segments"] == []
    # ...and so does the unknown-run refusal, so the page never has to guard.
    agent.RUNS = str(tmp_path)
    assert agent._poll("nope")["segments"] == []


# ------------------------------------------------------------------- history

def _history(agent, tmp_path, monkeypatch, rows):
    """`_history` over a persisted transcript whose rows are `rows`.

    The transcript's row shape differs from `out.jsonl`: the API message nests
    under `message` WITH its `role`, and there are no `stream_event` rows.
    """
    target = tmp_path / "proj" / "page.html"
    os.makedirs(target.parent, exist_ok=True)
    target.write_text("<html></html>")
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "PROJECTS", str(projects))
    d = projects / agent._munge(str(target.parent))
    os.makedirs(d, exist_ok=True)
    with open(d / "sess1.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return agent._history(str(target), "sess1")["turns"]


def _t_user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _t_assistant(content):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": content}}


def _t_result(tool_use_id, content):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}}


def test_history_turns_carry_segments(agent, tmp_path, monkeypatch):
    turns = _history(agent, tmp_path, monkeypatch, [
        _t_user("fix the title"),
        _t_assistant([{"type": "text", "text": "Editing now."},
                      {"type": "tool_use", "id": "tu1", "name": "Edit",
                       "input": {"file_path": "/page.html"}}]),
        _t_result("tu1", "applied"),
        _t_assistant([{"type": "text", "text": "Done."}]),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    # The text turn is untouched — consecutive assistant rows still merge.
    assert turns[1]["text"] == "Editing now.\n\nDone."
    kinds = [(s["kind"], s.get("name") or s.get("text")) for s in turns[1]["segments"]]
    assert kinds == [("text", "Editing now."), ("tool", "Edit"), ("text", "Done.")]
    tool = turns[1]["segments"][1]
    assert tool["status"] == "ok" and tool["output"] == "applied"


def test_history_keeps_a_stretch_that_only_called_tools(agent, tmp_path, monkeypatch):
    """An assistant message with no prose used to leave no turn at all, so the
    restored conversation silently lost the work it did."""
    turns = _history(agent, tmp_path, monkeypatch, [
        _t_user("run the tests"),
        _t_assistant([{"type": "tool_use", "id": "tu1", "name": "Bash",
                       "input": {"command": "pytest"}}]),
        _t_result("tu1", "1 passed"),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["text"] == ""
    assert [(s["kind"], s["name"]) for s in turns[1]["segments"]] == [("tool", "Bash")]


def test_history_does_not_leak_one_turns_segments_into_the_next(
        agent, tmp_path, monkeypatch):
    turns = _history(agent, tmp_path, monkeypatch, [
        _t_user("one"),
        _t_assistant([{"type": "text", "text": "A"},
                      {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]),
        _t_result("t1", "r1"),
        _t_user("two"),
        _t_assistant([{"type": "text", "text": "B"},
                      {"type": "tool_use", "id": "t2", "name": "Glob", "input": {}}]),
        _t_result("t2", "r2"),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant"]
    assert [s.get("name") for s in turns[1]["segments"]] == [None, "Read"]
    assert [s.get("name") for s in turns[3]["segments"]] == [None, "Glob"]
    assert turns[3]["segments"][1]["output"] == "r2"


def test_history_skips_meta_and_sidechain_rows(agent, tmp_path, monkeypatch):
    meta = _t_assistant([{"type": "tool_use", "id": "m", "name": "Bash", "input": {}}])
    meta["isMeta"] = True
    side = _t_assistant([{"type": "tool_use", "id": "s", "name": "Task", "input": {}}])
    side["isSidechain"] = True
    turns = _history(agent, tmp_path, monkeypatch, [
        _t_user("go"), meta, side,
        _t_assistant([{"type": "text", "text": "hi"}]),
    ])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert [s["kind"] for s in turns[1]["segments"]] == ["text"]


def test_history_still_hides_the_pushed_app_state_block(agent, tmp_path, monkeypatch):
    """The transcript holds what claude was SENT. Segments must not become a
    second route for the block the user never typed and never saw."""
    tag = agent.APP_STATE_TAG
    turns = _history(agent, tmp_path, monkeypatch, [
        _t_user("<%s>{\"console\": []}</%s>\nfix it" % (tag, tag)),
        _t_assistant([{"type": "text", "text": "ok"}]),
    ])
    assert turns[0]["text"] == "fix it"
    assert tag not in json.dumps(turns)


def test_history_of_an_unknown_session_is_still_empty(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "PROJECTS", str(tmp_path / "projects"))
    target = tmp_path / "page.html"
    target.write_text("x")
    assert agent._history(str(target), "missing") == {"turns": []}
    assert agent._history(str(target), "../escape") == {"turns": []}
