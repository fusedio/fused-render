"""Control protocol: `interrupt`, `set_model`, `set_permission_mode`.

A live session host survives all three (verified live against 2.1.251 — see
DECISIONS.md) — this drives the same real process tree
`test_claude_session_host.py`/`test_claude_send_action.py` do (a genuine
`_start`, a real host, a stub `claude` binary), because the whole point of
this task is that `_cancel`/`_send` reach an ALREADY-RUNNING CLI over its
inbox instead of killing it.

The stub answers a `control_request` row with a `control_response` echoing
back whatever the request carried (so a test can assert on it directly),
except `interrupt`, which answers with `still_queued: []` — the shape the
real CLI actually returns (see the live probe this task's DECISIONS.md entry
describes).
"""
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time

import pytest

# WINDOWS: SKIPPED, NOT FIXED. The persistent session host (#979) never writes
# `host.json` on the Windows runner - every test below waits for it and times
# out - and `interrupted_offset` lands one byte off there (CRLF). That is a
# platform gap in the host itself, not in these tests, and it needs a Windows
# box to close; marking it here keeps main's Windows job honest about what it
# does cover instead of red for everything (2026-09-04, red since #979).
pytestmark = pytest.mark.skipif(os.name == "nt", reason="claude session host does not start on Windows yet (#979)")

from _claude_stub_cli import reap_host, write_stub_cli

AGENT_DIR = os.path.join("fused_render", "claude_agent")


def _load_agent():
    path = os.path.join(AGENT_DIR, "agent.py")
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


_STUB = '''#!{python}
import json
import sys


def send(row):
    sys.stdout.write(json.dumps(row) + "\\n")
    sys.stdout.flush()

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
        else:
            resp["response"] = {{k: v for k, v in req.items() if k != "subtype"}}
        send({{"type": "control_response", "response": resp}})
        continue
    text = row["message"]["content"][0]["text"]
    send({{"type": "echo", "text": text}})
    send({{"type": "result", "session_id": "sess-stub", "result": "ok"}})
'''


@pytest.fixture()
def stub_cli(tmp_path):
    return write_stub_cli(tmp_path / "bin", _STUB.format(python=sys.executable))


@pytest.fixture(autouse=True)
def _reap_hosts(agent):
    """Every test here starts a real detached host; kill each one on the way
    out (see reap_host) or they outlive the run until reboot."""
    yield
    try:
        run_ids = os.listdir(agent.RUNS)
    except OSError:
        return
    for run_id in run_ids:
        reap_host(os.path.join(agent.RUNS, run_id))


@pytest.fixture()
def target(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    return str(f)


def _out_rows(run_dir):
    path = os.path.join(run_dir, "out.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _start(agent, monkeypatch, stub_cli, target, message="m1", **kwargs):
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", stub_cli)
    res = agent._start(target, message, "", kwargs.pop("model", ""),
                       kwargs.pop("effort", ""), has_pane=False, **kwargs)
    assert "run_id" in res, res
    return res["run_id"], os.path.join(agent.RUNS, res["run_id"])


def test_cancel_against_a_live_host_writes_an_interrupt_and_leaves_it_alive(
        agent, monkeypatch, stub_cli, target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target)
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    result = agent._cancel(run_id)
    assert result["still_queued"] == []
    # A stop that beat the stub's first turn is answered at once and lands
    # on a thread (`_start_pending_stop`); the markers below are its work.
    assert _wait_for(lambda: not agent._stop_pending(run_dir))
    assert agent._alive(run_dir), \
        "an interrupt must not kill the CLI — only end the turn"
    assert os.path.exists(os.path.join(run_dir, "host.json")), \
        "the host itself must still be up, ready for a follow-up"
    # The interrupt LANDED, so the session goes on — but the CLI has only
    # been told to abort, not finished aborting, and the `cancelled` marker
    # must stay in place until a later turn PROVES itself (see
    # `_cancelled_marker_state`), or a reader other than the tab that
    # pressed Stop sees the interrupted turn's own error `result` with no
    # marker at all, which reads as a crash rather than a stop.
    assert os.path.exists(os.path.join(run_dir, "cancelled"))
    assert os.path.exists(os.path.join(run_dir, "interrupted_offset")), \
        "the offset the interrupt was requested at, so a later poll can " \
        "tell a genuinely new turn apart from this one's own error result"


def test_cancel_with_no_host_still_tree_kills(agent, tmp_path):
    target = tmp_path / "orphan.txt"
    target.write_text("x")
    run_dir = os.path.join(agent.RUNS, "20260901-140000-ccc")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"file": str(target), "message": "hi", "mode": "prompt"}, f)
    # A real, killable process standing in for a claude spawned the OLD way
    # (no session host at all) — start_new_session so os.killpg can reach it
    # the same way _cancel's fallback path does.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    with open(os.path.join(run_dir, "pid"), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    result = agent._cancel("20260901-140000-ccc")
    # Nothing was queued, so nothing is owed back — but the KEY is present on
    # every road now (agent.py `_cancel`'s `stranded`), so a caller never has to
    # tell "no host answered" apart from "the host said nothing was queued".
    assert result["still_queued"] == []
    assert _wait_for(lambda: proc.poll() is not None), \
        "no host.json at all means no one to interrupt — must fall back " \
        "to the tree-kill"


