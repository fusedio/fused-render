"""`experts/knowledge.py`, the only writer of memory and skills (docs/experts-autonomous.md): every op, the activity
line, undo of a lesson (item level) and of a skill (whole file, refused when it changed since), the read-back
check, sections no op names staying byte for byte, unread / seen, the rule that nothing else writes, and the seed's
update rule for shipped skills. The skill store is the real `Bot.skills` code; no model."""
from __future__ import annotations

from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, …)
import glob
import json
import os
import re

import pytest

from fused_render.bots import bot as botmod
from fused_render.bots import registry
from fused_render.bots.bot import Bot
from fused_render.experts import knowledge, memory, store
from fused_render.experts import seed as xseed

BODY = '''Intro line.

## Shape

```toml
[canvas]
name = "x"
## not a heading
```

## Keys

\tTabs and  double  spaces stay.

## Rules

- rows and files agree'''

LINE_KEYS = {"id", "ts", "action", "target", "summary", "why", "evidence", "stage", "ok", "undoable", "undone",
             "error"}


class SkillBot:
    """A bot whose skills are read by the REAL `Bot.skills` code."""
    SKILL_MAX = Bot.SKILL_MAX
    SKILL_BODY_CAP = Bot.SKILL_BODY_CAP
    _parse_skill = staticmethod(Bot._parse_skill)
    skills = Bot.skills

    def __init__(self, root, bid="kn1"):
        self.id = bid
        self.meta = {"kind": "expert", "name": "Skill Expert", "expert": {}}
        self.public_dir = os.path.join(str(root), bid)
        self.dir = os.path.join(self.public_dir, ".fused")
        os.makedirs(self.dir, exist_ok=True)

    @property
    def skills_dir(self):
        return os.path.join(self.public_dir, "skills")


@pytest.fixture
def bot(tmp_path):
    return SkillBot(tmp_path)


def _file(bot, name):
    with open(os.path.join(bot.skills_dir, name + ".md"), "rb") as f:
        return f.read()


def _skill(bot, body=BODY):
    line = knowledge.apply(bot, {"op": "skill_create", "title": "Canvas layout", "trigger": "layout, canvas",
                                 "body": body, "why": "w"})
    assert line["ok"], line
    return line


def _claims(bot):
    return [i["claim"] for i in memory.items(bot)]


