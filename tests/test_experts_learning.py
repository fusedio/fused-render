"""How experts learn (docs/experts-autonomous.md "Learn : one loop"): a verified user correction takes effect at
once (trust user, via session; replace / narrow / unparseable), only independent evidence raises a count, lessons
that earned a skill are written into it at once (distill), a corrected skill rule is rewritten, and everything is
on the activity log. No real model: `model.text` answers from a queue (`fake_text`, from test_experts_core)."""
from __future__ import annotations

import json
import os

import pytest

from test_experts_core import FakeBot, _acts, _done, fake_text  # noqa: F401  (fixture)

from fused_render.experts import consolidate, distill, knowledge, memory, observations


@pytest.fixture
def bot(tmp_path):
    return FakeBot(tmp_path)


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


def _archive(bot):
    from fused_render.experts import store
    return store.read_jsonl(os.path.join(store.expert_dir(bot), memory.ARCHIVE_FILE))


# ----------------------------------------------------------- part 1: corrections ---

def test_a_verified_correction_is_a_user_lesson_used_at_once(bot, fake_text):
    o = _correction(bot)
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert (item["claim"], item["trust"], item["via"], item["count"], item["kind"]) == (
        NEW, "user", "session", 1, "user_correction")
    assert memory.indexed(item) and item["id"] in memory.index_text(bot)
    (line,) = knowledge.activity(bot)
    assert (line["action"], line["target"], line["why"]) == ("learned", item["id"], "You said so in a session.")
    assert line["evidence"] == {"session": "live", "quote": SAID, "item": o["id"]} and line["undoable"]


def test_an_unverified_correction_is_ordinary_session_evidence(bot, fake_text):
    o = observations.write(bot, kind="user_correction", claim=NEW, trust="outcome", session="live", by_user=False)
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert item["trust"] == "outcome" and item["via"] == "" and item["count"] == 0 and not memory.indexed(item)
    # A by_user flag without its quote proves nothing either.
    assert not consolidate.verified({"by_user": True}) and consolidate.verified({"by_user": True, "quote": SAID})


def test_replace_archives_the_old_lesson_as_corrected(bot, fake_text):
    old = _old(bot)
    o = _correction(bot, contradicts=old["id"])
    fake_text.replies += [lambda p: json.dumps([{"obs": o["id"], "op": "add"}]),
                          lambda p: '```json\n{"resolution": "replace"}\n```']
    consolidate.run(bot, force=True)
    prompt = fake_text.calls[1]
    assert OLD in prompt and NEW in prompt and SAID in prompt and '"narrow"' in prompt
    new = memory.items(bot)
    assert [i["claim"] for i in new] == [NEW] and new[0]["trust"] == "user"
    arch = _archive(bot)[0]
    assert (arch["id"], arch["reason"], arch["into"]) == (old["id"], "corrected", new[0]["id"])
    assert OLD not in memory.index_text(bot)
    assert _acts(bot) == [("learned", new[0]["id"]), ("removed", old["id"])]
    # Undo puts the corrected lesson back exactly as it was.
    knowledge.undo(bot, knowledge.activity(bot)[0]["id"])
    assert next(i for i in memory.items(bot) if i["id"] == old["id"])["count"] == 2
    out = _done(bot)[0]["outcome"]
    assert (out["op"], out["item"], out["new"], out["resolution"]) == ("contradict", old["id"], new[0]["id"],
                                                                       "replace")


def test_narrow_rewrites_the_old_lesson_and_keeps_its_history(bot, fake_text):
    old = _old(bot)
    memory.contradict(bot, old["id"], session="earlier")
    narrowed = "On the company-wide dashboard a chart node's title is '<Region> — Monthly revenue'."
    o = _correction(bot, contradicts=old["id"])
    fake_text.replies += [lambda p: json.dumps([{"obs": o["id"], "op": "add"}]),
                          lambda p: json.dumps({"resolution": "narrow", "narrowed_claim": narrowed})]
    consolidate.run(bot, force=True)
    items = {i["id"]: i for i in memory.items(bot)}
    kept = items[old["id"]]
    assert kept["claim"] == narrowed and kept["contradicted"] == 0 and kept["contradicted_sessions"] == []
    assert [h["claim"] for h in kept["history"]] == [OLD] and kept["history"][0]["at"] > 0
    assert kept["count"] == 2  # narrowing is not evidence
    assert sorted(i["claim"] for i in items.values()) == sorted([narrowed, NEW])  # the correction stands beside it
    assert _acts(bot)[-1] == ("corrected", old["id"]) and _done(bot)[0]["outcome"]["resolution"] == "narrow"


