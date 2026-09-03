"""`_poll` reads one turn, not the whole session.

A run used to BE a turn, so re-parsing all of `out.jsonl` on every ~400ms tick
was cheap. Tasks 2-4 made a run a whole multi-turn session behind one held-open
`claude` process, so that file only grows for as long as the chat is open — a
day-long session makes a full re-scan megabytes, 2.5 times a second.

`_poll` now persists a byte offset in `run_dir/cursor` and only reads from
there forward on its NEXT call. The offset only ever advances past a `result`
row that provably has more bytes after it (see `_read_current_turn`'s
docstring) — a `result` with nothing after it YET is exactly the D415 "turn
might reopen" case, and a `result` followed by a `<task-notification>` wake is
not a turn boundary at all from the page's point of view (a single poll call
must still return a wake and its continuation together, same as before this
cursor existed — `test_claude_agent_segments.py`'s notice-segment tests pin
that). What the cursor buys is real: a page that has been watching a run
continuously pays only for what changed since it last looked, same as a
`claude_spawn.record_session_when_ready` loop polling the same run in
parallel. A page that attaches to an already-multi-turn run for the very
first time still gets the whole history in that one call — a one-time cost,
not a per-poll one.
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


def _write(run_dir, rows):
    """Overwrites out.jsonl with exactly `rows` (dicts get json-encoded plus a
    trailing newline; a raw string is written as-is, letting a test spell out
    a half-written or missing-newline final line)."""
    body = "".join(json.dumps(r) + "\n" if isinstance(r, dict) else r
                   for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")


def _append(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" if isinstance(r, dict) else r
                   for r in rows)
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(body)


def _cursor(run_dir):
    try:
        return int((run_dir / "cursor").read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return None


def _text_row(chunk):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": chunk}}}


def _result_row(session="s"):
    return {"type": "result", "session_id": session, "result": "done"}


def _user_row(text):
    """The CLI's own `--replay-user-messages` echo of a turn `_send`/`_start`
    put on the wire — the one row shape `_starts_new_turn` treats as
    provably a fresh turn, as opposed to a D415 wake (which never has this
    exact `content`-is-a-list shape)."""
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _tool_result_row(id="toolu_1"):
    """A tool's result landing mid-turn — `type: "user"` with a LIST content,
    same as `_user_row`'s echoed-turn shape, but the list holds a
    `tool_result` block rather than the `_write_inbox_entry` text block a
    real echoed turn always carries. Every tool-using turn produces one or
    more of these; `_starts_new_turn` must not mistake it for a fresh turn."""
    return {"type": "user", "parent_tool_use_id": None, "message": {
        "role": "user", "content": [
            {"type": "tool_result", "tool_use_id": id, "content": "ok"}]}}


def _poll(agent, run_dir, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    return agent._poll("run")


# ----------------------------------------------------- (a) cold vs warm cursor

def test_a_naturally_advanced_cursor_trims_the_next_poll_to_the_new_turn(
        agent, run_dir):
    """A realistic sequence: turn 1 streams, closes, and turn 2 — a genuine
    new user message, echoed back by `--replay-user-messages` — starts before
    anyone polls again. THAT poll (the one that first sees turn 1's `result`
    with turn 2's own echoed row already behind it) still returns the full
    window — the cursor it writes only takes effect on the poll AFTER it. The
    one after that reads turn 2 alone, byte-identical to a page that
    hand-placed a cursor at the same offset (test below) — proving the offset
    the natural advance computed is the same one a warm read would compute."""
    _write(run_dir, [_text_row("first turn")])
    mid_turn_1 = _poll(agent, run_dir)
    assert mid_turn_1["text"] == "first turn"
    assert _cursor(run_dir) is None, \
        "turn 1's result has not been written yet, nothing to skip past"

    _append(run_dir, [_result_row(), _user_row("second turn"),
                       _text_row("second turn")])
    still_full_window = _poll(agent, run_dir)
    assert still_full_window["text"] == "first turnsecond turn", \
        "the cursor advance this poll computes only applies to the NEXT call"
    warm_cursor = _cursor(run_dir)
    assert warm_cursor is not None and warm_cursor > 0

    trimmed = _poll(agent, run_dir)
    assert trimmed["text"] == "second turn"


def test_a_cursor_planted_directly_at_the_newest_turn_reads_identically(
        agent, run_dir):
    """Hand-places the cursor at the exact byte offset a real poll sequence
    would have advanced it to, and confirms a fresh `_poll` call lands on the
    same result a poll that arrived there naturally would."""
    turn_1 = "".join(json.dumps(r) + "\n" for r in
                      [_text_row("first turn"), _result_row()])
    turn_2 = "".join(json.dumps(r) + "\n" for r in
                      [_user_row("second turn"), _text_row("second turn")])
    _write(run_dir, [turn_1 + turn_2])

    (run_dir / "cursor").write_text(str(len(turn_1.encode("utf-8"))),
                                    encoding="utf-8")
    warm = _poll(agent, run_dir)
    assert warm["text"] == "second turn"

    # Same file, no cursor at all: a single cold call still lands on the full
    # history (turn 1's text included) rather than raising or corrupting
    # anything — cursor absence never means "assume the newest turn only".
    (run_dir / "cursor").unlink()
    cold = _poll(agent, run_dir)
    assert cold["text"] == "first turnsecond turn"


# --------------------------------------------------------------- (b) bad cursor

@pytest.mark.parametrize("garbage", ["not-a-number", "-5", "999999999"])
def test_a_bad_cursor_falls_back_to_a_full_parse(agent, run_dir, garbage):
    _write(run_dir, [_text_row("first turn"), _result_row(),
                      _text_row("second turn")])
    (run_dir / "cursor").write_text(garbage, encoding="utf-8")

    data = _poll(agent, run_dir)
    # A full parse from 0 sees everything, exactly as a missing cursor would —
    # the garbage cursor must not have caused rows to be skipped, an offset
    # to land mid-row, or an exception to propagate.
    assert data["text"] == "first turnsecond turn"


def test_a_missing_out_jsonl_is_not_a_bad_cursor(agent, run_dir):
    # No out.jsonl written at all — a run that has not produced a single row
    # yet. Must not raise, must report an empty, not-yet-done poll.
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: True
    data = agent._poll("run")
    assert data["text"] == ""
    assert data["done"] is False


# --------------------------------------------------------------- (c) wake after result

def test_a_wake_after_a_trailing_result_is_not_skipped(agent, run_dir):
    # The turn closes and NOTHING follows yet — same shape a real out.jsonl
    # has the instant the CLI writes its `result` row and goes quiet.
    _write(run_dir, [_text_row("first turn"), _result_row()])

    first = _poll(agent, run_dir)
    assert first["text"] == "first turn"
    assert first["done"] is True
    # Nothing followed the result at poll time, so the cursor must NOT have
    # advanced past it — advancing here would mean a wake's rows land before
    # the cursor and are silently skipped forever.
    assert _cursor(run_dir) in (None, 0)

    # The harness wakes the same process for another turn: a hook or fresh
    # row lands after the result that used to be the last line.
    _append(run_dir, [_text_row("woken up")])

    second = _poll(agent, run_dir)
    # A single poll call always reads everything since the LAST call saw —
    # here that is still the whole file, since the first call never advanced
    # the cursor. The woken text must be present, not silently dropped.
    assert second["text"] == "first turnwoken up", \
        "the wake's row must still be read, not skipped by an over-eager " \
        "cursor advance"
    assert second["done"] is False


def test_a_wake_is_not_skipped_even_once_the_cursor_has_moved(agent, run_dir):
    """The same wake-not-skipped guarantee, but with an EARLIER turn already
    behind the cursor — proving the advance logic only ever skips bytes a
    previous call already returned, never bytes still waiting to be seen."""
    _write(run_dir, [_text_row("first turn"), _result_row(),
                      _user_row("second turn"), _text_row("second turn")])
    first = _poll(agent, run_dir)
    assert first["text"] == "first turnsecond turn"
    assert _cursor(run_dir) not in (None, 0), \
        "turn 1's result was followed by turn 2's own echoed message, so " \
        "it advanced"

    _append(run_dir, [_result_row(), _text_row("woken up")])
    second = _poll(agent, run_dir)
    assert second["text"] == "second turnwoken up"


# ------------------------------------------------------- (d) tool_result is not a turn

def test_a_tool_result_row_does_not_advance_the_cursor(agent, run_dir):
    """A tool_result row is `type: "user"` with a list `content`, the same
    top-level shape `_user_row`'s genuine echoed turn has — but it is not one,
    and treating it as one would silently drop everything before it (the
    tool-using message's own tokens/text included) from every later poll."""
    _write(run_dir, [_text_row("call a tool"), _tool_result_row(),
                      _text_row(" and reply")])
    result = _poll(agent, run_dir)
    assert result["text"] == "call a tool and reply"
    assert _cursor(run_dir) in (None, 0), \
        "a tool_result is not a fresh, user-authored turn"


# ------------------------------------------ (e) a follow-up absorbed mid-turn

def test_a_followup_echoed_midturn_does_not_advance_the_cursor(agent, run_dir):
    """`_send`'s whole point is a follow-up landing INSIDE a turn still in
    flight — the CLI echoes it back (`--replay-user-messages`) with no
    `result` row in between, because the turn it is being absorbed into has
    not closed yet. `_starts_new_turn` reads that echo exactly like a genuine
    new turn's own opening message, but the docstring's premise ("a real new
    turn only begins once the one before it is completely finished") does not
    hold here — the cursor must not jump past text this turn already
    streamed, or a later poll's `segments`/`text` would shrink and silently
    erase what is already on screen."""
    _write(run_dir, [_user_row("turn one"),
                      _text_row("Part one of the answer. ")])
    first = _poll(agent, run_dir)
    assert first["text"] == "Part one of the answer. "

    # The follow-up's own echo lands mid-turn — no `result` row before it.
    _append(run_dir, [_user_row("continue"), _text_row("Part two. ")])
    second = _poll(agent, run_dir)
    assert second["text"] == "Part one of the answer. Part two. ", \
        "the absorbed follow-up must not have advanced the cursor past " \
        "text this turn already streamed"

    # A third poll (nothing new) must see EXACTLY the same window — proving
    # the cursor genuinely did not move, not just that this one read was
    # still wide enough to cover the gap.
    third = _poll(agent, run_dir)
    assert third["text"] == second["text"]
    assert len(third["segments"]) == len(second["segments"]), \
        "segments must never shrink between polls of the same open turn"
