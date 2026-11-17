"""Skill proposals edit a skill by section and never rewrite or cut it (docs/experts.md §4 `proposals.py`): JSON
parsing that keeps fenced code inside strings, the section parser's round trip, add / replace / remove / move ops
applied strictly, warnings, the over-limit retry and fallback, approve's backup + read-back verification + restore
(of two skills for a move), skill pressure (reorganise), the shipped-skill repair path and its routes.
The skill store is the real `Bot.skills` / `Bot.skill_save` code; no real model."""
from __future__ import annotations

from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (client, …)
import json
import os
import re
import threading
import time

import pytest

from fused_render.bots.bot import Bot
from fused_render.experts import consolidate, memory, model, proposals, seed

FENCED = '''Intro line.

## Shape

```toml
[canvas]
name = "x"
```

## Keys

```python
def f():
    return {"a": [1, 2]}
```

## Rules

- rows and files agree'''


class SkillBot:
    """A bot whose skills go through the REAL `Bot.skills` / `Bot.skill_save` code."""
    SKILL_MAX = Bot.SKILL_MAX
    SKILL_BODY_CAP = Bot.SKILL_BODY_CAP
    _parse_skill = staticmethod(Bot._parse_skill)
    skills = Bot.skills
    skill_save = Bot.skill_save

    def __init__(self, root, bid="sk1", expert=None):
        self.id = bid
        self.meta = {"kind": "expert", "name": "Skill Expert", "expert": expert or {}}
        self.public_dir = os.path.join(str(root), bid)
        self.dir = os.path.join(self.public_dir, ".fused")
        os.makedirs(self.dir, exist_ok=True)

    @property
    def skills_dir(self):
        return os.path.join(self.public_dir, "skills")


@pytest.fixture
def bot(tmp_path):
    return SkillBot(tmp_path)


@pytest.fixture
def replies(monkeypatch):
    class Queue(list):
        prompts: list

    queue = Queue()

    def text(b, prompt, *, system="", timeout=180):
        text.prompts.append(prompt)
        r = queue.pop(0)
        r = r() if callable(r) else r
        return r if isinstance(r, str) else json.dumps(r)

    text.prompts = []
    monkeypatch.setattr(model, "text", text)
    monkeypatch.setattr(model, "available", lambda b: (True, ""))
    queue.prompts = text.prompts
    return queue


def _lesson(bot, claim):
    it = None
    for k in range(3):
        it = memory.add(bot, claim, session=f"s{k}")
    return it


def _skill(bot, name):
    return next(s for s in bot.skills() if s["name"] == name)


def _file(bot, name):
    with open(os.path.join(bot.skills_dir, name + ".md"), "rb") as f:
        return f.read()


# ------------------------------------------------------------- parse_json ---

def test_parse_json_keeps_a_fenced_block_inside_a_string_byte_for_byte():
    body = "Do this:\n\n```python\nx = {\"a\": [1]}\n```\n\nThen ```inline``` too."
    raw = json.dumps({"action": "create", "body": body})
    assert consolidate.parse_json(raw, dict)["body"] == body
    assert consolidate.parse_json("```json\n" + raw + "\n```", dict)["body"] == body  # outer wrapper only
    assert consolidate.parse_json("```\n" + raw + "\n```\n", dict)["body"] == body
    assert consolidate.parse_json("Here you go:\n" + raw + "\nHope that helps [really].", dict)["body"] == body
    prose = "Sure. ```json\n" + raw + "\n``` done"
    assert consolidate.parse_json(prose, dict)["body"] == body


def test_parse_json_lists_prose_and_failures():
    assert consolidate.parse_json('Answer: [{"obs": "o1", "op": "nothing"}] ok', list) == [{"obs": "o1",
                                                                                         "op": "nothing"}]
    assert consolidate.parse_json('see [note] then [1, "]"]', list) == [1, "]"]
    assert consolidate.parse_json('{"a": "unclosed', dict) is None
    assert consolidate.parse_json(None, list) is None
    assert consolidate.parse_json("[1]", dict) is None


# ------------------------------------------------------ sections and ops ---

def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


ROUND_TRIP = [
    "", "\n", "no headings at all", "## Only", "## Only\n", "Pre\n\n## A\nx\n## B\ny\n", "\n\n## A\n\n\n## A\n",
    "A\r\n## B\r\nc\r\n", "  ## indented is not a heading\n## Real", "##NoSpace\n## Yes\n##\n## ",
    "```\n## not a heading\n```\n## real\n", "~~~~\n```\n## inside\n~~~~\n## after", "```\nunclosed\n## inside",
    "````md\n```\n## inside\n```\n````\n## out\ntext", "trailing spaces   \n## H  \n  body  \n\n\n", FENCED,
]


