"""The segmented, resumable Hub fetch (SPEC AI-5i), driven against a real server.

`worker_base` fetches weights itself now instead of handing the whole job to
`snapshot_download`: several connections at once, split across files AND inside
one big file with `Range`, with per-segment offsets on disk so an interrupted
download continues instead of restarting. Every one of those properties is a
property of an HTTP conversation, so every test here drives a real
`http.server` on an ephemeral port rather than a stubbed reader — a fake that
returns bytes cannot lie about `Content-Range`, close mid-body, or ignore a
`Range` header, and those three are exactly what the code is defending against.

What is pinned, and why each one is a bug that has to stay fixed:

  - the offsets in the sidecar are bytes the kernel already has, so a `SIGKILL`
    mid-download resumes rather than re-fetching gigabytes;
  - a server that ignores `Range` still produces a correct file, because
    "ignores ranges" is a property of some CDNs and not an error;
  - a sidecar that does not match the file it sits beside is thrown away, not
    trusted — trusting it writes a silently corrupt blob;
  - a failure anywhere falls back to today's `snapshot_download`, because a
    download that got faster and sometimes broken is worse than a slow one.

`huggingface_hub` is not installed here (nor on CI), which is the point of
`worker_base` being stdlib-only. The Hub is therefore reached through exactly
two seams — `_hub_file_meta` and `repo_folder` — and both are monkeypatched.
"""
import hashlib
import http.server
import importlib.util
import json
import os
import socketserver
import threading
import types

import pytest

BASE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "ai", "runners", "worker_base.py",
)


