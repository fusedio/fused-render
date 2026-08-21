"""Recording fed by the page's own encoder (capture/_sink.py, SPEC CP-10).

This is the Windows and Linux recording path, and unlike the Apple one it is
testable in full: the bytes arrive over a WebSocket, so a test can BE the
browser. What is exercised here is everything that decides whether a recording
survives — the token on the socket, the ordering guarantee the `eos` handshake
buys, the ending a closed socket is, and the two ways this backend can lose a
recording without anything raising.

`_windows.py` and `_linux.py` add a native still on top of this, and the pure
parts of both (the monitor maths, the portal's reply) are at the bottom — those
run on every platform on purpose, because CI has no Windows box and no
compositor.
"""
import os
import time
import types

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fused_render import capture, jobs
from fused_render.capture import _linux, _sink, _windows
from fused_render.server import create_app

H = {"X-Fused": "1"}


def _probe():
    return {"client": True,
            "video": {"available": True, "granted": True, "reason": None},
            "audio": {"available": True, "granted": True, "reason": None},
            "systemAudio": {"available": True, "reason": None},
            "screenshot": {"available": True, "granted": True, "reason": None},
            "displays": [], "microphones": []}


@pytest.fixture()
def backend(monkeypatch):
    """The real `_sink`, with only a probe bolted on.

    Deliberately not a stub: the file writing, the token, the registry and the
    two failure clocks are the things under test, so the only thing faked is the
    platform probe that would otherwise need a desktop.
    """
    module = types.SimpleNamespace(
        probe=_probe, ext=_sink.ext, refuse=_sink.refuse,
        start_screen=_sink.start_screen, start_audio=_sink.start_audio,
        stop=_sink.stop, failure=_sink.failure, attach=_sink.attach,
        detach=_sink.detach)
    monkeypatch.setattr(capture, "_backend", lambda: module)
    jobs.reset()
    capture._sessions.clear()
    capture._finished.clear()
    _sink._sinks.clear()
    yield module
    capture._sessions.clear()
    _sink._sinks.clear()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    with TestClient(create_app(str(tmp_path))) as test_client:
        yield test_client


def _start(client, **body):
    body.setdefault("mode", "screen")
    res = client.post("/api/capture/start", json=body, headers=H)
    assert res.status_code == 200, res.json()
    return res.json()


def _settled(job_id, timeout=6.0):
    """The row once the watchdog has acted on it.

    Polled rather than slept on: what is being tested is that the watchdog
    reaches a conclusion at all, and a fixed sleep either makes the suite slow
    or makes it flaky.
    """
    deadline = time.monotonic() + timeout
    row = None
    while time.monotonic() < deadline:
        rows = [j for j in jobs.list_jobs() if j["id"] == job_id]
        row = rows[0] if rows else None
        if row and row["state"] in ("error", "done", "cancelled"):
            return row
        time.sleep(0.05)
    raise AssertionError(f"the row never settled: {row}")


def _stream_url(started):
    return (f"/api/capture/{started['id']}/stream"
            f"?token={started['streamToken']}")


# ------------------------------------------------------- what the reply carries


def test_the_start_reply_carries_what_the_page_needs_to_attach(backend, client):
    """`transport` and `streamToken` are on the WIRE and nowhere else.

    They exist so `runtime.js` can open the socket; the handle it builds for a
    page has neither, because CP-8 forbids a page branching on which backend
    served it. This test pins the wire half — the bridge half is guarded in
    tests/test_capture.py's spelling check.
    """
    started = _start(client)
    assert started["transport"] == "stream"
    assert started["streamToken"] and len(started["streamToken"]) > 20
    # THE START REPLY IS THE ONLY PLACE EITHER APPEARS. `GET /api/capture` is
    # unguarded — no `X-Fused` — so a token on its rows would hand every live
    # recording's bearer credential to anything that can make a GET, giving away
    # the guard the socket has instead of a header. `transport` on a `list()` row
    # would also be the `via` field CP-8 forbids.
    rows = client.get("/api/capture").json()["active"]
    assert rows and rows[0]["id"] == started["id"]
    for row in rows:
        assert "streamToken" not in row and "transport" not in row
    # The path is decided before a single byte exists, which is the whole
    # contract (CP-2) and is what makes `transcribe({path})` the next line.
    assert started["path"].endswith(".webm")
    assert os.path.exists(started["path"])