# ------------------------------------------------------------------ lessons ---
def test_lesson_ops_and_the_activity_line(bot):
    a = knowledge.apply(bot, {"op": "lesson_add", "claim": "Read canvas.toml first.", "kind": "lesson",
                              "trust": "outcome", "session": "s1", "about": ["canvas"], "stage": "lessons",
                              "why": "A session  did this.",
                              "evidence": {"session": "s1", "quote": "read it first", "junk": 1}})
    assert LINE_KEYS <= set(a) and "quiet" not in a
    assert (a["action"], a["ok"], a["undoable"], a["undone"], a["error"]) == ("learned", True, True, False, "")
    assert a["summary"] == "Read canvas.toml first." and a["why"] == "A session did this." and a["stage"] == "lessons"
    assert a["evidence"] == {"session": "s1", "quote": "read it first"}
    lid = a["target"]
    assert [(i["id"], i["count"], i["about"]) for i in memory.items(bot)] == [(lid, 1, ["canvas"])]

    again = knowledge.apply(bot, {"op": "lesson_add", "claim": "read canvas.toml first", "trust": "user",
                                  "via": "session", "session": "s2", "why": "w"})  # the equal one, upgraded
    assert (again["action"], again["target"], again.get("quiet")) == ("counted", lid, True)
    assert [(i["count"], i["trust"], i["via"]) for i in memory.items(bot)] == [(2, "user", "session")]

    b = knowledge.apply(bot, {"op": "lesson_bump", "id": lid, "session": "s3", "why": "w"})
    assert (b["action"], b["quiet"], memory.items(bot)[0]["count"]) == ("counted", True, 3)
    knowledge.apply(bot, {"op": "lesson_bump", "id": lid, "session": "s3", "why": "w"})  # once per session
    assert memory.items(bot)[0]["count"] == 3
    c = knowledge.apply(bot, {"op": "lesson_confirm", "id": lid, "session": "s4", "why": "w"})
    assert (c["action"], c["quiet"]) == ("counted", True)
    assert (memory.items(bot)[0]["confirmed"], memory.items(bot)[0]["count"]) == (1, 3)
    x = knowledge.apply(bot, {"op": "lesson_contradict", "id": lid, "session": "s5", "why": "w"})
    assert (x["action"], x["quiet"], x["undoable"]) == ("counted", True, True)
    assert (memory.items(bot)[0]["contradicted"], memory.items(bot)[0]["count"]) == (1, 3)
    knowledge.undo(bot, x["id"])
    assert memory.items(bot)[0]["contradicted"] == 0
    echo = knowledge.apply(bot, {"op": "lesson_add", "claim": "An echo.", "counted": False, "session": "s1",
                                 "why": "w"})
    assert echo["action"] == "learned" and memory.items(bot)[1]["count"] == 0

    e = knowledge.apply(bot, {"op": "lesson_edit", "id": lid, "claim": "Read canvas.toml before nodes.", "why": "w"})
    assert (e["action"], e["summary"]) == ("corrected", "Read canvas.toml before nodes.")
    assert [h["claim"] for h in memory.items(bot)[0]["history"]] == ["Read canvas.toml first."]

    other = knowledge.apply(bot, {"op": "lesson_add", "claim": "Name nodes with hyphens.", "why": "w"})["target"]
    m = knowledge.apply(bot, {"op": "lesson_merge", "ids": [lid, other],
                              "claim": "Read canvas.toml first and name nodes with hyphens.", "why": "w"})
    assert m["action"] == "merged" and m["ok"]
    assert _claims(bot) == ["Read canvas.toml first and name nodes with hyphens.", "An echo."]
    assert memory.items(bot)[0]["count"] == 3  # the largest original

    r = knowledge.apply(bot, {"op": "lesson_remove", "id": m["target"], "reason": "promoted", "skill": "layout",
                              "why": "It is in a skill now."})
    assert (r["action"], r["summary"]) == ("removed", "Read canvas.toml first and name nodes with hyphens.")
    f = knowledge.apply(bot, {"op": "lesson_remove", "id": echo["target"], "reason": "forgotten", "why": "w"})
    assert f["action"] == "forgot" and memory.items(bot) == []


def test_a_bad_change_is_logged_and_never_raised(bot):
    bad = [{"op": "lesson_bump", "id": "nope", "why": "w"}, {"op": "lesson_contradict", "id": "nope"}, {"op": "lesson_add", "claim": "", "why": "w"},
           {"op": "lesson_edit", "id": "nope", "claim": "x"}, {"op": "lesson_remove", "id": "nope", "reason": "stale"},
           {"op": "lesson_merge", "ids": ["a", "b"], "claim": "x"}, {"op": "skill_edit", "skill": "nope", "ops": []},
           {"op": "skill_edit", "skill": "../x", "ops": []}, {"op": "skill_restore", "skill": "nope"},
           {"op": "skill_create", "title": "T", "trigger": "", "body": "b"}, {"op": "note", "action": "shouted"},
           {"op": "explode"}, {}, None, "x"]
    for change in bad:
        line = knowledge.apply(bot, change)
        assert line["ok"] is False and line["error"] and line["undoable"] is False and LINE_KEYS <= set(line), change
    assert len(knowledge.activity(bot)) == len(bad) and memory.items(bot) == [] and bot.skills() == []
    assert knowledge.apply(bot, {"op": "lesson_remove", "id": "x", "reason": "stale"})["action"] == "removed"


def test_notes_change_nothing(bot):
    for action in knowledge.NOTE_ACTIONS:
        line = knowledge.apply(bot, {"op": "note", "action": action, "summary": "Looked for  the answer.",
                                     "why": "w", "stage": "research"})
        assert (line["action"], line["ok"], line["undoable"], line["summary"]) == (
            action, True, False, "Looked for the answer.")
        assert bool(line.get("quiet")) is (action == "noticed")
    assert memory.items(bot) == [] and bot.skills() == []
    with pytest.raises(knowledge.Refused, match="cannot be undone"):
        knowledge.undo(bot, line["id"])


