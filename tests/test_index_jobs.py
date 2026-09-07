"""The Activity bridge: index scan runs mirrored into sys:index:<run_id> jobs.

The card at the foot of the shell (`DownloadManager.tsx`) already polls
`GET /api/jobs` for every download/task the app is running; a re-index run
must show up there the same way instead of only being visible in
Preferences > Indexing. This module's `mirror_index_jobs_once` is the bridge:
one tick reads `runner.list_runs()` (the same fold `/api/index/status` uses)
and writes one `jobs.upsert(..., server=True)` per active/just-finished run.

See DECISIONS.md D724+.
"""
import pytest

from fused_render import jobs
from fused_render.server.routers import index as index_router


@pytest.fixture(autouse=True)
def _reset():
    jobs.reset()
    index_router._mirrored_terminal.clear()
    yield
    jobs.reset()
    index_router._mirrored_terminal.clear()


def _run(run_id, root="/Users/tester/docs", **over):
    base = {
        "run_id": run_id, "root": root, "running": True, "phase": "walking",
        "dirs": 3, "files": 12, "reused": 0, "current": "", "summary": None,
        "cancelled": False, "error": None,
    }
    base.update(over)
    return base


def _tick(monkeypatch, runs):
    monkeypatch.setattr(
        index_router.runner, "list_runs",
        lambda cfg, limit=20: {"runs": runs})
    index_router.mirror_index_jobs_once(cfg=object())


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
