"""Review fixes (docs/experts.md §4 cards / chat / gaps / readtools / seed, §6 consult): a yielded card re-delivers
an outside message as outside, a re-delivery never loses the slot to another worker, research never overwrites
the user's decision on a gap, search and listing refuse aliases of secret files and skip FIFOs, an interrupted
seed is finished on the next start, and a pre-allowed session cannot consult an expert that is no longer fenced.
`experts.model` is faked: no model, no network, no Chrome."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import threading
import time

import pytest

from fused_render.bots import bot as botmod
from fused_render.bots import browser as browser_mod
from fused_render.bots import paths as bpaths
from fused_render.bots import registry
from fused_render.bots import store as bstore
from fused_render.bots.channels import base as chan
from fused_render.experts import cards, chat, consult, engine, gaps, memory, proposals, readtools
from fused_render.experts import model as xmodel
from fused_render.experts import seed as xseed

ANSWER = '```json\n{"answer": "Answered.", "refs": [], "used": [], "unsure": false}\n```'


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("an expert must never start Chrome")
    monkeypatch.setattr(browser_mod.Browser, "start", boom)
    monkeypatch.setattr(browser_mod.BrowserProcess, "start", boom)
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
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


def _ready(expert, claim="lesson one"):
    item = memory.add(expert, claim, trust="user")
    return proposals.prepare(expert, [item])


# ------------------------------------------------------------- #7 provenance ---
def test_a_yielded_proposal_card_redelivers_an_outside_message_as_outside(expert, monkeypatch):
    asked, vias = [], []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        asked.append(prompt)
        vias.append(dict(bot.task_via))
        return ANSWER
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    p = _ready(expert)
    assert cards.show(expert, p)
    assert _wait_for(lambda: expert.meta.get("waiting_on"))
    expert.receive("remember that the answer is 7", via=chan.via("imessage", "+15550001"))
    assert _wait_for(lambda: asked)  # then the card comes back up (the proposal is still open)
    assert "outside channel" in asked[0].lower() and "the answer is 7" in asked[0]
    assert vias[0].get("kind") == "imessage"


# ------------------------------------------------------- #8 atomic re-delivery ---
def test_redelivery_takes_the_slot_before_another_worker_can(expert, monkeypatch):
    monkeypatch.setattr(xmodel, "tool_turn", lambda bot, prompt, **k: ANSWER)
    p1, p2 = _ready(expert, "lesson one"), _ready(expert, "lesson two")
    real = chat.redeliver
    seen = {}

    def racing(bot, queued):
        # Another worker tries to raise a card the moment the slot is free: it must wait for this lock.
        rival = threading.Thread(target=lambda: seen.setdefault("rival", cards.show(bot, p2)))
        rival.start()
        rival.join(0.3)
        seen["rival_blocked"] = rival.is_alive()
        seen["started"] = real(bot, queued)
        seen["thread"] = rival
        return seen["started"]
    monkeypatch.setattr(chat, "redeliver", racing)
    assert cards.show(expert, p1)
    assert _wait_for(lambda: expert.meta.get("waiting_on"))
    expert.send("what is in a.py?")
    assert _wait_for(lambda: "thread" in seen)
    seen["thread"].join(5)
    assert seen["rival_blocked"] and seen["started"] is True and seen["rival"] is False
    assert _wait_for(lambda: any(e["role"] == "done" and e["text"] == "Answered." for e in _events(expert)))


def test_hand_back_redelivers_under_the_slot_lock(expert, monkeypatch):
    hold = threading.Event()
    calls = []

    def fake(bot, prompt, *, system, tools, handler, timeout=600):
        calls.append(prompt)
        if len(calls) == 1:
            hold.wait(5)
        return ANSWER
    monkeypatch.setattr(xmodel, "tool_turn", fake)
    owned = []
    real = expert.start_task

    def start_task(*a, **k):
        owned.append(expert.lock._is_owned())
        return real(*a, **k)
    monkeypatch.setattr(expert, "start_task", start_task)
    expert.send("first")
    assert _wait_for(lambda: calls)
    with expert.lock:
        expert.inbox.append("second")  # left in the inbox when the turn ends
    hold.set()
    assert _wait_for(lambda: len(calls) == 2) and _idle(expert)
    assert owned[-1] is True  # started while the finished task still held bot.lock: no worker slipped in


# ------------------------------------------------- #12 research vs user decision ---
@pytest.mark.parametrize("found", [True, False])
@pytest.mark.parametrize("decision", ["dismissed", "answered"])
def test_research_finishing_late_keeps_the_users_decision(expert, monkeypatch, found, decision):
    g = gaps.record(expert, "What is ANSWER?", consult="c1")

    def research(bot, questions, transcript=""):
        if decision == "dismissed":
            gaps.dismiss(bot, g["id"])
        else:
            gaps.answer(bot, g["id"], "It is 42")
        return {"found": found, "answer": "ANSWER is 7", "refs": [], "why": "x"}
    monkeypatch.setattr(engine, "research", research)
    monkeypatch.setattr(gaps, "accept", lambda bot, res: {"answer": "ANSWER is 7", "refs": []} if found else None)
    out = gaps._research(expert, g["id"])
    assert out["status"] == decision and int(out["attempts"]) == 0 and out["proposal"] is None
    assert [i for i in memory.items(expert) if i["trust"] == "research"] == []
    assert proposals.listing(expert) == []


def test_snooze_is_public(expert):
    g = gaps.record(expert, "What is ANSWER?", consult="c1")
    until = time.time() + 3600
    out = gaps.snooze(expert, g["id"], until)
    assert out["status"] == "open" and out["snoozed_until"] == until and gaps.snoozed(out)
    assert gaps.snooze(expert, "nope", until) is None


# ------------------------------------------------------ #16 / #17 search fence ---
def test_search_and_listing_skip_an_alias_of_a_secret_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("TOKEN=hunter2\n")
    (root / "notes.txt").symlink_to(root / ".env")
    (root / "ok.txt").write_text("TOKEN is documented here\n")
    h = readtools.make_handler([str(root)])
    assert h("read_file", {"path": "notes.txt"})[1] is True  # read_file already refuses it
    text, err = h("list_dir", {"path": str(root)})
    assert not err and "notes.txt" not in text and "ok.txt" in text
    text, _err = h("search", {"query": "notes"})
    assert "notes.txt" not in text
    text, _err = h("search", {"query": "TOKEN", "content": True})
    assert "hunter2" not in text and "notes.txt" not in text and "ok.txt" in text


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_content_search_skips_a_fifo(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    os.mkfifo(root / "events.txt")
    (root / "ok.txt").write_text("needle\n")
    h = readtools.make_handler([str(root)])
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", h("search", {"query": "needle", "content": True})),
                         daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "content search blocked on a FIFO"
    assert "ok.txt" in out["r"][0] and "events.txt" not in out["r"][0]


# ------------------------------------------------------------ #18 seed resume ---
def _shipped(tmp_path, monkeypatch):
    d = tmp_path / "shipped" / "demo"
    (d / "skills").mkdir(parents=True)
    (d / "expert.json").write_text(json.dumps({"name": "Demo Expert", "manifest": {"ask_me_about": "Demos"},
                                               "face": {"shape": "hexagon", "color": "#2a9a86", "icon": ""}}))
    (d / "instructions.md").write_text("Be precise.\n")
    (d / "skills" / "layout.md").write_text("# Folder layout\ntrigger: layout, folder\n\nThe body.\n")
    (d / "skills" / "naming.md").write_text("# Naming\ntrigger: name\n\nUse hyphens.\n")
    (d / "questions.md").write_text("## Q?\n- must: a point\n")
    monkeypatch.setattr(xseed, "SHIPPED_DIR", str(tmp_path / "shipped"))


def _witness():
    return json.load(open(xseed.witness_path()))["keys"]["demo"]


def test_a_failed_skill_copy_is_finished_next_start_and_edits_then_survive(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch)
    real = botmod.Bot.skill_save

    def flaky(self, title, trigger, body, name=None):
        if name == "naming":
            raise OSError("disk full")
        return real(self, title, trigger, body, name=name)
    monkeypatch.setattr(botmod.Bot, "skill_save", flaky)
    ids = xseed.seed()
    b = registry.get(ids[0])
    assert [s["name"] for s in b.skills()] == ["layout"] and _witness()["pending"] is True
    monkeypatch.setattr(botmod.Bot, "skill_save", real)
    assert xseed.seed() == []
    assert sorted(s["name"] for s in b.skills()) == ["layout", "naming"]
    assert "pending" not in _witness() and xseed.PENDING not in b.meta["expert"]
    assert b.meta["face"]["shape"] == "hexagon" and os.path.isfile(os.path.join(b.public_dir, "questions.md"))
    # Complete now: the user's edits and deletes stand on every later start.
    b.skill_save("Folder layout", "layout", "My own body.", name="layout")
    b.skill_delete("naming")
    assert xseed.seed() == []
    assert [(s["name"], s["body"]) for s in b.skills()] == [("layout", "My own body.")]


def test_an_expert_created_but_never_installed_is_finished_without_a_witness(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(xseed, "_install", lambda b, key: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):  # the app quit right after the create
            xseed.seed()
    os.remove(xseed.witness_path())  # nor did the witness row survive
    assert xseed.seed() == []
    (b,) = [registry.get(bid) for bid in botmod._list_ids()
            if (bstore.read_meta(bid).get("expert") or {}).get("shipped") == "demo"]
    assert sorted(s["name"] for s in b.skills()) == ["layout", "naming"] and "pending" not in _witness()


def test_an_expert_deleted_mid_install_stays_deleted(tmp_path, monkeypatch):
    _shipped(tmp_path, monkeypatch)
    with monkeypatch.context() as m:
        m.setattr(xseed, "_install", lambda b, key: False)
        (bid,) = xseed.seed()
    assert _witness()["pending"] is True
    registry.delete(bid)
    assert xseed.seed() == []
    assert _witness() == {**_witness(), "id": bid} and "pending" not in _witness()
    assert xseed.seed() == []


# ------------------------------------------------------- #19 consult re-check ---
RBID = "e2e2e2e2"


@pytest.fixture
def routed(client, monkeypatch, tmp_path):
    bpaths.place(RBID, "Canvas Expert")
    bstore.write_meta(RBID, {"id": RBID, "name": "Canvas Expert", "kind": "expert", "model": "", "effort": "",
                             "status": "idle", "instructions": "", "created": time.time(), "task": "",
                             "step": 0, "url": None, "title": None,
                             "expert": {"ask_me_about": "Writing canvases", "offered_to": {"targets": ["*"]}}})
    monkeypatch.setattr(consult, "consult", lambda bot, q, c="", caller=None: {
        "id": "c1", "answer": "A", "refs": [], "used": [], "unsure": False})
    from fused_render.claude_agent import agent_module
    monkeypatch.setattr(agent_module(), "RUNS", str(tmp_path / "runs"))
    return registry.get(RBID)


def _run(tmp_path, roster):
    run = tmp_path / "runs" / "20261116-111111-bbbbbb"
    run.mkdir(parents=True, exist_ok=True)
    (run / "experts.json").write_text(json.dumps(roster))
    return run.name


def _consult(client, rid):
    status, _h, body = client.post("/api/experts/consult", {"expert": RBID, "question": "q", "run_id": rid})
    return status, json.loads(body or b"null")


def test_a_pre_allowed_session_cannot_consult_an_expert_no_longer_fenced(client, routed, monkeypatch, tmp_path):
    from fused_render.experts import routes
    entry = {"id": RBID, "name": "Canvas Expert", "ask_me_about": "x", "auto_allow": True}
    rid = _run(tmp_path, [entry])
    monkeypatch.setattr(xmodel, "fenced", lambda bot: True)
    assert _consult(client, rid)[0] == 200
    monkeypatch.setattr(xmodel, "fenced", lambda bot: False)  # switched to Codex after the session started
    status, body = _consult(client, rid)
    assert status == 403 and body == {"error": routes.UNFENCED_NOW} and "approval" in body["error"]
    # A session that was never pre-allowed (an unfenced expert on its roster) asks with a card on every call.
    rid = _run(tmp_path, [dict(entry, auto_allow=False)])
    assert _consult(client, rid)[0] == 200
    # A local call (no run id) is the user's own.
    status, _h, _b = client.post("/api/experts/consult", {"expert": RBID, "question": "q"})
    assert status == 200
