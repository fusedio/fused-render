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
import subprocess

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


def _record_tree_kills(agent, monkeypatch):
    """Record the pids `_kill_tree` ends, on whichever road THIS platform takes.

    `_kill_tree` is the same platform split `_cancel`'s tail always had: POSIX
    signals the process group (`start_new_session=True` makes the pid the pgid),
    while Windows has neither `os.killpg` nor a console for CTRL_BREAK to reach
    (the run is spawned DETACHED_PROCESS) and shells out to `taskkill /T /F` to
    walk the tree instead.

    A test that patches `os.killpg` alone therefore records nothing on Windows
    AND lets a real `taskkill /F` loose on whatever process happens to own that
    pid on the runner — which is how the R2-12 stops went red on
    `test-python-windows` while passing everywhere else. Patching the road the
    platform actually takes keeps the assertion honest on both, and the
    `/T`/`/F` check pins the tree-walking flags the Windows road depends on.

    Returns a list of pids, appended to in call order.
    """
    killed = []
    if os.name == "nt":
        def _run(cmd, **kwargs):
            assert cmd[:2] == ["taskkill", "/PID"], cmd
            assert "/T" in cmd, "must walk the tree, not just the named pid"
            assert "/F" in cmd, "the CLI does not answer a polite close"
            killed.append(int(cmd[2]))
            return subprocess.CompletedProcess(cmd, 0)
        monkeypatch.setattr(agent.subprocess, "run", _run)
    else:
        monkeypatch.setattr(agent.os, "killpg",
                            lambda pid, _sig: killed.append(pid))
    return killed


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


def test_a_genuinely_new_turn_IS_a_seam(agent):
    """A `result` closed the turn before this echo, so `_read_current_turn` is
    about to advance the cursor past it — and it is reported ANYWAY.

    This used to report nothing, on the reasoning that the cursor moves and the
    NEXT payload is the newer reply alone, so the page's shrink test would sort
    it out one lap later. That is only true when the shrink is VISIBLE: two
    one-segment replies leave `len(segments)` at 1 across the step, so nothing
    shrank, no seam was ever reported, and the newer reply was never placed at
    all — it showed up only on reload, which reads the same rows through
    `_history` and splits on exactly this boundary. Verified live: a mid-stream
    follow-up drained after the first reply's `result` had its answer missing
    from the transcript until the page was reloaded.

    A span the next poll no longer contains is not a problem for the page — it
    drops one for every fold-in the cursor carries out of the window already
    (`turn_breaks` shrinking is its own signal)."""
    rows = [
        _user_row("q1"), _text_row("A."), _result_row("A."),
        _user_row("q2"), _text_row("B."),
    ]
    breaks = agent._absorbed_turn_breaks(rows)
    segs = agent._segments_from_rows(rows)
    assert breaks == [{"segments": 1, "text": len("A.")}]
    assert [seg["text"] for seg in segs] == ["A.", "B."], (
        "and the seam falls between the segments, never inside one")


def test_a_wake_continued_turn_reports_its_seam_too(agent):
    """Bugbot, PR #1061, reopened by PR #1119. A D415 wake appends more rows of
    the SAME displayed turn after that turn's `result` — a `notice` divider and
    its continuation — so when a genuinely new turn then opens there is no
    `result` adjacent to the echo.

    This reported NOTHING, because a seam is only safe at a segment edge and
    `_segments_from_rows` broke at a `result` and nowhere else: the wake's
    continuation consumed that break, the next turn's prose grew onto the
    continuation's own segment, and any offset would have filed the wake's text
    under the turn that had not started yet.

    Withholding it has its own cost, and it is the one a second window on a
    conversation kept paying: with no seam the page has nothing to place the
    new reply against, so it re-rendered the PREVIOUS reply into the new turn's
    bubble. `_segments_from_rows` breaks at a genuine new turn now — the merge
    that made the seam unsafe cannot happen — so the honest answer is the seam,
    and the wake's text stays where it belongs."""
    rows = [
        _user_row("q1"), _text_row("A."), _result_row("A."),
        {"type": "system", "subtype": "task_notification",
         "message": "a task finished"},
        _text_row("And the task is done."),
        _user_row("q2"), _text_row("B."),
    ]
    breaks = agent._absorbed_turn_breaks(rows)
    segs = agent._segments_from_rows(rows)
    texts = [sg["text"] for sg in segs if sg["kind"] == "text"]
    # The new reply is a segment of its own — never grown onto the wake's.
    assert texts == ["A.", "And the task is done.", "B."]
    assert len(breaks) == 1
    seam = breaks[0]
    # AND IT FALLS BETWEEN SEGMENTS, never inside one: everything up to it is
    # the previous displayed turn, wake continuation included, and everything
    # after it is the reply to `q2` alone.
    assert [sg["text"] for sg in segs[:seam["segments"]] if sg["kind"] == "text"] \
        == ["A.", "And the task is done."]
    assert [sg["text"] for sg in segs[seam["segments"]:] if sg["kind"] == "text"] \
        == ["B."]
    assert seam["text"] == len("A.") + len("And the task is done.")


