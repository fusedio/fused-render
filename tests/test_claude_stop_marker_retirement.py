"""B1: a stop pressed on a live host must not read as a crash to anyone who
did not press it, and must not go on reading as "Stopped" once a later turn
of the same session actually completes.

`_cancel`'s `interrupt` control request only tells the CLI to abort — its own
error `result` row for the interrupted turn lands AFTER the control response
comes back, on the CLI's own schedule. The old fix (D680) removed the
`cancelled` marker the instant the control response landed, to stop a stale
marker from poisoning every later turn — but that left a gap: any `_poll`
that ran between the marker's removal and the error `result` actually being
written saw `is_error: true` with `cancelled: false`, i.e. a crash. Only the
tab that pressed Stop knew to swallow that via its own `stoppedRun`
variable; a second viewer, or the tasks queue card, saw the crash text.

`_cancel` now leaves the marker in place and writes `interrupted_offset`
(the byte offset of `out.jsonl` at the moment the interrupt was requested)
instead. `_poll`/`_stopped_last` (via `_cancelled_marker_state`) only retire
the marker once a PROVEN new turn boundary has advanced past that offset —
so the interrupted turn's own error result still reads as `cancelled: true`
regardless of when it lands, and a real later turn still clears it.
"""
import importlib.util
import json
import os
import sys
import time

import pytest

from _claude_stub_cli import run_dir_report, write_stub_cli

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load_agent():
    path = os.path.join(TEMPLATE_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


# The stub never answers the first message with a `result` — it holds the
# turn open (as a real long-running turn would) until either an `interrupt`
# lands, or a second message arrives on the same turn. An `interrupt`
# answers immediately, then writes the interrupted turn's own error
# `result` only once MORE stdin arrives (a next message, or EOF at
# teardown) — reproducing the real gap: the error result lands strictly
# after the control response, on its own schedule.
_STUB = '''#!{python}
import json
import sys

def send(row):
    sys.stdout.write(json.dumps(row) + "\\n")
    sys.stdout.flush()

interrupted = False
answered_one = False
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    if row.get("type") == "control_request":
        req = row["request"]
        resp = {{"request_id": row["request_id"], "subtype": "success"}}
        if req["subtype"] == "interrupt":
            resp["response"] = {{"still_queued": []}}
            interrupted = True
        else:
            resp["response"] = {{k: v for k, v in req.items() if k != "subtype"}}
        send({{"type": "control_response", "response": resp}})
        continue
    text = row["message"]["content"][0]["text"]
    if interrupted:
        # The aborted turn's own error result, only written now — after the
        # control response already went out.
        send({{"type": "result", "session_id": "sess-stub",
               "is_error": True, "result": "claude exited with an error"}})
        interrupted = False
    # `--replay-user-messages` shape (`_starts_new_turn` requires exactly
    # this: type "user", message.content a list of text blocks) so `_poll`'s
    # pending-echo wait actually clears.
    send({{"type": "user", "message": {{"role": "user",
           "content": [{{"type": "text", "text": text}}]}}}})
    # The FIRST message's turn is held open — no `result` closes it, the way a
    # long-running turn behaves — so the stop under test lands on a turn that
    # is genuinely still in flight. Every later message answers normally.
    if answered_one:
        send({{"type": "result", "session_id": "sess-stub", "result": "ok " + text}})
    answered_one = True
'''


@pytest.fixture()
def stub_cli(tmp_path):
    return write_stub_cli(tmp_path / "bin", _STUB.format(python=sys.executable))


@pytest.fixture()
def target(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    return str(f)


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _start(agent, monkeypatch, stub_cli, target, message="m1"):
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", stub_cli)
    res = agent._start(target, message, "", "", "", has_pane=False)
    assert "run_id" in res, res
    return res["run_id"], os.path.join(agent.RUNS, res["run_id"])


def test_a_stop_does_not_read_as_a_crash_to_a_second_viewer(
        agent, monkeypatch, stub_cli, target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target)
    assert _wait_for(lambda: os.path.exists(
        os.path.join(run_dir, "host.json"))), run_dir_report(run_dir)

    result = agent._cancel(run_id)
    assert result["still_queued"] == []

    # The interrupted turn's own error result hasn't landed on the wire yet
    # (the stub only writes it once more stdin arrives) — a poll here must
    # still see `cancelled: true` even with no error text present yet.
    poll = agent._poll(run_id)
    assert poll["cancelled"] is True

    # Now nudge the stub with a follow-up so it writes the interrupted
    # turn's own error result, THEN its ordinary echo/result for the new
    # message — a poll landing while only the error result has appeared
    # must still read `cancelled: true`, not a bare crash.
    sent = agent._send(run_id, "second message", "")
    assert sent == {"sent": True}

    def error_landed():
        out = os.path.join(run_dir, "out.jsonl")
        try:
            with open(out, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            return False
        return '"is_error": true' in body

    assert _wait_for(error_landed)
    poll = agent._poll(run_id)
    assert poll["error"], "the interrupted turn's own error must still surface"
    assert poll["cancelled"] is True, (
        "a genuinely new turn has not been PROVEN yet (the follow-up's own "
        "echo/result may not have landed), so the error result on the wire "
        "is still the interrupted turn's own — reading it as a bare crash "
        "is exactly the bug this fix closes")


def test_a_later_completed_turn_clears_the_stale_stop_marker(
        agent, monkeypatch, stub_cli, target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target)
    assert _wait_for(lambda: os.path.exists(
        os.path.join(run_dir, "host.json"))), run_dir_report(run_dir)

    agent._cancel(run_id)
    poll = agent._poll(run_id)
    assert poll["cancelled"] is True

    sent = agent._send(run_id, "second message", "")
    assert sent == {"sent": True}

    def second_turn_done():
        p = agent._poll(run_id)
        return p["done"] and "ok second message" in p["text"]

    assert _wait_for(second_turn_done)
    final = agent._poll(run_id)
    assert final["cancelled"] is False, (
        "turn 2 genuinely completed — the marker from the STOPPED turn 1 "
        "must not keep painting turn 2's own clean reply as cancelled")
    assert not os.path.exists(os.path.join(run_dir, "cancelled")), \
        "the marker (and its offset) are retired once proven stale"
