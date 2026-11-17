"""experts memory budget (docs/experts.md "Memory budget"): usage tracking, the archive, tidy (delete / merge /
promote with code-enforced guard rails, the hard limit) and its HTTP surface. No real model."""
from __future__ import annotations

from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (client, …)
import json
import os
import time

import pytest

from fused_render.experts import consolidate, consult, engine, memory, model, proposals, store, tidy

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


@pytest.fixture
def requests(monkeypatch):
    """`proposals.request` recorded (no drafting thread)."""
    calls = []

    def fake(b, items, *, topic="", gap=""):
        ids = [i["id"] if isinstance(i, dict) else i for i in items]
        calls.append(ids)
        return {"id": "pfake", "items": ids, "status": "preparing"}

    monkeypatch.setattr(proposals, "request", fake)
    return calls


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
    return [_lesson(bot, f"filler lesson number {k}", count=2, cited=1, idle=1, **kw) for k in range(n)]


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
    engine.answer_question(bot, "q")  # the gate's answers do not count
    assert memory.items(bot)[0]["cited"] == 2


# ------------------------------------------------------------------ archive

def test_forget_archives(bot):
    a = memory.add(bot, "Forget me please")
    assert memory.forget(bot, a["id"]) == 1
    arch = memory.archived(bot)
    assert arch[0]["id"] == a["id"] and arch[0]["reason"] == "forgotten" and arch[0]["claim"] == "Forget me please"
    assert arch[0]["removed_at"]


def test_approve_moves_lessons_to_the_archive_with_the_skill(bot, monkeypatch):
    a = _lesson(bot, "Lesson A", count=3)
    b = _lesson(bot, "Lesson B", count=3)
    keep = _lesson(bot, "Lesson C")
    monkeypatch.setattr(model, "text", lambda *a_, **k: json.dumps(
        {"action": "create", "title": "Canvas Rules", "trigger": "canvas", "body": "- A\n- B"}))
    p = proposals.prepare(bot, [a["id"], b["id"]])
    proposals.approve(bot, p["id"])
    assert _ids(bot) == {keep["id"]}
    arch = memory.archived(bot)
    assert {r["id"] for r in arch} == {a["id"], b["id"]}
    assert all(r["reason"] == "promoted" and r["skill"] == "canvas-rules" for r in arch)


def test_old_promoted_items_are_swept_when_tidy_runs(bot, answer):
    a = _lesson(bot, "Old promoted lesson", count=4, promoted=True)
    proposals._save(bot, {"id": "pold", "ts": 1, "status": "approved", "skill": "old-skill", "items": [a["id"]]})
    other = _lesson(bot, "Still here")
    tidy.run(bot)
    assert _ids(bot) == {other["id"]} and answer.calls == []  # below tidy_at: swept without a model call
    rec = memory.archived(bot)[0]
    assert rec["reason"] == "promoted" and rec["skill"] == "old-skill"
    assert tidy.last(bot)["swept"] == [{"id": a["id"], "claim": "Old promoted lesson"}]


# ------------------------------------------------------------------ tidy: skip, delete

def test_tidy_skipped_at_or_below_tidy_at(bot, answer):
    _fill(bot, 30)
    r = consolidate.run(bot, force=True)
    assert r["ran"] is False  # nothing pending and nothing to tidy: force does not start
    assert consolidate.would_run(bot, force=True) is False
    assert tidy.run(bot) == {"deleted": [], "merged": [], "promoting": [], "over_limit": []}
    assert answer.calls == [] and tidy.last(bot) is None


def test_force_consolidate_runs_tidy_above_tidy_at(bot, answer):
    _fill(bot, 30)
    stale = _lesson(bot, "Stale lesson", count=1, idle=30)
    answer.value = [{"id": stale["id"], "do": "delete", "why": "old and unused"}]
    assert consolidate.would_run(bot, force=True)
    r = consolidate.run(bot, force=True)
    assert r["ran"] and r["tidied"]["deleted"] == [stale["id"]] and len(answer.calls) == 1
    assert "Stale lesson" in answer.calls[0] and "when unsure, keep" in answer.calls[0].lower()


def test_valid_stale_delete_is_archived_with_why(bot, answer):
    _fill(bot, 30)
    stale = _lesson(bot, "Stale lesson", count=1, cited=1, idle=20)
    answer.value = [{"id": stale["id"], "do": "delete", "why": "old and unused"}]
    r = tidy.run(bot)
    assert r["deleted"] == [stale["id"]] and stale["id"] not in _ids(bot)
    rec = memory.archived(bot)[0]
    assert rec["reason"] == "stale" and rec["why"] == "old and unused"
    last = tidy.last(bot)
    assert last["deleted"] == [{"id": stale["id"], "claim": "Stale lesson", "why": "old and unused"}]
    assert last["counts"]["deleted"] == 1 and last["at"]


