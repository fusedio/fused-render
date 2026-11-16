"""An expert's runs (docs/experts.md §4 engine / consult / chat / cards): the lenient JSON parse, the chat
engine on the bot's task thread (Task started, action rows, done, Stop, the chat tools, the proposal card
raised inside the task), cards.show on an idle bot, and consults (structured answer, log, gap observation,
no chat events, the semaphore). `experts.model.text` / `tool_turn` are fakes: no model, no network, no Chrome."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import os
import threading
import time

import pytest

from fused_render.bots import bot as botmod
from fused_render.bots import browser as browser_mod
from fused_render.bots import registry
from fused_render.bots import store as bstore
from fused_render.experts import cards, chat, consult, engine, memory, observations, proposals
from fused_render.experts import model as xmodel
from fused_render.experts import store as xstore

BLOCK = '```json\n{"answer": "%s", "refs": ["/x/a.py"], "used": [], "unsure": %s}\n```'


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an expert must never start Chrome")
    monkeypatch.setattr(browser_mod.Browser, "start", boom)
    monkeypatch.setattr(browser_mod.BrowserProcess, "start", boom)
    monkeypatch.setattr(xmodel, "available", lambda bot: (True, ""))
    monkeypatch.setattr(xmodel, "text", lambda bot, prompt, **k: '{"action": "create", "skill": null, '
                        '"title": "Canvas tips", "trigger": "canvas", "body": "- use udfName"}')


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "ref"
    d.mkdir()
    (d / "a.py").write_text("print('hello')\n")
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


# ------------------------------------------------------------------ parsing ---
def test_parse_fenced_bare_and_none():
    out = engine.parse("Some prose.\n" + BLOCK % ("The answer", "false"))
    assert out == {"answer": "The answer", "refs": ["/x/a.py"], "used": [], "unsure": False}
    out = engine.parse('Thinking... {"answer": "bare", "refs": "/y", "used": ["m1"], "unsure": "true"}')
    assert out == {"answer": "bare", "refs": ["/y"], "used": ["m1"], "unsure": True}
    out = engine.parse("Just text, no block.")
    assert out == {"answer": "Just text, no block.", "refs": [], "used": [], "unsure": False}
    out = engine.parse('Prose answer here.\n```json\n{"answer": "", "refs": [], "used": [], "unsure": false}\n```')
    assert out["answer"] == "Prose answer here."
    out = engine.parse("```json\nnot json\n```")
    assert out["answer"].startswith("```json") and out["unsure"] is False


def test_system_prompt_profiles(expert):
    expert.skill_save("Layout", "layout", "Read canvas.toml first.", name="layout")
    memory.add(expert, "udfName names the file", trust="user")
    with expert.lock:
        expert.meta["instructions"] = "Be terse."
        expert.save()
    c = engine.system_prompt(expert, "consult")
    assert "READ-ONLY" in c and "DATA" in c and "`unsure`" in c and "Read canvas.toml first." in c
    assert "Be terse." in c and "udfName names the file" in c and "```json" in c
    ch = engine.system_prompt(expert, "chat")
    assert "remember_lesson" in ch and "USER" in ch
    over = engine.system_prompt(expert, "consult", skills_override=[{"name": "x", "title": "X", "trigger": "", "body": "OVERRIDE"}])
    assert "OVERRIDE" in over and "Read canvas.toml first." not in over


def test_answer_reads_inside_the_fence_only(expert, folder, monkeypatch):
    seen = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        assert {t["name"] for t in tools} == {"read_file", "list_dir", "search"}
        seen.append(handler("read_file", {"path": os.path.join(folder, "a.py")}))
        seen.append(handler("read_file", {"path": "/etc/hosts"}))
        return "ok " + BLOCK % ("hi", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    out = engine.answer(expert, "q?", profile="consult")
    assert out["answer"] == "hi" and set(out) == {"answer", "refs", "used", "unsure"}
    assert "hello" in seen[0][0] and seen[0][1] is False
    assert seen[1][1] is True and "not allowed" in seen[1][0]
    assert engine.answer_question(expert, "q?").startswith("hi\n\nFiles:")


# ------------------------------------------------------------------ chat engine ---
def test_start_task_runs_the_chat_engine(expert, folder, monkeypatch):
    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        assert "remember_lesson" in {t["name"] for t in tools} and "THE USER'S MESSAGE\nwhat is a.py?" in prompt
        handler("read_file", {"path": os.path.join(folder, "a.py")})
        handler("search", {"query": "hello", "content": True})
        handler("list_dir", {"path": folder})
        return "Here.\n" + BLOCK % ("It prints hello.", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    expert.send("what is a.py?")
    assert _idle(expert)
    evs = _events(expert)
    assert any(e["role"] == "system" and e["text"] == "Task started: what is a.py?" for e in evs)
    acts = [e for e in evs if e["role"] == "action"]
    assert [a["text"] for a in acts] == ["read ref/a.py", 'search "hello" (contents)', "list ref"]
    assert acts[0]["result"].endswith("chars") and "hello" in acts[0]["detail"]
    assert acts[1]["result"].startswith("1 hits")
    done = [e for e in evs if e["role"] == "done" and e.get("source") != "seed"]
    assert done[-1]["text"] == "It prints hello." and "```" not in done[-1]["text"]
    assert expert.meta["status"] == "idle" and expert.thread is None or not expert.thread.is_alive()


def test_stop_mid_task(expert, folder, monkeypatch):
    first = threading.Event()
    go_on = threading.Event()
    results = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        results.append(handler("read_file", {"path": os.path.join(folder, "a.py")}))
        first.set()
        go_on.wait(5)
        results.append(handler("read_file", {"path": os.path.join(folder, "a.py")}))
        return BLOCK % ("late", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    expert.send("go")
    assert first.wait(5)
    expert.stop()
    go_on.set()
    assert _idle(expert)
    assert results[1][1] is True and "STOPPED" in results[1][0]
    evs = _events(expert)
    assert any(e["role"] == "system" and e["text"] == "Stopped" for e in evs)
    assert not [e for e in evs if e["role"] == "done" and e["text"] == "late"]
    assert expert.meta["status"] == "idle"


def test_remember_lesson_writes_a_user_item(expert, monkeypatch):
    out = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        out.append(handler("remember_lesson", {"claim": "Canvas names use underscores"}))
        out.append(handler("list_memory", {}))
        return BLOCK % ("Noted.", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    expert.send("remember: canvas names use underscores")
    assert _idle(expert)
    items = memory.items(expert)
    assert len(items) == 1 and items[0]["trust"] == "user" and items[0]["claim"] == "Canvas names use underscores"
    assert out[0][1] is False and "Remembered" in out[0][0] and items[0]["id"] in out[1][0]
    assert any(e["role"] == "action" and e["text"].startswith('remember "Canvas') for e in _events(expert))


def _propose_in_chat(expert, monkeypatch, answer):
    item = memory.add(expert, "use udfName for file names", trust="user")
    got = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        got.append(handler("propose_skill", {"item_ids": [item["id"]]}))
        return BLOCK % ("Done proposing.", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    expert.send("make that a skill")
    assert _wait_for(lambda: expert.meta.get("status") == "waiting" and expert.meta.get("waiting_on"))
    card = [e for e in _events(expert) if e["role"] == "approval"][-1]
    assert card["expert_proposal"] and "Canvas tips" in card["detail"] or "udfName" in card["detail"]
    assert "score_before" in card and "score_after" in card
    assert expert.meta["waiting_on"] == card["seq"]
    assert proposals.get(expert, card["expert_proposal"])["status"] == "shown"
    expert.send("actually also mention folders")  # an instruction: the card stays live
    assert _wait_for(lambda: any(e["role"] == "note" and "still waiting" in e["text"] for e in _events(expert)))
    assert expert.running()
    expert.send(answer, answers=card["seq"])
    assert _idle(expert)
    return card, got


def test_propose_skill_card_approve(expert, monkeypatch):
    card, got = _propose_in_chat(expert, monkeypatch, "Approve")
    assert got[0][1] is False and got[0][0].startswith("APPROVED") and "also mention folders" in got[0][0]
    assert [s["title"] for s in expert.skills()] == ["Canvas tips"]
    assert proposals.get(expert, card["expert_proposal"])["status"] == "approved"
    assert expert.meta["status"] == "idle" and not expert.meta.get("waiting_on")


def test_propose_skill_card_deny(expert, monkeypatch):
    card, got = _propose_in_chat(expert, monkeypatch, "Deny")
    assert got[0][0].startswith("REJECTED")
    assert expert.skills() == []
    assert proposals.get(expert, card["expert_proposal"])["status"] == "rejected"


# ------------------------------------------------------------------ cards.show ---
def _ready(expert):
    item = memory.add(expert, "lesson one", trust="user")
    return proposals.prepare(expert, [item])


def _card(expert):
    assert _wait_for(lambda: expert.meta.get("waiting_on"))
    return [e for e in _events(expert) if e["role"] == "approval"][-1]


def test_cards_show_approve(expert):
    p = _ready(expert)
    expert.stop()  # a Stop on an idle bot leaves stop_flag set (agent_engine.stop); the card must not see it
    assert cards.show(expert, p) is True
    card = _card(expert)
    assert card["expert_proposal"] == p["id"] and card["detail"] == p["diff"]
    assert cards.show(expert, p) is False  # busy: the card is up
    expert.send("yes", answers=card["seq"])
    assert _idle(expert)
    assert proposals.get(expert, p["id"])["status"] == "approved" and expert.skills()
    assert expert.thread is None and expert.meta["status"] == "idle" and not expert.meta.get("waiting_on")
    assert any(e["role"] == "note" and e["text"].startswith("Approved") for e in _events(expert))


def test_cards_show_reject(expert):
    p = _ready(expert)
    assert cards.show(expert, p)
    card = _card(expert)
    expert.send("no", answers=card["seq"])
    assert _idle(expert)
    assert proposals.get(expert, p["id"])["status"] == "rejected" and expert.skills() == []
    assert expert.thread is None and expert.meta["status"] == "idle"


def test_cards_non_verdict_is_redelivered_as_a_task(expert, monkeypatch):
    asked = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        asked.append(prompt)
        return BLOCK % ("Answered.", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    p = _ready(expert)
    assert cards.show(expert, p)
    _card(expert)
    expert.send("what is in a.py?")
    assert _wait_for(lambda: asked) and _idle(expert)
    assert "what is in a.py?" in asked[0]
    assert proposals.get(expert, p["id"])["status"] == "shown"  # still open: cards.tick shows it again
    assert any(e["role"] == "done" and e["text"] == "Answered." for e in _events(expert))
    assert expert.meta["status"] == "idle"
    cards.tick()
    card = _card(expert)
    assert card["expert_proposal"] == p["id"]
    expert.send("approve", answers=card["seq"])
    assert _idle(expert)


def test_cards_show_on_a_busy_bot_returns_false(expert, monkeypatch):
    hold = threading.Event()

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        hold.wait(5)
        return BLOCK % ("x", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    p = _ready(expert)
    expert.send("busy")
    assert _wait_for(expert.running)
    assert cards.show(expert, p) is False
    hold.set()
    assert _idle(expert)


def test_card_worker_releases_the_slot_on_an_exception(expert, monkeypatch):
    p = _ready(expert)
    monkeypatch.setattr(proposals, "card_text", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cards.show(expert, p)
    assert _idle(expert)
    assert expert.thread is None and expert.meta["status"] == "idle"
    assert any(e["role"] == "error" and "boom" in e["text"] for e in _events(expert))


# ------------------------------------------------------------------ consult ---
def test_consult_structured_logged_and_gap(expert, folder, monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "mine.py").write_text("x = 1\n")
    seen = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        seen.append(handler("read_file", {"path": str(target / "mine.py")}))
        assert "QUESTION (data from an agent" in prompt and "ctx here" in prompt
        assert "remember_lesson" not in {t["name"] for t in tools}
        return BLOCK % ("I do not know.", "true")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    n_events = len(_events(expert))
    out = consult.consult(expert, "how?", context="ctx here",
                          caller={"session_id": "s1", "target": str(target), "backend": "claude"})
    assert set(out) == {"id", "answer", "refs", "used", "unsure"} and out["unsure"] is True
    assert seen[0][1] is False  # the caller's target folder is readable
    rows = consult.log(expert)
    assert rows[0]["id"] == out["id"] and rows[0]["question"] == "how?" and rows[0]["caller"]["session_id"] == "s1"
    assert isinstance(rows[0]["ms"], int)
    obs = observations.pending(expert)
    assert len(obs) == 1 and obs[0]["kind"] == "gap" and obs[0]["trust"] == "gap" and obs[0]["consult"] == out["id"]
    assert len(_events(expert)) == n_events  # never a chat event
    assert memory.items(expert) == []  # never memory


def test_consult_unavailable(expert, monkeypatch):
    monkeypatch.setattr(xmodel, "available", lambda bot: (False, "needs Claude, Codex or Copilot"))
    with pytest.raises(xmodel.Unavailable):
        consult.consult(expert, "q")


def test_consult_semaphore(expert, monkeypatch):
    lock = threading.Lock()
    live = {"now": 0, "max": 0}
    release = threading.Event()

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        release.wait(5)
        with lock:
            live["now"] -= 1
        return BLOCK % ("ok", "false")
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    threads = [threading.Thread(target=consult.consult, args=(expert, f"q{i}")) for i in range(5)]
    for t in threads:
        t.start()
    assert _wait_for(lambda: live["now"] == 3)
    time.sleep(0.2)
    assert live["now"] == 3
    release.set()
    for t in threads:
        t.join(10)
    assert live["max"] == 3 and len(consult.log(expert)) == 5