@pytest.mark.parametrize("body", ROUND_TRIP)
def test_parse_and_serialise_round_trip_exactly(body):
    pre, secs = proposals.parse_sections(body)
    assert proposals.serialise(pre, secs) == body
    assert all(s["text"].startswith("## ") for s in secs)


def test_round_trip_on_every_shipped_skill():
    for root, _dirs, files in os.walk(seed.SHIPPED_DIR):
        for n in files:
            if n.endswith(".md"):
                with open(os.path.join(root, n), encoding="utf-8") as f:
                    text = f.read()
                assert proposals.serialise(*proposals.parse_sections(text)) == text, n


def test_a_heading_inside_a_fence_is_not_a_heading():
    body = "A\n\n```md\n## Not a heading\n```\n\n## Real\n\nold\n\n~~~\n## Also not\n~~~"
    pre, secs = proposals.parse_sections(body)
    assert pre == "A\n\n```md\n## Not a heading\n```\n\n" and [s["heading"] for s in secs] == ["Real"]
    assert proposals.headings(body) == ["Real"] and proposals.fenced_blocks(body) == 2
    res = proposals.apply_ops(body, [{"op": "remove", "heading": "Not a heading"}])
    assert res["applied"] == [] and "no section “Not a heading”" in res["dropped"][0]["reason"]


def test_each_op():
    add = proposals.apply_ops(FENCED, [{"op": "add", "heading": "Extra", "section": "More."}])
    assert add["body"] == FENCED + "\n\n## Extra\n\nMore." and add["applied"][0]["op"] == "add"

    rep = proposals.apply_ops(FENCED, [{"op": "replace", "heading": " keys ", "section": "## Keys\n\nNew keys."}])
    assert rep["body"] == FENCED[:FENCED.index("## Keys")] + "## Keys\n\nNew keys.\n\n" + FENCED[FENCED.index("## Rules"):]

    ren = proposals.apply_ops(FENCED, [{"op": "replace", "heading": "Rules", "new_heading": "Invariants",
                                        "section": "- rows and files agree"}])
    assert proposals.headings(ren["body"]) == ["Shape", "Keys", "Invariants"] and ren["renamed"] == ["Rules"]

    rem = proposals.apply_ops(FENCED, [{"op": "remove", "heading": "Shape"}])
    assert rem["body"] == FENCED.replace(FENCED[FENCED.index("## Shape"):FENCED.index("## Keys")], "")

    mov = proposals.apply_ops(FENCED, [{"op": "move", "headings": ["Keys", "Shape"],
                                        "to": {"title": "Canvas keys", "trigger": "keys, shape"}}])
    assert mov["body"] == "Intro line.\n\n## Rules\n\n- rows and files agree"
    assert mov["moved"] == {"headings": ["Shape", "Keys"], "title": "Canvas keys", "trigger": "keys, shape",
                            "body": FENCED[FENCED.index("## Shape"):FENCED.index("## Rules")].strip()}


def test_ops_apply_in_sequence_and_add_on_an_existing_heading_replaces():
    res = proposals.apply_ops(FENCED, [
        {"op": "replace", "heading": "Keys", "new_heading": "Key names", "section": "Renamed."},
        {"op": "remove", "heading": "key names"},           # sees the rename
        {"op": "add", "heading": "Keys", "section": "Back at the end."},
        {"op": "add", "heading": "SHAPE", "section": "Shape, replaced by an add."},
    ])
    assert [o["op"] for o in res["applied"]] == ["replace", "remove", "add", "replace"]
    assert res["applied"][3]["heading"] == "Shape" and res["dropped"] == []
    assert res["body"] == ("Intro line.\n\n## Shape\n\nShape, replaced by an add.\n\n## Rules\n\n- rows and files agree"
                           "\n\n## Keys\n\nBack at the end.")


def test_unknown_or_ambiguous_headings_are_dropped_and_recorded():
    body = "## Notes\n\none\n\n## notes\n\ntwo\n\n## Other\n\nthree"
    res = proposals.apply_ops(body, [{"op": "replace", "heading": "Notes", "section": "x"},
                                     {"op": "remove", "heading": "Missing"},
                                     {"op": "move", "headings": ["Other", "Missing"], "to": {"title": "T"}},
                                     {"op": "frobnicate", "heading": "Other"},
                                     {"op": "add", "heading": "notes", "section": "y"}])
    assert res["applied"] == [] and res["body"] == body
    assert [d["reason"] for d in res["dropped"]] == [
        "“Notes” names 2 sections", "no section “Missing” in this skill", "no section “Missing” in this skill",
        "unknown operation “frobnicate”", "“notes” names 2 sections"]