@pytest.mark.parametrize("mode,container,suffix", [
    ("screen", None, ".webm"),
    ("screen", "webm", ".webm"),
    ("screen", "mp4", ".mp4"),
    ("audio", None, ".webm"),
    # An mp4 holding only audio is a .m4a — the name every other tool expects.
    ("audio", "mp4", ".m4a"),
])
def test_the_container_the_page_names_decides_the_extension(backend, client,
                                                            mode, container,
                                                            suffix):
    body = {"mode": mode}
    if container:
        body["container"] = container
    assert _start(client, **body)["path"].endswith(suffix)


def test_a_container_this_server_cannot_store_is_refused_not_guessed(backend,
                                                                    client):
    res = client.post("/api/capture/start",
                      json={"mode": "screen", "container": "mkv"}, headers=H)
    assert res.status_code == 400
    assert "'container' must be" in res.json()["error"]


@pytest.mark.parametrize("body,expected", [
    ({"display": 2}, "browser's share picker"),
    ({"rect": [0, 0, 10, 10]}, "shares a whole screen"),
    ({"cursor": True}, "browser's decision"),
    ({"cursor": False}, "browser's decision"),
])
def test_what_the_share_picker_owns_is_refused_with_a_pointer(backend, client,
                                                             body, expected):
    """Refused, never ignored (AI-10/D319). All three describe a capture region
    and the user picks the region in the browser's own dialog, so honouring them
    is impossible — and silently dropping `display: 2` hands back a recording of
    the wrong monitor with nothing in the reply to explain it."""
    res = client.post("/api/capture/start", json={"mode": "screen", **body},
                      headers=H)
    assert res.status_code == 400
    assert expected in res.json()["error"]
    # Nothing was opened: the refusal is before the file and before the row.
    assert not _sink._sinks and not jobs.list_jobs()


def test_a_cursor_nobody_asked_for_is_not_a_refusal(backend, client):
    """`cursor` arrives RAW so this can tell "the caller asked" from "the caller
    said nothing" — otherwise every page passing the documented default would be
    refused."""
    assert _start(client)["id"]


def test_a_path_that_contradicts_the_container_is_refused(backend, client,
                                                          tmp_path):
    res = client.post("/api/capture/start",
                      json={"mode": "screen", "container": "webm",
                            "path": str(tmp_path / "clip.mov")}, headers=H)
    assert res.status_code == 400
    assert "would name a file holding something else" in res.json()["error"]


# ---------------------------------------------------------------- the socket


def test_chunks_land_in_the_file_and_stop_reports_it(backend, client):
    started = _start(client)
    with client.websocket_connect(_stream_url(started)) as ws:
        ws.send_bytes(b"header-and-first-cluster")
        ws.send_bytes(b"-second")
        # The handshake the stop request depends on: a reply here proves every
        # chunk before it was already appended, because frames on one socket are
        # ordered and the stop travels on a different connection.
        ws.send_text("eos")
        assert ws.receive_text() == "flushed"
        done = client.post(f"/api/capture/{started['id']}/stop",
                           headers=H).json()
    assert done["state"] == "stopped"
    assert done["bytes"] == len(b"header-and-first-cluster-second")
    with open(started["path"], "rb") as handle:
        assert handle.read() == b"header-and-first-cluster-second"
    row = next(j for j in jobs.list_jobs() if j["id"] == started["jobId"])
    assert row["state"] == "done"


def test_a_wrong_token_cannot_write_into_this_servers_file(backend, client):
    started = _start(client)
    url = f"/api/capture/{started['id']}/stream?token=guessed"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(url) as ws:
            ws.send_bytes(b"nope")
            ws.receive_text()
    with open(started["path"], "rb") as handle:
        assert handle.read() == b""


def test_a_stream_with_no_token_at_all_is_refused(backend, client):
    started = _start(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
                f"/api/capture/{started['id']}/stream") as ws:
            ws.receive_text()


