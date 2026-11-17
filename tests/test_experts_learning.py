"""How experts learn (docs/experts.md "How the loop learns"): a verified user correction takes effect at once
(trust user, via session; replace / narrow / unparseable; the skill follows), gap replies are classified (skip /
snooze / answer), only independent evidence raises a count, and the proposals made from a correction. No real
model: `model.text` answers from a queue (`fake_text`, from test_experts_core)."""
from __future__ import annotations

import json
import time

import pytest

from test_experts_core import FakeBot, fake_text  # noqa: F401  (fixture)

from fused_render.experts import cards, consolidate, memory, observations, proposals, store


@pytest.fixture
def bot(tmp_path):
    return FakeBot(tmp_path)


@pytest.fixture
def requests(monkeypatch):
    """`proposals.request` recorded instead of drafting in a thread."""
    calls = []

    def request(b, items, *, topic="", gap="", correction=None):
        if any(p.get("status") == "preparing" for p in proposals.listing(b)):
            raise ValueError(proposals.BUSY)
        pid = store.new_id("p")
        rec = {"id": pid, "ts": time.time(), "status": "preparing", "items": [i if isinstance(i, str) else i["id"]
                                                                               for i in items]}
        if correction:
            rec.update(reason="correction", correction=correction)
        proposals._save(b, rec)
        calls.append({"items": rec["items"], "topic": topic, "correction": correction, "id": pid})
        return rec

    monkeypatch.setattr(proposals, "request", request)
    return calls


OLD = "A chart node's title is '<Region> — Monthly revenue'."
NEW = "A chart node's title on a regional canvas is just the region name."
SAID = "That is wrong for regional canvases. A chart node's title is just the region name."


def _correction(bot, contradicts="", session="live", quote=SAID, **kw):
    return observations.write(bot, kind="advice_contradicted" if contradicts else "user_correction", claim=NEW,
                              trust="outcome", session=session, contradicts=contradicts, by_user=True, quote=quote,
                              **kw)


def _old(bot, n=2):
    it = memory.add(bot, OLD, kind="lesson", session="s0")
    for k in range(1, n):
        memory.bump(bot, it["id"], session=f"s{k}")
    return memory.items(bot)[0]


# ----------------------------------------------------------- part 1: corrections ---

def test_a_verified_correction_is_a_user_lesson_used_at_once(bot, fake_text, requests):
    o = _correction(bot)
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    r = consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert (item["claim"], item["trust"], item["via"], item["count"], item["kind"]) == (
        NEW, "user", "session", 1, "user_correction")
    assert memory.indexed(item) and item["id"] in memory.index_text(bot) and r["added"] == [item["id"]]


def test_an_unverified_correction_is_ordinary_session_evidence(bot, fake_text):
    o = observations.write(bot, kind="user_correction", claim=NEW, trust="outcome", session="live", by_user=False)
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert item["trust"] == "outcome" and item["via"] == "" and item["count"] == 0 and not memory.indexed(item)
    # A by_user flag without its quote proves nothing either.
    assert not consolidate.verified({"by_user": True}) and consolidate.verified({"by_user": True, "quote": SAID})


def test_replace_archives_the_old_lesson_as_corrected(bot, fake_text, requests):
    old = _old(bot)
    o = _correction(bot, contradicts=old["id"])
    fake_text.replies += [lambda p: json.dumps([{"obs": o["id"], "op": "add"}]),
                          lambda p: '```json\n{"resolution": "replace"}\n```']
    r = consolidate.run(bot, force=True)
    prompt = fake_text.calls[1]
    assert OLD in prompt and NEW in prompt and SAID in prompt and '"narrow"' in prompt
    new = memory.items(bot)
    assert [i["claim"] for i in new] == [NEW] and new[0]["trust"] == "user"
    arch = memory.archived(bot)[0]
    assert (arch["id"], arch["reason"], arch["into"]) == (old["id"], "corrected", new[0]["id"])
    assert r["resolved"] == [{"item": old["id"], "resolution": "replace", "into": new[0]["id"]}]
    assert r["contradicted"] == [] and OLD not in memory.index_text(bot)
    out = observations.done(bot)[0]["outcome"]
    assert (out["op"], out["item"], out["new"], out["resolution"]) == ("contradict", old["id"], new[0]["id"],
                                                                       "replace")
    assert requests == []  # the old rule was never in a skill


