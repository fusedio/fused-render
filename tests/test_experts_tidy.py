"""experts memory budget (docs/experts.md "tidy"): usage tracking, and the tidy step (delete / merge
with code-enforced guard rails, the hard limit, heavy-use promotion through distill), asserted on memory and the
activity log. No real model."""
from __future__ import annotations

from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (client, …)
import json
import os
import time

import pytest

from fused_render.experts import consolidate, consult, distill, engine, knowledge, memory, model, store, tidy

DAY = 86400.0


class FakeBot:
    def __init__(self, root, bid="t1", expert=None):
        self.id = bid
        self.meta = {"kind": "expert", "name": "Tidy Expert", "expert": expert or {}}
        self.public_dir = os.path.join(str(root), bid)
        self.dir = os.path.join(self.public_dir, ".fused")
        os.makedirs(self.dir, exist_ok=True)

    @property
    def skills_dir(self):
        return os.path.join(self.public_dir, "skills")

    def skills(self):
        out = []
        for n in sorted(os.listdir(self.skills_dir)) if os.path.isdir(self.skills_dir) else []:
            with open(os.path.join(self.skills_dir, n), encoding="utf-8") as f:
                lines = f.read().split("\n")
            out.append({"name": n[:-3], "title": lines[0][2:], "trigger": lines[1].split(":", 1)[1].strip(),
                        "body": "\n".join(lines[2:]).strip()})
        return out

    def skill_save(self, title, trigger, body, name=None):
        name = name or "-".join(str(title).lower().split())
        os.makedirs(self.skills_dir, exist_ok=True)
        with open(os.path.join(self.skills_dir, name + ".md"), "w", encoding="utf-8") as f:
            f.write(f"# {title}\ntrigger: {trigger}\n\n{body}\n")
        return name


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(model, "available", lambda b: (True, ""))
    return FakeBot(tmp_path)


@pytest.fixture
def answer(monkeypatch):
    """`model.text` returns `answer.value` (a str, or an exception to raise); calls are recorded."""
    class A:
        value = "[]"
        calls: list = []

    def text(b, prompt, *, system="", timeout=180):
        A.calls.append(prompt)
        if isinstance(A.value, Exception):
            raise A.value
        return A.value if isinstance(A.value, str) else json.dumps(A.value)

    monkeypatch.setattr(model, "text", text)
    A.calls = []
    return A


def _tidy(bot):
    """Drive the tidy step the way the Learn loop does; returns the activity lines it produced."""
    gen, line, lines = tidy.step(bot, {}), None, []
    try:
        while True:
            line = knowledge.apply(bot, {"stage": "tidy", **gen.send(line)})
            lines.append(line)
    except StopIteration:
        return lines


def _archive(bot):
    return store.read_jsonl(os.path.join(store.expert_dir(bot), memory.ARCHIVE_FILE))


def _lesson(bot, claim, *, trust="outcome", count=1, cited=0, idle=30.0, age=None, sessions=None, **extra):
    """A lesson with chosen usage: `idle` days since last seen/used, created `age` days ago (default idle)."""
    it = memory.add(bot, claim, trust=trust)
    now = time.time()
    age = idle if age is None else age
    fields = {"count": count, "cited": cited, "created": now - age * DAY, "updated": now - idle * DAY,
              "last_used": None, "sessions": list(sessions or []), **extra}
    with store.lock(bot):
        data = memory._load(bot)
        for i in data["items"]:
            if i["id"] == it["id"]:
                i.update(fields)
        memory._save(bot, data)
    return next(i for i in memory.items(bot) if i["id"] == it["id"])


def _fill(bot, n, **kw):
    """`n` recent, unrelated lessons: no delete, merge or promote candidates among them."""
    return [_lesson(bot, f"filler k{k}a k{k}b k{k}c", count=2, cited=1, idle=1, **kw) for k in range(n)]


def _ids(bot):
    return {i["id"] for i in memory.items(bot)}


def _budget(bot, **kw):
    bot.meta["expert"] = {"memory": kw}