def _fresh_base():
    """A fresh import of worker_base, by path — see tests/test_ai_worker_base.py."""
    spec = importlib.util.spec_from_file_location("worker_base_under_test", BASE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def base():
    return _fresh_base()


# -- a server that can misbehave in the specific ways a CDN misbehaves -----------


class _Threaded(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _start_server(payload, **flags):
    """Serve `payload` over HTTP; return (url, state).

    `state["log"]` records the `Range` header of every request, which is what
    the resume test asserts on: "resumed" means the second run ASKED for only
    the missing bytes, not merely that it ended up with the right file. Every
    flag lives in `state` too, so a test can change the server's behaviour
    between two runs against one URL.

    The flags are the ways a real CDN misbehaves, each of which is a test:

      ranges=False        no range support at all
      lie_after_probe     206 to the probe, then a full body ignoring `Range` —
                          one body's bytes offered to four different offsets
      clamp               206 to everything, but always `Content-Range` from
                          byte 0: the same scattering wearing a legal status
      budget=N            serve N bytes in total and then hang up mid-body,
                          which is how a download is interrupted with no signal
      unauthorized=N      401 on the first N real fetches — a presigned URL
                          that expired, which the client answers by re-resolving
      unauthorized_on={n} 401 on those real fetches by number, so a download can
                          be made to outlive TWO presigned URLs
      break_first=N       the first N real responses are a truncated chunked
                          body, which raises `http.client.IncompleteRead`
      break_bytes=N       …after delivering N bytes that the client keeps, so
                          the exception arrives on top of real progress
      chunk_cap=N         at most N bytes per response, then hang up — a slow
                          link that keeps needing another connection
      probe_fail_first=N  the first N one-byte probes get a 503

    `state["requests"]` records the path, `Range` and `Authorization` of every
    request, which is how the CDN-credential test can assert on a header that
    must NOT be there.
    """
    state = {"log": [], "requests": [], "served": 0, "broken": 0, "real": 0,
             "probes": 0, "lock": threading.Lock(),
             "ranges": True, "lie_after_probe": False, "clamp": False,
             "budget": None, "unauthorized": 0, "unauthorized_on": (),
             "break_first": 0, "break_bytes": 0, "chunk_cap": None,
             "probe_fail_first": 0}
    state.update(flags)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get("Range")
            probe = header == "bytes=0-0"
            with state["lock"]:
                state["log"].append(header)
                state["requests"].append({
                    "path": self.path, "range": header,
                    "auth": self.headers.get("Authorization")})
                if probe:
                    state["probes"] += 1
                    failed_probe = state["probes"] <= state["probe_fail_first"]
                else:
                    state["real"] += 1
                    expired = (state["unauthorized"] > 0
                               or state["real"] in state["unauthorized_on"])
                    if state["unauthorized"] > 0:
                        state["unauthorized"] -= 1

            if probe and failed_probe:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not probe and expired:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not probe and state["break_first"]:
                with state["lock"]:
                    broken = state["broken"] < state["break_first"]
                    state["broken"] += 1 if broken else 0
                if broken:
                    # A well-formed HEAD and a body that falls apart: the
                    # response has to be one the writer would accept, or this
                    # tests the header checks instead of the retry loop.
                    self.send_response(206 if header else 200)
                    at = 0
                    if header:
                        spec = header.split("=", 1)[1]
                        first, _, last = spec.partition("-")
                        at = int(first)
                        self.send_header(
                            "Content-Range",
                            f"bytes {at}-{last or len(payload) - 1}"
                            f"/{len(payload)}")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    keep = payload[at:at + state["break_bytes"]]
                    if keep:
                        # One valid chunk the client keeps, and only THEN the
                        # garbage — bytes on disk under an exception, which is
                        # a different thing from a response that never worked.
                        self.wfile.write(f"{len(keep):X}\r\n".encode()
                                         + keep + b"\r\n")
                    self.wfile.write(b"not-a-chunk-length\r\n")
                    self.close_connection = True
                    return

            start, end, partial = 0, len(payload) - 1, False
            if header and state["ranges"] and (probe or not state["lie_after_probe"]):
                spec = header.split("=", 1)[1]
                first, _, last = spec.partition("-")
                start = int(first)
                end = int(last) if last else len(payload) - 1
                partial = True
            if partial and not probe and state["clamp"]:
                start = 0  # the range is answered, but not the one that was asked
            body = payload[start:end + 1]

            allowed = len(body)
            if state["chunk_cap"] is not None and not probe:
                allowed = min(allowed, state["chunk_cap"])
            if state["budget"] is not None and not probe:
                with state["lock"]:
                    allowed = max(0, min(allowed, state["budget"] - state["served"]))
                    state["served"] += allowed

            self.send_response(206 if partial else 200)
            self.send_header("Content-Length", str(len(body)))
            if partial:
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            self.wfile.write(body[:allowed])
            if allowed < len(body):
                # Short of what Content-Length promised, then gone: a stream
                # ending early raises nothing on the client, which is exactly
                # the interruption the retry loop has to notice by itself.
                self.close_connection = True

    server = _Threaded(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["server"] = server
    return f"http://127.0.0.1:{server.server_address[1]}/weights", state


@pytest.fixture()
def payload():
    """Deterministic bytes, big enough to split several ways."""
    return hashlib.sha256(b"weights").digest() * 6250  # 200_000 bytes


def _wire(base, monkeypatch, tmp_path, url, size, etag="e7ag", commit="c0m",
          segment_min=20_000):
    """Point worker_base at the local server and a throwaway cache folder."""
    folder = str(tmp_path / "models--org--m")
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": folder)
    monkeypatch.setattr(base, "SEGMENT_MIN_BYTES", segment_min)
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: {
        "url": url, "location": url, "etag": etag, "commit": commit, "size": size})
    return folder


def _planned(base, folder, url, size, etag="e7ag", commit="c0m"):
    """One `_FileFetch` past `plan()`, for the rules that are about publishing
    rather than about the wire."""
    fetch = base._FileFetch(
        folder, "org/m", "model.safetensors", "main",
        {"url": url, "location": url, "etag": etag, "commit": commit, "size": size},
        None, threading.Event())
    fetch.plan()
    return fetch


def _ranges(log):
    """Every request that asked for bytes, minus the one-byte probe."""
    return [h for h in log if h and h != "bytes=0-0"]


def _offsets(log):
    return sorted(int(h.split("=", 1)[1].split("-")[0]) for h in _ranges(log))


# -- speed: several connections per file ----------------------------------------


def test_a_large_file_is_fetched_on_several_connections(base, monkeypatch,
                                                        tmp_path, payload):
    """The reason the feature exists: a 4.6GB shard used to move on exactly one
    connection, so the whole download ran at one connection's speed."""
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert len(_ranges(state["log"])) > 1, "the file moved on one connection"
    # Contiguous and disjoint: every byte asked for exactly once.
    assert _offsets(state["log"]) == [0, 50_000, 100_000, 150_000]
    assert os.path.getsize(os.path.join(folder, "blobs", "e7ag")) == len(payload)


def test_a_small_file_costs_no_extra_request(base, monkeypatch, tmp_path, payload):
    """A cache full of small config files must not pay a probe each. Under the
    segment floor there is nothing to split, so there is nothing to ask."""
    url, state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload), segment_min=10_000_000)

    base._segmented_fetch("org/m", ["config.json"], "c0m")

    assert state["log"] == [None], state["log"]


# -- a server that will not play along ------------------------------------------