def test_narrow_rewrites_the_old_lesson_and_keeps_its_history(bot, fake_text, requests):
    old = _old(bot)
    memory.contradict(bot, old["id"], session="earlier")
    narrowed = "On the company-wide dashboard a chart node's title is '<Region> — Monthly revenue'."
    o = _correction(bot, contradicts=old["id"])
    fake_text.replies += [lambda p: json.dumps([{"obs": o["id"], "op": "add"}]),
                          lambda p: json.dumps({"resolution": "narrow", "narrowed_claim": narrowed})]
    r = consolidate.run(bot, force=True)
    items = {i["id"]: i for i in memory.items(bot)}
    kept = items[old["id"]]
    assert kept["claim"] == narrowed and kept["contradicted"] == 0 and kept["contradicted_sessions"] == []
    assert [h["claim"] for h in kept["history"]] == [OLD] and kept["history"][0]["at"] > 0
    assert kept["count"] == 2  # narrowing is not evidence
    assert sorted(i["claim"] for i in items.values()) == sorted([narrowed, NEW])  # the correction stands beside it
    assert r["resolved"][0]["resolution"] == "narrow"


@pytest.mark.parametrize("answer", ["I am not sure", '{"resolution": "narrow"}', '{"resolution": "delete"}',
                                    RuntimeError("down")])
def test_an_unparseable_resolution_keeps_the_contradiction_mark(bot, fake_text, requests, answer):
    old = _old(bot)
    o = _correction(bot, contradicts=old["id"])

    def resolution(p):
        if isinstance(answer, Exception):
            raise answer
        return answer
    fake_text.replies += [lambda p: json.dumps([{"obs": o["id"], "op": "add"}]), resolution]
    r = consolidate.run(bot, force=True)
    items = {i["id"]: i for i in memory.items(bot)}
    assert items[old["id"]]["claim"] == OLD and items[old["id"]]["contradicted"] == 1  # nothing lost
    assert r["contradicted"] == [old["id"]] and r["resolved"] == []
    assert "resolution" not in observations.done(bot)[0]["outcome"]


def test_an_unverified_contradiction_asks_no_resolution(bot, fake_text):
    old = _old(bot)
    o = observations.write(bot, kind="advice_contradicted", claim=NEW, trust="outcome", session="live",
                           contradicts=old["id"], by_user=False)
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    r = consolidate.run(bot, force=True)  # a second model call would fail the fake ("unexpected model call")
    assert len(fake_text.calls) == 1 and r["contradicted"] == [old["id"]]


def test_live_case_the_users_correction_takes_effect_and_the_skill_follows(bot, fake_text, requests):
    """The audit's session: the old rule was in use (seen twice) and written into the skill "charts"; the user said
    SAID. Before: stored as a session lesson seen once (unused), the old one kept with a mark, no skill change."""
    old = _old(bot)
    data = store.read_json(memory._path(bot), {})
    data["items"][0]["skill"] = "charts"  # a live lesson that carries the skill it was written into
    store.write_json(memory._path(bot), data)
    contra = _correction(bot, contradicts=old["id"])
    corr = _correction(bot)
    fake_text.replies += [lambda p: json.dumps([{"obs": contra["id"], "op": "same", "as": corr["id"]},
                                                {"obs": corr["id"], "op": "add"}]),
                          lambda p: json.dumps({"resolution": "replace"})]
    r = consolidate.run(bot, force=True)
    [new] = memory.items(bot)
    assert (new["claim"], new["trust"], new["via"]) == (NEW, "user", "session")
    index = memory.index_text(bot)
    assert NEW in index and OLD not in index  # the next session gets the corrected rule, not the old one
    assert memory.archived(bot)[0]["reason"] == "corrected"
    assert requests == [{"items": [new["id"]], "topic": requests[0]["topic"], "id": requests[0]["id"],
                         "correction": {"skill": "charts", "old": OLD, "quote": SAID}}]
    assert r["corrections"] == [requests[0]["id"]] and requests[0]["id"] in r["proposed"]