def test_undo_puts_a_lesson_back_as_it_was(bot):
    a = knowledge.apply(bot, {"op": "lesson_add", "claim": "Read canvas.toml first.", "session": "s1", "why": "w"})
    lid = a["target"]
    was = memory.items(bot)[0]
    e = knowledge.apply(bot, {"op": "lesson_edit", "id": lid, "claim": "Something else.", "why": "w"})
    other = knowledge.apply(bot, {"op": "lesson_add", "claim": "Unrelated.", "why": "w"})
    u = knowledge.undo(bot, e["id"])  # item level: the later, unrelated lesson is untouched
    assert (u["action"], u["target"], u["undoable"], u["summary"]) == ("undo", e["id"], False, "Undid: Something else.")
    assert memory.items(bot)[0] == was and _claims(bot) == ["Read canvas.toml first.", "Unrelated."]
    rows = {r["id"]: r for r in knowledge.activity(bot)}
    assert rows[e["id"]]["undone"] is True and rows[a["id"]]["undone"] is False and rows[u["id"]]["action"] == "undo"
    with pytest.raises(knowledge.Refused, match="already undone"):
        knowledge.undo(bot, e["id"])
    with pytest.raises(knowledge.Refused):
        knowledge.undo(bot, u["id"])
    with pytest.raises(knowledge.Refused, match="no such"):
        knowledge.undo(bot, "nope")

    r = knowledge.apply(bot, {"op": "lesson_remove", "id": lid, "reason": "stale", "why": "w"})
    assert _claims(bot) == ["Unrelated."]
    knowledge.undo(bot, r["id"])
    assert sorted(_claims(bot)) == ["Read canvas.toml first.", "Unrelated."]
    m = knowledge.apply(bot, {"op": "lesson_merge", "ids": [lid, other["target"]], "claim": "Both.", "why": "w"})
    assert _claims(bot) == ["Both."]
    knowledge.undo(bot, m["id"])
    assert sorted(_claims(bot)) == ["Read canvas.toml first.", "Unrelated."]
    knowledge.undo(bot, a["id"])  # undoing the add removes the lesson
    assert _claims(bot) == ["Unrelated."]


# ------------------------------------------------------------------- skills ---
def test_skill_create_and_edit_leave_unnamed_sections_byte_for_byte(bot):
    made = _skill(bot)
    assert (made["action"], made["target"], made["summary"]) == ("skill_created", "canvas-layout",
                                                                "New skill “Canvas layout”")
    assert _file(bot, "canvas-layout") == f"# Canvas layout\ntrigger: layout, canvas\n\n{BODY}\n".encode()
    pre, secs = knowledge.parse_sections(BODY)
    assert pre + "".join(s["text"] for s in secs) == BODY
    assert knowledge.headings(BODY) == ["Shape", "Keys", "Rules"] and knowledge.fenced_blocks(BODY) == 1

    line = knowledge.apply(bot, {"op": "skill_edit", "skill": "canvas-layout", "why": "The user said so.", "ops": [
        {"op": "replace", "heading": "keys", "section": "## Keys\n\nNew keys text."},
        {"op": "add", "heading": "Naming", "section": "Use hyphens.\n\n# Deep\nx"},
        {"op": "remove", "heading": "Rules"}],
        "triggers_add": ["naming", "Layout"], "triggers_remove": ["canvas"]})
    assert line["ok"] and line["action"] == "skill_updated" and line["target"] == "canvas-layout" and line["undoable"]
    assert line["summary"] == ("Updated “Canvas layout”: rewrote “Keys”, added “Naming”, removed “Rules”, "
                               "changed its trigger words")
    (s,) = bot.skills()
    assert s["trigger"] == "layout, naming"
    new_pre, new = knowledge.parse_sections(s["body"])
    assert new_pre == pre and new[0]["text"] == secs[0]["text"]  # the preamble and “Shape”: byte for byte
    assert [x["heading"] for x in new] == ["Shape", "Keys", "Naming"]
    assert new[1]["text"] == "## Keys\n\nNew keys text.\n\n" and new[2]["text"] == "## Naming\n\nUse hyphens.\n\n### Deep\nx"

    # All or nothing: an op naming a section that is not there changes nothing.
    before = _file(bot, "canvas-layout")
    for change in ({"ops": [{"op": "add", "heading": "More", "section": "x"}, {"op": "remove", "heading": "Gone"}]},
                   {"ops": [{"op": "move", "headings": ["Keys"]}]}, {"ops": []},
                   {"ops": [], "triggers_remove": ["layout", "naming"]},
                   {"ops": [{"op": "add", "heading": "Big", "section": "z" * Bot.SKILL_BODY_CAP}]}):
        bad = knowledge.apply(bot, {"op": "skill_edit", "skill": "canvas-layout", "why": "w", **change})
        assert bad["ok"] is False and bad["error"] and bad["action"] == "skill_updated", change
        assert _file(bot, "canvas-layout") == before
    assert "no section “Gone”" in knowledge.activity(bot)[4]["error"]

    second = knowledge.apply(bot, {"op": "skill_create", "title": "Canvas layout", "trigger": "x",
                                   "body": "# Canvas layout\ntrigger: x\n\nBody.", "why": "w"})
    assert second["target"] == "canvas-layout-2" and _file(bot, "canvas-layout-2") == b"# Canvas layout\ntrigger: x\n\nBody.\n"


