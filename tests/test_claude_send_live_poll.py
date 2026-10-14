"""A `_send` into an already-live session must not be reported `done` by the
very first poll that follows it.

`sendMessage` (template.html) takes the `live_host` -> `action:"send"` branch
for a follow-up typed while a turn is still streaming, then immediately calls
`pollLoop(run_id)`. At that instant `out.jsonl` still ends with the PREVIOUS
turn's `result` row — the session host has not drained the inbox yet (up to
`_DRAIN_INTERVAL_SECONDS`), and even once it has, the CLI has not echoed the
follow-up back yet either. `_poll`'s `idle = t == "result"` reads that stale
tail as "this turn is over" and `done = idle or not alive` believes it: the
page renders the PREVIOUS turn's reply as the answer to the new message and
stops polling, so the real reply never arrives.

`_send` now leaves a marker (`run_dir/pending_echo`) that `_poll` will not
report `done` for on `idle` grounds until it has actually seen the follow-up's
own `--replay-user-messages` echo (`_starts_new_turn`) — a dead process still
ends the poll regardless, so a crash mid-follow-up cannot hang the page
forever waiting for an echo that will never come.
"""
import importlib.util
import json
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


def _load(name):
    path = os.path.join(AGENT_DIR, name + ".py")
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
    body = "".join(json.dumps(r) + "\n" for r in rows)
    (run_dir / "out.jsonl").write_text(body, encoding="utf-8")


def _result_row(text, session="s"):
    return {"type": "result", "session_id": session, "result": text}


def _user_row(text):
    return {"type": "user", "message": {"role": "user",
            "content": [{"type": "text", "text": text}]}}


def _text_row(chunk):
    return {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": chunk}}}


def _setup(agent, run_dir, alive=True):
    agent.RUNS = str(run_dir.parent)
    agent._alive = lambda _run_dir: alive
    agent._pid_alive = lambda _pid: alive
    with open(run_dir / "host.json", "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242, "session_id": "s", "file": "", "mode": "",
                   "model": "", "effort": "", "read_dirs": []}, fh)


def test_a_send_into_a_live_session_is_not_believed_done_before_its_echo(
        agent, run_dir):
    # Turn one already finished; this is exactly what out.jsonl looks like the
    # instant a follow-up is sent into a session that just replied.
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                      _result_row("Turn one answer.")])
    _setup(agent, run_dir)

    sent = agent._send("run", "second message", "")
    assert sent == {"sent": True}

    # The host has not drained the inbox yet, let alone gotten an echo back
    # from the CLI — out.jsonl is untouched since the send.
    first_poll = agent._poll("run")
    assert first_poll["done"] is False, (
        "a send into a live session must not be believed done before its "
        "own echo lands — otherwise the previous turn's reply renders as "
        "the answer to the new message and the real reply never arrives")
    # `done` being false is not enough on its own: `rows` (from
    # `_read_current_turn`) is still windowed off the OLD cursor while the
    # echo is pending, so `text`/`segments` built off those rows are still
    # turn one's own content. A page's pollLoop paints whatever a non-done
    # poll hands back into a fresh bubble regardless of `done` — so leaving
    # these non-empty would paint turn one's reply as the answer to the new
    # message, then leave it on screen until the new turn's own tokens
    # started arriving.
    assert first_poll["text"] == "", (
        "a poll with a pending echo must not hand back the PREVIOUS turn's "
        "text — a page renders it into a fresh bubble regardless of `done`")
    assert first_poll["segments"] == [], (
        "same as `text` above — a pending echo must blank `segments` too")

    # Once the follow-up's own echo lands (and, later, its result), `done`
    # must resolve normally again.
    (run_dir / "out.jsonl").open("a", encoding="utf-8").write(
        json.dumps(_user_row("second message")) + "\n")
    second_poll = agent._poll("run")
    assert second_poll["done"] is False, "the new turn is still streaming"

    (run_dir / "out.jsonl").open("a", encoding="utf-8").write(
        "".join(json.dumps(r) + "\n" for r in [
            _text_row("Turn two answer."), _result_row("Turn two answer.")]))
    third_poll = agent._poll("run")
    assert third_poll["done"] is True
    assert third_poll["text"] == "Turn two answer."


