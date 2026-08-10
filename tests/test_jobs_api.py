"""The background-job registry (fused_render/jobs.py + routers/jobs.py) — the
model behind the shell's download manager (SPEC §36, D244).

What is actually at stake here is honesty about work the server cannot see. It
does not run the download, does not know which process is doing it, and cannot
tell "finished" from "the page that was reporting got closed" — so the tests
below are mostly about the states that distinction produces: stalled vs
running, a cancel that is a REQUEST rather than a kill, a dismissed row that
must not come back, and an error that outlives the 30s every other outcome
gets.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import jobs
from fused_render.server import create_app

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is process-global (one app, one list) — empty it per test."""
    jobs.reset()
    yield
    jobs.reset()


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def report(client, **body):
    return client.post("/api/jobs", json=body, headers={"X-Fused": "1"})


def listing(client):
    res = client.get("/api/jobs")
    assert res.status_code == 200
    return res.json()["jobs"]


# ------------------------------------------------------------------ reporting


def test_first_report_creates_a_row_and_later_ones_update_it(client):
    report(client, id="a", title="FLUX.2-klein-4B", kind="download", unit="bytes",
           total=8_000_000_000, done=0, cancellable=True)
    report(client, id="a", done=1_200_000_000, detail="transformer.gguf")

    rows = listing(client)
    assert len(rows) == 1
    row = rows[0]
    # The tick carried only done+detail; everything the FIRST report set has to
    # survive it, or a progress update would blank the row's own title.
    assert row["title"] == "FLUX.2-klein-4B"
    assert row["kind"] == "download"
    assert row["unit"] == "bytes"
    assert row["cancellable"] is True
    assert row["done"] == 1_200_000_000
    assert row["total"] == 8_000_000_000
    assert row["detail"] == "transformer.gguf"
    assert row["state"] == "running"
    assert row["stalled"] is False


def test_a_report_answers_with_the_stored_record(client):
    """The reply is the cancel channel — see `request_cancel`."""
    res = report(client, id="a", title="t")
    assert res.status_code == 200
    assert res.json()["id"] == "a"
    assert res.json()["cancel_requested"] is False


def test_the_first_report_must_name_the_job(client):
    res = report(client, id="a", done=5)
    assert res.status_code == 400
    assert "title" in res.json()["error"]
    assert listing(client) == []


def test_a_malformed_id_is_refused_rather_than_sanitised(client):
    for bad in ["", "a/b", "a b", "x" * 129, "a?b"]:
        res = report(client, id=bad, title="t")
        assert res.status_code == 400, bad
    assert listing(client) == []


def test_a_page_attributes_its_own_rows_through_the_header(client):
    client.post(
        "/api/jobs",
        json={"id": "a", "title": "t"},
        headers={"X-Fused": "1", "X-Fused-Page": "/tmp/my%20app/index.html"},
    )
    # Percent-decoded like every other X-Fused-* path header.
    assert listing(client)[0]["page"] == "/tmp/my app/index.html"


def test_a_non_finite_number_is_refused_not_painted(client):
    """`n / total` with total 0 gives inf; drawing it would be a confident bar
    built from the reporter's bug."""
    res = client.post(
        "/api/jobs",
        headers={"X-Fused": "1", "Content-Type": "application/json"},
        content=json.dumps({"id": "a", "title": "t", "done": float("inf")}),
    )
    assert res.status_code == 400
    assert "finite" in res.json()["error"]


def test_writes_need_the_x_fused_guard(client):
    assert client.post("/api/jobs", json={"id": "a", "title": "t"}).status_code == 403
    assert client.post("/api/jobs/a/cancel").status_code == 403
    assert client.post("/api/jobs/a/dismiss").status_code == 403
    assert client.post("/api/jobs/clear").status_code == 403
    # Reading is not a write: the shell's own GET carries no header.
    assert client.get("/api/jobs").status_code == 200


# ------------------------------------------------------------------- ordering


def test_rows_come_back_oldest_first_so_they_never_reorder(client):
    # A new row appends at the BOTTOM of the column, nearest the screen edge
    # the eye is already on — and no existing row moves under the pointer.
    for name in ["first", "second", "third"]:
        report(client, id=name, title=name)
    assert [r["id"] for r in listing(client)] == ["first", "second", "third"]


