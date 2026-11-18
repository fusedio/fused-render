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
    # "home" is the breakout's private workroom (C1), with what does not load there natively copied in
    work = os.path.join(spec.home, ".specialist", "breakouts", bid, "work")
    assert out["home"] == work == breakouts.cwd(spec.id, bid) and out["name"] == "Canvas"
    for rel in ("memory.md", "said.md"):
        with open(os.path.join(spec.home, rel)) as a, open(os.path.join(work, rel)) as b:
            assert a.read() == b.read()
    with open(os.path.join(work, "CLAUDE.md")) as f:
        assert "@memory.md\n@said.md\n" in f.read()
    with open(os.path.join(work, ".claude", "settings.json")) as f:
        assert json.load(f) == {"autoMemoryEnabled": False}
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
    assert launch.spec(out["start_params"]["specialist"], spec.home)["reason"] == "wrong_folder"
    got = launch.spec(out["start_params"]["specialist"], out["home"])
    assert "error" not in got, got
    assert any(parent["file"] in r for r in got["allow"])
    assert any(r == f"Read(//{spec.home.lstrip('/')}/**)" for r in got["allow"])  # knowledge reads, no card


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
    assert answer["calls"] == [(out["home"], "bs-1")]  # the transcript is keyed on the workroom
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
                     "home": out["home"]}
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
    assert kinds == ["breakout_closed"]  # one event per breakout, its current state (C5)
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
    for action in ("close-prompt", "closed", "discard", "reopen"):  # C5: 410, so the card can be dismissed
        assert _post(client, f"/api/specialists/{spec.id}/breakouts/{bid}/{action}") == (
            410, {"error": "This specialist was deleted.", "reason": "deleted"})
    assert _post(client, f"/api/specialists/never-was/breakouts/{bid}/discard")[0] == 404
    assert [e["kind"] for e in _events(client)] == ["breakout_deleted"]


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


# ------------------------------------------------------------ fix wave (BE-1) ---
def test_app_written_prefixes_are_pinned(client, parent):
    """CONTRACT C3: the frontend recognises app-written messages by these exact first characters."""
    from fused_render.specialists import breakouts

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    assert out["seed"].startswith('Brief from the task "')
    assert breakouts.BREAKOUT_SEED.startswith('Brief from the task "')
    assert breakouts.CLOSE_OUT.startswith("The user is closing this breakout")
    assert breakouts.CLOSE_OUT_FOLLOWUP.startswith("The user is closing this breakout")
    # C6's basis rule rides in the close-out request
    for text in (breakouts.CLOSE_OUT, breakouts.CLOSE_OUT_FOLLOWUP):
        assert "if the user stated it to you in this conversation, the basis is user-said" in text


def test_charter_of_a_new_home_says_no_ids_and_no_git(client):
    """CONTRACT C6 (new homes only)."""
    spec = make_specialist("Canvas", "Fused canvases")
    with open(os.path.join(spec.home, "CLAUDE.md")) as f:
        charter = f.read()
    for line in ("When you talk to the user, do not cite entry ids, note ids or commits; say it in plain words.",
                 "The app saves and commits your changes; never run git commands in this folder.",
                 "if the user stated it to you in this conversation, the basis is user-said"):
        assert line in charter, line


def test_workroom_is_private_and_resynced_each_turn(client, parent):
    """C1: the workroom lives under `.specialist/` (gitignored, so nothing written there is ever committed), the
    transcript lookups use it, and the prompt hook refreshes its memory copy each turn."""
    from fused_render.specialists import breakouts, history

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    bid, work = out["breakout_id"], out["home"]
    breakouts.set_session(spec.id, bid, "bs-7")
    assert breakouts.session_cwds(spec.id) == {"bs-7": work}
    assert os.path.realpath(work).startswith(os.path.realpath(os.path.join(spec.home, ".specialist")) + os.sep)
    with open(os.path.join(spec.home, "memory.md"), "w") as f:
        f.write("# Memory\n- [m-0001] Changed by the main chat.\n")
    history.before_turn(spec.id, "breakout", "bs-7")
    with open(os.path.join(work, "memory.md")) as f:
        assert "Changed by the main chat." in f.read()
    with open(os.path.join(work, "scratch.py"), "w") as f:
        f.write("print(1)\n")
    if history.available():
        assert history.log(spec.id)[0]["files"] == ["memory.md"]  # the turn's "pending changes" commit
        assert history.commit(spec.id, "x") is None  # the workroom's files never reach history


