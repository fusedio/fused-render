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
from test_canvas_sync_loops import PROBE_NOTE, PROBE_NOTE_TIPTAP, _rows
from test_canvases import (  # noqa: F401 — fixtures are used by name
    ENV,
    GUARD,
    _BASE_FILES,
    _FakeAgent,
    _cloned_shim_harness,
    _manager,
    _pull_when_stale,
    _reset_module_state,
    _stop_sync,
    _sync_view,
    _wait_for,
    _wait_sync,
    fake_agent,
    harness,
)

EMPTY_SNAPSHOT = {"dashboard_body": {"studioState": {"nodes": []}}, "udfs": []}


def _start(harness):
    res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
        json={"env": ENV, "name": "alpha", "gen": 1, "seq": 1, "body": EMPTY_SNAPSHOT}, headers=GUARD)
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
        "/api/canvases/sync/log?env=unstable&name=alpha").json()["events"]]
    assert "push start" in actions and "push end" in actions and "snapshot applied" in actions


@pytest.mark.parametrize("write_while_waiting, pushes", [(False, 1), (True, 2)])
def test_the_manual_push_waits_its_turn_behind_a_running_push(
        harness, tmp_path, monkeypatch, fake_agent, write_while_waiting, pushes):
    _quiet_watcher(monkeypatch)
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
    answers: list = []

    def push():
        answers.append(harness.client.post("/api/canvases/sync/push",
                                           json={"env": ENV, "name": "alpha"}, headers=GUARD))

    first = threading.Thread(target=push)
    first.start()
    assert started.wait(5)
    if write_while_waiting:
        (harness.root / "alpha" / "b.py").write_text("b2\n")
    second = threading.Thread(target=push)
    second.start()
    second.join(0.3)
    assert second.is_alive(), [a.json() for a in answers]
    release.set()
    first.join(10)
    second.join(10)
    assert [(a.status_code, a.json()["ok"]) for a in answers] == [(200, True), (200, True)], \
        [a.text for a in answers]
    assert len([c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]) == pushes


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
        res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"},
                                  headers=GUARD)
        assert res.status_code == 409, res.text
        body = res.json()
        assert body["code"] == "owned"
        assert body["owner"] == {"pid": other.pid, "port": 4242, "since": 1.5}
        assert (ENV, "alpha") not in canvases_mod._syncs
        state = harness.client.get("/api/canvases/sync/state?env=unstable&name=alpha").json()
        assert state["watching"] is False and state["watcher_epoch"] is None
        assert state["owner"] == {"pid": other.pid, "port": 4242}
        assert state["other_watchers"] == [{"pid": other.pid, "port": 4242}]
        # A session's push through the intercept learns WHO owns it.
        push = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"},
                                   headers=GUARD)
        assert push.status_code == 409 and push.json()["code"] == "owned", push.text
        assert push.json()["owner"]["pid"] == other.pid
        # Untouched by the refusal.
        assert json.loads(lock.read_text())["pid"] == other.pid
    finally:
        other.kill()
        other.wait()

    # The holder died: the kernel dropped its flock, the file still names it.
    res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert res.status_code == 200, res.text
    held = json.loads(lock.read_text())
    assert held["pid"] == os.getpid()
    events = harness.client.get("/api/canvases/sync/log?env=unstable&name=alpha").json()["events"]
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
    res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
    res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
    res = harness.client.get("/api/canvases/sync/state?env=unstable&name=alpha")
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
    s = harness.client.get("/api/canvases/sync/state?env=unstable&name=beta").json()
    assert s["watching"] is False and s["disk_rev"] == 0 and s["owner"] is None
    assert all(v["state"] == "idle" for v in s["legs"].values())
    assert harness.client.get("/api/canvases/sync/log?env=unstable&name=beta").json() == {
        "events": [], "next": 0}
    assert harness.client.get("/api/canvases/sync/state?env=unstable&name=../x").status_code == 400


