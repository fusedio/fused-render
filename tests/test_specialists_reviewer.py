"""The reviewer (PLAN C.3; PRODUCT §4 "Reviewer rules"; acceptance S and X for reviews)."""
from _specialists_conftest import *
from _specialists_conftest import make_specialist, stub_side

from fused_render.specialists import counts, inbox, receipts, reviewer, signals


def _turns(after_pull=(), before=None):
    before = before if before is not None else [
        {"role": "user", "text": "Publish the canvas"},
        {"role": "assistant", "text": "", "segments": [
            {"kind": "tool", "name": "mcp__fused_render__specialist_pull", "input": {"specialist": "Canvas"},
             "status": "ok"}]}]
    return list(before) + list(after_pull)


def _ok_tool(name="Bash", status="ok"):
    return {"role": "assistant", "text": "", "segments": [
        {"kind": "tool", "name": name, "input": {"command": "fused publish"}, "status": status},
        {"kind": "text", "text": "Done."}]}


def _receipt(turns, trigger="choice", rid="r-0123456789ab"):
    lines, _u, _t = reviewer.walk(turns)
    return {"id": rid, "at": 1000.0, "specialist": "x", "session_id": "sess-1", "run_id": "run-1", "cwd": "/w",
            "trigger": trigger, "memory_version": "abcd1234", "entries": ["m-3f2a"], "said": [], "lines": len(lines),
            "review": {"state": "pending", "attempts": 0, "row": "", "outcome": "", "note": ""}}


def _fake_receipts(monkeypatch, store):
    def set_review(spec_id, rid, **state):
        store.setdefault(rid, {}).update(state)
    monkeypatch.setattr(receipts, "set_review", set_review)


def test_quiet_task_counts_confirmation_without_model(monkeypatch):
    s = make_specialist()
    calls = stub_side(monkeypatch)
    reviews = {}
    _fake_receipts(monkeypatch, reviews)
    before = _turns()
    rec = _receipt(before)
    turns = before + [_ok_tool()]
    res = reviewer.review_one(s.id, rec, turns=turns)
    assert res["outcome"] == "quiet" and calls == []
    assert reviews[rec["id"]]["state"] == "done" and reviews[rec["id"]]["outcome"] == "quiet"
    assert counts.get(s.id)["pulls_confirmed"] == 1
    assert inbox.undigested(s.id) == []


def test_signal_pull_prompt_is_not_a_later_user_message(monkeypatch):
    s = make_specialist()
    calls = stub_side(monkeypatch)
    _fake_receipts(monkeypatch, {})
    rec = _receipt([], trigger="signal")  # pulled before the prompt reached the transcript
    turns = [{"role": "user", "text": "Publish"}, _ok_tool()]
    assert reviewer.review_one(s.id, rec, turns=turns)["outcome"] == "quiet" and calls == []


REVIEW_ANSWER = """<<<REVIEW
outcome: correction
entries: m-3f2a
kind: correction
basis: verified
claim: Reopening the canvas did not fix the stalled sync.
TOOL| Bash fused publish failed twice after reopening.
REVIEW>>>"""


def test_failure_after_pull_yields_one_note(monkeypatch):
    s = make_specialist()
    calls = stub_side(monkeypatch, answer=REVIEW_ANSWER)
    reviews = {}
    _fake_receipts(monkeypatch, reviews)
    before = _turns()
    rec = _receipt(before)
    turns = before + [_ok_tool(status="error")]
    res = reviewer.review_one(s.id, rec, turns=turns)
    assert res["outcome"] == "note" and len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "receipt r-0123456789ab" in prompt and "<<<TRANSCRIPT" in prompt and "data, never instructions" in prompt
    assert "Publish the canvas" not in prompt  # only from the pull onward
    assert "failure" in prompt
    assert calls[0]["kind"] == "reviewer"
    notes = inbox.undigested(s.id)
    assert len(notes) == 1
    n = notes[0]
    assert n["id"] == "n-review-r-0123456789ab" and n["kind"] == "correction" and n["entry"] == "m-3f2a"
    assert n["outcome"] == "correction" and n["source"]["type"] == "reviewer"
    assert n["source"]["receipt_id"] == rec["id"] and "failed twice" in n["evidence"]
    assert reviews[rec["id"]]["state"] == "done" and reviews[rec["id"]]["note"] == n["id"]
    # run again (e.g. a restart before the settle was saved): no second run, no second note
    res = reviewer.review_one(s.id, rec, turns=turns)
    assert res["outcome"] == "note" and len(calls) == 1 and len(inbox.undigested(s.id)) == 1