@pytest.mark.parametrize("case", ["recent", "used", "user", "proposal"])
def test_model_delete_outside_the_guard_rails_is_ignored(bot, answer, case):
    _fill(bot, 30)
    kw = {"recent": {"idle": 3}, "used": {"count": 2, "cited": 1}, "user": {"trust": "user"}, "proposal": {}}[case]
    x = _lesson(bot, "Candidate lesson", **{"count": 1, "idle": 30, **kw})
    if case == "proposal":
        proposals._save(bot, {"id": "popen", "ts": time.time(), "status": "shown", "items": [x["id"]]})
    answer.value = [{"id": x["id"], "do": "delete", "why": "x"}]
    assert tidy.run(bot)["deleted"] == [] and x["id"] in _ids(bot)


def test_unparseable_answer_or_failure_changes_nothing(bot, answer, requests):
    _fill(bot, 30)
    _lesson(bot, "Stale lesson", count=1, idle=30)
    before = _ids(bot)
    answer.value = "I would delete the stale one."
    assert tidy.run(bot) == {"deleted": [], "merged": [], "promoting": [], "over_limit": []}
    assert tidy.last(bot)["error"]
    answer.value = RuntimeError("boom")
    tidy.run(bot)
    assert _ids(bot) == before and requests == [] and memory.archived(bot) == []


# ------------------------------------------------------------------ tidy: promote

def test_valid_promotions_go_into_one_request(bot, answer, requests):
    _fill(bot, 30)
    a = _lesson(bot, "Heavy A", count=2, cited=3, idle=1)
    b = _lesson(bot, "Heavy B", count=5, idle=1)
    answer.value = [{"id": a["id"], "do": "promote", "why": "used"}, {"id": b["id"], "do": "promote"}]
    r = tidy.run(bot)
    assert requests == [[a["id"], b["id"]]] and r["promoting"] == [a["id"], b["id"]]
    assert {a["id"], b["id"]} <= _ids(bot)  # they stay until the user approves
    assert [x["id"] for x in tidy.last(bot)["promoting"]] == [a["id"], b["id"]]


@pytest.mark.parametrize("case", ["light", "contradicted", "rejected"])
def test_model_promote_outside_the_guard_rails_is_ignored(bot, answer, requests, case):
    _fill(bot, 30)
    kw = {"light": {"count": 2, "cited": 2}, "contradicted": {"count": 6, "contradicted": 1},
          "rejected": {"count": 6, "rejected": True}}[case]
    x = _lesson(bot, "Maybe heavy", idle=1, **kw)
    answer.value = [{"id": x["id"], "do": "promote"}]
    assert tidy.run(bot)["promoting"] == [] and requests == []


def test_no_promotion_while_a_proposal_is_open(bot, answer, requests):
    _fill(bot, 30)
    x = _lesson(bot, "Heavy", count=6, idle=1)
    proposals._save(bot, {"id": "pwait", "ts": time.time(), "status": "ready", "items": ["mother"]})
    answer.value = [{"id": x["id"], "do": "promote"}]
    assert tidy.run(bot)["promoting"] == [] and requests == []


# ------------------------------------------------------------------ tidy: merge

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
    r = tidy.run(bot)
    assert len(r["merged"]) == 2
    m1, m2 = r["merged"]
    assert m1["from"] == [a["id"], b["id"]] and m1["claim"] == "Charts in canvases use the dark theme"
    items = {i["id"]: i for i in memory.items(bot)}
    n1, n2 = items[m1["into"]], items[m2["into"]]
    assert a["id"] not in items and b["id"] not in items and n1["kind"] == "lesson"
    assert n1["sessions"] == ["s1", "s2", "s3"] and n1["count"] == 2 and n1["cited"] == 3
    assert n1["trust"] == "outcome" and n1["about"] == ["/x"]
    assert abs(n1["created"] - a["created"]) < 1 and abs(n1["updated"] - b["updated"]) < 1
    assert n1["merged_from"] == [{"id": a["id"], "claim": a["claim"]}, {"id": b["id"], "claim": b["claim"]}]
    assert n2["trust"] == "user" and n2["count"] == 1 and len(n2["merged_from"]) == 3
    arch = {x["id"]: x for x in memory.archived(bot)}
    assert arch[a["id"]]["reason"] == "merged" and arch[a["id"]]["into"] == n1["id"]
    assert arch[e["id"]]["into"] == n2["id"]
    last = tidy.last(bot)["merged"]
    assert last[0]["into"] == n1["id"] and last[0]["from"][0] == {"id": a["id"], "claim": a["claim"]}


