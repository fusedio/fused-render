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
import json
import os
import queue
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import calls
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
    """
    batches = []
    real_append = calls._append  # bind BEFORE patching, or flaky recurses into itself

    def flaky(records):
        batches.append(records)
        if len(batches) == 1:
            raise OSError("disk full")
        real_append(records)

    monkeypatch.setattr(calls, "_append", flaky)
    calls.record(rec(call_id="first"))
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not batches:
        time.sleep(0.05)
    assert batches, "the first record was never handed to the writer"

    calls.record(rec(call_id="second"))
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and len(batches) < 2:
        time.sleep(0.05)
    assert len(batches) >= 2, "writer thread died on the first failure"
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
    old = os.path.join(store, "2020-01-01-1.calls.jsonl")
    fresh = os.path.join(store, "2026-07-24-1.calls.jsonl")
    for path in (old, fresh):
        with open(path, "w") as fh:
            fh.write(json.dumps(rec()) + "\n")
    os.utime(old, (time.time() - 40 * 86_400,) * 2)
    monkeypatch.setenv(calls.RETENTION_DAYS_ENV, "14")
    assert calls.sweep() == 1
    assert os.path.exists(fresh) and not os.path.exists(old)


def test_sweep_trims_oldest_first_when_over_the_size_cap(store, monkeypatch):
    os.makedirs(store, exist_ok=True)
    paths = [os.path.join(store, f"2026-07-2{i}-1.calls.jsonl") for i in range(1, 4)]
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


def test_reading_the_log_does_not_append_to_the_log(app_client):
    """Otherwise the view's own polling feeds itself: each poll's reader calls
    would show up in the next poll's results, inflating the counts forever."""
    client, d = app_client
    reader = os.path.join(os.path.dirname(calls.__file__), "templates", "calls", "reader.py")
    client.post("/api/run",
                json={"py": reader, "html": str(d / "p.html"), "params": {"op": "overview"}},
                headers=app_headers(d / "p.html"))
    time.sleep(0.5)
    assert calls.query(limit=50)["records"] == []


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
    client, d = app_client
    target = d / "out.txt"
    client.post("/api/fs/write", json={"path": str(target), "content": "abcd"},
                headers=app_headers(d / "p.html"))
    assert drain()
    got = calls.query(limit=10)["records"][0]
    assert got["route"] == "/api/fs/write"
    assert got["bytes_written"] == 4
    assert "abcd" not in json.dumps(got), "file content must never be stored"


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
    path = os.path.join(store, "2026-07-24-1.calls.jsonl")
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
    assert body["today"].endswith(".calls.jsonl")
    assert body["enabled"] is True
    assert body["retention_days"] == 14


def test_day_file_is_per_process(store):
    """Two live servers must not interleave lines into one file (the same
    reasoning logs.py applies to its per-pid log)."""
    assert f"-{os.getpid()}.calls.jsonl" in calls.day_file()


# ------------------------------------------------------- registry + gate wiring

def test_calls_is_a_conditional_peer_on_html_and_py():
    """It joins the switcher via condition.py (CT-12) and is never the default:
    a page nobody has run must not grow a dead mode."""
    from fused_render import server

    for path, default in (("/x/sine.html", "_render"), ("/x/data.py", "code")):
        entries, error = server._templates_for(path, False)
        assert error is None
        modes = [e["mode"] for e in entries]
        assert modes[0] == default
        assert "calls" in modes
        entry = next(e for e in entries if e["mode"] == "calls")
        assert entry["conditional"] is True
        assert entry["path"].endswith("calls/template.html")
        assert entry["icon"] is not None


def test_the_store_itself_opens_in_the_calls_view():
    """`.calls.jsonl` (2 segments) beats bare `.jsonl` (1) — CT-3 specificity."""
    from fused_render import server

    entries, error = server._templates_for("/x/2026-07-24-99.calls.jsonl", False)
    assert error is None
    assert [e["mode"] for e in entries] == ["calls", "log_studio", "code"]


def test_gate_is_false_for_a_page_with_no_records(store, tmp_path):
    from fused_render.templates.calls import condition

    assert condition.main(str(tmp_path / "never-run.html")) is False


def test_gate_turns_true_once_the_page_has_records(store, tmp_path, monkeypatch):
    from fused_render.templates.calls import condition

    page = str(tmp_path / "sine.html")
    write_records([rec(page=page)])
    # The gate resolves the store from the env, exactly as a standalone copy in
    # the user template dir would.
    assert condition.main(page) is True


def test_gate_is_true_for_the_store_file_itself_without_touching_disk(tmp_path):
    from fused_render.templates.calls import condition

    assert condition.main(str(tmp_path / "2026-07-24-1.calls.jsonl")) is True


def test_reader_ops_are_reachable_and_bounded(store):
    from fused_render.templates.calls import reader

    write_records([rec(page="/a/p.html", outcome="ok"),
                   rec(page="/a/p.html", outcome="error")])
    assert reader.main(op="overview", page="/a/p.html")["total"] == 2
    assert len(reader.main(op="page", page="/a/p.html", limit=1)["records"]) == 1
    assert reader.main(op="series", bucket_ms=60_000)["bucket_ms"] == 60_000
    assert reader.main(op="targets")["targets"][0]["count"] == 2
    assert reader.main(op="config")["enabled"] is True
    assert "unknown op" in reader.main(op="nonsense")["error"]


def test_reader_since_accepts_a_relative_age(store):
    from fused_render.templates.calls import reader

    write_records([rec(occurred_at="2020-01-01T00:00:00.000Z"), rec()])
    # A small `since` is an age in seconds, so the 2020 record falls outside it.
    assert reader.main(op="overview", since=3600)["total"] == 1
    assert reader.main(op="overview", since=0)["total"] == 2


def test_calls_reader_is_allowlisted_for_in_process_execution():
    """The view polls while following; ~700 ms of subprocess spawn per poll is
    the difference between a live tail and a slideshow (D72).

    Resolved against the STAGED core-templates dir, not the package dir: that is
    where the executor actually reads built-in helpers from (core_templates.py).
    """
    from fused_render import executor
    from fused_render.core_templates import core_templates_dir

    reader = os.path.realpath(os.path.join(core_templates_dir(), "calls", "reader.py"))
    assert reader in executor.INPROCESS_HELPERS
