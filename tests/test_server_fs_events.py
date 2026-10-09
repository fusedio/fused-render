"""The file-change watch — the `fs.watch` topic of the events bus (what
`/api/fs/events` was, SPEC §13.2) — and its coalescing stat registry
(fused_render/server/watch.py).

These pin the poller hardening: never blocking the event loop on a stat, and
coalescing duplicate watchers onto one ticker, now across subscriptions on the
one socket per document.
"""
import asyncio
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.server import fs_stat as _server_fs_stat
from fused_render.server import watch as _server_watch
from fused_render.server import create_app
from fused_render.server.events import bus

# `ws_origin_ok` wants a loopback Host the Origin names; the test client stamps
# `Host: testserver`, so both are set explicitly.
_LOOPBACK = {"origin": "http://127.0.0.1", "host": "127.0.0.1"}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    bus.reset()
    yield h
    bus.reset()


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)), headers=_LOOPBACK)


def _watch(ws, sid, *paths):
    ws.receive_json()  # hello
    ws.send_json({"t": "sub", "id": sid, "topic": "fs.watch", "params": {"paths": list(paths)}})


def _frame(ws, want, tries=50):
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("t") == want:
            return msg
    raise AssertionError(f"no {want} frame")


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
        with client.websocket_connect("/api/events") as ws:
            _watch(ws, 1, watched)
            # The snapshot's own stat and the ticker's first read are now hung
            # in worker threads. An unrelated request must still return; run
            # it off the test thread so a regression (loop blocked) surfaces as
            # a timeout, not a hang.
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
            # teardown. By here we've already proven the loop stayed responsive.
            release.set()
    finally:
        release.set()  # safety net if an assertion above raised first


def test_duplicate_watchers_share_one_stat_stream(home, tmp_path, monkeypatch):
    # (c) Two documents watching the same path must share ONE ticker: N panes
    # previewing the same file made N stats/interval, multiplying remote load.
    # The bus refcounts per key, and the registry per path under it.
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
    with client.websocket_connect("/api/events") as a, client.websocket_connect("/api/events") as b:
        _watch(a, 1, watched)
        _watch(b, 1, watched)
        _frame(a, "snap")
        _frame(b, "snap")
        time.sleep(0.5)
        # One bus key with two subscribers, one registry entry with one queue
        # behind it (the bus's single pump per key).
        assert bus.subscriber_count("fs.watch") == 2
        entry = _server_watch._WATCH_REGISTRY._entries.get(watched)
        assert entry is not None
        assert len(entry.subscribers) == 1

    # One ticker at 200ms over ~0.5s reads ~3-4 times (plus the two snapshot
    # stats); two independent tickers would double that. The upper bound
    # proves a single stream.
    assert 1 <= count["n"] <= 8


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
    # Regression guard on the happy path: a local edit still reaches the
    # subscriber as a delta `{changes: [{path, mtime}]}` (LR-*).
    watched = tmp_path / "edit.html"
    watched.write_text("v1", encoding="utf-8")

    client = _client(tmp_path)
    with client.websocket_connect("/api/events") as ws:
        _watch(ws, 1, str(watched))
        snap = _frame(ws, "snap")
        assert snap["body"]["paths"][0]["path"] == str(watched)
        time.sleep(0.3)  # let the baseline prime
        watched.write_text("v2", encoding="utf-8")
        os.utime(watched, (time.time() + 2, time.time() + 2))
        delta = _frame(ws, "delta")
        assert delta["body"]["changes"][0]["path"] == str(watched)