@pytest.mark.parametrize("answer", ["I am not sure", '{"resolution": "narrow"}', '{"resolution": "delete"}',
                                    RuntimeError("down")])
def test_an_unparseable_resolution_keeps_the_contradiction_mark(bot, fake_text, answer):
    old = _old(bot)
    o = _correction(bot, contradicts=old["id"])

    def resolution(p):
        if isinstance(answer, Exception):
            raise answer
        return answer
    fake_text.replies += [lambda p: json.dumps([{"obs": o["id"], "op": "add"}]), resolution]
    consolidate.run(bot, force=True)
    items = {i["id"]: i for i in memory.items(bot)}
    assert items[old["id"]]["claim"] == OLD and items[old["id"]]["contradicted"] == 1  # nothing lost
    assert "resolution" not in _done(bot)[0]["outcome"]


def test_an_unverified_contradiction_asks_no_resolution(bot, fake_text):
    old = _old(bot)
    o = observations.write(bot, kind="advice_contradicted", claim=NEW, trust="outcome", session="live",
                           contradicts=old["id"], by_user=False)
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "add"}]))
    consolidate.run(bot, force=True)  # a second model call would fail the fake ("unexpected model call")
    assert len(fake_text.calls) == 1 and memory.items(bot)[0]["contradicted"] == 1
    assert all(ln["ok"] for ln in knowledge.activity(bot))


def test_a_confirmation_never_bumps_a_lesson_the_batch_contradicts(bot, fake_text):
    old = memory.add(bot, OLD, kind="lesson", session="s0")
    contra = observations.write(bot, kind="advice_contradicted", claim=NEW, trust="outcome", session="live",
                                contradicts=old["id"], by_user=False)
    observations.write(bot, kind="error_then_fix", claim=OLD, trust="outcome", session="other",
                       confirms=old["id"])
    fake_text.replies.append(lambda p: json.dumps([{"obs": contra["id"], "op": "add"}]))
    consolidate.run(bot, force=True)
    kept = next(i for i in memory.items(bot) if i["id"] == old["id"])
    assert kept["count"] == 1 and kept["contradicted"] == 1 and not memory.indexed(kept)


def test_a_verified_correction_confirming_a_lesson_makes_it_a_user_lesson(bot, fake_text):
    # Seen once in this same session: the count cannot rise, yet the user's statement must take effect at once.
    lesson = memory.add(bot, NEW, kind="user_correction", session="live")
    _correction(bot, confirms=lesson["id"])
    consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert (item["trust"], item["via"], item["count"]) == ("user", "session", 1)
    assert memory.indexed(item) and fake_text.calls == []
    # The model-chosen bump of another lesson upgrades it too.
    other = memory.add(bot, "Region charts use one line per region.", session="s0")
    o = _correction(bot, session="s9")
    fake_text.replies.append(lambda p: json.dumps([{"obs": o["id"], "op": "bump", "item": other["id"]}]))
    consolidate.run(bot, force=True)
    item = next(i for i in memory.items(bot) if i["id"] == other["id"])
    assert (item["trust"], item["via"], item["count"]) == ("user", "session", 2) and memory.indexed(item)


# ----------------------------------------------------------- part 1: gap replies ---

