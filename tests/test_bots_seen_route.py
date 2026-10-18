"""`POST /api/bots/{bid}/seen` (D1377): the per-bot unread mark moved off
localStorage (`browser-bot.seen`), which the desktop app's port walk wiped on
every relaunch that landed on a new origin. Stored as meta["seen_seq"],
monotone, surfaced on the status row as `seen_seq` (absent until opened)."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
from test_bots_routes import j, make, one, status, ws  # noqa: F401 — `ws` is a fixture

from fused_render.bots import store


def test_seen_is_absent_until_opened_then_monotone(client, ws):
    bid = make(client)
    assert "seen_seq" not in one(status(client), bid)

    st, out = j(client.post(f"/api/bots/{bid}/seen", {"seq": 3}))
    assert st == 200 and out == {"ok": True, "seen_seq": 3}
    assert one(status(client), bid)["seen_seq"] == 3
    assert one(status(client, fast=1), bid)["seen_seq"] == 3
    assert store.read_meta(bid)["seen_seq"] == 3

    # A stale page (or a racing window) posting an older cursor never lowers it.
    st, out = j(client.post(f"/api/bots/{bid}/seen", {"seq": 1}))
    assert st == 200 and out == {"ok": True, "seen_seq": 3}
    st, out = j(client.post(f"/api/bots/{bid}/seen", {"seq": 3}))
    assert st == 200 and out["seen_seq"] == 3
    st, out = j(client.post(f"/api/bots/{bid}/seen", {"seq": 7}))
    assert st == 200 and out["seen_seq"] == 7
    assert store.read_meta(bid)["seen_seq"] == 7


def test_seen_rejects_bad_seqs(client, ws):
    bid = make(client)
    for body in ({}, {"seq": -1}, {"seq": "4"}, {"seq": True}, {"seq": 1.5}, {"seq": None}):
        st, out = j(client.post(f"/api/bots/{bid}/seen", body))
        assert st == 400, (body, out)
    assert "seen_seq" not in one(status(client), bid)


def test_seen_unknown_bot_is_404_and_needs_x_fused(client, ws):
    st, _ = j(client.post("/api/bots/nope0000/seen", {"seq": 1}))
    assert st == 404
    bid = make(client)
    st, _ = j(client.post(f"/api/bots/{bid}/seen", {"seq": 1}, headers={"X-Fused": ""}))
    assert st == 403
    assert "seen_seq" not in one(status(client), bid)
