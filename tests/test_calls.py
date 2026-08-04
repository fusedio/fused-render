"""Tests for the app call log (fused_render/calls.py, docs/CALL_LOG_DESIGN.md).

Four things must hold, in priority order:

1. **Fail-open.** An unwritable store, a full queue, or a broken record must
   never change what /api/run returns. A logging feature that can break the
   thing it observes is worse than no logging.
2. **Bounds.** Per-record caps, a per-page rate cap, and retention by age AND
   size — each asserted, not assumed. A diagnostics store that fills the disk
   would be a worse bug than the one it exists to find.
3. **Attribution.** Only runtime-issued calls are logged (the X-Fused-Page
   header), so the shell's own traffic can never pollute an app's log — and
   reading the log never appends to it.
4. **Honest statistics.** Superseded calls are counted but excluded from every
   latency percentile.
"""
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from fused_render import calls
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server import create_app


# --------------------------------------------------------------------- helpers

@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the call store at a throwaway dir and reset module state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.setenv("FUSED_RENDER_CALLS", "1")
    monkeypatch.setattr(calls, "_buckets", {})
    monkeypatch.setattr(calls, "_dropped", 0)
    # The hot-path prefs snapshot and the templates-dir cache are keyed to a
    # home dir this fixture just moved — a warm entry from a previous test would
    # answer for the wrong store (and would ignore a monkeypatched pref).
    monkeypatch.setattr(calls, "_prefs_cache", None)
    monkeypatch.setattr(calls, "_templates_dirs_cache", None)
    return calls.store_dir()


def write_records(records):
    """Append records synchronously, bypassing the writer thread, so a read-side
    test is deterministic rather than sleeping on a queue drain."""
    calls._append(records)


def rec(**over):
    base = {
        "version": 1, "call_id": "c" + str(time.time_ns()), "kind": "call",
        "occurred_at": calls._now_iso(), "page": "/app/p.html", "route": "/api/run",
        "http_method": "POST", "status": 200, "outcome": "ok", "server_ms": 10,
        "entrypoint": "/app/d.py", "entrypoint_name": "d.py", "truncated": False,
    }
    base.update(over)
    return base


