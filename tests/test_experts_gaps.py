"""Research (docs/experts.md "research"): unsure consults become gap entries in the inbox; a Learn run
records and groups them, researches the open ones, turns a grounded finding into a `research` lesson with its
source, and logs "could not find" for the rest. Nobody is ever asked. `experts.model.text` / `tool_turn` are
fakes: no model, no network, no Chrome."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import threading
import time

import pytest

from fused_render.bots import browser as browser_mod
from fused_render.bots import registry
from fused_render.bots import store as bstore
from fused_render.experts import consolidate, consult, gaps, knowledge, memory, observations, scheduler, store
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


def _gap(b, gid):
    return next(g for g in gaps.listing(b) if g["id"] == gid)


def _done(b):
    d = os.path.join(store.expert_dir(b), "observations", "done")
    return [store.read_json(os.path.join(d, n), None) for n in os.listdir(d)]


def _found(path, answer="ANSWER is 42", read=True, refs=None):
    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        assert "HOW TO WORK (research)" in system and "<<<BEGIN QUESTIONS>>>" in prompt
        if read:
            handler("read_file", {"path": path})
        return "```json\n" + json.dumps({"found": True, "answer": answer, "refs": refs or [path],
                                         "why": "read it"}) + "\n```"
    return fake


# ------------------------------------------------------------------ records ---
def test_manifest_gap_defaults(expert):
    assert store.manifest(expert)["gaps"] == {"research": True, "max_attempts": 2}
    _set_gaps(expert, research=False, max_attempts="x", ask_after=5, ask_every_s=1)  # the old asking keys are gone
    assert store.manifest(expert)["gaps"] == {"research": False, "max_attempts": 2}


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
    assert consolidate.run(expert, force=True)["ran"]
    assert old["id"] in prompts[0] and "Open gaps" in prompts[0]
    rows = {g["id"]: g for g in gaps.listing(expert)}
    assert len(rows) == 2 and rows[old["id"]]["count"] == 2  # folded into the older gap
    assert "How can one UDF read another UDF's output?" in rows[old["id"]]["questions"]
    assert rows[old["id"]]["consults"] == ["c0", "c1"] and rows[old["id"]]["sessions"] == ["s1"]
    assert memory.items(expert) == [] and observations.pending(expert) == []
    lines = knowledge.activity(expert)
    assert [ln["action"] for ln in lines] == ["noticed", "noticed"] and all(ln.get("quiet") for ln in lines)
    assert knowledge.unread(expert) == 0  # noticing a question is not news


def test_unparseable_merge_answer_merges_nothing(expert, monkeypatch):
    _set_gaps(expert, research=False)
    gaps.record(expert, "How do I chain UDFs?", consult="c0")
    _gap_obs(expert, "How can one UDF read another's output?", "c1")
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: "no json here")
    assert consolidate.run(expert, force=True)["ran"]
    assert len(gaps.listing(expert)) == 2 and consolidate.failures(expert) == 0


# ------------------------------------------------------------------ research ---
def test_found_with_a_read_ref_becomes_a_research_lesson_with_its_source(expert, folder, monkeypatch):
    ref = os.path.realpath(os.path.join(folder, "a.py"))
    monkeypatch.setattr(xmodel, "tool_turn", _found(ref))
    _gap_obs(expert, "What is ANSWER?", "c1", session="s1")
    consolidate.run(expert, force=True)
    g = gaps.listing(expert)[0]
    assert g["status"] == "answered" and g["finding"] == {"answer": "ANSWER is 42", "refs": [ref]}
    (lesson,) = memory.items(expert)
    assert lesson["claim"] == "When asked: What is ANSWER? Answer: ANSWER is 42" and lesson["trust"] == "research"
    assert lesson["about"] == [ref]
    assert memory.indexed(lesson) and lesson["id"] in memory.index_text(expert)  # used in answers at once
    (line,) = knowledge.activity(expert, quiet=False)
    assert (line["action"], line["target"], line["stage"]) == ("learned", lesson["id"], "research")
    assert line["evidence"] == {"session": "s1", "source": ref, "item": g["id"]} and line["undoable"]
    assert gaps.repairable(expert) == [] and not expert.meta.get("waiting_on")
    assert not [e for e in _events(expert) if e["role"] in ("approval", "question")]  # nobody is asked anything


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
    assert g["status"] == "open" and g["attempts"] == 1 and memory.items(expert) == []
    (line,) = knowledge.activity(expert, quiet=False)
    assert line["action"] == "could_not_find" and "What is ANSWER?" in line["summary"] and line["why"]


def test_research_off_skips_repair(expert, monkeypatch):
    _set_gaps(expert, research=False)
    monkeypatch.setattr(xmodel, "tool_turn", lambda *a, **k: pytest.fail("no research when it is off"))
    _gap_obs(expert, "What is ANSWER?", "c1")
    _gap_obs(expert, "What is ANSWER", "c2")
    consolidate.run(expert, force=True)
    assert gaps.listing(expert)[0]["count"] == 2 and knowledge.activity(expert, quiet=False) == []


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
    assert consolidate.status(expert) == {"running": True, "stage": "research", "waiting": 0}
    assert consolidate.start(expert, force=True) is False
    assert consolidate.run(expert, force=True)["ran"] is False
    hold.set()
    assert _wait_for(lambda: not consolidate.status(expert)["running"])
    assert consolidate.status(expert) == {"running": False, "stage": "", "waiting": 0}
    assert _gap(expert, g["id"])["attempts"] == 1
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
    consolidate.run(expert, force=True)
    done = {o["id"]: o for o in _done(expert)}
    (new,) = [i for i in memory.items(expert) if i["id"] != old["id"]]
    assert done[o_add["id"]]["outcome"]["op"] == "add" and done[o_add["id"]]["outcome"]["item"] == new["id"]
    assert done[o_bump["id"]]["outcome"] == {"op": "bump", "item": old["id"], "at": done[o_bump["id"]]["outcome"]["at"]}
    assert done[o_none["id"]]["outcome"]["op"] == "nothing" and "item" not in done[o_none["id"]]["outcome"]
    assert done[o_gap["id"]]["outcome"]["op"] == "gap" and done[o_gap["id"]]["outcome"]["gap"] == g0["id"]
    new_gap = [g for g in gaps.listing(expert) if g["question"] == "Where do canvases live?"][0]
    assert done[o_new["id"]]["outcome"]["gap"] == new_gap["id"]


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


# ------------------------------------------------------------ research limits ---
def _no_research(monkeypatch):
    monkeypatch.setattr(xmodel, "tool_turn", lambda *a, **k: pytest.fail("no research run expected"))


def _capped(expert, question="What is ANSWER?", consult_id="c0", **fields):
    """An open gap at max_attempts (2), last researched two days ago against today's sources."""
    g = gaps.record(expert, question, consult=consult_id)
    gaps._update(expert, g["id"], **{"attempts": 2, "researched_at": time.time() - 2 * 86400,
                                     "research_fp": gaps.sources_fp(expert), **fields})
    return _gap(expert, g["id"])


