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


def _start_server(payload, ranges=True, lie_after_probe=False, budget=None):
    """Serve `payload` over HTTP; return (url, state).

    `state["log"]` records the `Range` header of every request, which is what
    the resume test asserts on: "resumed" means the second run ASKED for only
    the missing bytes, not merely that it ended up with the right file.

    `ranges=False` is a server with no range support at all. `lie_after_probe`
    answers the probe with a 206 and then ignores `Range` on the real fetch —
    the shape that would scatter one body's bytes across four segment offsets
    if the writer did not check the status it got back. `budget` serves that
    many bytes and then hangs up mid-body, which is how a download gets
    interrupted without a signal.
    """
    state = {"log": [], "served": 0, "budget": budget, "lock": threading.Lock()}

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            header = self.headers.get("Range")
            with state["lock"]:
                state["log"].append(header)
            probe = header == "bytes=0-0"
            start, end, partial = 0, len(payload) - 1, False
            if header and ranges and (probe or not lie_after_probe):
                spec = header.split("=", 1)[1]
                first, _, last = spec.partition("-")
                start = int(first)
                end = int(last) if last else len(payload) - 1
                partial = True
            body = payload[start:end + 1]

            allowed = len(body)
            if state["budget"] is not None and not probe:
                with state["lock"]:
                    allowed = max(0, min(len(body), state["budget"] - state["served"]))
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

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"])

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

    base._segmented_fetch("org/m", ["config.json"])

    assert state["log"] == [None], state["log"]


# -- a server that will not play along ------------------------------------------


def test_a_server_that_ignores_ranges_still_produces_the_file(base, monkeypatch,
                                                              tmp_path, payload):
    """No range support is a property of some hosts, not an error. It costs the
    speed-up and nothing else."""
    url, state = _start_server(payload, ranges=False)
    _wire(base, monkeypatch, tmp_path, url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"])

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
        base._segmented_fetch("org/m", ["model.safetensors"])

    assert not os.path.exists(os.path.join(folder, "blobs", "e7ag")), (
        "a scattered body was published as a finished blob")
    for name in os.listdir(os.path.join(folder, "blobs")):
        assert os.path.getsize(os.path.join(folder, "blobs", name)) <= len(payload)


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
        base._segmented_fetch("org/m", ["model.safetensors"])

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

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"])

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    resumed = _offsets(state["log"])
    expected = sorted(s["start"] + s["done"] for s in recorded["segments"]
                      if s["start"] + s["done"] <= s["end"])
    assert resumed == expected, "the second run re-fetched bytes it already had"
    assert not os.path.exists(sidecar)
    assert not os.path.exists(part)


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

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"])

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


# -- the cache layout is the Hub's, not ours ------------------------------------


def test_the_cache_layout_is_the_one_huggingface_hub_reads(base, monkeypatch,
                                                           tmp_path, payload):
    """A cache only this code can read is a cache the libraries cannot load
    from — the download would "succeed" and `from_pretrained` would go back to
    the network. Blob by etag, snapshot by commit, relative symlink, refs."""
    url, _state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"])

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

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"])

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

    snapshot = base._segmented_fetch("org/m", ["a.safetensors", "b.safetensors"])

    for name in ("a.safetensors", "b.safetensors"):
        assert open(os.path.join(snapshot, name), "rb").read() == payload
        assert os.path.exists(os.path.join(folder, "blobs", "e-" + name))
