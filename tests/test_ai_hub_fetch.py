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

The Hub is reached through exactly two seams — `_hub_file_meta` and
`repo_folder` — and both are monkeypatched.

`worker_base` is stdlib-only, and its being so is NOT enforced by hf's absence
from this environment — hf ships with the app (D402), so an accidental
module-scope import of it would pass unnoticed here.
`test_ai_worker_base.py::test_worker_base_imports_nothing_but_the_stdlib` is what
enforces the rule, by reading the module's own imports out of its source.
"""
import email.message
import email.utils
import hashlib
import http.server
import importlib.util
import json
import os
import socket
import socketserver
import threading
import time
import types
import urllib.error

import pytest

# Every test in this file drives _segmented_fetch directly and asserts the
# SEGMENTED layout — four bodies at four offsets, a sidecar of many pieces, a
# one-byte range probe. A platform without os.pwrite cannot produce one: it
# fetches each file on a single append-only stream instead (_appends_only), so
# these assertions would be red there for a reason that is not a defect. That
# path is not untested — tests/test_ai_hub_fetch_no_pwrite.py drives it, on
# every OS, and runs against the real platform condition on win32.
pytestmark = pytest.mark.skipif(
    not hasattr(os, "pwrite"),
    reason="this module asserts the multi-segment layout, which needs os.pwrite "
           "(POSIX). The single-stream path a platform without it takes is "
           "tested in test_ai_hub_fetch_no_pwrite.py, which runs everywhere.",
)

#: The only addresses these tests may talk to. Everything here drives a local
#: `http.server` on 127.0.0.1; nothing may reach huggingface.co.
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0"})


@pytest.fixture(autouse=True)
def no_egress(monkeypatch):
    """Make leaving this machine impossible, for every test in this module.

    **By construction, not by remembering.** Every one of these tests stubs the
    Hub — and the claim that they therefore reach no network was FALSE, proven by
    Windows CI: the mirror path could not run without `os.pwrite` at the time, so
    on win32 the branch degraded to the Hub listing, which is real
    `huggingface_hub`, which made a real HTTPS request and failed on a 401 from
    huggingface.co. On a machine with a valid `HF_TOKEN` that test would have
    PASSED by downloading a repo called `org/m`; on an air-gapped runner it would
    fail for a third unrelated reason. Neither is a test. (The mirror does run
    without `os.pwrite` now — one append-only stream — but the escape this guards
    against is any test whose mirror path degrades unexpectedly, which no
    transport change retires.)

    The fix cannot be "stub the Hub in every test", because the escape appears
    exactly where a test did not anticipate falling back. So this refuses the
    socket instead: any test whose mirror path breaks now fails saying so,
    naming the address it tried to reach, rather than reporting somebody else's
    status code.

    `getaddrinfo` as well as `connect`, so the refusal happens before a DNS
    lookup rather than after one.

    **Measured, not assumed.** Running this feature's three test files with
    `os.pwrite` removed (the win32 condition) and the platform skips disabled,
    FOUR tests try to resolve huggingface.co — the round-trip test that failed in
    CI, the two `_401_on_a_mirror_blob` tests here, and
    `test_download_file_returns_the_path_to_the_one_file`, which predates this
    feature: it stubs `_repo_files` but not `snapshot_download`, so
    `download_file`'s own fallback would have gone to the real Hub. That one was
    protected only by the module-level skip above and by the segmented path
    happening to work on POSIX. It is left as it is, because a stub there would
    change what an unrelated test asserts; this fixture is the right fix for all
    four.
    """
    connect = socket.socket.connect
    getaddrinfo = socket.getaddrinfo

    def guarded_connect(self, address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and host not in _LOOPBACK:
            raise AssertionError(
                f"this test tried to reach {address!r}. Every test here drives a "
                f"local server; a request leaving the machine means a code path "
                f"fell back to the real Hub, which is the bug, not the network.")
        return connect(self, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if host is not None and host not in _LOOPBACK:
            raise AssertionError(
                f"this test tried to resolve {host!r}; see `no_egress`.")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


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
      plain={path, …}     these paths answer NORMALLY, ignoring every
                          misbehaviour flag above (they are still logged). What
                          it is for: the model mirror serves a manifest and its
                          blobs on ONE host, and "the manifest arrives and then
                          the BLOB 401s" is a different test from "the manifest
                          401s" — without this, a flag aimed at the blobs hits
                          the manifest first, because the manifest is the first
                          request of the download.
      routes={path: …}    serve a DIFFERENT body per path instead of `payload`
                          everywhere: `bytes` is that path's body, an `int` is
                          the status to answer with, and a path not in the map
                          is a 404. This is what lets the model-mirror tests
                          drive one server that answers a manifest at one URL
                          and blobs at others — the mirror's whole protocol is
                          two object shapes on one host, and a second harness
                          would be a second set of CDN misbehaviours to keep in
                          step with this one.

    `state["requests"]` records the path, `Range` and `Authorization` of every
    request, which is how the CDN-credential test can assert on a header that
    must NOT be there.
    """
    state = {"log": [], "requests": [], "served": 0, "broken": 0, "real": 0,
             "routes": None, "plain": (),
             "probes": 0, "lock": threading.Lock(),
             "ranges": True, "lie_after_probe": False, "clamp": False,
             "budget": None, "unauthorized": 0, "unauthorized_on": (),
             "break_first": 0, "break_bytes": 0, "chunk_cap": None,
             "probe_fail_first": 0,
             # `hold_first_real`: the FIRST real (non-probe) request blocks
             # entirely — no status line, no bytes — until `state["release"]`
             # is set. Models one connection stalled mid-flight, for a test
             # that needs to observe OTHER chunks being asked for while one is
             # still open, not merely infer it from timing.
             "hold_first_real": False, "release": threading.Event(),
             "_held": False,
             # `throttle_first`: the first N real requests answer 429 (or
             # `throttle_status`, for the 503-with-a-Retry-After shape), each
             # carrying `retry_after` as the header verbatim when it is not
             # None. A rate limit is the one CDN misbehaviour that is not a
             # fault — the server is asking us to wait — so it needs its own
             # flag rather than riding on `unauthorized`, whose whole answer is
             # to re-resolve.
             # `ratelimit`: the IETF `RateLimit` header verbatim, which is what
             # the Hub actually answers a 429 with — `Retry-After` is the shape
             # other hosts use, so both are here and a test can send either,
             # both, or neither.
             "throttle_first": 0, "throttled": 0, "retry_after": None,
             "ratelimit": None, "throttle_status": 429}
    state.update(flags)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get("Range")
            probe = header == "bytes=0-0"
            failed_probe = expired = hold = False  # bound on every branch
            throttled = False
            # `whole` is what THIS path serves, and `status` a status to answer
            # with instead of a body. Without `routes` every path serves the one
            # `payload`, exactly as before.
            whole, status = payload, None
            plain = self.path in state["plain"]
            if state["routes"] is not None:
                served = state["routes"].get(self.path)
                if served is None:
                    status = 404
                elif isinstance(served, int):
                    status = served
                else:
                    whole = served
            with state["lock"]:
                state["log"].append(header)
                state["requests"].append({
                    "path": self.path, "range": header,
                    "auth": self.headers.get("Authorization")})
                if probe:
                    state["probes"] += 1
                    failed_probe = (not plain
                                    and state["probes"] <= state["probe_fail_first"])
                else:
                    state["real"] += 1
                    expired = not plain and (state["unauthorized"] > 0
                                             or state["real"] in state["unauthorized_on"])
                    if state["unauthorized"] > 0 and not plain:
                        state["unauthorized"] -= 1
                    if not plain and state["throttled"] < state["throttle_first"]:
                        state["throttled"] += 1
                        throttled = True
                    if state["hold_first_real"] and not state["_held"] and not plain:
                        state["_held"] = True
                        hold = True

            if hold:
                state["release"].wait(timeout=10.0)

            if status is not None:
                # Logged first, above: a test that asserts the mirror was
                # PROBED and got a 404 needs the request on the log either way.
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if failed_probe:
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not probe and throttled:
                self.send_response(state["throttle_status"])
                if state["retry_after"] is not None:
                    self.send_header("Retry-After", str(state["retry_after"]))
                if state["ratelimit"] is not None:
                    self.send_header("RateLimit", state["ratelimit"])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not probe and expired:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not probe and state["break_first"] and not plain:
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
                            f"bytes {at}-{last or len(whole) - 1}"
                            f"/{len(whole)}")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    keep = whole[at:at + state["break_bytes"]]
                    if keep:
                        # One valid chunk the client keeps, and only THEN the
                        # garbage — bytes on disk under an exception, which is
                        # a different thing from a response that never worked.
                        self.wfile.write(f"{len(keep):X}\r\n".encode()
                                         + keep + b"\r\n")
                    self.wfile.write(b"not-a-chunk-length\r\n")
                    self.close_connection = True
                    return

            start, end, partial = 0, len(whole) - 1, False
            if header and state["ranges"] and (probe or not state["lie_after_probe"]):
                spec = header.split("=", 1)[1]
                first, _, last = spec.partition("-")
                start = int(first)
                end = int(last) if last else len(whole) - 1
                partial = True
            if partial and not probe and state["clamp"]:
                start = 0  # the range is answered, but not the one that was asked
            body = whole[start:end + 1]

            allowed = len(body)
            if state["chunk_cap"] is not None and not probe and not plain:
                allowed = min(allowed, state["chunk_cap"])
            if state["budget"] is not None and not probe and not plain:
                with state["lock"]:
                    allowed = max(0, min(allowed, state["budget"] - state["served"]))
                    state["served"] += allowed

            self.send_response(206 if partial else 200)
            self.send_header("Content-Length", str(len(body)))
            if partial:
                self.send_header("Content-Range",
                                 f"bytes {start}-{end}/{len(whole)}")
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
    # The origin as well as the one URL: a `routes` server is addressed by
    # several paths, and the mirror's base URL is one of them.
    state["origin"] = f"http://127.0.0.1:{server.server_address[1]}"
    return state["origin"] + "/weights", state


@pytest.fixture()
def payload():
    """Deterministic bytes, big enough to split several ways."""
    return hashlib.sha256(b"weights").digest() * 6250  # 200_000 bytes