def test_untouched_sections_stay_byte_identical_after_a_replace_elsewhere(bot, replies):
    src = os.path.join(seed.SHIPPED_DIR, "canvas-reference", "skills", "canvas-folder-layout.md")
    with open(src, encoding="utf-8") as f:
        title, trigger, body = Bot._parse_skill(f.read())
    bot.skill_save(title, trigger, body, name="canvas-folder-layout")
    old = _skill(bot, "canvas-folder-layout")["body"]
    pre, secs = proposals.parse_sections(old)
    target = secs[2]["heading"]
    replies.append({"action": "update", "skill": "canvas-folder-layout", "why": "superseded",
                    "ops": [{"op": "replace", "heading": target, "section": "Shorter, corrected text."}]})
    p = proposals.prepare(bot, [_lesson(bot, "a correction")])
    assert old in replies.prompts[0]  # the chosen skill's full body is shown
    assert "Prefer changing the section that already covers the topic" in replies.prompts[0]
    proposals.approve(bot, p["id"])
    new = _skill(bot, "canvas-folder-layout")["body"]
    npre, nsecs = proposals.parse_sections(new)
    assert npre == pre and [s["text"] for s in nsecs[:2]] == [s["text"] for s in secs[:2]]
    assert [s["text"] for s in nsecs[3:-1]] == [s["text"] for s in secs[3:-1]]
    assert nsecs[-1]["text"] == secs[-1]["text"].rstrip()  # the body's end is stripped, nothing else
    assert nsecs[2]["text"] == f"## {target}\n\nShorter, corrected text.\n\n"
    assert new.startswith(old[:old.index("## " + target)]) and new.endswith(old[old.index("## " + secs[3]["heading"]):])


# ------------------------------------------------------- update proposals ---

def test_an_update_edits_named_sections_and_triggers(bot, replies):
    bot.skill_save("Canvas layout", "canvas, toml, legacy", FENCED, name="canvas-layout")
    a = _lesson(bot, "Canvas keys are snake_case")
    replies.append({"action": "update", "skill": "canvas-layout", "why": "keys changed",
                    "ops": [{"op": "replace", "heading": "Keys", "section": "Keys are snake_case."},
                            {"op": "add", "heading": "Text notes", "section": "Notes are rows."}],
                    "triggers_add": ["Canvas", "keys"], "triggers_remove": ["legacy"]})
    p = proposals.prepare(bot, [a])
    assert p["status"] == "ready" and p["action"] == "update" and p["skill"] == "canvas-layout"
    assert p["trigger"] == "canvas, toml, keys" and p["triggers_added"] == ["keys"]
    assert p["triggers_removed"] == ["legacy"] and p["why"] == "keys changed"
    assert p["warnings"] == ["removes 1 code block"] and p["dropped_ops"] == []
    assert "-trigger: canvas, toml, legacy" in p["diff"] and "+Keys are snake_case." in p["diff"]
    text, detail = proposals.card_text(p)
    assert "Warning: this change removes 1 code block." in text and text.endswith("Approve?") and detail == p["diff"]
    proposals.approve(bot, p["id"])
    assert _skill(bot, "canvas-layout")["body"] == p["body"]
    assert proposals.headings(p["body"]) == ["Shape", "Keys", "Rules", "Text notes"]


def test_a_shipped_section_may_be_replaced_and_an_add_on_it_becomes_a_replace(bot, replies):
    bot.skill_save("Canvas layout", "canvas", FENCED, name="canvas-layout")
    replies.append({"action": "update", "skill": "canvas-layout",
                    "ops": [{"op": "add", "heading": "rules", "section": "- rows, files and the index agree"}]})
    p = proposals.prepare(bot, [_lesson(bot, "the index agrees too")])
    assert p["ops"] == [{"op": "replace", "heading": "Rules", "section": "- rows, files and the index agree"}]
    assert p["body"] == FENCED.replace("- rows and files agree", "- rows, files and the index agree")
    assert p["warnings"] == []


def test_dropped_ops_are_recorded_on_the_proposal(bot, replies):
    bot.skill_save("Canvas layout", "canvas", FENCED, name="canvas-layout")
    replies.append({"action": "update", "skill": "canvas-layout",
                    "ops": [{"op": "replace", "heading": "Nope", "section": "x"},
                            {"op": "add", "heading": "Extra", "section": "More."}]})
    p = proposals.prepare(bot, [_lesson(bot, "extra")])
    assert p["status"] == "ready" and p["body"] == FENCED + "\n\n## Extra\n\nMore."
    assert p["dropped_ops"] == [{"op": {"op": "replace", "heading": "Nope", "section": "x"},
                                 "reason": "no section “Nope” in this skill"}]


def test_a_draft_whose_ops_all_drop_fails(bot, replies):
    bot.skill_save("Canvas layout", "canvas", FENCED, name="canvas-layout")
    a = _lesson(bot, "nothing")
    replies.append({"action": "update", "skill": "canvas-layout", "triggers_add": ["x"],
                    "ops": [{"op": "remove", "heading": "Nope"}, {"op": "replace", "heading": "Gone", "section": "y"}]})
    p = proposals.prepare(bot, [a])
    assert p["status"] == "failed" and p["error"] == proposals.NO_SECTION == "the draft named no section that exists"
    assert not memory.items(bot)[0]["proposed"] and _skill(bot, "canvas-layout")["body"] == FENCED


