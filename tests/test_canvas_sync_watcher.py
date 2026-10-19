"""Canvas sync watcher — the two-loops build's workstream A.

The local loop must never wait on the network: a push runs the CLI on a
frozen copy with `_op_lock` released, so an embed snapshot lands while it
runs. Plus the pieces that make the cloud loop visible — the one-writer pid
lock, `sync/state`, the event ring behind `sync/log`, the fs-event disk
signal, and the `sync/events` socket.

Reuses test_canvases.py's harness (stub CLI, manifest/zip shims).
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.canvases as canvases_mod
from fused_render import canvas_snapshot
from test_canvases import (  # noqa: F401 — fixtures are used by name
    GUARD,
    _cloned_shim_harness,
    _manager,
    _reset_module_state,
    _stop_sync,
    _wait_for,
    _wait_sync,
    fake_agent,
    harness,
)

EMPTY_SNAPSHOT = {"dashboard_body": {"studioState": {"nodes": []}}, "udfs": []}


def _start(harness):
    res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200, res.text
    return _manager()


def _quiet_watcher(monkeypatch):
    """No tick, no debounce, no poll: the test drives the manager's fields."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 30.0)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 30.0)


# -- frozen-copy push ----------------------------------------------------------

def test_a_snapshot_applies_while_a_slow_push_runs(harness, tmp_path, monkeypatch, fake_agent):
    """The CLI push runs on `.sync/push/<name>/` with `_op_lock` free: an embed
    snapshot arriving mid-push is applied at once (not 409'd after
    SNAPSHOT_LOCK_WAIT_S), and the write it makes — not in the frozen copy —
    re-arms the debounce so a second push ships it."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    (harness.root / "alpha" / ".cache").mkdir()
    (harness.root / "alpha" / ".cache" / "junk.bin").write_text("x")

    started = threading.Event()
    release = threading.Event()
    seen: dict = {}
    real = canvases_mod._SyncManager._run_push_cli

    def slow_push(self, cli, copy_dir):
        if not started.is_set():
            seen["op_lock_held"] = self._op_lock.locked()
            seen["copy_dir"] = copy_dir
            seen["copy_files"] = sorted(os.listdir(copy_dir))
            seen["copy_a"] = open(os.path.join(copy_dir, "a.py")).read()
            started.set()
            release.wait(5)
        return real(self, cli, copy_dir)

    monkeypatch.setattr(canvases_mod._SyncManager, "_run_push_cli", slow_push)
    manager = _start(harness)
    (harness.root / "alpha" / "a.py").write_text("a2-edited\n")
    assert started.wait(5), "the push never reached the CLI"
    assert manager.push_state == "pushing"

    t0 = time.time()
    res = harness.client.post(
        "/api/canvases/snapshot",
        json={"name": "alpha", "gen": 1, "seq": 1, "body": EMPTY_SNAPSHOT}, headers=GUARD)
    elapsed = time.time() - t0
    # A write the copy cannot contain: lands while the CLI is still running.
    (harness.root / "alpha" / "b.py").write_text("b2-during-push\n")
    time.sleep(0.15)
    release.set()

    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True, res.json()
    assert elapsed < canvases_mod.SNAPSHOT_LOCK_WAIT_S, elapsed
    assert seen["op_lock_held"] is False
    copy_dir = seen["copy_dir"]
    assert copy_dir == os.path.join(str(harness.root), ".sync", "push", "alpha")
    assert seen["copy_a"] == "a2-edited\n"
    assert ".fusedignore" in seen["copy_files"], seen["copy_files"]
    assert ".cache" not in seen["copy_files"], seen["copy_files"]

    # The mid-push write re-armed the debounce: a second push follows.
    status = _wait_sync(lambda s: s["push_seq"] >= 2 and s["push_state"] == "idle")
    assert status["push_seq"] >= 2, status
    pushes = [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    assert pushes and pushes[0][3] == copy_dir and pushes[0][4:] == ["--canvas", "alpha"]
    assert not os.path.exists(copy_dir), "the frozen copy outlived its push"
    actions = [e["action"] for e in harness.client.get(
        "/api/canvases/sync/log?name=alpha").json()["events"]]
    assert "push start" in actions and "push end" in actions and "snapshot applied" in actions


def test_the_manual_push_waits_its_turn_behind_a_running_push(
        harness, tmp_path, monkeypatch, fake_agent):
    """Pushes stay serialized per canvas without _op_lock spanning them: the
    flight lock answers a concurrent manual push with busy."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 0.1)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 30.0)
    monkeypatch.setattr(canvases_mod, "MANUAL_PUSH_LOCK_WAIT_S", 0.2)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    started, release = threading.Event(), threading.Event()
    real = canvases_mod._SyncManager._run_push_cli

    def slow_push(self, cli, copy_dir):
        started.set()
        release.wait(5)
        return real(self, cli, copy_dir)

    monkeypatch.setattr(canvases_mod._SyncManager, "_run_push_cli", slow_push)
    _start(harness)
    (harness.root / "alpha" / "a.py").write_text("a2\n")
    assert started.wait(5)
    try:
        res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"},
                                  headers=GUARD)
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "busy"
    finally:
        release.set()
    assert _wait_sync(lambda s: s["push_seq"] >= 1)["push_seq"] >= 1


