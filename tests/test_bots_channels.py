"""fused_render.bots.channels: the router's policy and the iMessage channel's
poll, with a fake channel and a fake chat.db (docs/bots.md §10)."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import sqlite3
import subprocess

import pytest

pytest.importorskip("fcntl")  # the iMessage channel's lock is POSIX-only; the Windows runner skips this file

from fused_render.bots import channels, imessage, registry, store  # noqa: E402
from fused_render.bots.channels import base, router as rmod
from fused_render.bots.channels.base import Caps, Channel, Inbound
from fused_render.bots.channels.imessage import CAPS, ImessageChannel


class FakeChannel(Channel):
    kind = "fake"
    caps = Caps(options=False, buttons=False, max_len=120, media=False, can_login=False)

    def __init__(self, door=("b1", "+1"), mode="own"):
        self._door, self._mode, self.sent, self.inbox = door, mode, [], []

    def door(self):
        return self._door

    def poll(self):
        out, self.inbox = self.inbox, []
        return out

    def send(self, addr, text, event=None):
        self.sent.append((addr, text))

    def identity(self):
        return {"mode": self._mode, "label": "me"}


class FakeBot:
    def __init__(self, bid, name, meta=None):
        self.id, self.meta, self.received, self.emitted = bid, {"name": name, **(meta or {})}, [], []
        self.task_started = 0.0

    def receive(self, text, via=None, reply_to=None):
        self.received.append((text, via))

    def emit(self, role, text, **extra):
        ev = {"seq": len(self.emitted) + 100, "role": role, "text": text, **extra}
        self.emitted.append(ev)
        return ev


class FakeRegistry:
    def __init__(self, *bots):
        self.bots = {b.id: b for b in bots}

    def get(self, bid):
        return self.bots[bid]


# ---- pure helpers -------------------------------------------------------------------
def test_render_numbers_options_and_shortens():
    assert rmod.render({"role": "question", "text": "Size?", "options": ["Studio", "1br", "2br"]}, CAPS) == \
        ("Size?\n\nReply 1 Studio · 2 1br · 3 2br", ["Studio", "1br", "2br"])
    assert rmod.render({"role": "question", "text": "Size?", "options": ["A", "B"]}, base.WEB_CAPS) == ("Size?", [])
    long = "First sentence. " * 60
    text, _ = rmod.render({"role": "done", "text": long}, CAPS)
    assert len(text) < 700 and text.endswith("… Full answer in the app.")


def test_map_answer():
    assert rmod.map_answer("2", ["a", "b", "c"]) == "b"
    assert rmod.map_answer("B.", ["a", "b", "c"]) == "b"
    assert rmod.map_answer("9", ["a", "b"]) == "9"
    assert rmod.map_answer("yes", ["a", "b"]) == "yes"
    assert rmod.map_answer("2", []) == "2"






def test_prompt_section_and_login_text():
    class B:
        task_via = {"kind": "imessage", "addr": "+1"}
        task_origin = "imessage"
    assert channels.prompt_for(B()).startswith("CHANNEL: the user sent this task from iMessage")
    assert "on the Mac" in channels.login_text(B(), "Needs sign-in.")
    assert channels.login_text(B(), "Needs sign-in.").endswith("then click Done. I'll wait.")
    assert channels.origin_label(B()).startswith("iMessage")

    class W:
        task_via = dict(base.WEB)
        task_origin = "manual"
    assert channels.prompt_for(W()) == "" and channels.origin_label(W()) == "chat"
    assert channels.login_text(W(), "Needs sign-in.") == "Needs sign-in. I've paused and opened my browser for you. Sign in there, then click Done."


# ---- router policy ------------------------------------------------------------------
def test_targets_reply_to_origin_only():
    ch = FakeChannel()
    b1 = FakeBot("b1", "Scout")
    r = rmod.Router(FakeRegistry(b1))
    r.add(ch)
    # a texted task answers the sender
    assert r.targets(ch, b1, {"role": "done", "text": "ok", "via": {"kind": "fake", "addr": "+1"}}) == ["+1"]
    # nothing else reaches a channel: no forwards for web tasks, routines or builds
    assert r.targets(ch, b1, {"role": "done", "text": "ok"}) == []
    assert r.targets(ch, b1, {"role": "done", "text": "ok", "via": {"kind": "routine", "addr": ""}}) == []
    assert r.targets(ch, b1, {"role": "done", "text": "ok", "source": "build"}) == []


def test_deliver_prefixes_in_own_identity_and_numbers_options():
    ch = FakeChannel()
    b1 = FakeBot("b1", "Scout")
    r = rmod.Router(FakeRegistry(b1))
    r.add(ch)
    ev = {"seq": 5, "role": "question", "text": "Size?", "options": ["Studio", "1br"], "via": {"kind": "fake", "addr": "+1"}}
    b1.meta["waiting_on"] = 5  # numbers map back only while the bot still waits on that question
    assert r.deliver(ch, b1, ev) == ["+1"]
    assert ch.sent == [("+1", "@Scout Size?\n\nReply 1 Studio · 2 1br")]
    # the numbered answer maps back on the way in, once
    ch.inbox = [Inbound(addr="+1", text="2")]
    for m in ch.poll():
        r.dispatch(ch, m)
    assert b1.received == [("1br", {"kind": "fake", "addr": "+1"})]
    ch.inbox = [Inbound(addr="+1", text="2")]
    for m in ch.poll():
        r.dispatch(ch, m)
    assert b1.received[-1] == ("2", {"kind": "fake", "addr": "+1"})
    # a dedicated identity carries no prefix
    ch2 = FakeChannel(mode="dedicated")
    r.add(ch2)
    r.deliver(ch2, b1, {"role": "done", "text": "Done.", "via": {"kind": "fake", "addr": "+1"}})
    assert ch2.sent == [("+1", "Done.")]




def test_split_summary_and_summary_render():
    assert base.split_summary("Full answer.\n\nSUMMARY: Short one.") == ("Full answer.", "Short one.")
    assert base.split_summary("Full answer.\n**Summary:** Short one.", "") == ("Full answer.", "Short one.")
    assert base.split_summary("Just text.", " given  field ") == ("Just text.", "given field")
    assert base.split_summary("Text.\nSUMMARY: ignored", "field wins") == ("Text.", "field wins")
    assert base.split_summary("No marker here") == ("No marker here", "")
    # the router texts the summary on a max_len surface, the full text on the web
    ev = {"role": "done", "text": "Long " * 200, "summary": "Short."}
    assert rmod.render(ev, CAPS) == ("Short.", [])
    assert rmod.render(ev, base.WEB_CAPS)[0].startswith("Long Long")
    ev = {"role": "question", "text": "Which of these three long options…", "summary": "Which?", "options": ["A", "B"]}
    assert rmod.render(ev, CAPS) == ("Which?\n\nReply 1 A · 2 B", ["A", "B"])


def test_deliver_writes_delivery_rows():
    ch = FakeChannel()
    b1 = FakeBot("b1", "Scout")
    r = rmod.Router(FakeRegistry(b1))
    r.add(ch)
    ev = {"seq": 7, "role": "done", "text": "Full.", "summary": "Short.", "via": {"kind": "fake", "addr": "+1"}}
    assert r.deliver(ch, b1, ev) == ["+1"]
    assert b1.emitted == [{"seq": 100, "role": "delivery", "text": "@Scout Short.", "ref": 7, "channel": "fake", "addr": "+1", "via": None}]
    # a send that keeps failing leaves a row with the error, and nothing is marked sent
    class Broken(FakeChannel):
        def send(self, addr, text, event=None):
            raise RuntimeError("osascript: Messages got an error")
    bad = Broken()
    r.add(bad)
    rmod.SEND_RETRY_WAIT_S, old = 0, rmod.SEND_RETRY_WAIT_S
    try:
        assert r.deliver(bad, b1, {"seq": 8, "role": "error", "text": "boom", "via": {"kind": "fake", "addr": "+1"}}) == []
    finally:
        rmod.SEND_RETRY_WAIT_S = old
    row = b1.emitted[-1]
    assert row["role"] == "delivery" and row["ref"] == 8 and row["error"].startswith("osascript")
    # delivery rows are not outbound-worthy themselves
    r.on_event(b1, row)
    assert r._q.empty()




def test_on_event_ignores_what_no_channel_carries():
    ch = FakeChannel()
    b1 = FakeBot("b1", "Scout")
    r = rmod.Router(FakeRegistry(b1))
    r.add(ch)
    r.on_event(b1, {"role": "action", "text": "click"})
    r.on_event(b1, {"role": "done", "text": "   "})
    assert r._q.empty()
    r.on_event(b1, {"role": "done", "text": "ok"})
    assert r._q.qsize() == 1


def test_registry_without_router_is_inert(app_home):
    """Tests never start the router (conftest); emit() -> registry.on_event must not care."""
    assert registry.router() is None and registry.channel_states() is None and registry.imessage_state() is None
    registry.on_event(FakeBot("b1", "Scout"), {"role": "done", "text": "ok"})  # no router: silently dropped


# ---- the iMessage channel against a fake chat.db --------------------------------------
def _fake_chat_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        create table handle (ROWID integer primary key, id text, service text);
        create table chat (ROWID integer primary key, chat_identifier text);
        create table chat_message_join (chat_id integer, message_id integer);
        create table message (ROWID integer primary key, handle_id integer, text text, attributedBody blob,
                              is_from_me integer, date integer, account text);
        insert into handle values (1, '+15551234567', 'iMessage'), (2, '+15551234567', 'SMS'), (3, '+15559999999', 'iMessage');
        insert into chat values (1, '+15551234567'), (2, '+15559999999'), (3, 'chat123');
        insert into message values (1, 1, 'old', null, 0, 0, null);
        insert into chat_message_join values (1, 1);
        insert into message values (2, 1, 'reply', null, 1, 0, 'P:+15551234567');
        insert into chat_message_join values (1, 2);
    """)
    db.commit()
    db.close()