def test_any_row_between_the_result_and_the_echo_still_leaves_a_seam(agent):
    """The shape a SECOND WINDOW on one conversation hits (PR #1119).

    Every send carries `model` and `permission_mode`, and `_send` queues a
    control request ahead of the message whenever either differs from what the
    host was spawned with — which a second window, with its own defaults, can
    easily make true. Whatever the CLI writes for that lands between the
    previous turn's `result` and this send's echo, and while the seam required
    the two to be ADJACENT, a single row in between was enough to withhold it —
    leaving the page to render the previous reply into the new turn's bubble.

    Nothing about a row in between makes the boundary less real, and nothing
    about it makes the cut less safe: `_segments_from_rows` breaks at the echo
    itself now, so the edge is there either way."""
    rows = [
        _user_row("q1"), _text_row("A."), _result_row("A."),
        {"type": "system", "subtype": "init", "session_id": "s"},
        _user_row("q2"), _text_row("B."),
    ]
    breaks = agent._absorbed_turn_breaks(rows)
    segs = agent._segments_from_rows(rows)
    assert breaks == [{"segments": 1, "text": len("A.")}]
    assert [sg["text"] for sg in segs if sg["kind"] == "text"] == ["A.", "B."], (
        "and the seam still falls between the segments, never inside one")


def test_a_wake_with_nothing_after_it_is_not_a_seam(agent):
    """A D415 wake is a `result` followed by more rows of the SAME displayed
    turn and NO user echo — `_segments_from_rows` joins it with a `notice`
    divider — so it must not be split into two bubbles. The genuine-boundary
    rule above cannot reach it: only an echo triggers that branch, and an echo
    is a new message by definition."""
    rows = [
        _user_row("q1"), _text_row("A."), _result_row("A."),
        {"type": "system", "subtype": "task_notification",
         "message": "a task finished"},
        _text_row("And the task is done."),
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
        agent, run_dir, monkeypatch):
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
    killed = _record_tree_kills(agent, monkeypatch)

    agent._cancel("run")
    assert killed == [4242], (
        "no answer inside the timeout means end the whole tree")
    agent._alive = lambda _run_dir: False
    assert agent._poll("run")["done"] is True


def test_an_interrupt_that_never_answers_still_hands_the_queued_text_back(
        agent, run_dir, monkeypatch):
    """The timeout road must not EAT the message.

    `_discard_inbox` deletes the undrained entries — that is the point, so a
    follow-up cannot be delivered right after the interrupt and open a fresh
    turn out of a Stop — which leaves the list it returns as the only surviving
    copy of text the user typed. It was returned on one road only (the
    interrupt that answered), so a host that did not answer inside the timeout
    lost the message outright: gone from disk, never seen by the CLI, never
    handed back to the composer. Strictly worse than not discarding at all."""
    _write(run_dir, [_user_row("turn one"), _text_row("A."), _result_row("A.")])
    _setup(agent, run_dir)
    agent._send("run", "queued message", "")
    (run_dir / "pid").write_text("4242", encoding="utf-8")

    agent._host_alive = lambda _run_dir: True
    agent._write_control_request = lambda _d, _kind, **kw: "req-1"
    agent._await_control_response = lambda _d, _rid, start_offset=0: None
    _record_tree_kills(agent, monkeypatch)

    assert agent._cancel("run")["still_queued"] == ["queued message"], (
        "the only copy of the user's text is the one _discard_inbox returned")


def test_a_stop_with_no_live_host_hands_the_queued_text_back_too(
        agent, run_dir, monkeypatch):
    """And the road where no host was ever found: the entries used to be left
    on disk forever — never reported, and deliverable by a future session as
    if they had just been typed."""
    _write(run_dir, [_user_row("turn one"), _text_row("A."), _result_row("A.")])
    _setup(agent, run_dir)
    agent._send("run", "orphaned message", "")
    (run_dir / "pid").write_text("4242", encoding="utf-8")

    agent._host_alive = lambda _run_dir: False
    _record_tree_kills(agent, monkeypatch)

    assert agent._cancel("run")["still_queued"] == ["orphaned message"]
    assert not any((run_dir / "inbox").glob("*.json")), (
        "and the inbox is emptied on this road too, not orphaned on disk")