# -- pid lock ------------------------------------------------------------------

_HOLDER = r"""
import fcntl, json, os, sys, time
path = sys.argv[1]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, json.dumps({"pid": os.getpid(), "port": 4242, "since": 1.5}).encode())
print("ready", flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_a_live_foreign_lock_refuses_start_and_a_dead_one_is_taken_over(
        harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    lock = harness.root / ".sync" / "alpha.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    other = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)],
                             stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "ready"
        res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"},
                                  headers=GUARD)
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["code"] == "owned"
        assert body["owner"] == {"pid": other.pid, "port": 4242, "since": 1.5}
        assert "alpha" not in canvases_mod._syncs
        state = harness.client.get("/api/canvases/sync/state?name=alpha").json()
        assert state["watching"] is False and state["watcher_epoch"] is None
        assert state["owner"] == {"pid": other.pid, "port": 4242}
        assert state["other_watchers"] == [{"pid": other.pid, "port": 4242}]
        # A session's push through the intercept learns WHO owns it.
        push = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"},
                                   headers=GUARD)
        assert push.status_code == 409 and push.json()["code"] == "owned", push.text
        assert push.json()["owner"]["pid"] == other.pid
        # Untouched by the refusal.
        assert json.loads(lock.read_text())["pid"] == other.pid
    finally:
        other.kill()
        other.wait()

    # The holder died: the kernel dropped its flock, the file still names it.
    res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200, res.text
    held = json.loads(lock.read_text())
    assert held["pid"] == os.getpid()
    events = harness.client.get("/api/canvases/sync/log?name=alpha").json()["events"]
    taken = [e for e in events if e["action"] == "lock taken over"]
    assert taken and str(other.pid) in taken[0]["detail"], events

    _stop_sync("alpha")
    assert not lock.exists(), "stop() left the writer lock behind"


def test_a_stale_file_naming_a_live_pid_does_not_refuse(harness, tmp_path, monkeypatch):
    """The lock is the flock, not the pid in the file: a file a quit left behind
    whose pid was reused after a reboot (here: our parent, alive) is free."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    lock = harness.root / ".sync" / "alpha.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getppid(), "port": 9, "since": 3.0}))
    res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200, res.text
    assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_the_quit_rung_stops_every_manager_and_refuses_new_ones(
        harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    lock = harness.root / ".sync" / "alpha.lock"
    assert lock.exists()
    monkeypatch.setattr(canvases_mod, "_QUITTING", False)  # restored after the test
    canvases_mod.shutdown()
    assert m.stop_event.is_set() and not m.thread.is_alive() and not m.fs_thread.is_alive()
    assert not lock.exists()
    assert canvases_mod._syncs == {}
    res = harness.client.post("/api/canvases/sync/start", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409 and res.json()["code"] == "busy", res.text
    assert not lock.exists()

def test_this_processes_own_lock_never_refuses_it(harness, tmp_path, monkeypatch):
    """A manager replaced inside the same server (stop/start, self-heal) finds
    its own pid in the file: re-taken, never 409."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    lock = harness.root / ".sync" / "alpha.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "port": 1, "since": 2.0}))
    first = _start(harness)
    # The manager died without stop() — the self-heal path replaces it.
    first.stop_event.set()
    second = _start(harness)
    assert second is not first
    assert json.loads(lock.read_text())["pid"] == os.getpid()
    # The old manager's late stop() must not remove the new one's claim.
    first.stop()
    assert lock.exists()


# -- sync/state ----------------------------------------------------------------

_LEG_KEYS = {
    "embed_to_disk": {"state", "oldest_age_ms", "reason"},
    "disk_to_embed": {"state", "oldest_age_ms", "reason"},
    "disk_to_server": {"state", "oldest_age_ms", "reason", "last_push_at", "push_seq"},
    "server_to_disk": {"state", "since_ms", "changed", "reason"},
}


def _state(harness):
    res = harness.client.get("/api/canvases/sync/state?name=alpha")
    assert res.status_code == 200, res.text
    body = res.json()
    assert {"name", "legs", "disk_rev", "watcher_epoch", "owner", "other_watchers",
            "error_detail"} <= set(body)
    for leg, keys in _LEG_KEYS.items():
        assert set(body["legs"][leg]) == keys, (leg, body["legs"][leg])
    return body


def test_state_reports_every_leg_state(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    m.push_state = "idle"
    m._dirty_since = None

    s = _state(harness)
    assert {leg: v["state"] for leg, v in s["legs"].items()} == {
        "embed_to_disk": "idle", "disk_to_embed": "idle",
        "disk_to_server": "idle", "server_to_disk": "idle"}
    assert s["owner"]["pid"] == os.getpid()
    assert s["watcher_epoch"] == m.watcher_epoch
    assert s["other_watchers"] == []

    # disk → server
    m._dirty_since = time.time() - 2
    m.push_state = "pending"
    leg = _state(harness)["legs"]["disk_to_server"]
    assert leg["state"] == "pending" and leg["oldest_age_ms"] >= 2000, leg
    m.push_state = "pushing"
    m._push_started_at = time.time() - 1
    leg = _state(harness)["legs"]["disk_to_server"]
    assert leg["state"] == "pushing" and leg["oldest_age_ms"] >= 1000, leg
    m.push_state = "error"
    m.last_error = "fused canvas push failed with 2 error(s)"
    m.error_detail = ["node x has no source file"]
    s = _state(harness)
    assert s["legs"]["disk_to_server"]["state"] == "error"
    assert s["legs"]["disk_to_server"]["reason"] == m.last_error
    assert s["error_detail"] == ["node x has no source file"]
    m.push_state = "pending"
    m.server_stale = {"since": time.time() - 3,
                      "udfs": [{"slug": "a", "kind": "changed"}], "layout": True}
    s = _state(harness)
    assert s["legs"]["disk_to_server"]["state"] == "held"
    assert s["legs"]["disk_to_server"]["reason"] == "server changed: a (changed)"

    # server → disk
    s2d = s["legs"]["server_to_disk"]
    assert s2d["state"] == "stale" and s2d["since_ms"] >= 3000, s2d
    assert s2d["changed"] == [{"slug": "a", "kind": "changed"}, {"slug": None, "kind": "layout"}]
    m._last_pull_error = "could not read the canvas from the server"
    s2d = _state(harness)["legs"]["server_to_disk"]
    assert s2d["state"] == "error" and s2d["reason"] == m._last_pull_error, s2d
    m._pull_in_flight = True
    m._pull_started_at = time.time() - 0.5
    s2d = _state(harness)["legs"]["server_to_disk"]
    assert s2d["state"] == "pulling" and s2d["since_ms"] >= 500, s2d
    m._pull_in_flight = False

    # disk → embed: nothing owed until a frame has spoken to this manager.
    m._frame_seen = True
    m._note_disk()
    m._view_rev = m.disk_rev - 1
    m._rev_times[m.disk_rev] = time.time() - 0.4
    d2e = _state(harness)["legs"]["disk_to_embed"]
    assert d2e["state"] == "pending" and d2e["oldest_age_ms"] >= 400, d2e
    m._last_cut_held = "source file missing for c"
    d2e = _state(harness)["legs"]["disk_to_embed"]
    assert d2e["state"] == "held" and d2e["reason"] == "source file missing for c", d2e
    m._delta_error = "the workbench refused the delta for rev 3"
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "error"
    m._delta_error = None
    m._last_cut_held = None
    m._view_rev = m.disk_rev
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"

    # The page fills this leg; the server never does.
    assert _state(harness)["legs"]["embed_to_disk"] == {
        "state": "idle", "oldest_age_ms": None, "reason": None}


def test_state_and_log_answer_for_an_unwatched_canvas(harness):
    s = harness.client.get("/api/canvases/sync/state?name=beta").json()
    assert s["watching"] is False and s["disk_rev"] == 0 and s["owner"] is None
    assert all(v["state"] == "idle" for v in s["legs"].values())
    assert harness.client.get("/api/canvases/sync/log?name=beta").json() == {
        "events": [], "next": 0}
    assert harness.client.get("/api/canvases/sync/state?name=../x").status_code == 400


def test_a_delta_cut_and_ack_show_on_the_disk_to_embed_leg(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    cut = harness.client.get("/api/canvases/delta?name=alpha&full=1").json()
    assert "ops" in cut, cut
    # A cut is no evidence of a frame (the page cuts and drops with none).
    assert m._frame_seen is False
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"
    ack = harness.client.post("/api/canvases/delta/ack",
                              json={"name": "alpha", "rev": cut["rev"]}, headers=GUARD)
    assert ack.status_code == 200 and ack.json()["ok"], ack.text
    assert m._frame_seen is True and m._view_rev == cut["rev"]
    # The cut persisted synthesized rows (a.py, b.py have none) after its rev
    # was taken: one more cut + ack brings the frame level.
    cut = harness.client.get("/api/canvases/delta?name=alpha").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"name": "alpha", "rev": cut["rev"]}, headers=GUARD)
    m._view_rev = m.disk_rev
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"
    # Disk moves, the frame has not acked it: pending.
    (harness.root / "alpha" / "a.py").write_text("a-moved\n")
    m._note_disk()
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "pending"
    actions = [e["action"] for e in harness.client.get(
        "/api/canvases/sync/log?name=alpha").json()["events"]]
    assert "delta cut" in actions and "delta acked" in actions, actions


def test_a_cut_that_synthesized_rows_persists_them(harness, tmp_path, monkeypatch):
    """B's compact_state_from_disk reports orphan files it gave a row; the cut
    writes them into canvas.toml (canvas_snapshot.persist_synthesized) under
    the lock, so the next push ships the node with its file."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    real = canvas_snapshot.compact_state_from_disk
    calls = []

    def with_synthesized(clone_dir, remote_ids=None, **kw):
        out = real(clone_dir, remote_ids, **kw)
        out["synthesized"] = ["orphan"]
        return out

    def persist(clone_dir, names):
        calls.append((clone_dir, list(names), _manager()._op_lock.locked()))
        return list(names)

    monkeypatch.setattr(canvas_snapshot, "compact_state_from_disk", with_synthesized)
    monkeypatch.setattr(canvas_snapshot, "persist_synthesized", persist)
    m = _start(harness)
    harness.client.get("/api/canvases/delta?name=alpha&full=1")
    assert calls == [(m.dir, ["orphan"], True)]
    events = harness.client.get("/api/canvases/sync/log?name=alpha").json()["events"]
    assert any(e["action"] == "rows synthesized" and "orphan" in e["detail"] for e in events)


# -- sync/log ------------------------------------------------------------------

def test_the_log_ring_keeps_200_and_pages_by_seq(harness, tmp_path, monkeypatch, caplog):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    caplog.set_level("INFO", logger="fused_render.canvases")
    for i in range(250):
        m.log_event("watcher", "tick", ms=i, detail=f"n{i}")
    assert "canvas alpha: watcher tick n249 (249 ms)" in caplog.text

    page = harness.client.get("/api/canvases/sync/log?name=alpha&after=0").json()
    events = page["events"]
    assert len(events) == 200
    last = m._log.last_seq
    assert events[-1]["seq"] == last and events[0]["seq"] == last - 199
    assert page["next"] == last
    assert set(events[-1]) == {"seq", "t", "leg", "action", "ms", "ok", "detail"}
    assert events[-1]["detail"] == "n249" and events[-1]["ms"] == 249

    page2 = harness.client.get(f"/api/canvases/sync/log?name=alpha&after={last - 3}").json()
    assert [e["detail"] for e in page2["events"]] == ["n247", "n248", "n249"]
    empty = harness.client.get(f"/api/canvases/sync/log?name=alpha&after={last}").json()
    assert empty == {"events": [], "next": last}
    # An `after` from a previous watcher (beyond this ring) reads as 0.
    reset = harness.client.get(f"/api/canvases/sync/log?name=alpha&after={last + 500}").json()
    assert len(reset["events"]) == 200


# -- fs events -----------------------------------------------------------------

def test_a_file_write_advances_disk_rev_without_the_scan_tick(harness, tmp_path, monkeypatch):
    pytest.importorskip("watchfiles")
    _quiet_watcher(monkeypatch)  # the 1 s scan cannot be what moved the rev
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    clone = harness.root / "alpha"
    # FSEvents needs a moment to arm: prime until the watcher demonstrably sees.
    primed = False
    for i in range(30):
        before = m.disk_rev
        (clone / "prime.md").write_text(f"p{i}\n")
        if _wait_for(lambda: m.disk_rev > before, timeout=0.3):
            primed = True
            break
    assert primed, "the fs watcher never reported a write"
    time.sleep(0.2)
    before = m.disk_rev
    (clone / "a.py").write_text("a-fs-event\n")
    assert _wait_for(lambda: m.disk_rev > before, timeout=1.0), \
        "a write did not advance disk_rev within 1 s"
    # Dot-dirs are not the clone's content.
    before = m.disk_rev
    (clone / ".hidden").mkdir(exist_ok=True)
    (clone / ".hidden" / "x.py").write_text("x\n")
    time.sleep(0.5)
    assert m.disk_rev == before


# -- sync/events socket --------------------------------------------------------

def _ws_client(harness):
    return TestClient(harness.client.app, base_url="http://127.0.0.1:1777")


def test_the_events_socket_streams_state_and_log(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    monkeypatch.setattr(canvases_mod, "SYNC_EVENTS_PING_S", 0.3)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    client = _ws_client(harness)
    with client.websocket_connect("ws://127.0.0.1:1777/api/canvases/sync/events?name=alpha",
                                  headers={"origin": "http://127.0.0.1:1777"}) as ws:
        first = ws.receive_json()
        assert first["type"] == "state"
        assert first["state"]["name"] == "alpha"
        assert first["state"]["watcher_epoch"] == m.watcher_epoch
        m.log_event("watcher", "hello socket", detail="from the test")
        got_log = got_ping = False
        state_after = None
        for _ in range(40):
            msg = ws.receive_json()
            if msg["type"] == "log" and msg["event"]["action"] == "hello socket":
                got_log = True
            elif msg["type"] == "state" and got_log is False:
                state_after = msg
            elif msg["type"] == "ping":
                got_ping = True
            if got_log and got_ping:
                break
        assert got_log, "the log event never reached the socket"
        assert got_ping, "no heartbeat"
        assert state_after is not None
        # A push-state change wakes it with a fresh state.
        m.push_state = "pending"
        m._dirty_since = time.time()
        for _ in range(40):
            msg = ws.receive_json()
            if msg["type"] == "state" and \
                    msg["state"]["legs"]["disk_to_server"]["state"] == "pending":
                break
        else:
            pytest.fail("the pending push never reached the socket")


def test_the_events_socket_refuses_a_foreign_origin(harness, tmp_path, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    client = _ws_client(harness)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("ws://127.0.0.1:1777/api/canvases/sync/events?name=alpha",
                                      headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


# -- review round: push path invariants -----------------------------------------

def _capture_push(monkeypatch, seen: dict):
    real = canvases_mod._SyncManager._run_push_cli

    def capture(self, cli, copy_dir):
        seen.setdefault("tomls", []).append(open(os.path.join(copy_dir, "canvas.toml")).read())
        seen.setdefault("files", []).append(sorted(os.listdir(copy_dir)))
        sub = os.path.join(copy_dir, "sub")
        if os.path.isdir(sub):
            seen.setdefault("sub", []).append(sorted(os.listdir(sub)))
        return real(self, cli, copy_dir)

    monkeypatch.setattr(canvases_mod._SyncManager, "_run_push_cli", capture)


def test_an_orphan_source_file_is_pushed_with_its_row_without_any_frame(
        harness, tmp_path, monkeypatch, fake_agent):
    """Invariant 2 on the push path: no frame ever cut a delta, yet the file
    Claude wrote without a [[canvas.nodes]] row ships WITH a synthesized row —
    written into the clone's canvas.toml before the freeze."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    seen: dict = {}
    _capture_push(monkeypatch, seen)
    _start(harness)
    (harness.root / "alpha" / "orphan_x.py").write_text("print(1)\n")
    res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    pushed = seen["tomls"][0]
    assert 'udfName = "orphan_x"' in pushed, pushed
    assert "orphan_x.py" in seen["files"][0]
    assert 'udfName = "orphan_x"' in (harness.root / "alpha" / "canvas.toml").read_text()
    events = harness.client.get("/api/canvases/sync/log?name=alpha").json()["events"]
    assert any(e["leg"] == "disk_to_server" and e["action"] == "rows synthesized"
               and "orphan_x" in e["detail"] for e in events), events


def test_a_manual_push_that_landed_is_ok_even_if_the_clone_moved_during_it(
        harness, tmp_path, monkeypatch, fake_agent):
    """A write during the CLI run leaves the fold "pending" for a follow-up,
    but THIS push published: the intercept must not tell the session it
    failed."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    real = canvases_mod._SyncManager._run_push_cli

    def push_with_write(self, cli, copy_dir):
        (harness.root / "alpha" / "a.py").write_text("written-during-push\n")
        return real(self, cli, copy_dir)

    monkeypatch.setattr(canvases_mod._SyncManager, "_run_push_cli", push_with_write)
    m = _start(harness)
    (harness.root / "alpha" / "a.py").write_text("a2\n")
    res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    body = res.json()
    assert res.status_code == 200, res.text
    assert body["ok"] is True, body
    assert body["push_state"] == "pending" and m._dirty_since is not None, body
    assert body["push_seq"] == 1


def test_a_failed_manual_push_is_not_ok(harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    _start(harness)
    harness.set_scenario({"pull_files": {}, "push_fail_lines": ["Error: node x has no file"]})
    res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    assert res.json()["ok"] is False and res.json()["push_state"] == "error", res.text


def test_fusedignored_files_stay_out_of_the_frozen_copy(
        harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    clone = harness.root / "alpha"
    seen: dict = {}
    _capture_push(monkeypatch, seen)
    _start(harness)
    # After start: sync/start re-seeds the app's own .fusedignore.
    with open(clone / ".fusedignore", "a") as f:
        f.write("scratch_*.csv\n")
    (clone / "scratch_big.csv").write_text("x,y\n")
    harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    files = seen["files"][0]
    assert "scratch_big.csv" not in files and "CLAUDE.md" not in files, files
    assert ".fusedignore" in files and "a.py" in files, files


def test_a_benign_delta_refusal_is_not_an_error_and_a_good_cut_clears_one(
        harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    cut = harness.client.get("/api/canvases/delta?name=alpha&full=1").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"name": "alpha", "rev": cut["rev"], "ok": False,
                              "reason": "no frame"}, headers=GUARD)
    assert m._delta_error is None
    assert _state(harness)["legs"]["disk_to_embed"]["state"] != "error"
    cut = harness.client.get("/api/canvases/delta?name=alpha&full=1").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"name": "alpha", "rev": cut["rev"], "ok": False,
                              "reason": "not hydrated yet"}, headers=GUARD)
    assert m._delta_error is None
    # Nothing but benign refusals so far: no frame has shown itself.
    assert m._frame_seen is False
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"
    # A real refusal is an error until the next good cut (the frame's hello).
    cut = harness.client.get("/api/canvases/delta?name=alpha&full=1").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"name": "alpha", "rev": cut["rev"], "ok": False,
                              "reason": "op 3: unknown node"}, headers=GUARD)
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "error"
    assert "unknown node" in _state(harness)["legs"]["disk_to_embed"]["reason"]
    harness.client.get("/api/canvases/delta?name=alpha&full=1")
    assert m._delta_error is None


# -- second review round --------------------------------------------------------

def test_the_quit_rung_terminates_a_push_in_flight(harness, tmp_path, monkeypatch):
    """shutdown() stops a push CLI rather than waiting PUSH_TIMEOUT for it (or
    leaving it to outlive the app): SIGTERM, the push folds as failed, the
    lock is released, all within a few seconds."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    monkeypatch.setattr(canvases_mod._SyncManager, "_push_argv",
                        lambda self, cli, copy_dir: [sys.executable, "-c",
                                                     "import time; time.sleep(60)"])
    monkeypatch.setattr(canvases_mod, "_QUITTING", False)  # restored after the test
    m = _start(harness)
    (harness.root / "alpha" / "a.py").write_text("a2\n")
    results = []
    pusher = threading.Thread(target=lambda: results.append(harness.client.post(
        "/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)))
    pusher.start()
    assert _wait_for(lambda: m._push_proc is not None, timeout=5), "the push CLI never started"
    proc = m._push_proc
    t0 = time.time()
    canvases_mod.shutdown()
    elapsed = time.time() - t0
    pusher.join(5)
    assert elapsed < canvases_mod.PUSH_TERMINATE_GRACE_S + 2, elapsed
    assert proc.poll() is not None, "the push CLI outlived the quit"
    assert not pusher.is_alive()
    assert results and results[0].json()["ok"] is False, results
    assert m.push_state == "error" and m.push_seq == 0
    assert not (harness.root / ".sync" / "alpha.lock").exists()


def test_nested_or_slashed_fusedignore_rules_never_drop_a_published_file(
        harness, tmp_path, monkeypatch, fake_agent):
    """A file left out of the copy is DELETED server-side; one wrongly copied
    is still filtered by the CLI. So: root-level basename matches only, and
    any rule with a "/" disables the copy-side filter entirely."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    clone = harness.root / "alpha"
    seen: dict = {}
    _capture_push(monkeypatch, seen)
    _start(harness)
    (clone / "sub").mkdir()
    (clone / "sub" / "nested.csv").write_text("n\n")
    (clone / "root.csv").write_text("r\n")
    (clone / "build").mkdir()
    (clone / "build" / "out.txt").write_text("o\n")
    with open(clone / ".fusedignore", "a") as f:
        f.write("*.csv\nbuild/\n")
    harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    copy_root = seen["files"][0]
    assert "root.csv" not in copy_root and "build" not in copy_root, copy_root
    assert "sub" in copy_root, copy_root
    assert seen["sub"][0] == ["nested.csv"], seen["sub"]

    # A rule with a "/" in it: the copy keeps everything, the CLI decides.
    with open(clone / ".fusedignore", "a") as f:
        f.write("src/*.py\n")
    (clone / "a.py").write_text("a3\n")
    harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    copy_root = seen["files"][1]
    assert "root.csv" in copy_root and "build" in copy_root, copy_root


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_a_failed_lock_write_closes_the_locked_fd(tmp_path, monkeypatch):
    from fused_render import canvas_sync_state as css

    path = str(tmp_path / ".sync" / "alpha.lock")

    def full(fd, record):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(css, "_write_fd", full)
    with pytest.raises(OSError):
        css.claim_lock(path)
    assert path not in css._HELD
    # The flock went with the fd: nobody holds it now.
    assert css.lock_holder(path) is None
    monkeypatch.undo()
    outcome, record = css.claim_lock(path)
    assert outcome in ("taken", "taken_over") and path in css._HELD
    assert css.release_lock(path, css._HELD[path][1])


def test_read_lock_retries_a_record_caught_mid_write(tmp_path):
    from fused_render import canvas_sync_state as css

    path = tmp_path / "alpha.lock"
    path.write_text("")
    timer = threading.Timer(0.005, lambda: path.write_text(
        json.dumps({"pid": 4321, "port": 7, "since": 1.0})))
    timer.start()
    try:
        assert css.read_lock(str(path)) == {"pid": 4321, "port": 7, "since": 1.0}
    finally:
        timer.join()
    path.write_text("{not json")
    assert css.read_lock(str(path)) is None
    assert css.read_lock(str(tmp_path / "missing.lock")) is None


# -- rowless files: the embed's new file, its node still in flight -------------

ROWLESS_BODY = {
    "dashboard_body": {"studioState": {"nodes": []}},
    "udfs": [{"slug": "fresh", "file": "fresh.py", "file_body": "print(2)\n"}],
}
FRESH_NODE_BODY = {
    "dashboard_body": {"studioState": {"nodes": [
        {"id": "n-fresh", "type": "simple-udf", "position": {"x": 900, "y": 10},
         "width": 300, "height": 200, "data": {"udfName": "fresh"}}]}},
    "udfs": [{"slug": "fresh", "file": "fresh.py", "file_body": "print(2)\n"}],
}


def _snapshot(harness, seq, body):
    res = harness.client.post("/api/canvases/snapshot",
                              json={"name": "alpha", "gen": 1, "seq": seq, "body": body},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    return res.json()


def _toml(harness):
    return (harness.root / "alpha" / "canvas.toml").read_text()


def _actions(harness):
    return [e["action"] for e in harness.client.get(
        "/api/canvases/sync/log?name=alpha").json()["events"]]


def test_a_rowless_file_within_its_grace_gets_no_row_no_op_and_no_push(
        harness, tmp_path, monkeypatch, fake_agent):
    """The embed created fresh.py without its node (the stale-closure race):
    inventing a row now would push a made-up position into the frame and the
    node jumps. Within ROWLESS_GRACE_S nothing is synthesized, the delta says
    nothing about it, and the push is held (the CLI only warns on a row-less
    file, so it would publish the UDF node-less)."""
    _quiet_watcher(monkeypatch)
    monkeypatch.setattr(canvases_mod, "ROWLESS_GRACE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _snapshot(harness, 1, ROWLESS_BODY)
    assert (harness.root / "alpha" / "fresh.py").read_text() == "print(2)\n"
    assert "fresh" in m._rowless
    cut = harness.client.get("/api/canvases/delta?name=alpha&full=1").json()
    assert "ops" in cut, cut
    assert "fresh" not in json.dumps(cut["ops"]), cut["ops"]
    assert 'udfName = "fresh"' not in _toml(harness)
    res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 409 and res.json()["code"] == "busy", res.text
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
    m.push_state = "pending"
    m._dirty_since = time.time()
    assert _state(harness)["legs"]["disk_to_server"]["reason"] == \
        "waiting for the workbench's row for fresh"
    actions = _actions(harness)
    assert "row deferred" in actions and "push held" in actions, actions


def test_a_rowless_file_past_its_grace_is_synthesized_and_pushed(
        harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    monkeypatch.setattr(canvases_mod, "ROWLESS_GRACE_S", 0.3)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    seen: dict = {}
    _capture_push(monkeypatch, seen)
    m = _start(harness)
    _snapshot(harness, 1, ROWLESS_BODY)
    rev = m.disk_rev
    time.sleep(0.4)
    cut = harness.client.get("/api/canvases/delta?name=alpha&full=1").json()
    assert m.disk_rev > rev, "the grace's end never woke the delta leg"
    assert "fresh" in json.dumps(cut["ops"]), cut["ops"]
    assert 'udfName = "fresh"' in _toml(harness)
    assert m._rowless == {}
    res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    assert 'udfName = "fresh"' in seen["tomls"][0]
    actions = _actions(harness)
    assert "row grace elapsed" in actions and "rows synthesized" in actions, actions


def test_the_grace_ends_on_the_watcher_tick_without_any_cut(
        harness, tmp_path, monkeypatch, fake_agent):
    """Nothing on disk moves when the grace ends, so the tick forces the
    disk_rev bump that makes the page cut again."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 30.0)
    monkeypatch.setattr(canvases_mod, "ROWLESS_GRACE_S", 0.8)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _snapshot(harness, 1, ROWLESS_BODY)
    time.sleep(0.3)  # the snapshot's own writes settle into disk_rev
    rev = m.disk_rev
    assert "fresh" in m._rowless
    assert _wait_for(lambda: "fresh" not in m._rowless, timeout=3)
    assert m.disk_rev > rev, "the grace ended without waking the delta leg"
    assert "row grace elapsed" in _actions(harness)


def test_a_snapshot_naming_the_node_settles_the_rowless_file(
        harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    monkeypatch.setattr(canvases_mod, "ROWLESS_GRACE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _snapshot(harness, 1, ROWLESS_BODY)
    assert "fresh" in m._rowless
    _snapshot(harness, 2, FRESH_NODE_BODY)
    assert m._rowless == {}
    toml = _toml(harness)
    # The workbench's own row, at the workbench's position — nothing invented.
    assert 'udfName = "fresh"' in toml and "x = 900" in toml, toml
    res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text


def test_a_row_written_to_disk_directly_settles_the_rowless_file(
        harness, tmp_path, monkeypatch, fake_agent):
    """Claude (or a rename) writing fresh's row into canvas.toml ends the grace
    as surely as a snapshot naming the node: the push is no longer held."""
    _quiet_watcher(monkeypatch)
    monkeypatch.setattr(canvases_mod, "ROWLESS_GRACE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _snapshot(harness, 1, ROWLESS_BODY)
    assert "fresh" in m._rowless
    path = os.path.join(m.dir, "canvas.toml")
    doc = canvas_snapshot._load_toml(path)
    canvas = doc.setdefault("canvas", {})
    canvas.setdefault("nodes", []).append(
        {"udfName": "fresh", "x": 5, "y": 6, "width": 300, "height": 200, "title": "fresh"})
    with open(path, "w", encoding="utf-8") as f:
        f.write(canvas_snapshot._dump_toml(doc))
    assert m._rowless_in_grace() == set()
    assert m._rowless == {}
    res = harness.client.post("/api/canvases/sync/push", json={"name": "alpha"}, headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
