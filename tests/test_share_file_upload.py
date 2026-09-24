"""The detached upload's state machine (share_file.py's read_upload_state /
start_upload / cancel_upload) — three marker files under
<STATE_DIR>/share_uploads/<id>/: log, pid, done. `done` is authoritative; no
`done` and a dead pid means the worker was killed outright, so the poll ends
`cancelled` rather than spinning (SPEC artifact-fileudf.md §4.1).

Drives the marker files directly, as the plan calls for — no real subprocess
spawn, no SDK, no network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import fused_render.share_app as share_app_mod
import fused_render.share_file as share_file_mod
from fused_render.server import create_app

GUARD = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))


def job_dir(upload_id):
    return share_file_mod._upload_paths(upload_id)["dir"]


def _record_cancel_signal(monkeypatch):
    """Record the pid `cancel_upload` signals, on whichever road THIS platform
    takes.

    `cancel_upload` is the same platform split `engine_host._kill_tree` uses:
    POSIX signals the process group (`os.killpg`/`os.getpgid`, since
    `_spawn_upload`'s `start_new_session=True` makes the pid the pgid), while
    Windows has neither `os.killpg` nor `os.getpgid` at all — not just
    unsupported, the attributes are absent from the frozen `os` module — and
    falls to `os.kill(pid, CTRL_BREAK_EVENT)`, then `taskkill /T /F` when that
    raises (a synthetic pid like the ones these tests seed always makes
    CTRL_BREAK_EVENT raise, so the fallback is what actually fires here).

    A test that patches `os.killpg` alone therefore raises `AttributeError`
    on Windows instead of recording anything (`monkeypatch.setattr` refuses
    to patch an attribute that does not exist). Patching the road the
    platform actually takes keeps the assertion honest on both.
    """
    killed = []
    if os.name == "nt":
        def _run(cmd, **kwargs):
            assert cmd[:2] == ["taskkill", "/PID"], cmd
            assert "/T" in cmd, "must walk the tree, not just the named pid"
            assert "/F" in cmd, "the CLI does not answer a polite close"
            killed.append(int(cmd[2]))
            return subprocess.CompletedProcess(cmd, 0)
        monkeypatch.setattr(share_file_mod.subprocess, "run", _run)
    else:
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append(pgid))
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    return killed


def seed(upload_id, *, size=1234, started_at=None, pid=None, done=None, log=None, result=None):
    paths = share_file_mod._upload_paths(upload_id)
    os.makedirs(paths["dir"], exist_ok=True)
    with open(paths["meta"], "w") as f:
        json.dump({"started_at": started_at if started_at is not None else time.time(),
                   "size": size}, f)
    if pid is not None:
        with open(paths["pid"], "w") as f:
            f.write(str(pid))
    if log is not None:
        with open(paths["log"], "w") as f:
            f.write(log)
    if result is not None:
        with open(paths["result"], "w") as f:
            json.dump(result, f)
    if done is not None:
        with open(paths["done"], "w") as f:
            f.write(str(done))
    return paths


def a_dead_pid():
    """A pid that is very unlikely to be alive: spawn a subprocess that
    exits immediately, wait for it to finish, and hand back its now-dead
    pid. `os.fork` doesn't exist on Windows, so this goes through
    `subprocess` (portable everywhere the interpreter itself runs) rather
    than forking directly."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# -- read_upload_state ----------------------------------------------------------


def test_no_job_dir_reads_none():
    state = share_file_mod.read_upload_state("nope")
    assert state["state"] == "none"


def test_done_file_with_zero_reads_done_and_carries_the_result():
    seed("job-done", done=0, result={"remote": "fd://h/x/f.parquet", "s3_uri": "s3://b/f.parquet"})
    state = share_file_mod.read_upload_state("job-done")
    assert state["state"] == "done"
    assert state["remote"] == "fd://h/x/f.parquet"
    assert state["s3_uri"] == "s3://b/f.parquet"
    assert state["bytes"] == 1234


def test_done_file_with_nonzero_reads_failed_and_surfaces_the_log_tail():
    seed("job-failed", done=1, log="Traceback...\nShareError: no such file\n")
    state = share_file_mod.read_upload_state("job-failed")
    assert state["state"] == "failed"
    assert "ShareError" in state["error"]


def test_no_done_file_and_a_dead_pid_reads_cancelled():
    dead = a_dead_pid()
    seed("job-cancelled", pid=dead)
    state = share_file_mod.read_upload_state("job-cancelled")
    assert state["state"] == "cancelled"


def test_no_done_file_and_a_live_pid_reads_running():
    seed("job-running", pid=os.getpid())  # our own pid is certainly alive
    state = share_file_mod.read_upload_state("job-running")
    assert state["state"] == "running"
    assert state["bytes"] == 1234


def test_no_done_file_and_no_pid_at_all_reads_cancelled():
    seed("job-never-started")
    state = share_file_mod.read_upload_state("job-never-started")
    assert state["state"] == "cancelled"


def test_elapsed_reflects_started_at():
    seed("job-elapsed", started_at=time.time() - 5, pid=os.getpid())
    state = share_file_mod.read_upload_state("job-elapsed")
    assert state["elapsed"] >= 5


# -- start_upload idempotency ----------------------------------------------------


def test_start_upload_attaches_to_a_running_job_instead_of_respawning(monkeypatch):
    seed("job-live", pid=os.getpid())
    calls = []
    monkeypatch.setattr(share_file_mod, "_spawn_upload",
                        lambda upload_id, path, share_id: calls.append(upload_id))
    state = share_file_mod.start_upload("/some/path.parquet", "job-live")
    assert state["state"] == "running"
    assert calls == []  # never spawned a second copy


def test_start_upload_spawns_when_nothing_is_running(monkeypatch):
    calls = []

    def fake_spawn(upload_id, path, share_id):
        calls.append((upload_id, path, share_id))
        seed(upload_id, pid=os.getpid())

    monkeypatch.setattr(share_file_mod, "_spawn_upload", fake_spawn)
    state = share_file_mod.start_upload("/some/path.parquet", "job-new")
    assert calls == [("job-new", "/some/path.parquet", "job-new")]
    assert state["state"] == "running"


def test_start_upload_respawns_once_a_previous_job_is_done(monkeypatch):
    seed("job-again", done=0, result={"remote": "fd://x", "s3_uri": "s3://x"})
    calls = []

    def fake_spawn(upload_id, path, share_id):
        calls.append(upload_id)
        # A real _spawn_upload clears the old job dir before writing a fresh
        # one (it wipes a stale `done` along with everything else) — mirror
        # that here so the stub reflects the state transition it stands in for.
        paths = share_file_mod._upload_paths(upload_id)
        if os.path.isfile(paths["done"]):
            os.remove(paths["done"])
        seed(upload_id, pid=os.getpid())  # overwrite: now running

    monkeypatch.setattr(share_file_mod, "_spawn_upload", fake_spawn)
    state = share_file_mod.start_upload("/some/path.parquet", "job-again")
    assert calls == ["job-again"]
    assert state["state"] == "running"


# -- cancel_upload ----------------------------------------------------------------


def test_cancel_upload_with_no_pid_is_a_harmless_noop():
    seed("job-no-pid")
    state = share_file_mod.cancel_upload("job-no-pid")
    assert state["state"] == "cancelled"


def test_cancel_upload_signals_a_real_process_group(monkeypatch):
    killed = _record_cancel_signal(monkeypatch)
    seed("job-to-cancel", pid=99999)
    share_file_mod.cancel_upload("job-to-cancel")
    assert killed == [99999]


# -- publish route: the large-file / upload_id branch -----------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(share_app_mod, "STATE_DIR", str(tmp_path / "home"))
    creds = tmp_path / "credentials"
    creds.write_text("{}")
    monkeypatch.setenv("FUSED_RENDER_FUSED_CREDENTIALS", str(creds))
    return TestClient(create_app(start_dir=str(tmp_path)))


def make_big_file(tmp_path, name="big.fused"):
    p = tmp_path / name
    p.write_bytes(b"x" * (share_file_mod.INLINE_PUBLISH_MAX_BYTES + 1))
    return str(p)


def test_publish_over_the_inline_cap_without_an_upload_requires_one_first(client, tmp_path):
    path = make_big_file(tmp_path)
    resp = client.post("/api/share/file/publish", json={"path": path}, headers=GUARD)
    assert resp.status_code == 409
    assert "call /api/share/file/upload first" in resp.json()["error"]


def test_publish_over_the_inline_cap_uses_a_finished_uploads_remote(client, tmp_path,
                                                                     monkeypatch):
    path = make_big_file(tmp_path)
    file_id = share_file_mod.file_identity(os.path.abspath(path))
    seed(file_id, done=0, result={"remote": "fd://h/x/big.fused", "s3_uri": "s3://b/big.fused"})

    captured = {}

    def fake_run_shim(request, timeout):
        captured["request"] = request
        return {"url": "https://udf.fused.ai/tok/big.html", "canvas_id": "c1",
                "canvas_name": "big_abc123", "share_token": "tok", "slug": "big_abc123",
                "remote": "fd://h/x/big.fused", "workbench_url": "https://x"}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", fake_run_shim)
    resp = client.post("/api/share/file/publish", json={"path": path}, headers=GUARD)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # The bounded canvas-only call: no local `file` re-uploaded, the
    # already-resolved remote/s3_uri from the finished job handed through.
    assert "file" not in captured["request"]
    assert captured["request"]["s3_uri"] == "s3://b/big.fused"
    assert captured["request"]["remote"] == "fd://h/x/big.fused"


def test_upload_route_spawns_and_status_route_reads_it_back(client, tmp_path, monkeypatch):
    path = make_big_file(tmp_path)
    spawned = {}

    def fake_spawn(upload_id, file_path, share_id):
        spawned["args"] = (upload_id, file_path, share_id)
        share_file_mod._upload_paths(upload_id)
        os.makedirs(share_file_mod._upload_paths(upload_id)["dir"], exist_ok=True)
        with open(share_file_mod._upload_paths(upload_id)["meta"], "w") as f:
            json.dump({"started_at": time.time(), "size": os.path.getsize(file_path)}, f)
        with open(share_file_mod._upload_paths(upload_id)["pid"], "w") as f:
            f.write(str(os.getpid()))

    monkeypatch.setattr(share_file_mod, "_spawn_upload", fake_spawn)
    resp = client.post("/api/share/file/upload", json={"path": path}, headers=GUARD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "running"
    upload_id = body["id"]
    assert spawned["args"][0] == upload_id

    status = client.get("/api/share/file/upload/status", params={"id": upload_id})
    assert status.json()["state"] == "running"


def test_upload_status_with_missing_id_errors(client):
    resp = client.get("/api/share/file/upload/status")
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_upload_cancel_without_guard_header_is_refused(client, tmp_path):
    resp = client.post("/api/share/file/upload/cancel", json={"id": "whatever"})
    assert resp.status_code == 403


def test_upload_cancel_signals_a_running_job(client, tmp_path, monkeypatch):
    killed = _record_cancel_signal(monkeypatch)
    seed("job-x", pid=12345)
    resp = client.post("/api/share/file/upload/cancel", json={"id": "job-x"}, headers=GUARD)
    assert resp.status_code == 200
    assert killed == [12345]


# -- finding 3: upload id path traversal --------------------------------------


def test_upload_status_rejects_a_path_traversal_id(client):
    resp = client.get("/api/share/file/upload/status", params={"id": "../../../etc/passwd"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_upload_status_rejects_an_id_with_a_path_separator(client):
    # Not just "..": ANY id that could steer os.path.join(share_uploads/, id)
    # outside share_uploads/ must be refused, not merely dot-dot sequences.
    resp = client.get("/api/share/file/upload/status", params={"id": "sub/dir"})
    assert resp.status_code == 400


def test_upload_status_still_accepts_a_real_minted_id(client, tmp_path):
    # A real id (file_identity()'s shape: slug + "_" + 6 hex chars) must keep
    # working — the guard must not be so tight it rejects legitimate ids.
    seed("job-running", pid=os.getpid())
    resp = client.get("/api/share/file/upload/status", params={"id": "job-running"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "running"


def test_upload_cancel_rejects_a_path_traversal_id(client):
    resp = client.post("/api/share/file/upload/cancel", json={"id": "../../../etc/passwd"},
                       headers=GUARD)
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_upload_cancel_still_signals_a_real_minted_id(client, monkeypatch):
    killed = _record_cancel_signal(monkeypatch)
    seed("job-x", pid=12345)
    resp = client.post("/api/share/file/upload/cancel", json={"id": "job-x"}, headers=GUARD)
    assert resp.status_code == 200
    assert killed == [12345]


# -- finding 4: publish must not trust a mismatched upload_id ------------------


def test_publish_rejects_an_upload_id_for_a_different_file(client, tmp_path):
    path = make_big_file(tmp_path, "big.fused")
    other_path = make_big_file(tmp_path, "other.fused")
    other_id = share_file_mod.file_identity(os.path.abspath(other_path))
    # Seed a finished upload under OTHER file's id, then try to publish
    # `big.fused` while claiming that upload as its own — this must not let
    # big.fused's canvas point at other.fused's uploaded remote (finding 4).
    seed(other_id, done=0, result={"remote": "fd://h/x/other.fused", "s3_uri": "s3://b/other.fused"})
    resp = client.post("/api/share/file/publish", json={"path": path, "upload_id": other_id},
                       headers=GUARD)
    assert resp.status_code == 400
    assert "upload_id" in resp.json()["error"]


def test_publish_still_accepts_the_uploads_own_matching_id(client, tmp_path, monkeypatch):
    path = make_big_file(tmp_path, "big.fused")
    file_id = share_file_mod.file_identity(os.path.abspath(path))
    seed(file_id, done=0, result={"remote": "fd://h/x/big.fused", "s3_uri": "s3://b/big.fused"})

    def fake_run_shim(request, timeout):
        return {"url": "https://udf.fused.ai/tok/big.html", "canvas_id": "c1",
                "canvas_name": "big_abc123", "share_token": "tok", "slug": "big_abc123",
                "remote": "fd://h/x/big.fused", "workbench_url": "https://x"}, None

    monkeypatch.setattr(share_app_mod, "_run_shim", fake_run_shim)
    resp = client.post("/api/share/file/publish", json={"path": path, "upload_id": file_id},
                       headers=GUARD)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# -- finding 11: the upload subprocess must not become a zombie ---------------


def test_spawn_upload_reaps_its_subprocess_instead_of_leaving_a_zombie(monkeypatch, tmp_path):
    # Nothing else ever calls proc.wait() — the state machine reads the
    # `done` marker file, not the child's exit as this process sees it — so
    # without a reaper thread the shell wrapper becomes a zombie child of the
    # server the instant it exits, and `_pid_alive` (os.kill(pid, 0)) reports
    # a zombie as alive: a cancelled upload could read "running" forever.
    waited = threading.Event()

    class FakeProc:
        pid = 424242

        def wait(self):
            waited.set()

    monkeypatch.setattr(share_app_mod, "_shim_command", lambda: (["true"], None))
    monkeypatch.setattr(share_file_mod, "fused_cli", lambda: "true")
    monkeypatch.setattr(share_file_mod, "child_env", lambda cli: {})
    monkeypatch.setattr(share_file_mod, "workbench_env", lambda: "prod")
    monkeypatch.setattr(share_file_mod.subprocess, "Popen", lambda *a, **k: FakeProc())

    f = tmp_path / "demo.fused"
    f.write_bytes(b"x")
    share_file_mod._spawn_upload("job-reap", str(f), "job-reap")

    assert waited.wait(timeout=2), "proc.wait() was never called — the child is never reaped"
