"""fused_render.bots.channels: the router's policy and the iMessage channel's
poll, with a fake channel and a fake chat.db (docs/bots.md §10)."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import sqlite3

from fused_render.bots import channels, imessage, registry, store
from fused_render.bots.channels import base, router as rmod
from fused_render.bots.channels.base import Caps, Channel, Inbound
from fused_render.bots.channels.imessage import CAPS, ImessageChannel


class FakeChannel(Channel):
    kind = "fake"
    caps = Caps(options=False, buttons=False, max_len=120, media=False, can_login=False)

    def __init__(self, owners, mode="own"):
        self._owners, self._mode, self.sent, self.inbox = owners, mode, [], []

    def owners(self):
        return self._owners

    def poll(self):
        out, self.inbox = self.inbox, []
        return out

    def send(self, addr, text, event=None):
        self.sent.append((addr, text))

    def identity(self):
        return {"mode": self._mode, "label": "me"}


class FakeBot:
    def __init__(self, bid, name, meta=None):
        self.id, self.meta, self.received = bid, {"name": name, **(meta or {})}, []

    def receive(self, text, via=None, reply_to=None):
        self.received.append((text, via))


class FakeRegistry:
    def __init__(self, *bots):
        self.bots = {b.id: b for b in bots}

    def get(self, bid):
        return self.bots[bid]


# ---- pure helpers -------------------------------------------------------------------
def test_render_numbers_options_and_shortens():
    assert rmod.render({"role": "question", "text": "Size?", "options": ["Studio", "1br", "2br"]}, CAPS) == \
        ("Size?\n\nReply 1 Studio · 2 1br · 3 2br", ["Studio", "1br", "2br"])
    assert rmod.render({"role": "approval", "text": "Buy?"}, CAPS) == ("Buy?\n\nReply yes or no.", [])
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


def test_split_at_name():
    names = {"scout": "s1", "mover": "m1"}
    assert rmod.split_at_name("@Scout find flats", names) == ("s1", "find flats")
    assert rmod.split_at_name("@sc: hi", names) == ("s1", "hi")
    assert rmod.split_at_name("@m, pack", names) == ("m1", "pack")
    assert rmod.split_at_name("@Scout", names) == ("s1", "")
    assert rmod.split_at_name("hello @Scout", names) == (None, "hello @Scout")
    assert rmod.split_at_name("@nobody do it", names) == (None, "@nobody do it")


def test_category_and_forwards():
    assert rmod.category({"role": "done"}) == "results"
    assert rmod.category({"role": "done", "via": {"kind": "routine", "addr": ""}}) == "routines"
    assert rmod.category({"role": "done", "source": "build"}) == "builds"
    assert rmod.category({"role": "question"}) == "questions" and rmod.category({"role": "approval"}) == "questions"
    assert rmod.category({"role": "error"}) == "errors"
    assert rmod.forwards_for({}, "imessage") == base.FORWARDS_DEFAULT
    assert rmod.forwards_for({"channel_forwards": {"imessage": ["errors"]}}, "imessage") == ("errors",)


def test_prompt_section_and_login_text():
    class B:
        task_via = {"kind": "imessage", "addr": "+1"}
        task_origin = "imessage"
    assert channels.prompt_for(B()).startswith("CHANNEL: the user sent this task from iMessage")
    assert "come sign in there" in channels.login_text(B(), "Needs sign-in.")
    assert channels.origin_label(B()).startswith("iMessage")

    class W:
        task_via = dict(base.WEB)
        task_origin = "manual"
    assert channels.prompt_for(W()) == "" and channels.origin_label(W()) == "chat"
    assert "click Hand back" in channels.login_text(W(), "Needs sign-in.")


# ---- router policy ------------------------------------------------------------------
def test_targets_reply_to_origin_then_forwards():
    ch = FakeChannel({"+1": ["b1", "b2"]})
    b1 = FakeBot("b1", "Scout")
    r = rmod.Router(FakeRegistry(b1))
    r.add(ch)
    # a texted task answers the sender, whatever the forwards say
    b1.meta["channel_forwards"] = {"fake": []}
    assert r.targets(ch, b1, {"role": "done", "text": "ok", "via": {"kind": "fake", "addr": "+1"}}) == ["+1"]
    # a web task reaches the owners only through forwards
    assert r.targets(ch, b1, {"role": "done", "text": "ok"}) == []
    b1.meta["channel_forwards"] = {"fake": ["results"]}
    assert r.targets(ch, b1, {"role": "done", "text": "ok"}) == ["+1"]
    assert r.targets(ch, b1, {"role": "error", "text": "x"}) == []
    # routines are off by default
    del b1.meta["channel_forwards"]
    assert r.targets(ch, b1, {"role": "done", "text": "ok", "via": {"kind": "routine", "addr": ""}}) == []
    assert r.targets(ch, b1, {"role": "done", "text": "ok", "source": "build"}) == ["+1"]


def test_deliver_prefixes_in_own_identity_and_numbers_options():
    ch = FakeChannel({"+1": ["b1"]})
    b1 = FakeBot("b1", "Scout")
    r = rmod.Router(FakeRegistry(b1))
    r.add(ch)
    ev = {"role": "question", "text": "Size?", "options": ["Studio", "1br"], "via": {"kind": "fake", "addr": "+1"}}
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
    ch2 = FakeChannel({"+1": ["b1"]}, mode="dedicated")
    r.add(ch2)
    r.deliver(ch2, b1, {"role": "done", "text": "Done.", "via": {"kind": "fake", "addr": "+1"}})
    assert ch2.sent == [("+1", "Done.")]


def test_dispatch_routes_by_name_then_sticky():
    store.write_meta("b1", {"id": "b1", "name": "Scout"})
    store.write_meta("b2", {"id": "b2", "name": "Mover"})
    ch = FakeChannel({"+1": ["b1", "b2"], "+2": ["b2"]})
    b1, b2 = FakeBot("b1", "Scout"), FakeBot("b2", "Mover")
    r = rmod.Router(FakeRegistry(b1, b2))
    r.add(ch)
    r.dispatch(ch, Inbound(addr="+1", text="first"))          # no history: the first bot
    r.dispatch(ch, Inbound(addr="+1", text="@mover pack up"))  # @name switches…
    r.dispatch(ch, Inbound(addr="+1", text="and the boxes"))   # …and sticks
    r.dispatch(ch, Inbound(addr="+1", text="@Mover"))          # a bare address: sticky, nothing to run
    r.dispatch(ch, Inbound(addr="+9", text="stranger"))        # not an owner: dropped
    assert [t for t, _ in b1.received] == ["first"]
    assert [t for t, _ in b2.received] == ["pack up", "and the boxes"]
    assert all(v == {"kind": "fake", "addr": "+1"} for _, v in b1.received + b2.received)


def test_on_event_ignores_what_no_channel_carries():
    ch = FakeChannel({"+1": ["b1"]})
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
    store.write_meta("b1", {"id": "b1", "name": "Scout", "imessage": "+1 555 123 4567"})
    ch = ImessageChannel()
    assert ch.owners() == {"+15551234567": ["b1"]}
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
    # an echo of what we sent is swallowed
    sent = []
    monkeypatch.setattr(imessage, "send_text", lambda h, t: sent.append((h, t)))
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
