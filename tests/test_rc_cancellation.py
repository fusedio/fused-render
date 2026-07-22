"""Tests for the CANCELLABLE rc path (shell/mounts._rc_cancellable).

The mount-runaway P0: a plain urlopen socket timeout on operations/list /
operations/stat abandons only the CLIENT socket while rclone keeps running its
unbounded ListObjectsV2 server-side — repeated timed-out calls piled up orphaned
walks and pinned a CPU for 14h. The fix submits these commands with
`_async=true` and, on timeout, calls job/stop so rclone actually cancels the
in-flight enumeration. These tests assert that stop-on-timeout contract against
the async-aware StubRcd (real rclone is never invoked).
"""

import pytest

import fused_render.shell.mounts as mounts_mod

# Reuse the async-aware stub + fixtures from the main mounts test module.
from tests.test_shell_mounts import StubRcd, home, rcd  # noqa: F401


def test_list_timeout_stops_the_job_and_raises_rc_list_timeout(home, rcd):
    # A directory too large to enumerate: the async job never finishes. On the
    # client deadline we must job/stop the SAME jobid (so rclone stops walking)
    # and still raise RcListTimeout for the caller.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["operations/list"] = {"list": []}
    rcd.delay["operations/list"] = float("inf")  # job never becomes ready

    with pytest.raises(mounts_mod.RcListTimeout):
        mounts_mod.rc_list_dir(mounts_mod.mountpoint(c) + "/huge", timeout=0.2)

    # The submit went out with _async, a jobid came back, and we cancelled it.
    submit = [b for (m, b) in rcd.calls if m == "operations/list"]
    assert submit and submit[0].get("_async") is not True  # _async stripped on record
    stops = [b for (m, b) in rcd.calls if m == "job/stop"]
    assert len(stops) == 1
    jobid = stops[0]["jobid"]
    # The cancelled job is the one the stub handed out (jobid 1 here).
    assert jobid in rcd.jobs
    assert rcd.jobs[jobid]["stopped"] is True


def test_stat_timeout_stops_the_job_and_reports_indeterminate(home, rcd):
    # operations/stat on a flat prefix also runs the unbounded lister. On
    # timeout the stat path must cancel the job and fail open to indeterminate
    # (never fall back to a kernel os.stat).
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["operations/stat"] = {"item": {"Size": 1}}
    rcd.delay["operations/stat"] = float("inf")

    # timeout must clear _DIRECT_PROBE_MIN_S (else _stat_item bails to
    # indeterminate before ever issuing the rc call we want to cancel).
    assert mounts_mod.rc_stat_for(mounts_mod.mountpoint(c) + "/f", timeout=1.0) == "indeterminate"

    stops = [b for (m, b) in rcd.calls if m == "job/stop"]
    assert len(stops) == 1
    assert rcd.jobs[stops[0]["jobid"]]["stopped"] is True


def test_list_fast_success_returns_normally_without_stopping(home, rcd):
    # The hot path: a job that finishes at once returns the SAME shape the old
    # synchronous call did, and never touches job/stop.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["operations/list"] = {
        "list": [
            {"Name": "a", "IsDir": True, "Size": -1},
            {"Name": "b.txt", "IsDir": False, "Size": 7},
        ]
    }

    entries = mounts_mod.rc_list_dir(mounts_mod.mountpoint(c) + "/sub", timeout=5)
    assert [e["Name"] for e in entries] == ["a", "b.txt"]
    assert not any(m == "job/stop" for (m, _) in rcd.calls)


def test_failed_job_maps_to_rc_list_error_not_timeout(home, rcd):
    # A job that finishes with an error (the remote path is a file, not a dir)
    # must surface as RcListError — same as the synchronous HTTPError path —
    # and NOT as a timeout, so the caller answers 400 not 503.
    c = mounts_mod.add_mount("data", "remote:bucket")
    rcd.responses["operations/list"] = (500, {"error": "not a directory"})

    with pytest.raises(mounts_mod.RcListError) as exc:
        mounts_mod.rc_list_dir(mounts_mod.mountpoint(c) + "/file.parquet", timeout=5)
    assert not isinstance(exc.value, mounts_mod.RcListTimeout)
    assert not any(m == "job/stop" for (m, _) in rcd.calls)


def _one_shot_server(reply_for):
    """Spin a localhost HTTP server that answers each POST with reply_for(body),
    recording the request bodies it saw. Returns (port, seen, shutdown)."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = _json.loads(self.rfile.read(n) or b"{}")
            seen.append((self.path.lstrip("/"), body))
            code, payload = reply_for(self.path.lstrip("/"), body)
            raw = _json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port, seen, srv.shutdown


def test_rc_cancellable_reuses_sync_payload_when_no_jobid(home):
    # Peer IGNORED _async and ran the command synchronously, handing back the
    # full payload with no jobid. _rc_cancellable must RETURN that result — not
    # re-issue the same unbounded enumeration a second time (Bugbot: "fallback
    # re-lists after sync result").
    def reply(method, body):
        return 200, {"list": [{"Name": "x"}]}  # never a jobid

    port, seen, shutdown = _one_shot_server(reply)
    try:
        out = mounts_mod._rc_cancellable(port, "operations/list", {"fs": "r:"}, timeout=2)
    finally:
        shutdown()

    assert out == {"list": [{"Name": "x"}]}
    # Exactly ONE call — the submit result was reused, not thrown away + redone.
    assert len(seen) == 1
    assert seen[0][1].get("_async") is True


def test_rc_cancellable_falls_back_on_empty_ack(home):
    # A truly empty/absent ack (no jobid, nothing to reuse) still degrades to a
    # plain synchronous _rc so behavior doesn't break.
    replies = iter([(200, {}), (200, {"list": [{"Name": "y"}]})])

    def reply(method, body):
        return next(replies)

    port, seen, shutdown = _one_shot_server(reply)
    try:
        out = mounts_mod._rc_cancellable(port, "operations/list", {"fs": "r:"}, timeout=2)
    finally:
        shutdown()

    assert out == {"list": [{"Name": "y"}]}
    # Submit (with _async) returned empty -> a second, plain sync call was made.
    assert len(seen) == 2
    assert seen[0][1].get("_async") is True
    assert "_async" not in seen[1][1]


def test_rc_cancellable_stops_job_when_polling_fails(home):
    # If job/status itself raises (a busy/slow rcd near the deadline), the job
    # must STILL be cancelled server-side rather than left enumerating — the
    # exact runaway this path exists to prevent (Bugbot: "poll failure skips job
    # cancellation"). The raised error must read as a timeout to callers.
    def reply(method, body):
        if method == "job/status":
            return 500, {"error": "server busy"}  # _rc -> RuntimeError
        if method == "job/stop":
            return 200, {}
        return 200, {"jobid": 1}  # the async submit

    port, seen, shutdown = _one_shot_server(reply)
    try:
        with pytest.raises(RuntimeError) as exc:
            mounts_mod._rc_cancellable(port, "operations/list", {"fs": "r:"}, timeout=2)
    finally:
        shutdown()

    # job/stop was issued for the submitted job despite the poll failure...
    stops = [b for (m, b) in seen if m == "job/stop"]
    assert stops and stops[0]["jobid"] == 1
    # ...and the surfaced error reads as a timeout (so rc_list_dir -> RcListTimeout).
    assert mounts_mod._rc_timed_out(exc.value)
