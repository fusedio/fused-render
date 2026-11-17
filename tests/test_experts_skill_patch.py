"""Skill proposals patch a skill and never rewrite or cut it (docs/experts.md §4 `proposals.py`): JSON parsing
that keeps fenced code inside strings, deterministic section append / replace, trigger append, overflow into a
new skill, approve's backup + read-back verification + restore, the shipped-skill repair path and its routes.
The skill store is the real `Bot.skills` / `Bot.skill_save` code; no real model."""
from __future__ import annotations

from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (client, …)
import json
import os
import re
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


# ------------------------------------------------------------ the patches ---

def test_an_update_appends_only_the_new_section(bot, replies):
    bot.skill_save("Canvas layout", "canvas, toml", FENCED, name="canvas-layout")
    before = _skill(bot, "canvas-layout")["body"]
    a = _lesson(bot, "Text notes are rows of type text")
    section = "Text notes:\n\n```toml\n[[nodes]]\ntype = \"text\"\n```"
    replies.append({"action": "update", "skill": "canvas-layout", "heading": "Text notes", "section": section,
                    "triggers": ["text note", "Canvas", "note"]})
    p = proposals.prepare(bot, [a])
    prompt = replies.prompts[0]
    assert "sections: Shape; Keys; Rules" in prompt and "never copy or rewrite" in prompt
    assert p["status"] == "ready" and p["action"] == "update" and p["skill"] == "canvas-layout"
    assert p["body"] == before + "\n\n## Text notes\n\n" + section
    assert p["trigger"] == "canvas, toml, text note, note"  # appended, deduplicated case-insensitively
    assert "-trigger: canvas, toml" in p["diff"] and "+trigger: canvas, toml, text note, note" in p["diff"]
    assert "+## Text notes" in p["diff"] and not [ln for ln in p["diff"].splitlines()
                                                  if ln.startswith("-") and not ln.startswith(("---", "-trigger"))]
    proposals.approve(bot, p["id"])
    after = _skill(bot, "canvas-layout")
    assert after["body"].startswith(before) and after["body"] == p["body"]
    assert proposals.learned_sections(bot) == [{"skill": "canvas-layout", "heading": "Text notes"}]


def test_same_heading_replaces_only_a_section_a_proposal_added(bot, replies):
    bot.skill_save("Canvas layout", "canvas", FENCED, name="canvas-layout")
    # "Keys" exists but was not added by a proposal: never replaced, a new section is appended
    replies.append({"action": "update", "skill": "canvas-layout", "heading": "keys", "section": "More keys."})
    p = proposals.prepare(bot, [_lesson(bot, "one")])
    assert p["body"] == FENCED + "\n\n## keys\n\nMore keys." and not p["replaced"]
    proposals.approve(bot, p["id"])
    # "Extra" added by a proposal, then a later proposal with the same heading replaces its content in place
    replies.append({"action": "update", "skill": "canvas-layout", "heading": "Extra", "section": "First."})
    proposals.approve(bot, proposals.prepare(bot, [_lesson(bot, "two")])["id"])
    replies.append({"action": "update", "skill": "canvas-layout", "heading": "EXTRA", "section": "Second."})
    p3 = proposals.prepare(bot, [_lesson(bot, "three")])
    assert p3["replaced"] is True
    proposals.approve(bot, p3["id"])
    body = _skill(bot, "canvas-layout")["body"]
    assert body == FENCED + "\n\n## keys\n\nMore keys.\n\n## Extra\n\nSecond."
    # a "keys" proposal now replaces the learned "keys" (the last one), never the original "Keys"
    replies.append({"action": "update", "skill": "canvas-layout", "heading": "Keys", "section": "Newer keys."})
    proposals.approve(bot, proposals.prepare(bot, [_lesson(bot, "four")])["id"])
    assert _skill(bot, "canvas-layout")["body"] == FENCED + "\n\n## keys\n\nNewer keys.\n\n## Extra\n\nSecond."


def test_patch_body_ignores_headings_inside_fences():
    body = "A\n\n```md\n## Not a heading\n```\n\n## Real\n\nold"
    assert proposals.headings(body) == ["Real"] and proposals.fenced_blocks(body) == 1
    assert proposals.patch_body(body, "Not a heading", "x", replace=True) == body + "\n\n## Not a heading\n\nx"
    assert proposals.patch_body(body, "real", "new", replace=True) == body.replace("old", "new")


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
    p = proposals.prepare(bot, [_lesson(bot, "Use the parquet loader")])
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
