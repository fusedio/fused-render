"""Two writers on one events.jsonl (two server processes on one app home, a
botsend.py) must never hand out the same seq: the page keys thread rows by
bot + seq and a repeated key leaves an orphaned bubble behind on bot switch."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
from fused_render.bots import bot as botmod


def test_emit_stays_ahead_of_another_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    a = botmod.create(name="A")
    b = botmod.Bot(a.id)  # a second process's view of the same bot
    a.emit("done", "from a")
    b.emit("system", "from b")
    a.emit("done", "from a again")
    seqs = [e["seq"] for e in a.events_since(0)]
    assert len(seqs) == len(set(seqs)), seqs
    assert seqs == sorted(seqs)


def test_status_cursor_covers_another_writers_lines(client, tmp_path, monkeypatch):
    """The `seq` the page stores as its cursor must count every line it was
    handed, including ones another process appended; otherwise the next poll
    re-sends them and the thread shows the same message twice."""
    import json
    from urllib.parse import quote
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    a = botmod.create(name="A")
    other = botmod.Bot(a.id)
    other.emit("system", "from another process")
    st, _, body = client.get("/api/bots?cursors=" + quote(json.dumps({a.id: 0})))
    assert st == 200
    me = next(b for b in json.loads(body)["bots"] if b["id"] == a.id)
    assert me["seq"] == len(me["events"]) == 2
    st, _, body = client.get("/api/bots?cursors=" + quote(json.dumps({a.id: me["seq"]})))
    me = next(b for b in json.loads(body)["bots"] if b["id"] == a.id)
    assert me["events"] == []


def test_pulse_is_the_small_row_for_the_sidebar(client, tmp_path, monkeypatch):
    """`GET /api/bots/pulse` is the sidebar's poll: status, seq, the read mark and the hidden
    flag per bot, with no events, usage or channel state — and no side effects on the bot."""
    import json
    monkeypatch.setattr(botmod.Bot, "greet", lambda self: None)
    a = botmod.create(name="A")
    a.emit("done", "first")
    a.emit("done", "second")
    st, _, body = client.get("/api/bots")
    full = next(b for b in json.loads(body)["bots"] if b["id"] == a.id)
    st, _, body = client.get("/api/bots/pulse")
    assert st == 200
    out = json.loads(body)
    me = next(b for b in out["bots"] if b["id"] == a.id)
    # The same seq the page's own row carries (create() emits a greeting line of its own).
    assert me["status"] == "idle" and me["seq"] == full["seq"] >= 2 and me["hidden"] is False
    assert "seen_seq" not in me and "events" not in me and "usage" not in out
    # `news_seq` is the newest MESSAGE row: a system note appended after it moves `seq`, not `news_seq`, so a bot
    # read up to its last message stays read on the rail (the page's unreadOf skips system rows the same way).
    assert me["news_seq"] == full["news_seq"] == me["seq"]
    a.emit("system", "Browser closed after 20 minutes idle")
    st, _, body = client.get("/api/bots/pulse")
    me = next(b for b in json.loads(body)["bots"] if b["id"] == a.id)
    assert me["seq"] == full["seq"] + 1 and me["news_seq"] == full["seq"]
    # Opening the bot stamps the mark; hiding it flips the flag; a status change shows as is.
    st, _, _ = client.post(f"/api/bots/{a.id}/seen", {"seq": 1})
    assert st == 200
    st, _, _ = client.post(f"/api/bots/{a.id}/flag", {"hidden": True})
    assert st == 200
    botmod.Bot(a.id).meta["status"] = "waiting"
    a.meta["status"] = "waiting"
    st, _, body = client.get("/api/bots/pulse")
    me = next(b for b in json.loads(body)["bots"] if b["id"] == a.id)
    assert me["seen_seq"] == 1 and me["hidden"] is True and me["status"] == "waiting"