def test_undo_restores_the_whole_skill_file_and_refuses_when_it_changed_since(bot):
    made = _skill(bot)
    v1 = _file(bot, "canvas-layout")
    first = knowledge.apply(bot, {"op": "skill_edit", "skill": "canvas-layout", "why": "w",
                                  "ops": [{"op": "add", "heading": "One", "section": "1"}]})
    v2 = _file(bot, "canvas-layout")
    second = knowledge.apply(bot, {"op": "skill_edit", "skill": "canvas-layout", "why": "w",
                                   "ops": [{"op": "add", "heading": "Two", "section": "2"}]})
    with pytest.raises(knowledge.Refused, match="changed since; undo the later change first"):
        knowledge.undo(bot, first["id"])
    assert not any(r["action"] == "undo" for r in knowledge.activity(bot))  # a refusal is not logged
    knowledge.undo(bot, second["id"])
    assert _file(bot, "canvas-layout") == v2
    knowledge.undo(bot, first["id"])
    assert _file(bot, "canvas-layout") == v1
    with open(os.path.join(bot.skills_dir, "canvas-layout.md"), "ab") as f:
        f.write(b"edited by hand\n")
    with pytest.raises(knowledge.Refused, match="changed since"):
        knowledge.undo(bot, made["id"])
    with open(os.path.join(bot.skills_dir, "canvas-layout.md"), "wb") as f:
        f.write(v1)
    knowledge.undo(bot, made["id"])  # undoing the create removes the file
    assert bot.skills() == []


def test_a_skill_that_does_not_read_back_is_put_back(bot, monkeypatch):
    _skill(bot)
    before = _file(bot, "canvas-layout")
    real = Bot.skills
    monkeypatch.setattr(SkillBot, "skills", lambda self: [dict(s, body=s["body"][:-1]) for s in real(self)])
    change = {"op": "skill_edit", "skill": "canvas-layout", "why": "w",
              "ops": [{"op": "add", "heading": "One", "section": "1"}]}
    line = knowledge.apply(bot, change)
    assert line["ok"] is False and "intact" in line["error"] and line["undoable"] is False
    assert _file(bot, "canvas-layout") == before
    new = knowledge.apply(bot, {"op": "skill_create", "title": "Other", "trigger": "o", "body": "Body.", "why": "w"})
    assert new["ok"] is False and not os.path.exists(os.path.join(bot.skills_dir, "other.md"))
    monkeypatch.setattr(SkillBot, "skills", real)
    assert knowledge.apply(bot, change)["ok"] is True