# ------------------------------------------------------------------ manifest / usage

def test_budget_defaults_merge_with_older_manifests(bot):
    bot.meta["expert"] = {"memory": {"propose_at": 4}}
    mem = store.manifest(bot)["memory"]
    assert mem["propose_at"] == 4
    assert {k: mem[k] for k in ("limit", "tidy_at", "stale_days", "rare_uses", "heavy_uses")} == \
        {"limit": 50, "tidy_at": 30, "stale_days": 14, "rare_uses": 2, "heavy_uses": 5}
    bot.meta["expert"] = {"memory": {"limit": 10, "tidy_at": "x", "heavy_uses": 0}}
    mem = store.manifest(bot)["memory"]
    assert mem["limit"] == 10 and mem["tidy_at"] == 30 and mem["heavy_uses"] == 5


def test_cite_uses_and_bump_last_used(bot):
    a = memory.add(bot, "Read canvas.toml first", session="s1")
    assert a["cited"] == 0 and a["last_used"] is None and memory.uses(a) == 1
    assert memory.cite(bot, [a["id"], a["id"], "mnope"]) == [a["id"]]
    a = memory.items(bot)[0]
    assert a["cited"] == 1 and a["last_used"] and memory.uses(a) == 2
    memory.cite(bot, [a["id"]])
    assert memory.uses(memory.items(bot)[0]) == 3
    old = _lesson(bot, "Old lesson", idle=20, age=40)
    assert 39 < memory.age_days(old) < 41 and 19 < memory.idle_days(old) < 21
    memory.bump(bot, old["id"], session="s2")
    assert memory.idle_days(next(i for i in memory.items(bot) if i["id"] == old["id"])) < 0.01


def test_consult_and_chat_cite_the_used_lessons(bot, monkeypatch):
    a = memory.add(bot, "Use fused.load", trust="user")
    monkeypatch.setattr(engine, "answer", lambda *a_, **k: {"answer": "A", "refs": [], "used": [a["id"]],
                                                              "unsure": False})
    consult.consult(bot, "how?")
    assert memory.items(bot)[0]["cited"] == 1
    monkeypatch.setattr(model, "tool_turn", lambda *a_, **k: '```json\n{"answer": "B", "refs": [], "used": ["%s"], '
                                                             '"unsure": false}\n```' % a["id"])
    engine._run(bot, "q", profile="chat")
    assert memory.items(bot)[0]["cited"] == 2


# ------------------------------------------------------------------ archive

def test_forget_archives(bot):
    a = memory.add(bot, "Forget me please")
    assert memory.forget(bot, a["id"]) == 1
    arch = _archive(bot)
    assert arch[0]["id"] == a["id"] and arch[0]["reason"] == "forgotten" and arch[0]["claim"] == "Forget me please"
    assert arch[0]["removed_at"]


# ------------------------------------------------------------------ the tidy step

def test_tidy_skipped_at_or_below_tidy_at(bot, answer):
    _fill(bot, 29)
    _lesson(bot, "Stale and rare", idle=40)  # 30 lessons: not above tidy_at
    assert _tidy(bot) == [] and answer.calls == []


def test_a_forced_run_tidies_above_tidy_at(bot, answer):
    _fill(bot, 30)
    stale = _lesson(bot, "Stale and rare", idle=40)
    answer.value = [{"id": stale["id"], "do": "delete", "why": "nobody used it"}]
    assert consolidate.run(bot, force=True)["ran"]
    assert stale["id"] not in _ids(bot)
    arch = _archive(bot)[0]
    assert (arch["id"], arch["reason"]) == (stale["id"], "stale")
    (line,) = knowledge.activity(bot)
    assert (line["action"], line["target"], line["stage"], line["why"]) == (
        "removed", stale["id"], "tidy", "nobody used it") and line["undoable"]


