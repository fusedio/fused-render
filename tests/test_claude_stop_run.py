"""Stopping a turn in the chat template.

`agent.py` has always been able to kill a run (`action="cancel"` → SIGTERM to
the process group, plus a release of every parked approval), but nothing in the
chat window ever called it: a turn that went off the rails could only be waited
out or escaped by navigating away, which orphans the subprocess. The chat now
offers a stop button on the working line and binds Escape to the same thing.

Two contracts are worth pinning here, and neither is visible from a test of the
approval bridge:

* the **page reaches the backend's cancel** — the button and the key both send
  the one action `main()` dispatches, with the live run's id;
* **a stopped run does not read as a crash.** Killing claude leaves the run dead
  with no `result` row, which `_poll` reports as an error *by design* (a
  truncated reply must never render as a clean success). When the user asked for
  the stop, that error is the expected outcome, not news — so the page suppresses
  it and says "stopped" instead, while keeping whatever text had streamed.

The end-of-run decision is one pure function in the page (`runEnding`), extracted
and executed under node rather than asserted about as source: what matters is the
text the user ends up reading.

This file was parametrised over the two chat templates while a plain chat and a
split chat both existed. The plain one is deleted — one chat for both kinds of
target — so it now runs against `claude` alone.
"""
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

# One chat template now. This used to be parametrised over the plain chat and
# the split chat because the stop was duplicated in a fork and D146 wants a rule
# in two implementations pinned by a test rather than a comment; the plain one is
# deleted, so there is one implementation and the parametrisation collapses to a
# single value. Kept as a list, and `template`/`_html` kept parametrised on it,
# because that is the seam a second chat surface would re-enter through — and
# because collapsing it to a bare constant would mean rewriting every test
# signature for no behavioural gain.
TEMPLATES = ["claude"]


def _dir(template):
    return os.path.join("fused_render", "templates", template)


def _load(template, name):
    path = os.path.join(_dir(template), name + ".py")
    spec = importlib.util.spec_from_file_location(f"{template}_stop_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(params=TEMPLATES)
def template(request):
    return request.param


@pytest.fixture
def agent(template):
    return _load(template, "agent")


def _html(template="claude"):
    return open(os.path.join(_dir(template), "template.html"), encoding="utf-8").read()


# ------------------------------------------------------- page reaches cancel

def test_the_page_sends_the_cancel_action_the_backend_dispatches(agent, template):
    """D146-shaped: the action name lives in two places (the page's call and
    `main()`'s dispatch), so a test holds them together rather than a comment."""
    html = _html(template)
    assert 'action: "cancel"' in html, "the page never calls the backend's cancel"
    # ...and the name it sends is one `main()` actually routes, rather than
    # falling through to "unknown action" — which would fail silently as a
    # stop button that does nothing.
    assert agent.main(action="cancel", run_id="no-such-run") == {"cancelled": "no-such-run"}
    assert "unknown action" in str(agent.main(action="stop"))


def test_stopping_is_offered_as_a_button(template):
    """The click target is discoverable and calls stopRun(). Escape no longer
    stops a live run (see escapeAction), so the button must not teach a key
    that does not do anything."""
    html = _html(template)
    assert "function stopRun(" in html
    assert 'id="stopbtn"' in html, "no stop control on the page"
    assert "stop · esc" not in html, "the button still teaches a key that does not stop the run"


# ------------------------------------------- the backend contract runEnding sits on

def test_a_killed_run_polls_as_done_with_an_error(agent, tmp_path, monkeypatch):
    """The shape the page's stop path has to absorb. A run killed mid-turn is
    dead with no `result` row, and `_poll` reports that as an error on purpose
    — this test pins that it still does, because `runEnding` below is written
    to swallow exactly this error and nothing else."""
    run_dir = tmp_path / "run"
    os.makedirs(run_dir)
    (run_dir / "out.jsonl").write_text("")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path))
    monkeypatch.setattr(agent, "_alive", lambda _d: False)

    out = agent._poll("run")
    assert out["done"] is True
    assert out["error"], "a killed run must not poll as a clean success"
    assert out["cancelled"] is False, "nothing asked for this end"