def test_skill_restore_and_its_undo(bot):
    shipped = os.path.join(store.expert_dir(bot), knowledge.SHIPPED_DIR)
    os.makedirs(shipped)
    raw = b"# Layout\r\ntrigger: layout\r\n\r\nShipped \xff body.\r\n"  # bytes survive, even ones that are not UTF-8
    with open(os.path.join(shipped, "layout.md"), "wb") as f:
        f.write(raw)
    assert knowledge.shipped_path(bot, "layout") and knowledge.shipped_path(bot, "../layout") is None
    first = knowledge.apply(bot, {"op": "skill_restore", "skill": "layout", "why": "w", "quiet": True,
                                  "summary": "Installed “layout”"})
    assert (first["action"], first["summary"], first["quiet"]) == ("skill_restored", "Installed “layout”", True)
    assert _file(bot, "layout") == raw
    with open(os.path.join(bot.skills_dir, "layout.md"), "wb") as f:
        f.write(b"# Layout\ntrigger: layout\n\nMine.\n")
    line = knowledge.apply(bot, {"op": "skill_restore", "skill": "layout", "why": "w"})
    assert line["ok"] and line["undoable"] and _file(bot, "layout") == raw
    knowledge.undo(bot, line["id"])
    assert _file(bot, "layout") == b"# Layout\ntrigger: layout\n\nMine.\n"


# ----------------------------------------------------------------- activity ---
def test_activity_unread_and_seen(bot):
    assert knowledge.activity(bot) == [] and knowledge.unread(bot) == 0
    knowledge.seen(bot)
    a = knowledge.apply(bot, {"op": "lesson_add", "claim": "One.", "why": "w"})
    b = knowledge.apply(bot, {"op": "lesson_bump", "id": a["target"], "session": "s", "why": "w"})  # quiet
    c = knowledge.apply(bot, {"op": "note", "action": "could_not_find", "summary": "No answer.", "why": "w"})
    assert [r["id"] for r in knowledge.activity(bot)] == [c["id"], b["id"], a["id"]]  # newest first
    assert [r["id"] for r in knowledge.activity(bot, quiet=False)] == [c["id"], a["id"]]
    assert [r["id"] for r in knowledge.activity(bot, limit=1)] == [c["id"]]
    assert [r["id"] for r in knowledge.activity(bot, before=c["id"])] == [b["id"], a["id"]]
    assert knowledge.activity(bot, before=a["id"]) == []
    assert knowledge.unread(bot) == 2  # quiet lines do not count
    knowledge.seen(bot)
    assert knowledge.unread(bot) == 0
    knowledge.undo(bot, a["id"])  # the user's own undo is not news
    assert knowledge.unread(bot) == 0
    knowledge.apply(bot, {"op": "lesson_add", "claim": "Two.", "why": "w"})
    assert knowledge.unread(bot) == 1
    with open(os.path.join(store.expert_dir(bot), knowledge.ACTIVITY_FILE)) as f:
        assert all(LINE_KEYS <= set(json.loads(ln)) for ln in f)


# ------------------------------------------------- nothing else may write ---
#: Calls that write `memory.json` or a skill file. `memory.cite` only counts how often an answer used a lesson
#: (Answer's bookkeeping, no op in the contract), so it is not in the list.
_WRITES = re.compile(r"\bmemory\.(add|bump|confirm|contradict|narrow|remove|merge|forget|mark|_save)\(|\bskill_save\(|"
                     r"\bskill_delete\(|memory\.json")


def test_only_knowledge_writes_memory_and_skills():
    root = os.path.dirname(knowledge.__file__)
    bad = []
    for path in sorted(glob.glob(os.path.join(root, "*.py"))):
        name = os.path.basename(path)
        if name in ("knowledge.py", "memory.py"):  # memory.py holds the mutators knowledge calls
            continue
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if _WRITES.search(line.split("#", 1)[0]):
                    bad.append(f"{name}:{n}: {line.strip()}")
    assert not bad, "only knowledge.py may write memory.json or skills/*.md:\n" + "\n".join(bad)