def test_what_a_breakout_plants_in_its_workroom_is_gone_before_its_next_turn(client, parent):
    """Seen live: a script run in a breakout created `.claude/skills/evil/` in the workroom and the next turn listed
    the skill. Skills, agents, CLAUDE.local.md and .mcp.json written there are removed before each turn; the
    breakout's own scratch files stay."""
    from fused_render.specialists import breakouts, history

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    bid, work = out["breakout_id"], out["home"]
    breakouts.set_session(spec.id, bid, "bs-8")
    planted = [".claude/skills/evil/SKILL.md", ".claude/agents/a.md", ".claude/settings.local.json",
               "CLAUDE.local.md", ".mcp.json"]
    for rel in planted + ["scratch.py"]:
        os.makedirs(os.path.dirname(os.path.join(work, rel)), exist_ok=True)
        with open(os.path.join(work, rel), "w") as f:
            f.write("x\n")
    history.before_turn(spec.id, "breakout", "bs-8")
    for rel in planted:
        assert not os.path.exists(os.path.join(work, rel)), rel
    assert sorted(os.listdir(os.path.join(work, ".claude"))) == ["settings.json"]
    assert os.path.exists(os.path.join(work, "scratch.py"))


def test_a_record_from_before_workrooms_keeps_the_home(client, parent):
    from fused_render.specialists import breakouts, counts

    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    bid = out["breakout_id"]
    path = os.path.join(spec.home, ".specialist", "breakouts", bid + ".json")
    rec = counts.read_json(path, {})
    rec.pop("work")
    rec["session_id"] = "old-1"
    counts.write_json(path, rec)
    assert breakouts.cwd(spec.id, bid) == spec.home and breakouts.session_cwds(spec.id) == {"old-1": spec.home}


def test_close_and_discard_end_the_breakouts_host(client, parent, answer, monkeypatch):
    """Fix wave item 11: a background job finishing after Close would start a turn in the live host; the host is
    ended at Close / Discard (and `launch.spec` refuses a start of a breakout that is not open)."""
    from fused_render.claude_agent import agent_module
    from fused_render.specialists import breakouts

    agent = agent_module()
    ended, asked = [], []
    monkeypatch.setattr(breakouts, "END_DELAY_S", 0.0)
    monkeypatch.setattr(agent, "_hosts_in", lambda folder: asked.append(folder) or ["run-x"], raising=False)
    monkeypatch.setattr(agent, "_cancel_run", lambda run_id, interrupt_first=True, queued=False:
                        ended.append((run_id, interrupt_first)) or {})
    spec = make_specialist("Canvas", "Fused canvases")
    _, out = _open(client, spec, parent)
    bid = out["breakout_id"]
    breakouts.set_session(spec.id, bid, "bs-1")
    _post(client, f"/api/specialists/{spec.id}/breakouts/{bid}/closed", {"session_id": "bs-1"})
    _post(client, f"/api/specialists/{spec.id}/breakouts/{bid}/discard")
    import time as _t

    deadline = _t.time() + 5
    while len(ended) < 2 and _t.time() < deadline:
        _t.sleep(0.05)
    assert ended == [("run-x", False), ("run-x", False)] and asked == [out["home"], out["home"]]


def test_hosts_in_and_live_runs_under(tmp_path, monkeypatch):
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    runs, home = tmp_path / "runs", tmp_path / "home"
    work = home / ".specialist" / "breakouts" / "b-1" / "work"
    work.mkdir(parents=True)
    for name, target in (("r1", home), ("r2", work), ("r3", tmp_path / "elsewhere")):
        (runs / name).mkdir(parents=True)
        (runs / name / "meta.json").write_text(json.dumps({"file": str(target)}))
    monkeypatch.setattr(agent, "RUNS", str(runs))
    monkeypatch.setattr(agent, "_alive", lambda run_dir: True)
    monkeypatch.setattr(agent, "_turn_state_fold", lambda run_dir, st, partial=False: (True, False))
    assert agent._hosts_in(str(work)) == ["r2"]
    assert sorted(r["run_id"] for r in agent._live_runs_under(str(home))) == ["r1", "r2"]
    assert [r["run_id"] for r in agent._live_runs_in(str(home))] == ["r1"]
