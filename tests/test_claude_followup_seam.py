"""Where a mid-stream follow-up lands INSIDE a poll payload, and what a stop
does to the echo gate that payload is hidden behind.

Two defects from the R1 chat feedback, one file, because they are two ends of
the same window:

  * **#9** — a follow-up sent while a reply is streaming leaves ONE poll payload
    carrying two conversational turns, with nothing in `segments`/`text` to say
    where the seam is (a user echo row produces no segment of its own). The page
    could only guess it, and the guess it had — "the lengths as of the poll
    before the send" — is wrong by however much of the first reply streamed
    after the send, which `pending_echo` makes invisible. `_absorbed_turn_breaks`
    reports the seam instead.

  * **#12** — `_send` writes `pending_echo` and `_poll` clears it only by SEEING
    the echo. An `interrupt` is precisely the event that guarantees the echo
    never comes (the CLI drops its queue and reports it in `still_queued`) while
    leaving the host UP by design, so `_poll`'s liveness escape never fires
    either: the run stayed `done: False` for the life of the session and the
    chat's status stuck on "running" with the Stop chrome up. `_cancel` retires
    the file when the interrupt lands.
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


def _user_row(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _text_row(chunk):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": chunk}}}


def _result_row(text, session="s"):
    return {"type": "result", "session_id": session, "result": text}


def _tool_result_user_row(tool_id):
    """A `tool_result` is a `type: "user"` row with a LIST content too, and one
    lands after every tool call — far more often than a genuine new turn."""
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}}


def _write(run_dir, rows):
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")


def _setup(agent, run_dir, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    agent._pid_alive = lambda _pid: alive
    with open(run_dir / "host.json", "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242, "session_id": "s", "file": "", "mode": "",
                   "model": "", "effort": "", "read_dirs": []}, fh)


# ---------------------------------------------------- the seam itself (#9)

def test_an_ordinary_turn_reports_no_seam(agent):
    """The empty answer is the one every poll of every normal run gets, and it
    is what keeps the work below off the hot path."""
    rows = [_user_row("do the thing"), _text_row("Doing it."),
            _result_row("Doing it.")]
    assert agent._absorbed_turn_breaks(rows) == []


def test_a_tool_result_is_not_a_seam(agent):
    """`_starts_new_turn` already refuses a `tool_result` row; this pins that
    the seam scan agrees, because a false seam would split a reply in half
    every time it used a tool."""
    rows = [_user_row("look"), _text_row("Checking. "),
            _tool_result_user_row("t1"), _text_row("Done.")]
    assert agent._absorbed_turn_breaks(rows) == []


def test_a_follow_up_absorbed_mid_reply_is_reported_with_its_offsets(agent):
    """The reported case: the CLI drained the inbox in the middle of a reply, so
    the echo sits between the two halves with NO `result` before it — which is
    exactly why `_read_current_turn` will not advance its cursor past it, and
    why one payload has to carry both turns."""
    rows = [
        _user_row("first question"),
        _text_row("Reply A, first half. "),
        _user_row("second question"),        # the absorbed follow-up
        _text_row("Reply A, second half."),
        _result_row("Reply A, second half."),
        _text_row("Reply B."),
    ]
    breaks = agent._absorbed_turn_breaks(rows)
    assert len(breaks) == 1, "one follow-up, one seam"
    # THE SEAM IS REPLY A'S `result`, not the echo: the echo is where Claude
    # READ the follow-up, which is mid-answer, and A's remainder streams after
    # it. Splitting at the echo is the defect, not the fix.
    before = agent._segments_from_rows(rows[:5])
    assert breaks[0]["segments"] == len(before)
    assert breaks[0]["text"] == len("Reply A, first half. Reply A, second half.")


def test_the_offsets_slice_the_poll_payload_exactly(agent, run_dir):
    """End to end, and this is the assertion the page's rendering rests on:
    `text[:seam]` is the answer to the first message and `text[seam:]` is the
    answer to the follow-up, with no byte of either in the other."""
    _write(run_dir, [
        _user_row("first question"),
        _text_row("Reply A. "),
        _user_row("second question"),   # read mid-answer; A keeps going
        _text_row("Still reply A. "),
        _result_row("Reply A. Still reply A. "),
        _text_row("Reply B."),
    ])
    _setup(agent, run_dir)
    poll = agent._poll("run")
    assert len(poll["turn_breaks"]) == 1
    at = poll["turn_breaks"][0]
    assert poll["text"][:at["text"]] == "Reply A. Still reply A. "
    assert poll["text"][at["text"]:] == "Reply B."
    # The same split on the authoritative list.
    first = poll["segments"][:at["segments"]]
    second = poll["segments"][at["segments"]:]
    assert "".join(s["text"] for s in first if s["kind"] == "text") == \
        "Reply A. Still reply A. "
    assert "".join(s["text"] for s in second if s["kind"] == "text") == \
        "Reply B."


def test_two_absorbed_follow_ups_report_two_seams(agent):
    """Both echoes land inside reply A, so the next two `result` rows are both
    seams: A closes, B answers the first follow-up, C answers the second."""
    rows = [
        _user_row("q1"), _text_row("A."),
        _user_row("q2"), _user_row("q3"),
        _result_row("A."),
        _text_row("B."), _result_row("B."),
        _text_row("C."),
    ]
    breaks = agent._absorbed_turn_breaks(rows)
    assert [b["text"] for b in breaks] == [len("A."), len("A.B.")]
    assert [b["segments"] for b in breaks] == [1, 2], (
        "the merge is broken at each reply boundary, so a seam never falls "
        "inside a segment")


def test_a_reply_still_streaming_reports_nothing_yet(agent):
    """A follow-up is outstanding but no reply has ENDED, so the whole payload
    is still the one reply — reporting a seam here is what put the remainder
    under the follow-up's bubble."""
    rows = [
        _user_row("q1"), _text_row("Reply A, first half. "),
        _user_row("q2"), _text_row("Reply A, second half."),
    ]
    assert agent._absorbed_turn_breaks(rows) == []