@pytest.mark.parametrize("case", ["user", "recent", "used", "unknown"])
def test_model_delete_outside_the_guard_rails_is_ignored(bot, answer, case):
    _fill(bot, 30)
    kw = {"user": {"trust": "user", "idle": 40}, "recent": {"idle": 3}, "used": {"idle": 40, "count": 2, "cited": 2},
          "unknown": {"idle": 40}}[case]
    it = _lesson(bot, "Maybe delete me", **kw)
    stale = _lesson(bot, "Zzz qqq www", idle=40)  # a real candidate, so the model is asked
    answer.value = [{"id": "mnope" if case == "unknown" else it["id"], "do": "delete"},
                    {"id": stale["id"], "do": "keep"}]
    assert _tidy(bot) == [] and it["id"] in _ids(bot) and len(answer.calls) == 1


@pytest.mark.parametrize("value", ["not json at all", RuntimeError("down")])
def test_unparseable_answer_or_failure_changes_nothing_and_is_asked_again(bot, answer, value):
    _fill(bot, 30)
    _lesson(bot, "Stale and rare", idle=40)
    answer.value = value
    before = _ids(bot)
    assert _tidy(bot) == [] and _ids(bot) == before
    _tidy(bot)
    assert len(answer.calls) == 2  # a failed review is not remembered as "unchanged"


def test_merge_two_and_three(bot, answer):
    _fill(bot, 30)
    a = _lesson(bot, "Charts use the dark theme", count=2, cited=1, idle=5, age=40, sessions=["s1", "s2"])
    b = _lesson(bot, "Charts in canvases are dark", count=1, cited=2, idle=2, age=10, sessions=["s2", "s3"],
                about=["/x"])
    c = _lesson(bot, "UDFs return dataframes", trust="user", count=1, idle=3, sessions=["s4"])
    d = _lesson(bot, "UDFs must return a dataframe", trust="user", count=1, idle=3, sessions=["s5"])
    e = _lesson(bot, "Return a GeoDataFrame from a UDF", trust="user", count=1, idle=3, sessions=["s6"])
    answer.value = [
        {"do": "merge", "ids": [a["id"], b["id"]], "claim": "Charts in canvases use the dark theme", "why": "same"},
        {"do": "merge", "ids": [c["id"], d["id"], e["id"]], "claim": "A UDF returns a (Geo)DataFrame"},
    ]
    l1, l2 = _tidy(bot)
    assert (l1["action"], l1["why"], l1["ok"]) == ("merged", "same", True) and l2["action"] == "merged"
    items = {i["id"]: i for i in memory.items(bot)}
    n1, n2 = items[l1["target"]], items[l2["target"]]
    assert a["id"] not in items and b["id"] not in items and n1["kind"] == "lesson"
    assert n1["claim"] == "Charts in canvases use the dark theme"
    # count is the LARGEST original, never the size of the session union.
    assert n1["sessions"] == ["s1", "s2", "s3"] and n1["count"] == 2 and n1["cited"] == 3
    assert n1["trust"] == "outcome" and n1["about"] == ["/x"]
    assert n1["merged_from"] == [{"id": a["id"], "claim": a["claim"]}, {"id": b["id"], "claim": b["claim"]}]
    assert n2["trust"] == "user" and n2["count"] == 1 and len(n2["merged_from"]) == 3
    arch = {x["id"]: x for x in _archive(bot)}
    assert arch[a["id"]]["reason"] == "merged" and arch[a["id"]]["into"] == n1["id"]
    knowledge.undo(bot, l1["id"])  # the originals come back as they were
    assert {a["id"], b["id"]} <= _ids(bot) and n1["id"] not in _ids(bot)


@pytest.mark.parametrize("case", ["one", "four", "unknown", "contradicted", "heavy", "empty", "long", "taken",
                                  "two_groups"])
