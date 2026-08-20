"""Native capture (fused_render/capture/ + routers/capture.py, SPEC §44).

The Apple half cannot run in the suite — it needs a Mac, a granted TCC
permission and a real display — so the backend is STUBBED here and what is
tested is everything that decides whether a recording behaves: the arguments
that must be refused before a microphone turns on, where the file lands, the job
row a recording appears as, and the three ways a recording can end (the page's
`stop`, the manager's ✕, and the cap).

The two endings are the point of most of this file. ✕ DISCARDS, like every other
row in the manager — and the cap STOPS AND KEEPS, because a page can be closed
mid-recording and then the ✕ is the only control left, so if the cap discarded
too there would be no ending that kept the file.
"""
import os
import sys
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import capture, jobs
from fused_render.server import create_app

H = {"X-Fused": "1"}


class FakeHandle:
    def __init__(self, path):
        self.path = path
        self.stopped = False


class FakeBackend:
    """Writes bytes where the real one writes a movie. Nothing else differs."""

    def __init__(self):
        self.handles = []

    def probe(self):
        return {"video": {"available": True, "granted": True, "reason": None},
                "audio": {"available": True, "granted": True, "reason": None},
                "systemAudio": {"available": True, "reason": None},
                "screenshot": {"available": True, "granted": True,
                               "reason": None},
                "displays": [{"id": 1, "width": 100, "height": 80,
                              "main": True}],
                "microphones": [{"id": "mic0", "name": "Fake", "default": True}]}

    def _start(self, out, spec):
        with open(out, "wb") as fh:
            fh.write(b"recording")
        handle = FakeHandle(out)
        self.handles.append(handle)
        return handle

    start_screen = _start
    start_audio = _start

    def stop(self, handle):
        handle.stopped = True

    def screenshot(self, out, spec):
        with open(out, "wb") as fh:
            fh.write(b"png")
        return {"width": 4, "height": 2}