def test_a_genuinely_new_turn_is_not_a_seam(agent):
    """A `result` closed the turn before this echo, so `_read_current_turn` is
    about to advance the cursor past it: it is a new turn, not a fold-in, and
    reporting it would make the page open a bubble for a span the very next poll
    no longer contains."""
    rows = [
        _user_row("q1"), _text_row("A."), _result_row("A."),
        _user_row("q2"), _text_row("B."),
    ]
    assert agent._absorbed_turn_breaks(rows) == []


def test_a_subagents_own_result_does_not_close_the_turn(agent):
    """The same exclusion `_read_current_turn`'s cursor makes: a subagent's
    `result` is not the main turn's, so an echo after one is still a fold-in."""
    rows = [
        _user_row("q1"), _text_row("A."),
        {"type": "result", "parent_tool_use_id": "t1", "result": "sub"},
        _user_row("q2"), _text_row("Still A."), _result_row("A. Still A."),
        _text_row("B."),
    ]
    assert len(agent._absorbed_turn_breaks(rows)) == 1


def test_a_pending_echo_on_an_IDLE_run_still_blanks_the_payload(agent, run_dir):
    """The one case the blanking exists for: the send was made between turns, so
    the window's rows are a reply that ALREADY ENDED and the page has already
    settled. Re-emitting them would paint a duplicate of the previous answer
    into a fresh bubble as the answer to the message just sent — and the seam
    offsets would index into a payload that is not the one they were measured
    against."""
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                     _result_row("Turn one answer.")])
    _setup(agent, run_dir)
    assert agent._send("run", "second message", "") == {"sent": True}
    poll = agent._poll("run")
    assert poll["text"] == ""
    assert poll["segments"] == []
    assert poll["turn_breaks"] == []


def test_a_pending_echo_MID_REPLY_keeps_streaming_the_reply_in_flight(
        agent, run_dir):
    """R2-1. The send landed while reply A was still streaming, so the window is
    A's own rows and A is what the page is watching grow. Blanking it froze the
    transcript for the whole send→echo window and then dumped A's remainder and
    the whole of B in one burst on the poll where the echo landed."""
    _write(run_dir, [_user_row("first question"),
                     _text_row("Reply A, first half. ")])
    _setup(agent, run_dir)
    assert agent._send("run", "second question", "") == {"sent": True}
    assert (run_dir / "pending_echo").exists(), "the gate is up"

    poll = agent._poll("run")
    assert poll["done"] is False, "the gate still keeps `done` honest"
    assert poll["text"] == "Reply A, first half. ", (
        "the reply in flight keeps flowing through the follow-up window")
    assert [s["text"] for s in poll["segments"] if s["kind"] == "text"] == [
        "Reply A, first half. "]
    assert poll["turn_breaks"] == [], "nothing has ended, so there is no seam"