def test_a_second_encoder_may_not_attach_to_one_recording(backend, client):
    """Two encoders appending to one file interleave two containers into
    something unplayable, so the honest failure is at the door."""
    started = _start(client)
    with client.websocket_connect(_stream_url(started)) as first:
        first.send_bytes(b"mine")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(_stream_url(started)) as second:
                second.receive_text()


def test_an_unknown_id_cannot_open_a_stream(backend, client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
                "/api/capture/nosuch/stream?token=x") as ws:
            ws.receive_text()


def test_a_page_on_another_origin_cannot_attach(backend, client):
    """The socket cannot carry `X-Fused` (a browser will not put a header on a
    handshake), so the guard is the per-recording token plus this."""
    started = _start(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
                _stream_url(started),
                headers={"origin": "https://evil.example"}) as ws:
            ws.receive_text()


def test_the_socket_closing_ends_the_recording_and_keeps_the_file(backend,
                                                                  client):
    """The ending with no macOS equivalent: the encoder lived in the page.

    A reload takes it away, so the recording is over — and the file is KEPT,
    because every container `MediaRecorder` writes is playable as written. What
    must not happen is a row still saying "recording" over a file nothing is
    writing.
    """
    started = _start(client)
    with client.websocket_connect(_stream_url(started)) as ws:
        ws.send_bytes(b"some-real-video")
        ws.send_text("eos")
        ws.receive_text()
    # No stop request was ever sent; the close was the ending.
    assert client.get("/api/capture").json()["active"] == []
    assert os.path.getsize(started["path"]) == len(b"some-real-video")
    row = next(j for j in jobs.list_jobs() if j["id"] == started["jobId"])
    assert row["state"] == "done"
    # And the page's stop, if it lands anyway, gets the same answer rather than
    # a 404 about its own recording.
    late = client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert late.status_code == 200 and late.json()["bytes"] == 15


def test_a_recording_that_received_nothing_is_an_error_not_an_empty_file(
        backend, client):
    """`stop` on a zero-byte recording must not hand back a path to nothing."""
    started = _start(client)
    done = client.post(f"/api/capture/{started['id']}/stop", headers=H).json()
    assert "no video was ever received" in done["error"]


def test_a_recording_past_the_byte_ceiling_stops_itself(backend, client,
                                                        monkeypatch):
    """The bytes are the page's to choose, so `maxSeconds` does not bound them.
    A page in a loop must hit a refusal rather than fill the disk."""
    monkeypatch.setattr(_sink, "MAX_BYTES", 8)
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = _start(client)
    with client.websocket_connect(_stream_url(started)) as ws:
        ws.send_bytes(b"12345678")
        ws.send_bytes(b"over the line")
        # The socket is closed from the server side the moment the ceiling is
        # hit, which is how the page learns to stop encoding.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    # Nothing past the ceiling reached the file, and the row says what happened
    # rather than ticking "Recording" for the rest of the cap.
    assert os.path.getsize(started["path"]) == 8
    row = _settled(started["jobId"])
    assert row["state"] == "error" and "GB" in row["message"]


# ------------------------------------------------- the two ways it can be lost


def test_a_page_that_never_attaches_does_not_tick_to_the_cap(backend, client,
                                                             monkeypatch):
    """A share dialog nobody answered is a job row over an empty file. Without
    this the row would say "Recording" for thirty minutes."""
    monkeypatch.setattr(_sink, "ATTACH_S", 0.0)
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = _start(client)
    row = _settled(started["jobId"])
    assert row["state"] == "error"
    assert "never opened its stream" in row["message"]
    assert capture.active() == []


def test_a_stream_that_goes_quiet_is_reported_not_waited_out(backend, client,
                                                             monkeypatch):
    monkeypatch.setattr(_sink, "STALL_S", 0.0)
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = _start(client)
    with client.websocket_connect(_stream_url(started)) as ws:
        ws.send_bytes(b"one chunk and then silence")
        row = _settled(started["jobId"])
    assert row["state"] == "error"
    assert "stopped sending" in row["message"]