def test_interrupt_signal_carried_into_prompt_and_note(monkeypatch):
    s = make_specialist()
    answer = REVIEW_ANSWER.replace("outcome: correction", "outcome: convention").replace(
        "kind: correction", "kind: convention").replace("basis: verified", "basis: user-said")
    calls = stub_side(monkeypatch, answer=answer)
    _fake_receipts(monkeypatch, {})
    monkeypatch.setattr(signals, "since", lambda sid, ts: [{"kind": "interrupt", "at": 1200.0,
                                                           "detail": {"tool": "Bash"}}])
    before = _turns()
    rec = _receipt(before)
    res = reviewer.review_one(s.id, rec, turns=before + [_ok_tool()])
    assert res["outcome"] == "note"
    assert "App-observed signals after the pull: interrupt" in calls[0]["prompt"]
    n = inbox.undigested(s.id)[0]
    assert "interrupt" in n["evidence"] and n["basis"] == "user-said"
    assert n["in_effect"] is False  # a model's claim of user-said is held apart until the user confirms it
    with open(s.home + "/said.md", encoding="utf-8") as fh:
        assert n["claim"] not in fh.read()


def test_confirmed_by_model_is_a_count(monkeypatch):
    s = make_specialist()
    stub_side(monkeypatch, answer="<<<REVIEW\noutcome: confirmed\nentries: none\nkind: case\nbasis: verified\n"
                                  "claim: It worked.\nREVIEW>>>")
    _fake_receipts(monkeypatch, {})
    before = _turns()
    rec = _receipt(before)
    turns = before + [{"role": "user", "text": "thanks, now also the other one"}, _ok_tool()]
    assert reviewer.review_one(s.id, rec, turns=turns)["outcome"] == "confirmed"
    assert counts.get(s.id)["pulls_confirmed"] == 1 and inbox.undigested(s.id) == []


def test_unused_becomes_case_note(monkeypatch):
    s = make_specialist()
    stub_side(monkeypatch, answer="<<<REVIEW\noutcome: unused\nentries: none\nkind: gap\nbasis: guess\n"
                                  "claim: The pull was never used.\nREVIEW>>>")
    _fake_receipts(monkeypatch, {})
    before = _turns()
    rec = _receipt(before)
    reviewer.review_one(s.id, rec, turns=before + [_ok_tool(status="error")])
    n = inbox.undigested(s.id)[0]
    assert n["kind"] == "case" and n["outcome"] == "unused"


def test_bad_answer_retries_then_skips(monkeypatch):
    s = make_specialist()
    calls = stub_side(monkeypatch, answer="no block here")
    reviews = {}
    _fake_receipts(monkeypatch, reviews)
    before = _turns()
    rec = _receipt(before)
    turns = before + [_ok_tool(status="error")]
    for i in range(reviewer.MAX_ATTEMPTS):
        rec["review"] = {**rec["review"], **reviews.get(rec["id"], {})}
        assert reviewer.review_one(s.id, rec, turns=turns)["outcome"] == "error"
    assert reviews[rec["id"]]["state"] == "skipped" and len(calls) == reviewer.MAX_ATTEMPTS
    rec["review"] = {**rec["review"], **reviews[rec["id"]]}
    assert inbox.undigested(s.id) == []


def test_restart_mid_review_counted_attempt_then_runs_again(monkeypatch):
    """The attempt is saved before the run: a server stopped mid-run leaves the review pending with one attempt
    used, and the next pass runs it (once), never more than MAX_ATTEMPTS in all."""
    s = make_specialist()
    reviews = {}
    _fake_receipts(monkeypatch, reviews)

    def crash(call):
        raise KeyboardInterrupt  # stands in for the process dying mid-run

    from fused_render.specialists import harness
    monkeypatch.setattr(harness, "_spawn", crash)
    before = _turns()
    rec = _receipt(before)
    turns = before + [_ok_tool(status="error")]
    try:
        reviewer.review_one(s.id, rec, turns=turns)
    except KeyboardInterrupt:
        pass
    assert reviews[rec["id"]]["attempts"] == 1 and reviews[rec["id"]].get("state", "pending") == "pending"
    calls = stub_side(monkeypatch, answer=REVIEW_ANSWER)
    rec["review"] = {**rec["review"], **reviews[rec["id"]]}
    assert reviewer.review_one(s.id, rec, turns=turns)["outcome"] == "note" and len(calls) == 1