# ---------------------------------------------------- seed: shipped skills ---
@pytest.fixture
def shipped(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    d = tmp_path / "shipped" / "demo"
    (d / "skills").mkdir(parents=True)
    (d / "expert.json").write_text(json.dumps({"name": "Demo Expert", "manifest": {"ask_me_about": "Demos"}}))
    (d / "instructions.md").write_text("Be precise.\n")
    (d / "skills" / "layout.md").write_text("# Folder layout\ntrigger: layout\n\nThe body.\n")
    (d / "skills" / "naming.md").write_text("# Naming\ntrigger: name\n\nUse hyphens.\n")
    (d / "questions.md").write_text("## Q?\n- must: a point\n")
    monkeypatch.setattr(xseed, "SHIPPED_DIR", str(tmp_path / "shipped"))
    return d / "skills"


def test_seed_keeps_the_shipped_version_and_seeds_no_questions(shipped):
    (bid,) = xseed.seed()
    b = registry.get(bid)
    kept = os.path.join(store.expert_dir(b), knowledge.SHIPPED_DIR)
    assert sorted(os.listdir(kept)) == ["layout.md", "naming.md"]
    for name in ("layout", "naming"):
        assert _file(b, name) == (shipped / f"{name}.md").read_bytes() == open(
            os.path.join(kept, name + ".md"), "rb").read()
    assert not os.path.exists(os.path.join(b.public_dir, "questions.md"))
    assert store.manifest(b)["learning"] is True and xseed.PENDING not in b.meta["expert"]
    assert knowledge.unread(b) == 0 and knowledge.activity(b, quiet=False) == []  # installing is not news
    assert [h["shipped"] for h in xseed.skills_health(b)] == [True, True]
    assert xseed.seed() == [] and len(knowledge.activity(b)) == 2  # nothing new shipped: nothing happens


def test_a_new_shipped_version_replaces_only_an_unchanged_copy(shipped):
    (bid,) = xseed.seed()
    b = registry.get(bid)
    edit = knowledge.apply(b, {"op": "skill_edit", "skill": "naming", "why": "w",
                               "ops": [{"op": "add", "heading": "Learned", "section": "Mine."}]})
    mine = _file(b, "naming")
    (shipped / "layout.md").write_text("# Folder layout\ntrigger: layout\n\nThe NEW body.\n")
    (shipped / "naming.md").write_text("# Naming\ntrigger: name\n\nUse underscores.\n")
    assert xseed.seed() == []
    assert b.skills()[0]["body"] == "The NEW body." and _file(b, "naming") == mine
    new, skipped = sorted(knowledge.activity(b, quiet=False)[:2], key=lambda r: r["target"])
    assert (new["action"], new["target"], new["undoable"]) == ("skill_restored", "layout", True)
    assert "new version" in new["summary"]
    assert (skipped["action"], skipped["target"], skipped["ok"]) == ("skipped", "naming", True)
    assert knowledge.unread(b) == 3  # the edit, the update, the skipped note
    n = len(knowledge.activity(b))
    assert xseed.seed() == [] and len(knowledge.activity(b)) == n  # said once, not on every start
    # Reset gives the newest shipped version, through knowledge (logged, undoable).
    assert xseed.restore_shipped_skill(b, "naming") is True and b.skills()[1]["body"] == "Use underscores."
    assert xseed.restore_shipped_skill(b, "mine") is False and edit["ok"]
    knowledge.undo(b, knowledge.activity(b)[0]["id"])
    assert _file(b, "naming") == mine


def test_an_install_from_before_skills_shipped_keeps_its_copies(shipped):
    import shutil
    (bid,) = xseed.seed()
    b = registry.get(bid)
    shutil.rmtree(os.path.join(store.expert_dir(b), knowledge.SHIPPED_DIR))  # as an older install looks
    with open(os.path.join(b.skills_dir, "layout.md"), "w") as f:
        f.write("# Folder layout\ntrigger: layout\n\nMy own body.\n")
    n = len(knowledge.activity(b))
    assert xseed.seed() == []
    assert b.skills()[0]["body"] == "My own body." and len(knowledge.activity(b)) == n
    assert knowledge.shipped_path(b, "layout") and knowledge.shipped_path(b, "naming")