def test_a_cancel_leaves_the_run_saying_its_end_was_asked_for(agent, tmp_path,
                                                              monkeypatch):
    """The durable half. `_cancel` writes the marker BEFORE the kill, and every
    later reader — this page's poll, a chat reopened tomorrow — reads the stop
    off the run instead of having to have been told by whoever pressed it."""
    run_dir = tmp_path / "run"
    os.makedirs(run_dir)
    (run_dir / "out.jsonl").write_text("")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path))
    monkeypatch.setattr(agent, "_alive", lambda _d: False)

    agent._cancel("run")  # no pid file: the kill cannot land, the intent stands
    assert (run_dir / "cancelled").exists()
    assert agent._poll("run")["cancelled"] is True


def test_a_successful_interrupt_retires_the_marker_once_a_later_turn_proves_it_stale(
        agent, tmp_path, monkeypatch):
    """B2 regression: `cancelled` used to be written unconditionally and never
    cleared, so an INTERRUPTED turn — the whole point of which is that the
    session survives — left every later poll of that session reporting
    `cancelled: true` forever. Turn 3's normal end then read as "the turn
    finished before the stop landed", and a genuine turn-3 error was
    swallowed into a silent "Stopped." instead of being shown.

    D696 keeps the marker in place the instant the interrupt lands (the CLI's
    own error `result` for the interrupted turn hasn't been written yet, and
    removing the marker synchronously would make that error read as a bare
    crash to anyone but the tab that pressed Stop — see
    `test_claude_stop_marker_retirement.py`). The invariant this test used to
    pin by asserting immediate deletion is instead pinned here through the
    retirement mechanism: a poll landing before any later turn is PROVEN to
    have started must still read `cancelled: true`, and one landing after a
    later turn's own boundary has been proven must read `cancelled: false`
    with the marker gone."""
    run_dir = tmp_path / "run"
    os.makedirs(run_dir)
    monkeypatch.setattr(agent, "RUNS", str(tmp_path))
    monkeypatch.setattr(agent, "_write_control_request",
                        lambda *a, **k: "req-1")
    monkeypatch.setattr(agent, "_await_control_response",
                        lambda *a, **k: {"still_queued": []})
    (run_dir / "host.json").write_text(json.dumps({"pid": os.getpid()}))

    # Turn 1 has already streamed something by the time Stop is pressed —
    # `_cancel` captures `out.jsonl`'s current size as `interrupted_offset`.
    streamed = json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "partial"}]}}) + "\n"
    (run_dir / "out.jsonl").write_text(streamed)

    result = agent._cancel("run")
    assert result == {"cancelled": "run", "still_queued": []}
    assert (run_dir / "cancelled").exists()
    assert (run_dir / "interrupted_offset").read_text().strip() == \
        str(len(streamed.encode("utf-8")))

    # The interrupted turn's own error `result` has landed, but nothing PROVEN
    # to be a later turn has started yet — still reads as cancelled, not a
    # crash (the complementary half D696 also fixed: see
    # test_claude_stop_marker_retirement.py::
    # test_a_stop_does_not_read_as_a_crash_to_a_second_viewer for the
    # equivalent case driven against a real stub CLI).
    error_result = json.dumps({"type": "result", "is_error": True,
                                "result": "claude exited with an error"}) + "\n"
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(error_result)
    poll = agent._poll("run")
    assert poll["cancelled"] is True, \
        "the interrupted turn's own error result must still read as cancelled"
    assert (run_dir / "cancelled").exists()

    # Turn 2 starts (the CLI's `--replay-user-messages` echo) and closes
    # cleanly — a PROVEN new turn boundary past `interrupted_offset`.
    echo = json.dumps({"type": "user", "message": {"role": "user",
        "content": [{"type": "text", "text": "second message"}]}}) + "\n"
    turn2_result = json.dumps({"type": "result",
                                "result": "ok second message"}) + "\n"
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(echo)
        fh.write(turn2_result)

    # The cursor `_poll` hands `_cancelled_marker_state` is the offset the
    # scan STARTED from, not the boundary it just found — that boundary is
    # only persisted for the NEXT call to read (see `_read_current_turn`'s
    # `advance_to`/`cursor` split). So this first poll still returns the
    # pre-turn-2 cursor and reads cancelled; only the poll after it, reading
    # the persisted advance, proves turn 2 and retires the marker.
    poll = agent._poll("run")
    assert poll["cancelled"] is True

    poll = agent._poll("run")
    assert poll["cancelled"] is False, \
        "turn 2 genuinely completed — the marker from the STOPPED turn 1 " \
        "must not keep painting turn 2's own clean reply as cancelled"
    assert not (run_dir / "cancelled").exists(), \
        "an interrupt that landed must not leave the session's every " \
        "later turn reading as pre-emptively stopped"
    assert not (run_dir / "interrupted_offset").exists()