def test_advice_confirmed_does_not_raise_the_count(bot, fake_text):
    lesson = memory.add(bot, "Use fused.load to chain UDFs.", session="s0")
    for s in ("s1", "s1", "s2"):
        observations.write(bot, kind="advice_confirmed", claim=lesson["claim"], trust="outcome", session=s,
                           confirms=lesson["id"], by_user=False)
    consolidate.run(bot, force=True)
    item = memory.items(bot)[0]
    assert item["count"] == 1 and item["confirmed"] == 2 and item["last_used"] and not memory.indexed(item)
    assert {o["outcome"]["op"] for o in _done(bot)} == {"confirm"}
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
    done = {o["id"]: o["outcome"]["op"] for o in _done(bot)}
    assert (done[echo["id"]], done[ungrounded["id"]], done[real["id"]]) == ("confirm", "confirm", "bump")


def test_use_in_answers_and_skills_read_count_only(bot):
    bot.meta["expert"] = {"memory": {"propose_at": 3}}
    it = memory.add(bot, "Confirmed a lot, never independently seen twice.", session="s0")
    for s in range(6):
        memory.confirm(bot, it["id"], session=f"c{s}")
    memory.confirm(bot, it["id"], session="c0")  # once per distinct session
    item = memory.items(bot)[0]
    assert item["confirmed"] == 6 and item["count"] == 1
    assert not memory.indexed(item) and distill.candidates(bot) == []
    for s in ("s1", "s2"):
        memory.bump(bot, it["id"], session=s)
    assert memory.indexed(memory.items(bot)[0]) and [i["id"] for i in distill.candidates(bot)] == [it["id"]]


def test_counts_rule_table():
    c = consolidate.counts
    assert c({"kind": "error_then_fix", "by_user": False})
    assert not c({"kind": "advice_confirmed"}) and not c({"kind": "advice_confirmed", "by_user": True, "quote": "x"})
    assert c({"kind": "user_correction", "by_user": True, "quote": "q"})
    assert not c({"kind": "user_correction", "by_user": True, "quote": "q", "after_advice": True})
    assert not c({"kind": "repeated_request", "by_user": False})
    assert c({"kind": "repeated_request"})  # stored before grounding existed: the old rule


# ------------------------------------------------------ part 3: lessons into skills ---

CHARTS = """Charts in canvases.

## Titles

A chart node's title is '<Region> — Monthly revenue'.

## Colours

Use the house palette:

```python
PALETTE = ["#113355", "#aa3300"]   # two   spaces, kept
```

## Axes

- Label both axes.
"""


def _skill(bot, name="charts", title="Charts", trigger="chart, title", body=CHARTS):
    os.makedirs(bot.skills_dir, exist_ok=True)
    with open(os.path.join(bot.skills_dir, name + ".md"), "w", encoding="utf-8") as f:
        f.write(f"# {title}\ntrigger: {trigger}\n\n{body.strip()}\n")


def _file(bot, name="charts"):
    with open(os.path.join(bot.skills_dir, name + ".md"), "rb") as f:
        return f.read().decode()


def _section(text, heading):
    """The bytes of one section: from its heading line up to the next heading (or the end)."""
    at = text.index(f"## {heading}\n")
    nxt = text.find("\n## ", at + 1)
    return text[at:] if nxt == -1 else text[at:nxt + 1]


def test_a_lesson_seen_often_enough_is_written_into_a_skill_at_once(bot, fake_text):
    _skill(bot)
    lesson = _old(bot, n=3)  # count 3 = propose_at
    extra = memory.add(bot, "Seen once.", session="s0")
    fake_text.replies.append(lambda p: json.dumps({
        "action": "update", "skill": "charts", "why": "Titles keep coming up.",
        "ops": [{"op": "add", "heading": "Legends", "section": "Put the legend below the chart."}],
        "triggers_add": ["legend"]}))
    assert [i["id"] for i in distill.candidates(bot)] == [lesson["id"]]
    consolidate.run(bot, force=True)
    prompt = fake_text.calls[0]
    assert OLD in prompt and "<<<BEGIN SKILL BODY>>>" in prompt and "Seen once." not in prompt
    for must in ("Prefer a reference to a canvas file", "in the user's own words", "Keep the skill short",
                 "Use code only where code explains it best"):
        assert must in prompt, must
    assert '"move"' not in prompt
    text = _file(bot)
    assert "## Legends\n\nPut the legend below the chart.\n" in text and "trigger: chart, title, legend" in text
    assert [i["id"] for i in memory.items(bot)] == [extra["id"]]  # the skill carries the lesson now
    assert memory.promoted(bot)[0]["skill"] == "charts"
    lines = list(reversed(knowledge.activity(bot)))
    assert [(ln["action"], ln["target"], ln["stage"]) for ln in lines] == [
        ("skill_updated", "charts", "distill"), ("removed", lesson["id"], "distill")]
    assert lines[0]["why"] == "Titles keep coming up." and lines[0]["undoable"]
    # Nothing waits for anyone: no pending state is left anywhere, and undo puts the file back.
    knowledge.undo(bot, lines[0]["id"])
    assert "Legends" not in _file(bot)