def test_a_server_that_ignores_ranges_still_produces_the_file(base, monkeypatch,
                                                              tmp_path, payload):
    """No range support is a property of some hosts, not an error. It costs the
    speed-up and nothing else."""
    url, state = _start_server(payload, ranges=False)
    _wire(base, monkeypatch, tmp_path, url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert _ranges(state["log"]) == [], "it kept range-fetching a server that said no"


def test_a_server_that_ignores_range_mid_fetch_cannot_overrun(base, monkeypatch,
                                                              tmp_path, payload):
    """A 206 to the probe and a full body afterwards is the worst case: four
    segments each writing byte 0 at their own offset would produce a file that
    is the right length and entirely wrong. The writer refuses a 200 it did not
    ask for at a non-zero offset, and the blob is never published."""
    url, _state = _start_server(payload, lie_after_probe=True)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert not os.path.exists(os.path.join(folder, "blobs", "e7ag")), (
        "a scattered body was published as a finished blob")
    for name in os.listdir(os.path.join(folder, "blobs")):
        assert os.path.getsize(os.path.join(folder, "blobs", name)) <= len(payload)


def test_a_206_that_answers_a_different_range_is_refused(base, monkeypatch,
                                                         tmp_path, payload):
    """A 206 is not a promise that it is the range we ASKED for.

    A proxy that clamps ranges answers `bytes=150000-` with `Content-Range:
    bytes 0-…` — the same scattering as a bare 200, wearing a legal status
    code, and the status check alone would wave it straight through into four
    segment offsets.
    """
    url, _state = _start_server(payload, clamp=True)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert not os.path.exists(os.path.join(folder, "blobs", "e7ag"))


def test_publishing_requires_every_segment_to_have_landed(base, monkeypatch,
                                                          tmp_path, payload):
    """The part file's LENGTH proves nothing, so it cannot be the check.

    It is `ftruncate`d to the final size before a byte arrives — a sparse file
    of pure holes measures exactly right. The only evidence a file is complete
    is the per-segment cursors, the same numbers the sidecar records. Publishing
    on the length would put a zero-filled blob under a real etag into the hub
    cache, which hf then serves from cache forever.
    """
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    fetch = _planned(base, folder, url, len(payload))
    assert os.path.getsize(fetch.part) == len(payload), "the part file is pre-sized"

    with pytest.raises(RuntimeError, match="landed"):
        fetch.finish()

    assert not os.path.exists(os.path.join(folder, "blobs", "e7ag"))


# -- credentials go to the Hub, never to the CDN --------------------------------


def test_the_hub_token_is_not_sent_to_the_presigned_cdn_url(base, monkeypatch,
                                                            tmp_path, payload):
    """S3 rejects a request carrying two authentication mechanisms with a 400.

    huggingface_hub drops the `Authorization` header the moment the download
    URL differs from the Hub URL, and it is not being fussy: a presigned URL
    already carries its credentials in the query string. Sent anyway, the probe
    fails, every segment burns its whole retry budget on 400s, and the download
    falls back — SLOWER than before this feature existed and silently so,
    because the fallback is invisible by design. For a user with a token set,
    which is everyone pulling a gated model, that is every download.

    The local server cannot reproduce the 400, so what is pinned is the header
    itself: nothing but the Hub URL may ever carry one.
    """
    hub_url, state = _start_server(payload)
    # Same server, deliberately a different HOST — that difference is the whole
    # rule, and a test that redirected within one host would not see it.
    cdn_url = hub_url.replace("127.0.0.1", "localhost").replace("/weights", "/cdn")
    _wire(base, monkeypatch, tmp_path, hub_url, len(payload))
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: {
        "url": hub_url, "location": cdn_url, "etag": "e7ag", "commit": "c0m",
        "size": len(payload)})
    monkeypatch.setattr(base, "_hf_token", lambda: "hf_secret")

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert state["requests"], "nothing was fetched"
    for request in state["requests"]:
        assert request["auth"] is None, request


def test_the_hub_token_is_still_sent_when_the_file_is_served_by_the_hub(
        base, monkeypatch, tmp_path, payload):
    """The other half of the same rule. A gated repo answers the metadata call
    for an anonymous caller and then 401s on the blob, so dropping the header
    unconditionally would break exactly the downloads it was added for."""
    url, state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "_hf_token", lambda: "hf_secret")

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert {r["auth"] for r in state["requests"]} == {"Bearer hf_secret"}


# -- the presigned URL expires mid-download -------------------------------------


def _changing_meta(url, sizes, etags, commits):
    """A `_hub_file_meta` whose answer changes on the second call."""
    calls = {"n": 0}

    def meta(repo, name, revision):
        i = min(calls["n"], 1)
        calls["n"] += 1
        return {"url": url, "location": url, "etag": etags[i],
                "commit": commits[i], "size": sizes[i]}

    return meta, calls


def test_a_repo_that_changes_under_a_re_resolve_aborts(base, monkeypatch,
                                                       tmp_path, payload):
    """Re-resolving an expired CDN URL may only replace the LOCATION.

    The blob path, every segment offset and the snapshot folder were all derived
    from the first answer, so a repo updated mid-download would have the new
    revision's bytes written at the old revision's offsets and published as
    `blobs/<old-etag>` — a mix of two revisions at exactly the right length,
    which nothing downstream would ever notice.
    """
    # One 401 per segment: after the re-resolve every fetch succeeds, so an
    # unchecked re-resolve really would go on to publish the mixture.
    url, _state = _start_server(payload, unauthorized=4)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    meta, _calls = _changing_meta(url, [len(payload)] * 2, ["e7ag", "newetag"],
                                  ["c0m", "c0m"])
    monkeypatch.setattr(base, "_hub_file_meta", meta)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    blobs = os.listdir(os.path.join(folder, "blobs"))
    assert [name for name in blobs if not name.startswith("e7ag")] == [], blobs
    assert "e7ag" not in blobs, "a mix of two revisions was published"