def test_the_cap_still_stops_and_keeps(backend, client, monkeypatch):
    """Unchanged from the native path, and it has to be: a page can be closed
    mid-recording, and then the manager's ✕ — which DISCARDS — is the only
    control left."""
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = _start(client, maxSeconds=1)
    with client.websocket_connect(_stream_url(started)) as ws:
        ws.send_bytes(b"a second of video")
        row = _settled(started["jobId"], timeout=8.0)
    assert row["state"] == "done"
    assert capture.active() == []
    assert os.path.exists(started["path"])


def test_attaching_a_stream_where_there_is_none_is_a_sentence(monkeypatch):
    """On macOS the app records natively, so there is nothing to attach — and a
    page that tries gets a sentence rather than a 500 from a missing hook."""
    monkeypatch.setattr(capture, "_backend",
                        lambda: types.SimpleNamespace(probe=_probe))
    with pytest.raises(capture.CaptureError) as caught:
        capture.attach_stream("whatever", "token")
    assert "captured by the app itself" in str(caught.value)


# ------------------------------------------------ the pure parts of Windows


def test_a_display_of_none_means_every_screen():
    """A still of "the screen" on a two-monitor desk is both of them."""
    monitors = [
        {"id": 1, "x": 0, "y": 0, "width": 1920, "height": 1080, "main": True},
        {"id": 2, "x": 1920, "y": -200, "width": 2560, "height": 1440,
         "main": False},
    ]
    whole = _windows._pick(None, monitors)
    # The union, not the primary and not a bounding box of the sizes: monitor 2
    # starts 200px above the origin and ends 160px below monitor 1's bottom.
    assert (whole["x"], whole["y"]) == (0, -200)
    assert (whole["width"], whole["height"]) == (4480, 1440)
    assert _windows._pick(2, monitors)["width"] == 2560


def test_an_unknown_display_names_the_ones_that_exist():
    monitors = [{"id": 1, "x": 0, "y": 0, "width": 8, "height": 6,
                 "main": True}]
    with pytest.raises(capture.CaptureError) as caught:
        _windows._pick(7, monitors)
    assert "this machine has 1" in str(caught.value)
    with pytest.raises(capture.CaptureError):
        _windows._pick("left", monitors)


def test_a_rect_is_relative_to_the_chosen_monitor():
    """`rect` means the same thing on every backend — points inside the display
    the caller named — so the second monitor's own origin has to be added."""
    monitor = {"id": 2, "x": 1920, "y": -200, "width": 2560, "height": 1440,
               "main": False}
    assert _windows._region(monitor, None) == (1920, -200, 2560, 1440)
    assert _windows._region(monitor, (10, 20, 100, 50)) == (1930, -180, 100, 50)


def test_windows_refuses_only_what_the_sink_refuses():
    assert _windows.refuse("screenshot", {"display": 2, "cursor": True}) is None
    assert _windows.refuse("screen", {"display": 2}) is not None


# -------------------------------------------------- the pure parts of Linux


@pytest.mark.parametrize("uri,expected", [
    ("file:///tmp/shot.png", "/tmp/shot.png"),
    ("file://localhost/tmp/shot.png", "/tmp/shot.png"),
    # Percent-encoding is real: a screenshot lands under the user's own name.
    ("file:///home/a%20b/Screenshot%20from%202026.png",
     "/home/a b/Screenshot from 2026.png"),
    ("https://example.com/shot.png", None),
    ("file://otherhost/tmp/shot.png", None),
    ("", None),
])
def test_the_portals_file_uri_becomes_a_path(uri, expected):
    assert _linux._path_from_uri(uri) == expected


class _Variant:
    """dbus-fast's `Variant` as the portal hands it over — `.value` and nothing
    else is read, and the real class is a Linux-only dependency this suite runs
    without."""

    def __init__(self, value):
        self.value = value


