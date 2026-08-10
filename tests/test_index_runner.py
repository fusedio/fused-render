"""The scan control plane: starting a detached run, polling its event log,
cancelling it, and the last-scan bookkeeping the startup scheduler debounces
on. See fused_render/index/specs/scan.md §1-§3.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from fused_render.index import runner
from fused_render.index.config import IndexConfig


def _cfg(tmp_path):
    return IndexConfig(dir=str(tmp_path / "ix"))


class _FakePopen:
    """Records the argv/kwargs a spawn would have used."""

    calls: list = []

    def __init__(self, argv, **kwargs):
        _FakePopen.calls.append((argv, kwargs))
        self.pid = 4242


@pytest.fixture()
def spawned(monkeypatch):
    _FakePopen.calls = []
    monkeypatch.setattr(runner.subprocess, "Popen", _FakePopen)
    return _FakePopen.calls


# -- start ---------------------------------------------------------------------

def test_start_spawns_the_worker_as_a_module_not_a_file(tmp_path, spawned):
    """`Popen([python, __file__])` has no meaning inside a py2app bundle —
    the source file isn't there. The module entrypoint is importable
    wherever the package is."""
    cfg = _cfg(tmp_path)
    started = runner.start(cfg, str(tmp_path))
    argv, kwargs = spawned[0]
    assert argv[:3] == [sys.executable, "-m", "fused_render.index.worker"]
    assert argv[3] == os.path.join(cfg.runs_dir, started["run_id"])


def test_start_spawns_without_forking_the_server(tmp_path, spawned):
    """The spawn kwargs must keep CPython on posix_spawn. `start_new_session`
    (or `preexec_fn`, or `close_fds=True`) forces fork()+exec, and a fork of
    a server that has loaded pyproj/rasterio runs PROJ's pthread_atfork
    handler and SIGSEGVs before Python starts: the startup scan works, every
    later on-demand scan dies with an empty worker.log. Session detachment
    lives in the worker's own main() (os.setsid) instead."""
    runner.start(_cfg(tmp_path), str(tmp_path))
    _, kwargs = spawned[0]
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert "start_new_session" not in kwargs
        assert "preexec_fn" not in kwargs
        assert kwargs.get("close_fds") is False


def test_start_writes_a_spec_the_worker_can_read(tmp_path, spawned):
    cfg = _cfg(tmp_path)
    cfg.ignore = ["node_modules"]
    started = runner.start(cfg, str(tmp_path), full=True)
    spec = json.load(open(os.path.join(cfg.runs_dir, started["run_id"], "spec.json")))
    assert spec["root"] == str(tmp_path)
    assert spec["full"] is True
    # the config travels WITH the run: the detached worker must not re-derive
    # the store location from an environment that may have moved
    assert spec["config"]["dir"] == cfg.dir
    assert spec["config"]["ignore"] == ["node_modules"]
    assert spec["mounts_dir"]