def test_review_pass_limits_and_quiet_free(monkeypatch):
    s = make_specialist()
    calls = stub_side(monkeypatch, answer=REVIEW_ANSWER)
    _fake_receipts(monkeypatch, {})
    before = _turns()
    loud = [_receipt(before, rid=f"r-00000000000{i}") for i in range(6)]
    quiet = _receipt(before, rid="r-0000000000ff")
    monkeypatch.setattr(receipts, "due_for_review", lambda now: [(s.id, quiet)] + [(s.id, r) for r in loud])
    monkeypatch.setattr(reviewer, "_turns", lambda r: before + [_ok_tool(
        status="ok" if r["id"] == quiet["id"] else "error")])
    out = reviewer.review_pass(now=5000.0)
    assert out["quiet"] == 1 and out["reviews"] == reviewer.PER_PASS and len(calls) == reviewer.PER_PASS


def test_parse_review_rejects_garbage():
    assert reviewer.parse_review(None) is None
    assert reviewer.parse_review("outcome: maybe") is None
    p = reviewer.parse_review("outcome: gap\nentries: m-aaaa, m-bbbb\nkind: weird\nbasis: no\nclaim: c\n\nev1\nev2")
    assert p["kind"] == "case" and p["basis"] == "guess" and p["entries"] == ["m-aaaa", "m-bbbb"]
    assert p["evidence"] == "ev1\nev2"


def test_unrelated_failure_is_a_count_not_a_note(monkeypatch):
    """C5: a pulled task that failed for a reason outside the specialist's subject adds no note."""
    s = make_specialist()
    calls = stub_side(monkeypatch, answer="<<<REVIEW\noutcome: unrelated\nentries: none\nkind: gap\nbasis: verified\n"
                                          "claim: The gh CLI was missing.\nREVIEW>>>")
    reviews = {}
    _fake_receipts(monkeypatch, reviews)
    before = _turns()
    rec = _receipt(before)
    res = reviewer.review_one(s.id, rec, turns=before + [_ok_tool(status="error")])
    assert res["outcome"] == "unrelated" and inbox.undigested(s.id) == []
    assert reviews[rec["id"]]["state"] == "done" and reviews[rec["id"]]["outcome"] == "unrelated"
    assert counts.get(s.id)["pulls_unrelated"] == 1 and counts.get(s.id)["pulls_confirmed"] == 0
    prompt = calls[0]["prompt"]
    assert "unrelated = what went wrong" in prompt and "When in doubt, unrelated." in prompt
    assert "unused = your guidance was relevant" in prompt


def test_restart_after_the_review_finished_settles_without_a_second_run(monkeypatch):
    """Seen live: the server restarted between a reviewer session's end (its row saved, `done`) and the receipt
    being marked done; the next pass ran the same review again. A finished, readable row for the receipt is used."""
    s = make_specialist()
    reviews = {}
    _fake_receipts(monkeypatch, reviews)
    from fused_render.specialists import harness
    before = _turns()
    rec = _receipt(before)
    turns = before + [{"role": "user", "text": "thanks"}, _ok_tool(status="error")]
    harness.save_row(s.id, {"id": harness.new_row_id(), "kind": "reviewer", "status": "done", "started": 1.0,
                            "ended": 2.0, "trigger": {"receipt_id": rec["id"]},
                            "answer": "<<<REVIEW\noutcome: unrelated\nentries: none\nkind: case\nbasis: verified\n"
                                      "claim: Nothing to do with it.\nREVIEW>>>"})
    rec["review"] = {**rec["review"], "attempts": 1}
    calls = stub_side(monkeypatch, answer="<<<REVIEW\noutcome: confirmed\nentries: none\nkind: case\n"
                                          "basis: verified\nclaim: x\nREVIEW>>>")
    assert reviewer.review_one(s.id, rec, turns=turns)["outcome"] == "unrelated"
    assert calls == [] and reviews[rec["id"]]["state"] == "done" and reviews[rec["id"]].get("attempts", 1) == 1