def test_a_touched_gap_at_the_cap_is_not_researched_again(expert, monkeypatch):
    g = _capped(expert)
    _no_research(monkeypatch)
    _gap_obs(expert, "What is ANSWER?", "c1")  # asked again: touched by this run
    consolidate.run(expert, force=True)
    after = _gap(expert, g["id"])
    assert after["attempts"] == 2 and after["count"] == 2 and after["status"] == "open"


def test_a_gap_out_of_attempts_is_reported_once_as_could_not_find(expert, monkeypatch):
    g = _capped(expert)
    _no_research(monkeypatch)
    consolidate.run(expert, force=True)
    lines = [ln for ln in knowledge.activity(expert) if ln["action"] == "could_not_find"]
    assert len(lines) == 1 and "quiet" not in lines[0] and lines[0]["stage"] == "research"
    assert lines[0]["summary"] == "Could not find an answer to: What is ANSWER?"
    assert lines[0]["why"] and lines[0]["evidence"]["item"] == g["id"]
    assert _gap(expert, g["id"])["reported"] is True and _gap(expert, g["id"])["attempts"] == 2
    _gap_obs(expert, "What is ANSWER?", "c1")  # asked again, another run: no second line
    consolidate.run(expert, force=True)
    consolidate.run(expert, force=True)
    assert len([ln for ln in knowledge.activity(expert) if ln["action"] == "could_not_find"]) == 1


def test_the_attempt_that_uses_up_the_cap_is_its_own_report(expert, monkeypatch):
    g = gaps.record(expert, "What is ANSWER?", consult="c0")
    gaps._update(expert, g["id"], attempts=1)
    monkeypatch.setattr(xmodel, "tool_turn", lambda bot, prompt, **k: NOT_FOUND)
    assert [c["action"] for c in gaps.step(expert, {})] == ["could_not_find"]
    assert _gap(expert, g["id"])["reported"] is True and gaps.gave_up(expert) == []
    assert list(gaps.step(expert, {})) == []


