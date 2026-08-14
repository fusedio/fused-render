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
    state; only the opening report a `fused.trackJob()` handle sends states
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


def test_a_reporter_posting_full_status_cannot_re_raise_a_row_it_aged_out_of():
    """Ageing out is the same statement as a dismissal — this row is over — so
    it needs the same protection from the same late tick.

    A reporter with no `fused.trackJob()` handle to remember it already finished (the
    documented direct-HTTP path: a detached worker POSTing its whole status each
    tick) would otherwise re-create the record the moment it aged out, and again
    every FINISHED_TTL_S after that: a finished download blinking back onto the
    screen every 30 seconds for as long as the worker kept posting.
    """
    jobs.upsert({"id": "w", "title": "Model", "state": "running"}, now=1000.0)
    jobs.upsert({"id": "w", "title": "Model", "state": "done"}, now=1001.0)
    aged = 1001.0 + jobs.FINISHED_TTL_S + 1
    assert jobs.list_jobs(now=aged) == []

    jobs.upsert({"id": "w", "title": "Model", "state": "done"}, now=aged + 0.1)
    assert jobs.list_jobs(now=aged + 0.2) == []

    # ...and the one case that SHOULD bring it back still does: a new run
    # announcing itself.
    jobs.upsert({"id": "w", "title": "Model", "state": "running"}, now=aged + 1)
    assert [r["state"] for r in jobs.list_jobs(now=aged + 1)] == ["running"]


def test_eviction_under_the_cap_does_not_silence_a_live_reporter():
    """Unlike an age-out, eviction is capacity pressure — not a statement that
    the work is over. Forgetting the id would silence its reporter for good,
    since only an opening report reopens a forgotten one and a poll loop sends
    deltas."""
    for i in range(jobs.MAX_JOBS + 1):
        jobs.upsert({"id": f"j{i}", "title": "x"}, now=1000.0 + i * 0.01)
    at = 1000.0 + jobs.MAX_JOBS * 0.01
    assert len(jobs.list_jobs(now=at)) == jobs.MAX_JOBS
    # j0 was evicted; its next ordinary tick puts it back.
    jobs.upsert({"id": "j0", "title": "x", "done": 5}, now=at)
    assert "j0" in {r["id"] for r in jobs.list_jobs(now=at)}


def test_live_SERVER_work_is_never_evicted_by_the_cap():
    """A row describing work the APP is running is a channel, not a cache.

    The cap was sized for a handful of downloads. A queue of transcriptions is
    the case that breaks that assumption — sixty recordings is sixty live
    server rows — and every one of them is simultaneously the queue's state,
    the ✕'s only channel, the progress display and the completion signal the
    page's `watchJob` polls. Evicting one is not "showing less"; it takes the
    ✕ away, and the page reads the absence as the work having stopped.

    So capacity pressure may drop what is FINISHED and what belongs to a page,
    but never live work the server itself is doing. The reporter that would
    have to heal the row cannot heal what the page has already concluded.
    """
    for i in range(jobs.MAX_JOBS * 2):
        jobs.upsert({"id": f"{jobs.SERVER_ID_PREFIX}ai-transcribe:{i}",
                     "title": f"rec{i}.m4a", "state": "running"},
                    server=True, now=1000.0 + i * 0.01)
    at = 1000.0 + jobs.MAX_JOBS * 2 * 0.01
    rows = jobs.list_jobs(now=at)
    assert len(rows) == jobs.MAX_JOBS * 2, "live server rows were evicted"
    assert all(r["owner"] == jobs.OWNER_SERVER for r in rows)


def test_live_server_rows_do_not_push_PAGE_rows_out():
    """The MIXED population, which neither single-population test can reach.

    An exemption is a rule about the RELATIONSHIP between two populations, so
    testing each alone proves nothing about it — and that is exactly the gap
    that let a real bug through: with the eviction COUNT measured over the whole
    dict while the slice was applied to the evictable list, live server rows
    past the cap made the excess exceed the page population and the slice took
    all of it. Seventy transcriptions deleted every page row, and the cap was
    still unmet, so the deletions bought nothing — the harm the exemption exists
    to prevent, relocated onto pages, whose reporters send deltas and whose
    `watchJob` settles null after five misses just the same.
    """
    for i in range(70):
        jobs.upsert({"id": f"{jobs.SERVER_ID_PREFIX}ai-transcribe:{i}",
                     "title": f"rec{i}.m4a", "state": "running"},
                    server=True, now=1000.0 + i * 0.01)
    for i in range(3):
        jobs.upsert({"id": f"pagejob{i}", "title": "my export", "state": "running"},
                    now=1000.7 + i * 0.01)
    at = 1001.0

    rows = jobs.list_jobs(now=at)
    page = [r for r in rows if r["owner"] == jobs.OWNER_PAGE]
    assert len(page) == 3, "live server work evicted the page's own rows"
    assert len(rows) == 73


def test_the_cap_still_bites_on_page_owned_rows():
    """Page-owned rows stay evictable, and that is the asymmetry that makes the
    exemption safe: a server row is minted only by this app's own code and is
    bounded by work actually in flight, while `fused.trackJob()` lets any page
    open as many as it likes and never finish them. An exemption for those
    would be an unbounded dict behind an HTTP call."""
    for i in range(jobs.MAX_JOBS + 10):
        jobs.upsert({"id": f"page{i}", "title": "x", "state": "running"},
                    now=1000.0 + i * 0.01)
    at = 1000.0 + (jobs.MAX_JOBS + 10) * 0.01
    assert len(jobs.list_jobs(now=at)) == jobs.MAX_JOBS


