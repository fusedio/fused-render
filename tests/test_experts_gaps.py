"""Gap repair (docs/experts.md §4 `gaps.py`, §11): unsure consults become gap observations; consolidation folds
them into gap records, researches open gaps, turns a grounded finding into a `research` lesson plus a skill
proposal, and leaves the rest for a question to the user. `experts.model.text` / `tool_turn` are fakes: no model,
no network, no Chrome."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import threading
import time

import pytest

from fused_render.bots import browser as browser_mod
from fused_render.bots import registry
from fused_render.bots import store as bstore
from fused_render.bots.channels import base as chan
from fused_render.experts import (cards, consolidate, consult, gaps, memory, observations, proposals, scheduler,
                                  store)
from fused_render.experts import model as xmodel

DRAFT = json.dumps({"action": "create", "skill": None, "title": "Canvas answers", "trigger": "canvas",
                    "body": "- the answer"})
NOT_FOUND = '```json\n{"found": false, "answer": "", "refs": [], "why": "searched everything"}\n```'


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an expert must never start Chrome")
    monkeypatch.setattr(browser_mod.Browser, "start", boom)
    monkeypatch.setattr(browser_mod.BrowserProcess, "start", boom)
    monkeypatch.setattr(xmodel, "available", lambda bot: (True, ""))
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: DRAFT)
    monkeypatch.setattr(xmodel, "tool_turn", lambda bot, prompt, **k: NOT_FOUND)


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "ref"
    d.mkdir()
    (d / "a.py").write_text("ANSWER = 42\n")
    (d / "b.py").write_text("print('b')\n")
    return str(d)


@pytest.fixture
def expert(folder):
    return registry.create("Ref", kind="expert", expert={"ask_me_about": "Canvases",
                                                         "reads": [{"kind": "folder", "path": folder}]})


def _events(b):
    return [ev for _, ev in bstore.iter_events(b.events_path)]


def _wait_for(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _idle(b):
    return _wait_for(lambda: not b.running())


def _set_gaps(b, **kw):
    raw = dict(b.meta.get("expert") or {})
    raw["gaps"] = {**raw.get("gaps", {}), **kw}
    b.meta["expert"] = raw


def _gap_obs(b, question, consult_id, session=""):
    return observations.write(b, kind="gap", claim=f"Could not answer: {question}", trust="gap",
                              session=session, consult=consult_id)


def _found(path, answer="ANSWER is 42", read=True, refs=None):
    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        assert "HOW TO WORK (research)" in system and "<<<BEGIN QUESTIONS>>>" in prompt
        if read:
            handler("read_file", {"path": path})
        return "```json\n" + json.dumps({"found": True, "answer": answer, "refs": refs or [path],
                                         "why": "read it"}) + "\n```"
    return fake


def _question_card(b):
    assert _wait_for(lambda: b.meta.get("waiting_on"))
    return [e for e in _events(b) if e["role"] == "question" and e.get("expert_gap")][-1]


# ------------------------------------------------------------------ records ---
def test_manifest_gap_defaults(expert):
    assert store.manifest(expert)["gaps"] == {"research": True, "ask_after": 2, "max_attempts": 2,
                                              "ask_every_s": 86400}
    _set_gaps(expert, research=False, ask_after="x")
    assert store.manifest(expert)["gaps"] == {"research": False, "ask_after": 2, "max_attempts": 2,
                                              "ask_every_s": 86400}


def test_record_dedupes_and_counts_once_per_consult(expert):
    g = gaps.record(expert, "How do I chain UDFs?", consult="c1", session="s1")
    assert g["status"] == "open" and g["count"] == 1 and g["questions"] == ["How do I chain UDFs?"]
    again = gaps.record(expert, "  how do i   chain udfs ", consult="c2", session="s2")
    assert again["id"] == g["id"] and again["count"] == 2 and again["consults"] == ["c1", "c2"]
    assert again["sessions"] == ["s1", "s2"]
    assert gaps.record(expert, "How do I chain UDFs?", consult="c2")["count"] == 2  # same consult: once
    other = gaps.record(expert, "Where do canvases live?", consult="c3")
    assert [x["id"] for x in gaps.listing(expert)] == [g["id"], other["id"]]  # most asked first
    with pytest.raises(ValueError):
        gaps.record(expert, "   ")


def test_consult_writes_only_the_gap_observation(expert, monkeypatch):
    monkeypatch.setattr(xmodel, "tool_turn", lambda bot, prompt, **k:
                        '```json\n{"answer": "no idea", "refs": [], "used": [], "unsure": true}\n```')
    out = consult.consult(expert, "how?")
    assert out["unsure"] and gaps.listing(expert) == []
    assert [o["kind"] for o in observations.pending(expert)] == ["gap"]


def test_consolidation_records_and_merges_gaps(expert, monkeypatch):
    _set_gaps(expert, research=False)
    old = gaps.record(expert, "How do I chain UDFs?", consult="c0")
    o1 = _gap_obs(expert, "How can one UDF read another UDF's output?", "c1", session="s1")
    o2 = _gap_obs(expert, "Where do canvases live?", "c2")
    prompts = []

    def text(bot, prompt, **k):
        prompts.append(prompt)
        return json.dumps([{"obs": o1["id"], "op": "gap", "gap": old["id"]}, {"obs": o2["id"], "op": "add",
                                                                             "claim": "never a lesson"}])
    monkeypatch.setattr(xmodel, "text", text)
    r = consolidate.run(expert, force=True)
    assert r["ran"] and r["added"] == [] and r["ignored"] == 2 and r["researched"] == []  # research off
    assert old["id"] in prompts[0] and "Open gaps" in prompts[0]
    rows = {g["id"]: g for g in gaps.listing(expert)}
    assert len(rows) == 2 and rows[old["id"]]["count"] == 2  # folded into the older gap
    assert "How can one UDF read another UDF's output?" in rows[old["id"]]["questions"]
    assert rows[old["id"]]["consults"] == ["c0", "c1"] and rows[old["id"]]["sessions"] == ["s1"]
    assert memory.items(expert) == [] and observations.pending(expert) == []


def test_unparseable_merge_answer_merges_nothing(expert, monkeypatch):
    _set_gaps(expert, research=False)
    gaps.record(expert, "How do I chain UDFs?", consult="c0")
    _gap_obs(expert, "How can one UDF read another's output?", "c1")
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: "no json here")
    r = consolidate.run(expert, force=True)
    assert r["ran"] and len(gaps.listing(expert)) == 2 and consolidate.failures(expert) == 0


# ------------------------------------------------------------------ research ---
def test_found_with_a_read_ref_becomes_a_research_lesson_and_a_proposal(expert, folder, monkeypatch):
    ref = os.path.join(folder, "a.py")
    monkeypatch.setattr(xmodel, "tool_turn", _found(ref))
    _gap_obs(expert, "What is ANSWER?", "c1")
    r = consolidate.run(expert, force=True)
    g = gaps.listing(expert)[0]
    assert r["researched"] == [g["id"]] and g["status"] == "proposed" and g["proposal"]
    assert g["finding"] == {"answer": "ANSWER is 42", "refs": [os.path.realpath(ref)]}
    lesson = [i for i in memory.items(expert) if i["trust"] == "research"][0]
    assert lesson["claim"] == "When asked: What is ANSWER? Answer: ANSWER is 42" and lesson["gap"] == g["id"]
    assert lesson["about"] == [os.path.realpath(ref)]
    assert not memory.indexed(lesson) and lesson["id"] not in memory.index_text(expert)  # not until approved
    p = proposals.get(expert, g["proposal"])
    assert p["gap"] == g["id"] and p["items"] == [lesson["id"]]
    # The draft finishes in the background and raises its own approval card; Approve answers the gap.
    assert _wait_for(lambda: expert.meta.get("waiting_on"))
    card = [e for e in _events(expert) if e["role"] == "approval"][-1]
    assert card["expert_proposal"] == p["id"] and "could not answer" in card["text"]
    expert.send("yes", answers=card["seq"])
    assert _idle(expert)
    g = gaps.get(expert, g["id"])
    assert g["status"] == "answered" and g["resolution"] == "skill" and expert.skills()
    assert lesson["id"] in memory.index_text(expert)


@pytest.mark.parametrize("case", ["outside", "unread", "missing"])
def test_found_without_a_grounded_ref_is_not_found(expert, folder, tmp_path, monkeypatch, case):
    outside = tmp_path / "elsewhere.py"
    outside.write_text("x\n")
    if case == "outside":
        fake = _found(str(outside), read=False)
    elif case == "unread":
        fake = _found(os.path.join(folder, "a.py"), read=False)
    else:
        fake = _found(os.path.join(folder, "a.py"), refs=[os.path.join(folder, "nope.py")])
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    _gap_obs(expert, "What is ANSWER?", "c1")
    consolidate.run(expert, force=True)
    g = gaps.listing(expert)[0]
    assert g["status"] == "open" and g["attempts"] == 1 and g["proposal"] is None
    assert memory.items(expert) == [] and proposals.listing(expert) == []


def test_not_found_below_ask_after_stays_open_without_a_question(expert):
    _gap_obs(expert, "What is ANSWER?", "c1")
    r = consolidate.run(expert, force=True)
    g = gaps.listing(expert)[0]
    assert r["researched"] == [g["id"]] and g["status"] == "open" and g["attempts"] == 1
    assert "not found" in g["error"] and not gaps.askable(expert, g)
    assert not any(e["role"] == "question" for e in _events(expert))
    # The second attempt (max_attempts 2) makes it askable; then it is not researched again.
    consolidate.run(expert, force=True)
    g = gaps.get(expert, g["id"])
    assert g["attempts"] == 2
    _question_card(expert)
    assert gaps.repairable(expert) == []


def test_not_found_at_ask_after_asks_and_a_local_answer_is_a_user_lesson(expert, monkeypatch):
    _gap_obs(expert, "What is ANSWER?", "c1")
    _gap_obs(expert, "what is answer", "c2")
    consolidate.run(expert, force=True)
    card = _question_card(expert)
    g = gaps.listing(expert)[0]
    assert g["status"] == "asked" and g["card_seq"] == card["seq"] and g["count"] == 2
    assert card["options"] == ["Skip"] and card["expert_gap"] == g["id"]
    assert card["text"].startswith("Sessions asked me this 2 times") and "What is ANSWER?" in card["text"]
    expert.send("It is 42, see a.py")
    assert _idle(expert)
    g = gaps.get(expert, g["id"])
    assert g["status"] == "answered" and g["resolution"] == "user"
    item = memory.items(expert)[0]
    assert item["trust"] == "user" and "It is 42, see a.py" in item["claim"]
    assert any(e["role"] == "note" and e["text"].startswith("Thanks. Saved as a lesson:") for e in _events(expert))
    assert expert.thread is None and expert.meta["status"] == "idle"
    # At most one gap question per ask_every_s.
    other = gaps.record(expert, "Another?", consult="c9")
    gaps._update(expert, other["id"], attempts=2)
    assert gaps.to_ask(expert) is None and cards.ask_if_needed(expert) is False


def test_skip_dismisses(expert):
    g = gaps.record(expert, "What is ANSWER?", consult="c1")
    gaps._update(expert, g["id"], attempts=2)
    assert cards.tick_one(expert) is True
    _question_card(expert)
    expert.send("Skip")
    assert _idle(expert)
    g = gaps.get(expert, g["id"])
    assert g["status"] == "dismissed" and g["resolution"] == "dismissed" and memory.items(expert) == []


def test_an_outside_reply_does_not_settle_the_card(expert, monkeypatch):
    asked = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        asked.append(prompt)
        return '```json\n{"answer": "hi", "refs": [], "used": [], "unsure": false}\n```'
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    g = gaps.record(expert, "What is ANSWER?", consult="c1")
    gaps._update(expert, g["id"], attempts=2)
    assert cards.ask_if_needed(expert)
    card = _question_card(expert)
    expert.receive("it is 7", via=chan.via("imessage", "+15550001"), answers=card["seq"])
    assert _wait_for(lambda: any(e["role"] == "note" and "Only you" in e["text"] for e in _events(expert)))
    assert expert.running() and gaps.get(expert, g["id"])["status"] == "asked" and memory.items(expert) == []
    expert.send("It is 42")
    assert _wait_for(lambda: asked) and _idle(expert)
    assert gaps.get(expert, g["id"])["status"] == "answered"
    assert [i["claim"] for i in memory.items(expert)] == ['When asked "What is ANSWER?": It is 42']
    assert "outside channel" in asked[0].lower() and "it is 7" in asked[0]  # re-delivered, still untrusted


def _proposed_gap(expert, folder, monkeypatch):
    monkeypatch.setattr(xmodel, "tool_turn", _found(os.path.join(folder, "a.py")))
    monkeypatch.setattr(cards, "show", lambda bot, p: False)  # no card: decide directly
    _gap_obs(expert, "What is ANSWER?", "c1")
    consolidate.run(expert, force=True)
    g = gaps.listing(expert)[0]
    assert _wait_for(lambda: proposals.get(expert, g["proposal"])["status"] == "ready")
    return g


def test_proposal_reject_dismisses_the_gap(expert, folder, monkeypatch):
    g = _proposed_gap(expert, folder, monkeypatch)
    proposals.reject(expert, g["proposal"])
    g = gaps.get(expert, g["id"])
    assert g["status"] == "dismissed" and g["resolution"] == "dismissed"
    assert gaps.repairable(expert) == [] and proposals.candidates(expert) == []


def test_proposal_failure_dismisses_the_gap(expert, folder, monkeypatch):
    g = _proposed_gap(expert, folder, monkeypatch)
    monkeypatch.setattr(expert, "skill_save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(ValueError):
        proposals.approve(expert, g["proposal"])
    assert gaps.get(expert, g["id"])["status"] == "dismissed"
    assert proposals.candidates(expert) == []  # its research lesson is not proposed again


def test_a_research_lesson_waits_for_the_next_run_when_a_proposal_is_preparing(expert, folder, monkeypatch):
    monkeypatch.setattr(xmodel, "tool_turn", _found(os.path.join(folder, "a.py")))
    monkeypatch.setattr(proposals, "_preparing", lambda bot: [{"id": "pbusy"}])
    _gap_obs(expert, "What is ANSWER?", "c1")
    r = consolidate.run(expert, force=True)
    g = gaps.listing(expert)[0]
    assert g["status"] == "proposed" and g["proposal"] is None and r["proposed"] == []
    lesson = [i for i in memory.items(expert) if i["trust"] == "research"][0]
    assert [i["id"] for i in proposals.candidates(expert)] == [lesson["id"]]  # count 1, still a candidate
    monkeypatch.undo()
    monkeypatch.setattr(xmodel, "available", lambda bot: (True, ""))
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: DRAFT)
    monkeypatch.setattr(cards, "show", lambda bot, p: False)
    pid = proposals.propose_candidates(expert)[0]
    assert proposals.get(expert, pid)["gap"] == g["id"] and gaps.get(expert, g["id"])["proposal"] == pid


def test_research_off_skips_repair(expert, monkeypatch):
    _set_gaps(expert, research=False)
    monkeypatch.setattr(xmodel, "tool_turn", lambda *a, **k: pytest.fail("no research when it is off"))
    _gap_obs(expert, "What is ANSWER?", "c1")
    _gap_obs(expert, "What is ANSWER", "c2")
    r = consolidate.run(expert, force=True)
    assert r["researched"] == []
    assert consolidate.would_run(expert, force=True) is False  # nothing pending, nothing repairable
    _question_card(expert)  # count 2 >= ask_after: asked without research


def test_no_engine_no_research(expert, monkeypatch):
    gaps.record(expert, "What is ANSWER?", consult="c1")
    monkeypatch.setattr(xmodel, "available", lambda bot: (False, "needs Claude, Codex or Copilot"))
    monkeypatch.setattr(xmodel, "tool_turn", lambda *a, **k: pytest.fail("no research without an engine"))
    r = consolidate.run(expert, force=True)
    assert r["ran"] is False and r["reason"]


# ------------------------------------------------------------- single flight ---
def test_background_consolidation_is_single_flight_and_reports_its_stage(expert, monkeypatch):
    hold, entered = threading.Event(), threading.Event()

    def slow(bot, prompt, **k):
        entered.set()
        hold.wait(5)
        return NOT_FOUND
    monkeypatch.setattr(xmodel, "tool_turn", slow)
    g = gaps.record(expert, "What is ANSWER?", consult="c1")
    assert consolidate.start(expert, force=True) is True
    assert entered.wait(5)
    assert consolidate.status(expert) == {"running": True, "stage": "researching", "gap": g["id"]}
    assert consolidate.start(expert, force=True) is False
    assert consolidate.run(expert, force=True)["ran"] is False
    hold.set()
    assert _wait_for(lambda: not consolidate.status(expert)["running"])
    assert consolidate.status(expert) == {"running": False, "stage": "", "gap": None}
    assert gaps.get(expert, g["id"])["attempts"] == 1
    assert consolidate.start(expert, force=False) is False  # nothing due


def test_scheduler_tick_researches_once(expert, monkeypatch):
    runs = []
    monkeypatch.setattr(xmodel, "tool_turn", lambda bot, prompt, **k: runs.append(1) or NOT_FOUND)
    monkeypatch.setattr(scheduler, "_owner", lambda: True)
    monkeypatch.setattr(scheduler, "_acquire", lambda: True)
    monkeypatch.setattr(scheduler, "_release", lambda: None)
    from fused_render.experts import capture
    monkeypatch.setattr(capture, "capture_due", lambda **k: {})
    for i in range(3):  # due: consolidate_at 3
        _gap_obs(expert, "What is ANSWER?", f"c{i}")
    out = scheduler.tick()
    assert out["consolidated"] == [expert.id] and len(runs) == 1
    out = scheduler.tick()  # nothing due: no consolidation, no research
    assert out["consolidated"] == [] and len(runs) == 1


# -------------------------------------------------------------------- routes ---
def test_gap_routes(client, expert):
    g = gaps.record(expert, "What is ANSWER?", consult="c1")
    status, _h, body = client.get(f"/api/experts/{expert.id}/gaps")
    body = json.loads(body)
    assert status == 200 and body["researching"] is None and [x["id"] for x in body["gaps"]] == [g["id"]]
    assert set(body["gaps"][0]) >= {"id", "question", "questions", "count", "consults", "sessions", "created",
                                    "updated", "status", "attempts", "finding", "proposal", "card_seq",
                                    "resolution", "error"}
    status, _h, body = client.get(f"/api/experts/{expert.id}/observations")
    assert status == 200 and json.loads(body)["consolidating"] == {"running": False, "stage": "", "gap": None}
    status, _h, body = client.post(f"/api/experts/{expert.id}/gaps/gnope/dismiss", {})
    assert status == 404 and json.loads(body) == {"error": "no such gap"}
    status, _h, body = client.post(f"/api/experts/{expert.id}/gaps/..%2F..%2Fx/dismiss", {})
    assert status == 404
    status, _h, _b = client._do(__import__("urllib.request").request.Request(
        client.base + f"/api/experts/{expert.id}/gaps/{g['id']}/dismiss", data=b"{}", method="POST",
        headers={"Content-Type": "application/json"}))
    assert status == 403
    status, _h, body = client.post(f"/api/experts/{expert.id}/gaps/{g['id']}/dismiss", {})
    body = json.loads(body)
    assert status == 200 and body["id"] == g["id"] and body["status"] == "dismissed"
    status, _h, body = client.get("/api/experts/nobody/gaps")
    assert status == 404 and json.loads(body)["error"]
    status, _h, _b = client.post(f"/api/experts/{expert.id}/gaps/{g['id']}/research", {})
    assert status in (404, 405)  # there is no manual research route: consolidation repairs gaps


def test_phone_door_allows_only_the_gaps_get():
    from fused_render import lan
    path = "/api/experts/e1e1e1e1/gaps"
    allowed = [d for d in lan._DOORS if d[0].match(path)]
    assert allowed and all(d[1] == frozenset({"GET"}) for d in allowed)
    assert not any(d[0].match(path + "/g1/dismiss") for d in lan._DOORS)


# -------------------------------------------------------------- observations ---
def test_archived_observations_record_their_outcome(expert, monkeypatch):
    _set_gaps(expert, research=False)
    old = memory.add(expert, "existing lesson")
    g0 = gaps.record(expert, "How do I chain UDFs?", consult="c0")
    o_add = observations.write(expert, kind="user_correction", claim="new", trust="outcome")
    o_bump = observations.write(expert, kind="user_correction", claim="again", trust="outcome")
    o_none = observations.write(expert, kind="user_correction", claim="noise", trust="outcome")
    o_gap = _gap_obs(expert, "How can a UDF read another's output?", "c1")
    o_new = _gap_obs(expert, "Where do canvases live?", "c2")
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: json.dumps([
        {"obs": o_add["id"], "op": "add", "claim": "A new lesson."}, {"obs": o_bump["id"], "op": "bump",
                                                                      "item": old["id"]},
        {"obs": o_none["id"], "op": "nothing"}, {"obs": o_gap["id"], "op": "gap", "gap": g0["id"]}]))
    r = consolidate.run(expert, force=True)
    done = {o["id"]: o for o in observations.done(expert)}
    assert done[o_add["id"]]["outcome"]["op"] == "add" and done[o_add["id"]]["outcome"]["item"] == r["added"][0]
    assert done[o_bump["id"]]["outcome"] == {"op": "bump", "item": old["id"], "at": done[o_bump["id"]]["outcome"]["at"]}
    assert done[o_none["id"]]["outcome"]["op"] == "nothing" and "item" not in done[o_none["id"]]["outcome"]
    assert done[o_gap["id"]]["outcome"]["op"] == "gap" and done[o_gap["id"]]["outcome"]["gap"] == g0["id"]
    new_gap = [g for g in gaps.listing(expert) if g["question"] == "Where do canvases live?"][0]
    assert done[o_new["id"]]["outcome"]["gap"] == new_gap["id"]
    assert [o["id"] for o in observations.done(expert, limit=2)] == [o["id"] for o in observations.done(expert)][:2]


def test_observations_carry_their_source(expert, monkeypatch, tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    monkeypatch.setattr(xmodel, "tool_turn", lambda bot, prompt, **k:
                        '```json\n{"answer": "no idea", "refs": [], "used": [], "unsure": true}\n```')
    consult.consult(expert, "how?", caller={"session_id": "s1", "target": str(target), "backend": "codex"})
    o = observations.pending(expert)[0]
    assert o["source"] == {"target": str(target), "backend": "codex"} and o["session"] == "s1"
    from fused_render.experts import capture
    monkeypatch.setattr(capture, "transcript_markdown", lambda s: "USER| always use x")
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: json.dumps(
        [{"kind": "repeated_request", "claim": "Use x.", "about": []}]))
    monkeypatch.setattr(capture, "mark_captured", lambda *a, **k: None)
    capture.capture_session(expert, {"session_id": "s2", "backend": "claude", "target": "/w/canvas", "path": ""})
    o = [x for x in observations.pending(expert) if x["session"] == "s2"][0]
    assert o["source"] == {"target": "/w/canvas", "backend": "claude"}
    plain = observations.write(expert, kind="user_correction", claim="c", trust="outcome")
    assert "source" not in plain


def test_observations_route_shape(client, expert, monkeypatch):
    _set_gaps(expert, research=False)
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: "[]")
    lesson = observations.write(expert, kind="user_correction", claim="c", trust="outcome")
    gobs = _gap_obs(expert, "What is ANSWER?", "c1")
    status, _h, body = client.get(f"/api/experts/{expert.id}/observations")
    body = json.loads(body)
    assert status == 200 and set(body) == {"pending", "done", "due", "consolidating"}
    assert [o["id"] for o in body["pending"]] == [lesson["id"], gobs["id"]] and body["done"] == []
    assert "gap" not in body["pending"][0] and body["pending"][1]["gap"] is None  # no gap record yet
    consolidate.run(expert, force=True)
    pending = _gap_obs(expert, "what is answer", "c2")  # not folded into a gap record until consolidation
    status, _h, body = client.get(f"/api/experts/{expert.id}/observations")
    body = json.loads(body)
    assert [o["id"] for o in body["done"]] == [gobs["id"], lesson["id"]]  # newest first
    done = {o["id"]: o for o in body["done"]}
    g = done[gobs["id"]]["gap"]
    assert g["question"] == "What is ANSWER?" and g["status"] == "open" and done[gobs["id"]]["outcome"]["op"] == "gap"
    assert done[lesson["id"]]["outcome"]["op"] == "nothing" and "gap" not in done[lesson["id"]]
    assert body["pending"][0]["id"] == pending["id"] and body["pending"][0]["gap"] is None
    assert body["consolidating"] == {"running": False, "stage": "", "gap": None}


def test_gaps_over_http_carry_their_proposal_state(client, expert):
    plain = gaps.record(expert, "Where do canvases live?", consult="c0")
    linked = gaps.record(expert, "What is ANSWER?", consult="c1")
    lost = gaps.record(expert, "Who wrote this?", consult="c2")
    rec = {"id": "pprep", "ts": time.time(), "status": "preparing", "items": [], "error": ""}
    proposals._save(expert, rec)
    gaps.mark_proposed(expert, linked["id"], "pprep")
    gaps.mark_proposed(expert, lost["id"], "pgone")  # its proposal cannot be found
    for o, c in ((plain, "c0"), (linked, "c1")):
        _gap_obs(expert, o["question"], c)
    stored = json.load(open(os.path.join(store.expert_dir(expert), "gaps.json")))
    assert not any("proposal_status" in g or "proposal_error" in g for g in stored["gaps"])  # never persisted

    def http():
        _s, _h, body = client.get(f"/api/experts/{expert.id}/gaps")
        by_id = {g["id"]: g for g in json.loads(body)["gaps"]}
        _s, _h, body = client.get(f"/api/experts/{expert.id}/observations")
        joined = {o["gap"]["id"]: o["gap"] for o in json.loads(body)["pending"] if o.get("gap")}
        return by_id, joined

    def state(g):
        return g["status"], g["proposal_status"], g["proposal_error"]

    # the observation's gap is joined by consult id
    by_id, joined = http()
    assert state(by_id[plain["id"]]) == ("open", "", "")
    assert state(by_id[linked["id"]]) == ("proposed", "preparing", "")
    assert state(by_id[lost["id"]]) == ("proposed", "", "")
    assert state(joined[plain["id"]]) == ("open", "", "") and state(joined[linked["id"]]) == ("proposed", "preparing", "")

    proposals._save(expert, dict(rec, status="failed", error="the draft failed"))
    by_id, joined = http()
    assert state(by_id[linked["id"]]) == ("proposed", "failed", "the draft failed")
    assert state(joined[linked["id"]]) == ("proposed", "failed", "the draft failed")
    proposals._save(expert, dict(rec, status="ready"))
    by_id, joined = http()
    assert by_id[linked["id"]]["proposal_status"] == "ready" == joined[linked["id"]]["proposal_status"]