def test_a_rule_already_promoted_into_a_skill_gets_a_correction_proposal(bot, fake_text, requests):
    old = _old(bot)
    memory.remove(bot, [old["id"]], "promoted", skill="charts")
    assert memory.promoted_ids(bot) == {old["id"]}
    o = _correction(bot, contradicts=old["id"])
    assert o["contradicts"] == old["id"]  # an archived promoted lesson may be named
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    consolidate.run(bot, force=True)  # nothing live to resolve: no resolution call
    [new] = memory.items(bot)
    assert requests[0]["correction"]["skill"] == "charts" and requests[0]["items"] == [new["id"]]
    assert observations.done(bot)[0]["outcome"]["item"] == old["id"]


def test_a_correction_waits_in_the_queue_while_a_proposal_is_open(bot, fake_text, requests):
    old = _old(bot)
    memory.remove(bot, [old["id"]], "promoted", skill="charts")
    proposals._save(bot, {"id": "pbusy", "ts": time.time(), "status": "ready", "items": []})
    o = _correction(bot, contradicts=old["id"])
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    r = consolidate.run(bot, force=True)
    assert requests == [] and r["corrections"] == []
    queued = consolidate.queued_corrections(bot)
    assert [(q["skill"], q["old"]) for q in queued] == [("charts", OLD)]
    assert consolidate.would_run(bot, force=True)
    # Still open at the next run: it stays queued.
    assert consolidate.drain_corrections(bot) == [] and len(consolidate.queued_corrections(bot)) == 1
    p = proposals.get(bot, "pbusy")
    p["status"] = "rejected"
    proposals._save(bot, p)
    r = consolidate.run(bot, force=True)
    assert r["corrections"] == [requests[0]["id"]] and requests[0]["correction"]["skill"] == "charts"
    assert consolidate.queued_corrections(bot) == []


def test_queued_corrections_are_kept_per_lesson_and_skill(bot, monkeypatch):
    proposals._save(bot, {"id": "pbusy", "ts": time.time(), "status": "ready", "items": []})
    for skill in ("charts", "titles", "charts"):
        assert consolidate.request_correction(bot, {"item": "m1", "skill": skill, "old": OLD}) is None
    assert sorted(q["skill"] for q in consolidate.queued_corrections(bot)) == ["charts", "titles"]
    # The BUSY path (another proposal claimed the slot between the check and the request) keeps the same identity.
    p = proposals.get(bot, "pbusy")
    p["status"] = "rejected"
    proposals._save(bot, p)

    def busy(*a, **k):
        raise ValueError(proposals.BUSY)

    monkeypatch.setattr(proposals, "request", busy)
    assert consolidate.request_correction(bot, {"item": "m1", "skill": "loaders", "old": OLD}) is None
    assert consolidate.request_correction(bot, {"item": "m1", "skill": "titles", "old": OLD}) is None
    assert sorted(q["skill"] for q in consolidate.queued_corrections(bot)) == ["charts", "loaders", "titles"]


def test_a_confirmation_never_bumps_a_lesson_the_batch_contradicts(bot, fake_text):
    old = memory.add(bot, OLD, kind="lesson", session="s0")
    contra = observations.write(bot, kind="advice_contradicted", claim=NEW, trust="outcome", session="live",
                                contradicts=old["id"], by_user=False)
    observations.write(bot, kind="error_then_fix", claim=OLD, trust="outcome", session="other",
                       confirms=old["id"])
    fake_text.replies.append(lambda p: json.dumps([{"obs": contra["id"], "op": "add"}]))
    r = consolidate.run(bot, force=True)
    kept = next(i for i in memory.items(bot) if i["id"] == old["id"])
    assert kept["count"] == 1 and kept["contradicted"] == 1 and not memory.indexed(kept)
    assert old["id"] not in r["bumped"] and r["contradicted"] == [old["id"]]