def test_a_dead_process_still_ends_the_poll_even_with_a_pending_echo(
        agent, run_dir):
    """A crash between `_send` writing the inbox entry and the CLI ever
    reading it must not hang the page waiting for an echo that will never
    come — liveness is still the hard stop."""
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                      _result_row("Turn one answer.")])
    _setup(agent, run_dir, alive=True)

    sent = agent._send("run", "second message", "")
    assert sent == {"sent": True}

    agent._alive = lambda _run_dir: False
    poll = agent._poll("run")
    assert poll["done"] is True


def _system_row(subtype):
    return {"type": "system", "subtype": subtype, "session_id": "s"}


def test_the_clis_turn_preamble_does_not_lift_the_echo_blank(agent, run_dir):
    """Claude Code 2.1.29x writes `system/init` and `system/status` the moment
    it picks a follow-up off stdin — BEFORE the `--replay-user-messages` echo.
    Read as "the run speaking", they flipped `idle` off and lifted the
    `pending_echo` blank, so every poll until the cursor stepped past the echo
    handed the page the PREVIOUS turn's reply, in full and with no seam: the
    previous answer flashed into the new bubble (Akshil, 2026-10-14)."""
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                     _result_row("Turn one answer.")])
    _setup(agent, run_dir)
    assert agent._send("run", "second message", "") == {"sent": True}
    with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_system_row("hook_started")) + "\n")
        fh.write(json.dumps(_system_row("init")) + "\n")
        fh.write(json.dumps(_system_row("status")) + "\n")
    poll = agent._poll("run")
    assert poll["done"] is False
    assert poll["text"] == "" and poll["segments"] == [], (
        "`init`/`status` are the CLI picking the message up, not a reply — "
        "the previous turn must stay blanked until the echo lands")
    # The echo: the window still opens on turn one for this one poll (the
    # cursor steps on the NEXT read), so the seam is what tells the page where
    # its own turn begins.
    with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_user_row("second message")) + "\n")
    poll = agent._poll("run")
    assert poll["done"] is False
    assert poll["text"] == "Turn one answer."
    assert poll["turn_breaks"] == [{"segments": 1, "text": len("Turn one answer.")}]
    with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_text_row("Turn two answer.")) + "\n")
    poll = agent._poll("run")
    assert poll["text"] == "Turn two answer." and poll["turn_breaks"] == []


def test_the_preamble_still_reopens_the_turn_when_no_send_is_pending(
        agent, run_dir):
    """The preamble exemption is scoped to the `pending_echo` window. With no
    send outstanding, `init` after a `result` is the run waking up (a D415
    wake whose hooks fire first, a slow SessionStart hook) and the turn must
    read open at once — otherwise `done` is True across the hook window."""
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                     _result_row("Turn one answer."),
                     _system_row("hook_started"), _system_row("init")])
    _setup(agent, run_dir)
    assert not (run_dir / "pending_echo").exists()
    assert agent._poll("run")["done"] is False


