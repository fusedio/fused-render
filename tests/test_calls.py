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
    first = calls.current_file()
    write_records([rec() for _ in range(4)])
    assert os.path.getsize(first) >= 400
    second = calls.current_file()
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
    live = calls.current_file()
    with open(live, "w") as fh:
        fh.write("x" * 3000)
    monkeypatch.setattr(calls, "DEFAULT_MAX_BYTES", 1000)
    monkeypatch.setenv(calls.RETENTION_DAYS_ENV, "14")
    assert calls.sweep() == 0
    assert os.path.exists(live), "the live file must survive an over-cap sweep"

    # An older file IS fair game, and the live one still survives.
    old = os.path.join(store, "2020-01-02-1-001.calls.jsonl")
    with open(old, "w") as fh:
        fh.write("y" * 3000)
    os.utime(old, (time.time() - 2 * 86400,) * 2)  # inside the age window
    assert calls.sweep() == 1
    assert os.path.exists(live) and not os.path.exists(old)


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
    old_path = os.path.join(store, "2020-01-01-1-001.calls.jsonl")
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
    path = calls.current_file()
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
    path = os.path.join(store, "2026-07-24-1-001.calls.jsonl")
    payloads = [json.dumps(rec(call_id=f"c-{i}", stdout_tail="x" * 97)) for i in range(200)]
    with open(path, "w") as fh:
        fh.write("\n".join(payloads) + "\n")
    got = [json.loads(line)["call_id"] for line in calls._iter_lines_reverse(path, chunk=128)]
    assert got == [f"c-{i}" for i in reversed(range(200))]


def test_a_file_without_a_trailing_newline_is_fully_read(store):
    os.makedirs(store, exist_ok=True)
    path = os.path.join(store, "2026-07-24-2-001.calls.jsonl")
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
    from fused_render import server as server_mod

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
    assert cfg["calls_dir"] == os.path.abspath(calls.store_dir())
    assert cfg["calls_suffix"] == calls.SUFFIX


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


def test_the_calls_view_stops_auto_reload_while_following(store):
    """Two live-update mechanisms must not fight: auto-reload rebuilds the frame
    on a file change, which would interrupt the poll. log_studio makes the same
    trade for its Tail button."""
    template = os.path.join(os.path.dirname(calls.__file__), "templates", "calls",
                            "template.html")
    src = open(template, encoding="utf-8").read()
    assert "fused.autoReload(!state.follow)" in src


# ------------------------------------------------ the CLI (Bugbot #283 review)

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
    path = calls.current_file()
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
    return os.path.join(calls.store_dir(),
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
    odd = os.path.join(store, "stray.calls.jsonl")
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
    line = open(calls.current_file()).read().strip()
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
    got = json.loads(open(calls.current_file()).read().strip())
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
    with open(calls.current_file(), "w") as fh:
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
    path = calls.current_file()
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
    with open(calls.current_file(), "w") as fh:
        fh.write(appended("mine-1", time.time() - 10, page="/app/mine.html") + "\n")
    got = calls.query(limit=5, cursor="mine-1", page="/app/mine.html")
    assert got["records"] == []
    assert got["cursor"] == "mine-1", "unchanged, not None"
    assert got["cursor_missing"] is False


def test_a_page_with_no_history_has_no_cursor(store):
    """No matching record means there is no id to resume from — None, so a
    follower's baseline moves the moment that page's first call lands."""
    os.makedirs(store, exist_ok=True)
    path = calls.current_file()
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
    with open(calls.current_file(), "w") as fh:
        fh.write(appended("other-1", now - 100, page="/app/other.html") + "\n")
        fh.write(appended("mine-1", now - 50, page="/app/mine.html") + "\n")
    got = calls.query(limit=5, cursor="other-1", page="/app/mine.html")
    assert got["cursor_missing"] is False, "found it, it just did not match"
    assert [r["call_id"] for r in got["records"]] == ["mine-1"]


def test_the_cli_omits_the_cursor_line_when_there_is_none(store, monkeypatch, capsys):
    """`cursor: None` invites passing it back verbatim."""
    os.makedirs(store, exist_ok=True)
    with open(calls.current_file(), "w") as fh:
        fh.write(appended("other-1", time.time() - 10, page="/app/other.html") + "\n")
    out = run_cli(monkeypatch, capsys, "--page", "/app/mine.html")
    assert "cursor:" not in out
    assert "None" not in out