def test_a_verified_correction_confirming_a_lesson_makes_it_a_user_lesson(bot, fake_text):
    # Seen once in this same session: the count cannot rise, yet the user's statement must take effect at once.
    lesson = memory.add(bot, NEW, kind="user_correction", session="live")
    _correction(bot, confirms=lesson["id"])
    r = consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert (item["trust"], item["via"], item["count"]) == ("user", "session", 1)
    assert memory.indexed(item) and r["bumped"] == [lesson["id"]] and fake_text.calls == []
    # The model-chosen bump of another lesson upgrades it too.
    other = memory.add(bot, "Region charts use one line per region.", session="s0")
    o = _correction(bot, session="s9")
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "bump", "item": other["id"]}]))
    consolidate.run(bot, force=True)
    item = next(i for i in memory.items(bot) if i["id"] == other["id"])
    assert (item["trust"], item["via"], item["count"]) == ("user", "session", 2) and memory.indexed(item)


def test_a_correction_draft_targets_the_skill_and_its_card_says_so(bot, fake_text):
    bot.skill_save("Charts", "chart, title", "## Titles\n\nA chart node's title is '<Region> — Monthly revenue'.\n\n"
                                             "## Colours\n\nUse the brand palette.", name="charts")
    bot.skill_save("Loaders", "loader, parquet", "## Loading\n\nUse a loader node.", name="loaders")
    new = memory.add(bot, NEW, kind="user_correction", trust="user", via="session")
    fake_text.replies.append(lambda p: json.dumps({"action": "update", "skill": "charts", "ops": [
        {"op": "replace", "heading": "Titles", "section": "On a regional canvas the title is the region name."}],
        "why": "the user corrected it"}))
    fields = proposals._draft(bot, [new], correction={"skill": "charts", "old": OLD, "quote": SAID})
    prompt = fake_text.calls[0]
    assert "THE USER CORRECTED A RULE" in prompt and SAID in prompt and "Do NOT only" in prompt
    assert "name: charts" in prompt  # that skill is shown in full
    assert fields["reason"] == "correction" and fields["skill"] == "charts" and "-A chart node's" in fields["diff"]
    text, _detail = proposals.card_text({**fields, "reason": "correction"})
    assert text.startswith("You corrected something a skill says. I'd like to change it:\n- " + NEW)
    # A draft that does not change that skill is refused rather than leaving the old rule in place.
    fake_text.replies.append(lambda p: json.dumps({"action": "create", "title": "Regions", "trigger": "region",
                                                   "body": "x"}))
    with pytest.raises(proposals.Unfit):
        proposals._draft(bot, [new], correction={"skill": "charts", "old": OLD, "quote": SAID})


# ----------------------------------------------------------- part 1: gap replies ---

@pytest.mark.parametrize("reply,verdict", [
    ("skip", "skip"), ("Drop it", "skip"), ("no", "skip"), ("nope.", "skip"),
    ("later", "snooze"), ("Not now", "snooze"), ("ask me later", "snooze"), ("Remind me tomorrow", "snooze"),
    ("I don't know", "snooze"), ("I don’t know yet", "snooze"), ("not sure", "snooze"), ("No idea!", "snooze"),
    ("Hmm, I'm not sure", "snooze"), ("idk", "snooze"), ("maybe later, sorry", "snooze"),
    ("It lives in team/shared/loaders.py", "answer"),
    ("No, it lives in the team folder under shared", "answer"),
    ("I don't know the name but it is in the team folder under shared", "answer"),  # 8 words or more
])
def test_gap_replies_are_classified_before_anything_is_saved(reply, verdict):
    assert cards.classify_reply(reply) == verdict


def test_a_snoozed_gap_is_not_asked_or_researched_until_then(bot, monkeypatch):
    from fused_render.experts import gaps
    g = gaps.record(bot, "Where do regional canvases live?", consult="c1")
    gaps.mark_asked(bot, g["id"], 7)
    before = len(memory.items(bot))
    out = cards.snooze_gap(bot, g["id"])
    assert out["status"] == "open" and out["card_seq"] is None
    assert 6.9 * 86400 < out["snoozed_until"] - time.time() <= 7 * 86400
    assert len(memory.items(bot)) == before  # nothing saved
    assert consolidate.snoozed(out) and not consolidate.snoozed({**out, "snoozed_until": time.time() - 1})
    assert cards.ask_gap(bot, gaps.get(bot, g["id"])) is False  # never asked while snoozed
    monkeypatch.setattr(gaps, "repairable", lambda b, touched=(): [gaps.get(b, g["id"])])
    assert consolidate._repairable(bot) == []  # never researched while snoozed