def test_a_re_resolve_that_fails_is_a_retry_not_the_end_of_the_download(
        base, monkeypatch, tmp_path, payload):
    """The re-resolve is a network call like any other. Letting its failure
    escape `run()` turns one unlucky moment into an aborted multi-file
    download — the retry budget exists precisely for this."""
    url, _state = _start_server(payload, unauthorized=99)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    calls = {"n": 0}

    def meta(repo, name, revision):
        calls["n"] += 1
        if calls["n"] > 1:
            raise ValueError("the Hub is down")
        return {"url": url, "location": url, "etag": "e7ag", "commit": "c0m",
                "size": len(payload)}

    monkeypatch.setattr(base, "_hub_file_meta", meta)
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(RuntimeError, match="gave up"):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")


def test_a_second_expiry_in_one_download_is_re_resolved_too(base, monkeypatch,
                                                            tmp_path, payload):
    """A presigned URL is good for minutes; a multi-gigabyte download is not.

    Allowing one re-resolve per SEGMENT rather than one per stall means the
    second expiry spends the retry budget on 401s and aborts into the fallback,
    which then deletes hours of resumable state. Bytes arriving is what says the
    new URL worked, so bytes arriving is what restores the allowance — the same
    rule the retry budget itself follows.
    """
    url, state = _start_server(payload, unauthorized_on={1, 3},
                               chunk_cap=len(payload) // 2)
    _wire(base, monkeypatch, tmp_path, url, len(payload), segment_min=10_000_000)
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert state["real"] == 4, state["log"]


def test_a_server_that_ignores_range_and_truncates_cannot_loop_forever(
        base, monkeypatch, tmp_path, payload):
    """The retry budget has to count PROGRESS, not bytes.

    One segment, a server that ignores `Range` and hangs up mid-body. Attempt 1
    takes a partial body from zero. Attempt 2 asks to resume, is handed a whole
    body again, rewinds the cursor to zero (the only safe reading of a 200) and
    copies the same prefix — bytes arrived, so a budget keyed on "bytes arrived"
    resets, and attempt 3 is identical. Forever: nothing raises, nothing sets
    stop, and the job hangs with the bar oscillating between 0% and 50% until
    the process is killed. A budget keyed on the cursor MOVING gives up and lets
    the fallback have it.
    """
    url, state = _start_server(payload, ranges=False, chunk_cap=len(payload) // 2)
    _wire(base, monkeypatch, tmp_path, url, len(payload), segment_min=10_000_000)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    finished, outcome = threading.Event(), {}

    def go():
        try:
            base._segmented_fetch("org/m", ["model.safetensors"], "c0m")
        except BaseException as error:  # noqa: BLE001 - carried out to the assertion
            outcome["error"] = error
        finally:
            finished.set()

    threading.Thread(target=go, daemon=True).start()
    ended = finished.wait(timeout=20)
    # Let a leaked thread finish rather than hammer the server for the rest of
    # the session; the assertion below is what reports the failure.
    state["ranges"], state["chunk_cap"] = True, None

    assert ended, "the retry loop never terminated"
    assert isinstance(outcome.get("error"), RuntimeError), outcome


def test_bytes_that_landed_before_an_exception_still_count_as_progress(
        base, monkeypatch, tmp_path, payload):
    """A connection that delivers half a gigabyte and then resets is making
    progress, and the retry budget is documented to reset when bytes arrive.

    It did not: `moved` was the return value of the drain, which a raising
    `read()` never reaches — so hundreds of megabytes on disk counted as a
    failed attempt, five of those aborted the whole multi-file download into the
    fallback, and `_clear_parts` deleted every recorded byte on the way out.
    """
    url, state = _start_server(payload, break_first=3, break_bytes=50_000)
    _wire(base, monkeypatch, tmp_path, url, len(payload), segment_min=10_000_000)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 2)
    # Smaller than one broken chunk, so several reads succeed before the one
    # that raises — which is what puts bytes on disk under an exception.
    monkeypatch.setattr(base, "READ_BYTES", 10_000)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert state["broken"] == 3, "the test never produced a mid-body failure"


def test_a_protocol_error_mid_stream_is_retried_rather_than_fatal(base, monkeypatch,
                                                                  tmp_path, payload):
    """`IncompleteRead` and friends are `http.client.HTTPException`, not
    `OSError` — one of the commonest ways a transport misbehaves, and outside
    the retry loop's reach it aborted the entire multi-file download."""
    url, state = _start_server(payload, break_first=2)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert state["broken"] == 2, "the test never produced a protocol error"


# -- resumability ---------------------------------------------------------------


def test_an_interrupted_fetch_resumes_from_the_recorded_offsets(base, monkeypatch,
                                                                tmp_path, payload):
    """The whole point of the sidecar. A cancel, a crash or a quit mid-download
    used to throw away every byte; the supervisor still kills the fetch on quit
    (AI-5e), and this is what makes that cheap instead of destructive.

    Asserted on the RANGES the second run asked for, not just on the bytes it
    ended up with: a run that silently re-fetched everything would still produce
    a correct file and would still be the bug.
    """
    url, state = _start_server(payload, budget=60_000)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    sidecar = os.path.join(folder, "blobs", "e7ag.fusedpart.json")
    recorded = json.load(open(sidecar))
    assert recorded["etag"] == "e7ag" and recorded["size"] == len(payload)
    landed = sum(s["done"] for s in recorded["segments"])
    assert 0 < landed < len(payload), recorded
    # Durable, not merely counted: the offsets are fsynced before they are
    # written down, so every byte the sidecar claims is really on the disk.
    part = os.path.join(folder, "blobs", "e7ag.fusedpart")
    for segment in recorded["segments"]:
        if segment["done"]:
            got = open(part, "rb").read()[segment["start"]:
                                          segment["start"] + segment["done"]]
            assert got == payload[segment["start"]:segment["start"] + segment["done"]]

    state["budget"] = None
    state["log"].clear()

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    resumed = _offsets(state["log"])
    expected = sorted(s["start"] + s["done"] for s in recorded["segments"]
                      if s["start"] + s["done"] <= s["end"])
    assert resumed == expected, "the second run re-fetched bytes it already had"
    assert not os.path.exists(sidecar)
    assert not os.path.exists(part)


def test_a_probe_that_fails_does_not_throw_away_recorded_progress(base, monkeypatch,
                                                                  tmp_path, payload):
    """4GB of a 4.6GB shard, the app quits, and on restart the CDN answers the
    one-byte probe with a 503.

    Deriving a fresh layout from that failed probe gives ONE segment, the
    sidecar is then rejected on the segment-count mismatch, and four gigabytes
    of durable, correctly recorded progress are deleted and re-fetched on a
    single connection. A probe failing is a network condition; it is not
    evidence that the bytes already on disk are wrong. The layout we already
    fetched into is the layout to resume with.

    Silence is the case here — `_supports_ranges` answering None. A probe that
    answers NO is a fact about the server and does discard the layout; that is
    the case its sibling test covers, and the pair is why the answer is
    three-valued rather than a bool.
    """
    url, state = _start_server(payload, budget=60_000)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    recorded = json.load(open(os.path.join(folder, "blobs", "e7ag.fusedpart.json")))
    assert 0 < sum(seg["done"] for seg in recorded["segments"]) < len(payload)

    state["budget"] = None
    state["log"].clear()
    monkeypatch.setattr(base, "_supports_ranges", lambda location, token: None)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert _offsets(state["log"]) == sorted(
        seg["start"] + seg["done"] for seg in recorded["segments"]
        if seg["start"] + seg["done"] <= seg["end"])


def test_a_probe_that_fails_once_does_not_demote_the_whole_repo(base, monkeypatch,
                                                                tmp_path, payload):
    """A cached NEGATIVE turns one 503 into a fact about the server.

    The probe is memoised per host so a thirty-shard repo asks once — but
    remembering a failure means one transient timeout quietly puts every
    remaining shard on a single connection for the rest of the download, with
    nothing on screen to say the fast path switched itself off. Only an ANSWER
    is worth remembering; a failure to ask is not one.
    """
    url, state = _start_server(payload, probe_fail_first=1)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: {
        "url": url, "location": url, "etag": "e-" + name, "commit": "c0m",
        "size": len(payload)})

    base._segmented_fetch("org/m", ["a.safetensors", "b.safetensors"], "c0m")

    assert state["probes"] == 2, "the failure was cached as an answer"
    # The first file is on one connection, as it must be — nobody knows better
    # yet. The second is not.
    assert _offsets(state["log"]) == [0, 50_000, 100_000, 150_000]


