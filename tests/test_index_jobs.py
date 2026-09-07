"""The Activity bridge: index scan runs mirrored into sys:index:<run_id> jobs.

The card at the foot of the shell (`DownloadManager.tsx`) already polls
`GET /api/jobs` for every download/task the app is running; a re-index run
must show up there the same way instead of only being visible in
Preferences > Indexing. This module's `mirror_index_jobs_once` is the bridge:
one tick reads `runner.list_runs()` (the same fold `/api/index/status` uses)
and writes one `jobs.upsert(..., server=True)` per active/just-finished run.

See DECISIONS.md D724+.
"""
import json
import os

import pytest

from fused_render import jobs
from fused_render.index.config import IndexConfig
from fused_render.server.routers import index as index_router


@pytest.fixture(autouse=True)
def _reset():
    jobs.reset()
    index_router._mirrored_terminal.clear()
    index_router._index_job_wake.clear()
    yield
    jobs.reset()
    index_router._mirrored_terminal.clear()
    index_router._index_job_wake.clear()


def _run(run_id, root="/Users/tester/docs", **over):
    base = {
        "run_id": run_id, "root": root, "running": True, "phase": "walking",
        "dirs": 3, "files": 12, "reused": 0, "current": "", "summary": None,
        "cancelled": False, "error": None,
    }
    base.update(over)
    return base


def _tick(monkeypatch, runs, cfg=None):
    monkeypatch.setattr(
        index_router.runner, "list_runs",
        lambda cfg, limit=20: {"runs": runs})
    index_router.mirror_index_jobs_once(cfg=cfg if cfg is not None else object())


def test_active_run_creates_an_indeterminate_job_keyed_by_run_id(monkeypatch):
    _tick(monkeypatch, [_run("r1", files=12)])
    rows = jobs.list_jobs()
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "sys:index:r1"
    assert row["state"] == "running"
    assert row["owner"] == "server"
    assert row["kind"] == "task"
    assert row["total"] is None
    assert row["done"] == 12.0


def test_two_concurrent_runs_produce_two_distinct_jobs(monkeypatch):
    _tick(monkeypatch, [
        _run("r1", root="/Users/tester/a"),
        _run("r2", root="/Users/tester/b"),
    ])
    ids = {j["id"] for j in jobs.list_jobs()}
    assert ids == {"sys:index:r1", "sys:index:r2"}


def test_phase_string_reaches_the_job(monkeypatch):
    _tick(monkeypatch, [_run("r1", phase="walking")])
    assert jobs.list_jobs()[0]["message"] == "walking"


def test_compaction_phase_reaches_the_job_too(monkeypatch):
    """Compaction has no structured flag — it appears only as the phase text
    ("writing index" / "writing signatures", index/store.py:424-426,510,554).
    The bridge must not try to detect it separately; it is just another
    phase string."""
    _tick(monkeypatch, [_run("r1", phase="writing index")])
    assert jobs.list_jobs()[0]["message"] == "writing index"
    _tick(monkeypatch, [_run("r1", phase="writing signatures")])
    assert jobs.list_jobs()[0]["message"] == "writing signatures"


def test_run_going_done_writes_terminal_state(monkeypatch):
    _tick(monkeypatch, [_run("r1", running=True)])
    _tick(monkeypatch, [_run(
        "r1", running=False, cancelled=False, error=None,
        summary={"files": 12, "dirs": 3})])
    row = jobs.list_jobs()[0]
    assert row["state"] == "done"


def test_run_going_error_writes_terminal_state(monkeypatch):
    _tick(monkeypatch, [_run("r1", running=True)])
    _tick(monkeypatch, [_run(
        "r1", running=False, error="disk full")])
    row = jobs.list_jobs()[0]
    assert row["state"] == "error"
    assert row["message"] == "disk full"


def test_run_going_cancelled_writes_terminal_state(monkeypatch):
    _tick(monkeypatch, [_run("r1", running=True)])
    _tick(monkeypatch, [_run("r1", running=False, cancelled=True)])
    row = jobs.list_jobs()[0]
    assert row["state"] == "cancelled"


def test_terminal_state_is_written_exactly_once(monkeypatch):
    """Once a run has gone terminal, `list_runs` keeps returning it (it stays
    in the KEEP_RUNS-sized recent list) — the bridge must not keep calling
    `jobs.upsert` for it forever, or every finished scan becomes a permanent
    tick-rate write."""
    upsert_calls = []
    real_upsert = jobs.upsert

    def spy_upsert(body, **kw):
        upsert_calls.append(dict(body))
        return real_upsert(body, **kw)

    monkeypatch.setattr(index_router.jobs, "upsert", spy_upsert)

    _tick(monkeypatch, [_run("r1", running=True)])
    _tick(monkeypatch, [_run("r1", running=True)])
    _tick(monkeypatch, [_run("r1", running=False, summary={"files": 12})])
    # The finished run stays in list_runs's output (it's still one of the
    # KEEP_RUNS most recent) for several more ticks.
    _tick(monkeypatch, [_run("r1", running=False, summary={"files": 12})])
    _tick(monkeypatch, [_run("r1", running=False, summary={"files": 12})])

    assert len(upsert_calls) == 3  # 2 running ticks + 1 terminal write
    assert jobs.list_jobs()[0]["state"] == "done"