def _wire(base, monkeypatch, tmp_path, url, size, etag="e7ag", commit="c0m",
          segment_min=20_000, chunk_bytes=50_000):
    """Point worker_base at the local server and a throwaway cache folder.

    `chunk_bytes` stands in for the real `CHUNK_BYTES` (32MB), the same way
    `segment_min` stands in for `SEGMENT_MIN_BYTES`: the 200_000-byte `payload`
    fixture has to split several ways without downloading real megabytes. The
    default, 50_000, divides the fixture into exactly four equal chunks — the
    same four offsets `MAX_SEGMENTS_PER_FILE` used to produce — so the many
    existing assertions on `[0, 50_000, 100_000, 150_000]` keep meaning what
    they said even though nothing under test caps the chunk COUNT any more.
    """
    folder = str(tmp_path / "models--org--m")
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": folder)
    monkeypatch.setattr(base, "SEGMENT_MIN_BYTES", segment_min)
    monkeypatch.setattr(base, "CHUNK_BYTES", chunk_bytes)
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
        json.dump({"version": base.SIDECAR_VERSION, "etag": "e7ag",
                  "size": len(payload), "segments": [
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
    state = {"version": base.SIDECAR_VERSION, "etag": "e7ag", "size": len(payload),
             "segments": [{"start": i * span, "end": (i + 1) * span - 1,
                           "done": span} for i in range(4)]}
    state[wrong] = {"etag": "an-older-revision", "size": len(payload) + 1,
                    "segments": state["segments"][:2]}[wrong]
    with open(os.path.join(blobs, "e7ag.fusedpart.json"), "w") as handle:
        json.dump(state, handle)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


def test_a_sidecar_from_before_the_chunk_queue_is_discarded_not_misread(
        base, monkeypatch, tmp_path, payload):
    """The chunk queue changed the shape of a resume: segments used to be
    `size / MAX_SEGMENTS_PER_FILE` equal shares, and are now fixed-size chunks
    pulled from a queue. A sidecar an OLDER build left behind describes the old
    shape — same field names, different boundaries — and reading it as the new
    shape would silently misplace every cursor: bytes recorded as landed at one
    offset are really the offset a four-way split put there, and resuming
    "into" them writes the file wrong under a correct-looking etag and size.

    So the sidecar carries an explicit `version`, and anything that is not
    today's number is treated exactly like no sidecar at all — the safe
    reading, since size/etag still match and nothing else marks it as stale.
    This one has no `version` key at all, which is what every sidecar written
    before this existed looks like.
    """
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    blobs = os.path.join(folder, "blobs")
    os.makedirs(blobs)
    with open(os.path.join(blobs, "e7ag.fusedpart"), "wb") as handle:
        handle.write(b"\0" * len(payload))
    with open(os.path.join(blobs, "e7ag.fusedpart.json"), "w") as handle:
        # The OLD shape: two equal size/2 shares, no `version` key at all.
        half = len(payload) // 2
        json.dump({"etag": "e7ag", "size": len(payload), "segments": [
            {"start": 0, "end": half - 1, "done": half},
            {"start": half, "end": len(payload) - 1, "done": half}]}, handle)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    # A fresh, fixed-size-chunk download — not a resume into the old layout's
    # offsets, which this asserts by seeing every chunk boundary asked for.
    assert _offsets(state["log"]) == [0, 50_000, 100_000, 150_000]


def test_a_sidecar_from_a_future_version_is_also_discarded(base, monkeypatch,
                                                            tmp_path, payload):
    """Not just missing — ANY version that is not today's number, because a
    format can change again and a stale reader must not guess that a number it
    does not recognise is close enough."""
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    blobs = os.path.join(folder, "blobs")
    os.makedirs(blobs)
    with open(os.path.join(blobs, "e7ag.fusedpart"), "wb") as handle:
        handle.write(b"\0" * len(payload))
    with open(os.path.join(blobs, "e7ag.fusedpart.json"), "w") as handle:
        json.dump({"version": base.SIDECAR_VERSION + 1, "etag": "e7ag",
                  "size": len(payload), "segments": [
                      {"start": 0, "end": len(payload) - 1, "done": len(payload)}]},
                  handle)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


@pytest.mark.parametrize("content", ["2", "[1, 2, 3]", '"just a string"'])
def test_a_sidecar_that_is_not_an_OBJECT_is_thrown_away_not_fatal(
        base, monkeypatch, tmp_path, payload, content):
    """A sidecar whose JSON parses but is not a dict — a truncated write that
    still happens to be valid JSON on its own — used to hit `state["etag"]`
    and raise `TypeError`, caught by the same tuple as everything else here:
    "no sidecar", one file restarts clean. `state.get("version")` runs BEFORE
    that check now, and `.get` on a non-dict raises `AttributeError` instead —
    which was NOT in the tuple, so this escaped `_saved` and `plan()` entirely,
    turning a clean one-file restart into a whole-repo fallback that deletes
    every OTHER file's progress via `_clear_parts`. This must still resolve
    quietly to a fresh download of the one affected file, on the segmented
    path — never a repo-wide fallback.
    """
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    blobs = os.path.join(folder, "blobs")
    os.makedirs(blobs)
    with open(os.path.join(blobs, "e7ag.fusedpart"), "wb") as handle:
        handle.write(b"\0" * len(payload))
    with open(os.path.join(blobs, "e7ag.fusedpart.json"), "w") as handle:
        handle.write(content)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    # Fresh, fixed-size-chunk download — the segmented path, not the fallback:
    # a repo-wide `_Unsegmentable`/fallback would never touch this server at
    # all, since `_fake_hub`/`_wire` only wires the real server for the fast
    # path.
    assert _offsets(state["log"]) == [0, 50_000, 100_000, 150_000]


def test_a_large_file_splits_into_many_fixed_size_chunks_not_four(
        base, monkeypatch, tmp_path):
    """`MAX_SEGMENTS_PER_FILE` capped a file at 4 segments so that a static
    size/N split never opened more than 4 sockets for one file — a number that
    stopped meaning anything once segments became fixed-size chunks pulled from
    a GLOBAL queue capped by `MAX_CONNECTIONS`. A file many times a chunk must
    still produce many chunks: that is what lets a worker that finishes early
    pull the NEXT chunk instead of finding nothing left to steal.
    """
    payload_size = 900_000  # 18 chunks at the 50_000-byte test chunk size
    url, state = _start_server(b"\0" * payload_size)
    _wire(base, monkeypatch, tmp_path, url, payload_size)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert len(_ranges(state["log"])) == 18, (
        "a big file was still capped at a handful of segments")


def test_a_slow_chunk_does_not_block_other_chunks_from_starting(base, monkeypatch,
                                                                tmp_path):
    """The mechanism behind the tail fix: workers pull the NEXT chunk off a
    shared queue rather than each owning a fixed share for the whole download.

    One connection is held open — mid-body, past its headers — while the pool
    has `MAX_CONNECTIONS - 1` other workers free. If chunks were still static
    per-worker shares, those workers would have nothing else queued once their
    own share was assigned; with a queue many chunks deep, they keep pulling
    and finishing the REST of the file while the slow one is still in flight.
    Asserted on ORDER, not on wall-clock: every other chunk's request has to
    reach the server before the held one is allowed to finish, which a
    time-based assertion could only ever suggest and this proves directly.
    """
    payload_size = 900_000  # 18 chunks at the 50_000-byte test chunk size
    url, state = _start_server(b"\0" * payload_size, hold_first_real=True)
    _wire(base, monkeypatch, tmp_path, url, payload_size)
    monkeypatch.setattr(base, "MAX_CONNECTIONS", 8)

    thread = threading.Thread(
        target=base._segmented_fetch, args=("org/m", ["model.safetensors"], "c0m"))
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while not state["_held"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state["_held"], "no request ever reached the server"

        # The other 17 chunks are asked for while the first sits open with no
        # response at all — only possible if a free worker pulls the NEXT
        # queued chunk instead of finding nothing left assigned to it, which
        # is exactly the property a static size/N split does not have.
        deadline = time.monotonic() + 5.0
        while len(_ranges(state["log"])) < 18 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(_ranges(state["log"])) == 18, (
            "the rest of the file waited on the one held-open chunk")
    finally:
        state["release"].set()
        thread.join(timeout=5.0)


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
                        lambda model_id, include=None, allow=None, ignore=None, revision="main":
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
                        lambda model_id, include=None, allow=None, ignore=None, revision="main":
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
                        lambda model_id, include=None, allow=None, ignore=None, revision="main":
                        ("c0m", [("model.safetensors", len(payload))]))
    _fake_hub(monkeypatch,
              snapshot_download=lambda model_id, **kwargs: "/cache/snapshots/abc",
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(siblings=[])))

    assert base.download_snapshot("org/m") == "/cache/snapshots/abc"
    assert state["log"], "the segmented route was never tried"
    left = os.listdir(os.path.join(folder, "blobs"))
    assert left == [], f"our part files were left for hf to trip over: {left}"


@pytest.mark.parametrize("breaks", ["listing", "fetch"])
def test_download_file_falls_back_like_the_snapshot_does(base, monkeypatch, tmp_path,
                                                         payload, breaks):
    """The single-file arm has the same two failure points as the snapshot arm
    and had neither of them exercised. It is the diffusers runner's quantized
    transformer — a 2.6GB GGUF — so "it falls back" is not a detail: a listing
    that fails and a fetch that fails must both still fetch the file."""
    url, _state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    _fake_hub(monkeypatch, hf_hub_download=lambda **kwargs: "/cache/blobs/gguf",
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(siblings=[])))

    def boom(*args, **kwargs):
        raise RuntimeError("the Hub is unreachable")

    if breaks == "listing":
        monkeypatch.setattr(base, "_repo_files", boom)
    else:
        monkeypatch.setattr(base, "_repo_files",
                            lambda repo, include=None, allow=None, ignore=None, revision="main":
                            ("c0m", [(include, len(payload))]))
        monkeypatch.setattr(base, "_hub_file_meta", boom)

    assert base.download_file("org/m", "q4.gguf") == "/cache/blobs/gguf"


def test_download_files_fallback_wires_a_byte_counter_through(base, monkeypatch,
                                                               tmp_path, payload):
    """`download_file`'s own `hub()`, not just `download_snapshot`'s: each call
    site builds its own `_HubByteTicker` and has to hand hf's downloader the
    matching `tqdm_class`, or the fallback bar is back to disk-walk-only."""
    _wire(base, monkeypatch, tmp_path, "http://unused/weights", len(payload))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    seen_bars = []

    def hf_hub_download(repo_id=None, filename=None, tqdm_class=None, **kwargs):
        if tqdm_class is not None:
            bar = tqdm_class(desc=f"{filename}: reconstructing file",
                             total=len(payload), unit="B")
            bar.update(len(payload))
            seen_bars.append(bar)
        return "/cache/blobs/gguf"

    _fake_hub(monkeypatch, hf_hub_download=hf_hub_download,
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(siblings=[])))
    monkeypatch.setattr(base, "_repo_files", lambda *a, **k: (
        _ for _ in ()).throw(RuntimeError("no listing")))

    assert base.download_file("org/m", "q4.gguf") == "/cache/blobs/gguf"
    assert len(seen_bars) == 1, "download_file's fallback did not pass tqdm_class"


