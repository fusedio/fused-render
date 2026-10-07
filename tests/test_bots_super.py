"""Super Bot (bot.py KINDS "super", docs/bots.md §5 "Super Bot") through the real
server: one per install, a Claude model, `super_access`, and the trigger lockdown
(only the user's chat starts it: routines, iMessage and the file inbox are
refused). The harness side (argv, permission cards, action rows) is in
test_bots_agent_engine.py."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import threading
import time

import pytest

from fused_render.bots import agent_engine, apptools, handoffs, steps_engine
from fused_render.bots import bot as botmod
from fused_render.bots import paths as bpaths
from fused_render.bots import registry
from fused_render.bots.channels import base as chan


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


def bots(client):
    st, out = j(client.get("/api/bots"))
    assert st == 200, out
    return {b["id"]: b for b in out["bots"]}


def test_one_ea_per_install_with_its_own_defaults(client, ws):
    st, out = j(client.post("/api/bots", {"name": "", "kind": "super"}))
    assert st == 200 and out["ok"], out
    sb = bots(client)[out["id"]]
    assert sb["kind"] == "super" and sb["super_access"] == "ask" and sb["name"] == botmod.SUPER_NAME
    assert sb["model"] == botmod.DEFAULT_MODEL and sb["face"] == botmod.SUPER_FACE
    assert sb["instructions"] == botmod.SUPER_INSTRUCTIONS
    assert sb["pinned"] is True  # Super Bot starts pinned in the sidebar and dock
    st, out2 = j(client.post("/api/bots", {"name": "Second", "kind": "super"}))
    assert st == 400 and "already a Super Bot" in out2["error"]
    assert botmod.super_id() == out["id"]
    # an ordinary bot is still fine, and has no kind
    st, out3 = j(client.post("/api/bots", {"name": "Scout"}))
    assert st == 200 and "kind" not in bots(client)[out3["id"]]
    assert not bots(client)[out3["id"]].get("pinned")


def test_super_create_options(client, ws):
    st, out = j(client.post("/api/bots", {"name": "Desk", "kind": "super", "model": "local-4b", "super_access": "full",
                                          "engine": "steps", "instructions": "be terse", "preset": "linkedin"}))
    assert st == 200, out
    sb = bots(client)[out["id"]]
    assert sb["name"] == "Desk" and sb["model"] == botmod.DEFAULT_MODEL  # a local model cannot be Super Bot
    assert sb["super_access"] == "full" and "engine" not in sb and "preset" not in sb  # presets and engines do not apply
    assert sb["instructions"] == "be terse"
    st, out = j(client.post("/api/bots", {"name": "x", "kind": "robot"}))
    assert st == 400 and "kind must be" in out["error"]


def test_super_settings(client, ws):
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    bid = out["id"]
    st, out = j(client.post(f"/api/bots/{bid}/settings", {"super_access": "full", "engine": "steps", "model": "opus"}))
    assert st == 200
    sb = bots(client)[bid]
    assert sb["super_access"] == "full" and sb["model"] == "opus" and "engine" not in sb
    st, out = j(client.post(f"/api/bots/{bid}/settings", {"model": "local-9b"}))
    assert st == 400 and "Claude model" in out["error"]
    st, out = j(client.post(f"/api/bots/{bid}/settings", {"super_access": "sometimes"}))
    assert st == 200 and bots(client)[bid]["super_access"] == "full"
    # super_access is Super Bot's alone
    st, out = j(client.post("/api/bots", {"name": "Scout"}))
    st, _ = j(client.post(f"/api/bots/{out['id']}/settings", {"super_access": "full"}))
    assert st == 200 and "super_access" not in bots(client)[out["id"]]


def test_super_refuses_routines_and_cloning(client, ws):
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    bid = out["id"]
    st, out = j(client.post(f"/api/bots/{bid}/routines", {"op": "add", "text": "tidy up", "kind": "interval", "minutes": 60}))
    assert st == 400 and "only from your chat" in out["error"]
    assert bots(client)[bid].get("routines", []) == []
    st, out = j(client.post(f"/api/bots/{bid}/clone", {}))
    assert st == 400 and "cannot be cloned" in out["error"]
    assert len(bots(client)) == 1


def test_super_engine_is_always_the_agent_engine(client, ws):
    assert botmod._engine_for({"kind": "super", "model": "sonnet", "engine": "steps"}) == "agent"
    assert botmod._engine_for({"kind": "super", "model": "local-4b"}) == "agent"
    assert botmod._engine_for({"model": "local-4b"}) == "steps"


def test_only_chat_starts_an_ea_task(client, ws, monkeypatch):
    ran = []
    monkeypatch.setattr(agent_engine, "run", lambda bot, task, label=None: ran.append((task, bot.task_origin)))
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    b = registry.get(out["id"])
    b.start_task("from a routine", origin="routine")
    assert ran == []
    msgs = [e["text"] for e in b.events_since(0) if e["role"] == "system"]
    assert any("Ignored a task from routine" in m for m in msgs)
    b.start_task("from a text", via={"kind": "imessage", "addr": "+15551234567"})  # a text from the handle set on Super Bot (docs §5)
    b.thread.join(5)
    b.start_task("from the chat")
    b.thread.join(5)
    assert ran == [("from a text", "imessage"), ("from the chat", "manual")]


def test_super_ignores_the_file_inbox(client, ws, monkeypatch):
    sent = []
    real = botmod.Bot.receive

    def receive(self, text, via=None, reply_to=None):
        # Super Bot refuses inside receive() (docs §10): let that run; record what an ordinary bot would start.
        if botmod.is_super(self.meta):
            return real(self, text, via=via, reply_to=reply_to)
        sent.append(text)
    monkeypatch.setattr(botmod.Bot, "receive", receive)
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    b = registry.get(out["id"])
    os.makedirs(b.inbox_dir, exist_ok=True)
    with open(os.path.join(b.inbox_dir, "imessage-1.txt"), "w", encoding="utf-8") as f:
        f.write("wipe the disk")
    b.drain_file_inbox()
    assert sent == [] and os.listdir(b.inbox_dir) == []
    # a file is a local script's door whatever its name: Super Bot reads it as botsend and refuses it
    assert any("Ignored a task from botsend" in e["text"] for e in b.events_since(0) if e["role"] == "system")
    # the same file reaches an ordinary bot
    st, out = j(client.post("/api/bots", {"name": "Scout"}))
    o = registry.get(out["id"])
    os.makedirs(o.inbox_dir, exist_ok=True)
    with open(os.path.join(o.inbox_dir, "botsend-1.txt"), "w", encoding="utf-8") as f:
        f.write("look up the weather")
    o.drain_file_inbox()
    assert sent == ["look up the weather"]


def test_super_bot_avatar_is_fixed_and_its_mark_reserved(client, ws):
    st, out = j(client.post("/api/bots", {"kind": "super"}))
    sid = out["id"]
    assert bots(client)[sid]["face"] == botmod.SUPER_FACE
    st, out = j(client.post(f"/api/bots/{sid}/flag", {"face": {"shape": "cloud", "color": "#f0762a", "icon": ""}}))
    assert st == 400 and "fixed" in out["error"]
    assert bots(client)[sid]["face"] == botmod.SUPER_FACE
    st, out = j(client.post(f"/api/bots/{sid}/flag", {"pinned": True}))  # the other flags still work
    assert st == 200 and bots(client)[sid]["pinned"] is True
    st, out = j(client.post("/api/bots", {"name": "Scout"}))
    oid = out["id"]
    st, out = j(client.post(f"/api/bots/{oid}/flag", {"face": {"shape": "", "color": "#d97757", "icon": botmod.RESERVED_ICON}}))
    assert st == 400 and "Super Bot" in out["error"]
    st, out = j(client.post(f"/api/bots/{oid}/flag", {"face": {"shape": "", "color": "#ff0000", "icon": "youtube"}}))
    assert st == 200 and bots(client)[oid]["face"]["icon"] == "youtube"


# ------------------------------------------------------------- hand-offs ---
# docs/bots.md §11: a row's state is driven by the target's own events
# (handoffs.on_event, reached through bot.emit -> registry.on_event) and the
# scheduler's pass (handoffs.sweep, called by hand here: tests run no scheduler).
@pytest.fixture
def pair(ws, monkeypatch):
    """(Super Bot, an ordinary bot "Scout", gate): the target's task runs a fake engine
    that writes `Task started` and then blocks on `gate` until the test releases it."""
    gate = threading.Event()

    def run(bot, task, label=None):
        bot.emit("system", f"Task started: {label or task}")
        gate.wait(10)
    monkeypatch.setattr(steps_engine, "run", run)
    monkeypatch.setattr(agent_engine, "run", run)
    sb = registry.create(kind="super")
    t = registry.create(name="Scout")
    yield sb, t, gate
    gate.set()
    if t.thread is not None:
        t.thread.join(5)


def rows(sb):
    return {h["id"]: h for h in sb.meta.get("handoffs") or []}


def cards(b, role):
    return [e for e in b.events_since(0) if e["role"] == role and e.get("source") == "handoff"]


def release(t, gate):
    gate.set()
    t.thread.join(5)
    gate.clear()


def test_handoff_runs_blocks_and_reports_one_result(pair):
    sb, t, gate = pair
    label, out = sb.handoff("scout", "find the cheapest flight to Lisbon")
    assert out.startswith("started"), out
    (hd,) = rows(sb).values()
    assert hd["state"] == "working" and hd["started_at"] and "start_seq" not in hd and "asked" not in hd
    hv = chan.handoff_via(sb.id, hd["id"])
    # the target waits on the user: blocked, and ONE "needs you at the laptop" card on Super Bot
    t.emit("approval", "About to submit the form. Approve?", via=hv)
    assert hd["state"] == "blocked" and hd["blocked"]["kind"] == "approval"
    t.emit("question", "Which airport?", via=hv)
    assert hd["blocked"] == {"kind": "question", "text": "Which airport?"}
    (ask,) = cards(sb, "question")
    assert ask["text"] == "Scout needs you at the laptop: About to submit the form. Answer it at the Mac."
    assert ask["handoff"]["state"] == "blocked" and "via" not in ask  # web-started: never texted
    # the wait ends when the bot acts again; a `note` progress line lands on the row
    t.emit("action", "goto kayak.com", via=hv)
    assert hd["state"] == "working" and "blocked" not in hd
    handoffs.on_event(t, {"role": "note", "text": "comparing three fares", "progress": True, "via": hv})
    assert hd["notes"] == ["comparing three fares"]
    t.emit("note", "Already ran py; using the result above.", via=hv)  # a harness note is not a progress line
    assert hd["notes"] == ["comparing three fares"]
    sec = handoffs.handoffs_section(sb)
    assert "working" in sec and "comparing three fares" in sec and "HAND-OFF RESULTS" not in sec
    t.emit("done", "TAP, 212 EUR on 3 May.", via=hv)
    assert hd["state"] == "done" and hd["result"] == "TAP, 212 EUR on 3 May." and hd["done_at"]
    (res,) = cards(sb, "done")
    assert res["text"] == "TAP, 212 EUR on 3 May." and res["handoff"]["state"] == "done"
    assert any(e["text"].startswith("Sent to Super Bot: TAP") for e in t.events_since(0) if e["role"] == "system")
    assert sb.meta["conversation"]["web_touched"] is True
    sec = handoffs.handoffs_section(sb)
    assert "done" in sec and "HAND-OFF RESULTS" in sec and "TAP, 212 EUR" in sec
    # terminal is final: a late event changes nothing
    t.emit("error", "boom", trace="x", via=hv)
    assert hd["state"] == "done" and len(cards(sb, "done")) == 1
    # a result stays on the board until the engine marks it seen (the preamble carrying it was written);
    # a turn ending meanwhile does not hide it (Bugbot: results landing mid-turn vanished)
    sb.meta["conversation"]["last_turn_ts"] = time.time() + 1
    assert "TAP, 212 EUR" in handoffs.handoffs_section(sb)
    handoffs.mark_seen(sb)
    assert hd["seen"] is True and handoffs.handoffs_section(sb) == ""
    assert handoffs.handoffs_section(t) == ""


def test_handoff_retry_error_is_not_terminal_but_a_fatal_one_is(pair):
    sb, t, gate = pair
    sb.handoff("Scout", "read the news")
    (hd,) = rows(sb).values()
    hv = chan.handoff_via(sb.id, hd["id"])
    t.emit("error", "Model returned no usable JSON; retrying", via=hv)
    assert hd["state"] == "working"
    t.emit("error", "RuntimeError: model kept returning invalid JSON", trace="tb", via=hv)
    assert hd["state"] == "failed" and cards(sb, "done")[0]["handoff"]["state"] == "failed"


def test_handoff_step_cap_is_failed(pair):
    sb, t, gate = pair
    sb.handoff("Scout", "read the news")
    (hd,) = rows(sb).values()
    t.emit("done", "Stopped after 60 steps without finishing.", via=chan.handoff_via(sb.id, hd["id"]))
    assert hd["state"] == "failed"


def test_handoff_queue_sweep_and_stop(pair):
    sb, t, gate = pair
    assert t.start_task("the user's own task")  # busy: the hand-off queues behind it
    label, out = sb.handoff("Scout", "check the weather")
    assert out.startswith("queued"), out
    (hd,) = rows(sb).values()
    assert hd["state"] == "received" and t._handoff_queue == [(sb.id, hd["id"])]
    handoffs.sweep(registry)
    assert hd["state"] == "received"  # still busy
    release(t, gate)
    handoffs.sweep(registry)
    assert hd["state"] == "working" and t._handoff_queue == []
    # a running hand-off is stopped through the target: its `Stopped` closes the row cancelled
    _, out = sb.handoff_stop("Scout")
    assert out.startswith("stopped"), out
    t.emit("system", "Stopped", via=chan.handoff_via(sb.id, hd["id"]))
    assert hd["state"] == "cancelled"
    release(t, gate)
    # a queued one is dropped and closed cancelled at once
    assert t.start_task("another task of the user's")
    sb.handoff("Scout", "and the tides")
    hd2 = [h for h in rows(sb).values() if h["id"] != hd["id"]][0]
    sb.handoff_stop("Scout")
    assert hd2["state"] == "cancelled" and hd2["result"].startswith("Cancelled before Scout") and t._handoff_queue == []


def test_handoff_sweep_closes_dead_timed_out_and_orphaned_rows(pair):
    sb, t, gate = pair
    sb.handoff("Scout", "one")
    (hd,) = rows(sb).values()
    release(t, gate)  # the task ends without done / error / Stopped
    handoffs.sweep(registry)
    assert hd["state"] == "failed" and "ended without a result" in hd["result"]
    sb.handoff("Scout", "two")
    hd2 = [h for h in rows(sb).values() if h["id"] != hd["id"]][0]
    hd2["created_at"] = time.time() - botmod.HANDOFF_MAX_S - 1
    handoffs.sweep(registry)
    assert hd2["state"] == "failed" and "No result from Scout" in hd2["result"]
    release(t, gate)
    sb.handoff("Scout", "three")
    hd3 = [h for h in rows(sb).values() if h["id"] not in (hd["id"], hd2["id"])][0]
    gate.set()
    registry.delete(t.id)  # deletion sweeps at once
    assert hd3["state"] == "failed" and "was deleted" in hd3["result"]


# ---- a finished hand-off carries the user's request on (docs §11 rule 5) ----

def _carry_on_turns(sb):
    return [e for e in sb.events_since(0) if e["role"] == "system" and e["text"].startswith("Task started: Carrying on:")]


def test_handoff_done_carries_the_request_on(pair):
    sb, t, gate = pair
    sb.meta["task"] = "summarize my X timeline and email it to me"
    label, out = sb.handoff("scout", "read the timeline")
    assert out.startswith("started")
    (hd,) = rows(sb).values()
    assert hd["asked_for"] == "summarize my X timeline and email it to me" and hd["asked_seq"] >= 0
    t.emit("done", "Five posts about Claude.", via=chan.handoff_via(sb.id, hd["id"]))
    release(t, gate)
    assert hd["state"] == "done" and hd["continued"] is True
    # Super Bot started a turn of its own: the note, the task label, the engine's first line
    assert any(e["role"] == "note" and e["text"] == "Scout is done; carrying on with your request." for e in sb.events_since(0))
    assert sb.meta["task"] == "Carrying on: summarize my X timeline and email it to me"
    assert sb.task_origin == botmod.HANDOFF_CONTINUE_ORIGIN
    if sb.thread is not None:
        gate.set(); sb.thread.join(5)
    assert len(_carry_on_turns(sb)) == 1
    # the result stays on the board for that turn (the engine marks it seen once the preamble is written)
    assert "Five posts about Claude." in handoffs.handoffs_section(sb)


def test_handoff_from_the_phone_carries_on_over_the_phone(pair):
    sb, t, gate = pair
    sb.meta["task"] = "text me my X digest"
    sb.task_via = {"kind": "imessage", "addr": "+15550100"}  # the asking task came in by text
    sb.handoff("scout", "read X")
    (hd,) = rows(sb).values()
    assert hd["origin_via"] == {"kind": "imessage", "addr": "+15550100"}
    t.emit("done", "Three posts.", via=chan.handoff_via(sb.id, hd["id"]))
    release(t, gate)
    assert hd["continued"] is True
    # the carry-on runs on the phone's channel (replies go back there) under its own ledger origin
    assert sb.task_via == {"kind": "imessage", "addr": "+15550100"} and sb.task_origin == botmod.HANDOFF_CONTINUE_ORIGIN
    if sb.thread is not None:
        gate.set(); sb.thread.join(5)
    assert len(_carry_on_turns(sb)) == 1


def test_handoff_does_not_carry_on_when_the_user_spoke_or_it_failed(pair):
    sb, t, gate = pair
    sb.meta["task"] = "find flights"
    sb.handoff("scout", "find the cheapest flight")
    (hd,) = rows(sb).values()
    sb.emit("user", "actually, never mind")  # the user's own message is the way on from here
    t.emit("done", "TAP, 212 EUR.", via=chan.handoff_via(sb.id, hd["id"]))
    release(t, gate)
    assert hd["state"] == "done" and hd["continued"] is True and not _carry_on_turns(sb)
    assert not any(e["role"] == "note" and "carrying on" in e["text"] for e in sb.events_since(0))
    # a failed hand-off only lands its card
    sb.meta["task"] = "find hotels"
    sb.handoff("scout", "find a hotel")
    hd2 = [h for h in rows(sb).values() if h["task"] == "find a hotel"][0]
    t.emit("error", "boom", trace="x", via=chan.handoff_via(sb.id, hd2["id"]))
    release(t, gate)
    assert hd2["state"] == "failed" and "continued" not in hd2 and not _carry_on_turns(sb)


def test_handoff_siblings_carry_on_once_when_the_last_lands(pair, monkeypatch):
    sb, t, gate = pair
    t2 = registry.create(name="Mailer")
    sb.meta["task"] = "digest X and Gmail"
    sb.task_started = time.time()  # both hand-offs come from this one turn
    sb.handoff("scout", "read X")
    sb.handoff("mailer", "read Gmail")
    a, b = rows(sb).values()
    t.emit("done", "X: five posts.", via=chan.handoff_via(sb.id, a["id"]))
    release(t, gate)
    assert a["state"] == "done" and "continued" not in a and not _carry_on_turns(sb)  # a sibling is still open
    t2.emit("done", "Gmail: two mails.", via=chan.handoff_via(sb.id, b["id"]))
    if t2.thread is not None:
        gate.set(); t2.thread.join(5); gate.clear()
    assert b["continued"] is True
    if sb.thread is not None:
        gate.set(); sb.thread.join(5)
    assert len(_carry_on_turns(sb)) == 1