def test_approve_fails_when_a_named_section_is_gone_since_the_draft(bot, replies):
    bot.skill_save("Canvas layout", "canvas", FENCED, name="canvas-layout")
    replies.append({"action": "update", "skill": "canvas-layout",
                    "ops": [{"op": "replace", "heading": "Keys", "section": "New keys."}]})
    p = proposals.prepare(bot, [_lesson(bot, "keys")])
    bot.skill_save("Canvas layout", "canvas", "Intro only.", name="canvas-layout")
    with pytest.raises(ValueError, match="changed since this was drafted"):
        proposals.approve(bot, p["id"])
    assert _skill(bot, "canvas-layout")["body"] == "Intro only."


# ---------------------------------------------------------------- warnings ---

WBODY = ("Intro.\n\n## A\n\n" + "a" * 100 + "\n\n## B\n\n```py\nx = 1\n```\n\n" + "b" * 100 + "\n\n## C\n\n"
         + "c" * 400)


def _warn(ops):
    res = proposals.apply_ops(WBODY, ops)
    return proposals.warnings_for(WBODY, res["body"], moved=res["moved"], renamed=res["renamed"])


def test_warnings_for_each_case_and_none_for_a_pure_add():
    assert _warn([{"op": "add", "heading": "D", "section": "d" * 50}]) == []
    assert _warn([{"op": "remove", "heading": "A"}]) == ["removes the section “A”"]
    assert _warn([{"op": "replace", "heading": "B", "section": "b" * 100}]) == ["removes 1 code block"]
    assert _warn([{"op": "replace", "heading": "A", "new_heading": "Aa", "section": "a" * 100}]) == []
    new = proposals.apply_ops(WBODY, [{"op": "remove", "heading": "C"}])["body"]
    pct = round(100 * (len(WBODY) - len(new)) / len(WBODY))
    assert pct > 25
    assert _warn([{"op": "remove", "heading": "C"}]) == ["removes the section “C”", f"shrinks this skill by {pct}%"]
    assert _warn([{"op": "move", "headings": ["C", "B"], "to": {"title": "Cee", "trigger": "c"}}]) == [
        f"shrinks this skill by {round(100 * (len(WBODY) - len(proposals.apply_ops(WBODY, [{'op': 'remove', 'heading': 'C'}, {'op': 'remove', 'heading': 'B'}])['body'])) / len(WBODY))}%",
        "moves 2 sections into a new skill “Cee”"]
    assert _warn([{"op": "remove", "heading": "B"}, {"op": "remove", "heading": "A"}])[:3] == [
        "removes the section “A”", "removes the section “B”", "removes 1 code block"]


# ------------------------------------------------------------------- move ---

def test_a_move_writes_and_verifies_two_skills(bot, replies):
    bot.skill_save("Big", "big", WBODY, name="big")
    replies.append({"action": "update", "skill": "big",
                    "ops": [{"op": "move", "headings": ["C"], "to": {"title": "Cee things", "trigger": "cee"}}]})
    p = proposals.prepare(bot, [_lesson(bot, "c is separate")])
    assert p["moved"]["name"] == "cee-things" and "moves 1 section into a new skill “Cee things”" in p["warnings"]
    assert "+++ skills/big.md" in p["diff"] and "--- /dev/null\n+++ skills/cee-things.md" in p["diff"]
    assert "+## C" in p["diff"].split("+++ skills/cee-things.md")[1]
    done = proposals.approve(bot, p["id"])
    assert done["status"] == "approved" and done["moved_to"] == "cee-things"
    assert _skill(bot, "big")["body"] == WBODY[:WBODY.index("\n\n## C")]
    moved = _skill(bot, "cee-things")
    assert moved["body"] == "## C\n\n" + "c" * 400 and moved["trigger"] == "cee" and moved["title"] == "Cee things"
    assert memory.items(bot) == []