def test_two_identical_files_are_fetched_once(base, monkeypatch, tmp_path, payload):
    """One etag is one blob, and a repo really does publish the same bytes under
    two names. Two fetches of one etag share a part file, a sidecar and a blob
    path: the bytes are pulled twice, and the loser's `os.replace` finds the
    part file already renamed and takes the whole download into the fallback."""
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors", "copy.safetensors"], "c0m")

    for name in ("model.safetensors", "copy.safetensors"):
        assert open(os.path.join(snapshot, name), "rb").read() == payload
    assert os.listdir(os.path.join(folder, "blobs")) == ["e7ag"]
    assert len(_ranges(state["log"])) == 4, "the same bytes were fetched twice"


def test_a_rejected_sidecar_does_not_keep_its_layout(base, monkeypatch, tmp_path,
                                                     payload):
    """Resuming with the layout the bytes were fetched into is right; keeping
    that layout after deciding the sidecar is unusable is not. It leaves a fresh
    download split by a number that came from a file we just deleted — one
    connection for a 4.6GB shard, or dozens for a small one."""
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    blobs = os.path.join(folder, "blobs")
    os.makedirs(blobs)
    with open(os.path.join(blobs, "e7ag.fusedpart"), "wb") as handle:
        handle.write(b"\0" * len(payload))
    with open(os.path.join(blobs, "e7ag.fusedpart.json"), "w") as handle:
        # Two segments, but not the two this size would produce: identity holds,
        # so it survives as far as the layout check, which rejects it.
        json.dump({"etag": "e7ag", "size": len(payload), "segments": [
            {"start": 0, "end": 10, "done": 0},
            {"start": 11, "end": len(payload) - 1, "done": 0}]}, handle)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert _offsets(state["log"]) == [0, 50_000, 100_000, 150_000]