def test_a_respawn_cancel_leaves_the_inbox_alone(agent, run_dir, monkeypatch):
    """`_send`'s respawn calls in with `interrupt_first=False` and re-sends the
    message itself — its caller reads `run_id`, never `still_queued` — so a
    discard there would delete text nobody is listening for a hand-back of."""
    _write(run_dir, [_user_row("turn one"), _text_row("A."), _result_row("A.")])
    _setup(agent, run_dir)
    agent._send("run", "still pending", "")
    (run_dir / "pid").write_text("4242", encoding="utf-8")
    _record_tree_kills(agent, monkeypatch)

    assert agent._cancel("run", interrupt_first=False)["still_queued"] == []
    assert any((run_dir / "inbox").glob("*.json")), (
        "the respawn road is not a stop; the entry stays where it is")


# --------------------------------- the seam offsets index the SAME segmentation

def _finalized_text_row(body):
    """An assistant reply with no stream deltas — an older CLI, or one run
    without `--include-partial-messages`. `_segments_from_rows`'s `streamed`
    gate is FALSE for a window of only these and TRUE the moment one delta
    lands anywhere in it, which is the whole hazard below."""
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": body}]}}


def _thinking_row(body):
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "thinking", "thinking": body}]}}


def _thinking_delta_row(body):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "thinking_delta", "thinking": body}}}


@pytest.mark.parametrize("rows", [
    # (a) reply A finalized-text-only, reply B streaming: `streamed` is False
    # for the prefix and True for the window, so the prefix segmentation was a
    # DIFFERENT document — A claimed all of B and B rendered empty.
    [_user_row("q1"), _finalized_text_row("Reply A."),
     _user_row("q2"), _result_row("Reply A."), _text_row("Reply B.")],
    # (b) the same asymmetry on the thinking gate.
    [_user_row("q1"), _text_row("Reply A."), _thinking_row("hmm"),
     _user_row("q2"), _result_row("Reply A."),
     _thinking_delta_row("later"), _text_row("Reply B.")],
    # (c) the ordinary all-deltas shape, where both gates are uniformly true —
    # the shape every other seam test in this file feeds, and the reason the
    # instability was invisible.
    [_user_row("q1"), _text_row("Reply A."),
     _user_row("q2"), _result_row("Reply A."), _text_row("Reply B.")],
])
def test_every_seam_indexes_a_prefix_of_the_payloads_own_segmentation(agent, rows):
    """THE INVARIANT THE OFFSETS DEPEND ON.

    `_absorbed_turn_breaks` measures each seam by segmenting `rows[:i+1]` and
    counting; `_poll` hands the page the segmentation of the WHOLE window. The
    offsets only mean anything if the first is a strict prefix of the second —
    and it was not, because `_segments_from_rows` re-derived its
    `streamed`/`thinking_streamed` gates from whatever list it was given."""
    full = agent._segments_from_rows(rows)
    breaks = agent._absorbed_turn_breaks(rows)
    assert breaks, "this window carries an absorbed follow-up"
    for br in breaks:
        n = br["segments"]
        assert 0 <= n <= len(full), "the offset indexes into the payload"
        # THE INVARIANT: the seam's `text` offset is the join of the text
        # segments the payload itself puts before that index. Measured against
        # the payload's own segmentation, which is the only one the page has.
        assert br["text"] == sum(
            len(seg.get("text") or "") for seg in full[:n]
            if seg.get("kind") == "text"), (
            "the seam offsets index a different segmentation from the one the "
            "poll payload carries")
    # AND THE LAST SPAN IS THE FOLLOW-UP'S OWN REPLY, not an empty slice. The
    # broken prefix segmentation put the seam PAST reply B's only segment, so
    # the reply before it claimed all of B and B's own bubble rendered empty.
    tail = full[breaks[-1]["segments"]:]
    assert "".join(seg.get("text") or "" for seg in tail
                   if seg.get("kind") == "text").strip() == "Reply B.", (
        "everything after the last seam is the answer to the follow-up")


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


def test_a_stop_with_anything_queued_ends_the_session(
        agent, run_dir, monkeypatch):
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
    killed = _record_tree_kills(agent, monkeypatch)

    assert agent._cancel("run")["still_queued"] == ["msg2"]
    assert killed == [4242], (
        "a live host would keep answering past the stop")


def test_a_stop_with_an_EMPTY_queue_leaves_the_host_alive(
        agent, run_dir, monkeypatch):
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
    killed = _record_tree_kills(agent, monkeypatch)

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