def app_current_file():
    """The file the writer would append to next for the default test app.

    Hand-composed store files must live inside a partition (CL-18) — the root
    holds only partition dirs and index.json — so this is `current_file` for
    the partition of `/app` (the dirname of `rec()`'s default page), with the
    directory created the way the writer's first append would.
    """
    path = calls.current_file(calls.partition_name("/app"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def in_store(store, name):
    """A hand-placed store file: inside the default app's partition (CL-18 —
    the root holds only partition dirs and index.json), directory created."""
    directory = os.path.join(store, calls.partition_name("/app"))
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, name)


def drain(timeout=6.0):
    """Wait for the background writer to land whatever is queued."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        q = calls._queue
        if q is not None and q.empty() and calls.store_files():
            time.sleep(0.25)  # let the in-flight batch finish its write
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def app_client(tmp_path, store):
    d = tmp_path / "work"
    d.mkdir()
    (d / "ok.py").write_text(
        "def main(freq: float = 1.0):\n    print('hi')\n    return [[1, freq]]\n")
    (d / "boom.py").write_text("def main():\n    return 1 / 0\n")
    (d / "p.html").write_text("<html><head></head><body></body></html>")
    client = TestClient(create_app(str(d)))
    return client, d


def app_headers(page, **extra):
    return {"X-Fused": "1", "X-Fused-Page": str(page), **extra}


# ------------------------------------------------------------------ fail-open

def test_unwritable_store_does_not_break_a_run(app_client, monkeypatch):
    """The whole point: logging must never fail the thing it observes."""
    client, d = app_client
    monkeypatch.setattr(calls, "_append",
                        lambda records: (_ for _ in ()).throw(OSError("read-only fs")))
    body = client.post("/api/run",
                       json={"py": str(d / "ok.py"), "html": str(d / "p.html"),
                             "params": {"freq": "2"}},
                       headers=app_headers(d / "p.html")).json()
    assert body["ok"] is True
    assert body["result"] == [[1, 2.0]]


def test_full_queue_drops_the_record_and_counts_it(store, monkeypatch):
    full = queue.Queue(maxsize=1)
    full.put(rec())
    monkeypatch.setattr(calls, "_ensure_writer", lambda: full)
    calls.record(rec())
    assert calls.dropped_count() == 1


def test_rate_cap_drops_a_runaway_page(store, monkeypatch):
    """A render loop must not fill the disk. The cap is per page."""
    landed = []
    monkeypatch.setattr(calls, "_ensure_writer", lambda: _CollectingQueue(landed))
    for _ in range(calls.RATE_BURST + 50):
        calls.record(rec(page="/app/loop.html"))
    assert len(landed) == calls.RATE_BURST
    assert calls.dropped_count() == 50
    # A different page has its own budget — one bad page can't silence the rest.
    calls.record(rec(page="/app/other.html"))
    assert len(landed) == calls.RATE_BURST + 1


class _CollectingQueue:
    def __init__(self, sink):
        self.sink = sink

    def put_nowait(self, item):
        self.sink.append(item)


def test_writer_survives_a_write_failure(store, monkeypatch):
    """A dead writer thread would silently stop logging while callers queued on.
    _writer_loop must swallow an OSError and keep draining.

    The two records are queued in separate waits on purpose: the loop coalesces
    everything already queued into ONE _append call, so recording both at once
    would put them in the same failing batch and prove nothing about recovery.

    Both waits are on Events the stub sets at a chosen moment, NOT on the length
    of `batches`: the stub appends BEFORE it writes, so waiting for a second
    element unblocked while that write was still in flight and the query below
    could read an empty store (a real ~1-in-a-few-hundred-runs flake, caught on
    3.13 as `assert [] == ['second']`). `entered` marks the failing batch
    reaching _append, `written` marks a batch reaching disk — and only the
    latter makes the store safe to read.
    """
    batches = []
    real_append = calls._append  # bind BEFORE patching, or flaky recurses into itself
    entered = threading.Event()  # the writer reached _append with the doomed batch
    written = threading.Event()  # a batch got all the way to disk

    def flaky(records):
        batches.append(records)
        if len(batches) == 1:
            entered.set()
            raise OSError("disk full")
        real_append(records)
        written.set()  # AFTER the write, so a waiter that sees this can query

    monkeypatch.setattr(calls, "_append", flaky)
    calls.record(rec(call_id="first"))
    assert entered.wait(6), "the first record was never handed to the writer"

    calls.record(rec(call_id="second"))
    assert written.wait(6), "writer thread died on the first failure"
    assert [r["call_id"] for r in calls.query(limit=10)["records"]] == ["second"]


# ---------------------------------------------------------------------- bounds

def test_output_and_traceback_are_capped_and_marked(store):
    call = {"truncated": False}
    calls.enrich_run(
        call, resolved="/app/d.py", params={}, engine="builtin",
        result={"ok": False, "stdout": "o" * 20_000, "stderr": "e" * 20_000,
                "error": {"type": "ValueError", "message": "m", "traceback": "t" * 40_000}},
    )
    assert len(call["stdout_tail"].encode()) <= calls.OUTPUT_CAP
    assert len(call["stderr_tail"].encode()) <= calls.OUTPUT_CAP
    assert len(call["error"]["traceback"].encode()) <= calls.ERROR_CAP
    assert call["truncated"] is True


def test_capping_keeps_the_tail_not_the_head():
    """The end of a traceback is the exception; the head is boilerplate."""
    text = "\n".join(f"line {i}" for i in range(5_000))
    capped, truncated = calls._cap_text(text, 200)
    assert truncated is True
    assert capped.endswith("line 4999")
    assert "line 0\n" not in capped


def test_oversized_params_degrade_to_key_names(store):
    call = {"truncated": False}
    calls.enrich_run(call, resolved="/app/d.py", engine="builtin",
                     params={"blob": "x" * 5_000, "freq": "2"},
                     result={"ok": True, "result": []})
    # The key names (the call's SHAPE) rather than an arbitrary subset of
    # values, which would read as a complete param set and mislead.
    assert call["params"] == ["blob", "freq"]
    assert call["params_truncated"] is True


def test_a_record_over_the_whole_cap_is_shrunk_not_dropped(store):
    write_records([rec(call_id="huge", stdout_tail="s" * 40_000,
                       error={"type": "E", "message": "m", "traceback": "t" * 40_000})])
    line = open(calls.store_files()[0], encoding="utf-8").read().strip()
    assert len(line.encode()) <= calls.RECORD_CAP
    stored = json.loads(line)
    assert stored["truncated"] is True
    assert stored["call_id"] == "huge", "the skeleton must survive"


def test_sweep_removes_files_past_the_retention_window(store, monkeypatch):
    os.makedirs(store, exist_ok=True)
    old = in_store(store, "2020-01-01-1.calls.jsonl")
    fresh = in_store(store, "2026-07-24-1.calls.jsonl")
    for path in (old, fresh):
        with open(path, "w") as fh:
            fh.write(json.dumps(rec()) + "\n")
    os.utime(old, (time.time() - 40 * 86_400,) * 2)
    monkeypatch.setenv(calls.RETENTION_DAYS_ENV, "14")
    assert calls.sweep() == 1
    assert os.path.exists(fresh) and not os.path.exists(old)


def test_retention_runs_while_the_writer_is_idle(store, monkeypatch):
    """Retention has to be a real timer, not a side effect of writing.

    The due-check used to sit at the BOTTOM of the writer loop, after a blocking
    `q.get()` — so it only ever ran just after a record landed. An app left open
    after a busy afternoon kept its expired files until something called Python
    again, which for a store whose whole job is "don't keep my activity forever"
    is the wrong way round: the case where nothing is happening is exactly the
    case where nobody triggers the cleanup.

    Driven through the REAL loop rather than by calling `sweep()`: what is under
    test is that the writer wakes on its own with an empty queue. The expired
    file is planted AFTER the loop's start-up sweep has already run against an
    empty store, so only a later, unprompted wake can remove it.
    """
    monkeypatch.setattr(calls, "SWEEP_POLL_S", 0.05)
    monkeypatch.setattr(calls, "SWEEP_INTERVAL_S", 0)  # every wake is due
    monkeypatch.setenv(calls.RETENTION_DAYS_ENV, "14")

    q = queue.Queue(maxsize=8)
    writer = threading.Thread(target=calls._writer_loop, args=(q,), daemon=True)
    writer.start()
    try:
        time.sleep(0.2)  # let the start-up sweep run against the empty store
        expired = in_store(store, "2020-01-01-1.calls.jsonl")
        with open(expired, "w") as fh:
            fh.write(json.dumps(rec()) + "\n")
        os.utime(expired, (time.time() - 40 * 86_400,) * 2)

        deadline = time.monotonic() + 6.0
        while os.path.exists(expired) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not os.path.exists(expired), (
            "an idle writer must still prune — nothing was queued the whole time"
        )
    finally:
        # Owned thread, stopped explicitly: left running it would keep waking
        # and could sweep a LATER test's store (the store dir is resolved from
        # the environment on every call, so it follows whichever test is live).
        q.put(calls._STOP)
        writer.join(timeout=5)
        assert not writer.is_alive(), "the writer must honour the stop sentinel"


def test_sweep_trims_oldest_first_when_over_the_size_cap(store, monkeypatch):
    os.makedirs(store, exist_ok=True)
    paths = [in_store(store, f"2026-07-2{i}-1.calls.jsonl") for i in range(1, 4)]
    for path in paths:
        with open(path, "w") as fh:
            fh.write("x" * 1_000)
    monkeypatch.setattr(calls, "DEFAULT_MAX_BYTES", 1_500)
    monkeypatch.setenv(calls.RETENTION_DAYS_ENV, "0")  # age pruning off
    calls.sweep()
    survivors = [os.path.basename(p) for p in calls.store_files()]
    assert survivors == ["2026-07-23-1.calls.jsonl"], "must drop the oldest first"


# ----------------------------------------------------------------- attribution

def test_a_call_without_the_page_header_is_not_logged(app_client):
    """The shell's own requests carry no attribution and must stay out of the
    app log — excluded by construction, not by an endpoint blocklist."""
    client, d = app_client
    client.get(f"/api/fs/stat?path={d / 'ok.py'}")
    client.post("/api/run", json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers={"X-Fused": "1"})
    time.sleep(0.5)
    assert calls.query(limit=50)["records"] == []


def test_an_attributed_run_is_logged_with_its_detail(app_client):
    client, d = app_client
    client.post("/api/run",
                json={"py": str(d / "ok.py"), "html": str(d / "p.html"),
                      "params": {"freq": "2.5"}},
                headers=app_headers(d / "p.html", **{"X-Fused-Call": "known-id"}))
    assert drain()
    records = calls.query(limit=10)["records"]
    assert len(records) == 1
    got = records[0]
    assert got["call_id"] == "known-id"          # the client's id is honoured
    assert got["page"] == str(d / "p.html")
    assert got["route"] == "/api/run"
    assert got["outcome"] == "ok"
    assert got["entrypoint_name"] == "ok.py"
    assert got["params"] == {"freq": "2.5"}
    assert got["stdout_tail"] == "hi\n"
    assert got["result_kind"] == "list" and got["result_rows"] == 1
    assert got["server_ms"] >= 0 and got["run_ms"] >= 0
    assert got["engine"] in ("builtin", "fused")


def test_a_failed_run_records_the_traceback(app_client):
    client, d = app_client
    client.post("/api/run", json={"py": str(d / "boom.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html"))
    assert drain()
    got = calls.query(limit=10)["records"][0]
    assert got["outcome"] == "error"
    assert got["error"]["type"] == "ZeroDivisionError"
    assert "ZeroDivisionError" in got["error"]["traceback"]


def test_a_non_latin1_path_rides_percent_encoded_and_lands_decoded(app_client):
    """A file whose name is not Latin-1 must not take the whole page down: a
    header holding one made `fetch` throw, killing every call the page made."""
    client, d = app_client
    target = d / "调查-обзор-مسح.pdf"
    page = d / "p.html"
    client.get(f"/api/fs/stat?path={target}", headers={
        "X-Fused-Page": quote(str(page)), "X-Fused-Target": quote(str(target)),
    })
    assert drain()

    got = calls.query(limit=10)["records"][0]
    assert got["page"] == canonical_fs_path(str(page))
    assert got["target_file"] == canonical_fs_path(str(target)), (
        "an encoded record matches no filter and names a file nobody has")


def test_the_runtime_encodes_the_paths_it_puts_in_headers(store):
    """The producer half: the decode above passes even if the runtime stops
    encoding, which is exactly the state that was broken."""
    from pathlib import Path

    import fused_render

    runtime = (Path(fused_render.__file__).parent / "static"
               / "runtime.js").read_text(encoding="utf-8")
    assert 'headers["X-Fused-Page"] = encodeURIComponent(page)' in runtime
    assert 'headers["X-Fused-Target"] = encodeURIComponent(target)' in runtime


def test_first_party_flags_a_template_page_not_the_users_own(store):
    """True for a template in ANY of its three homes — packaged, staged core,
    or a user fork — so the "My pages" filter shows the user's own work."""
    from fused_render.core_templates import core_templates_dir

    packaged = os.path.join(os.path.dirname(calls.__file__), "templates", "duckdb",
                            "template.html")
    staged = os.path.join(core_templates_dir(), "duckdb", "template.html")
    forked = os.path.join(calls.storage.home_dir(), "templates", "mine", "template.html")
    for path in (packaged, staged, forked):
        assert calls.is_first_party(path) is True, path
    assert calls.is_first_party("/home/me/views/sine.html") is False


def test_a_write_records_size_and_conflict_outcome_but_never_content(app_client):
    # The needle is UPPERCASE on purpose, so it cannot occur anywhere else in
    # the record: the only free-form fields are the random call_id (lowercase
    # hex) and the tmp paths (lowercase). The old needle "abcd" is legal hex and
    # duly turned up INSIDE a call_id in CI ("...888abcd3c4..."), failing a real
    # invariant for a coincidence — 29 windows in 32 hex chars is ~0.04% per
    # record, which a four-job matrix hits sooner or later. Keep any replacement
    # outside the alphabet of the other fields.
    content = "NEVER-LOG-THIS-PAYLOAD"
    client, d = app_client
    target = d / "out.txt"
    client.post("/api/fs/write", json={"path": str(target), "content": content},
                headers=app_headers(d / "p.html"))
    assert drain()
    got = calls.query(limit=10)["records"][0]
    assert got["route"] == "/api/fs/write"
    assert got["bytes_written"] == len(content)
    assert content not in json.dumps(got), "file content must never be stored"


def test_an_upload_records_size_and_path_but_never_the_bytes(app_client):
    """/api/fs/upload is a write like any other and belongs in the log.

    Without this a pasted screenshot or video is the one mutation that leaves
    no trace, which defeats the "what did my page put on disk" question the
    route table exists to answer. Same rule as /api/fs/write: the path and the
    byte count, never the payload.
    """
    client, d = app_client
    payload = b"\x89PNG\r\n\x1a\nNEVER-LOG-THESE-BYTES"
    target = d / "pasted.png"
    client.post("/api/fs/upload", data={"path": str(target)},
                files={"file": ("blob", payload, "image/png")},
                headers=app_headers(d / "p.html"))
    assert drain()
    got = calls.query(limit=10)["records"][0]
    assert got["route"] == "/api/fs/upload"
    assert got["entrypoint"] == str(target)
    assert got["entrypoint_name"] == "pasted.png"
    # The byte count is the blob's own length: there is no encoding step for a
    # binary body, unlike the UTF-8 round trip a text write measures.
    assert got["bytes_written"] == len(payload)
    assert "NEVER-LOG-THESE-BYTES" not in json.dumps(got)


def test_enrich_write_measures_a_binary_body_without_encoding_it(store):
    call = {"truncated": False}
    calls.enrich_write(call, path="/n/assets/a.png", content=b"\xff\xfe\x00", status=200)
    assert call["bytes_written"] == 3


def test_a_rejected_write_is_not_blamed_on_a_readonly_file(app_client):
    """Two different refusals answer 403 on this route — a read-only target, and
    the X-Fused guard turning the caller away — and mapping the status alone
    called both `readonly`. That points a reader at file permissions that are
    perfectly fine, for a request that never got past the door.

    Not reachable from static/runtime.js (it always sends the header), which is
    exactly why it would have gone unnoticed: the record contract should not
    rely on the only current caller being well-behaved.
    """
    client, d = app_client
    target = d / "guarded.txt"
    # Attribution present (so a record IS opened), authorization absent.
    response = client.post(
        "/api/fs/write", json={"path": str(target), "content": "x"},
        headers={"X-Fused-Page": str(d / "p.html")})
    assert response.status_code == 403
    assert drain()

    got = calls.query(limit=10)["records"][0]
    assert got["route"] == "/api/fs/write"
    assert got["status"] == 403
    assert got["outcome"] == "error", (
        "a refused request is an error, not a read-only file — `readonly` is "
        "reserved for the refusal that really is about the target"
    )
    assert got["level"] == "ERROR", "and the severity follows the outcome"


def test_a_readonly_target_still_reports_readonly(app_client, monkeypatch):
    """The other side of the same rule: the refusal that IS about the target
    keeps its own outcome, so narrowing the 403 mapping did not just delete it."""
    call = {"truncated": False}
    calls.enrich_write(call, path="/data/locked.txt", content="x", status=403)
    assert call["outcome"] == "readonly"
    assert calls._level_for(call) == "WARN", "readonly is a warning, not an error"


# ---------------------------------------------------------------- params modes

@pytest.mark.parametrize("mode,expected", [
    ("full", {"token": "s3cret", "freq": "2"}),
    ("keys", ["freq", "token"]),
    ("off", None),
])
def test_params_redaction_modes(store, monkeypatch, mode, expected):
    monkeypatch.setattr("fused_render.shell.prefs.calls_params_mode", lambda: mode)
    call = {"truncated": False}
    calls.enrich_run(call, resolved="/app/d.py", engine="builtin",
                     params={"token": "s3cret", "freq": "2"},
                     result={"ok": True, "result": []})
    got = call["params"]
    assert (sorted(got) if isinstance(got, list) else got) == expected


def test_capture_can_be_switched_off_entirely(app_client, monkeypatch):
    client, d = app_client
    monkeypatch.setenv("FUSED_RENDER_CALLS", "0")
    client.post("/api/run", json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html"))
    time.sleep(0.4)
    assert calls.query(limit=10)["records"] == []


# ------------------------------------------------------------------- read side

def test_query_cursor_returns_only_what_is_new(store):
    write_records([rec(call_id="old1"), rec(call_id="old2")])
    first = calls.query(limit=10)
    assert first["cursor"] == "old2"
    write_records([rec(call_id="new1")])
    after = calls.query(limit=10, cursor=first["cursor"])
    assert [r["call_id"] for r in after["records"]] == ["new1"]


def test_superseded_calls_are_counted_but_never_in_the_percentiles(store):
    """D114's latest-wins cancellation means one slider drag issues dozens of
    calls. Counting their latency would report "40 calls, p95 3.2s" for what the
    user experienced as one request."""
    write_records(
        [rec(outcome="ok", server_ms=10), rec(outcome="ok", server_ms=20)]
        + [rec(outcome="superseded", server_ms=9_000) for _ in range(20)]
    )
    row = calls.targets()["targets"][0]
    assert row["count"] == 22
    assert row["superseded"] == 20
    assert row["max"] == 20, "a thrown-away call must not set the max"
    assert row["p95"] <= 20

    point = calls.series(bucket_ms=3_600_000)["points"][0]
    assert point["count_ok"] == 2 and point["count_superseded"] == 20
    assert point["p95"] <= 20


def test_targets_rollup_reports_error_rate_and_bytes(store):
    write_records([
        rec(entrypoint="/a/x.py", entrypoint_name="x.py", outcome="ok", result_bytes=100),
        rec(entrypoint="/a/x.py", entrypoint_name="x.py", outcome="error", result_bytes=50),
        rec(entrypoint="/a/y.py", entrypoint_name="y.py", outcome="ok", result_bytes=7),
    ])
    rows = {r["name"]: r for r in calls.targets()["targets"]}
    assert rows["x.py"]["count"] == 2 and rows["x.py"]["errors"] == 1
    assert rows["x.py"]["error_rate"] == 0.5
    assert rows["x.py"]["bytes"] == 150
    assert rows["y.py"]["error_rate"] == 0.0


def test_page_errors_are_not_a_target_row(store):
    """A page error is what happened INSTEAD of a call, so it has no target and
    must not appear as an "(unknown)" row."""
    write_records([rec(kind="page-error", outcome="error", entrypoint=None,
                       entrypoint_name=None, route=None)])
    assert calls.targets()["targets"] == []
    assert calls.overview()["kinds"]["page-error"] == 1


def test_filters_narrow_by_page_target_and_failure(store):
    write_records([
        rec(page="/a/one.html", outcome="ok"),
        rec(page="/a/two.html", outcome="error"),
        rec(page="/t/tpl.html", target_file="/a/one.html", outcome="ok"),
    ])
    assert calls.overview(page="/a/one.html")["total"] == 2  # own + template-on-it
    assert calls.overview(failed=True)["total"] == 1
    assert calls.overview(page="/a/two.html", failed=True)["total"] == 1


def test_a_corrupt_line_is_skipped_not_fatal(store):
    os.makedirs(store, exist_ok=True)
    path = in_store(store, "2026-07-24-1.calls.jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps(rec(call_id="good")) + "\n")
        fh.write('{"partial": tru\n')  # a torn tail from an in-flight append
    assert [r["call_id"] for r in calls.query(limit=10)["records"]] == ["good"]


# ----------------------------------------------------------------- page errors

def test_page_error_endpoint_records_the_js_failure(app_client):
    """The record for when NO call happened — the signal that separates a page
    whose JS died from a page nobody opened."""
    client, d = app_client
    body = client.post("/api/calls/event", json={
        "kind": "page-error", "page": str(d / "p.html"), "type": "TypeError",
        "message": "freq is not defined", "source": str(d / "p.html"),
        "line": 42, "col": 7, "stack": "TypeError: ...\n at draw",
    }, headers={"X-Fused": "1"}).json()
    assert body["recorded"] is True
    assert drain()
    got = calls.query(limit=10)["records"][0]
    assert got["kind"] == "page-error"
    assert got["error"]["type"] == "TypeError"
    assert got["line"] == 42 and got["col"] == 7
    assert got["outcome"] == "error"


def test_page_error_requires_the_fused_header(app_client):
    client, _ = app_client
    assert client.post("/api/calls/event", json={"kind": "page-error", "page": "/a"}).status_code == 403


def test_page_event_rejects_an_unknown_kind(app_client):
    client, d = app_client
    res = client.post("/api/calls/event", json={"kind": "something-else", "page": str(d)},
                      headers={"X-Fused": "1"})
    assert res.status_code == 400
    assert "page-error" in res.json()["error"]


# ---------------------------------------------------------------------- config

def test_config_reports_the_store_location(app_client):
    client, _ = app_client
    body = client.get("/api/calls/config").json()
    assert body["dir"] == calls.store_dir()
    # No `today` key: with per-app partitions (CL-18) there is one live file
    # per partition, so a single "today's file" stopped being a fact — its
    # absence is asserted so it cannot half-return.
    assert "today" not in body
    assert body["enabled"] is True
    assert body["retention_days"] == 14


def test_day_file_is_per_process_and_part(store):
    """Two live servers must not interleave lines into one file (the same
    reasoning logs.py applies to its per-pid log), and the part suffix is
    zero-padded so a name sort stays chronological — which the oldest-first size
    trim depends on."""
    assert calls.day_file().endswith(f"-{os.getpid()}-001.calls.jsonl")
    assert sorted([calls.day_file(part=10), calls.day_file(part=2)]) == [
        calls.day_file(part=2), calls.day_file(part=10)]


def test_appends_roll_to_a_new_part_past_the_file_cap(store, monkeypatch):
    """Without a per-file cap a single day's file grows unbounded: the directory
    cap can only delete whole files and must never delete a live one."""
    monkeypatch.setattr(calls, "MAX_FILE_BYTES", 400)
    first = app_current_file()
    write_records([rec() for _ in range(4)])
    assert os.path.getsize(first) >= 400
    second = app_current_file()
    assert second != first and second.endswith("-002.calls.jsonl")
    write_records([rec(call_id="rolled")])
    assert os.path.exists(second)
    # Both parts are still one logical log.
    assert calls.overview()["total"] == 5


def test_size_trim_never_deletes_a_live_file(store, monkeypatch):
    """Trimming is whole-file, so today's files (possibly open for append by this
    process OR another server) are not candidates — deleting one silently
    discarded the whole day, since the writer just recreates it."""
    os.makedirs(store, exist_ok=True)
    live = app_current_file()
    with open(live, "w") as fh:
        fh.write("x" * 3000)
    monkeypatch.setattr(calls, "DEFAULT_MAX_BYTES", 1000)
    monkeypatch.setenv(calls.RETENTION_DAYS_ENV, "14")
    assert calls.sweep() == 0
    assert os.path.exists(live), "the live file must survive an over-cap sweep"

    # An older file IS fair game, and the live one still survives.
    old = in_store(store, "2020-01-02-1-001.calls.jsonl")
    with open(old, "w") as fh:
        fh.write("y" * 3000)
    os.utime(old, (time.time() - 2 * 86400,) * 2)  # inside the age window
    assert calls.sweep() == 1
    assert os.path.exists(live) and not os.path.exists(old)


# ------------------------------------------------------- registry + gate wiring

def test_a_user_fork_of_the_reader_is_never_allowlisted(tmp_path, monkeypatch):
    """The one copy that must stay on the subprocess path: once the file lives
    under ~/.fused-render/templates/ the user can edit it, so it is user code
    and keeps the timeout and process isolation. It still WORKS there — the
    child bootstraps the package (see below) — just without the in-process
    shortcut.

    `duckdb` because it IS allowlisted in its built-in copies: a fork of a
    helper that is not allowlisted anywhere would pass this vacuously."""
    from fused_render import executor

    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    fork = tmp_path / "templates" / "duckdb" / "reader.py"
    fork.parent.mkdir(parents=True)
    fork.write_text("def main(**kw):\n    return {}\n", encoding="utf-8")
    assert executor._is_builtin_helper(str(fork)) is False


def test_the_child_does_not_put_the_package_on_the_path(tmp_path):
    """The worker is hermetic: it does not make `fused_render` importable.

    It used to. `_child.py` appended the package's parent to sys.path and
    `executor._child_env()` appended the same value to PYTHONPATH, so a helper
    could `import fused_render` from the child even with the package not
    installed into that interpreter. Both halves are gone (PY-15): the one
    consumer was the call-log reader reading the store through
    `fused_render.calls`, nothing under `templates/` imports the package any more,
    and the fused local execution backend STRIPS PYTHONPATH from its children for
    venv hermeticity — so any template leaning on it worked under this executor
    and silently took its fallback branch under the other engine. Pinning the
    ABSENCE is what keeps a template from quietly acquiring that dependency again.

    Driven through the REAL child with the package's parent stripped from
    sys.path, so the assertion is about the bootstrap and not about however this
    checkout happens to be installed.
    """
    probe = tmp_path / "probe.py"
    parent = os.path.dirname(os.path.dirname(os.path.abspath(calls.__file__)))
    probe.write_text(
        "import sys\n"
        "def main(**kw):\n"
        f"    return {{'parent_on_path': {parent!r} in sys.path}}\n",
        encoding="utf-8")

    shim = tmp_path / "shim"
    shim.mkdir()
    # Strip exactly the path an editable install contributes, leaving
    # site-packages intact: an interpreter with the dependencies but not the
    # package — the packaged app's helper python, or a source run with no install.
    (shim / "sitecustomize.py").write_text(
        "import sys\n"
        f"sys.path[:] = [p for p in sys.path if p.rstrip('/\\\\') != {parent!r}]\n",
        encoding="utf-8")

    child = os.path.join(os.path.dirname(os.path.abspath(calls.__file__)), "_child.py")
    out = subprocess.run(
        [sys.executable, child],
        input=json.dumps({"path": str(probe), "params": {}}),
        capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),  # nothing importable next to the helper
        env={**os.environ, "PYTHONPATH": str(shim)})
    assert out.returncode == 0, out.stderr
    body = json.loads(out.stdout)
    assert body["ok"] is True, body.get("error")
    assert body["result"]["parent_on_path"] is False, \
        "the child must not put the package's parent on sys.path"


def test_neither_half_of_the_old_pythonpath_injection_survives(tmp_path, monkeypatch):
    """The two halves were one fix and must stay removed together.

    A half-removal is the dangerous state: the parent still exporting PYTHONPATH
    while the child no longer appends (or vice versa) leaves `import fused_render`
    working on the built-in engine and failing on the fused one — precisely the
    engine-dependent divergence PY-15 exists to end.
    """
    from fused_render import executor

    assert not hasattr(executor, "_child_env"), \
        "the parent half is gone; a template must not depend on PYTHONPATH"
    executor_src = open(os.path.abspath(executor.__file__), encoding="utf-8").read()
    assert "PYTHONPATH\"]" not in executor_src and "'PYTHONPATH'" not in executor_src, \
        "the worker env must not set PYTHONPATH"

    child_src = open(os.path.join(os.path.dirname(calls.__file__), "_child.py"),
                     encoding="utf-8").read()
    assert "sys.path.append(_PACKAGE_PARENT)" not in child_src, \
        "the child half is gone; the two must agree"


# ------------------------------------------------- superseded reporting (CL-5)

def test_an_abandoned_call_is_marked_superseded(app_client):
    """The seam the original test missed: it asserted the ROLLUP excludes
    superseded records, never that anything ever produces one.

    The server cannot detect this itself — aborting a fetch does not raise into
    the handler, so the run completes and would be recorded as an ordinary
    success. The page reports it, keyed by the X-Fused-Call id it already sent.
    """
    client, d = app_client
    assert client.post("/api/calls/event",
                       json={"kind": "superseded", "call_ids": ["scrub-1"]},
                       headers={"X-Fused": "1"}).json() == {"marked": 1}
    client.post("/api/run",
                json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html", **{"X-Fused-Call": "scrub-1"}))
    assert drain()
    got = calls.detail("scrub-1")
    assert got["outcome"] == "superseded"
    assert got["status"] == 200, "the run itself still succeeded; only the label differs"


def test_the_superseding_request_carries_the_mark_itself(app_client):
    """The mark rides the request that CAUSED it, so it cannot arrive late.

    Reported by Bugbot: the report was a separate POST deferred by
    `setTimeout(0)`, and `finish()` only stamps `superseded` if the mark is
    already there. Measured in Chromium against a local server, that POST landed
    ~19 ms after the abort — so any abandoned call whose handler finished inside
    that window was written as `ok` and counted in the latency percentiles,
    which is precisely what CL-5 exists to prevent. In-process helpers (D72)
    finish that fast routinely.

    A supersession only ever happens because the page is issuing a new call on
    the same channel, and that request leaves in the same synchronous task as
    the abort — so it carries the ids, and the server takes them in `begin()`,
    before it can write anything for the call being abandoned.

    Here the abandoned run is recorded AFTER the superseding request, with no
    prior /api/calls/event at all: only the header can produce this outcome.
    """
    client, d = app_client
    client.post("/api/run",
                json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html", **{
                    "X-Fused-Call": "keeper",
                    "X-Fused-Supersedes": "ditched-1,ditched-2",
                }))
    for call_id in ("ditched-1", "ditched-2"):
        client.post("/api/run",
                    json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                    headers=app_headers(d / "p.html", **{"X-Fused-Call": call_id}))
    assert drain()

    for call_id in ("ditched-1", "ditched-2"):
        assert calls.detail(call_id)["outcome"] == "superseded", call_id
    assert calls.detail("keeper")["outcome"] == "ok", \
        "the call that did the superseding is not itself superseded"


def test_a_mark_is_consumed_once_so_a_duplicate_cannot_mislabel_a_later_call(app_client):
    """Why the runtime hands the ids to the header INSTEAD of also posting them.

    `finish()` consumes a mark. A duplicate arriving afterwards would linger in
    the server's map for its whole TTL, and any later call that happened to
    reuse the id would be mislabelled. Ids are random so the second half is
    theoretical, but "sent twice" is a state the client should not create — the
    runtime splices the queue when the header takes it.
    """
    client, d = app_client
    client.post("/api/run", json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html", **{"X-Fused-Call": "keeper",
                                                     "X-Fused-Supersedes": "once"}))
    client.post("/api/run", json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html", **{"X-Fused-Call": "once"}))
    assert drain()
    assert calls.detail("once")["outcome"] == "superseded"
    assert calls._take_superseded("once") is False, "the mark is gone after one use"

    runtime = open(os.path.join(os.path.dirname(calls.__file__), "static", "runtime.js"),
                   encoding="utf-8").read()
    assert "function takePendingSupersedes()" in runtime
    assert "supersededIds.splice(0, supersededIds.length).join(\",\")" in runtime, \
        "the header must TAKE the ids, not copy them"


def test_a_superseded_run_is_excluded_from_the_percentiles_end_to_end(app_client):
    """One drag: several abandoned calls plus the one the user waited for. The
    chart must report the kept call's latency, not the drag's."""
    client, d = app_client
    for i in range(4):
        call_id = f"tick-{i}"
        if i < 3:  # the first three are superseded by the next
            client.post("/api/calls/event",
                        json={"kind": "superseded", "call_ids": [call_id]},
                        headers={"X-Fused": "1"})
        client.post("/api/run",
                    json={"py": str(d / "ok.py"), "html": str(d / "p.html"),
                          "params": {"freq": str(i)}},
                    headers=app_headers(d / "p.html", **{"X-Fused-Call": call_id}))
    assert drain()
    row = calls.targets()["targets"][0]
    assert row["count"] == 4
    assert row["superseded"] == 3
    kept = calls.detail("tick-3")
    assert row["max"] == kept["server_ms"], "percentiles must reflect only the kept call"


def test_a_mark_is_consumed_once(store):
    """An id must never stamp two records — a recycled or replayed report cannot
    silently reclassify a later, real call."""
    calls.mark_superseded(["once"])
    assert calls._take_superseded("once") is True
    assert calls._take_superseded("once") is False


def test_marks_expire_and_are_hard_bounded(store, monkeypatch):
    """A page that reports and then navigates away must not leave ids resident
    forever, and a runaway page must not grow the set without limit."""
    monkeypatch.setattr(calls, "_SUPERSEDED", {})
    monkeypatch.setattr(calls, "_SUPERSEDED_TTL_S", 0.0)
    calls.mark_superseded(["stale"])
    calls.mark_superseded(["fresh"])  # the sweep on entry evicts 'stale'
    assert calls._take_superseded("stale") is False

    monkeypatch.setattr(calls, "_SUPERSEDED", {})
    monkeypatch.setattr(calls, "_SUPERSEDED_TTL_S", 300.0)
    monkeypatch.setattr(calls, "_SUPERSEDED_MAX", 10)
    calls.mark_superseded([f"id-{i}" for i in range(50)])
    assert len(calls._SUPERSEDED) == 10


def test_superseded_event_validates_its_payload(app_client):
    client, _ = app_client
    res = client.post("/api/calls/event", json={"kind": "superseded", "call_ids": "nope"},
                      headers={"X-Fused": "1"})
    assert res.status_code == 400
    assert "call_ids" in res.json()["error"]


def test_the_runtime_reports_supersession(store):
    """The client half of CL-5 — assert the wiring exists in runtime.js, since
    nothing else in the suite executes it (it needs a real browser)."""
    runtime = os.path.join(os.path.dirname(calls.__file__), "static", "runtime.js")
    src = open(runtime, encoding="utf-8").read()
    assert "reportSuperseded(prev._callId)" in src, "the abort path must report"
    assert '"kind": "superseded"' in src or '"superseded"' in src
    assert "controller._callId = newCallId()" in src, "the id must be stable per call"
    assert "flushSuperseded();" in src, "a closing page must flush its queue"


# --------------------------------------------------- scoping + bounded reads

def test_this_file_scope_works_for_a_py_target(store):
    """A `.py` is never a record's `page` (the .html is) — so filtering by it
    must also match the call's entrypoint, or the Calls view the registry offers
    for a data file shows nothing on a file that plainly has history."""
    write_records([rec(page="/app/p.html", entrypoint="/app/d.py", entrypoint_name="d.py")])
    assert calls.overview(page="/app/d.py")["total"] == 1
    assert calls.overview(page="/app/p.html")["total"] == 1
    assert calls.overview(page="/app/other.py")["total"] == 0


def test_reads_skip_files_outside_the_window(store, monkeypatch):
    """A one-hour question must not parse a fortnight of records."""
    os.makedirs(store, exist_ok=True)
    old_path = in_store(store, "2020-01-01-1-001.calls.jsonl")
    with open(old_path, "w") as fh:
        for i in range(500):
            fh.write(json.dumps(rec(call_id=f"old-{i}",
                                    occurred_at="2020-01-01T00:00:00.000Z")) + "\n")
    os.utime(old_path, (time.time() - 400 * 86400,) * 2)
    write_records([rec(call_id="new")])

    opened = []
    real_open = calls._iter_lines_reverse

    def spy(path, *a, **kw):
        opened.append(os.path.basename(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr(calls, "_iter_lines_reverse", spy)
    assert calls.overview(since=time.time() - 3600)["total"] == 1
    assert old_path.split(os.sep)[-1] not in opened, "the stale file must not be read at all"


def test_reads_stop_at_the_window_inside_a_file(store):
    """Within a file, records are append-ordered, so the first one APPENDED before
    the window ends it — everything further back was appended earlier still.

    The stop key is `recorded_at` (append time), not `occurred_at` (call start),
    so the fixtures carry a real append stamp exactly as production writes them.
    """
    os.makedirs(store, exist_ok=True)
    path = app_current_file()
    stale = time.time() - 86_400
    with open(path, "w") as fh:
        for i in range(200):  # appended a day ago, written first
            fh.write(json.dumps(dict(calls._prune(rec(call_id=f"old-{i}")),
                                     recorded_at=stale)) + "\n")
        for i in range(3):
            fh.write(json.dumps(calls._prune(rec(call_id=f"new-{i}"))) + "\n")
    seen = list(calls._iter_records([path], since=time.time() - 3600))
    assert [r["call_id"] for r in seen] == ["new-2", "new-1", "new-0"]


def test_reverse_line_reader_handles_block_boundaries(store):
    """A line straddling the read-block boundary must not be split or lost."""
    os.makedirs(store, exist_ok=True)
    path = in_store(store, "2026-07-24-1-001.calls.jsonl")
    payloads = [json.dumps(rec(call_id=f"c-{i}", stdout_tail="x" * 97)) for i in range(200)]
    with open(path, "w") as fh:
        fh.write("\n".join(payloads) + "\n")
    got = [json.loads(line)["call_id"] for line in calls._iter_lines_reverse(path, chunk=128)]
    assert got == [f"c-{i}" for i in reversed(range(200))]


def test_a_file_without_a_trailing_newline_is_fully_read(store):
    os.makedirs(store, exist_ok=True)
    path = in_store(store, "2026-07-24-2-001.calls.jsonl")
    with open(path, "w") as fh:
        fh.write(json.dumps(rec(call_id="a")) + "\n" + json.dumps(rec(call_id="b")))
    assert [r["call_id"] for r in calls._iter_records([path])] == ["b", "a"]


# ---------------------------- client disconnect (a real socket, real uvicorn)

@pytest.fixture
def live_server(tmp_path, store):
    """A REAL uvicorn server on a loopback port.

    TestClient cannot express this test: its transport is in-process, so there is
    no socket to close, and a closed socket is the entire subject. Every
    disconnect defect in this feature slipped past a green suite for exactly that
    reason.
    """
    import threading

    import uvicorn

    d = tmp_path / "work"
    d.mkdir()
    (d / "slow.py").write_text("import time\ndef main():\n    time.sleep(2.5)\n    return [1]\n")
    (d / "p.html").write_text("<html><head></head><body></body></html>")

    app = create_app(str(d))
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server.started and server.servers and server.servers[0].sockets:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", d
    server.should_exit = True
    thread.join(timeout=10)


def _post_run_and_hang_up(base: str, d, call_id: str, after: float):
    """Start a run, then close the socket — a closed tab, mid-flight."""
    import socket
    from urllib.parse import urlsplit

    parts = urlsplit(base)
    body = json.dumps({"py": str(d / "slow.py"), "html": str(d / "p.html")}).encode()
    sock = socket.create_connection((parts.hostname, parts.port))
    sock.sendall(
        b"POST /api/run HTTP/1.1\r\nHost: localhost\r\n"
        b"Content-Type: application/json\r\nX-Fused: 1\r\n"
        b"X-Fused-Page: " + str(d / "p.html").encode() + b"\r\n"
        b"X-Fused-Call: " + call_id.encode() + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    time.sleep(after)
    sock.close()


def test_a_run_with_a_body_completes_under_the_middleware(live_server):
    """Regression guard for a hang this feature nearly shipped.

    Detecting a client hang-up (SPEC CL-5a's gap) tempts you to poll
    `request.is_disconnected()` from the middleware. That call PEEKS by
    CONSUMING a message off the receive channel, so polling it steals the
    `http.request` body message the downstream route is waiting for, and every
    request with a body hangs forever. A body-less spike hides this completely —
    which is exactly how it got as far as it did.

    So: a real POST with a body, over a real socket, must return promptly. If
    receive-channel polling is ever re-added to the middleware, this fails.
    """
    import urllib.request

    base, d = live_server
    req = urllib.request.Request(
        base + "/api/run",
        data=json.dumps({"py": str(d / "slow.py"), "html": str(d / "p.html")}).encode(),
        headers={"Content-Type": "application/json", "X-Fused": "1",
                 "X-Fused-Page": str(d / "p.html"), "X-Fused-Call": "patient"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(req, timeout=20) as res:
        assert json.load(res)["ok"] is True
    # The run sleeps 2.5s; anything near the timeout means the body was starved.
    assert time.monotonic() - started < 15

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and calls.detail("patient") is None:
        time.sleep(0.2)
    assert calls.detail("patient")["outcome"] == "ok"


def test_an_abandoned_run_is_still_recorded(live_server):
    """A hung-up run must not vanish from the log — it is recorded, just with
    the wrong outcome until CL-5a's gap is closed. This pins today's behaviour
    so the day it changes, a test says so."""
    base, d = live_server
    _post_run_and_hang_up(base, d, "hung-up", after=0.4)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and calls.detail("hung-up") is None:
        time.sleep(0.2)
    got = calls.detail("hung-up")
    assert got is not None, "the abandoned run was never recorded at all"
    # KNOWN GAP (CL-5a): nobody reports a closed tab, so it reads as served. A
    # supersession, which the page DOES report, is marked correctly — see
    # test_an_abandoned_call_is_marked_superseded.
    assert got["outcome"] == "ok"
    assert got["server_ms"] >= 2000, "the run itself still ran to completion"


# ------------------------------------------------------- err_id as a join key

def test_a_500_puts_the_same_err_id_in_the_body_and_the_record(app_client, monkeypatch):
    """`err_id` was a dead field: documented as the join key to the failure, never
    set. A screenshot of the 500 and the record must now name each other."""
    from fused_render.server.routers import run as server_mod

    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(server_mod, "run_python", boom)
    client, d = app_client
    # TestClient re-raises server exceptions by default; we want the 500 body a
    # browser would actually receive.
    client = TestClient(client.app, raise_server_exceptions=False)
    res = client.post("/api/run",
                      json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                      headers=app_headers(d / "p.html", **{"X-Fused-Call": "boom-1"}),
                      )
    assert res.status_code == 500
    assert drain()
    got = calls.detail("boom-1")
    assert got["outcome"] == "error" and got["status"] == 500
    assert got["err_id"], "the record must carry the correlation id"
    assert got["err_id"] in res.json()["error"], "the 500 body must echo the same id"


# ------------------------------------------------------------- bounded buckets

def test_rate_buckets_do_not_grow_without_bound(store, monkeypatch):
    """One entry per page visited would otherwise live for the whole process."""
    landed = []
    monkeypatch.setattr(calls, "_ensure_writer", lambda: _CollectingQueue(landed))
    monkeypatch.setattr(calls, "BUCKETS_MAX", 8)
    for i in range(200):
        calls.record(rec(page=f"/app/p{i}.html"))
    assert len(calls._buckets) <= 9, f"buckets grew to {len(calls._buckets)}"
    assert len(landed) == 200, "eviction must not cost anyone their record"


# ---------------------------------------------------- hot-path prefs snapshot

def test_toggling_capture_takes_effect_on_the_next_call(app_client, monkeypatch):
    """The prefs snapshot is cached for a second to keep prefs.json off the hot
    path (it cost ~2.8 ms/run, most of the logging overhead). A write through the
    prefs endpoint must still be visible immediately — CT-5's no-restart rule."""
    client, d = app_client
    # FUSED_RENDER_CALLS is the PROCESS-level override and beats the pref by
    # design, so the pref cannot be exercised while the fixture sets it.
    monkeypatch.delenv("FUSED_RENDER_CALLS", raising=False)
    assert client.put("/api/prefs", json={"calls_enabled": False},
                      headers={"X-Fused": "1"}).status_code == 200
    client.post("/api/run", json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html", **{"X-Fused-Call": "while-off"}))
    time.sleep(0.5)
    assert calls.detail("while-off") is None, "capture stayed on past the toggle"

    client.put("/api/prefs", json={"calls_enabled": True}, headers={"X-Fused": "1"})
    client.post("/api/run", json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html", **{"X-Fused-Call": "while-on"}))
    assert drain()
    assert calls.detail("while-on") is not None, "capture stayed off past the toggle"


def test_null_fields_are_not_written(store):
    """A narrow record (a stat, a raw read) was mostly nulls — wasteful, and the
    field NAME `"error": null` made every healthy call render as ERROR in
    log_studio, which infers a level by sniffing the raw line for level words."""
    write_records([rec(call_id="thin", error=None, stdout_tail=None, params=None,
                       stderr_tail=None, err_id=None, result_rows=None)])
    line = open(calls.store_files()[0], encoding="utf-8").read().strip()
    assert "error" not in line, "a successful record must not contain the word 'error'"
    assert "null" not in line
    stored = json.loads(line)
    assert stored["call_id"] == "thin"
    assert "error" not in stored and stored.get("error") is None  # absent reads as null


def test_a_real_error_still_carries_its_detail(store):
    """Pruning must not strip a record that genuinely failed."""
    write_records([rec(call_id="failed", outcome="error",
                       error={"type": "ValueError", "message": "bad", "traceback": "tb"})])
    got = calls.detail("failed")
    assert got["error"]["type"] == "ValueError"
    line = open(calls.store_files()[0], encoding="utf-8").read()
    assert "error" in line, "a failed record SHOULD read as an error to a log viewer"


# ---------------------------------------- viewing the log must not grow the log

def test_viewing_the_log_is_recorded_like_any_other_call(app_client):
    """Reads of the store are ordinary calls — what a viewer costs to open a big
    log is worth knowing, and excluding them would be a special case in the
    record contract. What makes it safe is that nothing WATCHES a log file, so
    the read cannot trigger a reload that reads again (see the two tests below).
    """
    client, d = app_client
    write_records([rec(call_id="seed")])
    log = calls.store_files()[0]

    client.get(f"/api/fs/raw?path={log}",
               headers={"X-Fused-Page": "/x/log_studio/template.html",
                        "X-Fused-Target": log})
    assert drain()
    ids = [r["call_id"] for r in calls.query(limit=10)["records"]]
    assert "seed" in ids
    assert len(ids) == 2, "the read of the log should itself be recorded"
    read = next(r for r in calls.query(limit=10)["records"] if r["call_id"] != "seed")
    assert read["route"] == "/api/fs/raw"
    assert calls.is_log_file(read["entrypoint"])


def test_the_runtime_never_watches_a_call_log_file(store):
    """The loop is killed at its source: a page watching a log file would reload
    on the append its own read caused. Excluded in the runtime beside the
    existing mount-backed exclusion, so generic templates (code, duckdb, tree —
    none of which opt out of auto-reload) need to know nothing about it."""
    runtime = os.path.join(os.path.dirname(calls.__file__), "static", "runtime.js")
    src = open(runtime, encoding="utf-8").read()
    assert "function isCallLog(" in src
    assert "isMountBacked(p) || isCallLog(p)" in src
    # Every site that used to consult the mount exclusion must use the union.
    assert "if (isUnwatchable(p)) return;" in src
    assert "if (own && !isUnwatchable(own)) watched.add(own);" in src
    assert "if (file && !isUnwatchable(file)) watched.add(file);" in src


def test_config_publishes_the_store_so_the_runtime_can_skip_it(app_client):
    """The runtime learns the prefix/suffix from the server, like mounts_root —
    templates stay ignorant of the call log."""
    client, _ = app_client
    cfg = client.get("/api/config").json()
    assert cfg["calls_dir"] == canonical_fs_path(os.path.abspath(calls.store_dir()))
    assert cfg["calls_suffix"] == calls.SUFFIX


def test_the_published_store_path_is_canonical():
    """`abspath` is backslashed on Windows; every path the runtime holds is
    forward-slashed. Handing the raw form over would make `isCallLog`'s prefix
    test dead code there — a call-log file whose name was changed would be
    watched, and viewing it appends to the store, which is the reload loop the
    exclusion exists to prevent. (The suffix half still covers every file the
    writer names itself, which is why this is quiet rather than obvious.)

    Asserted against the CODEC rather than against a hardcoded expectation, so
    this says "canonicalized" on every platform instead of "unchanged" on POSIX.
    """
    from fused_render.server.routers import config as _server_config

    src = open(_server_config.__file__, encoding="utf-8").read()
    line = next(ln for ln in src.splitlines() if '"calls_dir"' in ln)
    assert "canonical_fs_path(" in line, (
        "the published path must go through the codec — os.path.abspath alone "
        "is the Windows-backslash form the runtime can never match"
    )


def test_a_normal_page_is_still_logged_while_the_log_is_open(app_client):
    """The exclusion must be about the TARGET, not a blanket mute — real activity
    still has to be recorded while you sit watching the log."""
    client, d = app_client
    client.post("/api/run", json={"py": str(d / "ok.py"), "html": str(d / "p.html")},
                headers=app_headers(d / "p.html", **{"X-Fused-Call": "real-work"}))
    assert drain()
    assert calls.detail("real-work") is not None


@pytest.mark.parametrize("name,expected", [
    ("2026-07-24-1-001.calls.jsonl", True),
    ("notes.jsonl", False),
    ("sine.html", False),
])
def test_is_log_file_matches_the_stores_own_files(store, name, expected):
    assert calls.is_log_file(os.path.join("/somewhere", name)) is expected
    # Anything sitting IN the store counts, whatever it is named.
    assert calls.is_log_file(os.path.join(calls.store_dir(), "renamed.txt")) is True


# ------------------------------------------------- a conventional level (CL-2)

@pytest.mark.parametrize("record,level", [
    ({"outcome": "ok"}, "INFO"),
    ({"outcome": "error"}, "ERROR"),
    ({"outcome": "conflict"}, "ERROR"),
    ({"outcome": "readonly"}, "WARN"),
    # Work thrown away by latest-wins cancellation is normal for a slider, not a
    # warning — its significance is in aggregate, which the calls view shows.
    ({"outcome": "superseded"}, "DEBUG"),
    ({"outcome": "disconnected"}, "DEBUG"),
    ({"kind": "page-error", "outcome": "error"}, "ERROR"),
])
def test_records_carry_a_conventional_level(store, record, level):
    write_records([rec(**record)])
    assert calls.detail(calls.query(limit=1)["records"][0]["call_id"])["level"] == level


def test_a_generic_log_viewer_reads_the_level_not_the_payload(store):
    """`level` exists so an ordinary log viewer is useful on this file. It must be
    emitted EARLY, because such a viewer takes the FIRST level word in the line —
    otherwise a path like /x/error-demo.html outvotes the real severity."""
    import importlib.util

    path = os.path.join(os.path.dirname(calls.__file__), "templates", "log_studio",
                        "reader.py")
    spec = importlib.util.spec_from_file_location("ls", path)
    log_studio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(log_studio)

    healthy = json.dumps(calls._prune(rec(outcome="ok", page="/x/error-demo.html")))
    failed = json.dumps(calls._prune(
        rec(outcome="error", error={"type": "ValueError", "message": "m", "traceback": "tb"})))
    assert log_studio._level(healthy) == "INFO", "a healthy call must not read as an error"
    assert log_studio._level(failed) == "ERROR"


def run_cli(monkeypatch, capsys, *argv):
    """Invoke `fused-render calls …` in-process and return its stdout."""
    from fused_render import cli

    monkeypatch.setattr("sys.argv", ["fused-render", "calls", *argv])
    try:
        cli.main()
    except SystemExit as exit_code:  # argparse/--since validation
        assert exit_code.code in (0, None), exit_code
    return capsys.readouterr().out


def test_follow_timeout_shows_no_records_at_all(store, monkeypatch, capsys):
    """The trap --follow exists to close: an agent waits for activity, gets none,
    and is handed the ordinary digest of PRE-EXISTING records — which reads
    exactly like a successful verification."""
    write_records([rec(call_id="historical", entrypoint="/app/old.py",
                       entrypoint_name="old.py")])
    out = run_cli(monkeypatch, capsys, "--follow", "--timeout", "1", "--since", "all")
    assert "no new calls within 1s" in out
    assert "historical" not in out and "old.py" not in out


def test_follow_timeout_is_explicit_in_json(store, monkeypatch, capsys):
    write_records([rec(call_id="historical")])
    body = json.loads(run_cli(monkeypatch, capsys,
                              "--follow", "--timeout", "1", "--json", "--since", "all"))
    assert body["timed_out"] is True and body["followed"] is True
    assert body["records"] == [] and body["page_errors"] == []


def test_follow_reports_only_what_arrived(store, monkeypatch, capsys):
    """When something DOES arrive, the digest is what is new — not the history
    that was already there when the wait began."""
    import threading

    write_records([rec(call_id="historical")])

    def append_later():
        time.sleep(1.5)
        write_records([rec(call_id="fresh", outcome="error",
                           error={"type": "ValueError", "message": "boom",
                                  "traceback": "tb"})])

    thread = threading.Thread(target=append_later, daemon=True)
    thread.start()
    body = json.loads(run_cli(monkeypatch, capsys,
                              "--follow", "--timeout", "15", "--json", "--since", "all"))
    thread.join(timeout=5)
    assert body["timed_out"] is False
    assert [r["call_id"] for r in body["records"]] == ["fresh"]


def test_json_always_carries_page_errors(store, monkeypatch, capsys):
    """`failures` excludes page errors by construction (they are not call
    failures), so the default machine-readable output was hiding the single most
    informative record from the consumer that surface exists for."""
    write_records([
        rec(call_id="fine", outcome="ok"),
        rec(call_id="js-died", kind="page-error", outcome="error", route=None,
            entrypoint=None, entrypoint_name=None,
            error={"type": "TypeError", "message": "freq is not defined",
                   "traceback": "at draw"}),
    ])
    body = json.loads(run_cli(monkeypatch, capsys, "--json", "--since", "all"))
    assert [r["call_id"] for r in body["page_errors"]] == ["js-died"]
    assert body["page_errors"][0]["error"]["type"] == "TypeError"
    # Without --verbose the healthy call is still summarised, not dumped.
    assert "fine" not in [r["call_id"] for r in body["records"]]
    body = json.loads(run_cli(monkeypatch, capsys, "--json", "--verbose", "--since", "all"))
    assert {"fine", "js-died"} <= {r["call_id"] for r in body["records"]}


def test_a_stale_cursor_is_bounded_and_reported(store, monkeypatch, capsys):
    """A cursor purged by retention (or simply wrong) never matched, so the walk
    never ended and the "page" was every matching record in the store."""
    write_records([rec(call_id=f"c{i}") for i in range(50)])
    page = calls.query(limit=5, cursor="long-since-purged")
    assert len(page["records"]) == 5, "the limit must bind while seeking"
    assert page["cursor_missing"] is True

    found = calls.query(limit=5, cursor="c49")  # the newest record
    assert found["cursor_missing"] is False

    out = run_cli(monkeypatch, capsys, "--since-cursor", "long-since-purged",
                  "--since", "all")
    assert "was not found" in out


# --------------------------------- cursor paging + bounded digests (2nd review)

def test_a_cursor_beyond_the_page_is_not_reported_as_missing(store):
    """Bugbot's second-pass catch, and a regression from the first fix: with more
    newer records than `limit`, the walk filled the page before reaching the
    cursor, called the cursor missing (it was not), and advanced the cursor past
    every record in between — losing them silently and permanently."""
    write_records([rec(call_id=f"c{i:02d}") for i in range(40)])
    page = calls.query(limit=5, cursor="c00")  # 39 records are newer than c00
    assert len(page["records"]) == 5
    assert page["cursor_missing"] is False, "the cursor exists — it is just beyond the page"
    # The loss is now stated instead of silent, so a caller can raise its limit.
    assert page["skipped"] == 34 and page["more_available"] is True

    whole = calls.query(limit=100, cursor="c00")
    assert len(whole["records"]) == 39 and whole["skipped"] == 0
    assert whole["more_available"] is False


def test_a_genuinely_absent_cursor_is_still_reported(store):
    write_records([rec(call_id=f"c{i}") for i in range(10)])
    page = calls.query(limit=5, cursor="never-existed")
    assert page["cursor_missing"] is True
    assert page["scan_truncated"] is False, "a small store is walked exhaustively"
    assert len(page["records"]) == 5, "still bounded"


def test_aggregates_can_describe_a_given_record_set(store):
    """A cursor-bounded read needs its overview/targets to cover the SAME records,
    or the digest reports the window's history as though it were the new activity."""
    write_records([rec(call_id="old", entrypoint="/a/old.py", entrypoint_name="old.py")
                   for _ in range(5)])
    write_records([rec(call_id="new", entrypoint="/a/new.py", entrypoint_name="new.py")])

    assert calls.overview()["total"] == 6           # whole store
    fresh = calls.query(limit=10)["records"][:1]
    assert calls.overview(records=fresh)["total"] == 1
    assert [t["name"] for t in calls.targets(records=fresh)["targets"]] == ["new.py"]


def test_follow_timeout_json_aggregates_are_empty_too(store, monkeypatch, capsys):
    """A historical `overview` beside "records": [] reads as fresh activity."""
    write_records([rec(call_id="historical") for _ in range(4)])
    body = json.loads(run_cli(monkeypatch, capsys,
                              "--follow", "--timeout", "1", "--json", "--since", "all"))
    assert body["timed_out"] is True
    assert body["overview"]["total"] == 0, "aggregates must cover the empty answer"
    assert body["targets"] == [] and body["records"] == []


def test_a_cursor_bounded_digest_summarises_only_new_records(store, monkeypatch, capsys):
    write_records([rec(call_id=f"old{i}", entrypoint="/a/old.py",
                       entrypoint_name="old.py") for i in range(5)])
    first = calls.query(limit=1)["cursor"]
    write_records([rec(call_id="fresh", entrypoint="/a/new.py", entrypoint_name="new.py")])

    body = json.loads(run_cli(monkeypatch, capsys, "--json", "--since", "all",
                              "--since-cursor", first))
    assert body["bounded_by_cursor"] is True
    assert body["overview"]["total"] == 1, "the digest must not include the history"
    assert [t["name"] for t in body["targets"]] == ["new.py"]


# ------------------------- append order vs call-start order (3rd Bugbot review)

def test_a_window_does_not_miss_calls_hidden_behind_a_long_one(store):
    """The file is ordered by COMPLETION, not by call start: `occurred_at` is
    stamped in begin() while the line is appended in finish(), so a long call
    sits at the tail carrying an old start time. Breaking the reverse walk on
    `occurred_at` stopped there and skipped newer short calls appended before it
    — a 15s window over ordinary overlapping traffic returned NOTHING.
    """
    def iso(offset):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")

    # Two appends, so the slow call really does land after the fast one.
    write_records([rec(call_id="fast", occurred_at=iso(-5), server_ms=20)])
    write_records([rec(call_id="slow", occurred_at=iso(-40), server_ms=40_000)])

    window = time.time() - 15
    assert [r["call_id"] for r in calls.query(limit=10, since=window)["records"]] == ["fast"]
    # The aggregates read through the same walk, so they under-reported too.
    assert calls.overview(since=window)["total"] == 1
    assert calls.series(bucket_ms=60_000, since=window)["points"][0]["count_ok"] == 1
    # `slow` started outside the window, so excluding it is correct — it is only
    # the EARLY STOP that must not be driven by its stamp.
    assert {r["call_id"] for r in calls.query(limit=10)["records"]} == {"fast", "slow"}


def test_records_carry_their_append_time(store):
    before = time.time()
    write_records([rec(call_id="stamped", occurred_at="2020-01-01T00:00:00.000Z")])
    got = calls.detail("stamped")
    assert got["recorded_at"] >= before, "the append time, not the call's start"
    assert got["occurred_at"] == "2020-01-01T00:00:00.000Z", "the start time is preserved"


def test_a_legacy_record_without_an_append_time_never_stops_the_walk(store):
    """Correctness over speed on a store written before `recorded_at` existed."""
    os.makedirs(store, exist_ok=True)
    path = app_current_file()
    old = dict(rec(call_id="legacy", occurred_at="2020-01-01T00:00:00.000Z"))
    fresh = calls._prune(rec(call_id="fresh"))
    with open(path, "w") as fh:  # legacy line first, so a break would hide `fresh`
        fh.write(json.dumps(old) + "\n" + json.dumps(fresh) + "\n")
    seen = [r["call_id"] for r in calls._iter_records([path], since=time.time() - 3600)]
    assert "fresh" in seen and "legacy" in seen


# ------------------ merging same-day files by append time (4th Bugbot review)

def same_day(pid, part=0, when=None):
    """A store file named for TODAY under an arbitrary pid — the shape two live
    servers (or a restart) produce, and which name order cannot rank in time."""
    return in_store(calls.store_dir(),
                    f"{calls.day_stamp(when)}-{pid}-{part:03d}.calls.jsonl")


def appended(cid, when, **over):
    """A production-shaped line carrying an explicit append stamp."""
    return json.dumps(dict(calls._prune(rec(call_id=cid, **over)), recorded_at=when))


def test_same_day_files_from_two_servers_merge_newest_first(store):
    """Name order is date, then pid, then part — and pid order says NOTHING about
    time. Worse, it is lexical, so pid 8000 sorts AFTER pid 12345. Reading one
    file out before the next therefore returned a stale process's tail as
    "newest"; the day's files have to be merged on append time instead.
    """
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(same_day(8000), "w") as fh:  # sorts LAST, holds the OLDEST records
        fh.write(appended("stale-1", now - 600) + "\n")
        fh.write(appended("stale-2", now - 590) + "\n")
    with open(same_day(12345), "w") as fh:  # sorts first, holds the newest
        fh.write(appended("fresh-1", now - 5) + "\n")
        fh.write(appended("fresh-2", now - 1) + "\n")

    order = [r["call_id"] for r in calls._iter_records(calls.store_files())]
    assert order == ["fresh-2", "fresh-1", "stale-2", "stale-1"]
    assert calls.query(limit=1)["cursor"] == "fresh-2", "the globally newest call"
    assert [r["call_id"] for r in calls.query(limit=3)["records"]] == \
        ["fresh-2", "fresh-1", "stale-2"]


def test_follow_sees_a_write_to_a_lower_sorting_pid_file(store):
    """`--follow` waits for `query(limit=1)["cursor"]` to change. While same-day
    files were read one after another, a write from the lexically-earlier pid was
    reached only after the other file ran out — so the cursor never moved and
    following a live server waited out its whole timeout with activity in front
    of it.
    """
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(same_day(8000), "w") as fh:  # a stale server that sorts last
        fh.write(appended("stale", now - 600) + "\n")
    live = same_day(12345)
    with open(live, "w") as fh:
        fh.write(appended("seen", now - 10) + "\n")

    baseline = calls.query(limit=1)["cursor"]
    assert baseline == "seen"
    with open(live, "a") as fh:  # the live server logs another call
        fh.write(appended("arrived", time.time()) + "\n")
    assert calls.query(limit=1)["cursor"] == "arrived", "follow must wake on this"


def test_days_stay_newest_first_across_the_merge(store):
    """Merging is per day: a day's files interleave, whole days do not."""
    os.makedirs(store, exist_ok=True)
    now = time.time()
    yesterday = now - 86_400
    with open(same_day(8000, when=yesterday), "w") as fh:
        fh.write(appended("y-1", yesterday) + "\n")
    with open(same_day(12345, when=yesterday), "w") as fh:
        fh.write(appended("y-2", yesterday + 60) + "\n")
    with open(same_day(999), "w") as fh:
        fh.write(appended("t-1", now - 5) + "\n")

    order = [r["call_id"] for r in calls._iter_records(calls.store_files())]
    assert order == ["t-1", "y-2", "y-1"], "today's day-group first, then yesterday's"


def test_a_page_satisfied_by_today_never_opens_an_older_day(store, monkeypatch):
    """Grouping by day keeps the walk lazy — merging the whole store at once
    would open every file up front to answer a question today already covers."""
    os.makedirs(store, exist_ok=True)
    old = same_day(1, when=time.time() - 86_400)
    with open(old, "w") as fh:
        fh.write(appended("old", time.time() - 86_400) + "\n")
    with open(same_day(2), "w") as fh:
        for i in range(50):
            fh.write(appended(f"t-{i}", time.time() - 50 + i) + "\n")

    opened = []
    real = calls._iter_lines_reverse

    def spy(path, *a, **kw):
        opened.append(os.path.basename(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(calls, "_iter_lines_reverse", spy)
    got = list(itertools.islice(calls._iter_records(calls.store_files()), 3))
    assert [r["call_id"] for r in got] == ["t-49", "t-48", "t-47"]
    assert os.path.basename(old) not in opened, "yesterday must stay unopened"


def test_an_abandoned_merge_closes_the_day_s_files(store):
    """A full page or an exhausted budget abandons the walk mid-merge; the day's
    other file handles must not be left to the garbage collector."""
    os.makedirs(store, exist_ok=True)
    now = time.time()
    for pid in (8000, 12345, 999):
        with open(same_day(pid), "w") as fh:
            for i in range(40):
                fh.write(appended(f"c-{pid}-{i}", now - 100 + i) + "\n")

    def fds():
        return len(os.listdir("/proc/self/fd"))

    before = fds()
    walk = calls._iter_records(calls.store_files())
    next(walk)  # opens all three of the day's files to merge them
    assert fds() > before, "the merge really does hold several files open"
    walk.close()
    assert fds() == before, "closing the walk closes every stream it opened"


def test_a_legacy_record_still_appears_when_a_day_is_merged(store):
    """A record from before `recorded_at` existed has no merge key of its own;
    it falls back to its start time rather than dropping out of the order."""
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(same_day(8000), "w") as fh:
        fh.write(json.dumps(rec(call_id="legacy",
                                occurred_at="2020-01-01T00:00:00.000Z")) + "\n")
    with open(same_day(12345), "w") as fh:
        fh.write(appended("modern", now - 1) + "\n")

    order = [r["call_id"] for r in calls._iter_records(calls.store_files())]
    assert order == ["modern", "legacy"], "the stampless record sorts oldest, not gone"


def test_an_unrecognised_file_name_keeps_its_place(store):
    """A name that carries no date gets its own group rather than being merged
    into a day it may not belong to."""
    os.makedirs(store, exist_ok=True)
    odd = in_store(store, "stray.calls.jsonl")
    with open(odd, "w") as fh:
        fh.write(appended("stray", time.time() - 10) + "\n")
    with open(same_day(1), "w") as fh:
        fh.write(appended("dated", time.time() - 5) + "\n")
    ids = [r["call_id"] for r in calls._iter_records(calls.store_files())]
    assert sorted(ids) == ["dated", "stray"], "both are read"


# --------------------------- the oversized path (4th Bugbot review, finding 1)

def test_an_oversized_record_is_still_pruned_and_levelled(store):
    """`_shrink` marks dropped fields by setting them to None, so its result has
    to go back through `_prune`. Serializing it directly wrote those fields as
    explicit nulls and omitted `level`/`recorded_at` — reviving, on exactly the
    records most likely to matter, the `"error": null` field-name match that made
    a generic log viewer read every healthy call as ERROR.
    """
    write_records([rec(call_id="huge", error=None, params=None, stderr_tail=None,
                       stdout_tail="x" * (calls.RECORD_CAP + 100))])
    line = open(app_current_file()).read().strip()
    assert len(line.encode()) < calls.RECORD_CAP, "the shrink actually shrank it"
    got = json.loads(line)
    assert [k for k, v in got.items() if v is None] == [], "no null-valued keys"
    assert got["level"] == "INFO", "a healthy oversized call is not an error"
    assert got["truncated"] is True and "stdout_tail" not in got
    assert isinstance(got["recorded_at"], (int, float)), "the walk can stop on it"


def test_an_oversized_failure_keeps_its_error_level(store):
    """Shrinking must not cost the record its severity."""
    write_records([rec(call_id="huge-fail", outcome="error",
                       error={"type": "ValueError", "message": "boom",
                              "traceback": "T" * (calls.RECORD_CAP + 100)})])
    got = json.loads(open(app_current_file()).read().strip())
    assert got["level"] == "ERROR" and got["outcome"] == "error"
    assert got["truncated"] is True
    assert got["error"]["message"] == "boom", "the skeleton of the error survives"


def test_prune_is_idempotent(store):
    """The oversized path prunes twice; a second pass must not reorder the head
    fields or recompute `level` differently."""
    once = calls._prune(rec(call_id="x", outcome="error", error=None))
    twice = calls._prune(once)
    assert list(twice) == list(once)
    assert twice == once


# ------------------------ the cursor must be an id you were shown (5th review)

def test_the_cursor_is_the_newest_matching_record_not_the_newest_record(store):
    """Taken before the filter, the cursor was an id the caller was never shown.

    That breaks the one thing a cursor is for. `--follow --page X` woke on
    unrelated traffic and then reported nothing for X — an agent waiting to
    verify the page it just wrote concludes it never ran.
    """
    os.makedirs(store, exist_ok=True)
    now = time.time()
    mine, other = "/app/mine.html", "/app/other.html"
    with open(app_current_file(), "w") as fh:
        fh.write(appended("mine-1", now - 100, page=mine) + "\n")
        fh.write(appended("other-1", now - 50, page=other) + "\n")  # newest overall

    got = calls.query(limit=5, page=mine)
    assert [r["call_id"] for r in got["records"]] == ["mine-1"]
    assert got["cursor"] == "mine-1", "an id in the caller's own stream"


def test_unrelated_traffic_does_not_move_a_filtered_cursor(store):
    """The wake check `--follow` performs is `query(limit=1, **filters)["cursor"]`,
    so a cursor that tracks the whole store makes every other page's call a
    spurious wake."""
    os.makedirs(store, exist_ok=True)
    now = time.time()
    mine, other = "/app/mine.html", "/app/other.html"
    path = app_current_file()
    with open(path, "w") as fh:
        fh.write(appended("mine-1", now - 100, page=mine) + "\n")

    baseline = calls.query(limit=1, page=mine)["cursor"]
    with open(path, "a") as fh:  # someone else's page runs
        fh.write(appended("other-1", time.time(), page=other) + "\n")
    assert calls.query(limit=1, page=mine)["cursor"] == baseline, "must not wake"

    with open(path, "a") as fh:  # now MY page runs
        fh.write(appended("mine-2", time.time(), page=mine) + "\n")
    assert calls.query(limit=1, page=mine)["cursor"] == "mine-2", "must wake"


def test_a_filtered_read_with_nothing_new_keeps_the_callers_cursor(store):
    """Returning None would read as "start over" and answer with an unbounded
    newest page, losing the caller's position."""
    os.makedirs(store, exist_ok=True)
    with open(app_current_file(), "w") as fh:
        fh.write(appended("mine-1", time.time() - 10, page="/app/mine.html") + "\n")
    got = calls.query(limit=5, cursor="mine-1", page="/app/mine.html")
    assert got["records"] == []
    assert got["cursor"] == "mine-1", "unchanged, not None"
    assert got["cursor_missing"] is False


def test_a_page_with_no_history_has_no_cursor(store):
    """No matching record means there is no id to resume from — None, so a
    follower's baseline moves the moment that page's first call lands."""
    os.makedirs(store, exist_ok=True)
    path = app_current_file()
    with open(path, "w") as fh:
        fh.write(appended("other-1", time.time() - 10, page="/app/other.html") + "\n")
    assert calls.query(limit=1, page="/app/mine.html")["cursor"] is None
    with open(path, "a") as fh:
        fh.write(appended("mine-1", time.time(), page="/app/mine.html") + "\n")
    assert calls.query(limit=1, page="/app/mine.html")["cursor"] == "mine-1"


def test_a_cursor_outside_the_filter_still_stops_the_walk(store):
    """The stop is by identity, checked before `_matches`, so an old cursor that
    no longer matches the current filters must not read as purged."""
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(app_current_file(), "w") as fh:
        fh.write(appended("other-1", now - 100, page="/app/other.html") + "\n")
        fh.write(appended("mine-1", now - 50, page="/app/mine.html") + "\n")
    got = calls.query(limit=5, cursor="other-1", page="/app/mine.html")
    assert got["cursor_missing"] is False, "found it, it just did not match"
    assert [r["call_id"] for r in got["records"]] == ["mine-1"]


def test_the_cli_omits_the_cursor_line_when_there_is_none(store, monkeypatch, capsys):
    """`cursor: None` invites passing it back verbatim."""
    os.makedirs(store, exist_ok=True)
    with open(app_current_file(), "w") as fh:
        fh.write(appended("other-1", time.time() - 10, page="/app/other.html") + "\n")
    out = run_cli(monkeypatch, capsys, "--page", "/app/mine.html")
    assert "cursor:" not in out
    assert "None" not in out


# ----------- follow must not wait out records already on disk (6th review)

def test_follow_answers_immediately_when_the_cursor_already_has_records(
        store, monkeypatch, capsys):
    """The normal race for an agent: it asks a human to open a page, the calls
    land, and only THEN does it run `--follow --since-cursor C`.

    Waiting for the store tip to move past the pre-wait baseline times out while
    holding the very records it was waiting for, and answers "nothing ran" — the
    fourth distinct cause of that one false negative.
    """
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(app_current_file(), "w") as fh:
        fh.write(appended("seen-1", now - 300) + "\n")
        fh.write(appended("arrived-a", now - 20) + "\n")  # already on disk
        fh.write(appended("arrived-b", now - 10) + "\n")

    started = time.monotonic()
    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "seen-1",
                  "--timeout", "30", "--since", "all", "--verbose")
    assert time.monotonic() - started < 5, "must not wait; the records are already here"
    assert "no new calls" not in out
    assert "2 record(s)" in out
    assert "cursor: arrived-b" in out


def test_follow_still_waits_when_the_caller_is_up_to_date(store, monkeypatch, capsys):
    """The negative case for the fix above: a cursor AT the tip means the caller
    has seen everything, so following must still wait rather than return at once."""
    os.makedirs(store, exist_ok=True)
    with open(app_current_file(), "w") as fh:
        fh.write(appended("tip", time.time() - 60) + "\n")
    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "tip",
                  "--timeout", "1", "--since", "all")
    assert "no new calls within 1s" in out
    assert "tip" not in out.replace("--since-cursor tip", "")


def test_follow_without_a_cursor_still_waits(store, monkeypatch, capsys):
    """And the plain case keeps waiting too — the early answer is gated on an
    explicit cursor, which is the only thing that says what the caller has seen."""
    os.makedirs(store, exist_ok=True)
    write_records([rec(call_id="historical")])
    out = run_cli(monkeypatch, capsys, "--follow", "--timeout", "1", "--since", "all")
    assert "no new calls within 1s" in out


# ------------- a deep cursor is not a missing cursor (6th review, finding 2)

def test_a_cursor_deeper_than_the_scan_budget_is_reported_as_such(
        store, monkeypatch, capsys):
    """`cursor_missing` alone cannot tell "purged" from "never reached".

    The seeking walk gives up after a bounded scan, so a perfectly valid cursor
    in a deep store reports missing. Claiming "purged by retention, or wrong"
    there is a confident statement about something that was never checked — and
    it hides that the records between the cursor and this page were skipped.
    """
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(app_current_file(), "w") as fh:  # deeper than the 5000 budget
        for i in range(5200):
            fh.write(appended(f"c{i:05d}", now - 5200 + i) + "\n")

    body = json.loads(run_cli(monkeypatch, capsys, "--since-cursor", "c00000",
                              "--limit", "5", "--json", "--since", "all"))
    assert body["cursor_missing"] is True
    assert body["scan_truncated"] is True, "absence here is a guess, not a proof"
    assert body["more_available"] is True and body["skipped"] > 0

    out = run_cli(monkeypatch, capsys, "--since-cursor", "c00000",
                  "--limit", "5", "--since", "all")
    assert "was not reached within the scan budget" in out
    assert "purged by retention" not in out, "must not claim what it did not check"


def test_a_genuinely_absent_cursor_still_says_purged(store, monkeypatch, capsys):
    """The negative case: in a store small enough to walk to the end, absence IS
    a proof, and the message should stay the confident one."""
    os.makedirs(store, exist_ok=True)
    write_records([rec(call_id="only")])
    out = run_cli(monkeypatch, capsys, "--since-cursor", "ghost", "--since", "all")
    assert "purged by retention" in out
    assert "scan budget" not in out
    body = json.loads(run_cli(monkeypatch, capsys, "--since-cursor", "ghost",
                              "--json", "--since", "all"))
    assert body["cursor_missing"] is True and body["scan_truncated"] is False


# ------- the gate must not mistake name order for time order (7th review)

def test_the_size_trim_drops_the_oldest_append_first(store):
    """The trim iterated in name order and called it oldest-first — true only at
    day granularity. mtime is exact."""
    os.makedirs(store, exist_ok=True)
    now = time.time()
    big = "x" * 300_000
    paths = {}
    # Yesterday, two pids: the lexically-EARLIER name is the OLDER append.
    for pid, age in (("12345", 100_000), ("8000", 90_000)):
        path = in_store(store, f"{calls.day_stamp(now - 86_400)}-{pid}-000.calls.jsonl")
        with open(path, "w") as fh:
            fh.write(appended(f"p{pid}", now - age, stdout_tail=big) + "\n")
        os.utime(path, (now - age,) * 2)
        paths[pid] = path

    monkey = 400_000  # forces exactly one file to be trimmed
    original = calls.DEFAULT_MAX_BYTES
    try:
        calls.DEFAULT_MAX_BYTES = monkey
        calls.sweep(now=now)
    finally:
        calls.DEFAULT_MAX_BYTES = original

    assert not os.path.exists(paths["12345"]), "the older APPEND goes first"
    assert os.path.exists(paths["8000"]), "the newer append survives"


# --- a cursor from a BROADER read is not the tip of a narrower one (8th review)

def test_follow_waits_when_a_foreign_cursor_has_no_matching_records(
        store, monkeypatch, capsys):
    """Regression in the D141 fix, found by the 8th review pass.

    Comparing `cursor != baseline` answers "is this the current tip", and a
    cursor from a BROADER read is not the tip of a narrower one. So the ordinary
    agent pattern — take a global cursor from `calls --json`, then
    `--follow --page X` — skipped the wait entirely, matched nothing, and
    reported "no calls recorded". The test has to be a real bounded read: are any
    MATCHING records newer than the cursor?
    """
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(app_current_file(), "w") as fh:
        fh.write(appended("mine-1", now - 300, page="/app/mine.html") + "\n")
        fh.write(appended("other-5", now - 60, page="/app/other.html") + "\n")

    started = time.monotonic()
    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "other-5",
                  "--page", "/app/mine.html", "--timeout", "1", "--since", "all")
    assert "no new calls within 1s" in out, "must wait, not answer at once"
    assert time.monotonic() - started >= 1.0, "the wait really happened"


def test_follow_answers_at_once_when_a_foreign_cursor_does_have_records(
        store, monkeypatch, capsys):
    """The other half: a broader cursor with matching records behind it is still
    the D141 case and must be answered immediately, not waited out."""
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(app_current_file(), "w") as fh:
        fh.write(appended("other-5", now - 300, page="/app/other.html") + "\n")
        fh.write(appended("mine-9", now - 30, page="/app/mine.html") + "\n")

    started = time.monotonic()
    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "other-5",
                  "--page", "/app/mine.html", "--timeout", "30", "--since", "all")
    assert time.monotonic() - started < 5, "mine-9 is newer than the cursor"
    assert "no new calls" not in out
    assert "1 record(s)" in out


def test_follow_waits_when_the_cursor_cannot_be_found(store, monkeypatch, capsys):
    """A cursor that is not in the store proves nothing about what arrived, so it
    must fall through to the wait rather than count as "already new"."""
    os.makedirs(store, exist_ok=True)
    with open(app_current_file(), "w") as fh:
        fh.write(appended("only", time.time() - 60, page="/app/mine.html") + "\n")
    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "ghost",
                  "--page", "/app/mine.html", "--timeout", "1", "--since", "all")
    assert "no new calls within 1s" in out


# ----------- a lost cursor must not resurface as history (9th review)

def test_follow_after_a_lost_cursor_reports_only_what_arrived(
        store, monkeypatch, capsys):
    """An unfindable cursor made the post-wait read fall back to "the newest
    page", which the bounded digest then summarised as what arrived — the trap
    `--follow` exists to close, reached by waiting SUCCESSFULLY rather than by
    timing out (the timeout path was already empty and correct)."""
    import threading

    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(app_current_file(), "w") as fh:
        for i in range(4):
            fh.write(appended(f"history-{i}", now - 500 + i,
                              entrypoint="/app/old.py", entrypoint_name="old.py") + "\n")

    def append_later():
        time.sleep(1.5)
        write_records([rec(call_id="arrived", entrypoint="/app/new.py",
                           entrypoint_name="new.py")])

    worker = threading.Thread(target=append_later)
    worker.start()
    try:
        out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "ghost",
                      "--timeout", "20", "--since", "all", "--verbose")
    finally:
        worker.join()

    assert "1 record(s)" in out, "only the arrival, not the 4 historical records"
    assert "new.py" in out and "old.py" not in out
    assert "was not found" in out, "the caller still learns its cursor was unusable"


def test_a_lost_cursor_is_still_reported_in_json(store, monkeypatch, capsys):
    """Resuming from the baseline must not hide that the cursor was unusable."""
    os.makedirs(store, exist_ok=True)
    write_records([rec(call_id="only")])
    body = json.loads(run_cli(monkeypatch, capsys, "--follow", "--since-cursor",
                              "ghost", "--timeout", "1", "--json", "--since", "all"))
    assert body["timed_out"] is True  # nothing arrived, so this path is the empty one
    assert body["cursor_missing"] is True, "the timeout path must report it too"
    body = json.loads(run_cli(monkeypatch, capsys, "--since-cursor", "ghost",
                              "--json", "--since", "all"))
    assert body["cursor_missing"] is True


# ----------- a timeout must not swallow the lost cursor (10th review, finding 1)

def test_a_timeout_after_a_lost_cursor_says_so_in_text(store, monkeypatch, capsys):
    """Timing out does not make the cursor findable.

    The timeout branch returns before both output sites, so the fix that plumbed
    `cursor_lost` through reached the caller only when activity happened to
    arrive — i.e. a wrong cursor was reported when it mattered least and stayed
    silent in the case that actually needs explaining ("nothing ran" vs "I could
    not tell you what is new"). This is the same D144 promise, on the branch that
    the assertion for it did not cover.
    """
    os.makedirs(store, exist_ok=True)
    write_records([rec(call_id="only")])
    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "ghost",
                  "--timeout", "1", "--since", "all")
    assert "no new calls within 1s" in out
    assert "was not found" in out, "a lost cursor is never silent — including here"
    assert "purged by retention" in out


def test_a_timeout_with_a_usable_cursor_claims_nothing_about_it(
        store, monkeypatch, capsys):
    """The negative case: an ordinary quiet wait must stay a bare "nothing ran".
    Reporting a lost cursor unconditionally would make every timeout look like a
    caller error."""
    os.makedirs(store, exist_ok=True)
    write_records([rec(call_id="tip")])
    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "tip",
                  "--timeout", "1", "--since", "all")
    assert "no new calls within 1s" in out
    assert "was not found" not in out and "scan budget" not in out
    body = json.loads(run_cli(monkeypatch, capsys, "--follow", "--since-cursor",
                              "tip", "--timeout", "1", "--json", "--since", "all"))
    assert body["timed_out"] is True
    assert body["cursor_missing"] is False and body["scan_truncated"] is False


def test_a_deep_cursor_is_not_called_purged_on_the_follow_path(
        store, monkeypatch, capsys):
    """The proof/guess split has to hold wherever the reason is stated.

    The follow path learns "missing" from its own probe, not from the post-wait
    read — which by then is anchored on the baseline and is no longer looking for
    the caller's cursor at all. So the probe's `scan_truncated` has to be carried
    separately, or a valid-but-deep cursor gets the confident "purged" wording on
    every follow.
    """
    os.makedirs(store, exist_ok=True)
    now = time.time()
    with open(app_current_file(), "w") as fh:  # deeper than the 5000 budget
        for i in range(5200):
            fh.write(appended(f"c{i:05d}", now - 5200 + i) + "\n")

    out = run_cli(monkeypatch, capsys, "--follow", "--since-cursor", "c00000",
                  "--timeout", "1", "--since", "all")
    assert "was not reached within the scan budget" in out
    assert "purged by retention" not in out, "must not claim what it did not check"
    body = json.loads(run_cli(monkeypatch, capsys, "--follow", "--since-cursor",
                              "c00000", "--timeout", "1", "--json", "--since", "all"))
    assert body["cursor_missing"] is True and body["scan_truncated"] is True


# --------- the view must not drop an overlapping reload (10th review, finding 2)

def test_an_invalidation_during_a_prefs_read_is_not_lost(store, monkeypatch):
    """The prefs.json read happens outside the lock, so an invalidation can land
    mid-read. Storing the result unconditionally repopulated the cache with the
    PRE-write snapshot and served it for the whole TTL — so a capture toggle
    appeared not to take effect, defeating CL-14a's invalidate-on-write rule.
    """
    from fused_render.shell import prefs as shell_prefs

    calls.invalidate_prefs_cache()
    real_enabled = shell_prefs.calls_enabled

    def slow_enabled():
        value = real_enabled()
        # The prefs endpoint writes and invalidates while this read is in flight.
        calls.invalidate_prefs_cache()
        return value

    monkeypatch.setattr(shell_prefs, "calls_enabled", slow_enabled)
    calls._prefs_snapshot()
    assert calls._prefs_cache is None, "the superseded read must not be cached"


def test_the_prefs_snapshot_still_caches_when_nothing_invalidates(store):
    """The negative case: the generation guard must not disable caching."""
    calls.invalidate_prefs_cache()
    calls._prefs_snapshot()
    assert calls._prefs_cache is not None, "an uncontended read is still cached"


# ------ the gate must probe the dir the writer writes to (11th review)

# Every rule the raw env value gets wrong, plus the plain cases as controls.
BRANCH_REFS = [
    "",                                     # explicit baseline opt-out
    "main", "MAIN", "master", "head",       # default branches -> baseline, NOT nested
    "Feature_X",                            # case + separator normalisation
    "claude/fused-api-call-logging-d97w88",  # truncation, and a '/' that would nest
    "release/2.0",
    "---",                                  # collapses to nothing -> baseline
    "x" * 40,                               # long ref -> truncated
    "feature-x",                            # already canonical (must not change)
]


def test_the_store_is_the_logs_dir_under_the_shell_home(tmp_path, monkeypatch):
    """The store's directory NAME, pinned as a literal.

    Every other test here is relative to `store_dir()`, so the whole suite would
    pass if the leaf were renamed — while every store already on disk was
    orphaned and every page silently lost its history. The one fact worth
    spelling out is the one an edit in a single place can change invisibly.

    """
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)

    assert calls.store_dir() == os.path.join(str(tmp_path), "logs")


# Windows is simulated with `ntpath` and with literal drive-shaped strings
# rather than skipped behind a sys.platform guard: the bug is a disagreement
# between two *string* forms of a path, so it reproduces and stays fixed on
# any host. A platform guard here would mean the regression only ever ran on
# the one CI leg that does not exist for this suite.

WIN_PAGE = "C:/Users/foo/app/mine.html"      # X-Fused-Page: shell canonical
WIN_PY = "C:/Users/foo/app/sine.py"          # the same, for the .py target


def _run_resolved(html: str, py: str) -> str:
    """What /api/run computes for a relative `py` (server.py), on Windows."""
    import ntpath
    return ntpath.normpath(ntpath.join(ntpath.dirname(html), py))


def test_run_resolved_target_is_stored_canonically(store, monkeypatch):
    """The producer this whole finding turns on.

    /api/run builds its target with os.path.normpath, which on Windows answers
    with backslashes, while every other field arrives forward-slashed from a
    header. record() is the single write point, so it is where the store is
    made to hold one form.
    """
    backslashed = _run_resolved(WIN_PAGE, "sine.py")
    assert "\\" in backslashed, "precondition: normpath gives the OS-native form"

    landed = []
    monkeypatch.setattr(calls, "_ensure_writer", lambda: _CollectingQueue(landed))
    calls.record(rec(page=WIN_PAGE, entrypoint=backslashed))

    assert landed[0]["entrypoint"] == WIN_PY
    assert "\\" not in landed[0]["entrypoint"]


def test_every_path_field_is_canonicalized_on_write(store, monkeypatch):
    """All three fields _matches compares, not just the one that was reported."""
    landed = []
    monkeypatch.setattr(calls, "_ensure_writer", lambda: _CollectingQueue(landed))
    calls.record(rec(page="C:\\Users\\foo\\app\\mine.html",
                     target_file="C:\\Users\\foo\\data\\t.parquet",
                     entrypoint="C:\\Users\\foo\\app\\sine.py"))

    got = landed[0]
    assert got["page"] == WIN_PAGE
    assert got["target_file"] == "C:/Users/foo/data/t.parquet"
    assert got["entrypoint"] == WIN_PY


def test_a_posix_backslash_filename_is_not_mangled(store, monkeypatch):
    """The negative case that forbids an unconditional replace.

    On POSIX a backslash is a legal filename character. Canonicalizing it would
    silently rewrite the page's identity — the record would name a file that
    does not exist, and the page's own filter would then miss it.
    """
    odd = "/app/we\\ird.html"
    landed = []
    monkeypatch.setattr(calls, "_ensure_writer", lambda: _CollectingQueue(landed))
    calls.record(rec(page=odd, entrypoint="/app/d.py"))

    assert landed[0]["page"] == odd
    assert calls._matches(landed[0], page=odd) is True


def log_a_windows_run(call_id="win"):
    """Log one record through the REAL write path, shaped the way /api/run
    shapes it on Windows: a canonical page from the header, a backslashed
    target from normpath.

    Deliberately `record()` + `drain()` rather than the `write_records`
    shortcut — the shortcut appends straight to disk, which would put the
    canonicalization under test on the wrong side of the seam and let these
    read-side assertions pass on the broken parent commit.
    """
    calls.record(rec(call_id=call_id, page=WIN_PAGE,
                     entrypoint=_run_resolved(WIN_PAGE, "sine.py"),
                     entrypoint_name="sine.py"))
    assert drain(), "the writer did not land the record"


def test_windows_page_filter_finds_its_records(store):
    """The user-visible failure: on Windows `calls --page` matched nothing at
    all — not even the page the caller was standing on — because the CLI's
    abspath answered with backslashes and the store held forward slashes."""
    import ntpath

    from fused_render._view_url_codec import canonical_fs_path

    log_a_windows_run()

    # Exactly the expression cli.py computes, with ntpath standing in for
    # Windows' os.path.
    cli_filter = canonical_fs_path(ntpath.abspath(WIN_PAGE))
    assert cli_filter == WIN_PAGE

    got = calls.query(limit=10, page=cli_filter)["records"]
    assert [r["call_id"] for r in got] == ["win"]

    # …and the raw abspath, which is what shipped, finds nothing. Asserted so
    # the regression is pinned to the cause and not just to the symptom.
    assert calls.query(limit=10, page=ntpath.abspath(WIN_PAGE))["records"] == []


def test_windows_calls_view_on_a_py_finds_its_runs(store):
    """A `.py` is never a `page`; the Calls view matches it via `entrypoint`.
    With the target stored backslashed, the view on a data file that the gate
    had just confirmed has history rendered empty."""
    log_a_windows_run("run")

    got = calls.query(limit=10, page=WIN_PY)["records"]
    assert [r["call_id"] for r in got] == ["run"]


def test_windows_entrypoint_substring_filter_matches(store):
    """`--entrypoint` is a substring filter: a full drive path is canonicalized
    to match the store, and a bare fragment still works untouched."""
    from fused_render._view_url_codec import canonical_fs_path

    log_a_windows_run("run")

    full = canonical_fs_path("C:\\Users\\foo\\app\\sine.py")
    assert [r["call_id"] for r in calls.query(limit=10, entrypoint=full)["records"]] == ["run"]
    assert [r["call_id"] for r in calls.query(limit=10, entrypoint="sine.py")["records"]] == ["run"]


def test_posix_page_filter_still_works_through_the_cli(store, monkeypatch, capsys):
    """Guard on the ordinary path: the canonicalization must be a no-op here."""
    write_records([calls._prune(rec(call_id="posix", page="/app/p.html",
                                    entrypoint="/app/d.py", entrypoint_name="d.py"))])
    out = run_cli(monkeypatch, capsys, "--page", "/app/p.html", "--since", "all")
    assert "d.py" in out


# -- override resolvers: set vs in force (Bugbot #283 review, D150) ------------


def test_retention_days_override_reports_only_values_it_honours(monkeypatch):
    """`retention_days_override()` is the one answer to "is the env var in
    force" — None whenever `retention_days()` falls back to the pref.

    `0` is in the honoured set on purpose: it is a real override (age pruning
    off, size cap only), and the empty string next to it is the case a truthiness
    check conflates it with.
    """
    for raw, expected in (("7", 7), ("0", 0), ("-5", 0), ("  9 ", 9)):
        monkeypatch.setenv(calls.RETENTION_DAYS_ENV, raw)
        assert calls.retention_days_override() == expected, raw

    for raw in ("", "abc", "-", "3.5", "  ", "1d"):
        monkeypatch.setenv(calls.RETENTION_DAYS_ENV, raw)
        assert calls.retention_days_override() is None, raw

    monkeypatch.delenv(calls.RETENTION_DAYS_ENV, raising=False)
    assert calls.retention_days_override() is None


def test_retention_days_is_the_override_or_the_pref(monkeypatch, tmp_path):
    """The resolver and the getter cannot disagree: `retention_days()` returns
    the override when there is one and the stored pref when there is not."""
    from fused_render.shell import prefs

    monkeypatch.setattr(prefs.storage, "home_dir", lambda: str(tmp_path))
    prefs.storage.write_json(prefs._path(), {"calls_retention_days": 30})

    for raw in ("7", "0", "", "abc"):
        monkeypatch.setenv(calls.RETENTION_DAYS_ENV, raw)
        calls.invalidate_prefs_cache()
        override = calls.retention_days_override()
        assert calls.retention_days() == (30 if override is None else override), raw


def test_enabled_override_is_none_only_when_the_var_is_unset(monkeypatch):
    """Capture's variable differs from retention's: every set value decides
    something, so presence and force coincide. Pinned so the shared `forced_by`
    plumbing has a stated contract on both vars rather than one by accident.
    """
    for raw in ("0", "false", "no", "off", "OFF", " 0 "):
        monkeypatch.setenv(calls.DISABLE_ENV, raw)
        assert calls.enabled_override() is False, raw

    for raw in ("1", "yes", "", "garbage"):
        monkeypatch.setenv(calls.DISABLE_ENV, raw)
        assert calls.enabled_override() is True, raw

    monkeypatch.delenv(calls.DISABLE_ENV, raising=False)
    assert calls.enabled_override() is None


# ------------------------- per-app partitioning (CL-18, D151) ----------------

def test_records_land_in_their_apps_partition(store):
    """Two apps' records go to two directories, each named `<slug>-<hash>`, and
    the root holds nothing but partition dirs and the index."""
    write_records([rec(page="/apps/sine/page.html"),
                   rec(page="/apps/wave/page.html")])

    dirs = calls.partition_dirs()
    assert calls.partition_name("/apps/sine") in dirs
    assert calls.partition_name("/apps/wave") in dirs
    assert all(d.startswith(("sine-", "wave-")) for d in dirs)
    root_files = [n for n in os.listdir(store) if os.path.isfile(os.path.join(store, n))]
    assert root_files == ["index.json"]


def test_a_record_with_no_page_goes_to_the_unattributed_partition(store):
    write_records([rec(page="", call_id="orphan")])
    assert calls.partition_files(calls.UNATTRIBUTED), "orphan records still land"
    got = [r["call_id"] for r in calls.query(limit=5)["records"]]
    assert got == ["orphan"], "and the merged walk still reads them"


def test_partition_name_is_bounded_and_filesystem_safe(store):
    """A hostile or merely long folder name must not leak into the layout: the
    slug is sanitised and capped (Windows MAX_PATH is why the cap exists), and
    the hash carries the identity when the slug contributes nothing."""
    ugly = "/x/" + "Wei rd&Name!" * 8
    name = calls.partition_name(ugly)
    slug, _, digest = name.rpartition("-")
    assert len(slug) <= calls._SLUG_MAX
    assert re.fullmatch(r"[a-z0-9-]+", slug)
    assert len(digest) == 16
    assert calls.partition_name("/x/....") == calls.partition_name("/x/....").lower()
    # No slug at all: the hash alone is the name.
    assert re.fullmatch(r"[0-9a-f]{16}", calls.partition_name("/x/...."))


def test_two_spellings_of_one_folder_share_a_partition(store, tmp_path):
    """The D147 bug class one layer up: a symlinked spelling of an app's folder
    must not split its history into a second partition — a split here is
    structural (the gate and any per-partition tooling would miss half the
    records), not a mere filter miss."""
    real = tmp_path / "realapp"
    real.mkdir()
    alias = tmp_path / "alias"
    os.symlink(real, alias)

    assert calls.partition_name(str(alias)) == calls.partition_name(str(real))
    write_records([rec(page=str(real / "p.html"), call_id="via-real"),
                   rec(page=str(alias / "p.html"), call_id="via-alias")])
    files = calls.partition_files(calls.partition_name(str(real)))
    text = "".join(open(f, encoding="utf-8").read() for f in files)
    assert "via-real" in text and "via-alias" in text
    assert len(calls.partition_dirs()) == 1


def test_the_size_trim_takes_the_largest_partition_first(store, monkeypatch):
    """The flat store's trim was oldest-first across everything, so one chatty
    app evicted a quiet app's whole history. Now the chatty app pays its own
    bill: while the store is over cap, the largest partition loses its oldest
    file — and the quiet partition's history survives untouched."""
    chatty = calls.partition_name("/apps/chatty")
    quiet = calls.partition_name("/apps/quiet")
    day = calls.day_stamp(time.time() - 86_400)  # yesterday: trimmable
    for i in range(4):
        path = os.path.join(store, chatty, f"{day}-1-{i:03d}.calls.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("x" * 400 + "\n")
        os.utime(path, (time.time() - 3_600 - i,) * 2)
    quiet_path = os.path.join(store, quiet, f"{day}-1-001.calls.jsonl")
    os.makedirs(os.path.dirname(quiet_path), exist_ok=True)
    with open(quiet_path, "w") as fh:
        fh.write("y" * 100 + "\n")
    os.utime(quiet_path, (time.time() - 90_000,) * 2)  # OLDEST file in the store

    monkeypatch.setattr(calls, "DEFAULT_MAX_BYTES", 1_000)
    removed = calls.sweep()

    assert removed >= 1
    assert os.path.exists(quiet_path), \
        "the store's oldest file survives because its partition is not the problem"
    assert len(calls.partition_files(chatty)) < 4


def test_sweep_reaps_an_emptied_partition_and_its_index_entry(store, monkeypatch):
    """A partition whose files all aged out must not survive as an empty dir
    (months of browsing would strew hundreds of them), and the advisory index
    forgets it in the same pass."""
    write_records([rec(page="/apps/old/p.html"), rec(page="/apps/live/p.html")])
    old = calls.partition_name("/apps/old")
    for path in calls.partition_files(old):
        os.utime(path, (time.time() - 40 * 86_400,) * 2)
    monkeypatch.setenv(calls.RETENTION_DAYS_ENV, "14")

    calls.sweep()

    assert old not in calls.partition_dirs()
    assert old not in calls._index_read()
    assert calls.partition_name("/apps/live") in calls._index_read()


def test_the_index_is_advisory_a_corrupt_one_never_blocks_a_write(store):
    """index.json is a convenience map, not a load-bearing structure: garbage in
    it must cost the slug lookup and nothing else."""
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "index.json"), "w") as fh:
        fh.write("{ not json")

    write_records([rec(page="/apps/fine/p.html", call_id="ok-1")])

    assert [r["call_id"] for r in calls.query(limit=5)["records"]] == ["ok-1"]
    # And the write path healed the index rather than propagating the garbage.
    assert calls.partition_name("/apps/fine") in calls._index_read()


def test_the_index_maps_partitions_to_the_folders_they_name(store):
    write_records([rec(page="/apps/sine/p.html")])
    assert calls._index_read()[calls.partition_name("/apps/sine")] == "/apps/sine"


def test_is_log_file_covers_files_inside_a_partition(store):
    write_records([rec(page="/apps/sine/p.html")])
    stored = calls.store_files()[0]
    assert calls.is_log_file(stored) is True
    assert calls.is_log_file(os.path.join(os.path.dirname(stored), "renamed.txt")) is True
    assert calls.is_log_file(os.path.join(store, "index.json")) is True
    assert calls.is_log_file("/somewhere/else.txt") is False


def test_stray_root_files_are_ignored_by_the_walk(store):
    """A pre-partitioning dev store is not migrated (design §4.7): a root-level
    file is invisible to the reader rather than half-visible."""
    os.makedirs(store, exist_ok=True)
    with open(os.path.join(store, "2026-01-01-1-001.calls.jsonl"), "w") as fh:
        fh.write(json.dumps(calls._prune(rec(call_id="stray"))) + "\n")
    write_records([rec(page="/apps/sine/p.html", call_id="real")])

    assert [r["call_id"] for r in calls.query(limit=10)["records"]] == ["real"]