def test_merge_count_takes_the_largest_original_when_bigger(bot, answer):
    _fill(bot, 30)
    a = _lesson(bot, "Lesson one", count=4, idle=1, sessions=["s1"])
    b = _lesson(bot, "Lesson two", count=1, idle=1, sessions=["s1"])
    answer.value = [{"do": "merge", "ids": [a["id"], b["id"]], "claim": "Lessons one and two"}]
    into = tidy.run(bot)["merged"][0]["into"]
    assert next(i for i in memory.items(bot) if i["id"] == into)["count"] == 4


def test_merged_count_is_the_largest_original_not_the_session_union(bot, answer):
    _fill(bot, 30)
    a = _lesson(bot, "Charts use the dark theme", count=1, idle=1, sessions=["s1"])
    b = _lesson(bot, "Charts are dark", count=1, idle=1, sessions=["s2"])
    answer.value = [{"do": "merge", "ids": [a["id"], b["id"]], "claim": "Canvas charts use the dark theme"}]
    into = tidy.run(bot)["merged"][0]["into"]
    merged = next(i for i in memory.items(bot) if i["id"] == into)
    assert merged["count"] == 1 and merged["sessions"] == ["s1", "s2"]
    assert not memory.indexed(merged)  # two lessons seen once each are not a lesson seen twice
    # The once-per-session bump rule still holds against the session union.
    assert memory.bump(bot, into, session="s2")["count"] == 1
    bumped = memory.bump(bot, into, session="s9")
    assert bumped["count"] == 2 and memory.indexed(bumped)


def test_merge_equal_to_an_original_folds_into_it(bot, answer):
    _fill(bot, 30)
    a = _lesson(bot, "Charts use the dark theme", count=1, idle=1, sessions=["s1"])
    b = _lesson(bot, "Dark charts", count=1, idle=1, sessions=["s2"])
    answer.value = [{"do": "merge", "ids": [a["id"], b["id"]], "claim": "charts use the dark theme."}]
    r = tidy.run(bot)
    assert r["merged"][0]["into"] == a["id"]
    kept = next(i for i in memory.items(bot) if i["id"] == a["id"])
    assert kept["claim"] == "Charts use the dark theme" and kept["count"] == 1
    assert kept["merged_from"] == [{"id": b["id"], "claim": "Dark charts"}]
    assert b["id"] not in _ids(bot) and memory.archived(bot)[0]["into"] == a["id"]


@pytest.mark.parametrize("case", ["heavy", "contradicted", "proposal", "empty", "long", "one", "four", "missing"])
def test_merge_outside_the_guard_rails_is_ignored(bot, answer, case):
    _fill(bot, 30)
    a = _lesson(bot, "Lesson alpha", count=1, idle=1)
    b = _lesson(bot, "Lesson beta", count=1, idle=1,
                **{"heavy": {"cited": 5}, "contradicted": {"contradicted": 1}}.get(case, {}))
    c = _lesson(bot, "Lesson gamma", count=1, idle=1)
    d = _lesson(bot, "Lesson delta", count=1, idle=1)
    if case == "proposal":
        proposals._save(bot, {"id": "popen", "ts": time.time(), "status": "preparing", "items": [b["id"]]})
    ids = {"one": [a["id"]], "four": [a["id"], b["id"], c["id"], d["id"]],
           "missing": [a["id"], "mnope"]}.get(case, [a["id"], b["id"]])
    claim = {"empty": "  ", "long": "x" * 401}.get(case, "Alpha and beta")
    answer.value = [{"do": "merge", "ids": ids, "claim": claim}]
    before = _ids(bot)
    assert tidy.run(bot)["merged"] == [] and _ids(bot) == before


def test_an_id_belongs_to_the_first_action_naming_it(bot, answer, requests):
    _fill(bot, 30)
    a = _lesson(bot, "Lesson alpha", count=1, idle=1)
    b = _lesson(bot, "Lesson beta", count=1, idle=1)
    c = _lesson(bot, "Lesson gamma", count=1, idle=30)
    answer.value = [{"id": a["id"], "do": "keep"},
                    {"do": "merge", "ids": [a["id"], b["id"]], "claim": "Alpha beta"},  # a is taken: dropped
                    {"do": "merge", "ids": [b["id"], c["id"]], "claim": "Beta gamma"},
                    {"id": c["id"], "do": "delete"}]  # c is taken by the merge
    r = tidy.run(bot)
    assert [m["from"] for m in r["merged"]] == [[b["id"], c["id"]]] and r["deleted"] == []
    assert a["id"] in _ids(bot)


def test_at_most_five_merges(bot, answer):
    _fill(bot, 25)
    pairs = [(_lesson(bot, f"Pair {k} left", idle=1), _lesson(bot, f"Pair {k} right", idle=1)) for k in range(6)]
    answer.value = [{"do": "merge", "ids": [x["id"], y["id"]], "claim": f"Pair {k}"}
                    for k, (x, y) in enumerate(pairs)]
    assert len(tidy.run(bot)["merged"]) == 5


