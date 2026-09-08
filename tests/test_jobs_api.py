"""The background-job registry (fused_render/jobs.py + routers/jobs.py) — the
model behind the shell's download manager (SPEC §36, D244).

What is actually at stake here is honesty about work the server cannot see. It
does not run the download, does not know which process is doing it, and cannot
tell "finished" from "the page that was reporting got closed" — so the tests
below are mostly about the states that distinction produces: stalled vs
running, a cancel that is a REQUEST rather than a kill, a dismissed row that
must not come back, a finished row whose retention clock starts at first READ
rather than at completion (so a row born and finished with nobody watching is
not swept before anyone could see it), and an error that outlives every other
outcome's retention entirely.
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


def read_jobs(*, now=None):
    """Simulates the shell's `GET /api/jobs` — the ONE call allowed to start a
    finished row's retention clock. Most of this file calls `jobs.list_jobs`
    directly (rather than through `client`) so a test can control `now`
    precisely; `jobs.list_jobs` itself defaults `mark_read` to False because
    it has internal callers too (`supervisor._cancel_state`'s poll,
    `capture._cancel_requested`'s) that must never start that clock — see
    that function's own docstring. This wrapper is what makes a direct
    `jobs.list_jobs` call in a test actually model the client read it is
    standing in for.
    """
    return jobs.list_jobs(now=now, mark_read=True)


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


def test_total_scope_defaults_to_phase_and_a_download_plan_can_claim_the_whole(client):
    """SPEC AI-5n/D498: a bare download report never claims to be the whole
    download — `total_scope` defaults to "phase", the honest reading of a
    plain `download_snapshot` call that only ever knows its own repo. A
    reporter that DOES know the whole shape (`download_plan`) says so
    explicitly, and that claim survives a later tick that omits it — same
    "only present keys apply" rule every other field on this row gets."""
    report(client, id="a", title="LTX-2.3 int4", kind="download", unit="bytes",
           total=19_100_000_000, done=0)
    row = listing(client)[0]
    assert row["total_scope"] == "phase"

    report(client, id="a", total_scope="download", total=27_170_000_000)
    row = listing(client)[0]
    assert row["total_scope"] == "download"

    # A later tick that says nothing about scope must not silently revert it.
    report(client, id="a", done=6_000_000_000)
    row = listing(client)[0]
    assert row["total_scope"] == "download"


def test_total_scope_rejects_anything_outside_the_closed_set(client):
    res = report(client, id="a", title="x", total_scope="whole-repo")
    assert res.status_code == 400


def test_waiting_for_round_trips_on_a_server_upsert():
    """`waiting_for` is how `_wait_ready` merges a caller's row onto the model
    load it is blocked on (SPEC §36) — a server report naming another row's id
    must reach the listing verbatim, and clearing it (an empty value on the
    report that ends the wait) must reach the listing as "", not linger."""
    jobs.upsert({"id": "a", "title": "a cat", "waiting_for": "sys:ai-model:x"},
                server=True)
    row = jobs.list_jobs()[0]
    assert row["waiting_for"] == "sys:ai-model:x"

    jobs.upsert({"id": "a", "waiting_for": ""}, server=True)
    row = jobs.list_jobs()[0]
    assert row["waiting_for"] == ""


def test_a_page_owned_report_cannot_set_waiting_for(client):
    """A page could otherwise blank a live download's only row by falsely
    claiming to be waiting on it — see `Job.waiting_for`'s own comment. The
    field is silently dropped rather than rejected, same as `owner`."""
    report(client, id="a", title="a cat", waiting_for="sys:ai-model:x")
    assert listing(client)[0]["waiting_for"] == ""


def test_waiting_for_rejects_an_illegal_id():
    """The value still has to be a legal id — see `clean_id` — so a page (or a
    bug in the server-side reporter) cannot smuggle something the manager's
    lookup would choke on."""
    with pytest.raises(jobs.JobError):
        jobs.upsert({"id": "a", "title": "a cat", "waiting_for": "not a legal id"},
                    server=True)


def test_model_is_its_own_field_separate_from_title_and_detail(client):
    """The model must reach the client as its OWN value, not folded into
    `title` or `detail` — the UI dims it as a distinct element on the title
    row (JobRow's `.dl-model`) and would have nothing to key that styling off
    of if the model were just concatenated text. A tick that omits `model`
    must not blank one a previous tick set, same as `title`."""
    report(client, id="a", title="a red fox in snow", model="FLUX.1-schnell")
    report(client, id="a", detail="Denoising — step 2/4 · ~2s left")

    row = listing(client)[0]
    assert row["title"] == "a red fox in snow"
    assert row["model"] == "FLUX.1-schnell"
    assert row["detail"] == "Denoising — step 2/4 · ~2s left"


def test_model_defaults_to_empty_string_when_never_reported(client):
    """A download, a scheduled run, or a page's own `fused.trackJob()` never
    sends `model` at all — the row must render as it always has, which means
    an empty string (falsy in the client), never a missing key or None."""
    report(client, id="a", title="t")
    assert listing(client)[0]["model"] == ""


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


def test_clear_cancel_requested_disowns_a_stale_flag_but_not_state(client):
    """A caller opening a NEW attempt under a reused job id (envinstall's
    mirror thread is the one that does this) must be able to disown a flag a
    previous attempt's dead reporter never got to clear, without touching
    anything else `upsert`'s body has no key for."""
    report(client, id="a", title="t", cancellable=True)
    client.post("/api/jobs/a/cancel", headers={"X-Fused": "1"})
    assert jobs.list_jobs()[0]["cancel_requested"] is True

    row = jobs.clear_cancel_requested("a")
    assert row["cancel_requested"] is False
    assert row["state"] == "running"  # unrelated to the flag it disowned

    # A fresh ✕ after that still cancels normally.
    res = client.post("/api/jobs/a/cancel", headers={"X-Fused": "1"})
    assert res.json()["cancel_requested"] is True


def test_clear_cancel_requested_on_a_gone_row_says_so():
    assert jobs.clear_cancel_requested("nope") is None


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
    assert read_jobs(now=at)[0]["stalled"] is True
    assert jobs.dismiss("gone", now=at) is True
    assert read_jobs(now=at) == []


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
    assert read_jobs(now=1600.0) == []

    # A new run announcing itself. Same name, different job — it gets its row.
    jobs.upsert({"id": "flux:job", "title": "run two", "state": "running"}, now=1003.0)
    assert [r["title"] for r in read_jobs(now=1003.0)] == ["run two"]


def test_clear_takes_the_finished_rows_and_leaves_the_running_ones(client):
    report(client, id="run", title="a")
    report(client, id="ok", title="b", state="done")
    report(client, id="bad", title="c", state="error", message="boom")

    res = client.post("/api/jobs/clear", headers={"X-Fused": "1"})
    assert res.json() == {"cleared": 2}
    assert [r["id"] for r in listing(client)] == ["run"]


def test_clear_does_not_take_a_waiting_row(client):
    """`clear_finished` used to take any row where `state != RUNNING`, which
    includes `WAITING` — a row sitting in front of an open, answerable
    question (uv's "Install anyway" compile prompt), not finished work.
    Notifications' own "Clear" button (`RepoUpdatesDock.tsx`) is gated on and
    titled around TERMINAL rows only, so a `WAITING` row was reachable by a
    control that neither shows it nor counts it toward its own gate — and
    `_forget` blocks the id from being re-created by a later tick, so the
    open prompt could never come back either. `clear_finished` must survive
    scoped to `TERMINAL_STATES` so it only ever takes what it claims to."""
    jobs.upsert({"id": "prompt", "title": "compile foolib", "state": jobs.WAITING,
                 "message": "waiting for your approval to compile foolib"}, now=1000.0)
    report(client, id="ok", title="b", state="done")
    report(client, id="bad", title="c", state="error", message="boom")

    res = client.post("/api/jobs/clear", headers={"X-Fused": "1"})
    assert res.json() == {"cleared": 2}
    assert [r["id"] for r in listing(client)] == ["prompt"]


def test_clear_does_not_sweep_a_stalled_but_still_running_row():
    """`clear_finished` used to take any record where `state != RUNNING` OR
    `is_stalled(...)` — so a Clear press swept a RUNNING job whose reporter
    had merely gone quiet for `STALE_AFTER_S`, which a long model load, a
    slow generation between report ticks, or a throttled background tab all
    satisfy. The work itself does not stop (`ai/supervisor._cancel_state`
    returns None, read as "not cancelled", for a missing row) — only the
    RECORD of it does, and the page's `fused.watchJob` reads a missing row
    as work that stopped, telling the user their AI job was cancelled by a
    button that never touched it. `clear_finished` now takes terminal
    records only; a stalled row is still reachable one at a time through its
    own ✕ (`dismiss`, unchanged — see `test_a_stalled_row_can_be_dismissed`
    above), by a user who usually knows what that row was."""
    jobs.upsert({"id": "stalled", "title": "long load", "state": "running"}, now=1000.0)
    jobs.upsert({"id": "done", "title": "finished", "state": "done"}, now=1000.0)
    at = 1000.0 + jobs.STALE_AFTER_S + 1
    assert jobs.list_jobs(now=at)[0]["id"] in ("stalled", "done")  # sanity: both present
    assert any(r["stalled"] for r in jobs.list_jobs(now=at) if r["id"] == "stalled")

    cleared = jobs.clear_finished(now=at)

    assert cleared == 1
    remaining = [r["id"] for r in jobs.list_jobs(now=at)]
    assert remaining == ["stalled"]


def test_dismiss_still_takes_one_stalled_row_at_a_time():
    """The per-row ✕ stays exactly as permissive as before — only the BULK
    sweep (`clear_finished`) changed. A user closing one specific stalled row
    usually knows what it was; a bulk Clear does not know what any of its
    rows are."""
    jobs.upsert({"id": "stalled", "title": "long load", "state": "running"}, now=1000.0)
    at = 1000.0 + jobs.STALE_AFTER_S + 1

    assert jobs.dismiss("stalled", now=at) is True
    assert jobs.list_jobs(now=at) == []


# ---------------------------------------------------------------- the sweeper


def test_a_done_row_now_stays_until_dismissed_same_as_an_error(client):
    """D586, broadened by D662: every terminal job — not only `error` — now
    routes to the shell's Notifications list, which is meant to hold a log,
    not a "what's happening right now" snapshot. A `done`/`cancelled` row
    used to age out `FINISHED_TTL_S` after its first read; that would delete
    the very entry Notifications exists to keep, out from under a user who
    had not yet looked. Both now get the same unconditional exemption
    `error` already had."""
    jobs.upsert({"id": "ok", "title": "a", "state": "done"}, now=1000.0)
    jobs.upsert({"id": "bad", "title": "b", "state": "error", "message": "boom"}, now=1000.0)
    jobs.upsert({"id": "stopped", "title": "c", "state": "cancelled"}, now=1000.0)

    first_read = {r["id"] for r in read_jobs(now=1000.0)}
    assert first_read == {"ok", "bad", "stopped"}

    later = {r["id"] for r in read_jobs(now=1000.0 + jobs.FINISHED_TTL_S + 1)}
    assert later == {"ok", "bad", "stopped"}, "no terminal state ages out on its own any more"


def test_an_unread_done_row_outlives_the_unread_backstop_too():
    """`FINISHED_UNREAD_DROP_S` was the ceiling for an unread terminal row
    while `done`/`cancelled` still aged out on the read-gated clock — now
    that they get the same unconditional exemption `error` already had,
    neither backstop applies to them at all; `MAX_JOBS` is what bounds them."""
    jobs.upsert({"id": "ok", "title": "a", "state": "done"}, now=1000.0)
    assert read_jobs(now=1000.0 + jobs.FINISHED_UNREAD_DROP_S + 1) != []


def test_an_unread_error_outlives_even_the_unread_backstop():
    """`error`'s exemption is unconditional — `_sweep` `continue`s on it before
    either the read-gated clock or the unread backstop is even considered — so
    an error nobody has read yet must survive well past `FINISHED_UNREAD_DROP_S`
    too, not just past `FINISHED_TTL_S`."""
    jobs.upsert({"id": "bad", "title": "b", "state": "error", "message": "boom"}, now=1000.0)
    assert read_jobs(now=1000.0 + jobs.FINISHED_UNREAD_DROP_S + 1) != []


def test_an_unread_waiting_row_outlives_even_the_unread_backstop():
    """`WAITING` gets the same unconditional exemption as `error` (`_sweep`'s
    retention loop `continue`s on `job.state in ("error", WAITING)` before
    either retention clock is even considered): a row sitting on uv's
    "Install anyway" question must not vanish from the dock while the
    question is still open, no matter how long nobody has looked at it."""
    jobs.upsert({"id": "q", "title": "b", "state": jobs.WAITING,
                 "message": "waiting for your approval to compile foolib"}, now=1000.0)
    assert read_jobs(now=1000.0 + jobs.FINISHED_UNREAD_DROP_S + 1) != []


def test_a_waiting_row_and_a_done_row_both_survive_the_finished_ttl():
    jobs.upsert({"id": "ok", "title": "a", "state": "done"}, now=1000.0)
    jobs.upsert({"id": "q", "title": "b", "state": jobs.WAITING,
                 "message": "waiting for your approval to compile foolib"}, now=1000.0)

    first_read = {r["id"] for r in read_jobs(now=1000.0)}
    assert first_read == {"ok", "q"}

    later = {r["id"] for r in read_jobs(now=1000.0 + jobs.FINISHED_TTL_S + 1)}
    assert later == {"ok", "q"}, "a WAITING row and a done row both stay, exactly like error does"


def test_request_cancel_does_nothing_to_a_waiting_row(client):
    """`request_cancel` stays guarded on `RUNNING` alone: a `WAITING` row's
    reporter (the worker process) has already exited, so there is nobody
    left to signal — the dock's ✕ against a `WAITING` row goes through
    `dismiss` instead, exactly like it already does for `done`/`cancelled`."""
    jobs.upsert({"id": "q", "title": "b", "state": jobs.WAITING, "cancellable": True,
                 "message": "waiting for your approval to compile foolib"}, server=True)

    res = client.post("/api/jobs/q/cancel", headers={"X-Fused": "1"})
    assert res.status_code == 200
    row = res.json()
    assert row["state"] == jobs.WAITING
    assert row["cancel_requested"] is False, (
        "a WAITING row has nothing running to signal a cancel to"
    )


def test_a_waiting_row_can_be_dismissed_like_a_finished_one(client):
    jobs.upsert({"id": "q", "title": "b", "state": jobs.WAITING, "cancellable": True,
                 "message": "waiting for your approval to compile foolib"}, server=True)

    res = client.post("/api/jobs/q/dismiss", headers={"X-Fused": "1"})
    assert res.status_code == 200
    assert listing(client) == []


# `test_an_internal_caller_listing_jobs_does_not_start_the_retention_clock` is
# deleted rather than rewritten (D662): its whole premise was that a `done`
# row's retention clock (`first_read_at` / `FINISHED_TTL_S`) must not be
# started by an internal, non-`mark_read` `list_jobs()` poll — but a `done`
# row no longer runs that clock at all (`_sweep` keeps every terminal state
# until dismissed; see its docstring). The read-gate machinery this test
# pinned (`first_read_at`, `FINISHED_TTL_S`) is left in `jobs.py` because
# `mark_read` still has other callers with their own reasons, but no
# reachable job state exercises it any more, so there is nothing left for
# this test to prove.


def test_a_reporter_that_went_quiet_reads_as_stalled_then_disappears():
    jobs.upsert({"id": "a", "title": "t"}, now=1000.0)

    assert read_jobs(now=1000.0 + jobs.STALE_AFTER_S - 1)[0]["stalled"] is False
    # Its page was closed mid-download. The work is probably still running —
    # which is why the row stays and is merely marked, not deleted.
    assert read_jobs(now=1000.0 + jobs.STALE_AFTER_S + 1)[0]["stalled"] is True
    # A late tick un-stalls it without any timer having to fire.
    jobs.upsert({"id": "a", "done": 5}, now=1000.0 + jobs.STALE_AFTER_S + 2)
    assert read_jobs(now=1000.0 + jobs.STALE_AFTER_S + 3)[0]["stalled"] is False


def test_a_reporter_posting_full_status_cannot_re_raise_a_row_it_aged_out_of():
    """Ageing out is the same statement as a dismissal — this row is over — so
    it needs the same protection from the same late tick.

    A reporter with no `fused.trackJob()` handle to remember it already finished (the
    documented direct-HTTP path: a detached worker POSTing its whole status each
    tick) would otherwise re-create the record the moment it aged out, and again
    every tick after that: a finished download blinking back onto the screen
    every few seconds for as long as the worker kept posting.

    A `done` row no longer ages out at all (D662: every terminal state is kept
    until dismissed), so the still-reachable age-out this exercises is a
    RUNNING row going stale (`STALE_DROP_S`, its reporter gone silent) — the
    dismissal protection it proves (`_forget` / `_dismissed`, in `upsert`) is
    the same one either age-out path funnels through.
    """
    jobs.upsert({"id": "w", "title": "Model", "state": "running"}, now=1000.0)
    aged = 1000.0 + jobs.STALE_DROP_S + 1
    assert read_jobs(now=aged) == []

    # A late progress tick from the same dead reporter must not re-raise the
    # row it was just dismissed from...
    jobs.upsert({"id": "w", "title": "Model", "done": 5}, now=aged + 0.1)
    assert read_jobs(now=aged + 0.2) == []

    # ...and the one case that SHOULD bring it back still does: a new run
    # announcing itself.
    jobs.upsert({"id": "w", "title": "Model", "state": "running"}, now=aged + 1)
    assert [r["state"] for r in read_jobs(now=aged + 1)] == ["running"]


def test_eviction_under_the_cap_does_not_silence_a_live_reporter():
    """Unlike an age-out, eviction is capacity pressure — not a statement that
    the work is over. Forgetting the id would silence its reporter for good,
    since only an opening report reopens a forgotten one and a poll loop sends
    deltas."""
    for i in range(jobs.MAX_JOBS + 1):
        jobs.upsert({"id": f"j{i}", "title": "x"}, now=1000.0 + i * 0.01)
    at = 1000.0 + jobs.MAX_JOBS * 0.01
    assert len(read_jobs(now=at)) == jobs.MAX_JOBS
    # j0 was evicted; its next ordinary tick puts it back.
    jobs.upsert({"id": "j0", "title": "x", "done": 5}, now=at)
    assert "j0" in {r["id"] for r in read_jobs(now=at)}


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
    rows = read_jobs(now=at)
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

    rows = read_jobs(now=at)
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
    assert len(read_jobs(now=at)) == jobs.MAX_JOBS


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
    seen = {r["id"]: r for r in read_jobs(now=at)}.get(failed)
    assert seen is not None, "the terminal row was evicted before any watcher saw it"
    assert seen["state"] == "error" and "exploded" in seen["message"]

    # A cancel has no artefact either, and is the other outcome a page can only
    # learn from the row.
    stopped = f"{jobs.SERVER_ID_PREFIX}ai-transcribe:6"
    jobs.upsert({"id": stopped, "title": "rec6.m4a", "state": "cancelled"},
                server=True, now=at)
    seen = {r["id"]: r for r in read_jobs(now=at)}.get(stopped)
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

    ids = {r["id"] for r in read_jobs(now=at)}
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
    assert read_jobs(now=1000.0 + jobs.STALE_DROP_S + 1) == []


def test_a_dead_reporter_cannot_wedge_the_list_for_the_session():
    jobs.upsert({"id": "a", "title": "t"}, now=1000.0)
    assert read_jobs(now=1000.0 + jobs.STALE_DROP_S + 1) == []


def test_a_long_open_waiting_row_is_not_the_first_thing_evicted():
    """`evictable`'s sort key, `(state == RUNNING, updated_at)`, puts every
    non-running row first and orders each group oldest-`updated_at`-first. A
    `WAITING` row's reporter has already exited (the worker that raised the
    question is gone), so its `updated_at` never advances again — under that
    key alone it becomes the single OLDEST-updated evictable row, the very
    first thing the cap drops once the registry fills. That is exactly the
    outcome the `WAITING` sweep exemption exists to prevent: a long-open
    "Install anyway" prompt disappearing under capacity pressure from an
    unrelated queue of downloads. `WAITING` must be excluded from `evictable`
    entirely, not merely favoured by the sort."""
    jobs.upsert({"id": "prompt", "title": "compile foolib", "state": jobs.WAITING,
                 "message": "waiting for your approval to compile foolib"}, now=900.0)
    for i in range(jobs.MAX_JOBS):
        jobs.upsert({"id": f"j{i}", "title": "x", "state": "done"}, now=1000.0 + i * 0.01)
    at = 1000.0 + jobs.MAX_JOBS * 0.01

    ids = {r["id"] for r in read_jobs(now=at)}
    assert "prompt" in ids, "the open prompt was evicted ahead of finished downloads"


def test_over_the_cap_the_live_work_is_what_survives():
    # Finished rows go first, then the least recently updated — a running
    # download is the last thing evicted. All stamped inside one FINISHED_TTL_S
    # window so it is the CAP being exercised here and not the age sweep.
    for i in range(jobs.MAX_JOBS):
        jobs.upsert({"id": f"done{i}", "title": "x", "state": "done"}, now=1000.0 + i * 0.01)
    at = 1000.0 + jobs.MAX_JOBS * 0.01
    jobs.upsert({"id": "live", "title": "downloading"}, now=at)

    ids = [r["id"] for r in read_jobs(now=at)]
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