def test_a_reopened_chat_is_told_the_last_turn_was_stopped(agent, tmp_path,
                                                          monkeypatch):
    """The transcript cannot say why a reply stops mid-thought — a killed run
    just stops writing — so a chat reopened after a stop showed a half-finished
    answer and nothing else. `_history` reads the newest run's cancel marker and
    marks the turn, which is what puts the ⏹ note back on restore."""
    target = tmp_path / "proj"
    target.mkdir()
    runs = tmp_path / "runs"
    runs.mkdir()
    projects = tmp_path / "projects"
    monkeypatch.setattr(agent, "RUNS", str(runs))
    monkeypatch.setattr(agent, "PROJECTS", str(projects))

    session = "11111111-2222-3333-4444-555555555555"
    proj_dir = projects / agent._munge(str(target))
    proj_dir.mkdir(parents=True)
    (proj_dir / f"{session}.jsonl").write_text("\n".join([
        json.dumps({"message": {"role": "user",
                                "content": [{"type": "text", "text": "build it"}]}}),
        json.dumps({"message": {"role": "assistant",
                                "content": [{"type": "text", "text": "I was hal"}]}}),
    ]) + "\n")

    run_dir = runs / "20260821-120000-abc123"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target)}))
    (run_dir / "session").write_text(session)

    # Before the stop: nothing to say, and nothing said.
    assert "stopped" not in agent._history(str(target), session)["turns"][-1]

    (run_dir / "cancelled").write_text("1")
    turns = agent._history(str(target), session)["turns"]
    assert turns[-1]["stopped"] is True
    assert turns[-1]["role"] == "assistant"

    # A LATER run that completed answers for the bottom of the chat instead:
    # yesterday's stop is not what the reader is looking at.
    later = runs / "20260821-130000-def456"
    later.mkdir()
    (later / "meta.json").write_text(json.dumps({"file": str(target)}))
    (later / "session").write_text(session)
    assert "stopped" not in agent._history(str(target), session)["turns"][-1]


# ------------------------------------------------------------- runEnding, in node

def _run_ending(data, stopped, template="claude"):
    """Run the page's real `runEnding` over one terminal poll payload.

    The node guard lives HERE, at the shell-out itself, rather than in a fixture
    keyed on the test's name: the name-prefix version missed
    `test_the_two_chats_decide_endings_identically` — which is the one test the
    duplicated-rule decision (D146) rests on — and raised FileNotFoundError
    instead of skipping wherever node is absent. Sited on the subprocess call, no
    later test can drift out of the guard's reach. Same siting as
    test_claude_shots.py's and test_claude_app_state.py's `_node`.
    """
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own end-of-run decision")
    html = _html(template)
    start = html.index("function runEnding(")
    # up to the next top-level definition — `runEnding` is deliberately pure, so
    # nothing below it (which touches `document` and `fused`) may come along.
    fn = html[start:html.index("\nasync function stopRun(", start)]
    script = fn + "\nconsole.log(JSON.stringify(runEnding(%s, %s)));" % (
        json.dumps(data), json.dumps(stopped))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_ending_of_an_unstopped_failure_is_still_an_error(template):
    end = _run_ending({"done": True, "error": "claude exited with an error", "text": ""},
                      False, template)
    assert end["error"] == "claude exited with an error"
    assert not end["note"]
    assert end["keepText"] is False