def test_cancel_requested_on_the_job_actually_cancels_the_run(monkeypatch):
    """cancellable is advertised True, and the bridge must honor a request
    the same next-tick way every other `server`-owned job does: by calling
    `runner.cancel` — the same function `/api/index/cancel` calls."""
    cancelled_run_ids = []
    monkeypatch.setattr(
        index_router.runner, "cancel",
        lambda cfg, run_id: cancelled_run_ids.append(run_id) or {"cancelled": run_id})

    _tick(monkeypatch, [_run("r1", running=True)])
    assert jobs.list_jobs()[0]["cancellable"] is True
    jobs.request_cancel("sys:index:r1")
    assert cancelled_run_ids == []  # not yet honored — only on the NEXT tick

    _tick(monkeypatch, [_run("r1", running=True)])
    assert cancelled_run_ids == ["r1"]


# --------------------------------------------------------- idle backoff (D727)

def test_mirror_index_jobs_once_reports_whether_any_run_is_live(monkeypatch):
    """The loop's idle-vs-active cadence choice reads this return value
    instead of a second `list_runs` call — so it has to actually reflect
    liveness, not just "did something get upserted"."""
    monkeypatch.setattr(
        index_router.runner, "list_runs",
        lambda cfg, limit=20: {"runs": [_run("r1", running=True)]})
    assert index_router.mirror_index_jobs_once(cfg=object()) is True

    jobs.reset()
    index_router._mirrored_terminal.clear()
    monkeypatch.setattr(
        index_router.runner, "list_runs",
        lambda cfg, limit=20: {"runs": [_run("r1", running=False)]})
    assert index_router.mirror_index_jobs_once(cfg=object()) is False

    monkeypatch.setattr(
        index_router.runner, "list_runs", lambda cfg, limit=20: {"runs": []})
    assert index_router.mirror_index_jobs_once(cfg=object()) is False


class _StopLoop(Exception):
    """Escapes `_index_job_loop`'s `while True` after exactly one tick."""


def _one_tick_wait_spy(waits):
    def _wait(timeout):
        waits.append(timeout)
        raise _StopLoop
    return _wait


def test_loop_sleeps_the_active_interval_when_a_run_is_live(monkeypatch):
    waits = []
    monkeypatch.setattr(index_router, "mirror_index_jobs_once", lambda: True)
    monkeypatch.setattr(
        index_router._index_job_wake, "wait", _one_tick_wait_spy(waits))
    with pytest.raises(_StopLoop):
        index_router._index_job_loop()
    assert waits == [index_router.INDEX_JOB_ACTIVE_S]


def test_loop_sleeps_the_idle_interval_when_no_run_is_live(monkeypatch):
    waits = []
    monkeypatch.setattr(index_router, "mirror_index_jobs_once", lambda: False)
    monkeypatch.setattr(
        index_router._index_job_wake, "wait", _one_tick_wait_spy(waits))
    with pytest.raises(_StopLoop):
        index_router._index_job_loop()
    assert waits == [index_router.INDEX_JOB_IDLE_S]


def test_a_failing_tick_backs_off_to_idle_rather_than_pinning_fast(monkeypatch):
    """The existing exception protection (a bad tick must not kill the loop)
    must not also pin the loop to the fast cadence forever."""
    waits = []

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(index_router, "mirror_index_jobs_once", _boom)
    monkeypatch.setattr(
        index_router._index_job_wake, "wait", _one_tick_wait_spy(waits))
    with pytest.raises(_StopLoop):
        index_router._index_job_loop()
    assert waits == [index_router.INDEX_JOB_IDLE_S]


def test_a_scan_starting_while_idle_wakes_the_loop_without_waiting_it_out(
        monkeypatch):
    """The wake mechanism: a scan starting sets `_index_job_wake`, which is
    exactly what lets a real loop's `Event.wait(INDEX_JOB_IDLE_S)` return
    early instead of a freshly started run waiting out the full idle
    interval before it appears in Activity."""
    assert not index_router._index_job_wake.is_set()
    index_router._wake_index_job_bridge()
    assert index_router._index_job_wake.is_set()