def test_a_delta_cut_and_ack_show_on_the_disk_to_embed_leg(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    assert "ops" in cut, cut
    # A cut is no evidence of a frame (the page cuts and drops with none).
    assert m._frame_seen is False
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"
    ack = harness.client.post("/api/canvases/delta/ack",
                              json={"env": ENV, "name": "alpha", "rev": cut["rev"]}, headers=GUARD)
    assert ack.status_code == 200 and ack.json()["ok"], ack.text
    assert m._frame_seen is True and m._view_rev == cut["rev"]
    # The cut persisted synthesized rows (a.py, b.py have none) after its rev
    # was taken: one more cut + ack brings the frame level.
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"env": ENV, "name": "alpha", "rev": cut["rev"]}, headers=GUARD)
    m._view_rev = m.disk_rev
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"
    # Disk moves, the frame has not acked it: pending.
    (harness.root / "alpha" / "a.py").write_text("a-moved\n")
    m._note_disk()
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "pending"
    actions = [e["action"] for e in harness.client.get(
        "/api/canvases/sync/log?env=unstable&name=alpha").json()["events"]]
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
    harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1")
    assert calls == [(m.dir, ["orphan"], True)]
    events = harness.client.get("/api/canvases/sync/log?env=unstable&name=alpha").json()["events"]
    assert any(e["action"] == "rows synthesized" and "orphan" in e["detail"] for e in events)


# -- sync/log ------------------------------------------------------------------

def test_the_log_ring_keeps_200_and_pages_by_seq(harness, tmp_path, monkeypatch, caplog):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    caplog.set_level("INFO", logger="fused_render.canvases")
    for i in range(250):
        m.log_event("watcher", "tick", ms=i, detail=f"n{i}")
    assert "canvas unstable/alpha: watcher tick n249 (249 ms)" in caplog.text

    page = harness.client.get("/api/canvases/sync/log?env=unstable&name=alpha&after=0").json()
    events = page["events"]
    assert len(events) == 200
    last = m._log.last_seq
    assert events[-1]["seq"] == last and events[0]["seq"] == last - 199
    assert page["next"] == last
    assert set(events[-1]) == {"seq", "t", "leg", "action", "ms", "ok", "detail"}
    assert events[-1]["detail"] == "n249" and events[-1]["ms"] == 249

    page2 = harness.client.get(f"/api/canvases/sync/log?env=unstable&name=alpha&after={last - 3}").json()
    assert [e["detail"] for e in page2["events"]] == ["n247", "n248", "n249"]
    empty = harness.client.get(f"/api/canvases/sync/log?env=unstable&name=alpha&after={last}").json()
    assert empty == {"events": [], "next": last}
    # An `after` from a previous watcher (beyond this ring) reads as 0.
    reset = harness.client.get(f"/api/canvases/sync/log?env=unstable&name=alpha&after={last + 500}").json()
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
    with client.websocket_connect("ws://127.0.0.1:1777/api/canvases/sync/events?env=unstable&name=alpha",
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
        with client.websocket_connect("ws://127.0.0.1:1777/api/canvases/sync/events?env=unstable&name=alpha",
                                      headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert exc.value.code == 1008


@pytest.mark.parametrize("query", ["name=alpha", "name=alpha&env=staging"])
def test_the_events_socket_refuses_a_missing_or_unknown_env(harness, query):
    """A canvas is (env, name): no env, no channel. The socket has no body for
    the 400's reason, so it closes 1008 (sync/state carries the reason)."""
    from starlette.websockets import WebSocketDisconnect

    client = _ws_client(harness)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"ws://127.0.0.1:1777/api/canvases/sync/events?{query}",
                                      headers={"origin": "http://127.0.0.1:1777"}) as ws:
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
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    pushed = seen["tomls"][0]
    assert 'udfName = "orphan_x"' in pushed, pushed
    assert "orphan_x.py" in seen["files"][0]
    assert 'udfName = "orphan_x"' in (harness.root / "alpha" / "canvas.toml").read_text()
    events = harness.client.get("/api/canvases/sync/log?env=unstable&name=alpha").json()["events"]
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
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
    harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    files = seen["files"][0]
    assert "scratch_big.csv" not in files and "CLAUDE.md" not in files, files
    assert ".fusedignore" in files and "a.py" in files, files


def test_a_benign_delta_refusal_is_not_an_error_and_a_good_cut_clears_one(
        harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"env": ENV, "name": "alpha", "rev": cut["rev"], "ok": False,
                              "reason": "no frame"}, headers=GUARD)
    assert m._delta_error is None
    assert _state(harness)["legs"]["disk_to_embed"]["state"] != "error"
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"env": ENV, "name": "alpha", "rev": cut["rev"], "ok": False,
                              "reason": "not hydrated yet"}, headers=GUARD)
    assert m._delta_error is None
    # Nothing but benign refusals so far: no frame has shown itself.
    assert m._frame_seen is False
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "idle"
    # A real refusal is an error until the next good cut (the frame's hello).
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    harness.client.post("/api/canvases/delta/ack",
                        json={"env": ENV, "name": "alpha", "rev": cut["rev"], "ok": False,
                              "reason": "op 3: unknown node"}, headers=GUARD)
    assert _state(harness)["legs"]["disk_to_embed"]["state"] == "error"
    assert "unknown node" in _state(harness)["legs"]["disk_to_embed"]["reason"]
    harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1")
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
        "/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)))
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
    harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    copy_root = seen["files"][0]
    assert "root.csv" not in copy_root and "build" not in copy_root, copy_root
    assert "sub" in copy_root, copy_root
    assert seen["sub"][0] == ["nested.csv"], seen["sub"]

    # A rule with a "/" in it: the copy keeps everything, the CLI decides.
    with open(clone / ".fusedignore", "a") as f:
        f.write("src/*.py\n")
    (clone / "a.py").write_text("a3\n")
    harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
                              json={"env": ENV, "name": "alpha", "gen": 1, "seq": seq, "body": body},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    return res.json()