def test_no_fitting_skill_makes_a_new_one_and_an_unreadable_draft_falls_back_to_bullets(bot, fake_text):
    a = _old(bot, n=3)
    fake_text.replies.append(lambda p: json.dumps({"action": "create", "title": "Chart titles",
                                                   "trigger": "chart, title", "body": "- Title charts by region."}))
    consolidate.run(bot, force=True)
    assert [(s["name"], s["body"]) for s in bot.skills()] == [("chart-titles", "- Title charts by region.")]
    assert memory.items(bot) == [] and memory.promoted(bot)[0]["skill"] == "chart-titles"
    assert _acts(bot) == [("skill_created", "chart-titles"), ("removed", a["id"])]
    b = memory.add(bot, "Loaders are named load_<thing>.", session="s0")
    for s in ("s1", "s2"):
        memory.bump(bot, b["id"], session=s)
    fake_text.replies.append("no json here")
    consolidate.run(bot, force=True)
    new = [s for s in bot.skills() if s["name"] != "chart-titles"]
    assert [s["body"] for s in new] == ["- Loaders are named load_<thing>."] and memory.items(bot) == []


def test_a_draft_that_does_not_fit_is_logged_and_nothing_is_left_pending(bot, fake_text):
    _skill(bot)
    lesson = _old(bot, n=3)
    fake_text.replies.append(lambda p: json.dumps({
        "action": "update", "skill": "charts",
        "ops": [{"op": "replace", "heading": "No such section", "section": "x"}]}))
    before = _file(bot)
    consolidate.run(bot, force=True)
    assert _file(bot) == before and [i["id"] for i in memory.items(bot)] == [lesson["id"]]
    (line,) = knowledge.activity(bot)
    assert line["action"] == "skill_updated" and line["ok"] is False and "No such section" in line["error"]
    assert not any(k in memory.items(bot)[0] for k in ("proposed", "promoted", "rejected"))


def test_a_correction_end_to_end_the_lesson_is_replaced_and_the_skill_section_rewritten(bot, fake_text):
    """Inbox correction -> the old lesson is replaced -> the skill that states the old rule is rewritten -> every
    step is on the activity log. `m_old` is still in memory; `m_skill` is the same rule already inside a skill."""
    _skill(bot)
    live = _old(bot)
    in_skill = memory.add(bot, "Chart titles follow '<Region> — Monthly revenue'.", session="s0")
    knowledge.apply(bot, {"op": "lesson_remove", "id": in_skill["id"], "reason": "promoted", "skill": "charts",
                          "why": "setup"})
    c1 = _correction(bot, contradicts=live["id"])
    c2 = _correction(bot, contradicts=in_skill["id"])
    assert c2["contradicts"] == in_skill["id"]  # a rule that lives in a skill may be named
    assert consolidate.due(bot)  # a verified correction does not wait for three entries or a day
    fake_text.replies += [
        lambda p: json.dumps([{"obs": c1["id"], "op": "add"}, {"obs": c2["id"], "op": "same", "as": c1["id"]}]),
        lambda p: json.dumps({"resolution": "replace"}),
        lambda p: json.dumps({"action": "update", "skill": "charts", "why": "You corrected the title rule.",
                              "ops": [{"op": "replace", "heading": "Titles",
                                       "section": "On a regional canvas a chart node's title is just the region "
                                                  "name."}]})]
    before = _file(bot)
    assert consolidate.run(bot)["ran"]
    draft = fake_text.calls[2]
    assert draft.startswith('THE USER CORRECTED A RULE THAT THE SKILL "charts" STATES') and SAID in draft
    text = _file(bot)
    assert "## Titles\n\nOn a regional canvas a chart node's title is just the region name.\n" in text
    assert "Monthly revenue" not in text
    for heading in ("Colours", "Axes"):  # sections nobody named: byte for byte
        assert _section(text, heading) == _section(before, heading)
    assert memory.items(bot) == []  # the old lesson was replaced; the corrected one now lives in the skill
    reasons = {r["id"]: r["reason"] for r in _archive(bot)}
    assert reasons[live["id"]] == "corrected"
    lines = [ln for ln in reversed(knowledge.activity(bot, quiet=False)) if ln["why"] != "setup"]
    new_id = lines[0]["target"]
    assert [(ln["action"], ln["target"], ln["stage"]) for ln in lines] == [
        ("learned", new_id, "lessons"), ("removed", live["id"], "lessons"),
        ("skill_updated", "charts", "distill"), ("removed", new_id, "distill")]
    assert all(ln["ok"] and ln["why"] for ln in lines) and lines[2]["evidence"]["quote"] == SAID
    assert consolidate.waiting(bot) == []