def test_api_index_scan_wakes_the_bridge_on_a_started_run(monkeypatch):
    monkeypatch.setattr(
        index_router.index_gate, "indexing_blocked", lambda: "")
    monkeypatch.setattr(
        index_router, "load_config", lambda: object())
    monkeypatch.setattr(
        index_router.runner, "start",
        lambda cfg, root, full=False: {"run_id": "r1", "root": root})
    assert not index_router._index_job_wake.is_set()
    index_router.api_index_scan(body={"root": "/Users/tester/docs"},
                                x_fused="1")
    assert index_router._index_job_wake.is_set()


def test_run_startup_scan_wakes_the_bridge_on_a_started_run(monkeypatch):
    monkeypatch.setattr(
        index_router.index_gate, "indexing_blocked", lambda: "")
    monkeypatch.setattr(index_router, "load_config", lambda: object())
    monkeypatch.setattr(index_router.runner, "prune_runs",
                        lambda cfg, keep=20: None)
    monkeypatch.setattr(index_router, "scan_roots",
                        lambda cfg, start_dir=None: ["/Users/tester"])
    monkeypatch.setattr(index_router.runner, "last_scan",
                        lambda cfg, root: None)
    monkeypatch.setattr(
        index_router.runner, "start",
        lambda cfg, root, full=False: {"run_id": "r1", "root": root})
    assert not index_router._index_job_wake.is_set()
    index_router.run_startup_scan()
    assert index_router._index_job_wake.is_set()


# ------------------------------------------------- estimated total (rescan)

def test_no_prior_manifest_leaves_total_none(monkeypatch, tmp_path):
    """A first-ever scan: `partitions.json` does not exist yet, so there is
    no honest denominator — `total` stays the indeterminate `None`, not a
    guess."""
    cfg = IndexConfig(dir=str(tmp_path))
    _tick(monkeypatch, [_run("r1", files=10856)], cfg=cfg)
    row = jobs.list_jobs()[0]
    assert row["total"] is None
    assert "(estimated)" not in row["detail"]


def test_a_prior_manifests_row_count_becomes_the_estimated_total(
        monkeypatch, tmp_path):
    """A rescan: the last completed scan's row count (the same
    `partitions.json` field `/api/index/status` already reads as
    `files_indexed`) becomes the bar's denominator, and the row says so is a
    guess rather than a promise."""
    cfg = IndexConfig(dir=str(tmp_path))
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.partitions_json, "w") as f:
        json.dump({"rows": 672424, "updated": 0}, f)
    _tick(monkeypatch, [_run("r1", files=10856)], cfg=cfg)
    row = jobs.list_jobs()[0]
    assert row["total"] == 672424.0
    assert "(estimated)" in row["detail"]


def test_a_prior_manifest_with_zero_rows_leaves_total_none(monkeypatch, tmp_path):
    """`rows: 0` is not a usable estimate (an empty prior scan says nothing
    about how big this one will be) — treated the same as no manifest at
    all."""
    cfg = IndexConfig(dir=str(tmp_path))
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.partitions_json, "w") as f:
        json.dump({"rows": 0, "updated": 0}, f)
    _tick(monkeypatch, [_run("r1", files=5)], cfg=cfg)
    row = jobs.list_jobs()[0]
    assert row["total"] is None


def test_done_credits_reused_files_alongside_newly_walked_ones(monkeypatch):
    """`files` alone is only the NEWLY-walked count (`Sink.add`,
    `index/store.py:215-229`, credits `kind != "u"` dirs only) — an unchanged
    dir's cached file count lands in `reused` instead (`index/scan.py:78`).
    The previous scan's stored total (`prev_total`, used as `total` above)
    counts BOTH, so `done` must too, or a rescan that reuses most of a tree
    crawls to a few percent on `files` alone and then jumps straight to done
    the moment compaction lands — a bar that visibly lies while looking
    official. This has nothing to do with an estimated `total` being present:
    it is what the LIVE COUNT itself means."""
    _tick(monkeypatch, [_run("r1", files=200, reused=9800)])
    row = jobs.list_jobs()[0]
    assert row["done"] == 10000.0


def test_a_grown_tree_does_not_stop_the_estimate_from_being_offered(
        monkeypatch, tmp_path):
    """`done` (this scan's live count) already exceeds the prior scan's
    count when the tree has grown — the bridge still reports the honest
    numbers; clamping the displayed FRACTION is jobs.ts `jobFraction`'s job
    (it already clamps `done / total` to 1.0 for every job kind), not
    something the bridge should hide by lying about `total`."""
    cfg = IndexConfig(dir=str(tmp_path))
    os.makedirs(cfg.dir, exist_ok=True)
    with open(cfg.partitions_json, "w") as f:
        json.dump({"rows": 100, "updated": 0}, f)
    _tick(monkeypatch, [_run("r1", files=150)], cfg=cfg)
    row = jobs.list_jobs()[0]
    assert row["total"] == 100.0
    assert row["done"] == 150.0