def _add(path, rowid, handle_id, text, chat_id=1, from_me=0):
    db = sqlite3.connect(path)
    db.execute("insert into message values (?, ?, ?, null, ?, 0, null)", (rowid, handle_id, text, from_me))
    db.execute("insert into chat_message_join values (?, ?)", (chat_id, rowid))
    db.commit()
    db.close()


def test_imessage_poll_filters_and_identity(app_home, tmp_path, monkeypatch):
    dbp = str(tmp_path / "chat.db")
    _fake_chat_db(dbp)
    monkeypatch.setattr(imessage, "CHAT_DB", dbp)
    store.write_meta("b1", {"id": "b1", "name": "Scout", "kind": "super", "imessage": "+1 555 123 4567"})
    ch = ImessageChannel()
    assert ch.door() == ("b1", "+15551234567")
    assert ch.poll() == []                              # first run: cursor set to now, nothing replayed
    assert imessage.load_cursor()["rowid"] == 2
    _add(dbp, 3, 1, "find flats")                       # owner over iMessage
    _add(dbp, 4, 2, "yes")                              # same number over SMS: never a command
    _add(dbp, 5, 3, "hi")                               # not an owner
    _add(dbp, 6, 1, "group hello", chat_id=3)           # group chat
    got = ch.poll()
    assert [(m.addr, m.text, m.service) for m in got] == [("+15551234567", "find flats", "iMessage")]
    assert imessage.load_cursor()["rowid"] == 6
    # the Mac sends as +15551234567 (message.account of a sent row) and that is the owner: own identity
    assert ch.identity() == {"mode": "own", "label": "+15551234567"}
    # an echo of what we sent is swallowed. Stubbing only the osascript call
    # (not send_text itself) keeps its own recording of the chunk into the
    # cursor's echo window — that recording is what poll() below checks.
    sent = []

    def _fake_run(cmd, **kwargs):
        sent.append((cmd[-2], cmd[-1]))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(imessage.subprocess, "run", _fake_run)
    ch.send("+15551234567", "@Scout Which size?")
    assert sent == [("+15551234567", "@Scout Which size?")]
    _add(dbp, 7, 1, "@Scout Which size?")
    assert ch.poll() == [] and ch.state["echoes"] == 1
    s = ch.status()
    assert s["running"] is True and s["handles"] == 1 and s["last_in"] and s["last_out"]
    with open(imessage.state_path()) as f:
        assert json.load(f)["handles"] == 1
    ch.stop()
    assert not os.path.getsize(imessage.lock_path())


def test_imessage_lock_is_exclusive(app_home):
    a, b = ImessageChannel(), ImessageChannel()
    assert a.acquire() is True
    try:
        assert b.acquire() is False and b.state["holder"] == f"pid {os.getpid()}"
        assert b.poll() == [] and b.state["error"].startswith("another bridge is running")
    finally:
        a.stop()
    assert b.acquire() is True
    b.stop()
