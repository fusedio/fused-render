"""Specialist breakouts from a task (J, L, M, N, O, P, Z) and the router post-hook (`agent_hooks`, S signals).

The breakout session itself is not run: its close-out answer is stubbed (`breakouts.last_answer`)."""
import json
import os

import pytest

from _specialists_conftest import *  # noqa: F401,F403
from _specialists_conftest import make_specialist, needs_git

ANSWER = """<<<SUMMARY
Outcome: solved — reopen the canvas
Found: the sync loop waits on a stale lock.
Do next: reopen the canvas, then publish.
Files changed: None
SUMMARY>>>
<<<NOTE
kind: correction
basis: verified
claim: Retrying a stalled sync never helps; reopening does.
entry: m-3f2a
Tried retry three times; reopening fixed it at once.
NOTE>>>"""


@pytest.fixture
def parent(tmp_path, monkeypatch):
    from fused_render.specialists import breakouts

    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(breakouts, "_transcript_path", lambda file, sid: f"/transcripts/{sid}.jsonl")
    return {"session_id": "task-1", "run_id": "run1", "file": str(proj), "title": "Publish Q4 canvas"}


@pytest.fixture
def answer(monkeypatch):
    from fused_render.specialists import breakouts

    holder = {"text": ANSWER, "calls": []}

    def last(home, sid):
        holder["calls"].append((home, sid))
        return holder["text"]
    monkeypatch.setattr(breakouts, "last_answer", last)
    return holder


def _post(client, path, body=None):
    status, _, raw = client.post(path, body if body is not None else {})
    return status, json.loads(raw or b"null")


def _open(client, spec, parent, origin="offer", brief=None):
    return _post(client, f"/api/specialists/{spec.id}/breakouts", {
        "parent": parent, "origin": origin, "request_id": "req1" if origin == "offer" else "",
        "brief": brief or {"problem": "Sync stalls", "tried": "retry x3", "look_at": "canvas.toml"}})


def _events(client, sid="task-1"):
    return json.loads(client.get("/api/specialists/task-events?session_id=" + sid)[2])["events"]


def test_create_seeds_the_brief_and_counts(client, parent):
    from fused_render.specialists import breakouts, counts

    spec = make_specialist("Canvas", "Fused canvases")
    status, out = _open(client, spec, parent, brief={"problem": "Edited problem", "tried": "t", "look_at": "l"})
    assert status == 200, out
    bid = out["breakout_id"]
    assert out["home"] == spec.home and out["name"] == "Canvas"
    assert out["start_params"] == {"specialist": f"{spec.id}:breakout:{bid}"}
    seed = out["seed"]
    assert seed.startswith('Brief from the task "Publish Q4 canvas":\nProblem: Edited problem\nTried: t\nLook at: l')
    assert f"still running in {parent['file']}" in seed and "/transcripts/task-1.jsonl" in seed
    rec = breakouts.get(spec.id, bid)
    assert rec["status"] == "open" and rec["origin"] == "offer" and rec["request_id"] == "req1"
    assert counts.get(spec.id)["breakouts_opened"] == 1
    assert [e["kind"] for e in _events(client)] == ["breakout_open"]


def test_launch_accepts_the_breakout_record(client, parent):
    from fused_render.specialists import launch

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent, origin="hand")
    got = launch.spec(out["start_params"]["specialist"], spec.home)
    assert "error" not in got, got
    assert any(parent["file"] in r for r in got["allow"])


@pytest.mark.parametrize("body,status", [
    ({"parent": {"file": "/x"}, "origin": "hand", "brief": {"problem": "p"}}, 400),
    ({"parent": {"session_id": "s", "file": "/x"}, "origin": "magic", "brief": {"problem": "p"}}, 400),
    ({"parent": {"session_id": "s", "file": "/x"}, "origin": "hand", "brief": {"problem": ""}}, 400),
    ({"parent": {"session_id": "s", "file": "/x"}, "origin": "hand", "brief": {"problem": "x" * 1001}}, 400),
])
def test_create_validates(client, body, status):
    spec = make_specialist("Canvas", "Fused canvases")
    assert _post(client, f"/api/specialists/{spec.id}/breakouts", body)[0] == status
    assert _post(client, "/api/specialists/nope/breakouts", body)[0] == 404