# ------------------------------------------------- part 2: independent evidence ---

def test_advice_confirmed_does_not_raise_the_count(bot, fake_text):
    lesson = memory.add(bot, "Use fused.load to chain UDFs.", session="s0")
    for s in ("s1", "s1", "s2"):
        observations.write(bot, kind="advice_confirmed", claim=lesson["claim"], trust="outcome", session=s,
                           confirms=lesson["id"], by_user=False)
    r = consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert item["count"] == 1 and item["confirmed"] == 2 and item["last_used"] and not memory.indexed(item)
    assert r["bumped"] == [] and r["confirmed"] == [lesson["id"]]
    assert {o["outcome"]["op"] for o in observations.done(bot)} == {"confirm"}
    assert fake_text.calls == []


def test_after_advice_echo_is_noted_not_counted_and_grounded_requests_count(bot, fake_text):
    req = memory.add(bot, "The user asks for a regional revenue chart.", kind="repeated_request", session="s0")
    echo = observations.write(bot, kind="repeated_request", claim=req["claim"], trust="outcome", session="s1",
                              confirms=req["id"], by_user=True, quote="make a regional revenue chart",
                              after_advice=True)
    ungrounded = observations.write(bot, kind="repeated_request", claim=req["claim"], trust="outcome",
                                    session="s2", confirms=req["id"], by_user=False)
    fixed = observations.write(bot, kind="error_then_fix", claim="Pin pandas<3 for the loader.", trust="outcome",
                               session="s3")
    real = observations.write(bot, kind="repeated_request", claim=req["claim"], trust="outcome", session="s4",
                              confirms=req["id"], by_user=True, quote="make a regional revenue chart")
    new_echo = observations.write(bot, kind="user_correction", claim="Use the region name as the title.",
                                  trust="outcome", session="s5", by_user=True, quote="use the region name",
                                  after_advice=True)
    fake_text.replies.append(lambda p: json.dumps([{"obs": fixed["id"], "op": "add"},
                                                   {"obs": new_echo["id"], "op": "add"}]))
    consolidate.run(bot, force=True)
    items = {i["claim"]: i for i in memory.items(bot)}
    r = items[req["claim"]]
    assert r["count"] == 2 and r["sessions"] == ["s0", "s4"] and r["confirmed"] == 2  # echo + ungrounded
    assert items["Pin pandas<3 for the loader."]["count"] == 1
    e = items["Use the region name as the title."]
    assert e["count"] == 0 and e["confirmed"] == 1 and e["trust"] == "outcome"  # an echo is not a correction
    done = {o["id"]: o["outcome"]["op"] for o in observations.done(bot)}
    assert (done[echo["id"]], done[ungrounded["id"]], done[real["id"]]) == ("confirm", "confirm", "bump")


def test_use_in_answers_and_proposals_read_count_only(bot):
    bot.meta["expert"] = {"memory": {"propose_at": 3}}
    it = memory.add(bot, "Confirmed a lot, never independently seen twice.", session="s0")
    for s in range(6):
        memory.confirm(bot, it["id"], session=f"c{s}")
    memory.confirm(bot, it["id"], session="c0")  # once per distinct session
    item = memory.items(bot)[0]
    assert item["confirmed"] == 6 and item["count"] == 1
    assert not memory.indexed(item) and proposals.candidates(bot) == []
    for s in ("s1", "s2"):
        memory.bump(bot, it["id"], session=s)
    assert memory.indexed(memory.items(bot)[0]) and [i["id"] for i in proposals.candidates(bot)] == [it["id"]]


def test_counts_rule_table():
    c = consolidate.counts
    assert c({"kind": "error_then_fix", "by_user": False})
    assert not c({"kind": "advice_confirmed"}) and not c({"kind": "advice_confirmed", "by_user": True, "quote": "x"})
    assert c({"kind": "user_correction", "by_user": True, "quote": "q"})
    assert not c({"kind": "user_correction", "by_user": True, "quote": "q", "after_advice": True})
    assert not c({"kind": "repeated_request", "by_user": False})
    assert c({"kind": "repeated_request"})  # stored before grounding existed: the old rule