@pytest.mark.parametrize("call", ["snapshot", "file"])
def test_a_cancel_is_never_swallowed_into_a_fallback(base, monkeypatch, tmp_path,
                                                     call):
    """The ✕ is the one failure that must NOT degrade to hf's downloader.

    Every other failure here means "try the slow way"; a cancel means the user
    asked us to stop, and treating it as one more reason to fall back would
    start a fresh multi-gigabyte `snapshot_download` out of pressing Stop.
    """
    started = []
    _fake_hub(monkeypatch,
              snapshot_download=lambda *a, **k: started.append("snapshot"),
              hf_hub_download=lambda **k: started.append("file"),
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(
                      sha="c0m", siblings=[types.SimpleNamespace(
                          rfilename="w.gguf", size=10)])))
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": str(tmp_path))
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)

    def cancelled(*args, **kwargs):
        raise base.Cancelled()

    monkeypatch.setattr(base, "_segmented_fetch", cancelled)

    with pytest.raises(base.Cancelled):
        if call == "snapshot":
            base.download_snapshot("org/m")
        else:
            base.download_file("org/m", "w.gguf")

    assert started == [], "pressing Stop started a download instead"


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
                        lambda model_id, include=None, allow=None, ignore=None, revision="main":
                        ("c0m", [(include, len(payload))]))

    path = base.download_file("org/m", "q4.gguf")

    assert os.path.basename(path) == "q4.gguf"
    assert open(path, "rb").read() == payload


# -- metadata from somewhere other than the Hub ---------------------------------


def _provider(url, size, etag="e7ag", commit="c0m", **extra):
    """A `_hub_file_meta`-shaped provider, and the calls it received.

    The same five keys `_hub_file_meta` returns, so `_segmented_fetch` cannot
    tell where they came from. `extra` is what a NON-Hub source can add that the
    Hub cannot — `sha256`, for the mirror.
    """
    calls = []

    def meta(repo_id, filename, revision):
        calls.append((repo_id, filename, revision))
        return dict({"url": url, "location": url, "etag": etag,
                     "commit": commit, "size": size}, **extra)

    return meta, calls


def test_a_supplied_metadata_provider_replaces_the_hub_call(base, monkeypatch,
                                                            tmp_path, payload):
    """The seam the model mirror hangs off: the fetcher takes the per-file
    metadata from its caller instead of resolving it against the Hub.

    Asserted by making `_hub_file_meta` FATAL rather than by counting calls —
    "the Hub was not consulted" is the property, and a provider that silently
    fell back to it would still produce the right file.
    """
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))

    def never(repo, name, revision):
        raise AssertionError("the Hub was consulted for a mirrored file")

    monkeypatch.setattr(base, "_hub_file_meta", never)
    meta, calls = _provider(url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m",
                                     meta=meta)

    assert calls == [("org/m", "model.safetensors", "c0m")]
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    # Right down to the cache layout: the provider changes where the metadata
    # comes from and nothing else.
    assert os.path.exists(os.path.join(folder, "blobs", "e7ag"))
    assert open(os.path.join(folder, "refs", "main")).read() == "c0m"
    assert _offsets(state["log"]) == [0, 50_000, 100_000, 150_000]


def test_the_hub_path_resolves_exactly_as_it_did_before_the_seam(base, monkeypatch,
                                                                 tmp_path, payload):
    """No provider means today's behaviour, unchanged: one `_hub_file_meta` per
    file, with the repo id and the pinned revision."""
    url, _state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    seen = []
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: (
        seen.append((repo, name, revision)) or {
            "url": url, "location": url, "etag": name, "commit": "c0m",
            "size": len(payload)}))

    base._segmented_fetch("org/m", ["a.bin", "b.bin"], "c0m")

    assert seen == [("org/m", "a.bin", "c0m"), ("org/m", "b.bin", "c0m")]


def test_a_supplied_provider_keeps_the_one_commit_and_pinning_checks(base,
                                                                     monkeypatch,
                                                                     tmp_path,
                                                                     payload):
    """The two rules that make a fetch a SNAPSHOT rather than a pile of files
    are properties of the fetcher, not of the Hub, so a provider does not get to
    opt out of them.

    A provider is code we wrote, but it reads a manifest we did not: a manifest
    naming a commit other than the one being fetched at is exactly the mistake
    the pin exists to catch.
    """
    url, _state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))

    moved, _calls = _provider(url, len(payload), commit="b" * 40)
    with pytest.raises(Exception, match="asked for commit"):
        base._segmented_fetch("org/m", ["model.safetensors"], "a" * 40, meta=moved)

    def two(repo_id, filename, revision):
        return {"url": url, "location": url, "etag": filename,
                "commit": "a" * 40 if filename == "a.bin" else "b" * 40,
                "size": len(payload)}

    with pytest.raises(Exception, match="2 commits"):
        base._segmented_fetch("org/m", ["a.bin", "b.bin"], "a" * 40, meta=two)


def test_a_fetch_that_carries_no_token_sends_no_authorization(base, monkeypatch,
                                                              tmp_path, payload):
    """`token=None` means anonymous, even where `location == url`.

    `_cdn_token` sends the Hub token only when the blob URL IS the Hub URL, which
    is true of our own mirror by construction — same URL, no presigned redirect.
    Without this seam a user with a Hub token set would offer it to whatever host
    `FUSED_MODEL_MIRROR` names, which is a credential going somewhere it was
    never granted for.
    """
    url, state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "_hf_token", lambda: "hf_secret")
    meta, _calls = _provider(url, len(payload))

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m", meta=meta,
                          token=None)

    assert [r["auth"] for r in state["requests"]] == [None] * len(state["requests"])
    assert "hf_secret" not in json.dumps(state["requests"])


def test_the_hub_path_still_sends_the_token_when_the_hub_serves_the_blob(
        base, monkeypatch, tmp_path, payload):
    """The other half of the same rule: no `token` argument means ask hf's own
    store, exactly as before, or every gated repo 401s on the blob."""
    url, state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "_hf_token", lambda: "hf_secret")

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert any(r["auth"] == "Bearer hf_secret" for r in state["requests"])


# -- the model mirror: our own codepath for a suggested model --------------------
#
# `download_snapshot` is the decision point for a REPO, below every runner call
# site, so nothing in a runner changes. (A one-FILE download has its own branch
# and its own object — see the AI-5m section further down; this one is about the
# per-repo manifest and its completeness claim.) What lands on disk is a normal
# hf cache entry either way — which is the whole design — so these tests assert
# on the LAYOUT and on which host was asked, never on an internal flag.

MIRROR_COMMIT = "a1b2c3d4" * 5


def _mirror_manifest(payload, name="model.safetensors", **overrides):
    entry = {"name": name, "etag": "beef" * 10, "size": len(payload),
             "sha256": hashlib.sha256(payload).hexdigest()}
    manifest = {"schema": 1, "repo": "org/m", "commit": MIRROR_COMMIT,
                "complete": True, "files": [entry]}
    manifest.update(overrides)
    return manifest


MANIFEST_PATH = "/models/org/m/manifest.json"


def _mirror_server(payload, manifest=None, manifest_status=None, blob=True,
                   plain_manifest=False, **flags):
    """A mirror serving one manifest and one blob, on one local port.

    `manifest_status` replaces the manifest with a status code (404 for a model
    nobody mirrored, 503 for a distribution having a bad day). `blob=False`
    leaves the blob URL a 404 — a manifest that promises bytes the mirror does
    not hold, which is the mid-download failure. `plain_manifest` exempts the
    manifest from the misbehaviour flags, so a flag can be aimed at the BLOBS —
    the manifest is the download's first request and would otherwise absorb it.
    """
    manifest = _mirror_manifest(payload) if manifest is None else manifest
    body = manifest_status if manifest_status else json.dumps(manifest).encode()
    routes = {MANIFEST_PATH: body}
    if plain_manifest:
        flags["plain"] = (MANIFEST_PATH,)
    if blob and manifest.get("files"):
        etag = manifest["files"][0].get("etag", "")
        routes[f"/models/org/m/{manifest.get('commit')}/{etag}"] = payload
    _url, state = _start_server(b"", routes=routes, **flags)
    return state


def _mirror_wire(base, monkeypatch, tmp_path, state, model_id="org/m",
                 permitted=True):
    """Point `worker_base` at that mirror and make the Hub FATAL.

    Fatal rather than merely counted: "no request to huggingface.co" is the
    property the whole feature exists for, and a path that quietly listed the
    repo anyway would still produce the right files.
    """
    folder = str(tmp_path / "models--org--m")
    monkeypatch.setattr(base, "repo_folder",
                        lambda model_id, repo_type="model": folder)
    monkeypatch.setattr(base, "SEGMENT_MIN_BYTES", 20_000)
    monkeypatch.setattr(base, "CHUNK_BYTES", 50_000)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setenv("FUSED_MODEL_MIRROR", state["origin"])
    if permitted:
        monkeypatch.setenv("FUSED_MODEL_MIRROR_OK", model_id)
    else:
        monkeypatch.delenv("FUSED_MODEL_MIRROR_OK", raising=False)
    return folder


def _hub_is_fatal(base, monkeypatch):
    def listing(*args, **kwargs):
        raise AssertionError("the Hub was listed for a mirrored model")

    def meta(*args, **kwargs):
        raise AssertionError("the Hub was asked to resolve a mirrored file")

    monkeypatch.setattr(base, "_repo_files", listing)
    monkeypatch.setattr(base, "_hub_file_meta", meta)
    _fake_hub(monkeypatch, snapshot_download=listing,
              HfApi=listing, try_to_load_from_cache=listing)