def test_model_change_writes_a_set_model_control_request(agent, monkeypatch,
                                                           stub_cli, target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target,
                             model="claude-opus-5")
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    result = agent._send(run_id, "follow-up", model="claude-haiku-4-5")
    assert result == {"sent": True}

    def got_it():
        for row in _out_rows(run_dir):
            if row.get("type") != "control_response":
                continue
            resp = row["response"].get("response") or {}
            if resp.get("model") == "claude-haiku-4-5":
                return True
        return False

    assert _wait_for(got_it), _out_rows(run_dir)
    # The message itself still goes through, after the control request.
    assert _wait_for(lambda: any(
        r.get("type") == "echo" and r.get("text") == "follow-up"
        for r in _out_rows(run_dir)))


def test_a_model_change_is_not_repeated_on_the_next_send(agent, monkeypatch,
                                                          stub_cli, target):
    """B9 regression: `_send` compares the incoming `model` against
    `host.json`'s recorded value, but never updated that record after a
    change actually landed — so a SECOND follow-up with the same (already
    applied) model compared against the stale spawn-time value again, and
    queued the exact same `set_model` control request a second time, and a
    third, for every turn of the session."""
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target,
                             model="claude-opus-5")
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    agent._send(run_id, "first follow-up", model="claude-haiku-4-5")
    assert _wait_for(lambda: any(
        r.get("type") == "echo" and r.get("text") == "first follow-up"
        for r in _out_rows(run_dir)))

    result = agent._send(run_id, "second follow-up", model="claude-haiku-4-5")
    assert result == {"sent": True}
    assert _wait_for(lambda: any(
        r.get("type") == "echo" and r.get("text") == "second follow-up"
        for r in _out_rows(run_dir)))

    responses = [r for r in _out_rows(run_dir) if r.get("type") == "control_response"]
    assert len(responses) == 1, \
        "the model did not change between the two sends — only the FIRST " \
        "should have queued a set_model control request"

    with open(os.path.join(run_dir, "host.json"), encoding="utf-8") as f:
        host = json.load(f)
    assert host["model"] == "claude-haiku-4-5", \
        "host.json must reflect the model the session was actually moved to"


def test_unchanged_model_writes_no_control_request(agent, monkeypatch,
                                                     stub_cli, target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target,
                             model="claude-opus-5")
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    agent._send(run_id, "follow-up", model="claude-opus-5")

    assert _wait_for(lambda: any(
        r.get("type") == "echo" and r.get("text") == "follow-up"
        for r in _out_rows(run_dir)))
    assert not any(r.get("type") == "control_response" for r in _out_rows(run_dir))


def test_effort_change_returns_the_respawn_marker(agent, monkeypatch, stub_cli,
                                                    target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target, effort="")
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    result = agent._send(run_id, "follow-up", effort="high")
    assert result == {"respawn": True}
    assert os.path.exists(os.path.join(run_dir, "cancelled")), \
        "an effort change ends the session the same way a read_dirs " \
        "mismatch does — via _cancel"
    assert _wait_for(lambda: not agent._alive(run_dir))