def test_the_listing_carries_the_servers_clock(client):
    report(client, id="a", title="t")
    body = client.get("/api/jobs").json()
    assert body["now"] >= body["jobs"][0]["started_at"]


# --------------------------------------------------------------- cancellation


def test_cancel_is_a_request_the_reporter_reads_back(client):
    report(client, id="a", title="t", cancellable=True)

    res = client.post("/api/jobs/a/cancel", headers={"X-Fused": "1"})
    assert res.status_code == 200
    # Still RUNNING: the work has not stopped just because the shell asked.
    assert res.json()["state"] == "running"
    assert res.json()["cancel_requested"] is True

    # The reporter learns about it in the reply to the tick it was already
    # going to send — no second channel.
    assert report(client, id="a", done=1).json()["cancel_requested"] is True

    # ...and says so when it has actually stopped.
    assert report(client, id="a", state="cancelled").json()["state"] == "cancelled"


def test_cancelling_a_row_that_is_gone_says_so(client):
    res = client.post("/api/jobs/nope/cancel", headers={"X-Fused": "1"})
    assert res.status_code == 404


def test_reaching_a_terminal_state_spends_the_cancel_request(client):
    report(client, id="a", title="t", cancellable=True)
    client.post("/api/jobs/a/cancel", headers={"X-Fused": "1"})
    row = report(client, id="a", state="done").json()
    # Otherwise the finished row keeps its Cancel button lit.
    assert row["cancel_requested"] is False


# ------------------------------------------------------------------ dismissal


def test_a_finished_row_can_be_dismissed_and_a_live_one_cannot(client):
    report(client, id="run", title="running")
    report(client, id="fin", title="finished", state="done")

    assert client.post("/api/jobs/fin/dismiss", headers={"X-Fused": "1"}).status_code == 200
    # Hiding a live download would put the app back in exactly the state this
    # feature exists to fix.
    res = client.post("/api/jobs/run/dismiss", headers={"X-Fused": "1"})
    assert res.status_code == 409
    assert [r["id"] for r in listing(client)] == ["run"]


def test_a_stalled_row_can_be_dismissed():
    """Not a softening of the rule above but the same rule: nobody is reporting
    on it, so the row hides nothing the app could otherwise say — it IS the app
    saying it stopped knowing. The user closing it usually knows exactly what it
    was, because they closed the page."""
    jobs.upsert({"id": "gone", "title": "abandoned"}, now=1000.0)
    at = 1000.0 + jobs.STALE_AFTER_S + 1
    assert jobs.list_jobs(now=at)[0]["stalled"] is True
    assert jobs.dismiss("gone", now=at) is True
    assert jobs.list_jobs(now=at) == []


def test_a_dismissed_row_does_not_come_back_on_a_late_tick(client):
    report(client, id="a", title="t", state="done")
    client.post("/api/jobs/a/dismiss", headers={"X-Fused": "1"})
    # A poll loop that ran one more time after its job finished. Answered 200 —
    # a reporter mid-loop must not start erroring — but nothing is stored.
    assert report(client, id="a", title="t", done=5).status_code == 200
    assert listing(client) == []


def test_a_dismissal_silences_late_ticks_but_not_a_fresh_start():
    """What a dismissal refuses is a LATE TICK, never a new job.

    Refusing the id outright would break the documented pattern of reusing a
    STABLE id so a reloaded page re-attaches to its own row — the id would be
    dead the first time anyone dismissed it. A tick is a delta or a terminal
    state; only the opening report a `fused.job()` handle sends states
    `running` outright, which is what tells the two apart.
    """
    jobs.upsert({"id": "flux:job", "title": "run one", "state": "running"}, now=1000.0)
    jobs.upsert({"id": "flux:job", "state": "done"}, now=1001.0)
    jobs.dismiss("flux:job", now=1001.0)

    # The poll loop of the run that just ended, still going: refused, however
    # long it keeps at it.
    jobs.upsert({"id": "flux:job", "done": 5}, now=1002.0)
    jobs.upsert({"id": "flux:job", "state": "error", "message": "late"}, now=1600.0)
    assert jobs.list_jobs(now=1600.0) == []

    # A new run announcing itself. Same name, different job — it gets its row.
    jobs.upsert({"id": "flux:job", "title": "run two", "state": "running"}, now=1003.0)
    assert [r["title"] for r in jobs.list_jobs(now=1003.0)] == ["run two"]


