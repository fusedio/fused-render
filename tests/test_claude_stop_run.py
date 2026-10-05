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

The page half (the button, `runEnding`) was pinned here by node probes over the
retired iframe chat page; the native chat under frontend/src/apps/claude owns it
now. What stays is the backend half.

This file was parametrised over the two chat templates while a plain chat and a
split chat both existed. The plain one is deleted — one chat for both kinds of
target — so it now runs against `claude` alone.
"""
import importlib.util
import json
import os

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
    # The chat's backend left templates/ for the server package; the template
    # folder keeps only the registry marker and its gate.
    return os.path.join("fused_render", "claude_agent")


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


# ------------------------------------------------------- page reaches cancel


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


@pytest.mark.skipif(os.name == "nt", reason="claude session host does not start on Windows yet (#979)")
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


# ---------------------------------------------------------- stopAllowed, in node


# `test_the_two_chats_decide_endings_identically` lived here: it ran `runEnding`
# from both chat templates over the same four terminal payloads and demanded byte
# equality, which is what made the duplicated implementation safe. There is no
# second copy to compare against now, so the test is gone rather than reduced to
# comparing a function with itself. Every case it covered is still covered above,
# against the one surviving implementation.