def test_the_portals_three_endings_read_differently():
    assert _linux._uri_from_response(
        [0, {"uri": _Variant("file:///tmp/a.png")}]) == "file:///tmp/a.png"
    # A plain string is read the same way, so a bus binding that unwraps
    # variants itself needs no second code path.
    assert _linux._uri_from_response(
        [0, {"uri": "file:///tmp/b.png"}]) == "file:///tmp/b.png"
    with pytest.raises(RuntimeError) as cancelled:
        _linux._uri_from_response([1, {}])
    assert "cancelled or denied" in str(cancelled.value)
    with pytest.raises(RuntimeError) as other:
        _linux._uri_from_response([2, {}])
    assert "code 2" in str(other.value)
    # Success with nothing in it is still a failure: a screenshot nobody took
    # must not resolve with a path to nothing.
    with pytest.raises(RuntimeError):
        _linux._uri_from_response([0, {}])


def test_linux_refuses_the_two_options_the_portal_does_not_have():
    assert _linux.refuse("screenshot", {"display": None, "cursor": None}) is None
    assert "no client may" in _linux.refuse("screenshot", {"display": 1})
    assert "no such option" in _linux.refuse("screenshot", {"cursor": False})
    # And everything else defers to the shared sink.
    assert _linux.refuse("screen", {"rect": (0, 0, 1, 1)}) is not None


# ------------------------------------------------ the bridge, actually running


PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "_capture_bridge_probe.mjs")
RUNTIME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "fused_render", "static", "runtime.js")


def _probe():
    """Run `runtime.js`'s capture bridge against a DOM/media stub, via node."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:                                     # pragma: no cover
        pytest.skip("node is required to run the bridge")
    done = subprocess.run([node, PROBE, RUNTIME], capture_output=True,
                          text=True)
    assert done.returncode == 0, f"the probe crashed:\n{done.stderr[-3000:]}"
    out = json.loads(done.stdout)
    assert out["error"] is None, out["error"]
    return out


def test_the_streamed_start_does_things_in_the_only_safe_order():
    """The correctness of this path is almost entirely ORDER, and none of it is
    visible to Python — so the bridge is run for real (see the probe's header).

    Four orderings, each with a failure it prevents:
      * the share picker BEFORE the start request — a cancelled picker must not
        leave a job row over an empty file;
      * the socket BEFORE `recorder.start()` — a chunk produced before it is a
        hole in the middle of the container;
      * the last chunk BEFORE `eos`;
      * `eos` answered BEFORE the stop request, which travels on another
        connection and would otherwise close the file ahead of the tail.
    """
    order = [step for step in _probe()["order"] if step != "fetch GET /api/capture"]
    assert order == [
        "picker",
        "fetch POST /api/capture/start",
        "ws.open",
        "recorder.start:1000",
        "ws.chunk",
        "recorder.stop",
        "ws.chunk",
        "ws.eos",
        "fetch POST /api/capture/abc123/stop",
        "ws.close",
    ], order


def test_chunks_reach_the_socket_in_the_order_they_were_produced():
    """`Blob.arrayBuffer()` is async, so two chunks read in parallel can be sent
    out of order — and two swapped clusters are a corrupt container, not a
    glitch. The probe's blobs are 3 bytes then 1."""
    assert _probe()["chunks"] == [3, 1]


def test_the_handle_a_page_gets_names_no_backend():
    """CP-8, on the surface a page actually touches. `transport` and
    `streamToken` exist on the wire and must not survive into the handle, and
    `sources().client` must not survive into the payload."""
    out = _probe()
    assert out["handle"]["leaks"] == []
    assert out["sources"]["clientStripped"] is True
    assert out["handle"]["path"] == "/tmp/x.mp4"      # known before any frame
    assert out["stop"]["mime"] == "video/mp4"


def test_the_container_comes_from_what_the_browser_can_encode():
    """The stub supports mp4 only, so the bridge must ask for mp4 — the path in
    the reply is the server's, but the CHOICE is the browser's (CP-5)."""
    out = _probe()
    assert out["stop"]["path"].endswith(".mp4")


def test_a_microphone_with_no_label_yet_still_gets_a_name():
    """Chromium withholds device labels until the permission has been granted
    once. A page showing an empty string in a picker is worse than a placeholder,
    and this is a browser rule rather than something to fix."""
    mics = _probe()["sources"]["microphones"]
    assert [m["name"] for m in mics] == ["Microphone 1"]
    # And a camera is not a microphone.
    assert len(mics) == 1
