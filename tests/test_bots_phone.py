"""`/api/phone/bots/*` (fused_render/bots/phone.py, docs/bots.md §14) through the
real server: the list, the detail and its paging, a send stamped with the phone
via, the approval answer stamp (a real Bot driven to a waiting card by a fake
engine that runs the agent engine's own gate), files, create / settings /
routines / delete, and the ops the phone never gets."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import base64
import json
import os
import time
import types
import urllib.request

import pytest

from fused_render.bots import agent_engine, apptools, steps_engine, tools
from fused_render.bots import bot as botmod
from fused_render.bots import paths as bpaths
from fused_render.bots import registry

P = "/api/phone/bots"
DEV = {"X-Fused-Device": "a1b2c3d4e5f6"}


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    monkeypatch.setenv("FUSED_RENDER_DIR", str(w))
    os.makedirs(bpaths.apps_root())
    monkeypatch.setattr(apptools, "ROOTS", [bpaths.apps_root()])
    monkeypatch.setattr(apptools, "REGISTRY_FILES", [])
    monkeypatch.setattr(apptools, "_apps_cache_at", 0.0)
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    return w


def j(resp):
    status, headers, body = resp
    return status, json.loads(body or b"{}")


def make(client, **body):
    st, out = j(client.post(P, {"name": "Scout", **body}))
    assert st == 200 and out["ok"], out
    return out["id"]


def wait_for(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_list_super_first_and_setup(client, ws):
    a = make(client, model="haiku", instructions="watch prices")
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    sid = out["id"]
    time.sleep(0.01)
    b = make(client, name="Ranger")
    st, out = j(client.get(P))
    assert st == 200 and set(out) == {"bots", "ts", "setup"}
    assert [r["id"] for r in out["bots"]] == [sid, b, a]  # Super Bot, then newest first
    row = out["bots"][2]
    assert row["kind"] == "bot" and row["name"] == "Scout" and row["status"] == "idle" and row["busy"] is False
    assert row["waitingOn"] is None and row["control"] is False and row["routines"] == 0 and row["face"] is None
    assert row["last"] is None or row["last"]["role"] in ("user", "done", "question", "approval", "error")
    assert row["seq"] == 1 and row["updated"] > 0
    assert out["bots"][0]["kind"] == "super" and out["bots"][0]["face"] == botmod.SUPER_FACE
    setup = out["setup"]
    assert setup["models"] == list(botmod.MODELS) and setup["efforts"] == list(botmod.EFFORTS)
    assert setup["claude"]["cached"] in (True, False) and "owner" in setup
    st, out = j(client.get(P + "/presets"))
    assert st == 200 and isinstance(out["presets"], list)


def test_detail_paging_and_event_trim(client, ws):
    bid = make(client)
    b = registry.get(bid)
    for i in range(10):
        b.emit("note", f"n{i}")
    b.emit("action", "click", result="clicked", thumb="12.jpg", trace="Traceback …", detail={"raw": "x" * 2000},
           artifacts=["/Users/me/x.csv"])
    st, out = j(client.get(f"{P}/{bid}?since=0&limit=4"))
    assert st == 200 and set(out) == {"bot", "events", "seq", "first_seq", "ts"}
    assert [e["seq"] for e in out["events"]] == [9, 10, 11, 12] and out["first_seq"] == 9 and out["seq"] == 12
    act = out["events"][-1]
    assert act["thumb"] == f"{P}/{bid}/steps/12.jpg" and act["result"] == "clicked"
    assert "trace" not in act and "artifacts" not in act
    assert len(act["detail"]) == 600 and act["detail"].startswith('{"raw": "xxx')
    # "Load earlier"
    st, out = j(client.get(f"{P}/{bid}?since=0&limit=4&before=9"))
    assert [e["seq"] for e in out["events"]] == [5, 6, 7, 8] and out["first_seq"] == 5
    st, out = j(client.get(f"{P}/{bid}?since=0&limit=50&before=2"))
    assert [e["seq"] for e in out["events"]] == [1]
    # a later poll: everything after the cursor
    b.emit("note", "late")
    st, out = j(client.get(f"{P}/{bid}?since=12"))
    assert [e["text"] for e in out["events"]] == ["late"] and out["seq"] == 13
    st, out = j(client.get(f"{P}/{bid}?since=13"))
    assert out["events"] == [] and out["first_seq"] is None
    d = out["bot"]
    assert d["id"] == bid and d["approval"] == "ask" and d["engine"] == "auto" and d["max_steps"] is None
    assert d["waiting_seq"] is None and d["shot"] is None and d["viewport"] == [1280, 800]
    assert d["browser"] == {"running": False, "url": None, "title": None}
    assert d["routines"] == [] and d["memory"] is None and d["skills"] is None
    st, out = j(client.get(f"{P}/{bid}?detail=1"))
    assert out["bot"]["memory"] == "" and out["bot"]["skills"] == []
    assert j(client.get(f"{P}/nobody"))[0] == 404
    assert j(client.get(f"{P}/{bid}?since=x"))[0] == 400


def test_send_stamps_the_phone_via(client, ws, monkeypatch):
    started = []
    monkeypatch.setattr(botmod.Bot, "start_task",
                        lambda self, task, label=None, origin="manual", via=None, images=None: started.append((task, via, images)))
    bid = make(client)
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "  "}, headers=DEV))
    assert st == 400 and out["error"] == "empty message"
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "find me a laptop", "reply_to": 1}, headers=DEV))
    assert st == 200 and out == {"ok": True, "seq": 2, "held": False, "files": []}
    assert started[0][1] == {"kind": "phone", "addr": "a1b2c3d4e5f6"}
    assert started[0][0].startswith("Replying to your earlier message:\n> Scout created.")
    st, out = j(client.get(f"{P}/{bid}?since=1"))
    ev = out["events"][0]
    assert ev["role"] == "user" and ev["text"] == "find me a laptop" and ev["via"] == {"kind": "phone", "addr": "a1b2c3d4e5f6"}
    assert ev["reply"]["seq"] == 1
    assert out["bot"]["last"]["role"] == "user" and out["bot"]["last"]["text"] == "find me a laptop"
    # Loopback (no header) and a malformed header: addr "".
    client.post(f"{P}/{bid}/send", {"text": "again"})
    client.post(f"{P}/{bid}/send", {"text": "again"}, headers={"X-Fused-Device": "../../etc"})
    assert [s[1]["addr"] for s in started[1:]] == ["", ""]
    # No X-Fused: refused before anything happens.
    req = urllib.request.Request(client.base + f"{P}/{bid}/send", data=b'{"text":"x"}',
                                 headers={"Content-Type": "application/json"}, method="POST")
    assert client._do(req)[0] == 403


def test_files_land_in_files_with_the_attached_line(client, ws, monkeypatch):
    started = []
    monkeypatch.setattr(botmod.Bot, "start_task",
                        lambda self, task, label=None, origin="manual", via=None, images=None: started.append((task, images)))
    bid = make(client)
    png = b"\x89PNG small"
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "what is this", "files": [
        {"name": "shot.png", "data": base64.b64encode(png).decode(),
         "preview": {"media_type": "image/jpeg", "data": "cHJldmlldw=="}},
        {"name": "notes.txt", "data": base64.b64encode(b"hi").decode()}]}, headers=DEV))
    assert st == 200 and out["files"] == ["shot.png", "notes.txt"]
    assert open(os.path.join(bpaths.bot_dir(bid), "files", "shot.png"), "rb").read() == png
    task, images = started[0]
    assert task == "what is this\n\nAttached (in your FILES folder, use `upload` when a page needs them): shot.png, notes.txt"
    assert images == [{"media_type": "image/jpeg", "data": "cHJldmlldw=="}]
    big = base64.b64encode(b"x" * (8 * 1024 * 1024 + 1)).decode()
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "", "files": [{"name": "big.bin", "data": big}]}, headers=DEV))
    assert st == 400 and "too large" in out["error"]


@pytest.fixture
def gated(ws, monkeypatch):
    """A fake agent engine whose task raises ONE approval card through the agent engine's own gate
    (agent_engine._gate) and records the verdict and the posture it ran under."""
    seen = {}

    def run(bot, task, label=None):
        seen["phone_super"] = tools.phone_super(bot)
        sess = types.SimpleNamespace(pending_images=[], ran_calls=set(), denied=set(), current_result=None)
        notes = []
        seen["verdict"], _ = agent_engine._gate(bot, sess, "click \"Buy now\"", "It places the order.", notes)
        seen["notes"] = notes
        bot.emit("done", "finished")
        bot.set_status("idle")
    monkeypatch.setattr(agent_engine, "run", run)
    monkeypatch.setattr(steps_engine, "run", run)  # an ordinary bot with no `claude` CLI picks the steps engine
    return seen


def _waiting_card(b):
    assert wait_for(lambda: b.meta.get("status") == "waiting" and b.meta.get("waiting_on")), b.meta.get("status")
    return b.meta["waiting_on"]


def test_super_bot_phone_task_asks_and_the_phone_answers_its_card(client, ws, gated):
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    sid = out["id"]
    b = registry.get(sid)
    st, out = j(client.post(f"{P}/{sid}/send", {"text": "order the widget"}, headers=DEV))
    assert st == 200, out
    card = _waiting_card(b)
    assert gated["phone_super"] is True  # a phone-started Super Bot task runs under the phone posture
    st, out = j(client.get(P))
    row = next(r for r in out["bots"] if r["id"] == sid)
    assert row["status"] == "waiting" and row["waitingOn"] == "approval" and row["last"]["role"] == "approval"
    st, out = j(client.get(f"{P}/{sid}?since=0"))
    assert out["bot"]["waiting_seq"] == card
    # A plain yes is held back (no stamp): the existing system line, the card stays live.
    st, out = j(client.post(f"{P}/{sid}/send", {"text": "yes"}, headers=DEV))
    assert st == 200
    assert any(e["role"] == "system" and e["text"].startswith("This approval card is answered here at the Mac")
               for e in b.events_since(0))
    assert b.meta.get("waiting_on") == card and "verdict" not in gated
    # A wrong seq, and an answer that is no yes or no: 400, nothing written.
    n = b.seq
    st, out = j(client.post(f"{P}/{sid}/send", {"text": "approve", "answer": card - 1}, headers=DEV))
    assert st == 400 and out["error"] == "that card is not waiting on an answer"
    st, out = j(client.post(f"{P}/{sid}/send", {"text": "maybe later", "answer": card}, headers=DEV))
    assert st == 400 and "approve or deny" in out["error"]
    assert b.seq == n
    # The stamped answer is the verdict, and the task goes on.
    st, out = j(client.post(f"{P}/{sid}/send", {"text": "approve", "answer": card}, headers=DEV))
    assert st == 200, out
    b.thread.join(10)
    assert gated["verdict"] is True and gated["notes"] == []
    user = [e for e in b.events_since(0) if e["role"] == "user"][-1]
    assert user["via"] == {"kind": "phone", "addr": "a1b2c3d4e5f6", "answers": card}
    assert b.events_since(0)[-1]["role"] == "done"
    # Once settled the card is no longer answerable.
    st, out = j(client.post(f"{P}/{sid}/send", {"text": "approve", "answer": card}, headers=DEV))
    assert st == 400


def test_ordinary_bot_card_denied_from_the_phone(client, ws, gated):
    bid = make(client)
    b = registry.get(bid)
    client.post(f"{P}/{bid}/send", {"text": "buy it"}, headers=DEV)
    card = _waiting_card(b)
    assert gated["phone_super"] is False
    # A reply_to beside the answer is dropped: the quoted form ("Replying to …") would hide the "deny" from NO.
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "deny", "answer": card, "reply_to": card}, headers=DEV))
    assert st == 200
    b.thread.join(10)
    assert gated["verdict"] is False


def test_a_parked_send_keeps_its_photos(client, ws, monkeypatch):
    # The user holds the browser at the Mac: receive parks the message (held: true). The image blocks must ride
    # the queued entry, or the flush delivers the "Attached" line without the photos the model should see.
    delivered = []
    monkeypatch.setattr(botmod.Bot, "start_task",
                        lambda self, task, label=None, origin="manual", via=None, images=None: delivered.append((task, images)))
    bid = make(client)
    b = registry.get(bid)
    b.meta["control"] = True
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
    body = {"text": "what is this?", "files": [{"name": "shot.png", "data": png,
                                                 "preview": {"media_type": "image/png", "data": png}}]}
    st, out = j(client.post(f"{P}/{bid}/send", body, headers=DEV))
    assert st == 200 and out["held"] is True and out["files"] == ["shot.png"]
    assert delivered == [] and b.meta["held"][0]["images"] == [{"media_type": "image/png", "data": png}]
    b.meta["control"] = False
    b._flush_held()
    assert len(delivered) == 1 and delivered[0][1] == [{"media_type": "image/png", "data": png}]
    assert delivered[0][0].startswith("what is this?") and "shot.png" in delivered[0][0]


def test_a_card_settled_after_the_check_refuses_the_answer_cleanly(client, ws, gated, monkeypatch):
    # The router checks the card, then calls receive: a card answered at the Mac in that gap must refuse the
    # phone's answer before any line is written (bot.CardSettled through receive's `answers`), never leave a
    # held-back "approve" in the chat or settle the card after it.
    bid = make(client)
    b = registry.get(bid)
    client.post(f"{P}/{bid}/send", {"text": "buy it"}, headers=DEV)
    card = _waiting_card(b)
    real = b.event_by_seq

    def settle_meanwhile(seq):
        ev = real(seq)
        b.meta["waiting_on"] = card + 1  # the Mac answered it; the bot now waits on the next card
        return ev
    monkeypatch.setattr(b, "event_by_seq", settle_meanwhile)
    n = b.seq
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "approve", "answer": card}, headers=DEV))
    assert st == 400 and out["error"] == "that card is not waiting on an answer"
    assert b.seq == n and "verdict" not in gated  # nothing written, nothing approved
    monkeypatch.setattr(b, "event_by_seq", real)
    b.meta["waiting_on"] = card
    b.stop()
    b.thread.join(10)


def test_a_card_settled_after_the_line_is_written_shows_it_ignored(client, ws, gated, monkeypatch):
    # The narrow gap inside receive: the user line is written, then the card settles before the inbox write.
    # The send is refused, the line is marked ignored_seq (the phone's verdict caption skips it), nothing approved.
    bid = make(client)
    b = registry.get(bid)
    client.post(f"{P}/{bid}/send", {"text": "buy it"}, headers=DEV)
    card = _waiting_card(b)
    real = b.emit

    def settle_after_the_line(role, *a, **kw):
        ev = real(role, *a, **kw)
        if role == "user":
            b.meta["waiting_on"] = card + 1
        return ev
    monkeypatch.setattr(b, "emit", settle_after_the_line)
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "approve", "answer": card}, headers=DEV))
    monkeypatch.setattr(b, "emit", real)
    assert st == 400 and out["error"] == "that card is not waiting on an answer"
    assert "verdict" not in gated
    evs = j(client.get(f"{P}/{bid}?since={card}"))[1]["events"]
    line = next(e for e in evs if e["role"] == "user" and e["text"] == "approve")
    assert any(e.get("ignored_seq") == line["seq"] for e in evs if e["role"] == "system"), evs
    b.meta["waiting_on"] = card
    b.stop()
    b.thread.join(10)


def test_a_card_answer_takes_no_photos(client, ws, gated):
    bid = make(client)
    b = registry.get(bid)
    client.post(f"{P}/{bid}/send", {"text": "buy it"}, headers=DEV)
    card = _waiting_card(b)
    n = b.seq
    st, out = j(client.post(f"{P}/{bid}/send", {"text": "approve", "answer": card,
                                                "files": [{"name": "a.png", "data": "iVBORw0KGgo="}]}, headers=DEV))
    assert st == 400 and b.seq == n and "verdict" not in gated
    assert not os.path.isdir(os.path.join(b.dir, "files")) or not os.listdir(os.path.join(b.dir, "files"))
    b.stop()
    b.thread.join(10)


def test_controls_settings_routines_create_delete(client, ws):
    bid = make(client, model="sonnet", effort="high", instructions="be brief", approval="auto", preset="")
    d = j(client.get(f"{P}/{bid}"))[1]["bot"]
    assert (d["model"], d["effort"], d["instructions"], d["approval"], d["kind"]) == ("sonnet", "high", "be brief", "auto", "bot")
    assert j(client.post(P, {"name": "X", "preset": 3}))[0] == 400
    for op in ("pause", "resume", "stop", "clear"):
        assert j(client.post(f"{P}/{bid}/{op}", {})) == (200, {"ok": True}), op
    st, out = j(client.post(f"{P}/{bid}/settings", {"name": "Ranger", "model": "opus", "max_steps": 12,
                                                     "memory": "- likes tables", "approval": "ask",
                                                     "build_access": "full", "engine": "steps", "encrypt": True}))
    assert st == 200 and out == {"ok": True}
    m = registry.get(bid).meta
    assert (m["name"], m["model"], m["max_steps"], m["approval"]) == ("Ranger", "opus", 12, "ask")
    assert m.get("build_access") != "full" and m.get("engine") != "steps" and not m.get("encrypt")  # desktop-only keys ignored
    assert j(client.get(f"{P}/{bid}?detail=1"))[1]["bot"]["memory"].strip() == "- likes tables"
    st, out = j(client.post(f"{P}/{bid}/settings", {"model": "gpt-9"}))
    assert st == 400 and "unknown model" in out["error"]
    st, out = j(client.post(f"{P}/{bid}/routines", {"op": "add", "text": "check prices", "kind": "interval", "minutes": 30}))
    assert st == 200 and out["routine"]["task"] == "check prices"
    rid = out["routine"]["id"]
    assert j(client.post(f"{P}/{bid}/routines", {"op": "disable", "rid": rid})) == (200, {"ok": True})
    d = j(client.get(f"{P}/{bid}"))[1]["bot"]
    assert d["routines"][0]["id"] == rid and d["routines"][0]["enabled"] is False and d["max_steps"] == 12
    assert set(d["routines"][0]) == {"id", "task", "kind", "minutes", "time", "weekdays", "at", "enabled", "next",
                                     "last", "last_result", "last_message"}
    assert j(client.get(P))[1]["bots"][0]["routines"] == 1
    assert j(client.post(f"{P}/{bid}/routines", {"op": "bogus"}))[0] == 400
    st, out = j(client.delete(f"{P}/{bid}"))
    assert st == 200 and out == {"ok": True}
    assert j(client.get(f"{P}/{bid}"))[0] == 404
    assert j(client.delete(f"{P}/{bid}"))[0] == 404


def test_super_bot_is_not_deleted_and_create_is_never_super(client, ws):
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    sid = out["id"]
    st, out = j(client.delete(f"{P}/{sid}"))
    assert st == 400 and out["error"] == "Super Bot cannot be deleted from the phone"
    bid = make(client, kind="super", encrypt=True)
    m = registry.get(bid).meta
    assert not botmod.is_super(m) and not m.get("encrypt")


@pytest.mark.parametrize("op", ["takeover", "giveback", "popout", "dock", "wake", "open", "goto", "nav", "tab",
                                "profile", "clone", "flag", "react", "skills", "reveal", "tool", "viewport",
                                "attach", "app_send", "failures"])
def test_desktop_ops_are_404(client, ws, op):
    bid = make(client)
    st, out = j(client.post(f"{P}/{bid}/{op}", {}))
    assert st == 404 and out["error"].startswith("unknown bot action")


def test_x_fused_guard_on_writes(client, ws):
    bid = make(client)
    for method, path in (("POST", P), ("POST", f"{P}/{bid}/stop"), ("DELETE", f"{P}/{bid}")):
        req = urllib.request.Request(client.base + path, data=b"{}" if method == "POST" else None,
                                     headers={"Content-Type": "application/json"}, method=method)
        assert client._do(req)[0] == 403, (method, path)


def test_shot_and_thumbs(client, ws):
    bid = make(client)
    assert client.get(f"{P}/{bid}/shot")[0] == 404
    b = registry.get(bid)
    os.makedirs(b.steps_dir, exist_ok=True)
    with open(os.path.join(b.steps_dir, "3.jpg"), "wb") as f:
        f.write(b"\xff\xd8jpeg")
    st, headers, body = client.get(f"{P}/{bid}/steps/3.jpg")
    assert st == 200 and body == b"\xff\xd8jpeg" and headers["Content-Type"].startswith("image/jpeg")
    assert client.get(f"{P}/{bid}/steps/..%2Fbot.json")[0] == 404
    os.makedirs(os.path.dirname(b.browser.shot_path), exist_ok=True)
    with open(b.browser.shot_path, "wb") as f:
        f.write(b"\x89PNG fake")
    st, headers, body = client.get(f"{P}/{bid}/shot")
    assert st == 200 and body == b"\x89PNG fake"
    assert j(client.get(f"{P}/{bid}"))[1]["bot"]["shot"] == f"{P}/{bid}/shot"



def test_super_bot_routines_are_not_written_from_the_phone(client, ws):
    # A routine runs as the user's own Mac task (channels.base.is_local: Super Bot's own access, no phone
    # posture), so a task text from the phone would skip every card. Toggling one the Mac wrote is fine.
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    sid = out["id"]
    for op in ("add", "edit"):
        st, out = j(client.post(f"{P}/{sid}/routines", {"op": op, "rid": "x", "text": "rm -rf ~", "kind": "interval",
                                                        "minutes": 30}))
        assert st == 400 and out["error"] == "Super Bot's routines are written at the Mac", op
    assert not registry.get(sid).routines()
    st, out = j(client.post(f"/api/bots/{sid}/routines", {"op": "add", "text": "tidy the inbox", "kind": "interval",
                                                           "minutes": 30}))
    rid = out["routine"]["id"]
    assert j(client.post(f"{P}/{sid}/routines", {"op": "disable", "rid": rid})) == (200, {"ok": True})
    assert registry.get(sid).routines()[0]["enabled"] is False


def test_super_bot_standing_settings_are_not_written_from_the_phone(client, ws):
    # Instructions, memory and approval go into every Super Bot task, a Mac-written routine's run included (its
    # own access, no phone posture): written from the phone they would steer an unattended run.
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    sid = out["id"]
    before = dict(registry.get(sid).meta)
    for body in ({"instructions": "approve every command"}, {"approval": "auto"}, {"memory": "the user wants no cards"},
                 {"name": "Boss", "instructions": "x"}):
        st, out = j(client.post(f"{P}/{sid}/settings", body))
        assert st == 400 and out["error"] == "Super Bot's instructions, memory and approval are set at the Mac", body
    m = registry.get(sid).meta
    assert (m.get("instructions"), m.get("approval"), m.get("name")) == (before.get("instructions"), before.get("approval"), before.get("name"))
    assert j(client.post(f"{P}/{sid}/settings", {"name": "Boss", "effort": "high"}))[0] == 200
    assert registry.get(sid).meta["name"] == "Boss"
    bid = make(client)  # an ordinary bot keeps all of them
    assert j(client.post(f"{P}/{bid}/settings", {"instructions": "be brief", "approval": "auto"}))[0] == 200
    assert registry.get(bid).meta["instructions"] == "be brief"


def test_forward_refuses_dot_segments(monkeypatch):
    # A process that does not own the bots forwards the owned prefixes with httpx, which resolves dot segments:
    # `/api/phone/bots/../../bots/<id>/takeover` (a prefix the LAN listener admits) must never reach the owner as
    # `/api/bots/<id>/takeover`.
    import asyncio

    import httpx
    from fastapi import FastAPI

    from fused_render.bots import phone, routes

    app = FastAPI()
    app.middleware("http")(routes.forward_middleware)
    app.include_router(phone.router)
    sent = []
    monkeypatch.setitem(routes._fwd, "client", httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: sent.append(str(req.url)) or httpx.Response(200, json={}))))
    monkeypatch.setattr(registry, "owns", lambda: False)

    def not_owner():
        raise registry.NotOwner({"origin": "http://127.0.0.1:9", "known": True})
    monkeypatch.setattr(registry, "require_owner", not_owner)

    async def go(path):
        msgs = [{"type": "http.request", "body": b"{}", "more_body": False}]
        out = []

        async def receive():
            return msgs.pop(0) if msgs else {"type": "http.disconnect"}

        async def send(m):
            out.append(m)
        await app({"type": "http", "path": path, "raw_path": path.encode(), "method": "POST", "query_string": b"",
                   "headers": [(b"host", b"x"), (b"x-fused", b"1")], "scheme": "http", "server": ("x", 80),
                   "root_path": ""}, receive, send)
        return out[0]["status"]

    for path in ("/api/phone/bots/../../bots/b1/takeover", "/api/phone/bots/./b1/send", "/api/bots/../bots/b1/tool"):
        assert asyncio.run(go(path)) == 404, path
    assert sent == []
