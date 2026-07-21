"""Tests for the background whole-file prefetcher (shell/prefetch.py) and
its trigger in the /api/fs/raw proxy. A stub HTTP server stands in for the
rclone serve; no rclone and no real mount is involved."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import fused_render.shell.prefetch as prefetch_mod


class StubServe:
    """Range-capable HTTP stand-in for an rclone serve. Serves `data` at
    every path, records requests, and can fail the first N GETs with a 500
    (the transient store errors the real serve surfaces mid-stream) or, with
    `partial_first_gets`, send a truncated body under a full Content-Length so
    the client hits an IncompleteRead *mid-chunk* (partial bytes delivered,
    then the connection drops)."""

    def __init__(self, data: bytes, fail_first_gets: int = 0,
                 partial_first_gets: int = 0, short_clean_first_gets: int = 0):
        self.data = data
        self.requests = []          # (method, range_header)
        self.fail_remaining = fail_first_gets
        self.partial_remaining = partial_first_gets
        self.short_clean_remaining = short_clean_first_gets
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_HEAD(self):
                stub.requests.append(("HEAD", None))
                self.send_response(200)
                self.send_header("Content-Length", str(len(stub.data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def _body(self, rng):
                if rng:
                    lo, hi = rng.removeprefix("bytes=").split("-")
                    return stub.data[int(lo):int(hi) + 1], 206
                return stub.data, 200

            def do_GET(self):
                rng = self.headers.get("Range")
                stub.requests.append(("GET", rng))
                if stub.fail_remaining > 0:
                    stub.fail_remaining -= 1
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body, status = self._body(rng)
                if stub.partial_remaining > 0:
                    stub.partial_remaining -= 1
                    # Deliver half the body as one complete chunked frame, then
                    # close WITHOUT the terminating 0-length chunk. The client
                    # cleanly receives (and returns to the reader) the partial
                    # bytes, then raises IncompleteRead on the missing
                    # terminator — a genuine mid-chunk failure with partial
                    # bytes already counted, which is what the retry accounting
                    # must survive. (A clean short EOF or an RST would either be
                    # tolerated as a short read or discard the buffered bytes,
                    # neither of which exercises the double-count path.)
                    half = body[: max(1, len(body) // 2)]
                    self.wfile.write(b"HTTP/1.1 %d x\r\n" % status)
                    self.wfile.write(b"Transfer-Encoding: chunked\r\n\r\n")
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(half), half))
                    self.wfile.flush()
                    self.close_connection = True
                    return
                if stub.short_clean_remaining > 0:
                    stub.short_clean_remaining -= 1
                    # Deliver half the body under HTTP/1.0 with NO Content-Length
                    # and NO chunked framing, then close. The body is delimited
                    # by connection close, so the client returns the partial
                    # bytes and sees a *clean* EOF — no IncompleteRead, no
                    # exception. This is the silent short read: the fetch returns
                    # "successfully" with a truncated body, so the missing bytes
                    # must be caught by an explicit full-range check.
                    half = body[: max(1, len(body) // 2)]
                    self.wfile.write(b"HTTP/1.0 %d x\r\n\r\n" % status)
                    self.wfile.write(half)
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/f.bin"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def fast(monkeypatch):
    """Reset module state and collapse the politeness delays so tests run
    in milliseconds; shrink the chunk so multi-chunk paths are exercised."""
    monkeypatch.setattr(prefetch_mod, "_jobs", {})
    monkeypatch.setattr(prefetch_mod, "_touched", {})
    monkeypatch.setattr(prefetch_mod, "START_DELAY_S", 0.0)
    monkeypatch.setattr(prefetch_mod, "IDLE_WAIT_S", 0.0)
    monkeypatch.setattr(prefetch_mod, "MAX_IDLE_HOLD_S", 0.0)
    monkeypatch.setattr(prefetch_mod, "CHUNK_BYTES", 1024)
    # Tests use tiny payloads; drop the size floor so they aren't skipped.
    monkeypatch.setattr(prefetch_mod, "MIN_BYTES", 0)
    monkeypatch.setattr(prefetch_mod, "ENABLED", True)


def wait_status(path, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = prefetch_mod.status().get(path, {}).get("status")
        if st == want:
            return prefetch_mod.status()[path]
        time.sleep(0.01)
    raise AssertionError(
        f"prefetch of {path} never reached {want!r}: {prefetch_mod.status()}")


def test_streams_whole_file_in_sequential_chunks(fast):
    stub = StubServe(os.urandom(3000))          # 3 chunks at CHUNK_BYTES=1024
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        job = wait_status("/m/f.bin", "done")
        assert job["done"] == job["size"] == 3000
        gets = [r for r in stub.requests if r[0] == "GET"]
        assert [r[1] for r in gets] == [
            "bytes=0-1023", "bytes=1024-2047", "bytes=2048-2999"]
    finally:
        stub.close()


def test_prioritizes_head_then_footer_before_middle(fast, monkeypatch):
    # A file larger than the head region: the head (row group 0) streams first,
    # then the footer tail, and only then the middle bulk — so an interactive
    # first-page read racing the stream finds footer + row group 0 warm early.
    monkeypatch.setattr(prefetch_mod, "HEAD_BYTES", 1024)
    monkeypatch.setattr(prefetch_mod, "FOOTER_BYTES", 1024)
    stub = StubServe(os.urandom(5000))          # CHUNK_BYTES=1024 -> 5 chunks
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        job = wait_status("/m/f.bin", "done")
        assert job["done"] == job["size"] == 5000
        gets = [r[1] for r in stub.requests if r[0] == "GET"]
        # head first, footer tail second, middle after.
        assert gets == ["bytes=0-1023", "bytes=3976-4999",
                        "bytes=1024-2047", "bytes=2048-3071", "bytes=3072-3975"]
        # every byte covered exactly once (no gap, no overlap).
        covered = []
        for g in gets:
            lo, hi = g.removeprefix("bytes=").split("-")
            covered.extend(range(int(lo), int(hi) + 1))
        assert sorted(covered) == list(range(5000))
    finally:
        stub.close()


def test_size_gate_skips_large_files(fast, monkeypatch):
    monkeypatch.setattr(prefetch_mod, "MAX_BYTES", 100)
    stub = StubServe(b"x" * 3000)
    try:
        prefetch_mod.schedule("/m/big.bin", stub.url)
        wait_status("/m/big.bin", "skipped")
        assert not any(r[0] == "GET" for r in stub.requests)
    finally:
        stub.close()


def test_size_gate_skips_small_files(fast, monkeypatch):
    # A zarr chunk / tiny metadata object: below the floor, never streamed.
    monkeypatch.setattr(prefetch_mod, "MIN_BYTES", 1000)
    stub = StubServe(b"x" * 100)
    try:
        prefetch_mod.schedule("/m/chunk.bin", stub.url)
        wait_status("/m/chunk.bin", "skipped")
        assert not any(r[0] == "GET" for r in stub.requests)
    finally:
        stub.close()


def test_size_gate_decides_without_start_delay(fast, monkeypatch):
    # The gate HEAD runs before START_DELAY (delay applies to the download
    # phase only), so a sub-floor file is dismissed immediately even with a
    # long delay configured — no 5s wait to decide not to prefetch.
    monkeypatch.setattr(prefetch_mod, "START_DELAY_S", 30.0)
    monkeypatch.setattr(prefetch_mod, "MIN_BYTES", 1000)
    stub = StubServe(b"x" * 100)
    try:
        prefetch_mod.schedule("/m/chunk.bin", stub.url)
        wait_status("/m/chunk.bin", "skipped", timeout=5.0)   # << 30s delay
        assert not any(r[0] == "GET" for r in stub.requests)
    finally:
        stub.close()


def test_evicts_oldest_terminal_jobs_over_cap(fast, monkeypatch):
    # Thousands of chunks would otherwise mint a permanent entry each; the
    # map stays bounded by evicting oldest terminal jobs.
    monkeypatch.setattr(prefetch_mod, "MAX_TRACKED", 2)
    stub = StubServe(b"a" * 50)
    try:
        for i in range(5):
            prefetch_mod.schedule(f"/m/f{i}.bin", stub.url)
            wait_status(f"/m/f{i}.bin", "done")
        assert len(prefetch_mod.status()) <= 2
    finally:
        stub.close()


def test_eviction_is_lru_by_access_not_completion(fast, monkeypatch):
    # A done file still being read must survive; the least-recently-read
    # one is dropped first. Otherwise is_done routing flaps and an in-use
    # file gets re-prefetched.
    monkeypatch.setattr(prefetch_mod, "MAX_TRACKED", 2)
    stub = StubServe(b"a" * 50)
    try:
        for p in ("/m/old.bin", "/m/mid.bin"):
            prefetch_mod.schedule(p, stub.url)
            wait_status(p, "done")
        # Re-read old.bin: touches it even though it completed first.
        prefetch_mod.schedule("/m/old.bin", stub.url)   # done -> touch only
        # New file trips the cap; least-recently-read (mid.bin) is evicted.
        prefetch_mod.schedule("/m/new.bin", stub.url)
        wait_status("/m/new.bin", "done")
        st = prefetch_mod.status()
        assert "/m/old.bin" in st       # recently re-read -> survives
        assert "/m/mid.bin" not in st   # least-recently-read -> evicted
    finally:
        stub.close()


def test_schedule_is_idempotent(fast):
    stub = StubServe(b"y" * 500)
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        wait_status("/m/f.bin", "done")
        n = len(stub.requests)
        prefetch_mod.schedule("/m/f.bin", stub.url)   # already done -> no-op
        time.sleep(0.1)
        assert len(stub.requests) == n
    finally:
        stub.close()


def test_resumes_after_transient_errors(fast):
    stub = StubServe(os.urandom(2500), fail_first_gets=2)
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        job = wait_status("/m/f.bin", "done", timeout=15.0)
        assert job["done"] == 2500
    finally:
        stub.close()


def test_progress_stays_exact_across_midchunk_retry(fast, monkeypatch):
    # A mid-chunk failure that delivers real bytes before raising (partial
    # bytes counted, then IncompleteRead) must not double-count on retry: the
    # retry re-requests the whole chunk from its start, so progress resets to
    # the committed base rather than adding the failed attempt's partial bytes
    # again. done must land exactly on size, never past it. MB-scale so the
    # partial delivery clears _fetch_chunk's 1MB read block and is genuinely
    # counted before the stream drops (the buggy `+= len(b)` overshoots here).
    mb = 1024 * 1024
    monkeypatch.setattr(prefetch_mod, "CHUNK_BYTES", 8 * mb)   # one chunk
    monkeypatch.setattr(prefetch_mod, "HEAD_BYTES", 8 * mb)
    stub = StubServe(os.urandom(3 * mb), partial_first_gets=1)  # ~2MB then drops
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        job = wait_status("/m/f.bin", "done", timeout=15.0)
        assert job["done"] == job["size"] == 3 * mb    # exact, never > size
    finally:
        stub.close()


def test_short_clean_read_is_retried_not_committed(fast, monkeypatch):
    # A truncated body under a connection-close-delimited response yields a
    # *clean* EOF: _fetch_chunk's read loop ends without an exception even
    # though only half the range arrived. Treating that as a full chunk would
    # commit bytes that never landed and finish the job as "done" with data
    # missing. _fetch_chunk must verify it consumed the whole [start, end]
    # range and raise otherwise, so the retry loop re-requests the chunk.
    mb = 1024 * 1024
    monkeypatch.setattr(prefetch_mod, "CHUNK_BYTES", 8 * mb)   # one chunk
    monkeypatch.setattr(prefetch_mod, "HEAD_BYTES", 8 * mb)
    stub = StubServe(os.urandom(3 * mb), short_clean_first_gets=1)
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        job = wait_status("/m/f.bin", "done", timeout=15.0)
        assert job["done"] == job["size"] == 3 * mb
        # The short read forced a retry: the chunk was requested twice.
        gets = [r for r in stub.requests if r[0] == "GET"]
        assert len(gets) >= 2
    finally:
        stub.close()


def test_gives_up_after_sustained_errors_then_retries(fast, monkeypatch):
    monkeypatch.setattr(prefetch_mod, "MAX_CONSECUTIVE_ERRORS", 2)
    stub = StubServe(b"z" * 100, fail_first_gets=10 ** 6)
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        wait_status("/m/f.bin", "failed", timeout=30.0)
        # within the cooldown a re-schedule must NOT restart the download
        n = len(stub.requests)
        prefetch_mod.schedule("/m/f.bin", stub.url)
        time.sleep(0.1)
        assert len(stub.requests) == n
        # after the cooldown it retries (and now succeeds)
        stub.fail_remaining = 0
        monkeypatch.setattr(prefetch_mod, "FAILED_RETRY_COOLDOWN_S", 0.0)
        prefetch_mod.schedule("/m/f.bin", stub.url)
        wait_status("/m/f.bin", "done", timeout=15.0)
    finally:
        stub.close()


def test_disabled_by_env(fast, monkeypatch):
    monkeypatch.setattr(prefetch_mod, "ENABLED", False)
    stub = StubServe(b"q" * 100)
    try:
        prefetch_mod.schedule("/m/f.bin", stub.url)
        time.sleep(0.1)
        assert prefetch_mod.status() == {} and stub.requests == []
    finally:
        stub.close()


# -- trigger: /api/fs/raw schedules a prefetch for mount-backed files --------


def test_fs_raw_schedules_prefetch_for_mount_backed_file(
        fast, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from fused_render.server import create_app
    import fused_render.shell.mounts as mounts_mod

    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    mp = home / "mounts" / "data"
    mp.mkdir(parents=True)
    f = mp / "table.parquet"
    f.write_bytes(b"PAR1 not really parquet")

    stub = StubServe(f.read_bytes())
    (home).mkdir(exist_ok=True)
    (home / "serves.json").write_text(json.dumps(
        {str(mp): stub.url.rsplit("/", 1)[0]}))
    # This test seeds serves.json directly and never creates a mounts.json
    # record for `mp` (no rclone/mount involved at all, per the module
    # docstring) — real automount would rightly treat that as a stale entry
    # with no backing mount and wipe it (D123's stale-serve cleanup).
    # Disable the background automount thread create_app spawns so it can't
    # race with/clobber this test's manual serves.json setup.
    monkeypatch.setattr(mounts_mod, "startup", lambda: None)

    # The warm-read fallthrough resolves a mount-backed path's shape through the
    # rcd (_mount_probe), never a kernel stat — stub the parent listing so the
    # file reads as a present regular object and the proxy is reached.
    monkeypatch.setattr(mounts_mod, "rc_list_dir",
                        lambda p, timeout=None: [{"Name": "table.parquet",
                                                  "IsDir": False, "Size": 23,
                                                  "ModTime": "2024-01-02T03:04:05Z"}])

    scheduled = []
    monkeypatch.setattr(prefetch_mod, "schedule",
                        lambda path, url: scheduled.append((path, url)))
    try:
        client = TestClient(create_app(str(tmp_path)))
        r = client.get("/api/fs/raw", params={"path": str(f)})
        assert r.status_code == 200
        assert scheduled == [(str(f), mounts_mod.serve_url_for(str(f)))]

        # a local (non-mount) file must not schedule anything
        local = tmp_path / "plain.txt"
        local.write_text("hi")
        client.get("/api/fs/raw", params={"path": str(local)})
        assert len(scheduled) == 1
    finally:
        stub.close()