def _hub_answers(base, monkeypatch, payload, name="model.safetensors"):
    """The Hub path, wired to succeed — what every failure must land on."""
    fell_back = []

    def snapshot_download(model_id, **kwargs):
        fell_back.append(model_id)
        return "/cache/snapshots/from-the-hub"

    monkeypatch.setattr(base, "_repo_files",
                        lambda model_id, include=None, allow=None, ignore=None,
                        revision="main": (None, [(name, len(payload))]))
    _fake_hub(monkeypatch, snapshot_download=snapshot_download,
              HfApi=lambda: types.SimpleNamespace(
                  model_info=lambda *a, **k: types.SimpleNamespace(siblings=[])))
    return fell_back


def test_a_mirrored_model_is_fetched_from_our_own_distribution(base, monkeypatch,
                                                               tmp_path, payload):
    """The feature, end to end: no request to the Hub, and an hf cache entry.

    Byte-for-byte the layout hf itself would have produced — blob under its etag,
    a relative symlink from the snapshot, `refs/main` pointing at the commit —
    because everything downstream (the loaders, the Local tab's inventory, disk
    usage, deletion) reads that layout and knows nothing about this feature.
    """
    state = _mirror_server(payload)
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal(base, monkeypatch)

    snapshot = base.download_snapshot("org/m")

    assert snapshot == os.path.join(folder, "snapshots", MIRROR_COMMIT)
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert os.path.islink(os.path.join(snapshot, "model.safetensors"))
    assert not os.path.isabs(os.readlink(os.path.join(snapshot,
                                                      "model.safetensors")))
    assert open(os.path.join(folder, "refs", "main")).read() == MIRROR_COMMIT
    # One manifest request, before any bytes: the counting key.
    paths = [r["path"] for r in state["requests"]]
    assert paths.count("/models/org/m/manifest.json") == 1
    assert paths[0] == "/models/org/m/manifest.json"
    # …and the bytes came off the commit-pinned blob URL, range-fetched by the
    # existing chunk queue rather than by anything new.
    etag = "beef" * 10
    assert all(p == f"/models/org/m/{MIRROR_COMMIT}/{etag}"
               for p in paths[1:]), paths
    assert _offsets(state["log"]) == [0, 50_000, 100_000, 150_000]
    # No credential offered to a host that was never granted one.
    assert [r["auth"] for r in state["requests"]] == [None] * len(paths)


def test_a_mirrored_download_is_then_served_from_the_cache(base, monkeypatch,
                                                           tmp_path, payload):
    """A second download returns instantly, with no network at all.

    The fast path is keyed off this app's own fetch RECORD, so the mirror path
    has to write one — otherwise a mirrored model is cold forever and every
    load re-resolves over the network.
    """
    state = _mirror_server(payload)
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal(base, monkeypatch)

    snapshot = base.download_snapshot("org/m")
    served = len(state["requests"])

    # `local()` is hf's `snapshot_download(local_files_only=True)`, which reads
    # the cache the mirror just filled; stubbed to the path it would resolve.
    _fake_hub(monkeypatch, snapshot_download=lambda *a, **k: snapshot)

    assert base.download_snapshot("org/m") == snapshot
    assert len(state["requests"]) == served, "the mirror was asked again"
    assert base._recorded_files(folder, MIRROR_COMMIT, None, None) == [
        "model.safetensors"]


@pytest.mark.parametrize("kind, kwargs", [
    ("404", {"manifest_status": 404}),
    ("5xx", {"manifest_status": 503}),
    ("malformed", {"manifest": {"schema": 1, "repo": "org/m", "complete": True,
                                "commit": "not-a-sha", "files": []}}),
    ("not json", {"manifest": None}),
])
def test_a_mirror_that_cannot_answer_lands_on_the_hub_path(base, monkeypatch,
                                                           tmp_path, payload,
                                                           kind, kwargs, capsys):
    """Four ways for the mirror to be useless, one outcome: today's download.

    This is the property the whole feature is allowed to exist on. A mirror that
    is down, half-deployed or serving junk costs a slower download; it never
    costs a failed one.
    """
    if kind == "not json":
        state = _mirror_server(payload)
        state["routes"]["/models/org/m/manifest.json"] = b"<html>NoSuchKey</html>"
    else:
        state = _mirror_server(payload, **kwargs)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]


def test_a_mirror_that_drops_mid_download_lands_on_the_hub_path(base, monkeypatch,
                                                                tmp_path, payload,
                                                                capsys):
    """A good manifest and a blob that hangs up halfway.

    The bytes that landed stay on disk as a resumable part file for a LATER run,
    but this attempt hands the repo to hf — and says on stderr that it was the
    MIRROR that gave up, not the segmented Hub fetch, because the two fail for
    completely different reasons.
    """
    state = _mirror_server(payload, budget=60_000)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]
    assert "the model mirror of org/m unavailable" in capsys.readouterr().err


def test_a_manifest_promising_a_blob_the_mirror_does_not_hold(base, monkeypatch,
                                                              tmp_path, payload):
    """The manifest and the objects can disagree — a half-finished upload — and
    that is a 404 on the blob, mid-download."""
    state = _mirror_server(payload, blob=False)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]


def test_an_unmirrored_model_never_touches_the_mirror(base, monkeypatch, tmp_path,
                                                     payload):
    """The privacy rule at the level it matters: a model the supervisor did not
    permit is never NAMED to our distribution, so we cannot learn that this user
    downloaded it."""
    state = _mirror_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state, permitted=False)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]
    assert state["requests"] == [], "an unpermitted model was named to the mirror"


def test_the_documented_opt_out_leaves_every_download_exactly_where_it_was(
        base, monkeypatch, tmp_path, payload):
    """The explicit opt-out. `FUSED_MODEL_MIRROR` unset now means the shipped
    default (`https://render.fused.io/mirror`) — this pins the OTHER state, an
    operator (or a privacy-conscious user) setting it to `""`, which is the same
    "no mirror code in the path at all" as before this default flipped on."""
    state = _mirror_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "")
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]
    assert state["requests"] == []


def test_an_extra_argument_skips_the_mirror_outright(base, monkeypatch, tmp_path,
                                                     payload):
    """Same rule as the cached fast path: an argument this function does not know
    about changes what a download IS, and a manifest describes the whole repo at
    one commit and nothing else."""
    state = _mirror_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot(
        "org/m", revision="refs/pr/3") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]
    assert state["requests"] == []


def test_a_scoped_download_fetches_the_scope_off_the_mirror_too(base, monkeypatch,
                                                                tmp_path, payload):
    """`torch_image` passes an allow-list, so the mirror path has to honour it.

    Filtered with the same `selects` the Hub listing goes through — a manifest
    describes the whole repo, and fetching all of it behind a bar priced at the
    scope is the AI-5b trap the allow-list exists to avoid. The record is written
    at that scope too, or the next download re-fetches.
    """
    manifest = _mirror_manifest(payload)
    manifest["files"].append({"name": "unwanted.bin", "etag": "b" * 40,
                              "size": 999_999,
                              "sha256": hashlib.sha256(b"nope").hexdigest()})
    state = _mirror_server(payload, manifest=manifest)
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal(base, monkeypatch)

    snapshot = base.download_snapshot("org/m",
                                      allow_patterns=["model.safetensors"])

    assert sorted(os.listdir(snapshot)) == ["model.safetensors"]
    assert base._recorded_files(folder, MIRROR_COMMIT,
                                ["model.safetensors"], None) == [
        "model.safetensors"]
    assert not any("unwanted" in r["path"] for r in state["requests"])


def test_a_manifest_that_selects_nothing_at_this_scope_is_not_a_mirror_hit(
        base, monkeypatch, tmp_path, payload):
    """An allow-list matching nothing in the manifest means the mirror does not
    hold what was asked for. The Hub listing is the authority on that, not a
    manifest that happens to be missing a file."""
    state = _mirror_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot(
        "org/m", allow_patterns=["nothing-like-this/*"]) == (
        "/cache/snapshots/from-the-hub")
    assert fell_back == ["org/m"]


def test_a_cancel_during_a_mirror_fetch_is_never_swallowed(base, monkeypatch,
                                                           tmp_path, payload):
    """The one failure that must not be answered by starting a download
    somewhere else."""
    state = _mirror_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    started = []

    def cancelled(*args, **kwargs):
        raise base.Cancelled()

    monkeypatch.setattr(base, "_segmented_fetch", cancelled)
    monkeypatch.setattr(base, "_repo_files",
                        lambda *a, **k: started.append(1) or (None, []))
    _fake_hub(monkeypatch,
              snapshot_download=lambda *a, **k: started.append(1) or "/never")

    with pytest.raises(base.Cancelled):
        base.download_snapshot("org/m")

    assert started == [], "pressing Stop started a download instead"


# -- hashes, on the mirror path only ---------------------------------------------


def test_a_mirror_serving_one_wrong_byte_leaves_no_blob_behind(base, monkeypatch,
                                                               tmp_path, payload,
                                                               capsys):
    """The failure this check exists for is PERMANENT, not merely slow.

    A wrong blob filed under a real etag is served out of the hub cache forever —
    by hf's own loaders as much as ours — and no later download refetches it. So
    a mismatch must leave the cache exactly as it found it, and the repo goes to
    the Hub.
    """
    wrong = bytearray(payload)
    wrong[0] ^= 0xFF  # one byte, so nothing but the hash can notice
    state = _mirror_server(bytes(wrong), manifest=_mirror_manifest(payload))
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"], "the repo did not fall through to the Hub"
    assert os.listdir(os.path.join(folder, "blobs")) == [], "a bad blob was kept"
    assert not os.path.exists(os.path.join(folder, "snapshots", MIRROR_COMMIT,
                                           "model.safetensors"))
    assert "the mirror served" in capsys.readouterr().err