def test_the_reply_in_flight_grows_across_polls_inside_the_window(
        agent, run_dir):
    """The streaming half of R2-1: two polls, both still before the echo, and the
    second carries MORE of the same reply — which is what the page's typer
    needs to animate instead of a single late burst."""
    _write(run_dir, [_user_row("q1"), _text_row("Reply A, ")])
    _setup(agent, run_dir)
    agent._send("run", "q2", "")
    first = agent._poll("run")

    _write(run_dir, [_user_row("q1"), _text_row("Reply A, "),
                     _text_row("still going.")])
    second = agent._poll("run")
    assert first["text"] == "Reply A, "
    assert second["text"] == "Reply A, still going."
    assert second["done"] is False
    assert (run_dir / "pending_echo").exists(), (
        "no echo has landed, so the gate is still up — the payload flowing is "
        "not the gate coming down")


def test_the_echo_landing_hands_back_both_replies_with_the_seam(agent, run_dir):
    """The end of the window: the echo is in the file, so the gate comes down,
    A's `result` closes it, and the payload is two replies with the seam between
    them — the page's slot 0 keeps A's bubble and slot 1 opens B's."""
    _write(run_dir, [_user_row("q1"), _text_row("Reply A. ")])
    _setup(agent, run_dir)
    agent._send("run", "q2", "")
    agent._poll("run")

    _write(run_dir, [
        _user_row("q1"), _text_row("Reply A. "),
        _user_row("q2"),                        # the echo, mid-reply
        _text_row("Still A."), _result_row("Reply A. Still A."),
        _text_row("Reply B."),
    ])
    poll = agent._poll("run")
    assert not (run_dir / "pending_echo").exists(), "the echo retired the gate"
    assert poll["text"] == "Reply A. Still A.Reply B."
    assert [b["text"] for b in poll["turn_breaks"]] == [
        len("Reply A. Still A.")]


# ------------------------------------- the echo gate and the stop (#12)

def test_a_landed_interrupt_retires_the_pending_echo(agent, run_dir):
    """Without this the run never reports `done` again: the CLI dropped the
    queued message so the echo never comes, and `interrupt` deliberately leaves
    the host alive so liveness never ends the poll either."""
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                     _result_row("Turn one answer.")])
    _setup(agent, run_dir)
    assert agent._send("run", "queued message", "") == {"sent": True}
    assert (run_dir / "pending_echo").exists()
    # The poll is hung on the echo gate, exactly as reported.
    assert agent._poll("run")["done"] is False

    # The stop button's road: a host that answers the interrupt, and names the
    # message it dropped.
    agent._host_alive = lambda _run_dir: True
    agent._write_control_request = lambda _d, _kind, **kw: "req-1"
    agent._await_control_response = lambda _d, _rid, start_offset=0: {
        "still_queued": ["queued message"]}

    result = agent._cancel("run")
    assert result["still_queued"] == ["queued message"]
    assert not (run_dir / "pending_echo").exists(), (
        "a landed interrupt is the one event that proves the echo will never "
        "arrive — leaving the gate up hangs `done` for the life of the session")

    poll = agent._poll("run")
    assert poll["done"] is True, "the turn can end again"
    assert poll["cancelled"] is True, "and it reads as a stop, not a crash"


def test_an_interrupt_that_never_answers_leaves_the_gate_to_the_tree_kill(
        agent, run_dir):
    """No control response inside the timeout falls through to ending the whole
    process tree, and a dead process is `_poll`'s own hard stop — so the gate
    does not have to be touched on that road, and is not."""
    _write(run_dir, [_user_row("turn one"), _text_row("A."), _result_row("A.")])
    _setup(agent, run_dir)
    agent._send("run", "queued message", "")
    (run_dir / "pid").write_text("4242", encoding="utf-8")

    agent._host_alive = lambda _run_dir: True
    agent._write_control_request = lambda _d, _kind, **kw: "req-1"
    agent._await_control_response = lambda _d, _rid, start_offset=0: None
    killed = []
    agent.os.killpg = lambda pid, sig: killed.append(pid)

    agent._cancel("run")
    assert killed, "no answer inside the timeout means end the whole tree"
    agent._alive = lambda _run_dir: False
    assert agent._poll("run")["done"] is True