def test_a_resume_whose_server_stopped_honouring_ranges_restarts_that_file(
        base, monkeypatch, tmp_path, payload):
    """The resume path skips the probe on purpose — but only a probe can tell it
    the ground has moved.

    If ranges have genuinely gone (a host swap, an interposing proxy), every
    segment past the first is handed byte 0, `_whole_body` refuses it, and the
    refusal takes down the WHOLE repo: the fallback then deletes the very
    sidecar the un-probed resume existed to protect, plus every other file's
    progress. Restarting this one file from a single segment costs one file's
    bytes and keeps the rest. The distinction that makes both rules hold at once
    is the one round 2 introduced: a probe that fails to ANSWER still leaves the
    recorded layout standing; only a probe that answers "no" discards it.
    """
    url, state = _start_server(payload, budget=60_000)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    sidecar = os.path.join(folder, "blobs", "e7ag.fusedpart.json")
    assert sum(s["done"] for s in json.load(open(sidecar))["segments"]) > 0

    state["budget"], state["ranges"] = None, False
    state["log"].clear()

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert _ranges(state["log"]) == [], "it kept range-fetching a server that said no"


def test_a_segment_cancelled_before_it_reads_winds_down_quietly(base, monkeypatch,
                                                                tmp_path, payload):
    """The wind-down path, which is the ORDINARY way a segment ends badly.

    `stop` is set exactly when a sibling has failed, so every other segment
    arrives at the drain to leave without reading — and two more paths reach the
    same exit, an empty first read and a segment with no room left. A vestigial
    `return moved` survived the rewrite that stopped anyone reading the value,
    so all three raised `UnboundLocalError`. That is a `NameError`, which is not
    in `_TRANSIENT`, so it escaped the retry loop, propagated out of the pool
    and took the download into the fallback — where `_clear_parts` deletes every
    recorded byte. A tidy shutdown became a total loss of resumable state.

    Nothing failed loudly when the return went stale because the caller had
    stopped reading it, which is the general lesson: a value nobody reads is not
    a value nobody evaluates.
    """
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    fetch = _planned(base, folder, url, len(payload))
    segment = fetch.segments[0]

    fetch.stop.set()
    with base._open(url, None) as response:
        assert fetch._drain(response, segment, 0) is None
    assert segment["done"] == 0, "a cancelled segment wrote anyway"

    # …and the loop that starts but breaks on its first pass.
    fetch.stop.clear()
    segment["done"] = segment["end"] - segment["start"] + 1
    with base._open(url, None) as response:
        assert fetch._drain(response, segment, segment["start"]) is None


def test_a_failure_while_publishing_stops_the_other_segments(base, monkeypatch,
                                                             tmp_path, payload):
    """`finish()` is the last thing a segment does, and it can fail for reasons
    that have nothing to do with this download — a full disk, an `os.replace`
    across devices, another instance publishing the same blob first.

    Outside the guard, the exception reached `future.result()` but left `stop`
    clear, and the pool's own shutdown then waits: every remaining segment of
    every remaining file runs to completion before the fallback even starts,
    which on a thirty-shard repo is many minutes and many gigabytes spent on a
    download that has already failed.
    """
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    fetch = _planned(base, folder, url, len(payload))
    segment = fetch.segments[0]
    segment["done"] = segment["end"] - segment["start"] + 1  # the last one home
    fetch.pending = 1
    fetch.finish = lambda: (_ for _ in ()).throw(OSError("No space left on device"))

    with pytest.raises(OSError):
        base._run_segment(fetch, segment)

    assert fetch.stop.is_set(), "the siblings were left pulling bytes for nothing"


@pytest.mark.parametrize("wrong", ["etag", "size", "segments"])
def test_a_sidecar_that_does_not_match_is_thrown_away(base, monkeypatch, tmp_path,
                                                      payload, wrong):
    """Resume state left over from a different revision of the file is the one
    input that turns a successful download into a corrupt blob — every segment
    "already done", nothing fetched, and a file of exactly the right length
    holding whatever was there before.

    The sidecar deliberately claims the file is COMPLETE and describes the same
    layout this run computes, so nothing but the field under test can reject it.
    """
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    blobs = os.path.join(folder, "blobs")
    os.makedirs(blobs)
    with open(os.path.join(blobs, "e7ag.fusedpart"), "wb") as handle:
        handle.write(b"\xff" * len(payload))
    span = len(payload) // 4
    state = {"etag": "e7ag", "size": len(payload),
             "segments": [{"start": i * span, "end": (i + 1) * span - 1,
                           "done": span} for i in range(4)]}
    state[wrong] = {"etag": "an-older-revision", "size": len(payload) + 1,
                    "segments": state["segments"][:2]}[wrong]
    with open(os.path.join(blobs, "e7ag.fusedpart.json"), "w") as handle:
        json.dump(state, handle)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


# -- the cache layout is the Hub's, not ours ------------------------------------