def test_a_mirror_serving_the_right_bytes_is_verified_once_per_file(base,
                                                                   monkeypatch,
                                                                   tmp_path,
                                                                   payload):
    """Once per FILE, not once per chunk.

    The segments write out of order, so there is no streaming hash to keep — the
    check is one read of the finished file. Four chunks and two files here, so a
    per-chunk implementation would show eight.
    """
    manifest = _mirror_manifest(payload)
    manifest["files"].append({"name": "second.safetensors", "etag": "cafe" * 10,
                              "size": len(payload),
                              "sha256": hashlib.sha256(payload).hexdigest()})
    state = _mirror_server(payload, manifest=manifest)
    state["routes"][f"/models/org/m/{MIRROR_COMMIT}/{'cafe' * 10}"] = payload
    _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal(base, monkeypatch)

    hashed = []
    real = base._blob_sha256
    monkeypatch.setattr(base, "_blob_sha256",
                        lambda path: hashed.append(path) or real(path))

    snapshot = base.download_snapshot("org/m")

    assert len(hashed) == 2, f"{len(hashed)} hashes for 2 files: {hashed}"
    assert len(_ranges(state["log"])) == 8, "not four chunks per file any more"
    for name in ("model.safetensors", "second.safetensors"):
        assert open(os.path.join(snapshot, name), "rb").read() == payload


def test_the_hub_path_is_not_hashed(base, monkeypatch, tmp_path, payload):
    """hf's own downloader does not hash either, and re-reading every gigabyte
    off the disk would give back a good part of what the segmented fetch is for.

    The Hub cannot supply a digest anyway — that asymmetry IS the gate, so this
    is what pins it rather than a flag somebody could set on both paths.
    """
    url, _state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    hashed = []
    monkeypatch.setattr(base, "_blob_sha256",
                        lambda path: hashed.append(path) or "")

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert hashed == [], "the Hub path paid for a hash it cannot check"


def test_a_verified_blob_already_on_disk_is_not_re_hashed(base, monkeypatch,
                                                          tmp_path, payload):
    """A blob the cache already holds cost nothing to obtain and is not re-read.

    `plan()` returns no work for it, so no bytes came off the mirror this run and
    there is nothing this check could be checking. Re-hashing it would put a
    multi-gigabyte read in front of a download that is already complete.
    """
    state = _mirror_server(payload)
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal(base, monkeypatch)
    os.makedirs(os.path.join(folder, "blobs"), exist_ok=True)
    with open(os.path.join(folder, "blobs", "beef" * 10), "wb") as handle:
        handle.write(payload)

    hashed = []
    monkeypatch.setattr(base, "_blob_sha256",
                        lambda path: hashed.append(path) or "")

    snapshot = base.download_snapshot("org/m")

    assert hashed == []
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert _ranges(state["log"]) == [], "bytes were fetched for a blob we had"


def test_a_mismatch_does_not_leave_bytes_a_later_run_would_resume_into(
        base, monkeypatch, tmp_path, payload):
    """The part file and its sidecar go too.

    Kept, the next run resumes into bytes already known to be wrong, hashes to
    the same mismatch, and does so forever — a download that can never succeed
    while the mirror keeps serving what it is serving.
    """
    wrong = bytearray(payload)
    wrong[-1] ^= 0xFF
    state = _mirror_server(bytes(wrong), manifest=_mirror_manifest(payload))
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_answers(base, monkeypatch, payload)

    base.download_snapshot("org/m")

    # Both halves, because either one alone passes for the wrong reason: an
    # empty `blobs/` is what says the mismatch was DETECTED, and no part file is
    # what says the rejected bytes are not waiting to be resumed into.
    assert os.listdir(os.path.join(folder, "blobs")) == []
    leftovers = [name for _d, _s, files in os.walk(folder) for name in files
                 if base.PART_SUFFIX in name]
    assert leftovers == [], leftovers


# -- the mirror path has no presigned URL to refresh (review finding 1) ----------


def _hub_meta_recorder(base, monkeypatch, state, payload, name="model.safetensors"):
    """Record `_hub_file_meta` calls, answering the way the HUB really would.

    Realistic on purpose. The etag, size and commit match, because they are what
    `_re_resolve`'s guard compares and a mismatching stub would abort for the
    wrong reason — and there is NO `sha256`, because the Hub has none. That
    absence is the bug this pins: it used to switch the mirror path's hash check
    off silently. And RECORDED rather than raised, because the retry loop catches
    a re-resolve's own exception by design, so a stub that raises makes the test
    pass while the request is still being made.
    """
    asked = []
    etag = _mirror_manifest(payload)["files"][0]["etag"]
    url = f"{state['origin']}/models/org/m/{MIRROR_COMMIT}/{etag}"

    def meta(repo_id, filename, revision):
        asked.append((repo_id, filename))
        return {"url": url, "location": url, "etag": etag,
                "commit": MIRROR_COMMIT, "size": len(payload)}

    monkeypatch.setattr(base, "_hub_file_meta", meta)
    return asked


def test_a_401_on_a_mirror_blob_is_retried_without_ever_asking_the_hub(base,
                                                                       monkeypatch,
                                                                       tmp_path,
                                                                       payload):
    """A 401/403 mid-download must not send us to huggingface.co.

    On the Hub path a 401 means the presigned CDN URL expired, and re-resolving
    is right. On the mirror path there IS no presigned URL — the blob URL is
    commit-pinned and immutable — so a re-resolve has nothing to refresh, and
    doing it anyway makes a request to the one host this whole feature exists to
    keep out of the conversation. A 403 from a CDN for a misconfigured object is
    the common case, not an exotic one.
    """
    state = _mirror_server(payload, unauthorized=1, plain_manifest=True)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)
    asked = _hub_meta_recorder(base, monkeypatch, state, payload)

    snapshot = base.download_snapshot("org/m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert asked == [], "a mirror download made a metadata call to the Hub"
    assert all(r["path"].startswith("/models/org/m/") for r in state["requests"])


def test_a_401_on_a_mirror_blob_never_costs_the_hash_check(base, monkeypatch,
                                                           tmp_path, payload):
    """…and the digest survives it, which is the half that was silent.

    A re-resolve replaced `self.meta` wholesale with the Hub's version, and Hub
    metadata has no `sha256` — so `finish()`'s gate went falsy and the
    verification simply disappeared, with the etag/size/commit guard still
    passing because a mirror etag IS an hf blob name. The check vanished exactly
    in the failure mode it exists for, and the resulting blob is published under
    a real etag forever.
    """
    state = _mirror_server(payload, unauthorized=1, plain_manifest=True)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)
    # A REALISTIC Hub answer, which is the whole point: same etag, same size,
    # same commit — every field the re-resolve guard checks — and no `sha256`,
    # because the Hub has none to give. A stub that raised instead would be
    # swallowed by the retry loop and this test would pass without the fix.
    asked = _hub_meta_recorder(base, monkeypatch, state, payload)
    hashed = []
    real = base._blob_sha256
    monkeypatch.setattr(base, "_blob_sha256",
                        lambda path: hashed.append(path) or real(path))

    base.download_snapshot("org/m")

    assert asked == [], "a mirror download made a metadata call to the Hub"
    assert len(hashed) == 1, "the 401 cost the file its hash check"


def test_a_mirror_blob_that_keeps_401ing_falls_back_rather_than_re_resolving(
        base, monkeypatch, tmp_path, payload):
    """Exhausting the retries hands the repo to hf, as any other failure does.

    What must not happen in between is a metadata call to the Hub — the segment
    loop's own retry budget is the whole answer here.
    """
    state = _mirror_server(payload, unauthorized=99, plain_manifest=True)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 2)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    # RECORDED, not raised. A raise here is swallowed by the retry loop's own
    # `except Exception as again`, so the test would pass while the request was
    # being made — the exact hole this finding is about.
    asked = _hub_meta_recorder(base, monkeypatch, state, payload)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]
    assert asked == [], "the Hub was asked to re-resolve a mirror blob"


def test_the_hub_path_still_re_resolves_an_expired_presigned_url(base, monkeypatch,
                                                                 tmp_path, payload):
    """The other side of the same rule, unchanged: a Hub fetch whose presigned
    URL expires mid-download still gets a fresh one, because there it really has
    expired and the budget must not be spent on 401s."""
    url, state = _start_server(payload, unauthorized=1)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)
    resolved = []
    monkeypatch.setattr(base, "_hub_file_meta", lambda repo, name, revision: (
        resolved.append(name) or {"url": url, "location": url, "etag": "e7ag",
                                  "commit": "c0m", "size": len(payload)}))

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert len(resolved) >= 2, "the Hub path stopped re-resolving"


def test_a_replaced_meta_cannot_erase_the_hash_check(base, tmp_path, payload):
    """The structural half of the same fix.

    Whether a blob is verified is decided ONCE, from the metadata the fetch was
    planned with, and is not re-read out of `self.meta` at publish time — so no
    later reassignment of that dict, for any reason anybody invents next, can
    turn the check off. `_re_resolve` is only the way it happened to happen.
    """
    url, _state = _start_server(payload)
    folder = str(tmp_path / "models--org--m")
    digest = hashlib.sha256(payload).hexdigest()
    fetch = base._FileFetch(
        folder, "org/m", "model.safetensors", "main",
        {"url": url, "location": url, "etag": "beef" * 10, "commit": "c0m",
         "size": len(payload), "sha256": digest},
        None, threading.Event())
    fetch.plan()
    for seg in fetch.segments:  # pretend every byte arrived, but write nothing
        seg["done"] = seg["end"] - seg["start"] + 1
    fetch.meta = dict(fetch.meta)
    del fetch.meta["sha256"]  # exactly what a re-resolve used to do

    with pytest.raises(RuntimeError, match="the mirror served"):
        fetch.finish()

    assert not os.path.exists(os.path.join(folder, "blobs", "beef" * 10))


# -- a mirror host that misbehaves at the HTTP level (review finding 2) ----------


def test_a_manifest_response_that_falls_apart_lands_on_the_hub_path(base,
                                                                    monkeypatch,
                                                                    tmp_path,
                                                                    payload):
    """`IncompleteRead` is an `HTTPException`, not an `OSError`.

    So it escaped the client's guard AND `_mirror_snapshot`'s, since the manifest
    was fetched before the `try` — and the download FAILED where the Hub could
    have served it. Both halves are fixed; this pins the outcome the docstrings
    always claimed.
    """
    state = _mirror_server(payload, break_first=1, break_bytes=4)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]