def test_merge_outside_the_guard_rails_is_ignored(bot, answer, case):
    _fill(bot, 30)
    a = _lesson(bot, "Charts use the dark theme", idle=2)
    b = _lesson(bot, "Charts in canvases are dark theme", idle=2,
                **({"contradicted": 1} if case == "contradicted" else {}),
                **({"count": 3, "cited": 3} if case == "heavy" else {}))
    c = _lesson(bot, "Dark theme charts everywhere", idle=2)
    d = _lesson(bot, "Dark charts theme again", idle=2)
    u1 = _lesson(bot, "Loaders parquet files monthly", idle=2)
    u2 = _lesson(bot, "Parquet loaders files weekly", idle=2)
    ids = {"one": [a["id"]], "four": [a["id"], b["id"], c["id"], d["id"]], "unknown": [a["id"], "mnope"],
           "two_groups": [a["id"], u1["id"]]}.get(case, [a["id"], b["id"]])
    claim = {"empty": "  ", "long": "x" * 401, "taken": u2["claim"]}.get(case, "Charts are dark")
    answer.value = [{"do": "merge", "ids": ids, "claim": claim}]
    before = _ids(bot)
    assert _tidy(bot) == [] and _ids(bot) == before


def test_an_id_belongs_to_the_first_action_naming_it_and_at_most_five_merges(bot, answer):
    _fill(bot, 30)
    pairs = [(_lesson(bot, f"Topic{k} alpha{k} beta{k} one", idle=2), _lesson(bot, f"Topic{k} alpha{k} beta{k} two",
                                                                         idle=2)) for k in range(6)]
    answer.value = [{"do": "merge", "ids": [x["id"], y["id"]], "claim": f"Merged {k}"}
                    for k, (x, y) in enumerate(pairs)] + [{"id": pairs[0][0]["id"], "do": "delete"}]
    lines = _tidy(bot)
    assert [ln["action"] for ln in lines] == ["merged"] * tidy.MAX_MERGES
    assert {pairs[5][0]["id"], pairs[5][1]["id"]} <= _ids(bot)


def test_hard_limit_removes_least_used_non_user_first_and_needs_no_model(bot, answer):
    _budget(bot, limit=5, tidy_at=10)
    keep = [_lesson(bot, f"Used k{k}a k{k}b", count=3, cited=2, idle=1) for k in range(3)]
    user = _lesson(bot, "User taught and never used", trust="user", count=0, idle=90)
    rare_old = _lesson(bot, "Rare old thing", count=1, idle=60)
    rare_new = _lesson(bot, "Rare new stuff", count=1, idle=2)
    mid = _lesson(bot, "Middle used item", count=2, idle=2)
    lines = _tidy(bot)
    assert answer.calls == []
    assert [(ln["action"], ln["target"]) for ln in lines] == [("removed", rare_old["id"]), ("removed", rare_new["id"])]
    assert "at most 5" in lines[0]["why"]
    assert _ids(bot) == {i["id"] for i in keep} | {user["id"], mid["id"]}
    assert {x["reason"] for x in _archive(bot)} == {"over_limit"}


def test_hard_limit_takes_user_lessons_only_when_nothing_else_is_left(bot, answer):
    _budget(bot, limit=2, tidy_at=10)
    u = [_lesson(bot, f"User lesson u{k}a u{k}b", trust="user", count=k, idle=1) for k in range(3)]
    o = _lesson(bot, "Outcome lesson", count=9, idle=1)
    _tidy(bot)
    assert _ids(bot) == {u[1]["id"], u[2]["id"]} and o["id"] not in _ids(bot)


def test_add_never_refuses_over_the_limit(bot):
    _budget(bot, limit=2)
    for k in range(4):
        memory.add(bot, f"Lesson number {k}")
    assert len(memory.items(bot)) == 4  # the limit is enforced by tidy, not by add


def test_no_model_call_without_candidates_and_an_unchanged_memory_is_not_reviewed_twice(bot, answer):
    _fill(bot, 31)
    assert _tidy(bot) == [] and answer.calls == []
    stale = _lesson(bot, "Zzz qqq www", idle=40)
    answer.value = [{"id": stale["id"], "do": "keep"}]
    _tidy(bot)
    _tidy(bot)
    assert len(answer.calls) == 1  # same fingerprint: skipped with no model call
    _lesson(bot, "Vvv bbb nnn", idle=40)  # memory changed: reviewed again
    _tidy(bot)
    assert len(answer.calls) == 2
    prompt = answer.calls[-1]
    assert "Zzz qqq www" in prompt and "filler k0a" not in prompt  # the model sees only the candidates