def test_a_failed_move_restores_both_skills(bot, replies, monkeypatch):
    bot.skill_save("Big", "big", WBODY, name="big")
    original = _file(bot, "big")
    a = _lesson(bot, "c is separate")
    replies.append({"action": "update", "skill": "big",
                    "ops": [{"op": "move", "headings": ["C"], "to": {"title": "Cee things", "trigger": "cee"}}]})
    p = proposals.prepare(bot, [a])
    real = Bot.skill_save

    def second_fails(self, title, trigger, body, name=None):
        if name == "cee-things":
            raise OSError("disk full")
        return real(self, title, trigger, body, name=name)

    monkeypatch.setattr(SkillBot, "skill_save", second_fails)
    with pytest.raises(ValueError, match="disk full"):
        proposals.approve(bot, p["id"])
    assert _file(bot, "big") == original and [s["name"] for s in bot.skills()] == ["big"]
    assert proposals.get(bot, p["id"])["status"] == "failed" and not memory.items(bot)[0]["proposed"]

    # a save that cuts the moved skill: verification fails, both restored
    replies.append({"action": "update", "skill": "big",
                    "ops": [{"op": "move", "headings": ["C"], "to": {"title": "Cee things", "trigger": "cee"}}]})
    monkeypatch.setattr(SkillBot, "skill_save", real)
    p2 = proposals.prepare(bot, [a])
    monkeypatch.setattr(SkillBot, "skill_save", lambda self, t, tr, b, name=None: real(
        self, t, tr, b[:20] if name == "cee-things" else b, name=name))
    with pytest.raises(ValueError, match="could not be saved intact"):
        proposals.approve(bot, p2["id"])
    assert _file(bot, "big") == original and [s["name"] for s in bot.skills()] == ["big"]


# ----------------------------------------------------------- over the limit ---

def _near_full(cap):
    half = int(cap * 0.45)
    return f"## A\n\n{'a' * half}\n\n## B\n\n{'b' * half}"


def test_an_over_limit_result_retries_with_a_move(bot, replies):
    cap = Bot.SKILL_BODY_CAP
    body = _near_full(cap)
    bot.skill_save("Full", "full", body, name="full")
    replies.append({"action": "update", "skill": "full",
                    "ops": [{"op": "add", "heading": "New", "section": "n" * int(cap * 0.2)}]})
    replies.append({"ops": [{"op": "move", "headings": ["B"], "to": {"title": "Bee", "trigger": "bee"}}]})
    p = proposals.prepare(bot, [_lesson(bot, "new stuff")])
    assert len(replies.prompts) == 2 and "does not fit" in replies.prompts[1] and "\nB: " in replies.prompts[1]
    assert p["status"] == "ready" and p["action"] == "update" and len(p["body"]) <= cap
    assert [o["op"] for o in p["ops"]] == ["add", "move"] and p["moved"]["name"] == "bee"
    proposals.approve(bot, p["id"])
    assert proposals.headings(_skill(bot, "full")["body"]) == ["A", "New"]
    assert _skill(bot, "full")["body"].startswith(body[:body.index("\n\n## B")])
    assert _skill(bot, "bee")["body"] == "## B\n\n" + "b" * int(cap * 0.45)


def test_an_over_limit_result_falls_back_to_a_new_skill_after_the_retry(bot, replies):
    cap = Bot.SKILL_BODY_CAP
    bot.skill_save("Full", "full", _near_full(cap), name="full")
    replies.append({"action": "update", "skill": "full", "triggers_add": ["newish"],
                    "ops": [{"op": "add", "heading": "New stuff", "section": "n" * int(cap * 0.2)}]})
    replies.append({"ops": [{"op": "move", "headings": ["Nope"], "to": {"title": "X", "trigger": "x"}}]})
    p = proposals.prepare(bot, [_lesson(bot, "new stuff")])
    assert len(replies.prompts) == 2
    assert p["action"] == "create" and p["overflow_from"] == "full" and p["title"] == "New stuff"
    assert p["body"] == "n" * int(cap * 0.2) and p["trigger"] == "newish"
    proposals.approve(bot, p["id"])
    assert _skill(bot, "full")["body"] == _near_full(cap)


# --------------------------------------------------------------- reorganise ---

def _over(cap, share, letter):
    n = int(cap * share)
    third = n // 3
    return "\n\n".join(f"## {letter}{k}\n\n" + letter * third for k in range(3))


@pytest.fixture
def no_cards(monkeypatch):
    from fused_render.experts import cards
    monkeypatch.setattr(cards, "show", lambda b, p: True)


def test_skill_pressure_reorganises_the_largest_skill_once(bot, replies, no_cards):
    from fused_render.experts import tidy
    cap = Bot.SKILL_BODY_CAP
    bot.skill_save("Small", "small", _over(cap, 0.78, "s"), name="small")
    assert tidy.skill_pressure(bot) is None  # under 80%: nothing
    bot.skill_save("Big", "big", _over(cap, 0.85, "b"), name="big")
    bot.skill_save("Bigger", "bigger", _over(cap, 0.9, "g"), name="bigger")
    go = threading.Event()

    def slow():
        go.wait(5)
        return json.dumps({"action": "update", "skill": "bigger", "why": "g2 is its own topic",
                           "ops": [{"op": "move", "headings": ["g2"], "to": {"title": "Gee", "trigger": "gee"}}]})

    replies.append(slow)
    pid = tidy.skill_pressure(bot)
    p = proposals.get(bot, pid)
    assert p["kind"] == "reorganise" and p["skill"] == "bigger" and p["items"] == []
    assert tidy.skill_pressure(bot) is None  # single flight: one is preparing
    go.set()
    assert _wait(lambda: proposals.get(bot, pid)["status"] == "ready")
    assert tidy.skill_pressure(bot) is None  # ...or waiting on its card
    assert "nearly full" in replies.prompts[0] and "<<<BEGIN SKILL BODY>>>" in replies.prompts[0]
    p = proposals.get(bot, pid)
    text, _ = proposals.card_text(p)
    assert "“Bigger” is nearly full" in text and "g2 is its own topic" in text
    assert "Warning: this change moves 1 section into a new skill “Gee”." in text
    proposals.approve(bot, pid)
    assert proposals.headings(_skill(bot, "bigger")["body"]) == ["g0", "g1"] and _skill(bot, "gee")