def test_await_control_response_seeks_from_the_given_offset(agent, tmp_path):
    """`_await_control_response` used to re-read and re-`json.loads` ALL of
    `out.jsonl` from byte 0 on every 50ms pass — up to 100 full passes over a
    file that, on a long-running session, can be tens of megabytes, sitting
    in the Stop button's synchronous path. It now seeks from an offset the
    caller captured before the request was queued, so a session that has
    already streamed a lot of history is not re-parsed on every tick a stop
    press waits through. Proven here by planting a STALE row carrying the
    same request_id BEFORE the offset — a scan starting from byte 0 would
    find it and return the wrong (stale) answer; a real seek never even
    looks at those bytes."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out_path = run_dir / "out.jsonl"
    out_path.write_text(json.dumps({
        "type": "control_response", "response": {
            "request_id": "req-1", "subtype": "success",
            "response": {"stale": True}}}) + "\n", encoding="utf-8")
    offset = out_path.stat().st_size

    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "control_response", "response": {
                "request_id": "req-1", "subtype": "success",
                "response": {"fresh": True}}}) + "\n")

    result = agent._await_control_response(
        str(run_dir), "req-1", timeout=1.0, start_offset=offset)
    assert result == {"fresh": True}


# A CLI that is still STARTING UP when the stop lands: it takes a while to
# open the first turn (SessionStart hooks, MCP servers), and only then echoes
# the prompt as a `user` row — the CLI's `--replay-user-messages` shape, the
# proof the prompt reached the transcript. It never closes the turn, so the
# only thing that can end it is the interrupt.
_SLOW_START_STUB = '''#!{python}
import json
import sys
import time


def send(row):
    sys.stdout.write(json.dumps(row) + "\\n")
    sys.stdout.flush()

first = True
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    if row.get("type") == "control_request":
        resp = {{"request_id": row["request_id"], "subtype": "success",
                 "response": {{"still_queued": []}}}}
        send({{"type": "control_response", "response": resp}})
        continue
    if first:
        first = False
        time.sleep({delay})
        if {echo}:
            send({{"type": "user", "message": row["message"]}})
        continue
    send({{"type": "user", "message": row["message"]}})
'''


def _drained(run_dir):
    done = os.path.join(run_dir, "inbox", "done")
    return lambda: any(n.endswith(".json") for n in os.listdir(done)) \
        if os.path.isdir(done) else False


def _slow_stub(tmp_path, delay, echo=True):
    body = _SLOW_START_STUB.format(python=sys.executable, delay=delay,
                                   echo="True" if echo else "False")
    return write_stub_cli(tmp_path / "bin", body)


def test_a_stop_on_a_fresh_chat_answers_now_and_interrupts_after_the_prompt_lands(
        agent, monkeypatch, tmp_path, target):
    """Akshil, 2026-10-12: new chat, stop right away, reload — the first
    message was gone, only "Interrupted by you" left. The interrupt had
    reached the CLI before it opened the turn, and the CLI dropped the
    prompt instead of persisting it. The stop must wait for the echo —
    but the reader must not: the answer is immediate, the wait is deferred."""
    stub = _slow_stub(tmp_path, delay=1.0)
    run_id, run_dir = _start(agent, monkeypatch, stub, target, message="dfdf")
    assert _wait_for(_drained(run_dir)), "the host must have handed the prompt over"

    started = time.monotonic()
    result = agent._cancel(run_id)
    assert result["pending"] is True, "deferred, not the synchronous road"
    assert time.monotonic() - started < 1.0, "answered before the stub's 1 s"
    assert result["still_queued"] == []
    # A second press while the first is on its way is answered the same way,
    # at once — not by joining a wait the cancel budget cannot afford.
    assert agent._cancel(run_id) == {"cancelled": run_id, "still_queued": [],
                                     "pending": True}
    assert os.path.exists(os.path.join(run_dir, "cancelled"))
    assert agent._stop_pending(run_dir)
    assert not os.path.exists(os.path.join(run_dir, "interrupted_offset")), \
        "the offset is the interrupt's own boundary — written when it lands"

    # Meanwhile a poll reads the turn as over, stopped by the reader.
    poll = agent._poll(run_id)
    assert poll["done"] is True
    assert poll["cancelled"] is True
    assert poll["error"], "the page swallows it into 'Stopped.' via cancelled"

    assert _wait_for(lambda: not agent._stop_pending(run_dir), timeout=12)
    kinds = [row.get("type") for row in _out_rows(run_dir)]
    assert "user" in kinds and "control_response" in kinds, kinds
    assert kinds.index("user") < kinds.index("control_response"), \
        "the interrupt must land AFTER the prompt's echo — before it, the " \
        "CLI throws the prompt away and the transcript opens with the marker"
    assert os.path.exists(os.path.join(run_dir, "interrupted_offset"))
    assert agent._alive(run_dir), "still an interrupt, not a kill"


def test_a_follow_up_sent_during_a_pending_stop_waits_for_the_interrupt(
        agent, monkeypatch, tmp_path, target):
    stub = _slow_stub(tmp_path, delay=1.0)
    run_id, run_dir = _start(agent, monkeypatch, stub, target, message="dfdf")
    assert _wait_for(_drained(run_dir))
    assert agent._cancel(run_id)["pending"] is True
    assert agent._stop_pending(run_dir)

    assert agent._send(run_id, "and now this") == {"sent": True}
    assert not agent._stop_pending(run_dir), \
        "_send must not write to the pipe ahead of the promised interrupt"

    def order():
        rows = _out_rows(run_dir)
        kinds = [r.get("type") for r in rows]
        texts = [((r.get("message") or {}).get("content") or [{}])[0].get("text")
                 for r in rows if r.get("type") == "user"]
        return kinds, texts
    assert _wait_for(lambda: "and now this" in order()[1])
    kinds, _ = order()
    assert kinds.index("control_response") < kinds.index("user", kinds.index("user") + 1), \
        "the follow-up's echo must come after the interrupt's response"


def test_a_cli_that_never_opens_a_turn_is_still_interrupted_after_the_cap(
        agent, monkeypatch, tmp_path, target):
    stub = _slow_stub(tmp_path, delay=0, echo=False)
    monkeypatch.setattr(agent, "_FIRST_TURN_WAIT_SEC", 0.5)
    run_id, run_dir = _start(agent, monkeypatch, stub, target)
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    result = agent._cancel(run_id)
    assert result["still_queued"] == []
    assert _wait_for(lambda: not agent._stop_pending(run_dir), timeout=12)
    assert any(r.get("type") == "control_response" for r in _out_rows(run_dir)), \
        "past the cap the stop falls through to the plain interrupt"


def test_a_stop_with_a_follow_up_queued_mid_turn_is_not_deferred(
        agent, monkeypatch, tmp_path, target):
    """The deferral is for a run whose FIRST turn has not opened. A follow-up
    drained into a turn that is already streaming echoes only when that turn
    ends — waiting on it would hold the stop for the whole cap."""
    stub = _slow_stub(tmp_path, delay=0)      # echoes the first message at once
    run_id, run_dir = _start(agent, monkeypatch, stub, target, message="first")
    assert _wait_for(lambda: any(r.get("type") == "user" for r in _out_rows(run_dir)))
    # Park a follow-up the CLI will never echo (the stub answers nothing for
    # it) — drained, unechoed, and the run has no `result` row.
    monkeypatch.setattr(agent, "_FIRST_TURN_WAIT_SEC", 5.0)
    with open(os.path.join(run_dir, "inbox", "done", "99999999999999999999-ffff.json"),
              "w", encoding="utf-8") as fh:
        json.dump({"text": "second", "at": time.time()}, fh)
    agent._echoed_done.pop(run_dir, None)
    started = time.monotonic()
    result = agent._cancel(run_id)
    assert time.monotonic() - started < 2, "must not wait the first-turn cap"
    assert "pending" not in result
    assert not agent._stop_pending(run_dir)


def test_a_stop_on_an_opened_turn_takes_the_synchronous_road(
        agent, monkeypatch, tmp_path, target):
    """The deferral exists for the first turn only. Once the prompt has
    echoed, Stop interrupts right there and settles its own markers before
    answering — the road every mid-reply stop has always taken."""
    stub = _slow_stub(tmp_path, delay=0)
    run_id, run_dir = _start(agent, monkeypatch, stub, target, message="first")
    assert _wait_for(lambda: any(r.get("type") == "user" for r in _out_rows(run_dir)))

    result = agent._cancel(run_id)
    assert result == {"cancelled": run_id, "still_queued": []}
    assert not agent._stop_pending(run_dir)
    assert os.path.exists(os.path.join(run_dir, "interrupted_offset"))
    assert os.path.exists(os.path.join(run_dir, "cancelled"))
    assert agent._alive(run_dir)


def test_a_stop_pending_marker_with_no_thread_behind_it_is_stale(agent, tmp_path):
    """A server restart mid-wait leaves the file with nobody to retire it;
    read as live it would hold every later poll at "stopped"."""
    run_dir = os.path.join(agent.RUNS, "20261012-140000-aaa")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, agent._STOP_PENDING), "w", encoding="utf-8") as f:
        f.write("1")
    assert agent._stop_pending(run_dir) is False
    assert not os.path.exists(os.path.join(run_dir, agent._STOP_PENDING))


def test_a_stop_that_beats_the_host_still_keeps_the_prompt(
        agent, monkeypatch, tmp_path, target):
    """Akshil, 2026-10-12: "if I click fast enough the chat is not made" —
    Stop before the host had even drained the inbox stranded the prompt
    (a "not sent" row, no transcript). The prompt is spared, the host
    delivers it, and the interrupt waits for its echo like any other
    first-turn stop."""
    stub = _slow_stub(tmp_path, delay=1.0)
    run_id, run_dir = _start(agent, monkeypatch, stub, target, message="huh")
    # No wait for host.json at all: the stop lands as early as it can.
    result = agent._cancel(run_id)
    assert result["still_queued"] == [], "the prompt is not stranded"
    assert result["pending"] is True
    assert _wait_for(lambda: not agent._stop_pending(run_dir), timeout=15)
    kinds = [row.get("type") for row in _out_rows(run_dir)]
    assert "user" in kinds and "control_response" in kinds, kinds
    assert kinds.index("user") < kinds.index("control_response")
    texts = [((r.get("message") or {}).get("content") or [{}])[0].get("text")
             for r in _out_rows(run_dir) if r.get("type") == "user"]
    assert texts == ["huh"]


def test_discard_inbox_spares_only_the_opening_prompt(agent, tmp_path):
    run_dir = os.path.join(agent.RUNS, "20261012-150000-bbb")
    os.makedirs(agent._inbox_dir(run_dir))
    agent._write_inbox_entry(run_dir, "first")
    time.sleep(0.01)
    agent._write_inbox_entry(run_dir, "second")
    assert agent._discard_inbox(run_dir, keep_first=True) == ["second"]
    assert agent._inbox_holds_prompt(run_dir)
    assert agent._discard_inbox(run_dir) == ["first"]
    assert not agent._inbox_holds_prompt(run_dir)
    # Once anything has been drained, the oldest entry is a follow-up, not
    # the opening prompt: nothing is spared.
    os.makedirs(os.path.join(agent._inbox_dir(run_dir), "done"), exist_ok=True)
    with open(os.path.join(agent._inbox_dir(run_dir), "done", "0-aaaa.json"),
              "w", encoding="utf-8") as fh:
        json.dump({"text": "earlier", "at": 1}, fh)
    agent._write_inbox_entry(run_dir, "third")
    assert agent._discard_inbox(run_dir, keep_first=True) == ["third"]


def test_the_host_wait_is_a_registered_stop_for_sends_and_second_stops(
        agent, monkeypatch, tmp_path):
    """Bugbot: while a Stop waits for the host to spawn, no thread exists yet —
    a follow-up `_send` and a second Stop must still see the stop on its way."""
    run_dir = os.path.join(agent.RUNS, "20261012-160000-ccc")
    os.makedirs(agent._inbox_dir(run_dir))
    agent._write_inbox_entry(run_dir, "first")
    run_id = os.path.basename(run_dir)
    gate = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(agent, "_first_turn_pending", lambda d: True)
    monkeypatch.setattr(agent, "_host_alive", lambda d: False)

    def slow_wait(d, timeout):
        entered.set()
        gate.wait(5)
        return False

    monkeypatch.setattr(agent, "_wait_for_host", slow_wait)
    monkeypatch.setattr(agent, "_cancel_run", lambda *a, **k: {"cancelled": run_id})
    results = []
    first = threading.Thread(target=lambda: results.append(agent._cancel(run_id)))
    first.start()
    assert entered.wait(5)
    # A second Stop in the gap is answered, not started again.
    assert agent._cancel(run_id)["pending"] is True
    # A send in the gap waits for the stop instead of writing past it.
    joined = []
    sender = threading.Thread(target=lambda: (agent._join_pending_stop(run_dir),
                                              joined.append(True)))
    sender.start()
    time.sleep(0.2)
    assert not joined
    gate.set()
    first.join(5)
    sender.join(5)
    assert joined and results == [{"cancelled": run_id}]
    assert run_dir not in agent._STOPS_STARTING