def test_a_mirror_client_that_raises_at_all_cannot_fail_the_download(base,
                                                                     monkeypatch,
                                                                     tmp_path,
                                                                     payload):
    """Belt and braces around the whole branch, not just the fetch.

    "Any failure falls back to the Hub" is the promise the feature is allowed to
    exist on, and it should not depend on having enumerated every exception a
    URL library can raise. So the manifest call, the filter and the fetch are all
    inside one guard.
    """
    state = _mirror_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers(base, monkeypatch, payload)
    def boom(model_id):
        raise RuntimeError("an exception nobody enumerated")

    monkeypatch.setattr(base._mirror_module(), "manifest", boom)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]


# -- ONE FILE off our own distribution, claiming nothing about the repo (AI-5m) --
#
# `download_snapshot` was the only decision point, and `llama_text.download`
# does not go through it: it fetches ONE GGUF with `download_file`, because a
# GGUF repo publishes dozens of quantizations (`unsloth/Qwen3.5-9B-GGUF` is
# 147.81GB whole for a 2.6GB file). Since llama.cpp became the only local text
# engine on Windows and Linux (D416) that left every suggested TEXT model on
# those platforms off the mirror entirely. Hence a second branch, in
# `download_file`, reading a manifest that lists one named file and makes no
# completeness claim — and writing no fetch record, which is what makes dropping
# that claim safe rather than convenient.

FILE_NAME = "q4.gguf"
FILE_MANIFEST_PATH = f"/models/org/m/files/{FILE_NAME}/manifest.json"


def _file_manifest(payload, name=FILE_NAME, **overrides):
    """A PER-FILE manifest: exactly one entry, and NO `complete` claim."""
    entry = {"name": name, "etag": "beef" * 10, "size": len(payload),
             "sha256": hashlib.sha256(payload).hexdigest()}
    manifest = {"schema": 1, "repo": "org/m", "commit": MIRROR_COMMIT,
                "files": [entry]}
    manifest.update(overrides)
    return manifest


def _mirror_file_server(payload, manifest=None, manifest_status=None, blob=True,
                        **flags):
    """A mirror serving one per-file manifest and its blob.

    Same shape as `_mirror_server` above — a status instead of the manifest for a
    file nobody mirrored or a distribution having a bad day, `blob=False` for a
    manifest promising bytes the mirror does not hold.
    """
    manifest = _file_manifest(payload) if manifest is None else manifest
    body = manifest_status if manifest_status else json.dumps(manifest).encode()
    routes = {FILE_MANIFEST_PATH: body}
    if blob and manifest.get("files"):
        etag = manifest["files"][0].get("etag", "")
        routes[f"/models/org/m/{manifest.get('commit')}/{etag}"] = payload
    _url, state = _start_server(b"", routes=routes, **flags)
    return state


def _no_cached_file(monkeypatch, **extra):
    """hf's read-only cache lookup, answering "not cached".

    `_cached_file` calls it on every `download_file`, and it is not a Hub
    request — it resolves a ref and a blob off the disk and cannot download — so
    a fatal wiring stubs it to a MISS rather than treating it as egress.
    """
    _fake_hub(monkeypatch, try_to_load_from_cache=lambda *a, **k: None, **extra)


def _hub_is_fatal_for_a_file(base, monkeypatch):
    """Everything `download_file` could ask huggingface.co, made fatal.

    Fatal rather than counted, for the reason the snapshot arm's twin gives: "no
    request to huggingface.co" is the property the feature exists for, and a path
    that listed the repo anyway would still produce the right file.
    """
    def boom(*args, **kwargs):
        raise AssertionError("the Hub was consulted for a mirrored file")

    monkeypatch.setattr(base, "_repo_files", boom)
    monkeypatch.setattr(base, "_hub_file_meta", boom)
    _no_cached_file(monkeypatch, hf_hub_download=boom, snapshot_download=boom,
                    HfApi=boom)


def _hub_answers_for_a_file(base, monkeypatch, payload):
    """The Hub's single-file path, wired to succeed — what every failure lands on."""
    fell_back = []

    def hf_hub_download(repo_id=None, filename=None, **kwargs):
        fell_back.append((repo_id, filename))
        return "/cache/blobs/from-the-hub"

    # No sha, so `download_file` takes hf's own downloader rather than the
    # segmented one — the arm a mirror failure has to reach.
    monkeypatch.setattr(base, "_repo_files",
                        lambda repo, include=None, allow=None, ignore=None,
                        revision="main": (None, [(include, len(payload))]))
    _no_cached_file(monkeypatch, hf_hub_download=hf_hub_download,
                    HfApi=lambda: types.SimpleNamespace(
                        model_info=lambda *a, **k: types.SimpleNamespace(
                            siblings=[])))
    return fell_back


def test_a_mirrored_gguf_is_fetched_without_the_hub_being_listed(base, monkeypatch,
                                                                 tmp_path, payload):
    """The feature for a one-file download: no request to the Hub, and an hf
    cache entry.

    The branch sits AFTER the cached-file fast path and BEFORE the listing,
    because a listing before it would defeat the whole point — the manifest
    request is meant to be the only metadata call a mirrored download makes.
    """
    state = _mirror_file_server(payload)
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal_for_a_file(base, monkeypatch)

    path = base.download_file("org/m", FILE_NAME)

    assert os.path.basename(path) == FILE_NAME
    assert open(path, "rb").read() == payload
    # hf's own layout, byte for byte: the blob under its etag and a snapshot
    # entry pointing at it, which is what every later load reads.
    assert os.listdir(os.path.join(folder, "blobs")) == ["beef" * 10]
    assert os.path.exists(os.path.join(folder, "snapshots", MIRROR_COMMIT,
                                       FILE_NAME))
    assert state["requests"][0]["path"] == FILE_MANIFEST_PATH
    assert all(r["path"] in (FILE_MANIFEST_PATH,
                             f"/models/org/m/{MIRROR_COMMIT}/{'beef' * 10}")
               for r in state["requests"])


def test_a_per_file_mirror_hit_writes_no_fetch_record(base, monkeypatch, tmp_path,
                                                      payload):
    """The reason the per-file manifest needs no completeness assertion.

    `download_file` has never written an AI-5k record and this branch does not
    start: one file is not a scope anybody can later be told is complete. So a
    manifest that is wrong about the repo cannot poison a later bring-up — there
    is no record for it to poison — and the assertion the per-repo reader
    demands would buy nothing here but 147.81GB of mirrored bytes.
    """
    state = _mirror_file_server(payload)
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal_for_a_file(base, monkeypatch)

    base.download_file("org/m", FILE_NAME)

    assert base._has_fetch_record(folder) is False
    assert [name for name in os.listdir(folder) if "fused-fetch" in name] == []


@pytest.mark.parametrize("kwargs, why", [
    ({"manifest_status": 404}, "a file nobody mirrored"),
    ({"manifest_status": 503}, "a distribution having a bad day"),
    ({"blob": False}, "a manifest promising bytes the mirror does not hold"),
    ({"budget": 60_000}, "a mirror that drops mid-download"),
])
def test_a_per_file_mirror_that_cannot_answer_lands_on_the_hub_path(base,
                                                                   monkeypatch,
                                                                   tmp_path,
                                                                   payload,
                                                                   kwargs, why):
    """The property the whole feature is allowed to exist on, one file wide: a
    mirror that is down costs a slower download and never a failed one."""
    state = _mirror_file_server(payload, **kwargs)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers_for_a_file(base, monkeypatch, payload)

    assert base.download_file("org/m", FILE_NAME) == "/cache/blobs/from-the-hub", why
    assert fell_back == [("org/m", FILE_NAME)], why


def test_a_per_file_mirror_serving_wrong_bytes_leaves_nothing_behind(base,
                                                                    monkeypatch,
                                                                    tmp_path,
                                                                    payload,
                                                                    capsys):
    """The sha256 is what makes a claim-free manifest safe: the worst a wrong one
    can do is serve the wrong bytes, and those never reach the cache."""
    wrong = bytearray(payload)
    wrong[0] ^= 0xFF  # one byte, so nothing but the hash can notice
    state = _mirror_file_server(bytes(wrong),
                               manifest=_file_manifest(payload))
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers_for_a_file(base, monkeypatch, payload)

    assert base.download_file("org/m", FILE_NAME) == "/cache/blobs/from-the-hub"
    assert fell_back == [("org/m", FILE_NAME)]
    assert os.listdir(os.path.join(folder, "blobs")) == [], "a bad blob was kept"
    assert "the mirror served" in capsys.readouterr().err


def test_an_unpermitted_file_is_never_named_to_the_mirror(base, monkeypatch,
                                                          tmp_path, payload):
    """A file out of a repo the user found themselves. The probe itself is what
    would tell us they downloaded it, so it is never made."""
    state = _mirror_file_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state, permitted=False)
    fell_back = _hub_answers_for_a_file(base, monkeypatch, payload)

    assert base.download_file("org/m", FILE_NAME) == "/cache/blobs/from-the-hub"
    assert fell_back == [("org/m", FILE_NAME)]
    assert state["requests"] == []


def test_the_documented_opt_out_leaves_a_one_file_download_where_it_was(base,
                                                                     monkeypatch,
                                                                     tmp_path,
                                                                     payload):
    """The explicit opt-out (`FUSED_MODEL_MIRROR=""`): no mirror code in the
    path at all. Unset alone no longer means this — see `mirror.DEFAULT_BASE`."""
    state = _mirror_file_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    monkeypatch.setenv("FUSED_MODEL_MIRROR", "")
    fell_back = _hub_answers_for_a_file(base, monkeypatch, payload)

    assert base.download_file("org/m", FILE_NAME) == "/cache/blobs/from-the-hub"
    assert fell_back == [("org/m", FILE_NAME)]
    assert state["requests"] == []


def test_a_cached_file_is_never_re_fetched_off_the_mirror(base, monkeypatch,
                                                          tmp_path, payload):
    """The fast path stays first. A file already on disk means no manifest
    request, which also keeps the access log honest: a manifest request means a
    download really started."""
    state = _mirror_file_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    already = tmp_path / "blobs" / "already-here"
    already.parent.mkdir(parents=True, exist_ok=True)
    already.write_bytes(payload)
    monkeypatch.setattr(base, "_cached_file", lambda repo, name: str(already))

    assert base.download_file("org/m", FILE_NAME) == str(already)
    assert state["requests"] == []