def _toml(harness):
    return (harness.root / "alpha" / "canvas.toml").read_text()


def _actions(harness):
    return [e["action"] for e in harness.client.get(
        "/api/canvases/sync/log?env=unstable&name=alpha").json()["events"]]


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
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    assert "ops" in cut, cut
    assert "fresh" not in json.dumps(cut["ops"]), cut["ops"]
    assert 'udfName = "fresh"' not in _toml(harness)
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    assert m.disk_rev > rev, "the grace's end never woke the delta leg"
    assert "fresh" in json.dumps(cut["ops"]), cut["ops"]
    assert 'udfName = "fresh"' in _toml(harness)
    assert m._rowless == {}
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
    # Wait for the bump itself, not just the expiry: `_rowless_in_grace`
    # drops the entry under its lock, then logs and `_force_rev`s (which
    # fingerprints the clone) — a loaded runner read disk_rev in that gap.
    assert _wait_for(lambda: m.disk_rev > rev, timeout=3), (
        "the grace ended without waking the delta leg")
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
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
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
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text


# -- third review round -------------------------------------------------------------

def test_a_rename_during_the_grace_keeps_the_grace(
        harness, tmp_path, monkeypatch, fake_agent):
    """Rename a rowless file before its row arrives: the grace follows the new
    name (same start), so no row is invented for it until the grace ends."""
    _quiet_watcher(monkeypatch)
    monkeypatch.setattr(canvases_mod, "ROWLESS_GRACE_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    _snapshot(harness, 1, ROWLESS_BODY)
    started = m._rowless["fresh"]
    res = harness.client.post("/api/canvases/snapshot", json={
        "env": ENV, "name": "alpha", "gen": 1, "seq": 2,
        "rename": {"from": "fresh", "to": "fresh2",
                   "file_from": "fresh.py", "file_to": "fresh2.py"}}, headers=GUARD)
    assert res.status_code == 200, res.text
    assert m._rowless == {"fresh2": started}
    assert (harness.root / "alpha" / "fresh2.py").exists()
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    assert "fresh2" not in json.dumps(cut["ops"]), cut["ops"]
    assert 'udfName = "fresh2"' not in _toml(harness)
    # The grace ends (its start is the original file's): now it is synthesized.
    monkeypatch.setattr(canvases_mod, "ROWLESS_GRACE_S", 0.0)
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    assert "fresh2" in json.dumps(cut["ops"]), cut["ops"]
    assert 'udfName = "fresh2"' in _toml(harness)


def test_a_write_after_the_pull_recheck_is_not_baselined_as_synced(
        harness, tmp_path, monkeypatch, fake_agent):
    """Window A of the clean force pull: a file written after the `--dry-run`
    recheck read the folder (so the recheck says up to date) but before the
    re-baseline used to become part of the sync point — an edit the push would
    never ship. The pull now compares the disk against the moment `--force`
    landed and keeps such a write as a local edit."""
    _quiet_watcher(monkeypatch)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    m._remote = {"id": "c1", "last_updated": "t1", "udfs": {}}
    pulled = {**_BASE_FILES, "remote_udf.py": "print('x')\n"}
    harness.set_scenario({"pull_files": pulled})
    shims.set_manifest("t2")
    m.server_stale = {"since": time.time(), "udfs": [], "layout": True}

    real_run = subprocess.run

    def run(args, *a, **kw):
        out = real_run(args, *a, **kw)
        if "--dry-run" in args and "pull" in args:
            # Lands after the recheck read the folder.
            (harness.root / "alpha" / "a.py").write_text("written-after-recheck\n")
        return out

    monkeypatch.setattr(canvases_mod.subprocess, "run", run)
    res = harness.client.post("/api/canvases/sync/pull", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    monkeypatch.setattr(canvases_mod.subprocess, "run", real_run)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    assert (harness.root / "alpha" / "a.py").read_text() == "written-after-recheck\n"
    assert m._dirty_since is not None and m.push_state == "pending", m.push_state
    # Not part of the sync point: the next merge must read a.py as a local edit.
    assert (m._base_files or {}).get("a.py") != canvases_mod.hashlib.md5(
        b"written-after-recheck\n").hexdigest()
    assert "pull raced a local write" in _actions(harness)


def test_a_wholesale_pull_waits_for_a_live_claude_session(
        harness, tmp_path, monkeypatch, fake_agent):
    """The CLI `--force` can overwrite a file the session writes mid-pull before
    any check sees it, so a clean (wholesale) pull is deferred while a session
    is live: ok False, code agent_live, the text in `pull_error`, nothing
    written, the canvas still server-changed (not a red pull error)."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 0.1)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    shims = _cloned_shim_harness(harness, tmp_path, monkeypatch)
    _start(harness)
    assert _wait_for(lambda: (_manager()._remote or {}).get("last_updated") == "t1")
    remote = {**_BASE_FILES, "remote_udf.py": "print('x')\n"}
    harness.set_scenario({"pull_files": remote})
    shims.set_remote_files(remote)  # a stamp move over unchanged files is adopted
    shims.set_manifest("t2")
    fake_agent.live = "run-abc"

    body = _pull_when_stale(harness)
    assert body["ok"] is False and body["code"] == "agent_live", body
    assert body["pull_error"] == ("a Claude session is editing this folder; "
                                  "pull when it finishes"), body
    forces = [c for c in harness.calls()
              if c[:3] == ["workbench", "canvas", "pull"] and "--force" in c]
    assert len(forces) == 1, forces  # the clone's only
    assert not (harness.root / "alpha" / "remote_udf.py").exists()
    s2d = _state(harness)["legs"]["server_to_disk"]
    assert s2d["state"] == "stale", s2d
    assert "pull deferred" in _actions(harness)

    fake_agent.live = ""
    res = harness.client.post("/api/canvases/sync/pull", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert res.json()["ok"] is True, res.text
    assert (harness.root / "alpha" / "remote_udf.py").exists()


def test_the_manual_push_refuses_over_a_server_change(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    m.server_stale = {"since": time.time(),
                      "udfs": [{"slug": "a", "kind": "changed"}], "layout": False}
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"}, headers=GUARD)
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "server_changed", body
    assert body["server_stale"]["udfs"] == [{"slug": "a", "kind": "changed"}]
    assert "a (changed)" in body["error"] and "pull first" in body["error"], body
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]


def test_a_rebaseline_during_a_scan_does_not_mark_the_clone_pending(
        harness, tmp_path, monkeypatch, fake_agent):
    """A Pull (request thread) re-baselines `_fingerprint` while the watcher is
    mid-walk: that walk's reading predates the new baseline and must be
    discarded (`_fp_gen`), not compared — or the pull's own writes read as a
    local edit and the clone goes "pending"."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    monkeypatch.setattr(canvases_mod, "DEBOUNCE_S", 30.0)
    monkeypatch.setattr(canvases_mod, "PULL_POLL_S", 30.0)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    real = canvases_mod._SyncManager._take_fingerprint
    armed, fired = threading.Event(), threading.Event()

    def take(self):
        if armed.is_set() and threading.current_thread() is self.thread:
            armed.clear()
            # The reading from before the pull (a file it deleted is still
            # there), and the pull's re-baseline landing mid-walk.
            stale = {**real(self), "ghost.py": (1.0, 1)}
            self._fingerprint = real(self)
            fired.set()
            return stale
        return real(self)

    monkeypatch.setattr(canvases_mod._SyncManager, "_take_fingerprint", take)
    m = _start(harness)
    time.sleep(0.2)
    assert m._dirty_since is None and m.push_state == "idle"
    armed.set()
    assert fired.wait(3), "the scan never ran"
    time.sleep(0.3)  # several more ticks on the real disk
    assert m._dirty_since is None, "a stale mid-walk reading marked the clone dirty"
    assert m.push_state == "idle"


def test_the_watcher_tick_keeps_the_live_run_cache_sync_state_reads(
        harness, tmp_path, monkeypatch):
    """Only the watcher scans RUNS for a live session (`agent_run_id()` every
    tick, clean clone or not); sync/state reads the cached answer — the pending
    push's reason — and never pays for a scan itself."""
    monkeypatch.setattr(canvases_mod, "SCAN_INTERVAL_S", 0.05)
    agent = _FakeAgent(live="run-xyz")
    monkeypatch.setattr(canvases_mod, "_agent_module", lambda: agent)
    monkeypatch.setattr(canvases_mod, "AGENT_LIVE_CACHE_S", 0.3)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    # Clean clone, nothing to debounce: the watcher still refreshes the cache.
    assert _wait_for(lambda: len(agent.calls) >= 2, timeout=3), agent.calls
    m.push_state = "pending"
    m._dirty_since = time.time()
    before = len(agent.calls)
    for _ in range(5):
        leg = _state(harness)["legs"]["disk_to_server"]
        assert leg["reason"] == "waiting for the Claude session in this folder to finish", leg
    assert len(agent.calls) - before <= 1, (
        "sync/state paid for the RUNS scan instead of reading the watcher's cache",
        agent.calls)


# -- hand-over: sync/release + sync/takeover -----------------------------------

def test_release_stops_this_processes_watcher_and_drops_the_lock(harness, tmp_path, monkeypatch):
    """A peer's sync/takeover asks us to release: the watcher stops, the lock
    file goes, and the page here reads "no watcher" on its next state."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    m = _start(harness)
    lock = harness.root / ".sync" / "alpha.lock"
    assert lock.exists()
    res = harness.client.post("/api/canvases/sync/release", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    assert res.json()["released"] is True
    assert m.stop_event.is_set() and not m.thread.is_alive()
    assert not lock.exists()
    assert (ENV, "alpha") not in canvases_mod._syncs
    assert (ENV, "alpha") not in canvases_mod._HANDING_OVER
    state = harness.client.get("/api/canvases/sync/state?env=unstable&name=alpha").json()
    assert state["watching"] is False and state["owner"] is None
    # Nothing to give: no watcher ran here.
    again = harness.client.post("/api/canvases/sync/release", json={"env": ENV, "name": "alpha"},
                                headers=GUARD)
    assert again.status_code == 200 and again.json()["released"] is False
    # Guarded like every write.
    assert harness.client.post("/api/canvases/sync/release",
                               json={"env": ENV, "name": "alpha"}).status_code == 403


class _Running:
    def poll(self):
        return None


def test_release_waits_for_a_publish_then_refuses_if_it_outlasts_the_wait(
        harness, tmp_path, monkeypatch):
    """Dropping the lock under a running push CLI would let the taker push
    over it — the two-writer race the lock prevents. The release waits for
    the push (a dirty clone pushes seconds after it opens, right when a
    second page asks); past RELEASE_PUSH_WAIT_S it is `busy`, lock kept."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    monkeypatch.setattr(canvases_mod, "RELEASE_PUSH_WAIT_S", 0.6)
    m = _start(harness)
    lock = harness.root / ".sync" / "alpha.lock"
    m._push_proc = _Running()
    try:
        t0 = time.monotonic()
        res = harness.client.post("/api/canvases/sync/release", json={"env": ENV, "name": "alpha"},
                                  headers=GUARD)
        assert res.status_code == 409 and res.json()["code"] == "busy", res.text
        assert time.monotonic() - t0 >= 0.5, "did not wait for the push"
        assert lock.exists() and (ENV, "alpha") in canvases_mod._syncs
    finally:
        m._push_proc = None
        _stop_sync("alpha")


def test_release_waits_for_a_pull_too(harness, tmp_path, monkeypatch):
    """A pull writes the clone as much as a push: not a lock to drop under."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    monkeypatch.setattr(canvases_mod, "RELEASE_PUSH_WAIT_S", 0.5)
    m = _start(harness)
    lock = harness.root / ".sync" / "alpha.lock"
    m._pull_in_flight = True
    try:
        res = harness.client.post("/api/canvases/sync/release", json={"env": ENV, "name": "alpha"},
                                  headers=GUARD)
        assert res.status_code == 409 and res.json()["code"] == "busy", res.text
        assert lock.exists() and (ENV, "alpha") in canvases_mod._syncs
        assert (ENV, "alpha") not in canvases_mod._HANDING_OVER
    finally:
        m._pull_in_flight = False
        _stop_sync("alpha")


def test_own_sync_start_is_held_off_while_handing_over(harness, tmp_path, monkeypatch):
    """The owner's page self-heals through sync/start the moment its watcher
    goes; under the gate it gets `busy` (it retries), never a new watcher on
    the still-held flock."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    key = (ENV, "alpha")
    with canvases_mod._SYNC_LOCK:
        canvases_mod._HANDING_OVER.add(key)
    try:
        res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"},
                                  headers=GUARD)
        assert res.status_code == 409 and res.json()["code"] == "busy", res.text
        assert "handed over" in res.json()["error"]
        assert key not in canvases_mod._syncs
    finally:
        with canvases_mod._SYNC_LOCK:
            canvases_mod._HANDING_OVER.discard(key)
    res = harness.client.post("/api/canvases/sync/start", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    _stop_sync("alpha")


def test_release_goes_through_once_the_publish_ends(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    monkeypatch.setattr(canvases_mod, "RELEASE_PUSH_WAIT_S", 5.0)
    m = _start(harness)
    lock = harness.root / ".sync" / "alpha.lock"
    m._push_proc = _Running()
    threading.Timer(0.5, lambda: setattr(m, "_push_proc", None)).start()
    t0 = time.monotonic()
    res = harness.client.post("/api/canvases/sync/release", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200 and res.json()["released"] is True, res.text
    assert 0.4 <= time.monotonic() - t0 < 4.0
    assert not lock.exists() and (ENV, "alpha") not in canvases_mod._syncs


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_takeover_asks_the_owner_to_release_then_starts_here(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    lock = harness.root / ".sync" / "alpha.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    other = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)],
                             stdout=subprocess.PIPE, text=True)
    asked: list[tuple[int, str, str]] = []

    def fake_release(port, env, name):
        asked.append((port, env, name))
        # The owner honours the request: its watcher stops and its lock goes
        # (here: the holder process ends, which drops its flock).
        other.kill()
        other.wait()
        return 200, {"released": True, "name": name, "env": env}

    monkeypatch.setattr(canvases_mod, "_ask_peer_release", fake_release)
    try:
        assert other.stdout.readline().strip() == "ready"
        res = harness.client.post("/api/canvases/sync/takeover", json={"env": ENV, "name": "alpha"},
                                  headers=GUARD)
        assert res.status_code == 200, res.text
        assert res.json()["name"] == "alpha" and res.json()["dir"]
        assert asked == [(4242, ENV, "alpha")]
        assert json.loads(lock.read_text())["pid"] == os.getpid()
        assert (ENV, "alpha") in canvases_mod._syncs
    finally:
        if other.poll() is None:
            other.kill()
            other.wait()
        _stop_sync("alpha")


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX")
def test_takeover_from_an_owner_that_cannot_hand_over_keeps_its_lock(harness, tmp_path, monkeypatch):
    """An owner without the release route (an older build) answers 404; one
    mid-publish answers busy. Both come back as the page's `owned` 409 with
    the reason, and the owner's lock is untouched."""
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    lock = harness.root / ".sync" / "alpha.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    other = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)],
                             stdout=subprocess.PIPE, text=True)
    answers = {}
    monkeypatch.setattr(canvases_mod, "_ask_peer_release", lambda port, env, name: answers["next"])
    try:
        assert other.stdout.readline().strip() == "ready"
        for answer, code, needle in (
                ((404, {}), "owner_too_old", "older Fused"),
                ((409, {"code": "busy", "error": "canvas is publishing"}), "busy", "publishing"),
                ((None, {}), "owned", "did not answer"),
        ):
            answers["next"] = answer
            res = harness.client.post("/api/canvases/sync/takeover",
                                      json={"env": ENV, "name": "alpha"}, headers=GUARD)
            assert res.status_code == 409, res.text
            body = res.json()
            assert body["code"] == code and needle in body["error"], body
            assert body["owner"]["pid"] == other.pid and body["owner"]["port"] == 4242
            assert (ENV, "alpha") not in canvases_mod._syncs
            assert json.loads(lock.read_text())["pid"] == other.pid
    finally:
        other.kill()
        other.wait()


def test_takeover_of_a_free_clone_is_a_plain_start(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    monkeypatch.setattr(canvases_mod, "_ask_peer_release",
                        lambda *a: pytest.fail("nobody to ask"))
    res = harness.client.post("/api/canvases/sync/takeover", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    assert (ENV, "alpha") in canvases_mod._syncs
    _stop_sync("alpha")


def _post_note_body(harness, text, base):
    return harness.client.post("/api/canvases/body", json={
        "env": ENV, "name": "alpha", "node": "probe", "file": "probe.md",
        "base_hash": canvas_snapshot.body_hash(base), "text": text}, headers=GUARD)


def _trashed(harness):
    root = harness.root / ".sync" / "trash" / "alpha"
    return sorted(p.read_text() for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_a_note_body_is_written_only_when_its_text_changed(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    _start(harness)
    note = harness.root / "alpha" / "probe.md"
    note.write_text(PROBE_NOTE)
    res = _post_note_body(harness, PROBE_NOTE_TIPTAP, PROBE_NOTE)
    assert res.status_code == 200, res.text
    assert res.json() == {"ok": True, "node": "probe", "file": "probe.md", "changed": False,
                          "created": False, "hash": canvas_snapshot.body_hash(PROBE_NOTE)}
    assert note.read_text() == PROBE_NOTE and _trashed(harness) == []
    edited = PROBE_NOTE_TIPTAP.replace("About", "Roughly")
    res = _post_note_body(harness, edited, PROBE_NOTE)
    assert res.status_code == 200 and res.json()["changed"] is True, res.text
    assert note.read_text() == edited and _trashed(harness) == [PROBE_NOTE]


def test_a_new_file_gets_no_row_while_a_session_holds_the_push(
        harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    fake_agent.live = "run-held"
    _start(harness).agent_run_id()
    before = _toml(harness)
    (harness.root / "alpha" / "foo.md").write_text("# foo\n")
    cut = harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1").json()
    assert "ops" in cut, cut
    assert not [op for op in cut["ops"] if op.get("name") in ("a", "b", "foo")], cut["ops"]
    assert _toml(harness) == before
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    assert sorted(r["udfName"] for r in _rows(harness.root / "alpha")) == ["a", "b", "foo"]


def test_a_row_the_session_adds_inside_a_folder_is_the_only_row_the_push_ships(
        harness, tmp_path, monkeypatch, fake_agent):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    seen: dict = {}
    _capture_push(monkeypatch, seen)
    fake_agent.live = "run-held"
    _start(harness).agent_run_id()
    (harness.root / "alpha" / "foo.md").write_text("# foo\n")
    harness.client.get("/api/canvases/delta?env=unstable&name=alpha&full=1")
    with open(harness.root / "alpha" / "canvas.toml", "a") as fh:
        fh.write('\n[[canvas.nodes]]\nudfName = "Docs"\nfolderName = "Docs"\ntype = "udf-folder"\n'
                 'x = 0\ny = 0\nchildUdfOrder = ["foo"]\n\n'
                 '[[canvas.nodes]]\nudfName = "foo"\nx = 12\ny = 34\n')
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200 and res.json()["ok"] is True, res.text
    foo = [r for r in _rows(harness.root / "alpha") if r["udfName"] == "foo"]
    assert len(foo) == 1 and (foo[0]["x"], foo[0]["y"]) == (12, 34), foo
    assert seen["tomls"][0].count('udfName = "foo"') == 1, seen["tomls"][0]


def test_two_rows_for_one_node_fail_the_push_before_the_cli_runs(harness, tmp_path, monkeypatch):
    _quiet_watcher(monkeypatch)
    _cloned_shim_harness(harness, tmp_path, monkeypatch)
    _start(harness)
    (harness.root / "alpha" / "canvas.toml").write_text(
        'type = "canvas"\n\n[[canvas.nodes]]\nudfName = "a"\n\n[[canvas.nodes]]\nudfName = "b"\n\n'
        '[[canvas.nodes]]\nudfName = "a"\nx = 5\n')
    res = harness.client.post("/api/canvases/sync/push", json={"env": ENV, "name": "alpha"},
                              headers=GUARD)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False and body["push_state"] == "error", body
    assert any("'a'" in line for line in body["error_detail"]), body
    assert not any("'b'" in line for line in body["error_detail"]), body
    assert not [c for c in harness.calls() if c[:3] == ["workbench", "canvas", "push"]]