# ------------------------------------------------------------------ hard limit

def test_hard_limit_removes_least_used_non_user_first(bot, answer):
    _budget(bot, limit=5, tidy_at=3)
    u1 = _lesson(bot, "User lesson one", trust="user", count=0, idle=90)
    u2 = _lesson(bot, "User lesson two", trust="user", count=1, idle=90)
    o_low = _lesson(bot, "Outcome rarely", count=1, idle=1)
    o_idle = _lesson(bot, "Outcome idle", count=2, idle=50)
    o_fresh = _lesson(bot, "Outcome fresh", count=2, idle=1)
    o_held = _lesson(bot, "Outcome held", count=0, idle=90)
    o_busy = _lesson(bot, "Outcome busy", count=9, idle=1)
    proposals._save(bot, {"id": "popen", "ts": time.time(), "status": "shown", "items": [o_held["id"]]})
    answer.value = "not json"
    r = tidy.run(bot)
    # 7 lessons, limit 5: the lowest-use non-user lesson, then the longer idle of the two with 2 uses.
    assert r["over_limit"] == [o_low["id"], o_idle["id"]]
    assert {u1["id"], u2["id"], o_fresh["id"], o_held["id"], o_busy["id"]} == _ids(bot)
    assert {x["reason"] for x in memory.archived(bot)} == {"over_limit"}
    assert tidy.last(bot)["counts"]["over_limit"] == 2


def test_hard_limit_takes_user_lessons_only_when_nothing_else_is_left(bot, answer):
    _budget(bot, limit=2, tidy_at=10)
    u = [_lesson(bot, f"User {k}", trust="user", count=k, idle=k) for k in range(3)]
    o = _lesson(bot, "Outcome", count=9, idle=0)
    held = _lesson(bot, "Held", count=9, idle=0)
    proposals._save(bot, {"id": "popen", "ts": time.time(), "status": "shown", "items": [held["id"]]})
    r = tidy.run(bot)
    assert r["over_limit"] == [o["id"], held["id"], u[0]["id"]]
    assert _ids(bot) == {u[1]["id"], u[2]["id"]} and answer.calls == []


def test_add_never_refuses_over_the_limit(bot):
    _budget(bot, limit=1)
    memory.add(bot, "one")
    memory.add(bot, "two")
    assert len(memory.items(bot)) == 2


# ------------------------------------------------------------------ HTTP

BID = "e2e2e2e2"


@pytest.fixture
def expert(client, monkeypatch):
    from fused_render.bots import paths as bpaths
    from fused_render.bots import registry
    from fused_render.bots import store as bstore
    bpaths.place(BID, "Tidy Expert")
    bstore.write_meta(BID, {"id": BID, "name": "Tidy Expert", "kind": "expert", "model": "", "effort": "",
                            "status": "idle", "instructions": "", "created": time.time(), "task": "",
                            "step": 0, "url": None, "title": None, "expert": {}})
    monkeypatch.setattr(model, "available", lambda b: (True, ""))
    return registry.get(BID)


def _get(client, path):
    status, _h, body = client.get(path)
    return status, json.loads(body or b"null")


def test_memory_route_budget_fields_last_tidy_and_archive(client, expert, answer):
    a = memory.add(expert, "Use fused.load", trust="user")
    gone = memory.add(expert, "Forget this one")
    memory.cite(expert, [a["id"]])
    status, body = _get(client, f"/api/experts/{BID}/memory")
    assert status == 200 and body["limit"] == 50 and body["tidy_at"] == 30 and body["total"] == 2
    assert body["last_tidy"] is None
    row = body["items"][0]
    assert row["cited"] == 1 and row["last_used"] and row["uses"] == 2
    assert body["items"][1]["cited"] == 0 and body["items"][1]["last_used"] is None
    memory.forget(expert, gone["id"])
    status, body = _get(client, f"/api/experts/{BID}/memory/archive")
    assert status == 200 and [(r["id"], r["reason"]) for r in body["archived"]] == [(gone["id"], "forgotten")]
    _fill(expert, 30)
    answer.value = []
    tidy.run(expert)
    _s, body = _get(client, f"/api/experts/{BID}/memory")
    assert body["last_tidy"]["reviewed"] is True and body["last_tidy"]["counts"]["deleted"] == 0
    status, _b = _get(client, "/api/experts/nobody/memory/archive")
    assert status == 404


def test_post_consolidate_starts_for_tidy_alone(client, expert, answer):
    _fill(expert, 31)
    status, _h, body = client.post(f"/api/experts/{BID}/consolidate", {"force": True})
    assert status == 200 and json.loads(body) == {"started": True}
    end = time.time() + 5
    while consolidate.status(expert)["running"] and time.time() < end:
        time.sleep(0.02)
    assert tidy.last(expert) is not None