def test_a_cancel_during_a_per_file_mirror_fetch_is_never_swallowed(base,
                                                                   monkeypatch,
                                                                   tmp_path,
                                                                   payload):
    """The one failure that must not be answered by starting the download
    somewhere else (AI-5e)."""
    state = _mirror_file_server(payload)
    _mirror_wire(base, monkeypatch, tmp_path, state)
    started = []

    def cancelled(*args, **kwargs):
        raise base.Cancelled()

    monkeypatch.setattr(base, "_segmented_fetch", cancelled)
    monkeypatch.setattr(base, "_repo_files",
                        lambda *a, **k: started.append(1) or (None, []))
    _no_cached_file(monkeypatch,
                    hf_hub_download=lambda **k: started.append(1) or "/never")

    with pytest.raises(base.Cancelled):
        base.download_file("org/m", FILE_NAME)

    assert started == [], "pressing Stop started a download instead"


# -- an empty file is a real file (review finding 6) ------------------------------


def test_a_zero_byte_file_in_a_manifest_is_fetched_and_filed(base, monkeypatch,
                                                             tmp_path, payload):
    """Allowing size 0 in the manifest is only worth anything if the fetcher
    really handles it, so this drives the whole path rather than reasoning about
    `_chunks(0)`."""
    empty_etag = hashlib.sha256(b"").hexdigest()
    manifest = _mirror_manifest(payload)
    manifest["files"].append({"name": "empty.txt", "etag": empty_etag,
                              "size": 0, "sha256": empty_etag})
    state = _mirror_server(payload, manifest=manifest)
    state["routes"][f"/models/org/m/{MIRROR_COMMIT}/{empty_etag}"] = b""
    _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal(base, monkeypatch)

    snapshot = base.download_snapshot("org/m")

    assert open(os.path.join(snapshot, "empty.txt"), "rb").read() == b""
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


# -- a rate limit is a WAIT, not a failure ---------------------------------------
#
# A 429 from the Hub used to be indistinguishable from a broken link: it landed
# in the generic `HTTP <code>` branch, burned the segment's whole retry budget
# in about seven seconds of backoff, and handed the repo to hf's own
# `snapshot_download` — a slower download, and one nothing explained. These
# tests pin the three halves of the fix: the wait is honoured, the budget is not
# spent, and the row says what is happening (including the sign-in that raises
# the limit, and only when there is no token).


def _no_waiting(base, monkeypatch):
    """Record every throttle wait instead of serving it.

    The notice is captured WITH each wait, which is the only moment it is
    guaranteed to be readable: it is cleared the instant a segment makes
    progress, so a fetch that succeeded has (correctly) left nothing behind.
    """
    waits = []

    def recorded(stop, seconds):
        waits.append((seconds, base._throttle_detail()))

    monkeypatch.setattr(base, "_throttle_sleep", recorded)
    return waits


def _one_segment(base, monkeypatch, tmp_path, url, payload, **flags):
    """Wire a fetch that is exactly ONE segment, so the retry arithmetic is
    deterministic — with four chunks in flight, two throttles can land on two
    different segments and neither counter reaches two."""
    return _wire(base, monkeypatch, tmp_path, url, len(payload),
                 segment_min=len(payload) + 1, **flags)


def test_a_throttled_segment_waits_the_time_the_server_asked_for(base, monkeypatch,
                                                                 tmp_path, payload):
    """`Retry-After: 2` is a two-second wait, then the download carries on."""
    url, _state = _start_server(payload, throttle_first=1, retry_after=2)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert [seconds for seconds, _detail in waits] == [2.0]
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


def test_a_retry_after_that_is_a_date_is_parsed_as_one(base, monkeypatch, tmp_path,
                                                       payload):
    """The other form the RFC permits, and one the Hub really serves."""
    url, _state = _start_server(
        payload, throttle_first=1,
        retry_after=email.utils.formatdate(time.time() + 5, usegmt=True))
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    # Roughly, not exactly: the header carries whole seconds and the clock moves
    # between formatting it and reading it back.
    assert len(waits) == 1 and 3.0 <= waits[0][0] <= 6.0, waits


def test_a_throttle_with_no_retry_after_still_waits_and_retries(base, monkeypatch,
                                                                tmp_path, payload):
    """Nothing to honour is not nothing to do: it backs off on its own, doubling,
    rather than hammering a host that has just said it is over its limit."""
    url, _state = _start_server(payload, throttle_first=2, retry_after=None)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert [seconds for seconds, _detail in waits] == [base.RETRY_BACKOFF_S,
                                                       base.RETRY_BACKOFF_S * 2]
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


def test_a_server_named_wait_far_above_the_ceiling_is_clamped(base, monkeypatch,
                                                              tmp_path, payload):
    """An hour is a legal `Retry-After` and must not become an hour of silence —
    nor a `time.sleep` the ✕ cannot get through (`THROTTLE_WAIT_MAX_S`)."""
    url, _state = _start_server(payload, throttle_first=1, retry_after=3600)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert [seconds for seconds, _detail in waits] == [base.THROTTLE_WAIT_MAX_S]


def test_a_long_run_of_throttles_does_not_fall_back(base, monkeypatch, tmp_path,
                                                    payload):
    """THE regression this change exists to prevent.

    `SEGMENT_ATTEMPTS` is a claim about a file being unreachable, and a 429 is
    not that claim. Sharing the budget with it meant a throttled download gave
    up after five attempts and fell into `snapshot_download` — slower, and with
    the resumable state deleted on the way.

    A one-second reset each time, which is what the Hub names at the end of a
    window, so twenty of them is twenty seconds of intended wait — well inside
    `THROTTLE_TOTAL_MAX_S`, which is the bound that actually governs.
    """
    throttles = base.SEGMENT_ATTEMPTS * 4
    url, _state = _start_server(payload, throttle_first=throttles,
                               ratelimit='"resolvers";r=0;t=1')
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert len(waits) == throttles and set(w for w, _ in waits) == {1.0}
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


def test_a_host_that_throttles_forever_still_gives_up_eventually(base, monkeypatch,
                                                                 tmp_path, payload):
    """The allowance is generous, not infinite: a segment parked on a 429 for the
    life of the process holds a pool slot no other chunk can use."""
    url, _state = _start_server(payload, throttle_first=999, retry_after=1)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    monkeypatch.setattr(base, "THROTTLE_ATTEMPTS", 3)
    waits = _no_waiting(base, monkeypatch)

    with pytest.raises(RuntimeError, match="HTTP 429"):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert len(waits) == 3, "the attempt bound stopped bounding the requests"


def test_the_TOTAL_time_spent_throttled_is_what_is_bounded(base, monkeypatch,
                                                           tmp_path, payload):
    """The real guarantee, and the review finding that produced it: 60 attempts
    at a 60-second ceiling is an hour of one segment sitting still, which is
    precisely what the ceiling exists to prevent. The clock bounds it, and the
    last wait is trimmed to what is left of the budget rather than overshooting
    it."""
    url, _state = _start_server(payload, throttle_first=999,
                               ratelimit='"resolvers";r=0;t=60')
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    monkeypatch.setattr(base, "THROTTLE_TOTAL_MAX_S", 200.0)
    waits = _no_waiting(base, monkeypatch)

    with pytest.raises(RuntimeError, match="HTTP 429"):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    spent = sum(seconds for seconds, _detail in waits)
    assert spent == 200.0, waits
    assert len(waits) == 4  # 60 + 60 + 60 + the 20 that was left


def _headers(**fields):
    """Response headers shaped like a real one — `email.message.Message`, which
    is what `urllib` hands back and which looks up case-insensitively."""
    message = email.message.Message()
    for name, value in fields.items():
        message[name.replace("_", "-")] = str(value)
    return message


def _throttle_error(status=429, **fields):
    """The exception a throttled `urllib` request raises."""
    return urllib.error.HTTPError("https://huggingface.co/org/m/resolve/main/f",
                                  status, "Too Many Requests", _headers(**fields), None)


class _RequestsShapedError(Exception):
    """What `huggingface_hub` raises: the response beside the error, not in it.

    Not a `urllib.error.HTTPError`, and that is the point — the throttle logic
    has to read a status and headers off both shapes, and this file cannot import
    `requests` to build the real thing (nor should `worker_base` be able to).
    """

    def __init__(self, status, **fields):
        super().__init__(f"HTTP {status}")
        self.response = types.SimpleNamespace(status_code=status,
                                             headers=_headers(**fields))


def test_the_hubs_own_RateLimit_header_is_what_is_honoured(base):
    """The Hub does not send `Retry-After` at all. It rate-limits by request
    count over five-minute fixed windows and answers a 429 with
    `RateLimit: "resolvers";r=0;t=N` — `t` being the exact seconds left. Reading
    only `Retry-After` meant guessing a backoff while the real answer sat unread
    in the response, worse informed than hf's own fallback client."""
    assert base._throttle_wait_s(
        _throttle_error(RateLimit='"resolvers";r=0;t=42'), 1) == 42.0


def test_the_RateLimit_reset_wins_over_a_Retry_After(base):
    """Both present is not a conflict to split — the Hub's own header is the
    precise one, and `Retry-After` is the shape other hosts use."""
    assert base._throttle_wait_s(
        _throttle_error(RateLimit='"resolvers";r=0;t=30', Retry_After=5), 1) == 30.0


def test_the_bucket_that_is_actually_EXHAUSTED_names_the_wait(base):
    """Several buckets arrive in one header and only one of them is why we are
    being refused: `r=0`. Taking the wrong entry's `t` is a wait that has nothing
    to do with the limit that was hit."""
    header = '"api";r=100;t=280, "resolvers";r=0;t=17'
    assert base._throttle_wait_s(_throttle_error(RateLimit=header), 1) == 17.0


def test_a_RateLimit_header_it_cannot_read_falls_through(base):
    """Tolerant, not clever: a structured field whose parameters this does not
    understand must fall through to the next source rather than raise or invent a
    zero. Each of these has a `Retry-After` behind it, and each must reach it."""
    for header in ('"resolvers";r=0;t=abc', '"resolvers";r=0', '', 'nonsense',
                   '"resolvers";r=0;t=-5'):
        assert base._throttle_wait_s(
            _throttle_error(RateLimit=header, Retry_After=7), 1) == 7.0, header


def test_a_stated_wait_of_zero_still_waits(base):
    """`t=0` means "the window resets about now", and taken literally it turned
    the allowance into an immediate re-request loop against a host that had just
    said it was over its limit. Both spellings of zero fall through to the
    backoff, which is the floor."""
    assert base._throttle_wait_s(_throttle_error(RateLimit='"resolvers";r=0;t=0'),
                                 1) == base.RETRY_BACKOFF_S
    assert base._throttle_wait_s(_throttle_error(Retry_After=0), 3) == \
        base.RETRY_BACKOFF_S * 4