def test_an_echo_landing_between_the_rows_read_and_the_tail_scan_keeps_the_turn_open(
        agent, run_dir, monkeypatch):
    """`_poll` reads the window first and scans the `pending_echo` tail after.
    If the echo lands in between, the window ends on the preamble alone
    (which the gate told `idle` to ignore) while the tail already has the
    echo — so the gate lifts, and without the tail re-opening the turn the
    poll reported `done` with the previous reply as the new turn's text."""
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                     _result_row("Turn one answer.")])
    _setup(agent, run_dir)
    assert agent._send("run", "second message", "") == {"sent": True}
    with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_system_row("init")) + "\n")
        fh.write(json.dumps(_system_row("status")) + "\n")
    real = agent._read_current_turn

    def read_then_echo_lands(*args, **kwargs):
        out = real(*args, **kwargs)
        with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(_user_row("second message")) + "\n")
        return out
    monkeypatch.setattr(agent, "_read_current_turn", read_then_echo_lands)
    poll = agent._poll("run")
    assert poll["done"] is False
    # Still BLANK: the window this payload was built from has no echo and no
    # seam, so handing it over would paint the previous reply (Bugbot). The
    # marker stays for the next poll, whose window carries the echo.
    assert poll["text"] == "" and poll["segments"] == [] and poll["turn_breaks"] == []
    assert (run_dir / "pending_echo").exists()
    monkeypatch.setattr(agent, "_read_current_turn", real)
    poll = agent._poll("run")
    assert poll["done"] is False
    assert poll["text"] == "Turn one answer."
    assert poll["turn_breaks"] == [{"segments": 1, "text": len("Turn one answer.")}]
    assert not (run_dir / "pending_echo").exists()


def test_a_whole_turn_landing_in_the_tail_is_not_finalised_from_the_old_window(
        agent, run_dir, monkeypatch):
    """Same race, but the tail also holds the new turn's text and `result`:
    reporting `done` off a window that still holds the PREVIOUS reply would
    finalise the wrong text (CodeRabbit, PR #1650). Pending now; the next
    poll's window has the whole new turn and ends it properly."""
    _write(run_dir, [_user_row("turn one"), _text_row("Turn one answer."),
                     _result_row("Turn one answer.")])
    _setup(agent, run_dir)
    assert agent._send("run", "second message", "") == {"sent": True}
    with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_system_row("init")) + "\n")
    real = agent._read_current_turn

    def read_then_turn_lands(*args, **kwargs):
        out = real(*args, **kwargs)
        with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
            for r in (_user_row("second message"), _text_row("Turn two answer."),
                      _result_row("Turn two answer.")):
                fh.write(json.dumps(r) + "\n")
        return out
    monkeypatch.setattr(agent, "_read_current_turn", read_then_turn_lands)
    poll = agent._poll("run")
    assert poll["done"] is False and poll["text"] == ""
    monkeypatch.setattr(agent, "_read_current_turn", real)
    poll = agent._poll("run")
    assert poll["done"] is True
    assert poll["text"] == "Turn one answer.Turn two answer."
    assert poll["turn_breaks"] == [{"segments": 1, "text": len("Turn one answer.")}]


def test_a_repeated_message_landing_in_the_tail_is_not_matched_to_its_older_echo(
        agent, run_dir, monkeypatch):
    """Same race, same words twice: the window already holds turn one's echo
    of "again", shaped exactly like the one the tail just found. Matching by
    value lifted the gate off the OLD echo and finalised turn one's reply as
    the answer (Bugbot/CodeRabbit, PR #1650); matching by file position does
    not."""
    _write(run_dir, [_user_row("again"), _text_row("Turn one answer."),
                     _result_row("Turn one answer.")])
    _setup(agent, run_dir)
    assert agent._send("run", "again", "") == {"sent": True}
    with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(_system_row("init")) + "\n")
    real = agent._read_current_turn

    def read_then_turn_lands(*args, **kwargs):
        out = real(*args, **kwargs)
        with (run_dir / "out.jsonl").open("a", encoding="utf-8") as fh:
            for r in (_user_row("again"), _text_row("Turn two answer."),
                      _result_row("Turn two answer.")):
                fh.write(json.dumps(r) + "\n")
        return out
    monkeypatch.setattr(agent, "_read_current_turn", read_then_turn_lands)
    poll = agent._poll("run")
    assert poll["done"] is False and poll["text"] == ""
    assert (run_dir / "pending_echo").exists()
    monkeypatch.setattr(agent, "_read_current_turn", real)
    poll = agent._poll("run")
    assert poll["done"] is True
    assert poll["text"] == "Turn one answer.Turn two answer."