def test_the_ending_of_a_stopped_run_is_a_note_not_an_error(template):
    """The kill IS the error; reporting it would blame the user's own click."""
    end = _run_ending(
        {"done": True, "error": "claude exited before completing the reply",
         "text": "I was hal"}, True, template)
    assert not end["error"], end
    assert "stopped" in end["note"].lower()
    # the partial reply is real work and stays on screen
    assert end["keepText"] is True


def test_a_stop_this_page_did_not_press_is_still_a_stop(template):
    """The queue card's ✕ (schedule.py -> agent._cancel) kills the run without
    telling this page, so `stopped` is False here and the kill's error used to
    be reported as a crash — to the person who asked for the stop. The RUN's own
    cancel marker rides back on the poll (`cancelled`) and decides it instead."""
    end = _run_ending(
        {"done": True, "error": "claude exited before completing the reply",
         "text": "I was hal", "cancelled": True}, False, template)
    assert not end["error"], end
    assert "stopped" in end["note"].lower()
    assert end["keepText"] is True


def test_a_cancelled_flag_on_a_clean_run_still_says_the_stop_missed(template):
    """The marker is written before the kill, so a reply that completed in
    between comes back cancelled AND clean — the same race the button already
    has, and it must read the same way."""
    end = _run_ending({"done": True, "error": "", "text": "all done",
                       "cancelled": True}, False, template)
    assert not end["error"]
    assert end["note"], "a stop that did not land must not be silent"
    assert end["keepText"] is True


def test_the_ending_of_a_clean_run_says_nothing(template):
    end = _run_ending({"done": True, "error": "", "text": "all done"}, False, template)
    assert not end["error"] and not end["note"]
    assert end["keepText"] is True


def test_the_ending_of_a_run_that_beat_the_stop_says_so(template):
    """Clicking stop a beat after the last poll: the reply is whole, so it is
    shown whole — but the user is told why nothing was cut off, rather than
    being left wondering whether the button works."""
    end = _run_ending({"done": True, "error": "", "text": "all done"}, True, template)
    assert not end["error"]
    assert end["note"], "a stop that did not land must not be silent"
    assert end["keepText"] is True


# ---------------------------------------------------------- stopAllowed, in node

def _stop_allowed(run_id, seat, stopped_seat, template="claude"):
    """Run the page's real `stopAllowed` over one (run_id, seat, stoppedSeat)
    triple, the same node-extraction pattern `_run_ending` uses above."""
    if not shutil.which("node"):
        pytest.skip("node is needed to run the page's own stop-allowed decision")
    html = _html(template)
    start = html.index("function stopAllowed(")
    fn = html[start:html.index("\n\n", start)]
    script = fn + "\nconsole.log(JSON.stringify(stopAllowed(%s, %s, %s)));" % (
        json.dumps(run_id), json.dumps(seat), json.dumps(stopped_seat))
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_stop_already_asked_for_on_this_turn_is_not_repeated(template):
    assert _stop_allowed("run-1", 3, 3, template) is False


def test_a_new_turns_seat_is_not_blocked_by_an_earlier_turns_stop(template):
    """B3 regression: `activeRun`'s run_id now spans a whole multi-turn
    session behind a session host, so a guard keyed on run_id alone (rather
    than the seat pollLoop hands out fresh each turn) went dead for every
    turn after the first one Stop was ever pressed on. Pressing Stop again on
    a LATER turn of the same run must still go through."""
    assert _stop_allowed("run-1", 4, 3, template) is True


def test_stop_is_never_allowed_with_no_run_or_no_seat(template):
    assert _stop_allowed("", 1, 0, template) is False
    assert _stop_allowed("run-1", 0, 0, template) is False


# `test_the_two_chats_decide_endings_identically` lived here: it ran `runEnding`
# from both chat templates over the same four terminal payloads and demanded byte
# equality, which is what made the duplicated implementation safe. There is no
# second copy to compare against now, so the test is gone rather than reduced to
# comparing a function with itself. Every case it covered is still covered above,
# against the one surviving implementation.