def test_clear_takes_the_finished_rows_and_leaves_the_running_ones(client):
    report(client, id="run", title="a")
    report(client, id="ok", title="b", state="done")
    report(client, id="bad", title="c", state="error", message="boom")

    res = client.post("/api/jobs/clear", headers={"X-Fused": "1"})
    assert res.json() == {"cleared": 2}
    assert [r["id"] for r in listing(client)] == ["run"]


# ---------------------------------------------------------------- the sweeper


def test_a_finished_row_ages_out_but_an_error_waits_to_be_read():
    jobs.upsert({"id": "ok", "title": "a", "state": "done"}, now=1000.0)
    jobs.upsert({"id": "bad", "title": "b", "state": "error", "message": "boom"}, now=1000.0)

    still_there = {r["id"] for r in jobs.list_jobs(now=1000.0 + jobs.FINISHED_TTL_S - 1)}
    assert still_there == {"ok", "bad"}

    # An error is the one outcome the user may have to act on, so it stays
    # until dismissed — the persistent-error toast's rule.
    later = {r["id"] for r in jobs.list_jobs(now=1000.0 + jobs.FINISHED_TTL_S + 1)}
    assert later == {"bad"}


def test_a_reporter_that_went_quiet_reads_as_stalled_then_disappears():
    jobs.upsert({"id": "a", "title": "t"}, now=1000.0)

    assert jobs.list_jobs(now=1000.0 + jobs.STALE_AFTER_S - 1)[0]["stalled"] is False
    # Its page was closed mid-download. The work is probably still running —
    # which is why the row stays and is merely marked, not deleted.
    assert jobs.list_jobs(now=1000.0 + jobs.STALE_AFTER_S + 1)[0]["stalled"] is True
    # A late tick un-stalls it without any timer having to fire.
    jobs.upsert({"id": "a", "done": 5}, now=1000.0 + jobs.STALE_AFTER_S + 2)
    assert jobs.list_jobs(now=1000.0 + jobs.STALE_AFTER_S + 3)[0]["stalled"] is False


def test_a_dead_reporter_cannot_wedge_the_list_for_the_session():
    jobs.upsert({"id": "a", "title": "t"}, now=1000.0)
    assert jobs.list_jobs(now=1000.0 + jobs.STALE_DROP_S + 1) == []


def test_over_the_cap_the_live_work_is_what_survives():
    # Finished rows go first, then the least recently updated — a running
    # download is the last thing evicted. All stamped inside one FINISHED_TTL_S
    # window so it is the CAP being exercised here and not the age sweep.
    for i in range(jobs.MAX_JOBS):
        jobs.upsert({"id": f"done{i}", "title": "x", "state": "done"}, now=1000.0 + i * 0.01)
    at = 1000.0 + jobs.MAX_JOBS * 0.01
    jobs.upsert({"id": "live", "title": "downloading"}, now=at)

    ids = [r["id"] for r in jobs.list_jobs(now=at)]
    assert len(ids) == jobs.MAX_JOBS
    assert "live" in ids
    assert "done0" not in ids  # the oldest finished row is the one that went


# ------------------------------------------------------- wiring the two halves


def test_progress_ticks_stay_out_of_the_call_log():
    """A tick is bookkeeping ABOUT a call, not a call. ~160 of them per
    four-minute download would crowd out the runs they annotate."""
    from fused_render import calls

    assert "/api/jobs".startswith(calls.SKIP_PREFIXES)


def test_the_runtime_and_the_shell_agree_on_the_ping_key():
    """runtime.js writes the key; frontend/lib/jobs.ts listens for it. They are
    in two languages with no shared module, so the literal is pinned here."""
    runtime = open(
        os.path.join(REPO_ROOT, "fused_render", "static", "runtime.js"), encoding="utf-8"
    ).read()
    shell = open(
        os.path.join(REPO_ROOT, "frontend", "src", "platform", "lib", "jobs.ts"),
        encoding="utf-8",
    ).read()
    key = '"fused-render:jobs-ping"'
    assert key in runtime
    assert key in shell


def test_the_bridge_exposes_job_on_window_fused():
    runtime = open(
        os.path.join(REPO_ROOT, "fused_render", "static", "runtime.js"), encoding="utf-8"
    ).read()
    api = runtime.split("window.fused = {", 1)[1].split("};", 1)[0]
    assert "\n    job,\n" in api