def test_a_throttle_is_recognised_however_the_client_raised_it(base):
    """The Hub calls go through `huggingface_hub`, which raises `requests`-shaped
    errors; our own requests go through `urllib`. One server, one 429, and the
    throttle logic reads both rather than existing twice."""
    assert base._is_throttled(_RequestsShapedError(429)) is True
    assert base._throttle_wait_s(
        _RequestsShapedError(429, RateLimit='"resolvers";r=0;t=12'), 1) == 12.0
    assert base._is_throttled(_RequestsShapedError(404)) is False
    # And anything carrying no status at all is not a throttle, which is what
    # lets `_throttled_retry` ask this of an arbitrary exception.
    assert base._is_throttled(OSError("connection reset")) is False


def test_progress_gives_the_throttle_allowance_back(base, monkeypatch):
    """A long download over a busy link is throttled in BURSTS, and the allowance
    is a claim about one burst.

    Without the reset, the 61st 429 of a healthy multi-hour download — an hour
    and several gigabytes after the 60th — became an ordinary fault and spent the
    segment's retry budget falling into the fallback. `tries` has always reset on
    the cursor moving; this is that rule one level up, and `_Throttle` is where
    both halves of it live.
    """
    monkeypatch.setattr(base, "THROTTLE_ATTEMPTS", 2)
    monkeypatch.setattr(base, "_throttle_sleep", lambda stop, seconds: None)
    error = _throttle_error(Retry_After=1)
    throttle = base._Throttle(hub=True)

    assert throttle.wait(error) and throttle.wait(error)
    assert throttle.wait(error) is False, "the burst allowance is not bounded"

    throttle.progressed()

    assert throttle.wait(error) is True, "bytes moved and the allowance stayed spent"


def test_a_rate_limited_METADATA_call_is_waited_out_too(base, monkeypatch,
                                                        tmp_path, payload):
    """Where a Hub 429 realistically lands.

    The Hub meters URLs containing a `/resolve/` segment; the ranged GETs go to
    the presigned CDN location, which has none. So the metadata call is the
    throttled one — and a 429 there raised straight out of `_segmented_fetch`,
    taking the whole repo into the fallback with none of this waiting or any of
    the disclosure, however patient the chunk loop was.
    """
    url, _state = _start_server(payload)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)
    real = base._hub_file_meta
    calls = []

    def limited(repo_id, filename, revision):
        calls.append(filename)
        if len(calls) < 3:
            raise _RequestsShapedError(429, RateLimit='"resolvers";r=0;t=9')
        return real(repo_id, filename, revision)

    monkeypatch.setattr(base, "_hub_file_meta", limited)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert [seconds for seconds, _detail in waits] == [9.0, 9.0]
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


def test_a_metadata_failure_that_is_NOT_a_throttle_is_not_retried(base, monkeypatch,
                                                                  tmp_path, payload):
    """The wrapper waits out rate limits and nothing else: a repo that is gone, a
    socket that broke, a manifest that will not parse all have their own
    handlers, and swallowing them into a retry loop would hide a real failure
    behind a minute of "waiting"."""
    url, _state = _start_server(payload)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)
    monkeypatch.setattr(base, "_hub_file_meta", lambda *a: (_ for _ in ()).throw(
        _RequestsShapedError(404)))

    with pytest.raises(Exception, match="HTTP 404"):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert waits == []


def test_a_503_is_only_a_throttle_when_it_carries_a_retry_after(base, monkeypatch,
                                                                tmp_path, payload):
    """A bare 503 is an overloaded or broken host, which is what the ordinary
    budget is for; one that names a wait is the server asking for it."""
    url, _state = _start_server(payload, throttle_first=1, throttle_status=503,
                                retry_after=2)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    waits = _no_waiting(base, monkeypatch)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert [seconds for seconds, _detail in waits] == [2.0]

    bare = _fresh_base()
    url, _state = _start_server(payload, throttle_first=999, throttle_status=503)
    _one_segment(bare, monkeypatch, tmp_path / "bare", url, payload)
    monkeypatch.setattr(bare, "RETRY_BACKOFF_S", 0)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        bare._segmented_fetch("org/m", ["model.safetensors"], "c0m")


def test_the_row_says_the_hub_is_limiting_the_download(base, monkeypatch, tmp_path,
                                                       payload):
    """Signed in, so there is no login to suggest: the row states the fact and
    the wait, and nothing else."""
    url, _state = _start_server(payload, throttle_first=1, retry_after=2)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    monkeypatch.setattr(base, "_hf_token", lambda: "hf_secret_token")
    waits = _no_waiting(base, monkeypatch)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert [detail for _seconds, detail in waits] == [
        "Hugging Face is limiting this download — waiting 2s"]


def test_the_row_offers_the_sign_in_only_when_there_is_no_token(base, monkeypatch,
                                                                tmp_path, payload):
    """The one action that raises the limit, said where the limit is felt — and
    with no part of any token in it."""
    url, _state = _start_server(payload, throttle_first=1, retry_after=2)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    monkeypatch.setattr(base, "_hf_token", lambda: None)
    waits = _no_waiting(base, monkeypatch)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    detail = waits[0][1]
    # "Preferences → AI" verbatim, the same place `hub_models.py` and
    # `discoverView.ts` send a rate-limited or refused reader: a message naming a
    # screen that does not hold the setting is a message that wastes a click.
    assert detail == ("Hugging Face is limiting this download — sign in to "
                      "Hugging Face in Preferences → AI for a higher limit")


def test_an_off_hub_throttle_names_neither_the_hub_nor_a_sign_in(base, monkeypatch,
                                                                 tmp_path, payload):
    """A 429 from whatever `FUSED_MODEL_MIRROR` names is not the Hub throttling
    the user, and "sign in to Hugging Face" would be advice about a host that is
    not involved. It is still a rate limit and still worth saying."""
    url, _state = _start_server(payload, throttle_first=1, retry_after=2)
    _one_segment(base, monkeypatch, tmp_path, url, payload)
    monkeypatch.setattr(base, "_hf_token", lambda: None)
    monkeypatch.setattr(base, "_hub_file_meta", lambda *a: (_ for _ in ()).throw(
        AssertionError("the Hub was consulted for a mirrored file")))
    meta, _calls = _provider(url, len(payload))
    waits = _no_waiting(base, monkeypatch)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m", meta=meta)

    detail = waits[0][1]
    assert "Hugging Face" not in detail and "sign in" not in detail
    assert "rate-limited" in detail and "waiting 2s" in detail


def test_the_notice_is_retired_once_bytes_move_again(base, monkeypatch, tmp_path,
                                                     payload):
    """A row that goes on saying "waiting" over a download that is running is the
    same defect as one that never said it, wearing the other sign."""
    url, _state = _start_server(payload, throttle_first=1, retry_after=0)
    _one_segment(base, monkeypatch, tmp_path, url, payload)

    base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert base._throttle_detail() is None


def test_the_job_row_prefers_the_throttle_notice_over_the_plain_detail(base,
                                                                      monkeypatch):
    """`fetch_with_progress`'s tick is the only channel to the row, and the
    segment threads cannot reach it. Without this the row said "Fetching
    weights…" through a wait it was never told about."""
    ticks = []
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 0)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: ticks.append(fields))
    monkeypatch.setattr(base, "_hf_token", lambda: "hf_secret_token")

    def call():
        base._note_throttle(4.0, hub=True)
        time.sleep(1.2)  # past the one-second tick, so a poll sees the notice
        return "/snap"

    base.fetch_with_progress("org/m", call, total=1024, detail="Fetching weights…")

    assert any(tick.get("detail") == "Hugging Face is limiting this download — waiting 4s"
               for tick in ticks), ticks
    base._clear_throttle()


def test_a_fetch_does_not_inherit_the_previous_fetch_s_notice(base, monkeypatch):
    """A resident worker fetches component models during requests, so the
    process global really can outlive the download that set it."""
    monkeypatch.setattr(base, "repo_folder", lambda model_id, repo_type="model": "/repo")
    monkeypatch.setattr(base, "bytes_on_disk", lambda folder: 0)
    monkeypatch.setattr(base, "report", lambda job=None, **fields: None)
    monkeypatch.setattr(base, "_hf_token", lambda: "hf_secret_token")
    base._note_throttle(4.0, hub=True)

    base.fetch_with_progress("org/m", lambda: "/snap", total=1024)

    assert base._throttle_detail() is None


def test_a_stop_during_a_throttle_wait_aborts_promptly(base):
    """The ✕ reaches a parked segment only through `stop`, and a single
    `time.sleep` of a minute would blunt it into a minute of "cancelling…"."""
    stop = threading.Event()
    threading.Timer(0.1, stop.set).start()

    started = time.monotonic()
    base._throttle_sleep(stop, 30.0)

    assert time.monotonic() - started < 5.0


# -- the guard above, asserted rather than assumed -------------------------------


def test_these_tests_cannot_reach_the_network():
    """`no_egress` is only worth having if it is really installed.

    Asserted in each file that relies on it, because the fixture is IMPORTED into
    two of them — and an import that silently stops resolving would disable the
    guard everywhere without a single test turning red.
    """
    with pytest.raises(AssertionError, match="tried to resolve"):
        socket.getaddrinfo("huggingface.co", 443)
    with pytest.raises(AssertionError, match="tried to reach"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("1.1.1.1", 80))


# -- Windows has no `os.pwrite`, and now has a mirror anyway ---------------------
#
# `test_without_pwrite_the_mirror_declines_and_the_hub_serves_the_repo` used to
# live here and asserted the opposite: the mirror declined on win32 because
# `_segmented_fetch` refused without `os.pwrite`, so every Windows acquisition
# went to the Hub and none of them reached our access logs. The transport now
# falls back to a single append-only stream instead of refusing, so that test is
# replaced by its inverse — the mirror SERVES the repo — in
# `tests/test_ai_hub_fetch_no_pwrite.py`, along with the rest of the no-pwrite
# path. It is a file of its own so that those tests escape the module-level skip
# above and run natively on Windows CI, which is the only place the real platform
# condition exists.