# --------------------------------- stop means stop, queue included (R2-12)

def test_the_undrained_inbox_is_discarded_by_a_stop(agent, run_dir):
    """R2-12. `interrupt` only reaches the CLI's OWN queue. A follow-up the
    session host has not shipped to stdin yet is not in it — it sat in the
    inbox through the interrupt (which leaves the host alive by design) and was
    delivered on the very next drain tick, opening a brand-new turn out of a
    Stop: "Claude still answers msg2"."""
    _write(run_dir, [_user_row("q1"), _text_row("A.")])
    _setup(agent, run_dir)
    agent._send("run", "msg1", "")
    agent._send("run", "msg2", "")
    inbox = run_dir / "inbox"
    assert len(list(inbox.glob("*.json"))) == 2

    agent._host_alive = lambda _run_dir: True
    agent._write_control_request = lambda _d, _kind, **kw: "req-1"
    agent._await_control_response = lambda _d, _rid, start_offset=0: {}

    result = agent._cancel("run")
    assert list(inbox.glob("*.json")) == [], (
        "nothing the host has not drained yet may reach the CLI after a stop")
    assert result["still_queued"] == ["msg1", "msg2"], (
        "the CLI never saw them, so it cannot name them — they still have to "
        "come back to the composer (R1 #11)")


def test_a_stop_with_anything_queued_ends_the_session(agent, run_dir):
    """The CLI reports what it dropped in `still_queued` and was observed
    ANSWERING it anyway, and there is no control request that clears its queue.
    Legacy T does nothing about this (its `stopRun` only pastes the text back),
    so the session is ended whenever the queue was non-empty."""
    _write(run_dir, [_user_row("q1"), _text_row("A.")])
    _setup(agent, run_dir)
    (run_dir / "pid").write_text("4242", encoding="utf-8")
    agent._host_alive = lambda _run_dir: True
    agent._write_control_request = lambda _d, _kind, **kw: "req-1"
    agent._await_control_response = lambda _d, _rid, start_offset=0: {
        "still_queued": ["msg2"]}
    killed = []
    agent.os.killpg = lambda pid, sig: killed.append(pid)

    assert agent._cancel("run")["still_queued"] == ["msg2"]
    assert killed == [4242], (
        "a live host would keep answering past the stop")


def test_a_stop_with_an_EMPTY_queue_leaves_the_host_alive(agent, run_dir):
    """The ordinary Stop, and the whole reason `interrupt` is tried first: the
    session survives, background tasks with it, and the next message continues
    it instead of resuming a dead one."""
    _write(run_dir, [_user_row("q1"), _text_row("A.")])
    _setup(agent, run_dir)
    (run_dir / "pid").write_text("4242", encoding="utf-8")
    agent._host_alive = lambda _run_dir: True
    agent._write_control_request = lambda _d, _kind, **kw: "req-1"
    agent._await_control_response = lambda _d, _rid, start_offset=0: {
        "still_queued": []}
    killed = []
    agent.os.killpg = lambda pid, sig: killed.append(pid)

    assert agent._cancel("run") == {"cancelled": "run", "still_queued": []}
    assert killed == []


def test_discarding_the_inbox_never_eats_a_control_request(agent, run_dir):
    """`_write_control_request` writes into the SAME directory — a blanket
    unlink would eat the very `interrupt` row the caller is about to queue, and
    any `set_model`/`set_permission_mode` still waiting behind it."""
    _setup(agent, run_dir)
    agent._write_control_request(str(run_dir), "set_model", model="opus")
    agent._write_inbox_entry(str(run_dir), "a follow-up")
    assert agent._discard_inbox(str(run_dir)) == ["a follow-up"]
    left = [json.loads((run_dir / "inbox" / n.name).read_text())
            for n in sorted((run_dir / "inbox").glob("*.json"))]
    assert [r["type"] for r in left] == ["control_request"]