def test_heavy_use_promotion_goes_through_distill(bot, answer):
    """Tidy only names the lessons used a lot (and seen twice); distill writes them into a skill."""
    _budget(bot, propose_at=9)
    _fill(bot, 30)
    heavy = _lesson(bot, "Heavy hitter lesson", count=2, cited=3, idle=1)
    cited_only = _lesson(bot, "Cited only lesson", count=1, cited=9, idle=1)
    contra = _lesson(bot, "Heavy but contradicted", count=3, cited=3, idle=1, contradicted=1)
    b = tidy.budget(bot)
    assert [i["id"] for i in tidy.promote_ready(memory.items(bot), b)] == [heavy["id"]]
    assert [i["id"] for i in distill.candidates(bot)] == [heavy["id"]]
    assert _tidy(bot) == [] and heavy["id"] in _ids(bot)  # tidy itself removes nothing
    answer.value = {"action": "create", "title": "Heavy hitters", "trigger": "heavy", "body": "- Heavy hitter."}
    consolidate.run(bot, force=True)
    assert heavy["id"] not in _ids(bot) and {cited_only["id"], contra["id"]} <= _ids(bot)
    assert [s["name"] for s in bot.skills()] == ["heavy-hitters"]
    assert memory.promoted(bot)[0]["skill"] == "heavy-hitters"
    _budget(bot, propose_at=9, tidy_at=99)  # below tidy_at heavy use alone earns nothing
    again = _lesson(bot, "Another heavy lesson", count=2, cited=3, idle=1)
    assert again["id"] not in [i["id"] for i in distill.candidates(bot)]


def test_merge_candidates_group_related_lessons_only(bot):
    b = tidy.budget(bot)
    x = _lesson(bot, "Charts in canvases use the dark theme", idle=1)
    y = _lesson(bot, "Canvas charts default to a dark theme", idle=1)
    tagged1 = _lesson(bot, "Read the manifest first", idle=1, about=["/w/canvas.toml"])
    tagged2 = _lesson(bot, "Never edit generated files", idle=1, about=["/w/canvas.toml"])
    unrelated = _lesson(bot, "Exports go to the downloads folder", idle=1)
    other = _lesson(bot, "Python files need a main function", idle=1)
    contra = _lesson(bot, "Dark theme charts in canvases", idle=1, contradicted=1)
    groups = [sorted(i["id"] for i in g) for g in tidy.merge_candidates(memory.items(bot), b)]
    assert sorted(x_["id"] for x_ in (x, y)) in groups
    assert sorted(t["id"] for t in (tagged1, tagged2)) in groups
    flat = {i for g in groups for i in g}
    assert unrelated["id"] not in flat and other["id"] not in flat and contra["id"] not in flat
    assert tidy.related(unrelated, other) == 0.0 and tidy.related(x, y) >= tidy.MERGE_OVERLAP


def test_a_lesson_cited_while_the_review_ran_is_not_deleted(bot, answer, monkeypatch):
    """The delete candidates are picked before the model call, which holds no lock: a consult that cites one
    meanwhile makes it used, and the deletion is checked again against memory as it is now."""
    _fill(bot, 30)
    cited = _lesson(bot, "Zzz qqq www", idle=40)
    stale = _lesson(bot, "Yyy ppp vvv", idle=40)
    review = model.text

    def text(b, prompt, **kw):
        memory.cite(b, [cited["id"]])  # an answer relies on it during the review
        return review(b, prompt, **kw)

    monkeypatch.setattr(model, "text", text)
    answer.value = [{"id": cited["id"], "do": "delete"}, {"id": stale["id"], "do": "delete"}]
    lines = _tidy(bot)
    assert [(ln["action"], ln["target"]) for ln in lines] == [("removed", stale["id"])]
    assert cited["id"] in _ids(bot) and stale["id"] not in _ids(bot)
