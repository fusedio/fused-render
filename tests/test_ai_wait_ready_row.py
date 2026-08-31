"""`_wait_ready`'s row merge (SPEC §36, this change's own decision entry).

Before this, a caller waiting on a shared model load and the load itself
each reported to their own row, and the manager drew both — two rows saying
the same thing for one wait. `_wait_ready` now mirrors the load row's live
progress onto the caller's row and marks it `waiting_for` the load's job id,
which `jobs.ts` `mergedRows` reads to hide the load row on the client for as
long as that reference holds. This file is the server half: that the tick
actually carries the mirrored fields, and that the merge is undone on every
exit from the wait so a load that fails afterwards is not left invisible
(D266).

Driven exactly the way the brief suggested: `_start_resident`/`ready_worker`
stubbed out (no real subprocess, no real model), the load row seeded by hand
with `jobs.upsert(..., server=True)`, and `_report` spied on (while still
calling through to the real one) so each tick's kwargs can be inspected
directly rather than reconstructed from timing.
"""
import pytest

from fused_render import jobs
from fused_render.ai import registry, supervisor


@pytest.fixture(autouse=True)
def clean_registry():
    """Both registries this file pokes directly: `jobs._jobs` (rows) and
    `supervisor._workers` (residency) — each test seeds a fake `Worker`
    straight into `_workers` to satisfy `_wait_ready`'s eviction check without
    a real bring-up, and leaving that in place would leak a "resident" model
    into every OTHER test in the suite that checks `supervisor.describe()` or
    `ready_worker()`."""
    jobs.reset()
    yield
    jobs.reset()
    with supervisor._lock:
        supervisor._workers.clear()


def test_the_merged_tick_mirrors_the_load_row_and_clears_on_exit(monkeypatch):
    capability = registry.IMAGE_GENERATION
    model = "org/slow-flux"
    load_job = supervisor.job_id_for(model)
    caller_job = supervisor.IMAGE_JOB_PREFIX + "waiter"

    # A real `Worker` so `_wait_ready`'s `_workers.get(capability) is not
    # pending` eviction check reads "not evicted" — its identity is all that
    # matters here, not what loads it.
    pending = supervisor.Worker(model=model, capability=capability, runner_code="fake")
    with supervisor._lock:
        supervisor._workers[capability] = pending

    monkeypatch.setattr(
        supervisor, "_start_resident",
        lambda m, c: ({"jobId": load_job, "model": m, "state": "loading"}, pending),
    )

    # Not ready for the first two polls, ready on the third — so the merged
    # tick fires (at least) twice before the wait returns.
    calls = {"n": 0}

    def fake_ready_worker(cap, mdl=None):
        calls["n"] += 1
        return pending if calls["n"] >= 3 else None

    monkeypatch.setattr(supervisor, "ready_worker", fake_ready_worker)
    monkeypatch.setattr(supervisor.time, "sleep", lambda s: None)  # test speed

    reports = []
    real_report = supervisor._report

    def spy_report(job, **fields):
        reports.append((job, fields))
        real_report(job, **fields)

    monkeypatch.setattr(supervisor, "_report", spy_report)

    # The caller's row IDENTITY, the way `transcribe_row_fields` and the
    # image/video opening reports build one — `unit: "s"` here specifically
    # to prove the load row's `unit: "bytes"` WINS during the wait and the
    # caller's own `"s"` comes back once the wait ends (the ordering rule the
    # docstring calls out).
    row = {"title": "a cat", "model": model, "kind": "task", "cancellable": True,
           "unit": "s"}
    jobs.upsert({"id": caller_job, **row, "state": "running"}, server=True)

    # The load's own row, seeded as its real reporter would leave it mid-pull.
    jobs.upsert({
        "id": load_job, "title": model, "model": model, "kind": "download",
        "state": "running", "unit": "bytes", "done": 1_200_000_000,
        "total": 8_000_000_000, "total_scope": "download",
        "detail": "Loading weights into memory…",
    }, server=True)

    worker = supervisor._wait_ready(model, capability, caller_job, row)
    assert worker is pending

    caller_ticks = [fields for job, fields in reports if job == caller_job]
    assert len(caller_ticks) >= 2, "expected at least one merged tick plus the clearing one"

    # (a) While waiting: the load row's detail verbatim, its byte
    # done/total/unit, and waiting_for naming the load's job id.
    merged = caller_ticks[0]
    assert merged["detail"] == "Loading weights into memory…"
    assert merged["done"] == 1_200_000_000
    assert merged["total"] == 8_000_000_000
    assert merged["unit"] == "bytes"
    assert merged["total_scope"] == "download"
    assert merged["waiting_for"] == load_job

    # (b) After the wait ends: waiting_for cleared, done/total cleared, and
    # the caller's OWN unit ("s") restored rather than left at "bytes".
    cleared = caller_ticks[-1]
    assert cleared["waiting_for"] == ""
    assert cleared["done"] is None
    assert cleared["total"] is None
    assert cleared["unit"] == "s"

    # And the registry itself agrees — not just the kwargs `_report` was
    # called with.
    final_row = next(j for j in jobs.list_jobs() if j["id"] == caller_job)
    assert final_row["waiting_for"] == ""
    assert final_row["unit"] == "s"


def test_the_merge_is_cleared_even_when_the_wait_fails(monkeypatch):
    """D266's guarantee only holds if a failing load's row is not still
    hidden by a stale `waiting_for` from a wait that has already ended — the
    `finally` around the loop is what this test is pinning down."""
    capability = registry.IMAGE_GENERATION
    model = "org/doomed"
    load_job = supervisor.job_id_for(model)
    caller_job = supervisor.IMAGE_JOB_PREFIX + "waiter2"

    pending = supervisor.Worker(model=model, capability=capability, runner_code="fake",
                                state="error", error="the download exploded")
    with supervisor._lock:
        supervisor._workers[capability] = pending

    monkeypatch.setattr(
        supervisor, "_start_resident",
        lambda m, c: ({"jobId": load_job, "model": m, "state": "error"}, pending),
    )
    monkeypatch.setattr(supervisor, "ready_worker", lambda cap, mdl=None: None)

    row = {"title": "a cat", "model": model, "kind": "task", "cancellable": True,
           "unit": ""}
    jobs.upsert({"id": caller_job, **row, "state": "running"}, server=True)

    with pytest.raises(supervisor.SupervisorError, match="download exploded"):
        supervisor._wait_ready(model, capability, caller_job, row)

    final_row = next(j for j in jobs.list_jobs() if j["id"] == caller_job)
    assert final_row["waiting_for"] == ""