def test_the_cache_layout_is_the_one_huggingface_hub_reads(base, monkeypatch,
                                                           tmp_path, payload):
    """A cache only this code can read is a cache the libraries cannot load
    from — the download would "succeed" and `from_pretrained` would go back to
    the network. Blob by etag, snapshot by commit, relative symlink, refs."""
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert snapshot == os.path.join(folder, "snapshots", "c0m")
    link = os.path.join(snapshot, "model.safetensors")
    blob = os.path.join(folder, "blobs", "e7ag")
    if os.path.islink(link):
        # Relative, like hf's own `_create_symlink`: an absolute one breaks the
        # moment the cache is moved or read from another mount.
        assert not os.path.isabs(os.readlink(link))
    assert os.path.realpath(link) == os.path.realpath(blob)
    assert open(os.path.join(folder, "refs", "main")).read() == "c0m"


def test_a_file_already_in_the_cache_is_not_fetched_again(base, monkeypatch,
                                                          tmp_path, payload):
    """A load of a cached model must cost nothing, and it must still LINK: the
    blob being present says nothing about the snapshot entry existing."""
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    os.makedirs(os.path.join(folder, "blobs"))
    with open(os.path.join(folder, "blobs", "e7ag"), "wb") as handle:
        handle.write(payload)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert state["log"] == [], "a cached file was fetched again"
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


def test_several_files_share_one_connection_budget(base, monkeypatch, tmp_path,
                                                   payload):
    """Segments across ALL files are the units of work, under one cap. A pool
    per file would multiply the caps together and open thirty sockets."""
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: {
        "url": url, "location": url, "etag": "e-" + name, "commit": "c0m",
        "size": len(payload)})

    snapshot = base._segmented_fetch("org/m", ["a.safetensors", "b.safetensors"], "c0m")

    for name in ("a.safetensors", "b.safetensors"):
        assert open(os.path.join(snapshot, name), "rb").read() == payload
        assert os.path.exists(os.path.join(folder, "blobs", "e-" + name))


# -- the fallback ---------------------------------------------------------------


def _fake_hub(monkeypatch, **members):
    import sys

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(**members))


def test_one_listing_pins_the_total_the_file_list_and_the_revision(base, monkeypatch,
                                                                   tmp_path, payload):
    """One Hub call decides all three, which is the only way they agree.

    Two of those were once decided separately: the list came from
    `model_info` with no revision — whatever the repo's DEFAULT branch is —
    while the fetch was hardcoded to `main`. Where a repo's default is not
    `main`, that is a list from one revision fetched at another: a genuinely
    different set of bytes, recorded under a ref for the revision we did not
    read, and internally consistent the whole way down, since every etag still
    matches its content. Nothing downstream could detect it.

    So the revision is asked for by name and the fetch is pinned to the COMMIT
    that name resolved to — which also settles the repo moving between the
    listing and the last byte, and it is the same `main` hf's own
    `snapshot_download` defaults to, so the fast path and the fallback cannot
    land on different revisions of one model.
    """
    sha = "a1b2c3d4" * 5  # a real 40-hex commit, so the sha path is exercised
    listings, resolved = [], []

    class _Api:
        def model_info(self, model_id, revision=None, files_metadata=False):
            listings.append((model_id, revision))
            return types.SimpleNamespace(sha=sha, siblings=[
                types.SimpleNamespace(rfilename="model.safetensors",
                                      size=len(payload))])

    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    _fake_hub(monkeypatch, HfApi=_Api, snapshot_download=lambda *a, **k: "/never")
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: (
        resolved.append(revision) or {
            "url": url, "location": url, "etag": "e7ag", "commit": sha,
            "size": len(payload)}))

    snapshot = base.download_snapshot("org/m")

    assert listings == [("org/m", "main")], f"{len(listings)} listings, {listings}"
    assert resolved == [sha], "the file was fetched at a revision by NAME"
    # …and the happy path really did run through us, not through the fallback.
    assert snapshot == os.path.join(folder, "snapshots", sha)
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    # The ref is the NAME that resolved to it, which is what a later offline
    # load asks for — hf writes the same one.
    assert open(os.path.join(folder, "refs", "main")).read() == sha


def test_a_repo_whose_default_branch_is_not_main_never_fetches_at_main(
        base, monkeypatch, tmp_path):
    """The listing names its revision, so a repo that has no `main` fails the
    LISTING rather than quietly fetching something else.

    That is the same answer hf's own downloader gives such a repo — it defaults
    to `main` too — so the two paths agree about it, and the fallback is a
    fallback rather than a divergence. What must never happen is the middle
    case: resolving files at a revision the file list never described.
    """
    resolved = []

    class _Api:
        def model_info(self, model_id, revision=None, files_metadata=False):
            if revision == "main":
                raise RuntimeError("Revision Not Found: main")
            return types.SimpleNamespace(sha="deadbee", siblings=[])

    _fake_hub(monkeypatch, HfApi=_Api,
              snapshot_download=lambda *a, **k: "/cache/snapshots/abc")
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": str(tmp_path))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "_hub_file_meta",
                        lambda repo, name, revision: resolved.append(revision))

    assert base.download_snapshot("org/m") == "/cache/snapshots/abc"
    assert resolved == [], "a file was resolved at a revision nothing listed"