def test_start_expands_and_canonicalizes_the_root(tmp_path, spawned):
    cfg = _cfg(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    started = runner.start(cfg, str(sub) + "/")
    assert started["root"] == str(sub)


def test_start_rejects_a_non_directory(tmp_path, spawned):
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        runner.start(_cfg(tmp_path), str(f))
    assert spawned == []


def test_start_refuses_a_mount_backed_root(tmp_path, spawned, monkeypatch):
    """Indexing a remote mount is out of scope AND unsafe: the crawl would be
    kernel I/O on an rclone NFS path."""
    mounts = tmp_path / "mounts"
    (mounts / "m1").mkdir(parents=True)
    monkeypatch.setattr(runner, "_mounts_dir", lambda: str(mounts))
    with pytest.raises(ValueError, match="mount"):
        runner.start(_cfg(tmp_path), str(mounts / "m1"))
    assert spawned == []


def test_start_records_the_scan_time_for_debouncing(tmp_path, spawned):
    cfg = _cfg(tmp_path)
    assert runner.last_scan(cfg, str(tmp_path)) is None
    runner.start(cfg, str(tmp_path))
    assert runner.last_scan(cfg, str(tmp_path)) > 0
    assert runner.last_scan(cfg, str(tmp_path / "elsewhere")) is None


def test_start_joins_a_live_run_of_the_same_root(tmp_path, spawned):
    """Two scans of one root are never wanted: they duplicate the whole walk,
    race each other's reuse cache, and — because each worker stamps the ignore
    sig from ITS OWN spec — let a pre-edit run finish last and stamp the OLD
    rules over the post-edit run's, leaving the root stale forever. The store
    lock keeps the two compactions from corrupting the manifest; it does
    nothing about any of that. So the second start joins the first."""
    cfg = _cfg(tmp_path)
    first = runner.start(cfg, str(tmp_path))
    again = runner.start(cfg, str(tmp_path))
    assert again["run_id"] == first["run_id"]
    assert again["already_running"] is True
    assert len(spawned) == 1  # no second worker


def test_start_scans_a_different_root_while_one_runs(tmp_path, spawned):
    """The guard is per ROOT: configured roots scan concurrently by design."""
    cfg = _cfg(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    first = runner.start(cfg, str(tmp_path))
    second = runner.start(cfg, str(other))
    assert second["run_id"] != first["run_id"]
    assert "already_running" not in second
    assert len(spawned) == 2


def test_start_ignores_a_finished_run_of_the_same_root(tmp_path, spawned):
    cfg = _cfg(tmp_path)
    first = runner.start(cfg, str(tmp_path))
    with open(os.path.join(cfg.runs_dir, first["run_id"], "events.jsonl"),
              "w") as f:
        f.write(json.dumps({"type": "run_end", "summary": {}}) + "\n")
    second = runner.start(cfg, str(tmp_path))
    assert second["run_id"] != first["run_id"]
    assert len(spawned) == 2


def test_start_ignores_an_abandoned_run_of_the_same_root(tmp_path, spawned):
    """Liveness is the heartbeat, not the mere presence of a run dir: a
    killed worker leaves a `running` log behind, and treating that as live
    would wedge every future scan of the root."""
    cfg = _cfg(tmp_path)
    first = runner.start(cfg, str(tmp_path))
    rd = os.path.join(cfg.runs_dir, first["run_id"])
    dead = time.time() - runner.ABANDONED_RUN_S - 60
    for name in os.listdir(rd):
        os.utime(os.path.join(rd, name), (dead, dead))
    second = runner.start(cfg, str(tmp_path))
    assert second["run_id"] != first["run_id"]
    assert len(spawned) == 2


# -- status / cancel / list ----------------------------------------------------

def _run_with_events(cfg, run_id, events):
    d = os.path.join(cfg.runs_dir, run_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r", "full": False, "started": 0}, f)
    with open(os.path.join(d, "events.jsonl"), "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return d


def test_status_folds_the_log_into_a_flat_state(tmp_path):
    cfg = _cfg(tmp_path)
    _run_with_events(cfg, "r1", [
        {"type": "run_start", "msg": "/r"},
        {"type": "phase", "msg": "scanning (incremental)"},
        {"type": "progress", "dirs": 3, "files": 9, "reused": 2, "current": "/r/a"},
        {"type": "run_end", "msg": "complete", "summary": {"rows": 9}},
    ])
    st = runner.status(cfg, "r1")["state"]
    assert st["running"] is False
    assert st["phase"] == "scanning (incremental)"
    assert (st["dirs"], st["files"], st["reused"]) == (3, 9, 2)
    assert st["summary"] == {"rows": 9}
    assert st["cancelled"] is False


def test_status_returns_only_events_after_the_cursor(tmp_path):
    cfg = _cfg(tmp_path)
    _run_with_events(cfg, "r1", [{"type": "phase", "msg": "a"},
                                 {"type": "phase", "msg": "b"}])
    out = runner.status(cfg, "r1", since=1)
    assert [e["msg"] for e in out["events"]] == ["b"]
    assert out["cursor"] == 2


def test_status_tolerates_a_half_written_last_line(tmp_path):
    cfg = _cfg(tmp_path)
    d = _run_with_events(cfg, "r1", [{"type": "phase", "msg": "a"}])
    with open(os.path.join(d, "events.jsonl"), "a") as f:
        f.write('{"type": "progr')
    assert [e["msg"] for e in runner.status(cfg, "r1")["events"]] == ["a"]


def test_status_of_an_unknown_run_raises(tmp_path):
    with pytest.raises(ValueError):
        runner.status(_cfg(tmp_path), "nope")


def test_cancel_writes_the_flag_file(tmp_path):
    cfg = _cfg(tmp_path)
    d = _run_with_events(cfg, "r1", [])
    runner.cancel(cfg, "r1")
    assert os.path.exists(os.path.join(d, "cancel"))


def test_cancel_of_an_unknown_run_raises(tmp_path):
    with pytest.raises(ValueError):
        runner.cancel(_cfg(tmp_path), "nope")


def test_list_runs_is_newest_first_with_state(tmp_path):
    cfg = _cfg(tmp_path)
    _run_with_events(cfg, "20260101-000000-aa", [{"type": "run_end", "msg": "complete"}])
    _run_with_events(cfg, "20260102-000000-bb", [{"type": "phase", "msg": "scanning"}])
    runs = runner.list_runs(cfg)["runs"]
    assert [r["run_id"] for r in runs] == ["20260102-000000-bb", "20260101-000000-aa"]
    assert runs[0]["running"] is True
    assert runs[1]["running"] is False


def test_prune_runs_keeps_the_newest_and_deletes_the_rest(tmp_path):
    """OpenIndex never cleaned its run directories; here they live under the
    index dir, so a shard-heavy abandoned run would grow the store forever."""
    cfg = _cfg(tmp_path)
    for i in range(5):
        _run_with_events(cfg, f"2026010{i}-000000-x",
                         [{"type": "run_end", "msg": "complete"}])
    runner.prune_runs(cfg, keep=2)
    assert sorted(os.listdir(cfg.runs_dir)) == [
        "20260103-000000-x", "20260104-000000-x"]


def test_a_dead_worker_stops_reporting_as_scanning(tmp_path):
    """A worker that dies without a run_end (killed, OOM, spawn crash) just
    stops appending — the log alone reads as `running` forever, which kept
    /api/index/status saying `scanning: true` (UI: "indexing…", buttons
    disabled) for a day. An unfinished run untouched for ABANDONED_RUN_S is
    reported dead, with the reason in `error`."""
    cfg = _cfg(tmp_path)
    dead = _run_with_events(cfg, "20260101-000000-x", [{"type": "phase", "msg": "scanning"}])
    old = time.time() - runner.ABANDONED_RUN_S - 60
    for name in os.listdir(dead):
        os.utime(os.path.join(dead, name), (old, old))
    run = runner.list_runs(cfg)["runs"][0]
    assert run["running"] is False
    assert "died" in run["error"]
    st = runner.status(cfg, "20260101-000000-x")["state"]
    assert st["running"] is False


def test_a_quiet_but_recent_run_still_reports_as_scanning(tmp_path):
    cfg = _cfg(tmp_path)
    _run_with_events(cfg, "20260101-000000-x", [{"type": "phase", "msg": "compacting"}])
    run = runner.list_runs(cfg)["runs"][0]
    assert run["running"] is True
    assert run["error"] is None


def test_a_spawn_crash_with_an_empty_log_reports_dead_not_scanning(tmp_path):
    """The exact shape the fork/SIGSEGV bug left behind: spec.json + an empty
    worker.log and nothing else."""
    cfg = _cfg(tmp_path)
    d = os.path.join(cfg.runs_dir, "20260101-000000-x")
    os.makedirs(d)
    with open(os.path.join(d, "spec.json"), "w") as f:
        json.dump({"root": "/r", "full": False, "started": 0}, f)
    open(os.path.join(d, "worker.log"), "w").close()
    old = time.time() - runner.ABANDONED_RUN_S - 60
    for name in os.listdir(d):
        os.utime(os.path.join(d, name), (old, old))
    run = runner.list_runs(cfg)["runs"][0]
    assert run["running"] is False


def test_prune_runs_reclaims_a_run_that_died_without_closing_its_log(tmp_path):
    cfg = _cfg(tmp_path)
    _run_with_events(cfg, "20260104-000000-x", [{"type": "run_end", "msg": "complete"}])
    dead = _run_with_events(cfg, "20260101-000000-x", [{"type": "phase", "msg": "scanning"}])
    old = time.time() - runner.STALE_RUN_S - 60
    for name in os.listdir(dead):
        os.utime(os.path.join(dead, name), (old, old))
    runner.prune_runs(cfg, keep=1)
    assert os.listdir(cfg.runs_dir) == ["20260104-000000-x"]


def test_prune_runs_never_deletes_a_live_run(tmp_path):
    cfg = _cfg(tmp_path)
    for i in range(4):
        _run_with_events(cfg, f"2026010{i}-000000-x",
                         [{"type": "run_end", "msg": "complete"}])
    _run_with_events(cfg, "20260100-000000-live", [{"type": "phase", "msg": "scanning"}])
    runner.prune_runs(cfg, keep=1)
    assert "20260100-000000-live" in os.listdir(cfg.runs_dir)


# -- the module entrypoint, for real -------------------------------------------

def test_the_worker_module_runs_a_scan_end_to_end(tmp_path):
    """Spawn `python -m fused_render.index.worker` the way `start` does (but
    synchronously) — the one test that proves the entrypoint exists and the
    package is importable from a child."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.txt").write_text("hi", encoding="utf-8")
    cfg = _cfg(tmp_path)
    run_dir = os.path.join(cfg.runs_dir, "manual")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "spec.json"), "w") as f:
        json.dump({"root": str(src), "full": False, "started": 0,
                   "config": cfg.to_dict()}, f)
    proc = subprocess.run(
        [sys.executable, "-m", "fused_render.index.worker", run_dir],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in
              open(os.path.join(run_dir, "events.jsonl")) if line.strip()]
    end = [e for e in events if e["type"] == "run_end"][-1]
    assert end["msg"] == "complete", end.get("error")
    assert end["summary"]["rows"] == 1


def test_start_checks_the_mount_guard_before_touching_the_kernel(tmp_path, spawned, monkeypatch):
    """The mount refusal must come from pure string work: an os.path.isdir on
    a path under a wedged NFS mount blocks the request thread indefinitely,
    so the guard has to fire before ANY kernel syscall on the root."""
    mounts = tmp_path / "mounts"
    (mounts / "m1").mkdir(parents=True)
    monkeypatch.setattr(runner, "_mounts_dir", lambda: str(mounts))

    def wedged_isdir(path):
        raise AssertionError(f"kernel isdir on {path} before the mount guard")

    monkeypatch.setattr(runner.os.path, "isdir", wedged_isdir)
    with pytest.raises(ValueError, match="mount"):
        runner.start(_cfg(tmp_path), str(mounts / "m1"))
    assert spawned == []
