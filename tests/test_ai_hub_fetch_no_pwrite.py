"""The fetch on a platform with no `os.pwrite` — which is Windows (AI-5i).

Every test here runs on EVERY OS, deliberately, and that is why they are in a
file of their own rather than beside their siblings in `test_ai_hub_fetch.py`:
that module asserts the multi-segment layout (four bodies at four offsets), so
it carries a module-wide skip for a platform that cannot produce one. These
tests assert the opposite shape — ONE append-only stream — so on win32 they run
against the real platform condition, and on POSIX they run against the same
condition simulated by taking `os.pwrite` away. Windows CI is therefore the
proof rather than the exception.

The invariant under test is AI-5i's own: **a counted byte is a written byte.**
Segments need `os.pwrite` because they write OUT OF ORDER into a pre-sized
file, where nothing but an unbuffered positional write can promise that. A
single append-only stream meets the same promise by a different route — the
file's LENGTH is the progress, so there is nothing to buffer and nothing to
seek — which is why this is a second way of satisfying AI-5i rather than a
weakening of it. What these tests pin down is the places where the two routes
differ and a shared code path could quietly do the segmented thing: the part
file must not be pre-sized, a resume must append at the length it already has,
a body that ignores `Range` must rewind the FILE and not just the cursor, and a
sidecar written by the segmented path must never be appended into.

The harness — the misbehaving CDN, the `no_egress` guard, the mirror server —
is imported from `test_ai_hub_fetch` rather than copied, for the reason that
module's own imports give: a second set of CDN misbehaviours would be a second
set to keep in step with the first.
"""
import json
import os
import threading
import time

import pytest

from test_ai_hub_fetch import (MANIFEST_PATH, MIRROR_COMMIT, _hub_answers,
                               _hub_is_fatal, _mirror_manifest, _mirror_server,
                               _mirror_wire, _offsets, _ranges, _start_server,
                               _wire, base, no_egress,  # noqa: F401
                               payload)


@pytest.fixture(autouse=True)
def no_pwrite(monkeypatch):
    """The win32 condition, on whatever platform this is running.

    A no-op on Windows, where the attribute was never there — which is the
    point: the tests below do not care which of the two they got.
    """
    monkeypatch.delattr(os, "pwrite", raising=False)


def _part(folder):
    return os.path.join(folder, "blobs", "e7ag.fusedpart")


def _sidecar(folder):
    return _part(folder) + ".json"


# -- one stream, and a part file whose LENGTH is the progress --------------------


def test_a_file_is_fetched_on_one_append_only_stream(base, monkeypatch, tmp_path,
                                                    payload):
    """The whole of the no-pwrite path: the bytes still land, on one body.

    No `Range` on the wire at all — not even the one-byte probe, which exists to
    decide whether a file may be SPLIT and has nothing to decide when it cannot
    be. A file that is fetched sequentially from byte zero asks for the file.
    """
    url, state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert state["log"] == [None], "a single sequential fetch asked for a range"


def test_the_part_file_is_not_pre_sized_so_its_length_is_the_progress(
        base, monkeypatch, tmp_path, payload):
    """The mechanism the resume rests on, asserted directly.

    The segmented path `ftruncate`s the part file to its final size before a
    byte arrives — which is why AI-5b measures it by allocated BLOCKS and why
    publishing is gated on the cursors rather than on the length. Appending
    cannot do that: a pre-sized file has nowhere to append TO, and a length of
    200_000 with 60_000 bytes in it would make the next resume ask for byte
    200_000 of a file it has barely started.
    """
    url, state = _start_server(payload, budget=60_000)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    recorded = json.load(open(_sidecar(folder)))
    assert len(recorded["segments"]) == 1, "the file was split without pwrite"
    landed = recorded["segments"][0]["done"]
    assert 0 < landed < len(payload), recorded
    assert os.path.getsize(_part(folder)) == landed
    assert open(_part(folder), "rb").read() == payload[:landed]
    # …and the bar reads that, rather than a pre-sized 200_000 (AI-5b). The
    # sidecar's own few hundred bytes are counted too, as they always were.
    on_disk = base.bytes_on_disk(folder)
    assert landed <= on_disk < landed + 1_000, on_disk