def test_a_ref_is_only_written_for_a_name_a_loader_would_ask_for(base, tmp_path):
    """`refs/<sha>` is not a thing hf ever reads, and a missing ref name is not
    a filename. Both are skipped rather than written as junk beside the blobs."""
    base._write_ref(str(tmp_path), "a1b2c3d4" * 5, "a1b2c3d4" * 5)
    base._write_ref(str(tmp_path), None, "a1b2c3d4" * 5)
    assert not os.path.exists(os.path.join(tmp_path, "refs"))

    base._write_ref(str(tmp_path), "main", "c0m")
    assert open(os.path.join(tmp_path, "refs", "main")).read() == "c0m"


def test_a_cache_filesystem_without_sparse_files_falls_back(base, monkeypatch,
                                                            tmp_path, payload):
    """Out-of-order segments need a file that can be pre-sized for free.

    Where `ftruncate` allocates instead of punching a hole, pre-sizing every
    file up front asks for the repo's whole 25GB before a byte downloads, and
    the progress walk — which counts allocated blocks — would read 100% from the
    first second. Both are hf's job on such a filesystem, not ours.
    """
    _fake_hub(monkeypatch, snapshot_download=lambda *a, **k: "/cache/snapshots/abc",
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(siblings=[])))
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": str(tmp_path))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "_repo_files",
                        lambda model_id, include=None, ignore=None, revision="main":
                        ("c0m", [("model.safetensors", 10)]))
    monkeypatch.setattr(base, "_sparse_ok", lambda folder: False)

    assert base.download_snapshot("org/m") == "/cache/snapshots/abc"


def test_this_machine_can_hold_a_sparse_file(base, tmp_path):
    """A guard against silently losing the whole feature: every filesystem the
    app supports does this, so a red here says the cache lives somewhere that
    cannot, not that the code is wrong."""
    assert base._sparse_ok(str(tmp_path)) is True
    assert os.listdir(tmp_path) == [], "the probe file was left behind"


def test_a_failed_segmented_fetch_falls_back_to_snapshot_download(base, monkeypatch,
                                                                  tmp_path):
    """A repo we cannot range-fetch, or a Hub API that moved, must degrade to
    the behaviour that shipped before this — never to a broken download."""
    called = {}

    def snapshot_download(model_id, ignore_patterns=None, **kwargs):
        called["model"] = model_id
        return "/cache/snapshots/abc"

    _fake_hub(monkeypatch, snapshot_download=snapshot_download,
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(siblings=[])))
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": str(tmp_path))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    def boom(repo, name, revision):
        raise RuntimeError("get_hf_file_metadata is gone")

    monkeypatch.setattr(base, "_hub_file_meta", boom)
    monkeypatch.setattr(base, "_repo_files",
                        lambda model_id, include=None, ignore=None, revision="main":
                        ("c0m", [("model.safetensors", 10)]))

    assert base.download_snapshot("org/m") == "/cache/snapshots/abc"
    assert called["model"] == "org/m"


def test_the_fallback_does_not_inherit_our_half_written_parts(base, monkeypatch,
                                                              tmp_path, payload):
    """Our part files are deliberately not hf's `.incomplete` — hf resumes one
    of those by seeking to its length, and ours are written out of order, so
    handing it one would produce a silently corrupt blob. Belt and braces: the
    fallback clears them anyway, so a stalled attempt cannot go on counting
    towards the progress bar either."""
    url, state = _start_server(payload, budget=60_000)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "_repo_files",
                        lambda model_id, include=None, ignore=None, revision="main":
                        ("c0m", [("model.safetensors", len(payload))]))
    _fake_hub(monkeypatch,
              snapshot_download=lambda model_id, **kwargs: "/cache/snapshots/abc",
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(siblings=[])))

    assert base.download_snapshot("org/m") == "/cache/snapshots/abc"
    assert state["log"], "the segmented route was never tried"
    left = os.listdir(os.path.join(folder, "blobs"))
    assert left == [], f"our part files were left for hf to trip over: {left}"


def test_a_commit_that_moved_under_the_listing_is_refused(base, monkeypatch,
                                                          tmp_path, payload):
    """The fetch is pinned to a commit; the Hub answering with a different one
    means the listing this file set came from no longer describes what is being
    served, and half of each revision is not a snapshot."""
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: {
        "url": url, "location": url, "etag": "e7ag", "commit": "b" * 40,
        "size": len(payload)})

    with pytest.raises(Exception, match="asked for commit"):
        base._segmented_fetch("org/m", ["model.safetensors"], "a" * 40)

    assert not os.path.exists(os.path.join(folder, "blobs", "e7ag"))


def test_download_file_returns_the_path_to_the_one_file(base, monkeypatch,
                                                        tmp_path, payload):
    """`download_file`'s contract is a PATH, and its caller opens it. A GGUF
    fetched into the cache with no snapshot entry would return a path to
    nothing."""
    url, _state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "_repo_files",
                        lambda model_id, include=None, ignore=None, revision="main":
                        ("c0m", [(include, len(payload))]))

    path = base.download_file("org/m", "q4.gguf")

    assert os.path.basename(path) == "q4.gguf"
    assert open(path, "rb").read() == payload