@pytest.mark.parametrize("reply", [
    {"action": "update", "skill": "charts", "ops": [{"op": "add", "heading": "New", "section": "beside it"}]},
    {"action": "create", "title": "Other", "trigger": "x", "body": "beside it"},
    "not json"])
def test_a_correction_must_replace_or_remove_the_old_rule_in_its_skill(bot, fake_text, reply):
    _skill(bot)
    new = memory.add(bot, NEW, trust="user", via="session")
    fake_text.replies.append(reply if isinstance(reply, str) else json.dumps(reply))
    lines = []
    gen = distill.step(bot, {"corrections": [{"item": new["id"], "skill": "charts", "old": OLD, "quote": SAID}]})
    for change in gen:
        lines.append(change)
    (note,) = lines  # a failed draft is one skipped line, never a pending state
    assert (note["op"], note["action"]) == ("note", "skipped") and "charts" in note["why"] or "read" in note["why"]
    assert [i["id"] for i in memory.items(bot)] == [new["id"]]


def test_repeated_silent_edits_leave_untouched_sections_and_code_blocks_byte_for_byte(bot, fake_text):
    _skill(bot)
    original = _file(bot)
    edits = [
        [{"op": "add", "heading": "Legends", "section": "Below the chart."}],
        [{"op": "replace", "heading": "Titles", "section": "Just the region name."}],
        [{"op": "replace", "heading": "Legends", "section": "Below the chart, one row.\n\n```json\n{\"rows\": 1}\n```"}],
        [{"op": "remove", "heading": "Axes"}],
        [{"op": "add", "heading": "Tooltips", "section": "Show the exact value."}],
    ]
    for n, ops in enumerate(edits):
        lesson = memory.add(bot, f"Recurring chart lesson {n}.", session="a")
        for s in ("b", "c"):
            memory.bump(bot, lesson["id"], session=s)
        fake_text.replies.append(json.dumps({"action": "update", "skill": "charts", "ops": ops}))
        consolidate.run(bot, force=True)
        assert memory.items(bot) == [], n
    text = _file(bot)
    assert [ln["ok"] for ln in knowledge.activity(bot)] == [True] * 10
    assert knowledge.headings(bot.skills()[0]["body"]) == ["Titles", "Colours", "Legends", "Tooltips"]
    # Never named by any edit: the preamble, and the section holding the code block, are the original bytes.
    assert text.split("## Titles")[0] == original.split("## Titles")[0]
    assert _section(text, "Colours") == _section(original, "Colours")
    assert 'PALETTE = ["#113355", "#aa3300"]   # two   spaces, kept' in text
    assert '```json\n{"rows": 1}\n```' in text and knowledge.fenced_blocks(bot.skills()[0]["body"]) == 2