def test_a_breakout_cannot_open_a_breakout(client):
    spec = make_specialist("Canvas", "Fused canvases")
    status, out = _post(client, f"/api/specialists/{spec.id}/breakouts", {
        "parent": {"session_id": "bs", "file": spec.home}, "origin": "hand", "brief": {"problem": "p"}})
    assert status == 400 and "cannot open a breakout" in out["error"]


@needs_git
def test_close_drafts_the_summary_and_files_the_note(client, parent, answer):
    from fused_render.specialists import breakouts, inbox

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    bid = out["breakout_id"]
    base = f"/api/specialists/{spec.id}/breakouts/{bid}"
    breakouts.set_session(spec.id, bid, "bs-1")
    status, prompt = _post(client, base + "/close-prompt")
    assert status == 200 and prompt["message"].startswith("The user is closing this breakout. Reply with exactly")
    status, closed = _post(client, base + "/closed", {"session_id": "bs-1"})
    assert status == 200, closed
    assert closed["draft"].startswith("Outcome: solved — reopen the canvas\nFound:")
    assert closed["draft"].splitlines()[-1].startswith("Full session: Canvas breakout: Sync stalls")
    assert closed["label"] == "Draft from the Canvas specialist · not sent yet"
    note = inbox.get(spec.id, closed["note_id"])
    assert note["kind"] == "correction" and note["basis"] == "verified" and note["entry"] == "m-3f2a"
    assert note["claim"] == "Retrying a stalled sync never helps; reopening does."
    assert "reopening fixed it" in note["evidence"]
    assert note["source"] == {"type": "breakout", "breakout_id": bid, "session_id": "bs-1",
                              "title": note["source"]["title"]}
    assert answer["calls"] == [(spec.home, "bs-1")]
    rec = breakouts.get(spec.id, bid)
    assert rec["status"] == "closed" and len(rec["closes"]) == 1
    # a retry of the same close (same answer) files nothing twice
    status, retry = _post(client, base + "/closed", {"session_id": "bs-1"})
    assert retry == closed
    assert len(breakouts.get(spec.id, bid)["closes"]) == 1
    assert [n["id"] for n in inbox.undigested(spec.id)].count(closed["note_id"]) == 1
    assert closed["note_id"].startswith("n-close-" + bid[2:])
    # Reopen continues the same session; a second close asks for a follow-up only.
    status, again = _post(client, base + "/reopen")
    assert again == {"session_id": "bs-1", "start_params": {"specialist": f"{spec.id}:breakout:{bid}"},
                     "home": spec.home}
    assert breakouts.get(spec.id, bid)["status"] == "open"
    assert _post(client, base + "/close-prompt")[1]["message"].startswith(
        "The user is closing this breakout again. Summarise only what happened since your last summary.")
    answer["text"] = "No blocks this time, just prose."
    status, second = _post(client, base + "/closed", {"session_id": "bs-1"})
    assert second["draft"].startswith("Outcome: not given\nFull session: ")
    note2 = inbox.get(spec.id, second["note_id"])
    assert note2["kind"] == "case" and note2["basis"] == "guess" and "just prose" in note2["evidence"]
    assert len(breakouts.get(spec.id, bid)["closes"]) == 2
    kinds = [e["kind"] for e in _events(client)]
    assert kinds.count("breakout_closed") == 2 and kinds[0] == "breakout_open"
    listed = json.loads(client.get("/api/specialists/breakouts?parent_session_id=task-1")[2])["breakouts"]
    assert [(b["id"], b["name"], b["status"]) for b in listed] == [(bid, "Canvas", "closed")]


