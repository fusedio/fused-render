"""Tests for the /api/fs/events WebSocket change feed and its coalescing stat
registry (fused_render/server.py).

These pin the poller hardening: never blocking the event loop on a stat,
and coalescing duplicate watchers onto one ticker.
"""
import asyncio
import os
import threading
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fused_render.server import fs_stat as _server_fs_stat
from fused_render.server import watch as _server_watch
from fused_render.server import create_app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_hung_stat_does_not_block_the_event_loop(home, tmp_path, monkeypatch):
    # (b) A stat that blocks forever must not freeze the server's event loop:
    # every other request would stall. Stats run in a worker thread, so an
    # unrelated HTTP request must still complete promptly while one hangs.
    watched = str(tmp_path / "hangs.html")
    (tmp_path / "hangs.html").write_text("<html></html>", encoding="utf-8")

    release = threading.Event()
    real_stat = os.stat

    def spy(path, *a, **k):
        if os.fspath(path) == watched:
            release.wait()  # block until the test releases us (teardown-safe)
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", spy)

    client = _client(tmp_path)
    try:
        with client.websocket_connect("/api/fs/events?path=" + quote(watched)):
            # The ticker's first read is now hung in a worker thread. An
            # unrelated request must still return; run it off the test thread so
            # a regression (loop blocked) surfaces as a timeout, not a hang.
            result = {}

            def do_get():
                result["status"] = client.get("/api/config").status_code

            t = threading.Thread(target=do_get)
            t.start()
            t.join(timeout=5)
            assert not t.is_alive(), "event loop blocked by a hung stat"
            assert result["status"] == 200
            # Release BEFORE leaving the `with`: the loop's shutdown joins its
            # executor threads, so a still-blocked stat worker would deadlock
            # teardown (the very "can't cancel a thread" property item 1 works
            # around). By here we've already proven the loop stayed responsive.
            release.set()
    finally:
        release.set()  # safety net if an assertion above raised first


def test_duplicate_watchers_share_one_stat_stream(home, tmp_path, monkeypatch):
    # (c) Two sockets watching the same path must share ONE ticker: N panes
    # previewing the same file made N stats/interval, multiplying remote load.
    watched = str(tmp_path / "shared.html")
    (tmp_path / "shared.html").write_text("<html></html>", encoding="utf-8")

    count = {"n": 0}
    real_stat = os.stat

    def spy(path, *a, **k):
        if os.fspath(path) == watched:
            count["n"] += 1
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", spy)

    client = _client(tmp_path)
    url = "/api/fs/events?path=" + quote(watched)
    with client.websocket_connect(url), client.websocket_connect(url):
        time.sleep(0.5)
        # Registry coalesced to a single refcounted entry with two subscribers.
        entry = _server_watch._WATCH_REGISTRY._entries.get(watched)
        assert entry is not None
        assert len(entry.subscribers) == 2

    # One ticker at 200ms over ~0.5s reads ~3-4 times; two independent tickers
    # would double that. The upper bound proves a single stream.
    assert 1 <= count["n"] <= 5


def test_read_consumes_a_completed_slow_stat(tmp_path, monkeypatch):
    # (3.3) A stat that outlives its wait_for keeps running; the NEXT tick must
    # CONSUME its finished result rather than discard the done future and start
    # over — else a path whose stat always exceeds the timeout never primes.
    entry = _server_watch._WatchEntry(str(tmp_path / "f.html"))
    monkeypatch.setattr(_server_fs_stat, "_STAT_TIMEOUT_S", 0.02)

    async def slow():
        await asyncio.sleep(0.1)
        return 123.0

    entry._stat_signal = slow

    async def scenario():
        first = await entry._read()
        assert first is _server_watch._UNCHANGED         # timed out; future left running
        assert entry._inflight is not None
        await asyncio.sleep(0.15)                  # let the slow stat finish
        second = await entry._read()
        assert second == 123.0                     # consumed, not discarded
        assert entry._inflight is None

    asyncio.run(scenario())


def test_local_change_is_reported(home, tmp_path):
    # Regression guard on the happy path: a local edit still reaches the socket
    # (the coalescing rewrite must not have broken change delivery, LR-*).
    watched = tmp_path / "edit.html"
    watched.write_text("v1", encoding="utf-8")

    client = _client(tmp_path)
    with client.websocket_connect(
            "/api/fs/events?path=" + quote(str(watched))) as ws:
        time.sleep(0.3)  # let the baseline prime
        watched.write_text("v2", encoding="utf-8")
        os.utime(watched, (time.time() + 2, time.time() + 2))
        msg = ws.receive_json()
        # Skip an interleaved keepalive if one lands first.
        if msg.get("keepalive"):
            msg = ws.receive_json()
        assert msg["path"] == str(watched)