def test_a_gap_is_researched_at_most_once_a_day(expert, monkeypatch):
    g = gaps.record(expert, "What is ANSWER?", consult="c0")
    gaps._update(expert, g["id"], attempts=1, researched_at=time.time() - 3600)
    assert gaps.repairable(expert) == []
    gaps._update(expert, g["id"], researched_at=time.time() - gaps.RESEARCH_EVERY_S - 60)
    assert [x["id"] for x in gaps.repairable(expert)] == [g["id"]]


def test_changed_sources_allow_one_more_research_at_the_cap(expert, folder, monkeypatch):
    g = _capped(expert)
    legacy = _capped(expert, "Who wrote b.py?", "c9", research_fp="")  # no stored fingerprint: never
    assert gaps.repairable(expert) == []
    time.sleep(0.01)
    with open(os.path.join(folder, "new.py"), "w") as f:  # a read root changed
        f.write("NEW = 1\n")
    assert [x["id"] for x in gaps.repairable(expert)] == [g["id"]]
    runs = []
    monkeypatch.setattr(xmodel, "tool_turn", lambda bot, prompt, **k: runs.append(1) or NOT_FOUND)
    # (two lines: the legacy gap is reported as given up, the other is researched once more)
    assert [c["action"] for c in gaps.step(expert, {})] == ["could_not_find", "could_not_find"]
    g2 = _gap(expert, g["id"])
    assert runs == [1] and g2["attempts"] == 3 and g2["research_fp"] == gaps.sources_fp(expert)
    gaps._update(expert, g["id"], researched_at=time.time() - 2 * 86400)  # a day later: still no change
    assert gaps.repairable(expert) == [] and legacy["id"] not in {x["id"] for x in gaps.repairable(expert)}
    expert.skill_save("Canvas rules", "canvas", "- a rule")  # a skill changed: one more again
    assert [x["id"] for x in gaps.repairable(expert)] == [g["id"]]


# ---------------------------------------------------------------- idle tick ---
class _Tasks:
    def __init__(self, rows):
        self.rows = rows

    def _collect(self):
        return {r["session_id"]: dict(r) for r in self.rows}

    def _place(self, task):
        pass


def _idle_tick_fakes(monkeypatch):
    from fused_render import session_liveness
    from fused_render.experts import capture
    monkeypatch.setattr(scheduler, "_owner", lambda: True)
    monkeypatch.setattr(scheduler, "_acquire", lambda: True)
    monkeypatch.setattr(scheduler, "_release", lambda: None)
    monkeypatch.setattr(xmodel, "text", lambda *a, **k: pytest.fail("an idle tick calls no model"))
    monkeypatch.setattr(xmodel, "tool_turn", lambda *a, **k: pytest.fail("an idle tick calls no model"))
    monkeypatch.setattr(session_liveness, "tail_activity", lambda *a, **k: pytest.fail("no transcript read"))
    monkeypatch.setattr(capture, "_agent", lambda: pytest.fail("no transcript read"))
    monkeypatch.setattr(capture, "transcript_markdown", lambda *a, **k: pytest.fail("no transcript read"))
    return capture


def test_an_idle_tick_calls_no_model_and_reads_no_transcript(expert, monkeypatch, tmp_path):
    capture = _idle_tick_fakes(monkeypatch)
    raw = dict(expert.meta.get("expert") or {})
    raw["offered_to"] = {"targets": ["*"]}  # every session is relevant: capture must list them
    expert.meta["expert"] = raw
    fresh, old = tmp_path / "fresh.jsonl", tmp_path / "old.jsonl"
    fresh.write_text('{"x": 1}\n')
    old.write_text('{"x": 1}\n')
    os.utime(old, (time.time() - 10 * 86400,) * 2)  # past the lookback window
    listed = []
    tasks = _Tasks([{"session_id": "s-fresh", "path": str(fresh)}, {"session_id": "s-old", "path": str(old)}])
    monkeypatch.setattr(capture, "_tasks", lambda: listed.append(1) or tasks)
    out = scheduler.tick()
    assert out["ran"] and out["errors"] == 0 and listed  # sessions were listed (stat only)
    assert out["capture"]["sessions"] == 0 and out["consolidated"] == []


def test_an_idle_tick_skips_capture_when_no_expert_takes_sessions(expert, monkeypatch):
    capture = _idle_tick_fakes(monkeypatch)
    monkeypatch.setattr(capture, "capture_due", lambda **k: pytest.fail("no session listing"))
    monkeypatch.setattr(capture, "_tasks", lambda: pytest.fail("no session listing"))
    out = scheduler.tick()  # not offered anywhere, never consulted
    assert out["ran"] and out["errors"] == 0 and out["capture"]["skipped"]