def test_no_second_reorganise_within_seven_days_of_a_rejected_one(bot, replies, no_cards):
    from fused_render.experts import tidy
    bot.skill_save("Big", "big", _over(Bot.SKILL_BODY_CAP, 0.85, "b"), name="big")
    replies.append({"action": "update", "skill": "big", "ops": [{"op": "remove", "heading": "b1"}]})
    pid = tidy.skill_pressure(bot)
    assert _wait(lambda: proposals.get(bot, pid)["status"] == "ready")
    rejected = proposals.reject(bot, pid)
    t = rejected["decided"]
    assert tidy.skill_pressure(bot, now=t + 6 * 86400) is None
    replies.append({"action": "update", "skill": "big", "ops": [{"op": "remove", "heading": "b2"}]})
    pid2 = tidy.skill_pressure(bot, now=t + 7 * 86400 + 1)
    assert pid2 and pid2 != pid
    assert _wait(lambda: proposals.get(bot, pid2)["status"] == "ready")


def test_a_reorganise_that_names_nothing_fails(bot, replies, no_cards):
    bot.skill_save("Big", "big", _over(Bot.SKILL_BODY_CAP, 0.85, "b"), name="big")
    replies.append({"action": "update", "skill": "big", "ops": [{"op": "remove", "heading": "zz"}]})
    p = proposals.request_reorganise(bot, "big")
    assert _wait(lambda: proposals.get(bot, p["id"])["status"] == "failed")
    assert proposals.get(bot, p["id"])["error"] == proposals.NO_SECTION


def test_three_approved_updates_keep_every_fence_and_heading(bot, replies):
    """The regression: a shipped skill with fenced code lost its fences and sections after three approvals."""
    src = os.path.join(seed.SHIPPED_DIR, "canvas-reference", "skills", "canvas-folder-layout.md")
    with open(src, encoding="utf-8") as f:
        title, trigger, body = Bot._parse_skill(f.read())
    bot.skill_save(title, trigger, body, name="canvas-folder-layout")
    original = _skill(bot, "canvas-folder-layout")
    fences, heads = proposals.fenced_blocks(original["body"]), proposals.headings(original["body"])
    assert fences == 1 and len(heads) == 7  # one ```toml block (two fence lines)
    for k in range(3):
        section = f"Lesson {k}:\n\n```python\nprint({k})\n```"
        # a model answer wrapped in an outer fence, carrying fenced code inside the JSON string
        replies.append("```json\n" + json.dumps({"action": "update", "skill": "canvas-folder-layout",
                                                 "heading": f"Learned {k}", "section": section,
                                                 "triggers": [f"word{k}"]}) + "\n```")
        p = proposals.prepare(bot, [_lesson(bot, f"lesson number {k}")])
        assert p["action"] == "update"
        proposals.approve(bot, p["id"])
    now = _skill(bot, "canvas-folder-layout")
    assert now["body"].startswith(original["body"])
    assert proposals.fenced_blocks(now["body"]) == fences + 3
    assert proposals.headings(now["body"]) == heads + ["Learned 0", "Learned 1", "Learned 2"]
    assert now["trigger"] == original["trigger"] + ", word0, word1, word2"
    assert not any(h["damaged"] for h in seed.skills_health(bot))


# --------------------------------------------------------------- overflow ---

def test_an_over_limit_body_becomes_a_new_skill_never_a_cut(bot, replies):
    big = "x" * (Bot.SKILL_BODY_CAP - 20)
    bot.skill_save("Big skill", "big", big, name="big-skill")
    replies.append({"action": "update", "skill": "big-skill", "heading": "Parquet loading",
                    "section": "Use the parquet loader.", "triggers": ["parquet", "big"]})
    replies.append("no idea")  # the retry asking for a move: unreadable, so the fallback
    p = proposals.prepare(bot, [_lesson(bot, "Use the parquet loader")])
    assert len(replies.prompts) == 2
    assert p["status"] == "ready" and p["action"] == "create" and p["overflow_from"] == "big-skill"
    assert p["title"] == "Parquet loading" and p["body"] == "Use the parquet loader."
    assert p["trigger"] == "parquet" and p["name"] == "parquet-loading"
    text, _ = proposals.card_text(p)
    assert "That skill is full, so this goes into a new skill “Parquet loading”" in text
    proposals.approve(bot, p["id"])
    assert _skill(bot, "big-skill")["body"] == big  # untouched
    assert _skill(bot, "parquet-loading")["body"] == "Use the parquet loader."