def test_a_watcher_SEES_THE_OUTCOME_of_a_job_that_finishes_under_a_full_queue():
    """The consequence of exempting rows from eviction while still COUNTING
    them toward the budget: the pressure lands on whatever just finished.

    Over the cap with 64+ live server rows, a terminal row was the only
    evictable candidate left, so it was deleted on the very next `list_jobs()`
    — which is the same read `fused.watchJob` polls. A watcher therefore never
    observed the outcome. A success is still recoverable from the artefact on
    disk; a FAILURE or a CANCEL has no artefact, so the page reported the
    generic "no longer being reported" instead of the real reason, on exactly
    the large queue the exemption exists to support.

    Asserted on the OUTCOME rather than on `len(_jobs)`, because the row count
    is what made this look fine: the list was the right length throughout.
    """
    for i in range(jobs.MAX_JOBS + 6):
        jobs.upsert({"id": f"{jobs.SERVER_ID_PREFIX}ai-transcribe:{i}",
                     "title": f"rec{i}.m4a", "state": "running"},
                    server=True, now=1000.0 + i * 0.01)
    at = 1000.0 + (jobs.MAX_JOBS + 6) * 0.01

    failed = f"{jobs.SERVER_ID_PREFIX}ai-transcribe:5"
    jobs.upsert({"id": failed, "title": "rec5.m4a", "state": "error",
                 "message": "the decoder exploded"}, server=True, now=at)
    seen = {r["id"]: r for r in jobs.list_jobs(now=at)}.get(failed)
    assert seen is not None, "the terminal row was evicted before any watcher saw it"
    assert seen["state"] == "error" and "exploded" in seen["message"]

    # A cancel has no artefact either, and is the other outcome a page can only
    # learn from the row.
    stopped = f"{jobs.SERVER_ID_PREFIX}ai-transcribe:6"
    jobs.upsert({"id": stopped, "title": "rec6.m4a", "state": "cancelled"},
                server=True, now=at)
    seen = {r["id"]: r for r in jobs.list_jobs(now=at)}.get(stopped)
    assert seen is not None and seen["state"] == "cancelled"


def test_finished_server_rows_are_still_capped():
    """The exemption is for LIVE work only. A server row that has finished is
    as evictable as any other, or a day of completed transcriptions would grow
    the list without bound — and the cap counting only what it can shed must
    not be read as the cap no longer applying to what it can.

    `MAX_JOBS + 2` finished rows is what produces the pressure now; the live
    rows alongside are not counted, which is the point of the previous test.
    """
    for i in range(jobs.MAX_JOBS + 2):
        jobs.upsert({"id": f"{jobs.SERVER_ID_PREFIX}ai-transcribe:old{i}",
                     "title": "x", "state": "done"},
                    server=True, now=1000.0 + i * 0.01)
    at = 1000.0 + (jobs.MAX_JOBS + 2) * 0.01
    for i in range(5):
        jobs.upsert({"id": f"{jobs.SERVER_ID_PREFIX}ai-transcribe:live{i}",
                     "title": "rec.m4a", "state": "running"}, server=True, now=at)

    ids = {r["id"] for r in jobs.list_jobs(now=at)}
    # The two oldest finished rows paid; every live row survived.
    assert f"{jobs.SERVER_ID_PREFIX}ai-transcribe:old0" not in ids
    assert f"{jobs.SERVER_ID_PREFIX}ai-transcribe:old1" not in ids
    assert all(f"{jobs.SERVER_ID_PREFIX}ai-transcribe:live{i}" in ids for i in range(5))
    # Finished rows are held at the cap, so the total is the cap plus live work.
    assert len(ids) == jobs.MAX_JOBS + 5


def test_a_stale_server_row_is_still_dropped_by_the_AGE_sweep():
    """The exemption is from CAPACITY pressure, not from death. A reporter that
    stopped reporting for `STALE_DROP_S` is gone whatever it claimed to be, or
    a crashed worker's row would sit on the screen for the session."""
    jobs.upsert({"id": f"{jobs.SERVER_ID_PREFIX}ai-transcribe:zombie",
                 "title": "x", "state": "running"}, server=True, now=1000.0)
    assert jobs.list_jobs(now=1000.0 + jobs.STALE_DROP_S + 1) == []


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


def test_the_bridge_exposes_track_job_on_window_fused():
    """`trackJob`, not `job` (D244): a page does not START work through this
    bridge, it reports on work it started itself — and `fused.job(...)` read as
    the job itself rather than as the handle for describing one.

    The rule survived local inference (SPEC §40) rather than being flipped by it.
    A page CAN now observe work it did not start — a model download the server
    runs — but that arrived as `watchJob`, trackJob's sibling, so the bare noun
    is still not the API: TRACK takes a spec and creates a row, WATCH takes an id
    and looks at one.
    """
    runtime = open(
        os.path.join(REPO_ROOT, "fused_render", "static", "runtime.js"), encoding="utf-8"
    ).read()
    api = runtime.split("window.fused = {", 1)[1].split("};", 1)[0]
    assert "\n    trackJob,\n" in api
    assert "\n    watchJob,\n" in api
    assert "\n    job,\n" not in api