@pytest.fixture()
def backend(monkeypatch):
    fake = FakeBackend()
    monkeypatch.setattr(capture, "_backend", lambda: fake)
    jobs.reset()
    capture._sessions.clear()
    yield fake
    for cid in list(capture._sessions):
        capture._sessions.pop(cid, None)
    jobs.reset()


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """`<home>/recordings` is where an unnamed recording lands."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return tmp_path


# ------------------------------------------------------- the honest refusals


def test_a_machine_that_cannot_capture_says_so_rather_than_raising(monkeypatch):
    """`sources()` answers on EVERY platform — a page must be able to ask."""
    monkeypatch.setattr(capture.sys, "platform", "win32")
    payload = capture.sources()
    assert payload["video"]["available"] is False
    assert payload["audio"]["available"] is False
    assert "macOS" in payload["video"]["reason"]
    # Shape-identical to the real probe, EVERY key included — a page reading
    # `sources().screenshot.available` must not throw where the answer is "no".
    assert payload["displays"] == [] and payload["microphones"] == []
    assert set(payload) == set(FakeBackend().probe())
    for key in ("video", "audio", "systemAudio", "screenshot"):
        assert payload[key]["available"] is False
        assert payload[key]["reason"]


def test_starting_on_an_unsupported_platform_is_a_409_not_a_500(monkeypatch,
                                                               client):
    monkeypatch.setattr(capture.sys, "platform", "linux")
    res = client.post("/api/capture/start", json={"mode": "screen"}, headers=H)
    assert res.status_code == 409
    assert "macOS" in res.json()["error"]


@pytest.mark.parametrize("body,expected", [
    ({"mode": "sideways"}, "mode must be"),
    ({"mode": "screen", "audio": "microphone"}, "'audio' must be false"),
    ({"mode": "screen", "rect": [0, 0, 0, 10]}, "must be positive"),
    ({"mode": "screen", "rect": [0, 0, 10]}, "'rect' must be"),
    ({"mode": "screen", "maxSeconds": 0}, "must be between"),
    ({"mode": "screen", "maxSeconds": "soon"}, "whole number"),
    ({"mode": "audio", "source": False}, "'source' must be"),
    ({"mode": "audio", "source": "system"}, "record the screen"),
    ({"mode": "audio", "device": "mic0"}, "cannot be chosen here"),
    ({"mode": "screen", "path": "clip.mov"}, "must be absolute"),
])
def test_a_request_a_typo_deserves_an_answer_about_is_refused_before_recording(
        backend, client, body, expected):
    res = client.post("/api/capture/start", json=body, headers=H)
    assert res.status_code == 400, res.json()
    assert expected in res.json()["error"]
    # Nothing was started and no row appeared: the refusal is BEFORE the device.
    assert backend.handles == []
    assert jobs.list_jobs() == []


def test_the_mutating_routes_carry_the_x_fused_guard(client):
    for path in ("/api/capture/start", "/api/capture/screenshot",
                 "/api/capture/abc/stop", "/api/capture/abc/cancel"):
        assert client.post(path, json={}).status_code == 403, path
    # …and the read is open, like the other read-only routes.
    assert client.get("/api/capture").status_code == 200


def test_audio_only_refuses_a_device_naming_where_it_does_work(backend, client):
    """Refused, not ignored: a silently-wrong mic is a recording made twice."""
    res = client.post("/api/capture/start",
                      json={"mode": "audio", "device": "mic0"}, headers=H)
    assert "audio: 'mic'" in res.json()["error"]
    # The same option IS accepted on a screen recording, which is the point.
    assert client.post("/api/capture/start",
                       json={"mode": "screen", "audio": "mic",
                             "device": "mic0"}, headers=H).status_code == 200


# ------------------------------------------------------------ where it lands


def test_an_unnamed_recording_lands_in_the_app_home(backend, client, home):
    started = client.post("/api/capture/start", json={"mode": "audio"},
                          headers=H).json()
    assert started["path"].startswith(str(home / "home" / "recordings"))
    assert started["path"].endswith(".m4a")
    assert os.path.exists(started["path"])


def test_a_relative_path_resolves_beside_the_calling_page(backend, client,
                                                          tmp_path):
    page = tmp_path / "views" / "recorder.html"
    page.parent.mkdir(parents=True)
    page.write_text("<p>", encoding="utf-8")
    started = client.post("/api/capture/start",
                          json={"mode": "screen", "path": "demo.mov",
                                "base": str(page)}, headers=H).json()
    assert started["path"] == str(page.parent / "demo.mov")


def test_the_path_is_known_before_the_recording_ends(backend, client, home):
    """The whole reason this is native: `path` is on the START reply."""
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    assert started["state"] == "recording"
    stopped = client.post(f"/api/capture/{started['id']}/stop",
                          headers=H).json()
    assert stopped["path"] == started["path"]
    assert stopped["url"].startswith("/api/fs/raw?path=")
    assert stopped["mime"] == "video/quicktime"
    assert stopped["bytes"] == len(b"recording")


def test_a_missing_directory_is_refused_rather_than_half_recorded(backend,
                                                                  client,
                                                                  tmp_path):
    res = client.post("/api/capture/start",
                      json={"mode": "audio",
                            "path": str(tmp_path / "nope" / "a.m4a")},
                      headers=H)
    assert res.status_code == 400
    assert "no such directory" in res.json()["error"]


# ---------------------------------------------------------------- the endings


def test_a_recording_is_a_server_owned_job_row_the_manager_can_stop(backend,
                                                                    client,
                                                                    home):
    started = client.post("/api/capture/start",
                          json={"mode": "screen", "title": "Walkthrough"},
                          headers=H).json()
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
               if j["id"] == started["jobId"])
    assert row["title"] == "Walkthrough"
    assert row["owner"] == "server"      # so the ✕ really stops it
    assert row["cancellable"] is True
    assert row["unit"] == "s" and row["total"] == started["maxSeconds"]
    assert "discards" in row["detail"]   # the ✕'s meaning, in words
    assert client.get("/api/capture").json()["active"][0]["id"] == started["id"]


def test_stop_keeps_the_file_and_finishes_the_row(backend, client, home):
    started = client.post("/api/capture/start", json={"mode": "audio"},
                          headers=H).json()
    client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert os.path.exists(started["path"])
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
               if j["id"] == started["jobId"])
    assert row["state"] == "done"
    assert backend.handles[0].stopped is True
    # Live-only: a finished recording is a file, not a registry entry.
    assert client.get("/api/capture").json()["active"] == []


def test_cancel_deletes_the_file(backend, client, home):
    started = client.post("/api/capture/start", json={"mode": "audio"},
                          headers=H).json()
    gone = client.post(f"/api/capture/{started['id']}/cancel",
                       headers=H).json()
    assert not os.path.exists(started["path"])
    # No path handed back for a file that no longer exists.
    assert gone["path"] is None and gone["url"] is None
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
               if j["id"] == started["jobId"])
    assert row["state"] == "cancelled"


def test_stopping_the_same_recording_twice_is_a_404_not_a_second_stop(backend,
                                                                     client,
                                                                     home):
    """The registry entry is taken under the lock before the device is touched,
    so a ✕ landing at the same moment as the page's own stop cannot finalise
    (or delete) the file twice."""
    started = client.post("/api/capture/start", json={"mode": "audio"},
                          headers=H).json()
    assert client.post(f"/api/capture/{started['id']}/stop",
                       headers=H).status_code == 200
    assert client.post(f"/api/capture/{started['id']}/stop",
                       headers=H).status_code == 404
    assert client.post(f"/api/capture/{started['id']}/cancel",
                       headers=H).status_code == 404


def test_the_managers_cross_discards_the_recording(backend, client, home,
                                                   monkeypatch):
    """The ✕ sets `cancel_requested`; the watchdog is what acts on it."""
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = client.post("/api/capture/start", json={"mode": "audio"},
                          headers=H).json()
    client.post(f"/api/jobs/{started['jobId']}/cancel", headers=H)
    deadline = time.monotonic() + 5
    while os.path.exists(started["path"]) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(started["path"])
    assert capture.active() == []


def test_hitting_max_seconds_stops_and_keeps_the_file(backend, client, home,
                                                      monkeypatch):
    """The cap is the one automatic ending, and it is a STOP: for a recording
    whose page was closed it is the only ending that does not destroy it."""
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    monkeypatch.setattr(capture, "_max_seconds", lambda value: 0.2)
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    deadline = time.monotonic() + 5
    while capture.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert capture.active() == []
    assert os.path.exists(started["path"])
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
               if j["id"] == started["jobId"])
    assert row["state"] == "done"


def test_stop_all_finalises_everything_on_the_way_out(backend, client, home):
    """A truncated .mov has no moov atom — an exiting server must not leave one."""
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    capture.stop_all()
    assert backend.handles[0].stopped is True
    assert os.path.exists(started["path"])
    assert capture.active() == []


# --------------------------------------------------------------- screenshots


def test_a_screenshot_is_a_file_and_no_job_row(backend, client, home):
    shot = client.post("/api/capture/screenshot", json={"format": "png"},
                       headers=H).json()
    assert shot["path"].endswith(".png") and shot["mime"] == "image/png"
    assert shot["width"] == 4 and shot["height"] == 2
    assert jobs.list_jobs() == []            # milliseconds need no row


def test_screenshot_refuses_a_format_it_cannot_write(backend, client):
    res = client.post("/api/capture/screenshot", json={"format": "gif"},
                      headers=H)
    assert res.status_code == 400 and "'png' or 'jpg'" in res.json()["error"]


# ------------------------------------------------------------- the bridge doc


def test_the_runtime_bridge_exposes_capture():
    """`window.fused.capture` is the contract; this is its spelling guard."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    source = open(os.path.join(root, "fused_render", "static", "runtime.js"),
                  encoding="utf-8").read()
    assert "\n    capture,\n" in source          # on the window.fused literal
    for method in ("screen:", "audio:", "screenshot:", "sources:", "list:",
                   "attach:"):
        assert method in source, method
    # The header block is the documented public bridge (EXPORT.md) — a method
    # that is not described there is not a contract anybody can rely on.
    assert "fused.capture.* -> record the screen, record the mic, grab a still" \
        in source


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_the_macos_backend_imports_and_probes_without_prompting():
    """Import + probe only: it must not need a display, a grant, or a click."""
    from fused_render.capture import _darwin

    payload = _darwin.probe()
    for key in ("video", "audio", "systemAudio", "screenshot"):
        assert set(payload[key]) >= {"available", "reason"}
    assert isinstance(payload["displays"], list)
    assert isinstance(payload["microphones"], list)