def test_an_over_limit_trigger_becomes_a_new_skill(bot, replies):
    long_trigger = ", ".join(f"phrase{k:03d}" for k in range(27))  # 295 characters
    assert len(long_trigger) <= proposals.trigger_cap() < len(long_trigger) + 10
    bot.skill_save("Many triggers", long_trigger, "Body.", name="many-triggers")
    replies.append({"action": "update", "skill": "many-triggers", "heading": "Viewport",
                    "section": "Viewport rules.", "triggers": ["viewport zoom"]})
    replies.append({"ops": []})  # the retry: nothing that helps
    p = proposals.prepare(bot, [_lesson(bot, "viewport rules")])
    assert p["action"] == "create" and p["trigger"] == "viewport zoom" and p["overflow_from"] == "many-triggers"
    proposals.approve(bot, p["id"])
    assert _skill(bot, "many-triggers")["trigger"] == long_trigger


def test_material_too_big_even_for_a_new_skill_fails(bot, replies):
    a = _lesson(bot, "huge")
    replies.append({"action": "create", "title": "Huge", "trigger": "huge",
                    "body": "y" * (Bot.SKILL_BODY_CAP + 1)})
    p = proposals.prepare(bot, [a])
    assert p["status"] == "failed" and "does not fit even a new skill" in p["error"]
    assert bot.skills() == [] and not memory.items(bot)[0]["proposed"]


def test_an_old_whole_body_update_record_is_never_written(bot):
    bot.skill_save("S", "s", FENCED, name="s")
    a = _lesson(bot, "old")
    proposals._save(bot, {"id": "pold", "ts": 1, "status": "shown", "action": "update", "skill": "s",
                          "title": "S", "trigger": "s", "body": "rewritten without fences", "items": [a["id"]]})
    with pytest.raises(ValueError, match="rewrote the whole skill"):
        proposals.approve(bot, "pold")
    assert _skill(bot, "s")["body"] == FENCED and proposals.get(bot, "pold")["status"] == "failed"


# ---------------------------------------------------------------- approve ---

def test_approve_backs_up_verifies_and_restores_a_bad_save(bot, replies, monkeypatch):
    bot.skill_save("Canvas layout", "canvas", FENCED, name="canvas-layout")
    original = _file(bot, "canvas-layout")
    a = _lesson(bot, "a lesson")
    replies.append({"action": "update", "skill": "canvas-layout", "heading": "New", "section": "New stuff."})
    p = proposals.prepare(bot, [a])
    real = Bot.skill_save

    def cutting(self, title, trigger, body, name=None):  # simulates a save that cuts the body
        return real(self, title, trigger, body[:50], name=name)

    monkeypatch.setattr(SkillBot, "skill_save", cutting)
    with pytest.raises(ValueError, match="could not be saved intact"):
        proposals.approve(bot, p["id"])
    assert _file(bot, "canvas-layout") == original  # restored byte for byte
    after = proposals.get(bot, p["id"])
    assert after["status"] == "failed" and "could not be saved intact" in after["error"]
    items = memory.items(bot)
    assert [i["id"] for i in items] == [a["id"]] and not items[0]["proposed"]  # the lesson stays in memory
    assert proposals.learned_sections(bot) == []
    backups = proposals.backups(bot, "canvas-layout")
    assert len(backups) == 1 and open(backups[0], "rb").read() == original
    assert re.fullmatch(r"canvas-layout\.\d+\.\d+\.md", os.path.basename(backups[0]))


def test_a_bad_save_of_a_new_skill_leaves_no_file(bot, replies, monkeypatch):
    replies.append({"action": "create", "title": "Fresh", "trigger": "fresh", "body": "Fresh body."})
    p = proposals.prepare(bot, [_lesson(bot, "fresh")])
    monkeypatch.setattr(SkillBot, "skill_save",
                        lambda self, t, tr, b, name=None: Bot.skill_save(self, t, "other", b, name=name))
    with pytest.raises(ValueError, match="intact"):
        proposals.approve(bot, p["id"])
    assert bot.skills() == [] and memory.items(bot)


def test_backups_are_pruned_to_the_newest_twenty(bot):
    bot.skill_save("S", "s", "Body.", name="s")
    paths = [proposals.backup_skill(bot, "s") for _ in range(25)]
    kept = proposals.backups(bot, "s")
    assert len(kept) == proposals.BACKUPS_KEPT == 20 and kept == paths[-20:]
    assert proposals.backup_skill(bot, "missing") is None


