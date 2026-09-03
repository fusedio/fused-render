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
