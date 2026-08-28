"""What the working line can say beyond a verb: `_poll`'s `activity` block.

The status line used to read "Thinking… (47s)" with a frozen token count over
a minute of extended thinking, a Bash `sleep 30`, a 40 KB Write streaming its
input, or a slow hook — indistinguishable from a hang. Every one of those is a
row the CLI already writes to out.jsonl (captured from real runs, 2026-08-28):

  * `system`/`status {"status": "requesting"}` — request out, no token back.
  * `system`/`thinking_tokens {estimated_tokens}` — thinking progress.
  * `content_block_start tool_use` (name) + `input_json_delta` — a tool call
    and its input streaming; the finalized `assistant` row carries the input.
  * `user` row with `tool_result` — the tool finished.
  * `system`/`hook_started` … `hook_response` — a hook running.
  * `system`/`task_started` … `task_notification` / `background_tasks_changed`.
  * `parent_tool_use_id` set — a subagent's rows (SDK contract; not yet seen
    in a local run, so counted rather than rendered).

Fixtures are shared with test_claude_stream.py by shape, not import: one file
per concern keeps each self-contained.
"""
import importlib.util
import json
import os

import pytest

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load(name):
    path = os.path.join(TEMPLATE_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent():
    return _load("agent")


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "runs" / "run"
    (d / "perm").mkdir(parents=True)
    (d / "appstate").mkdir(parents=True)
    return d


def _poll(agent, run_dir, rows, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    body = "".join(json.dumps(r) + "\n" if isinstance(r, dict) else r
                   for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")
    return agent._poll("run")


# ------------------------------------------------------------ row builders
def _ev(event, **extra):
    row = {"type": "stream_event", "event": event, "session_id": "s"}
    row.update(extra)
    return row


def _delta(kind, _row=None, **fields):
    d = {"type": kind}
    d.update(fields)
    return _ev({"type": "content_block_delta", "index": 0, "delta": d}, **(_row or {}))


def _thinking(text="hm", **row):
    return _delta("thinking_delta", row, thinking=text)


def _text(text, **row):
    return _delta("text_delta", row, text=text)


def _tool_start(name, id="toolu_1"):
    return _ev({"type": "content_block_start", "index": 1,
                "content_block": {"type": "tool_use", "id": id, "name": name, "input": {}}})


def _input_json(fragment):
    return _delta("input_json_delta", partial_json=fragment)


def _assistant_tool(name, inp, id="toolu_1"):
    return {"type": "assistant", "session_id": "s", "message": {
        "role": "assistant", "content": [
            {"type": "tool_use", "id": id, "name": name, "input": inp}]}}


def _tool_result(id="toolu_1", **extra):
    row = {"type": "user", "session_id": "s", "parent_tool_use_id": None,
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": id, "content": "ok"}]}}
    row.update(extra)
    return row


def _status(status="requesting"):
    return {"type": "system", "subtype": "status", "status": status, "session_id": "s"}


def _thinking_tokens(total, delta=None):
    return {"type": "system", "subtype": "thinking_tokens", "session_id": "s",
            "estimated_tokens": total,
            "estimated_tokens_delta": delta if delta is not None else total}


def _sys(subtype, **fields):
    row = {"type": "system", "subtype": subtype, "session_id": "s"}
    row.update(fields)
    return row


def _message_start():
    return _ev({"type": "message_start", "message": {"id": "m", "usage": {}}})


# ----------------------------------------------------------------- phases

def test_a_fresh_run_is_thinking_with_an_empty_activity(agent, run_dir):
    data = _poll(agent, run_dir, [])
    assert data["phase"] == "thinking"
    assert data["activity"] == {"tool": None, "tool_input_bytes": 0,
                                "thinking_tokens": 0, "hook": "", "tasks": [],
                                "agent_rows": 0}


def test_a_request_out_with_nothing_back_is_requesting(agent, run_dir):
    """The most common "what is it doing": the CLI's own `status: requesting`
    row, written when the request leaves and before any token returns."""
    data = _poll(agent, run_dir, [_status()])
    assert data["phase"] == "requesting"


def test_the_first_delta_ends_requesting(agent, run_dir):
    assert _poll(agent, run_dir, [_status(), _thinking()])["phase"] == "thinking"
    assert _poll(agent, run_dir, [_status(), _text("a")])["phase"] == "composing"


def test_thinking_progress_rides_the_cli_estimate(agent, run_dir):
    """`message_delta` (the only token count the line had) arrives once, at
    message end — so a minute of thinking showed a frozen counter. The CLI's
    running `thinking_tokens` estimate is the number that moves."""
    rows = [_message_start(), _thinking(), _thinking_tokens(50),
            _thinking(), _thinking_tokens(120, 70)]
    data = _poll(agent, run_dir, rows)
    assert data["phase"] == "thinking"
    assert data["activity"]["thinking_tokens"] == 120


def test_thinking_estimate_resets_per_message_and_hides_off_phase(agent, run_dir):
    rows = [_message_start(), _thinking(), _thinking_tokens(400), _text("done"),
            _message_start(), _thinking(), _thinking_tokens(30)]
    assert _poll(agent, run_dir, rows)["activity"]["thinking_tokens"] == 30
    # Composing: the estimate is not what the line is about any more.
    rows = [_message_start(), _thinking(), _thinking_tokens(400), _text("x")]
    data = _poll(agent, run_dir, rows)
    assert data["phase"] == "composing" and data["activity"]["thinking_tokens"] == 0


# ------------------------------------------------------------------- tools

def test_a_tool_call_is_named_the_moment_it_starts(agent, run_dir):
    data = _poll(agent, run_dir, [_tool_start("Bash")])
    assert data["phase"] == "tooling"
    assert data["activity"]["tool"] == {"id": "toolu_1", "name": "Bash", "detail": ""}


def test_streaming_input_is_measured_while_the_detail_is_unknown(agent, run_dir):
    """A big Write streams its input for many seconds before the finalized
    `assistant` row arrives with the whole thing. Bytes are the progress."""
    rows = [_tool_start("Write"), _input_json('{"file_path": "/a/b/big.py", "con'),
            _input_json('tent": "' + "x" * 3000)]
    data = _poll(agent, run_dir, rows)
    assert data["activity"]["tool"]["name"] == "Write"
    assert data["activity"]["tool"]["detail"] == ""
    assert data["activity"]["tool_input_bytes"] == len('{"file_path": "/a/b/big.py", "con') + len('tent": "') + 3000


def test_the_finalized_row_fills_in_the_detail(agent, run_dir):
    rows = [_tool_start("Bash"), _input_json("{}"),
            _assistant_tool("Bash", {"command": "sleep 30", "description": "Wait for the deploy"})]
    data = _poll(agent, run_dir, rows)
    assert data["activity"]["tool"] == {"id": "toolu_1", "name": "Bash",
                                        "detail": "Wait for the deploy"}
    assert data["phase"] == "tooling"


@pytest.mark.parametrize("name, inp, detail", [
    ("Bash", {"command": "ls -la\npwd"}, "ls -la"),
    ("Read", {"file_path": "/x/y/agent.py"}, "agent.py"),
    ("Edit", {"file_path": "/x/template.html", "old_string": "a"}, "template.html"),
    ("Write", {"file_path": "/x/new.md"}, "new.md"),
    ("Grep", {"pattern": "def _poll"}, "def _poll"),
    ("Task", {"description": "Map the phase machine", "subagent_type": "Explore"}, "Map the phase machine"),
    ("Agent", {"subagent_type": "Explore"}, "Explore"),
    ("Skill", {"skill": "brainstorming"}, "brainstorming"),
    ("WebFetch", {"url": "https://example.com"}, "https://example.com"),
    ("mcp__cmux__screenshot", {"x": 1}, ""),
    ("Bash", {"description": "  spaces   and\nnewlines  "}, "spaces and newlines"),
    ("Bash", {"description": "d" * 200}, "d" * 77 + "…"),
    ("Bash", "not a dict", ""),
])
def test_tool_detail_is_one_short_line(agent, name, inp, detail):
    assert agent._tool_detail(name, inp) == detail


def test_a_finished_tool_clears_the_line(agent, run_dir):
    """THE reported case: the tool ended, nothing has streamed, the line said
    "Working…". A `tool_result` row means the tool is gone; until the CLI says
    `requesting` (or streams), the phase is `requesting` — not `tooling`."""
    rows = [_tool_start("Bash"), _assistant_tool("Bash", {"command": "sleep 30"}),
            _tool_result()]
    data = _poll(agent, run_dir, rows)
    assert data["activity"]["tool"] is None
    assert data["activity"]["tool_input_bytes"] == 0
    assert data["phase"] == "requesting"


def test_requesting_after_a_tool_then_thinking_again(agent, run_dir):
    rows = [_tool_start("Bash"), _tool_result(), _status(), _message_start(), _thinking()]
    data = _poll(agent, run_dir, rows)
    assert data["phase"] == "thinking" and data["activity"]["tool"] is None


def test_a_second_tool_starts_clean(agent, run_dir):
    rows = [_tool_start("Write", id="t1"), _input_json("x" * 500), _tool_result(id="t1"),
            _status(), _tool_start("Read", id="t2")]
    data = _poll(agent, run_dir, rows)
    assert data["activity"]["tool"]["name"] == "Read"
    assert data["activity"]["tool_input_bytes"] == 0


def test_a_tool_that_finished_before_the_result_row_stays_open(agent, run_dir):
    """`content_block_stop` and even the finalized assistant row do not mean the
    tool has RUN — only the result row does. A Bash sleeping 30 s is exactly a
    finalized call with no result yet."""
    rows = [_tool_start("Bash"), _ev({"type": "content_block_stop", "index": 1}),
            _assistant_tool("Bash", {"command": "sleep 30", "description": "Nap"})]
    data = _poll(agent, run_dir, rows)
    assert data["phase"] == "tooling"
    assert data["activity"]["tool"]["detail"] == "Nap"


# ------------------------------------------------------------ hooks & tasks

def test_a_hook_in_flight_is_named(agent, run_dir):
    rows = [_sys("hook_started", hook_id="h1", hook_name="SessionStart:startup",
                 hook_event="SessionStart")]
    assert _poll(agent, run_dir, rows)["activity"]["hook"] == "SessionStart:startup"


def test_a_hook_that_responded_is_gone(agent, run_dir):
    rows = [_sys("hook_started", hook_id="h1", hook_name="SessionStart:startup"),
            _sys("hook_response", hook_id="h1", hook_name="SessionStart:startup", output="")]
    assert _poll(agent, run_dir, rows)["activity"]["hook"] == ""


def test_the_newest_open_hook_wins(agent, run_dir):
    rows = [_sys("hook_started", hook_id="h1", hook_name="A"),
            _sys("hook_started", hook_id="h2", hook_name="B"),
            _sys("hook_response", hook_id="h1", hook_name="A")]
    assert _poll(agent, run_dir, rows)["activity"]["hook"] == "B"


def test_background_tasks_are_listed_until_they_report(agent, run_dir):
    rows = [_sys("task_started", task_id="b1", tool_use_id="toolu_1",
                 description="Poll PR checks every 5 min", is_backgrounded=True,
                 task_type="local_bash")]
    data = _poll(agent, run_dir, rows)
    assert data["activity"]["tasks"] == [{"id": "b1", "description": "Poll PR checks every 5 min"}]
    rows.append(_sys("task_notification", task_id="b1", status="completed", summary="…"))
    assert _poll(agent, run_dir, rows)["activity"]["tasks"] == []


def test_task_updated_to_a_terminal_status_removes_it(agent, run_dir):
    rows = [_sys("task_started", task_id="b1", description="x"),
            _sys("task_updated", task_id="b1", patch={"status": "killed", "end_time": 1})]
    assert _poll(agent, run_dir, rows)["activity"]["tasks"] == []
    rows = [_sys("task_started", task_id="b1", description="x"),
            _sys("task_updated", task_id="b1", patch={"status": "running"})]
    assert len(_poll(agent, run_dir, rows)["activity"]["tasks"]) == 1


def test_background_tasks_changed_is_authoritative(agent, run_dir):
    rows = [_sys("task_started", task_id="b1", description="old"),
            _sys("background_tasks_changed", tasks=[
                {"task_id": "b2", "task_type": "local_bash", "description": "new"}])]
    assert _poll(agent, run_dir, rows)["activity"]["tasks"] == [{"id": "b2", "description": "new"}]
    rows.append(_sys("background_tasks_changed", tasks=[]))
    assert _poll(agent, run_dir, rows)["activity"]["tasks"] == []


# --------------------------------------------------------------- subagents

def test_subagent_rows_are_counted_and_do_not_move_the_line(agent, run_dir):
    """A subagent's stream comes tagged with the parent call's id. Its text
    deltas must not flip the parent's line to "Composing" while the parent is
    still "Running agent"."""
    rows = [_tool_start("Agent"), _assistant_tool("Agent", {"description": "Map the code"}),
            _text("child says hi", parent_tool_use_id="toolu_1"),
            _thinking("child thinks", parent_tool_use_id="toolu_1")]
    data = _poll(agent, run_dir, rows)
    assert data["phase"] == "tooling"
    assert data["activity"]["tool"]["detail"] == "Map the code"
    assert data["activity"]["agent_rows"] == 2
    assert data["text"] == "", "a child's prose is not the parent's reply"


# ------------------------------------------------------ precedence & legacy

def test_a_retry_outranks_every_activity(agent, run_dir):
    retry = {"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
             "retry_delay_ms": 5, "error_status": 529, "error": "overloaded", "session_id": "s"}
    rows = [_tool_start("Bash"), _tool_result(), retry]
    data = _poll(agent, run_dir, rows)
    assert data["phase"] == "retrying"


def test_a_parked_approval_outranks_a_tool(agent, run_dir):
    rows = [_tool_start("Bash"), _assistant_tool("Bash", {"command": "rm x"})]
    (run_dir / "perm" / "p1.req.json").write_text(json.dumps(
        {"id": "p1", "tool": "Bash", "input": {"command": "rm x"}}), encoding="utf-8")
    data = _poll(agent, run_dir, rows)
    assert data["phase"] == "awaiting"


def test_the_legacy_fields_are_untouched(agent, run_dir):
    """`activity` is additive: text/tokens/done/segments are what they were."""
    rows = [_message_start(), _thinking(), _thinking_tokens(9), _text("hi"),
            _ev({"type": "message_delta", "usage": {"output_tokens": 7}}),
            _ev({"type": "message_stop"}),
            {"type": "result", "session_id": "s1", "result": "hi", "is_error": False}]
    data = _poll(agent, run_dir, rows)
    assert data["text"] == "hi" and data["tokens"] == 7 and data["done"]
    assert [s["kind"] for s in data["segments"]] == ["thinking", "text"]


def test_a_half_written_last_line_never_breaks_activity(agent, run_dir):
    rows = [_tool_start("Bash"), '{"type": "system", "subtype": "thinking_to']
    data = _poll(agent, run_dir, rows)
    assert data["activity"]["tool"]["name"] == "Bash"


def test_the_real_capture_replays_cleanly(agent, run_dir):
    """A slice of a real out.jsonl (claude 2.1.x, 2026-08-27) in file order:
    tool call → result → status → thinking → text → result."""
    rows = [
        _sys("init", cwd="/x", tools=["Bash"]),
        _sys("hook_started", hook_id="h", hook_name="SessionStart:startup"),
        _sys("hook_response", hook_id="h", hook_name="SessionStart:startup"),
        _status(), _message_start(),
        _ev({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        _thinking(), _thinking_tokens(50), _thinking(), _thinking_tokens(101, 51),
        _delta("signature_delta", signature="sig"),
        _ev({"type": "content_block_stop", "index": 0}),
        _tool_start("Bash"), _input_json('{"command": "sleep 2", "description": "Nap"}'),
        _ev({"type": "content_block_stop", "index": 1}),
        _ev({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 40}}),
        _ev({"type": "message_stop"}),
        _assistant_tool("Bash", {"command": "sleep 2", "description": "Nap"}),
    ]
    at_tool = _poll(agent, run_dir, rows)
    assert at_tool["phase"] == "tooling" and at_tool["activity"]["tool"]["detail"] == "Nap"
    assert at_tool["activity"]["hook"] == ""
    rows.append(_tool_result(timestamp="2026-08-27T15:20:40.000Z", tool_use_result={"stdout": ""}))
    assert _poll(agent, run_dir, rows)["phase"] == "requesting"
    rows += [_status(), _message_start(), _thinking(), _thinking_tokens(20)]
    mid = _poll(agent, run_dir, rows)
    assert mid["phase"] == "thinking" and mid["activity"]["thinking_tokens"] == 20
    rows += [_text("All done."),
             _ev({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 12}}),
             _ev({"type": "message_stop"}),
             {"type": "result", "session_id": "s", "result": "All done.", "is_error": False}]
    end = _poll(agent, run_dir, rows)
    assert end["done"] and end["text"] == "All done." and end["tokens"] == 52
