"""Tests for the /api/fs/events WebSocket change feed and its coalescing stat
registry (fused_render/server.py).

These pin the hardening added after a read-only S3-backed rclone NFS mount died
with the macOS "Server connections interrupted" dialog. Root cause: the poller
called os.stat() on every watched path every 200ms, and each stat is a kernel
NFS GETATTR that can force rclone to re-list a huge remote directory (a
world-scale .zarr) and blow past the NFS client's timeout, killing the mount.

The registry (see server._WATCH_REGISTRY) fixes it by: never stat'ing a
mount-backed path through the kernel (rc API instead), never blocking the event
loop on a stat, coalescing duplicate watchers onto one ticker, and polling
mounts slowly.
"""
import os
import threading
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

import fused_render.server as server
from fused_render.server import create_app


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "mounts").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    return h


def _client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_mount_path_is_never_kernel_stat(home, tmp_path, monkeypatch):
    # (a) A path under the mounts dir must never be os.stat'd — that GETATTR is
    # the mount-killing hazard. With no live rcd, rc_mtime_for returns None
    # ("unchanged"), and crucially the code must NOT fall back to os.stat.
    mount_path = str(home / "mounts" / "s3demo" / "world.zarr")

    seen = []
    real_stat = os.stat

    def spy(path, *a, **k):
        seen.append(os.fspath(path))
        return real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", spy)

    client = _client(tmp_path)
    with client.websocket_connect("/api/fs/events?path=" + quote(mount_path)):
        # Give the ticker's immediate first read time to run (it runs before any
        # sleep). It resolves via the rc API in a thread, not os.stat.
        time.sleep(0.4)

    assert mount_path not in seen


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
        entry = server._WATCH_REGISTRY._entries.get(watched)
        assert entry is not None
        assert len(entry.subscribers) == 2

    # One ticker at 200ms over ~0.5s reads ~3-4 times; two independent tickers
    # would double that. The upper bound proves a single stream.
    assert 1 <= count["n"] <= 5


def test_mount_paths_tick_slowly_local_paths_tick_fast(home, tmp_path):
    # (d) Classification fixes the poll interval once: mount-backed paths poll
    # every 5s (rc API, low remote pressure), local paths every 200ms.
    mount_entry = server._WatchEntry(str(home / "mounts" / "m" / "f.parquet"))
    local_entry = server._WatchEntry(str(tmp_path / "local.html"))

    assert mount_entry.is_mount is True
    assert mount_entry.interval == server._MOUNT_POLL_S == 5.0
    assert local_entry.is_mount is False
    assert local_entry.interval == server._LOCAL_POLL_S == 0.2


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