def test_discard_keeps_the_session_and_drafts_nothing(client, parent):
    from fused_render.specialists import breakouts

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent, origin="hand")
    bid = out["breakout_id"]
    breakouts.set_session(spec.id, bid, "bs-2")
    assert _post(client, f"/api/specialists/{spec.id}/breakouts/{bid}/discard") == (200, {"ok": True})
    rec = breakouts.get(spec.id, bid)
    assert rec["status"] == "discarded" and rec["closes"] == [] and rec["discarded"]
    # the nightly job still finds the session
    assert breakouts.session_ids(spec.id) == {"bs-2": "discarded"}
    assert _events(client)[-1]["kind"] == "breakout_discarded"


def test_two_breakouts_from_one_task_with_two_specialists(client, parent):
    a = make_specialist("Canvas", "Fused canvases")
    b = make_specialist("Data", "datasets")
    _, one = _open(client, a, parent)
    _, two = _open(client, b, parent, origin="hand")
    listed = json.loads(client.get("/api/specialists/breakouts?parent_session_id=task-1")[2])["breakouts"]
    assert {(x["name"], x["status"]) for x in listed} == {("Canvas", "open"), ("Data", "open")}
    assert one["breakout_id"] != two["breakout_id"]


def test_project_queue_never_gates_a_home(client):
    from fused_render import project_queue

    spec = make_specialist("Canvas", "Fused canvases")
    assert project_queue.queue_key(spec.home) == ""


@needs_git
def test_deleted_specialist_with_an_open_breakout(client, parent):
    from fused_render.specialists import store

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    bid = out["breakout_id"]
    store.delete(spec.id)
    listed = json.loads(client.get("/api/specialists/breakouts?parent_session_id=task-1")[2])["breakouts"]
    assert [(x["id"], x["status"], x["name"]) for x in listed] == [(bid, "deleted", "Canvas")]
    assert _post(client, f"/api/specialists/{spec.id}/breakouts/{bid}/close-prompt")[0] == 404
    assert [e["kind"] for e in _events(client)] == ["breakout_open"]


# ------------------------------------------------------------- agent_hooks ---
def test_after_agent_call_records_sessions_and_signals(client, parent, monkeypatch, tmp_path):
    from fused_render.specialists import agent_hooks, breakouts, chat, signals

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    bid = out["breakout_id"]
    agent_hooks.after_agent_call("start", {"specialist": f"{spec.id}:breakout:{bid}"},
                                 {"result": {"session_id": "bs-9", "run_id": "r"}})
    assert breakouts.get(spec.id, bid)["session_id"] == "bs-9"
    agent_hooks.after_agent_call("start", {"specialist": f"{spec.id}:main"}, {"result": {"session_id": "ms-1"}})
    assert chat.state(spec.id)["current"] == "ms-1"
    # an errored start records nothing
    agent_hooks.after_agent_call("start", {"specialist": f"{spec.id}:main"}, {"result": {"error": "x"}})
    monkeypatch.setattr(agent_hooks, "_session_of_run", lambda run_id: "task-1")
    monkeypatch.setattr(agent_hooks, "_offer_card", lambda run_id, request_id: request_id == "offer")
    agent_hooks.after_agent_call("decide", {"run_id": "run1", "request_id": "q", "decision": "deny"},
                                 {"result": {"decided": "q", "decision": "deny"}})
    agent_hooks.after_agent_call("decide", {"run_id": "run1", "request_id": "offer", "decision": "deny"},
                                 {"result": {"decided": "offer"}})
    agent_hooks.after_agent_call("decide", {"run_id": "run1", "request_id": "q", "decision": "allow"},
                                 {"result": {"decided": "q"}})
    agent_hooks.after_agent_call("cancel", {"run_id": "run1"}, {"result": {"ok": True, "session_id": "task-1"}})
    agent_hooks.after_agent_call("snapshot_revert", {"session_id": "task-1", "version_id": "v"},
                                 {"result": {"ok": True}})
    assert [s["kind"] for s in signals.since("task-1", 0)] == ["deny", "stop", "undo"]
    # never raises
    agent_hooks.after_agent_call("start", None, None)
