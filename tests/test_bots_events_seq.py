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