# ---------------------------------------------------------- repair path ---

def test_restore_shipped_skill_and_health(tmp_path):
    bot = SkillBot(tmp_path, expert={"shipped": "canvas-reference"})
    src = os.path.join(seed.SHIPPED_DIR, "canvas-reference", "skills", "canvas-folder-layout.md")
    with open(src, encoding="utf-8") as f:
        title, trigger, body = Bot._parse_skill(f.read())
    # damaged as on real data: fences stripped, the last two sections gone, padded to the limit
    damaged = body.replace("```toml\n", "").replace("```python\n", "").replace("```\n", "").replace("```", "")
    damaged = damaged.split("## Default rows fused-render writes")[0].strip()
    damaged = (damaged + "\n\n" + "z" * Bot.SKILL_BODY_CAP)[:Bot.SKILL_BODY_CAP]
    bot.skill_save(title, trigger, damaged, name="canvas-folder-layout")
    bot.skill_save("Mine", "mine", "Mine.", name="mine")
    proposals._add_learned(bot, "canvas-folder-layout", "Extra")
    proposals._add_learned(bot, "mine", "Other")
    health = {h["name"]: h for h in seed.skills_health(bot)}
    h = health["canvas-folder-layout"]
    assert h["shipped"] is True and h["damaged"] is True and h["limit"] == Bot.SKILL_BODY_CAP
    assert h["body_chars"] == Bot.SKILL_BODY_CAP and len(h["reasons"]) == 3
    assert "fenced code blocks" in h["reasons"][0] and "Authoritative format reference" in h["reasons"][1]
    assert "limit" in h["reasons"][2]
    assert health["mine"] == {"name": "mine", "shipped": False, "body_chars": 5, "limit": Bot.SKILL_BODY_CAP,
                              "damaged": False, "reasons": []}

    assert seed.restore_shipped_skill(bot, "mine") is False
    assert seed.restore_shipped_skill(bot, "../x") is False
    before = _file(bot, "canvas-folder-layout")
    assert seed.restore_shipped_skill(bot, "canvas-folder-layout") is True
    with open(src, "rb") as f:
        assert _file(bot, "canvas-folder-layout") == f.read()
    assert open(proposals.backups(bot, "canvas-folder-layout")[-1], "rb").read() == before
    assert proposals.learned_sections(bot) == [{"skill": "mine", "heading": "Other"}]
    assert not {h["name"]: h for h in seed.skills_health(bot)}["canvas-folder-layout"]["damaged"]
    assert seed.restore_shipped_skill(SkillBot(tmp_path, bid="plain"), "canvas-folder-layout") is False


# ----------------------------------------------------------------- routes ---

BID = "e2e2e2e2"


@pytest.fixture
def expert(client, monkeypatch):
    from fused_render.bots import paths as bpaths
    from fused_render.bots import registry
    from fused_render.bots import store as bstore
    bpaths.place(BID, "Canvas Expert")
    bstore.write_meta(BID, {"id": BID, "name": "Canvas Expert", "kind": "expert", "model": "", "effort": "",
                            "status": "idle", "instructions": "", "created": time.time(), "task": "",
                            "step": 0, "url": None, "title": None,
                            "expert": {"ask_me_about": "Canvases", "shipped": "canvas-reference"}})
    monkeypatch.setattr(model, "available", lambda bot: (True, ""))
    return registry.get(BID)


def _json(res):
    status, _headers, body = res
    return status, json.loads(body or b"null")


def test_skill_health_and_restore_routes(client, expert):
    expert.skill_save("Canvas folder layout", "canvas", "No fences left.", name="canvas-folder-layout")
    expert.skill_save("Mine", "mine", "Mine.", name="mine")
    status, body = _json(client.get(f"/api/experts/{BID}/skills/health"))
    assert status == 200
    rows = {r["name"]: r for r in body}
    assert set(rows["canvas-folder-layout"]) == {"name", "shipped", "body_chars", "limit", "damaged", "reasons"}
    assert rows["canvas-folder-layout"]["shipped"] and rows["canvas-folder-layout"]["damaged"]
    assert rows["mine"]["shipped"] is False and rows["mine"]["damaged"] is False

    status, body = _json(client.post(f"/api/experts/{BID}/skills/canvas-folder-layout/restore", {}))
    assert status == 200 and body == {"restored": True}
    assert proposals.fenced_blocks(next(s for s in expert.skills() if s["name"] == "canvas-folder-layout")["body"]) == 1
    status, body = _json(client.post(f"/api/experts/{BID}/skills/mine/restore", {}))
    assert status == 404 and "no such shipped skill" in body["error"]
    status, _b = _json(client.post("/api/experts/nobody/skills/mine/restore", {}))
    assert status == 404
    status, _b = _json(client.get("/api/experts/nobody/skills/health"))
    assert status == 404