def test_an_interrupted_fetch_resumes_by_appending_at_the_length_it_has(
        base, monkeypatch, tmp_path, payload):
    """Resume is a `Range` from the part file's current length.

    Asserted on the range the second run ASKED for, like its segmented sibling:
    a run that re-fetched from zero would produce a correct file and would still
    have thrown away the progress the sidecar exists to keep.
    """
    url, state = _start_server(payload, budget=60_000)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    landed = os.path.getsize(_part(folder))
    assert 0 < landed < len(payload)
    state["budget"] = None
    state["log"].clear()

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert _offsets(state["log"]) == [landed], "the resume re-fetched what it had"
    assert not os.path.exists(_part(folder))
    assert not os.path.exists(_sidecar(folder))


def test_a_server_that_ignores_range_on_a_resume_rewinds_the_FILE(base, monkeypatch,
                                                                 tmp_path, payload):
    """A 200 answering a ranged request rewinds the cursor to zero — and here
    that has to rewind the file with it.

    On the segmented path the cursor is the only thing to rewind: the writes are
    positional, so byte 0 of that body goes to offset 0 whatever the part file
    already holds. An append-only stream has no offsets, so a cursor rewound
    while the file keeps its 60_000 bytes writes the whole body AFTER them: the
    cursors then say 200_000 landed, `finish()` believes them, and a 260_000-byte
    blob is published under a real etag — the exact permanent failure AI-5i's
    three publishing rules exist to make impossible.
    """
    url, state = _start_server(payload, budget=60_000)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "SEGMENT_ATTEMPTS", 1)
    monkeypatch.setattr(base, "RETRY_BACKOFF_S", 0)

    with pytest.raises(Exception):
        base._segmented_fetch("org/m", ["model.safetensors"], "c0m")
    assert 0 < os.path.getsize(_part(folder)) < len(payload)

    state["budget"] = None
    state["ranges"] = False  # the CDN stopped honouring Range between the two runs

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    blob = os.path.join(folder, "blobs", "e7ag")
    assert os.path.getsize(blob) == len(payload), "the body was appended, not rewound"
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


def test_a_sidecar_from_the_SEGMENTED_path_is_never_appended_into(base, monkeypatch,
                                                                 tmp_path, payload):
    """The cross-mode resume, which is silently wrong if it is allowed.

    A part file written by segments is pre-sized and full of holes: length
    200_000, and the bytes present are wherever four connections happened to put
    them. Appending onto that — or trusting its recorded cursors — produces a
    blob of exactly the right length and partly wrong content. The layout check
    catches it because the plan derived here is ONE segment and the sidecar
    describes four, so the sidecar is discarded exactly like no sidecar at all
    and the file restarts whole.

    This is a real transition, not a hypothetical: a user's download is
    interrupted, they update to a build whose platform lost `os.pwrite`, or the
    same cache folder is shared over a mount by two machines.
    """
    url, state = _start_server(payload)
    folder = _wire(base, monkeypatch, tmp_path, url, len(payload))
    os.makedirs(os.path.join(folder, "blobs"), exist_ok=True)
    # A pre-sized file holding chunks 0 and 2, exactly as `os.pwrite` would
    # have left it, and the sidecar the segmented path would have written.
    with open(_part(folder), "wb") as handle:
        handle.truncate(len(payload))
        handle.seek(0)
        handle.write(payload[:50_000])
        handle.seek(100_000)
        handle.write(payload[100_000:150_000])
    json.dump({"version": base.SIDECAR_VERSION, "etag": "e7ag",
               "size": len(payload),
               "segments": [{"start": 0, "end": 49_999, "done": 50_000},
                            {"start": 50_000, "end": 99_999, "done": 0},
                            {"start": 100_000, "end": 149_999, "done": 50_000},
                            {"start": 150_000, "end": 199_999, "done": 0}]},
              open(_sidecar(folder), "w"))

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert _ranges(state["log"]) == [], "it resumed into a segmented part file"


def test_a_filesystem_that_cannot_hold_a_sparse_file_is_no_longer_a_reason_to_fall_back(
        base, monkeypatch, tmp_path, payload):
    """The sparse requirement belongs to the pre-sized file, and there isn't one.

    A cache filesystem that ALLOCATES what `ftruncate` promises would turn a
    4.6GB download into 4.6GB of zeroes on disk before the first byte, which is
    why the segmented path refuses one. Appending never pre-sizes anything, so
    that reason does not apply here — and refusing anyway would take the whole
    feature off exactly the platform this change exists for.
    """
    url, _state = _start_server(payload)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    monkeypatch.setattr(base, "_sparse_ok", lambda folder: False)

    snapshot = base._segmented_fetch("org/m", ["model.safetensors"], "c0m")

    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload


# -- several files still move at once ------------------------------------------


def test_the_serialization_is_per_file_not_repo_wide(base, monkeypatch, tmp_path,
                                                     payload):
    """Only WITHIN a file does segmentation need `os.pwrite`. Across files there
    is nothing to write out of order, so a repo of shards still fetches on
    `MAX_CONNECTIONS` streams — one per file rather than one overall.

    Asserted by observing two bodies open at once: the first real request blocks
    until it is released, and a repo-wide serialization would deadlock this test
    on the second file rather than fetch it.
    """
    url, state = _start_server(payload, hold_first_real=True)
    _wire(base, monkeypatch, tmp_path, url, len(payload))
    metas = {"a.safetensors": "e7ag", "b.safetensors": "b10b"}
    monkeypatch.setattr(base, "_hub_file_meta",
                        lambda repo, name, revision: {
                            "url": url, "location": url, "etag": metas[name],
                            "commit": "c0m", "size": len(payload)})

    done = []
    thread = threading.Thread(
        target=lambda: done.append(base._segmented_fetch(
            "org/m", ["a.safetensors", "b.safetensors"], "c0m")))
    thread.start()
    # Two real requests in flight while the first is still held: the second file
    # got its own connection.
    deadline = time.monotonic() + 10.0
    while state["real"] < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert state["real"] >= 2, "the second file waited for the first to finish"
    state["release"].set()
    thread.join(timeout=30.0)
    assert done, "the fetch did not finish"
    for name in metas:
        assert open(os.path.join(done[0], name), "rb").read() == payload


# -- the mirror, which is the reason this matters -------------------------------


def test_the_mirror_serves_the_repo_without_pwrite(base, monkeypatch, tmp_path,
                                                   payload):
    """The inverse of what this file replaces.

    `test_without_pwrite_the_mirror_declines_and_the_hub_serves_the_repo` used to
    assert that a win32 client made the one manifest request and then took the
    Hub path. That was the standing limitation of AI-5l — Windows model
    acquisitions were invisible to the access logs, which defeats the counting
    the feature exists for — and the append-only transport is what removes it.
    The Hub is FATAL here: a mirrored model on a platform without `os.pwrite`
    must now be served by the mirror, end to end, in hf's own cache layout.
    """
    state = _mirror_server(payload)
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    _hub_is_fatal(base, monkeypatch)

    snapshot = base.download_snapshot("org/m")

    assert snapshot == os.path.join(folder, "snapshots", MIRROR_COMMIT)
    assert open(os.path.join(snapshot, "model.safetensors"), "rb").read() == payload
    assert open(os.path.join(folder, "refs", "main")).read() == MIRROR_COMMIT
    paths = [r["path"] for r in state["requests"]]
    assert paths[0] == MANIFEST_PATH and paths.count(MANIFEST_PATH) == 1
    etag = "beef" * 10
    assert all(p == f"/models/org/m/{MIRROR_COMMIT}/{etag}" for p in paths[1:]), paths
    # One stream for the file, and no credential offered to that host.
    assert _ranges(state["log"]) == []
    assert [r["auth"] for r in state["requests"]] == [None] * len(paths)


def test_the_hash_check_still_guards_the_blob_without_pwrite(base, monkeypatch,
                                                            tmp_path, payload,
                                                            capsys):
    """Hashing is on the part file before `os.replace`, on either transport.

    The append path changes WHERE the bytes came from and nothing about what is
    published: a wrong blob under a real etag is permanent, so the mismatch has
    to leave the cache exactly as it found it and take the repo to the Hub.
    """
    wrong = bytearray(payload)
    wrong[0] ^= 0xFF  # one byte, so nothing but the hash can notice
    state = _mirror_server(bytes(wrong), manifest=_mirror_manifest(payload))
    folder = _mirror_wire(base, monkeypatch, tmp_path, state)
    fell_back = _hub_answers(base, monkeypatch, payload)

    assert base.download_snapshot("org/m") == "/cache/snapshots/from-the-hub"
    assert fell_back == ["org/m"]
    assert os.listdir(os.path.join(folder, "blobs")) == [], "a bad blob was kept"
    assert "the mirror served" in capsys.readouterr().err
