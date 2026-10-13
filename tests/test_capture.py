"""Native capture (fused_render/capture/ + routers/capture.py, SPEC §45).

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
import builtins
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
        self.error = None
        self.paused = False


class FakeBackend:
    """Writes bytes where the real one writes a movie. Nothing else differs."""

    def __init__(self):
        self.handles = []
        self.paused = []
        self.pausable = True
        self.lvl = 0.0

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

    def ext(self, mode, spec):
        return ".mov" if mode == "screen" else ".m4a"

    def refuse(self, mode, spec):
        # The real macOS backend's one refusal, kept here because the neutral
        # half's plumbing for it is what these tests exercise.
        if mode == "audio" and spec.get("device"):
            return ("audio-only recording uses the system's current input "
                    "device, so 'device' cannot be chosen here — record the "
                    "screen with audio: 'mic' to pick a specific microphone")
        return None

    def stop(self, handle):
        handle.stopped = True

    def can_pause(self, handle):
        return self.pausable

    def pause(self, handle):
        handle.paused = True
        self.paused.append(("pause", handle))

    def resume(self, handle):
        handle.paused = False
        self.paused.append(("resume", handle))

    def level(self, handle):
        if isinstance(self.lvl, Exception):
            raise self.lvl
        return self.lvl

    #: What `warning(handle)` answers; the real hook is the macOS tap's.
    warn = None

    def warning(self, handle):
        return self.warn

    def failure(self, handle):
        return handle.error

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
    """`sources()` answers on EVERY platform — a page must be able to ask.

    Three platforms have a backend now, so the machine with none is something
    else entirely (a BSD, an unknown `sys.platform`). What is being tested is
    unchanged and is the promise CP-8 makes: an answer, shaped like every other
    answer, with a reason in it.
    """
    monkeypatch.setattr(capture.sys, "platform", "sunos5")
    payload = capture.sources()
    assert payload["video"]["available"] is False
    assert payload["audio"]["available"] is False
    assert "sunos5" in payload["video"]["reason"]
    # Shape-identical to the real probe, EVERY key included — a page reading
    # `sources().screenshot.available` must not throw where the answer is "no".
    assert payload["displays"] == [] and payload["microphones"] == []
    assert set(payload) == set(FakeBackend().probe())
    for key in ("video", "audio", "systemAudio", "screenshot"):
        assert payload[key]["available"] is False
        assert payload[key]["reason"]


def test_starting_on_an_unsupported_platform_is_a_409_not_a_500(monkeypatch,
                                                               client):
    monkeypatch.setattr(capture.sys, "platform", "aix7")
    res = client.post("/api/capture/start", json={"mode": "screen"}, headers=H)
    assert res.status_code == 409
    assert "aix7" in res.json()["error"]


def test_a_backend_that_will_not_import_is_a_reason_not_a_500(monkeypatch,
                                                              client):
    """The backend imports its Apple frameworks at module top, and
    `ScreenCaptureKit.framework` does not exist before macOS 12.3 — under an
    `LSMinimumSystemVersion` of 11.0. So this is a machine inside the supported
    range, and CP-8 promises it an answer: `available: false` with a reason on
    the read, a 409 on a start. An ImportError reaching either is a 500."""
    monkeypatch.setattr(capture.sys, "platform", "darwin")
    real_import = builtins.__import__

    def no_framework(name, globs=None, locs=None, fromlist=(), level=0):
        # Scoped to the backend import alone — `_backend()` reaches it as
        # `from fused_render.capture import _darwin`, i.e. a fromlist entry.
        if "_darwin" in (fromlist or ()):
            raise ImportError("No module named 'ScreenCaptureKit'")
        return real_import(name, globs, locs, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_framework)
    payload = capture.sources()
    assert payload["video"]["available"] is False
    assert "ScreenCaptureKit" in payload["video"]["reason"]
    assert set(payload) == set(FakeBackend().probe())
    res = client.post("/api/capture/start", json={"mode": "screen"}, headers=H)
    assert res.status_code == 409, res.json()


def test_a_probe_that_raises_is_still_an_answer(monkeypatch, client):
    """A PROBE MAY NOT RAISE: it is read while a page is drawing a record
    button, and `available: false` is something that page can render where a
    500 is not. The promise is about the SHAPE, so it cannot hold only for the
    failures this module predicted."""
    class Exploding:
        def probe(self):
            raise OSError("CoreGraphics said no")

    monkeypatch.setattr(capture, "_backend", lambda: Exploding())
    payload = capture.sources()
    assert set(payload) == set(FakeBackend().probe())
    for key in ("video", "audio", "systemAudio", "screenshot"):
        assert payload[key]["available"] is False
        assert "CoreGraphics said no" in payload[key]["reason"]
    # And through the route the page actually calls.
    res = client.get("/api/capture")
    assert res.status_code == 200
    assert res.json()["sources"]["video"]["available"] is False


@pytest.mark.parametrize("body,expected", [
    ({"mode": "sideways"}, "mode must be"),
    ({"mode": "screen", "audio": "microphone"}, "'audio' must be false"),
    ({"mode": "screen", "rect": [0, 0, 0, 10]}, "must be positive"),
    ({"mode": "screen", "rect": [0, 0, 10]}, "'rect' must be"),
    ({"mode": "screen", "maxSeconds": 0}, "must be between"),
    ({"mode": "screen", "maxSeconds": "soon"}, "whole number"),
    ({"mode": "audio", "source": "loud"},
     "'source' must be 'mic', 'system' or 'both', not 'loud'"),
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
                 "/api/capture/abc/stop", "/api/capture/abc/cancel",
                 "/api/capture/abc/pause", "/api/capture/abc/resume"):
        assert client.post(path, json={}).status_code == 403, path
    # …and the read is open, like the other read-only routes.
    assert client.get("/api/capture").status_code == 200


@pytest.mark.parametrize("body,want", [
    ({"mode": "audio"}, "mic"),
    ({"mode": "audio", "source": None}, "mic"),
    ({"mode": "audio", "source": ""}, "mic"),
    ({"mode": "audio", "source": True}, "mic"),
    ({"mode": "audio", "source": "mic"}, "mic"),
    ({"mode": "audio", "source": "system"}, "system"),
    ({"mode": "audio", "source": "both"}, "both"),
])
def test_audio_only_source_reaches_the_backend(backend, client, home, body, want):
    seen = []
    real = backend.start_audio

    def spy(out, spec):
        seen.append(dict(spec))
        return real(out, spec)

    backend.start_audio = spy
    res = client.post("/api/capture/start", json=body, headers=H)
    assert res.status_code == 200, res.json()
    assert seen[0]["audio"] == want
    assert res.json()["path"].endswith(".m4a")


def test_a_backend_warning_rides_the_stop_result_and_the_job_row(backend, client, home):
    backend.warn = "no system audio was captured"
    cid = client.post("/api/capture/start",
                      json={"mode": "audio", "source": "system"},
                      headers=H).json()["id"]
    done = client.post(f"/api/capture/{cid}/stop", json={}, headers=H).json()
    assert done["state"] == "stopped"
    assert done["warning"] == "no system audio was captured"
    assert "no system audio" in jobs.list_jobs()[0]["detail"]


def test_no_warning_key_when_the_backend_has_nothing_to_say(backend, client, home):
    cid = client.post("/api/capture/start", json={"mode": "audio"},
                      headers=H).json()["id"]
    done = client.post(f"/api/capture/{cid}/stop", json={}, headers=H).json()
    assert "warning" not in done


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
    # `fused.capture.*` is callable from any page's own script, so no single
    # hosting page names this row's source honestly — "Capture" names the
    # FEATURE that raised it instead, the same way `benchmark.py`'s own row
    # names itself "Benchmark".
    assert row["origin"] == "Capture"


def test_the_row_opens_the_page_that_started_the_capture(backend, client, home):
    """SPEC-actionable-notifications.md: the capture job's destination is
    threaded from `X-Fused-Page`, the same header/unquote channel
    `routers/jobs.py` already uses for a page-owned row's own `page`."""
    started = client.post(
        "/api/capture/start", json={"mode": "screen"},
        headers={**H, "X-Fused-Page": "/tmp/my%20app/index.html"},
    ).json()
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
              if j["id"] == started["jobId"])
    assert row["page"] == "/tmp/my app/index.html"


def test_no_page_header_leaves_the_row_with_no_destination(backend, client, home):
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
              if j["id"] == started["jobId"])
    assert row["page"] == ""


def test_the_rows_source_comes_from_the_ambient_X_Fused_Source(backend, client, home):
    """SPEC-quiet-notifications.md bug 2: `capture/__init__.py`'s `_report`
    calls `jobs.upsert(..., page=session.page, server=True)` with no
    `source=` of its own — the ambient default in `jobs.upsert` is the only
    thing that can put anything in `row["source"]` here. Sent with no
    `X-Fused-Page` (so `page` stays empty and cannot be mistaken for the
    source), the header must still land."""
    started = client.post(
        "/api/capture/start", json={"mode": "screen"},
        headers={**H, "X-Fused-Source": "/ai-models/playground"},
    ).json()
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
              if j["id"] == started["jobId"])
    assert row["page"] == ""
    assert row["source"] == "/ai-models/playground"


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


def test_a_second_stop_is_the_same_answer_and_not_a_second_stop(backend,
                                                               client, home):
    """The registry entry is taken under the lock before the device is touched,
    so a ✕ landing at the same moment as the page's own stop cannot finalise
    (or delete) the file twice.

    The LOSER of that race is answered from the finished-record cache rather
    than told its own recording never existed. Two real cases: a
    double-clicked stop button, and a streamed recording whose socket closed a
    moment before the page's stop request landed — that close is itself a valid
    ending, so a 404 there would report a failure for a recording that worked.
    """
    started = client.post("/api/capture/start", json={"mode": "audio"},
                          headers=H).json()
    first = client.post(f"/api/capture/{started['id']}/stop", headers=H)
    again = client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert first.status_code == again.status_code == 200
    assert again.json()["path"] == first.json()["path"]
    # The file was finalised ONCE: the second stop never reached the backend.
    assert len([h for h in backend.handles if h.stopped]) == 1


def test_a_cancel_after_a_stop_still_deletes_the_file(backend, client, home):
    """The one thing the cache must not do is let a `cancel` become a no-op.

    On the streamed backends the page's socket can close (an ending that KEEPS
    what arrived) a moment before its cancel request lands. Answering that with
    the cached "stopped" record would leave the file the caller asked to destroy
    on disk, reported as deleted.
    """
    started = client.post("/api/capture/start", json={"mode": "audio"},
                          headers=H).json()
    client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert os.path.exists(started["path"])
    gone = client.post(f"/api/capture/{started['id']}/cancel", headers=H).json()
    assert gone["path"] is None and gone["state"] == "cancelled"
    assert not os.path.exists(started["path"])


def test_an_id_that_never_existed_is_still_a_404(backend, client, home):
    assert client.post("/api/capture/nope/stop", headers=H).status_code == 404


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


def test_a_recording_that_dies_mid_flight_ends_its_row_then(backend, client,
                                                            home, monkeypatch):
    """Otherwise the row ticks "Recording" to the cap while nothing is written,
    and the user narrates half an hour into a dead file."""
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    backend.handles[0].error = "the display went away"
    deadline = time.monotonic() + 5
    while capture.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert capture.active() == []
    row = next(j for j in client.get("/api/jobs").json()["jobs"]
               if j["id"] == started["jobId"])
    assert row["state"] == "error"
    assert "display went away" in row["message"]
    assert backend.handles[0].stopped is True


def test_a_tick_that_raises_does_not_strand_the_recording(backend, client,
                                                          home, monkeypatch):
    """The watchdog is the ONLY control left once the page that started a
    recording is gone — it carries both the cap and the ✕. So a tick that
    raises must not take the thread with it: a dead watchdog is a microphone
    nothing can turn off, behind a row that ticks "Recording" forever.

    This probe fails on EVERY tick, not one, which is the case a try/except
    around the loop does not answer on its own — it is why `_tick` checks the
    cap before anything that can fail. With the probe above the cap, the guard
    logs the same failure until the process dies and the recording never ends.
    """
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    monkeypatch.setattr(capture, "_max_seconds", lambda value: 0.3)

    calls = []

    def exploding_failure(session):
        calls.append(session.id)
        raise RuntimeError("the backend probe fell over")

    # `_failure` guards itself, so REPLACING it is what gets an exception past
    # the tick's own handling and onto the loop.
    monkeypatch.setattr(capture, "_failure", exploding_failure)
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    deadline = time.monotonic() + 5
    while capture.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    # The cap still landed, the file was KEPT, and the backend was finalised —
    # `elapsed` only grows, so a failing tick costs one tick, not the ending.
    assert capture.active() == []
    assert os.path.exists(started["path"])
    assert backend.handles[0].stopped is True
    assert len(calls) > 1                        # it really did keep ticking


def test_a_job_store_that_cannot_be_written_still_records(backend, client,
                                                          home, monkeypatch):
    """`_report` is a ROW, not the work. It used to catch two named exceptions
    under a docstring promising it never breaks a recording — and the `start`
    call site is load-bearing: raising there registers a session whose watchdog
    thread was never spawned, i.e. a recording with no cap and no ✕."""
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    monkeypatch.setattr(capture, "_max_seconds", lambda value: 0.3)

    def exploding_upsert(body, **kwargs):
        raise RuntimeError("the job store fell over")

    monkeypatch.setattr(jobs, "upsert", exploding_upsert)
    res = client.post("/api/capture/start", json={"mode": "screen"}, headers=H)
    assert res.status_code == 200                # the row failed, not the start
    started = res.json()
    deadline = time.monotonic() + 5
    while capture.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert capture.active() == []                # the cap still ran
    assert os.path.exists(started["path"])
    assert backend.handles[0].stopped is True


def test_an_unreadable_job_store_is_not_a_cancel(backend, client, home,
                                                 monkeypatch):
    """`_cancel_requested` is a probe: a registry it cannot read answers "no".
    Reading it as a cancel would DELETE a recording over a transient failure —
    the one ending that destroys the file."""
    monkeypatch.setattr(capture, "TICK_S", 0.05)

    def exploding_list(**kwargs):
        raise RuntimeError("the job store fell over")

    monkeypatch.setattr(jobs, "list_jobs", exploding_list)
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    time.sleep(0.3)
    assert [r["id"] for r in capture.active()] == [started["id"]]
    assert os.path.exists(started["path"])


def test_stop_all_is_wired_into_the_paths_that_actually_run_on_exit():
    """`atexit` is the BACKSTOP, not the mechanism: the packaged app quits via
    `os._exit` (SPEC DM-9), which runs no atexit handler at all. So the two real
    exits have to name it — the quit ladder and the ASGI shutdown — or a quit
    mid-recording leaves an unplayable .mov behind a row that said "recording"."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ladder = open(os.path.join(root, "fused_render", "app.py"),
                  encoding="utf-8").read()
    assert '("capture", stop_captures)' in ladder
    assert "capture.stop_all()" in ladder
    server = open(os.path.join(root, "fused_render", "server", "app.py"),
                  encoding="utf-8").read()
    assert "capture.stop_all()" in server


def test_the_quit_ladder_runs_the_capture_rung_before_the_exit_record(monkeypatch):
    """Order matters: the recording is finalised while the teardown still runs;
    the exit record is the last rung."""
    from fused_render import app as desktop_app

    steps = desktop_app.quit_teardown(
        None, stop_captures=lambda: None, close_duckdb=lambda: None,
        stop_children=lambda: None, record_exit=lambda: None)
    assert steps.index("capture") < steps.index("exit-record")


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
    shot = client.post("/api/capture/screenshot", json={}, headers=H).json()
    assert shot["path"].endswith(".png") and shot["mime"] == "image/png"
    assert shot["width"] == 4 and shot["height"] == 2
    assert jobs.list_jobs() == []            # milliseconds need no row


def test_the_screenshots_container_follows_its_filename(backend, client,
                                                        tmp_path):
    """There is no `format` option: a `path` and a `format` could disagree, and
    "shot.jpg" holding PNG bytes is a file every other tool misreads."""
    shot = client.post("/api/capture/screenshot",
                       json={"path": str(tmp_path / "shot.jpg")},
                       headers=H).json()
    assert shot["mime"] == "image/jpeg"
    res = client.post("/api/capture/screenshot",
                      json={"path": str(tmp_path / "shot.gif")}, headers=H)
    assert res.status_code == 400
    assert ".png, .jpg or .jpeg" in res.json()["error"]


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
    # Cut in review, and the cut is the contract: no private `onTick` (the job
    # row + `fused.watchJob` already say elapsed) and no `format` (the output
    # filename decides png vs jpeg).
    block = source[source.index("// ----------------------------------------------------------- fused.capture"):]
    block = block[:block.index("window.fused = {")]
    assert "opts.onTick" not in block
    assert '"format"' not in block
    # The header block is the documented public bridge (EXPORT.md) — a method
    # that is not described there is not a contract anybody can rely on.
    assert "fused.capture.* -> record the screen, record the mic, grab a still" \
        in source
    # THE TWO WIRE-ONLY FIELDS MUST NOT REACH A PAGE. `transport` and
    # `streamToken` steer the browser-encoder path (CP-10); a handle carrying
    # either would be the `via` field CP-8 forbids, and a page would start
    # branching on which platform it landed on.
    handle = block[block.index("const handle = {"):]
    handle = handle[:handle.index("return handle;")]
    assert "transport" not in handle and "streamToken" not in handle
    # `client` is the same promise on the read side: the flag that says "this
    # platform's recording capability is the browser's" is consumed by the merge
    # and deleted before `sources()` resolves.
    assert "delete sources.client;" in block


def test_the_runtime_bridge_still_parses():
    """`node --check` on runtime.js — the cheapest guard that exists here.

    Nothing in this suite EXECUTES the bridge (it needs a browser), so every
    other test reads it as text and a stray brace would ship a file that breaks
    every page in the app rather than one feature. Skipped rather than failed
    where node is absent, like `test_git_view_renders.py` does.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:                                     # pragma: no cover
        pytest.skip("node is not installed")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    done = subprocess.run(
        [node, "--check",
         os.path.join(root, "fused_render", "static", "runtime.js")],
        capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_the_macos_backend_imports_and_probes_without_prompting():
    """Import + probe only: it must not need a display, a grant, or a click."""
    from fused_render.capture import _darwin

    payload = _darwin.probe()
    for key in ("video", "audio", "systemAudio", "screenshot"):
        assert set(payload[key]) >= {"available", "reason"}
    assert isinstance(payload["displays"], list)
    assert isinstance(payload["microphones"], list)


# --------------------------------------------------- the macOS 13-14 recorder


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_the_floors_are_per_verb_and_not_one_number():
    """CP-8. `SCRecordingOutput` is 15 and `SCScreenshotManager` is 14, but
    neither is the floor of the FEATURE: `_darwin_mux` writes the movie below
    15 and `CGDisplayCreateImage` takes the still below 14, so what is left is
    the floor of the thing native capture is FOR — system audio, macOS 13."""
    from fused_render.capture import _darwin

    assert _darwin.RECORD_MIN == (13, 0)
    assert _darwin.SHOT_MIN == (13, 0)
    assert _darwin.SCRO_MIN == (15, 0)
    assert _darwin.SCSHOT_MIN == (14, 0)


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_below_the_floor_is_a_reason_naming_thirteen(monkeypatch):
    """A 12.x Mac must read `available: false` with a sentence, not a raise —
    the probe is read while a page draws a record button."""
    from fused_render.capture import _darwin

    monkeypatch.setattr(_darwin, "_os_version", lambda: (12, 4))
    monkeypatch.setattr(_darwin.platform, "mac_ver", lambda: ("12.4", "", ""))
    payload = _darwin.probe()
    assert payload["video"]["available"] is False
    assert "needs macOS 13" in payload["video"]["reason"]
    assert payload["systemAudio"]["available"] is False
    assert payload["screenshot"]["available"] is False
    # Audio-only never needed ScreenCaptureKit at all, so it does not move.
    assert payload["audio"]["available"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_a_thirteen_or_fourteen_mac_routes_to_the_muxer(monkeypatch):
    from fused_render.capture import _darwin

    for version in ((13, 0), (13, 6), (14, 5)):
        monkeypatch.setattr(_darwin, "_os_version", lambda v=version: v)
        assert _darwin._use_mux() is True
    for version in ((15, 0), (26, 6)):
        monkeypatch.setattr(_darwin, "_os_version", lambda v=version: v)
        assert _darwin._use_mux() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_the_muxer_can_be_forced_on_a_new_mac(monkeypatch):
    """Every API `_darwin_mux` uses exists on 15 too. Without this env var the
    only code path nobody developing it can run is the one just written."""
    from fused_render.capture import _darwin

    monkeypatch.setattr(_darwin, "_os_version", lambda: (26, 6))
    monkeypatch.setenv(_darwin.FORCE_MUX, "1")
    assert _darwin._use_mux() is True
    monkeypatch.setenv(_darwin.FORCE_MUX, "0")
    assert _darwin._use_mux() is False


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_the_stream_is_not_asked_for_a_microphone_on_the_mux_path(monkeypatch):
    """`captureMicrophone` is macOS 15, and on the mux path the microphone
    comes from an `AVCaptureSession` at EVERY version — asking the stream too
    would mix the same voice into the movie twice.

    Asserted against a REAL `SCStreamConfiguration` rather than a recording
    stub: the guard is a `respondsToSelector_` check, so a fake that answers
    every selector would pass while the thing being tested does not exist."""
    import ScreenCaptureKit as SCK

    from fused_render.capture import _darwin

    if not SCK.SCStreamConfiguration.alloc().init().respondsToSelector_(
            b"setCaptureMicrophone:"):
        pytest.skip("this Mac is below macOS 15, where the guard is moot")

    monkeypatch.setattr(_darwin, "_display_scale", lambda display: 2)

    class FakeDisplay:
        @staticmethod
        def width():
            return 100

        @staticmethod
        def height():
            return 100

        @staticmethod
        def displayID():
            return 1

    muxed = _darwin._configure(FakeDisplay(), {"audio": "both"},
                               stream_mic=False)
    assert bool(muxed.capturesAudio()) is True
    assert bool(muxed.captureMicrophone()) is False

    native = _darwin._configure(FakeDisplay(), {"audio": "both"},
                                stream_mic=True)
    assert bool(native.capturesAudio()) is True
    assert bool(native.captureMicrophone()) is True


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_a_cursor_still_is_refused_on_thirteen_rather_than_drawn_without_one():
    """`CGDisplayCreateImage` never draws the pointer. Handing back a still
    that silently lacks it is the D319 mistake; the sentence names the version
    where `cursor` works."""
    from fused_render.capture import _darwin

    with pytest.raises(capture.CaptureError) as caught:
        _darwin._cg_shot("/tmp/never-written.png", object(), {"cursor": True})
    assert "macOS 14" in str(caught.value)


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_the_mux_backend_imports_and_exposes_the_seam():
    """Import alone: the delegates declare real ObjC protocols and the module
    creates its dispatch queues at import time, so a typo in either is an
    ImportError here rather than a recording that never delivers a frame."""
    from fused_render.capture import _darwin_mux

    for hook in ("start", "stop", "failure"):
        assert callable(getattr(_darwin_mux, hook))
    assert _darwin_mux._SCREEN_Q.className() == "OS_dispatch_queue_serial"
    assert _darwin_mux._MIC_Q.className() == "OS_dispatch_queue_serial"


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_a_mux_recording_that_saw_no_frames_is_an_error_not_a_dead_file():
    """`finishWriting` over a session that never started produces a file that
    does not play. A row saying "done" over one of those is the worst outcome
    available (D409), so this raises instead."""
    from fused_render.capture import _darwin_mux

    handle = _darwin_mux.MuxHandle("/tmp/never-written.mov", None)
    cancelled = []
    handle.writer = type("W", (), {
        "cancelWriting": lambda self: cancelled.append(True),
        "error": lambda self: None,
    })()
    with pytest.raises(RuntimeError) as caught:
        handle.finish()
    assert "no frames" in str(caught.value)
    assert cancelled == [True]


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS backend")
def test_the_mux_handle_reports_an_error_it_has_already_died_of():
    """The read side of the per-tick `failure(handle)` hook. Without it a
    stream that dies at minute two ticks "Recording" to the cap over a file
    nothing is writing."""
    from fused_render.capture import _darwin_mux

    handle = _darwin_mux.MuxHandle("/tmp/x.mov", "system")
    assert _darwin_mux.failure(handle) is None
    handle.note_error("the display went away")
    assert _darwin_mux.failure(handle) == "the display went away"
    handle.note_error("something later")
    assert _darwin_mux.failure(handle) == "the display went away"


# ------------------------------------------------------------ native serialisation

def test_backend_calls_never_overlap(backend, monkeypatch, tmp_path):
    """Every call into the native backend is serialised.

    The backends drive ObjC objects from whichever threadpool thread the
    request landed on; the same shape crashed the app through NSPasteboard.
    A backend that counts how many callers are inside it at once must never
    see more than one across concurrent screenshots, starts and stops.
    """
    import threading
    import time

    inside = {"n": 0, "peak": 0}
    guard = threading.Lock()

    def tracked(fn):
        def wrapper(*a, **kw):
            with guard:
                inside["n"] += 1
                inside["peak"] = max(inside["peak"], inside["n"])
            try:
                time.sleep(0.002)
                return fn(*a, **kw)
            finally:
                with guard:
                    inside["n"] -= 1
        return wrapper

    for name in ("start_screen", "start_audio", "stop", "screenshot"):
        monkeypatch.setattr(backend, name, tracked(getattr(backend, name)))

    def shoot(i):
        capture.screenshot({"path": str(tmp_path / f"s{i}.png")})

    def record(i):
        rec = capture.start("audio", {"path": str(tmp_path / f"r{i}.m4a")})
        capture.stop(rec["id"])

    threads = [threading.Thread(target=shoot, args=(i,)) for i in range(6)]
    threads += [threading.Thread(target=record, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert inside["peak"] == 1


def test_an_ending_does_not_wait_forever_behind_a_prompting_start(
        backend, monkeypatch, tmp_path, caplog):
    """A `start` can hold the native lock for the whole OS permission prompt
    (up to WAIT_S). A `stop` — user, cap, death or quit — waits a bounded
    time and then ends the recording unserialised rather than leaving a
    microphone on or a quit hanging."""
    import threading
    import time

    monkeypatch.setattr(capture, "STOP_LOCK_WAIT_S", 0.05)
    rec = capture.start("audio", {"path": str(tmp_path / "r.m4a")})

    prompting = threading.Event()
    release = threading.Event()

    def slow_start():
        with capture._native():
            prompting.set()
            release.wait(5)

    holder = threading.Thread(target=slow_start, daemon=True)
    holder.start()
    assert prompting.wait(2)

    t0 = time.monotonic()
    result = capture.stop(rec["id"])
    elapsed = time.monotonic() - t0
    release.set()
    holder.join(2)

    assert result["state"] == "stopped"
    assert backend.handles[0].stopped
    assert elapsed < 1.0
    assert "native lock busy" in caplog.text
    # The lock was never released by the loser: it is back to free now.
    assert capture._native_lock.acquire(timeout=1)
    capture._native_lock.release()


# ------------------------------------------- macOS audio-only system audio


_darwin_only = pytest.mark.skipif(sys.platform != "darwin",
                                  reason="the macOS backend")


@_darwin_only
def test_darwin_refuse_device_follows_the_source():
    from fused_render.capture import _darwin

    mic = _darwin.refuse("audio", {"audio": "mic", "device": "x"})
    assert mic and "audio: 'mic'" in mic            # the unchanged sentence
    system = _darwin.refuse("audio", {"audio": "system", "device": "x"})
    assert system and "source: 'both'" in system and "no microphone" in system
    assert _darwin.refuse("audio", {"audio": "both", "device": "x"}) is None
    assert _darwin.refuse("audio", {"audio": "system"}) is None


@_darwin_only
@pytest.mark.parametrize("version,forced,source,want", [
    ((14, 2), False, "system", "tap"),
    ((15, 0), False, "both", "tap"),
    ((26, 6), True, "system", "sck"),
    ((14, 1), False, "both", "sck"),
    ((13, 0), False, "system", "sck"),
    ((26, 6), True, "mic", "mic"),
    ((13, 0), False, "mic", "mic"),
])
def test_darwin_audio_only_routes_by_os_version(monkeypatch, version, forced,
                                                source, want):
    from fused_render.capture import _darwin, _darwin_mux, _darwin_tap

    calls = []
    monkeypatch.setattr(_darwin, "_os_version", lambda: version)
    monkeypatch.setattr(_darwin.platform, "mac_ver",
                        lambda: (".".join(map(str, version)), "", ""))
    if forced:
        monkeypatch.setenv(_darwin.FORCE_SCK_AUDIO, "1")
    else:
        monkeypatch.delenv(_darwin.FORCE_SCK_AUDIO, raising=False)
    monkeypatch.setattr(_darwin_tap, "start",
                        lambda out, spec: calls.append("tap") or "h")
    monkeypatch.setattr(_darwin, "_display", lambda d: "display")
    monkeypatch.setattr(_darwin, "_configure", lambda *a, **k: "config")
    monkeypatch.setattr(_darwin_mux, "start_audio",
                        lambda out, display, config, spec:
                        calls.append("sck") or "h")

    class Recorder:
        @staticmethod
        def alloc():
            calls.append("mic")
            raise RuntimeError("stop here")

    monkeypatch.setattr(_darwin.AVF, "AVAudioRecorder", Recorder)
    try:
        _darwin.start_audio("/tmp/x.m4a", {"audio": source})
    except RuntimeError:
        assert want == "mic"
    assert calls == [want]


@_darwin_only
def test_darwin_probe_system_audio_grant_shape(monkeypatch):
    from fused_render.capture import _darwin

    monkeypatch.setattr(_darwin, "_os_version", lambda: (15, 1))
    monkeypatch.setattr(_darwin.platform, "mac_ver", lambda: ("15.1", "", ""))
    sa = _darwin.probe()["systemAudio"]
    assert sa == {"available": True, "granted": None, "reason": None}

    monkeypatch.setattr(_darwin, "_os_version", lambda: (14, 1))
    monkeypatch.setattr(_darwin.platform, "mac_ver", lambda: ("14.1", "", ""))
    monkeypatch.setattr(_darwin, "_screen_granted", lambda: False)
    sa = _darwin.probe()["systemAudio"]
    assert sa["available"] is True and sa["granted"] is False
    assert "Screen Recording" in sa["reason"]
    monkeypatch.setattr(_darwin, "_screen_granted", lambda: True)
    assert _darwin.probe()["systemAudio"]["reason"] is None

    monkeypatch.setattr(_darwin, "_os_version", lambda: (12, 4))
    monkeypatch.setattr(_darwin.platform, "mac_ver", lambda: ("12.4", "", ""))
    sa = _darwin.probe()["systemAudio"]
    assert sa["available"] is False and "macOS 13" in sa["reason"]


@_darwin_only
def test_darwin_probe_forced_sck_on_14_2_reports_screen_grant(monkeypatch):
    from fused_render.capture import _darwin

    monkeypatch.setenv(_darwin.FORCE_SCK_AUDIO, "1")
    monkeypatch.setattr(_darwin, "_os_version", lambda: (14, 5))
    monkeypatch.setattr(_darwin.platform, "mac_ver", lambda: ("14.5", "", ""))
    monkeypatch.setattr(_darwin, "_screen_granted", lambda: False)
    sa = _darwin.probe()["systemAudio"]
    assert sa["granted"] is False and "Screen Recording" in sa["reason"]
    monkeypatch.setattr(_darwin, "_screen_granted", lambda: True)
    assert _darwin.probe()["systemAudio"]["granted"] is True


@_darwin_only
def test_darwin_tap_handle_dispatch_and_warning(monkeypatch):
    from fused_render.capture import _darwin, _darwin_tap

    handle = _darwin_tap.TapHandle("/tmp/x.m4a", False)
    seen = []
    monkeypatch.setattr(_darwin_tap, "stop", lambda h: seen.append(("stop", h)))
    _darwin.stop(handle)
    assert seen == [("stop", handle)]
    handle.error = "boom"
    assert _darwin.failure(handle) == "boom"

    # warning: silent only after >= 1 s, and never once anything was heard.
    handle = _darwin_tap.TapHandle("/tmp/x.m4a", False)
    assert _darwin.warning(handle) is None                  # never started
    handle.started_at = _darwin_tap.time.monotonic() - 0.2
    assert _darwin.warning(handle) is None                  # too short
    handle.started_at = _darwin_tap.time.monotonic() - 2
    assert "no system audio was captured" in _darwin.warning(handle)
    handle.heard_system = True
    assert _darwin.warning(handle) is None


@_darwin_only
def test_darwin_mux_audio_only_session_starts_on_audio_not_video(monkeypatch):
    from fused_render.capture import _darwin_mux

    monkeypatch.setattr(_darwin_mux.CM, "CMSampleBufferGetPresentationTimeStamp",
                        lambda sbuf: "pts")

    class Writer:
        started = []

        def startSessionAtSourceTime_(self, t):
            self.started.append(t)

    class Input:
        taken = 0

        def isReadyForMoreMediaData(self):
            return True

        def appendSampleBuffer_(self, sbuf):
            self.taken += 1
            return True

    handle = _darwin_mux.MuxHandle("/tmp/x.m4a", "system", audio_only=True)
    handle.writer, handle.audio_input = Writer(), Input()
    # The fake buffer has no CoreMedia timing; pause retiming has its own test.
    monkeypatch.setattr(handle, "_shifted", lambda sbuf: sbuf)
    handle.on_video("frame")                      # ignored: there is no video track
    assert handle.writer.started == [] and handle.audio_input.taken == 0
    handle._append(handle.audio_input, "a", video=False)
    assert handle.writer.started == ["pts"] and handle.audio_input.taken == 1


@_darwin_only
def test_darwin_mux_peak_and_system_audio_warning(monkeypatch):
    import array

    from fused_render.capture import _darwin, _darwin_mux

    quiet = array.array("f", [0.0, 0.00001, -0.00002]).tobytes()
    loud = array.array("f", [0.0, -0.5]).tobytes()
    assert _darwin_mux._peak_f32(b"") == 0.0
    assert _darwin_mux._peak_f32(quiet) < 1e-4
    assert _darwin_mux._peak_f32(loud + b"\x01") == 0.5       # ragged tail ignored

    now = _darwin_mux.time.monotonic()
    handle = _darwin_mux.MuxHandle("/tmp/x.m4a", "system", audio_only=True)
    assert _darwin.warning(handle) is None                    # never started
    handle.started_at = now - 0.2
    assert _darwin.warning(handle) is None                    # too short
    handle.started_at = now - 5
    assert "reached the recording" in _darwin.warning(handle)  # zero buffers
    handle.system_buffers = 3
    assert "no system audio was captured" in _darwin.warning(handle)
    handle.system_heard = True
    assert _darwin.warning(handle) is None

    both = _darwin_mux.MuxHandle("/tmp/x.m4a", "both", audio_only=True)
    both.started_at = now - 5
    assert _darwin.warning(both) is not None
    for h in (_darwin_mux.MuxHandle("/tmp/x.m4a", "mic", audio_only=True),
              _darwin_mux.MuxHandle("/tmp/x.mp4", "system", audio_only=False)):
        h.started_at = now - 5
        assert _darwin.warning(h) is None

    # _note_system: silence is counted, sound flips the flag, a parse failure
    # is "unknown" (heard) and never raises.
    h = _darwin_mux.MuxHandle("/tmp/x.m4a", "system", audio_only=True)
    h._note_system("sbuf", bytearray(quiet))
    assert (h.system_buffers, h.system_heard) == (1, False)
    h._note_system("sbuf", bytearray(loud))
    assert (h.system_buffers, h.system_heard) == (2, True)
    h = _darwin_mux.MuxHandle("/tmp/x.m4a", "system", audio_only=True)
    monkeypatch.setattr(_darwin_mux, "_pcm",
                        lambda sbuf: (_ for _ in ()).throw(ValueError("bad")))
    h._note_system("sbuf")
    assert h.system_heard is True


@_darwin_only
@pytest.mark.parametrize("ver,narrower", [((13, 6), False), ((14, 1), True)])
def test_darwin_audio_permission_message(monkeypatch, ver, narrower):
    from fused_render.capture import _darwin

    text = f"{ver[0]}.{ver[1]}"
    monkeypatch.setattr(_darwin, "_os_version", lambda: ver)
    monkeypatch.setattr(_darwin.platform, "mac_ver", lambda: (text, "", ""))
    monkeypatch.setattr(_darwin, "_screen_granted", lambda: False)
    monkeypatch.setattr(_darwin, "_use_tap", lambda: False)

    def no_display(display_id):
        raise RuntimeError("no capturable display")

    monkeypatch.setattr(_darwin, "_display", no_display)
    with pytest.raises(RuntimeError) as err:
        _darwin.start_audio("/tmp/x.m4a", {"audio": "system"})
    msg = str(err.value)
    assert f"on macOS {text} needs the Screen Recording permission" in msg
    assert "only sound is recorded, no picture" in msg
    assert msg.endswith("then quit and reopen the app." + (
        " Updating to macOS 14.2 or later uses the narrower System Audio "
        "Recording permission instead." if narrower else ""))

    # Granted: the original error is not rewritten.
    monkeypatch.setattr(_darwin, "_screen_granted", lambda: True)
    with pytest.raises(RuntimeError, match="no capturable display"):
        _darwin.start_audio("/tmp/x.m4a", {"audio": "system"})


@_darwin_only
def test_darwin_tap_start_failure_is_not_masked_by_teardown_error(monkeypatch):
    from fused_render.capture import CaptureError, _darwin_tap

    def bad_start(handle, out, spec):
        handle.tap = 7
        raise CaptureError("no such microphone: 'x'")

    def bad_destroy(tap):
        raise RuntimeError("destroy failed")

    monkeypatch.setattr(_darwin_tap, "_start", bad_start)
    monkeypatch.setattr(_darwin_tap.CA, "AudioHardwareDestroyProcessTap", bad_destroy)
    with pytest.raises(CaptureError, match="no such microphone"):
        _darwin_tap.start("/tmp/x.m4a", {"audio": "both"})


# ---------------------------------------------------------- pause and resume


class Clock:
    """One fake wall clock for a session's start time AND its pause clock."""

    def __init__(self, session):
        self.t = 5000.0
        session.started_at = self.t
        session.pauses._clock = self

    def __call__(self):
        return self.t


def _audio(client):
    return client.post("/api/capture/start", json={"mode": "audio"},
                       headers=H).json()


def _row(client, started):
    return next(j for j in client.get("/api/jobs").json()["jobs"]
                if j["id"] == started["jobId"])


def test_pause_and_resume_round_trip(backend, client, home):
    started = _audio(client)
    assert started["paused"] is False and started["canPause"] is True
    cid = started["id"]
    res = client.post(f"/api/capture/{cid}/pause", headers=H)
    assert res.status_code == 200, res.json()
    body = res.json()
    assert body["paused"] is True and body["canPause"] is True
    assert body["state"] == "recording"
    assert [kind for kind, _ in backend.paused] == ["pause"]
    assert "Paused" in _row(client, started)["detail"]
    assert capture.active()[0]["paused"] is True
    body = client.post(f"/api/capture/{cid}/resume", headers=H).json()
    assert body["paused"] is False
    assert [kind for kind, _ in backend.paused] == ["pause", "resume"]
    assert _row(client, started)["detail"].startswith("Recording")


def test_a_concurrent_resume_waits_for_the_pause_to_finish(backend, client,
                                                          home):
    import threading

    gate, entered = threading.Event(), threading.Event()
    real_pause = backend.pause

    def slow_pause(handle):
        entered.set()
        assert gate.wait(5)
        real_pause(handle)

    backend.pause = slow_pause
    cid = _audio(client)["id"]
    session = capture._live(cid)
    t1 = threading.Thread(target=capture.pause, args=(cid,))
    t1.start()
    assert entered.wait(5)
    t2 = threading.Thread(target=capture.resume, args=(cid,))
    t2.start()
    t2.join(0.3)
    assert t2.is_alive()                 # resume is queued behind the pause
    assert backend.paused == []
    gate.set()
    t1.join(5)
    t2.join(5)
    assert [k for k, _ in backend.paused] == ["pause", "resume"]
    assert session.pauses.paused is False


def test_pausing_an_unknown_recording_is_a_404(backend, client, home):
    for action in ("pause", "resume"):
        assert client.post(f"/api/capture/nope/{action}",
                           headers=H).status_code == 404
    assert client.get("/api/capture/nope/state").status_code == 404


def test_pausing_a_recording_the_backend_cannot_pause_is_a_409(backend, client,
                                                               home):
    backend.pausable = False
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    assert started["canPause"] is False
    res = client.post(f"/api/capture/{started['id']}/pause", headers=H)
    assert res.status_code == 409
    assert "screen recordings" in res.json()["error"]
    assert backend.paused == []
    audio = _audio(client)
    assert audio["canPause"] is False
    res = client.post(f"/api/capture/{audio['id']}/pause", headers=H)
    assert res.status_code == 409 and "cannot be paused" in res.json()["error"]


def test_a_backend_without_the_hooks_cannot_pause(backend, client, home,
                                                  monkeypatch):
    """The Windows/Linux backends are browser-fed and define none of them."""
    class Bare:
        probe = staticmethod(backend.probe)
        ext = staticmethod(backend.ext)
        start_audio = backend.start_audio
        stop = staticmethod(backend.stop)

    bare = Bare()
    monkeypatch.setattr(capture, "_backend", lambda: bare)
    started = _audio(client)
    assert started["canPause"] is False
    assert client.post(f"/api/capture/{started['id']}/pause",
                       headers=H).status_code == 409
    assert client.get(f"/api/capture/{started['id']}/state").json()["level"] == 0.0


def test_a_second_pause_or_resume_is_idempotent(backend, client, home):
    cid = _audio(client)["id"]
    client.post(f"/api/capture/{cid}/resume", headers=H)      # not paused: no-op
    assert backend.paused == []
    client.post(f"/api/capture/{cid}/pause", headers=H)
    again = client.post(f"/api/capture/{cid}/pause", headers=H)
    assert again.status_code == 200 and again.json()["paused"] is True
    assert len(backend.paused) == 1                            # the backend heard once


def test_a_failing_backend_pause_leaves_the_recording_running(backend, client,
                                                              home, monkeypatch):
    cid = _audio(client)["id"]

    def boom(handle):
        raise RuntimeError("the microphone did not pause")

    monkeypatch.setattr(backend, "pause", boom)
    res = client.post(f"/api/capture/{cid}/pause", headers=H)
    assert res.status_code == 500
    assert capture.state(cid)["paused"] is False


def test_the_elapsed_time_freezes_while_paused(backend, client, home):
    cid = _audio(client)["id"]
    session = capture._sessions[cid]
    clock = Clock(session)
    clock.t += 10
    assert session.public()["seconds"] == 10
    capture.pause(cid)
    clock.t += 500
    assert session.public()["seconds"] == 10
    assert capture.state(cid)["seconds"] == 10
    capture.resume(cid)
    clock.t += 5
    assert session.public()["seconds"] == 15


def test_a_paused_recording_does_not_reach_its_cap(backend, client, home,
                                                   monkeypatch):
    monkeypatch.setattr(capture, "_max_seconds", lambda value: 60)
    cid = _audio(client)["id"]
    session = capture._sessions[cid]
    clock = Clock(session)
    clock.t += 50
    capture.pause(cid)
    clock.t += 600                                  # far past the cap, but paused
    assert capture._tick(session) is False
    assert capture.active() != []
    capture.resume(cid)
    clock.t += 5
    assert capture._tick(session) is False          # 55 s recorded
    clock.t += 6
    assert capture._tick(session) is True           # 61 s: the cap stops it
    assert capture.active() == [] and backend.handles[0].stopped


def test_a_recording_paused_forever_is_stopped_and_kept(backend, client, home,
                                                        monkeypatch):
    monkeypatch.setattr(capture, "PAUSE_MAX_S", 100)
    cid = _audio(client)["id"]
    session = capture._sessions[cid]
    clock = Clock(session)
    capture.pause(cid)
    clock.t += 99
    assert capture._tick(session) is False
    clock.t += 2
    assert capture._tick(session) is True
    assert capture.active() == []
    assert os.path.exists(session.path)             # stopped, not discarded


def test_the_state_route_reports_the_level_from_the_hook(backend, client, home,
                                                         monkeypatch):
    cid = _audio(client)["id"]
    backend.lvl = 0.37
    res = client.get(f"/api/capture/{cid}/state")
    assert res.status_code == 200
    body = res.json()
    assert body == {"id": cid, "state": "recording", "paused": False,
                    "seconds": body["seconds"], "maxSeconds": 1800,
                    "level": 0.37, "canPause": True}
    backend.lvl = 7.0
    assert client.get(f"/api/capture/{cid}/state").json()["level"] == 1.0
    backend.lvl = RuntimeError("no meters")
    assert client.get(f"/api/capture/{cid}/state").json()["level"] == 0.0
    backend.lvl = 0.5
    monkeypatch.setattr(capture, "_backend", lambda: object())
    assert client.get(f"/api/capture/{cid}/state").json()["level"] == 0.0


def test_state_answers_with_level_zero_when_the_backend_lookup_raises(
        backend, client, home, monkeypatch):
    cid = _audio(client)["id"]

    def boom():
        raise capture.Unsupported("no backend")

    monkeypatch.setattr(capture, "_backend", boom)
    body = capture.state(cid)
    assert body["level"] == 0.0 and body["id"] == cid


def test_state_answers_while_another_thread_holds_the_native_lock(backend,
                                                                  client, home):
    import threading

    cid = _audio(client)["id"]
    capture._native_lock.acquire()
    try:
        out = []
        t = threading.Thread(target=lambda: out.append(capture.state(cid)))
        t.start()
        t.join(2)
        assert not t.is_alive() and out and out[0]["id"] == cid
    finally:
        capture._native_lock.release()


# ----------------------------------------------------- the recording pill hooks


@pytest.fixture()
def pill(monkeypatch):
    """Recording what the native hooks are told, with the dict restored after."""
    from fused_render import recording_pill

    calls = []
    monkeypatch.setattr(recording_pill, "native_hooks", {
        "show": lambda info: calls.append(("show", info["id"], info)),
        "hide": lambda cid: calls.append(("hide", cid)),
        "hide_all": lambda: calls.append(("hide_all",)),
    })
    return calls


def _kinds(calls, kind):
    return [c for c in calls if c[0] == kind]


def test_the_pill_shows_once_on_an_audio_recording_and_hides_on_stop(
        backend, client, home, pill):
    started = _audio(client)
    assert [c[:2] for c in pill] == [("show", started["id"])]
    info = pill[0][2]
    assert info["canPause"] is True and info["mode"] == "audio"
    client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert [c[:2] for c in pill] == [("show", started["id"]),
                                     ("hide", started["id"])]
    # The second ending (double click) answers from the cache and hides nothing.
    client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert len(_kinds(pill, "hide")) == 1


def test_a_screen_recording_gets_no_pill(backend, client, home, pill):
    started = client.post("/api/capture/start", json={"mode": "screen"},
                          headers=H).json()
    assert pill == []
    client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert pill == []


def test_the_pill_hides_on_cancel(backend, client, home, pill):
    started = _audio(client)
    client.post(f"/api/capture/{started['id']}/cancel", headers=H)
    assert _kinds(pill, "hide") == [("hide", started["id"])]


def test_the_pill_hides_when_the_cap_ends_the_recording(backend, client, home,
                                                        pill, monkeypatch):
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    monkeypatch.setattr(capture, "_max_seconds", lambda value: 0.2)
    started = _audio(client)
    deadline = time.monotonic() + 5
    while capture.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _kinds(pill, "hide") == [("hide", started["id"])]


def test_the_pill_hides_when_the_backend_loses_the_recording(
        backend, client, home, pill, monkeypatch):
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = _audio(client)
    backend.handles[0].error = "the device went away"
    deadline = time.monotonic() + 5
    while capture.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _kinds(pill, "hide") == [("hide", started["id"])]


def test_the_pill_hides_when_the_job_rows_cross_is_pressed(
        backend, client, home, pill, monkeypatch):
    monkeypatch.setattr(capture, "TICK_S", 0.05)
    started = _audio(client)
    client.post(f"/api/jobs/{started['jobId']}/cancel", headers=H)
    deadline = time.monotonic() + 5
    while capture.active() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _kinds(pill, "hide") == [("hide", started["id"])]


def test_stop_all_hides_every_pill(backend, client, home, pill):
    a, b = _audio(client), _audio(client)
    capture.stop_all()
    assert {c[1] for c in _kinds(pill, "hide")} == {a["id"], b["id"]}
    assert _kinds(pill, "hide_all") == [("hide_all",)]


def test_a_start_that_raises_after_the_pill_showed_hides_it(
        backend, client, home, pill, monkeypatch):
    class Boom(Exception):
        pass

    def no_thread(*a, **k):
        raise Boom("could not spawn the watchdog")

    monkeypatch.setattr(capture.threading, "Thread", no_thread)
    with pytest.raises(Boom):
        capture.start("audio", {})
    assert len(_kinds(pill, "show")) == 1
    assert _kinds(pill, "hide") == [("hide", _kinds(pill, "show")[0][1])]


def test_a_start_that_never_produced_a_recording_shows_nothing(
        backend, client, home, pill, monkeypatch):
    def boom(out, spec):
        raise RuntimeError("the microphone did not start")

    monkeypatch.setattr(backend, "start_audio", boom)
    assert client.post("/api/capture/start", json={"mode": "audio"},
                       headers=H).status_code == 500
    assert pill == []


def test_a_raising_pill_hook_never_breaks_start_or_stop(backend, client, home,
                                                        monkeypatch):
    from fused_render import recording_pill

    def boom(*args):
        raise RuntimeError("AppKit fell over")

    monkeypatch.setattr(recording_pill, "native_hooks",
                        {"show": boom, "hide": boom, "hide_all": boom})
    started = _audio(client)
    res = client.post(f"/api/capture/{started['id']}/stop", headers=H)
    assert res.status_code == 200 and res.json()["state"] == "stopped"
    capture.stop_all()


def test_with_no_hooks_installed_the_pill_is_a_no_op(backend, client, home,
                                                     monkeypatch):
    from fused_render import recording_pill

    monkeypatch.setattr(recording_pill, "native_hooks", {})
    started = _audio(client)
    assert client.post(f"/api/capture/{started['id']}/stop",
                       headers=H).status_code == 200


def test_the_pill_page_route_serves_the_built_page_or_says_it_is_missing(client):
    res = client.get("/recording-pill")
    assert res.status_code in (200, 503)
    if res.status_code == 503:
        assert "recording pill page not built" in res.text


def test_describe_carries_the_apps_icon_url(tmp_path):
    from fused_render import recording_pill

    app = tmp_path / "My App"
    app.mkdir()
    (app / "icon.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (app / "index.html").write_text("<html></html>")

    class S:
        id, mode, can_pause = "c1", "audio", True
        page = str(app / "index.html")

    info = recording_pill.describe(S())
    assert info["id"] == "c1" and info["canPause"] is True
    assert info["app"] == str(app) and info["name"] == "My App"
    assert info["icon"].startswith("/api/fs/raw?path=") and "&v=" in info["icon"]
    assert info["flavor"] in ("render", "bot")

    S.page = ""
    info = recording_pill.describe(S())
    assert info["icon"] is None and info["app"] == "" and info["name"] == "Fused"
    S.page = "not/absolute.html"
    assert recording_pill.describe(S())["icon"] is None


def _pill_session(page):
    class S:
        id, mode, can_pause = "c1", "audio", True

    S.page = str(page)
    return S


def _pill_repo(tmp_path, monkeypatch):
    """A parent repo whose pyproject.toml declares deps, so it is the project root."""
    from fused_render import projectenv

    monkeypatch.setattr(projectenv, "_ceiling", lambda: str(tmp_path))
    repo = tmp_path / "repo"
    (repo / "apps" / "foo" / "sub").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "r"\nversion = "0"\ndependencies = ["requests"]\n')
    (repo / "apps" / "foo" / "index.html").write_text("<html></html>")
    (repo / "apps" / "foo" / "sub" / "page.html").write_text("<html></html>")
    return repo


_SVG = "<svg xmlns='http://www.w3.org/2000/svg'/>"


def test_describe_uses_the_nearest_icon_below_a_parent_project_root(tmp_path, monkeypatch):
    from urllib.parse import unquote
    from fused_render import recording_pill

    repo = _pill_repo(tmp_path, monkeypatch)
    foo = repo / "apps" / "foo"
    (foo / "icon.svg").write_text(_SVG)

    info = recording_pill.describe(_pill_session(foo / "sub" / "page.html"))
    assert info["icon"] is not None
    assert unquote(info["icon"]).split("path=")[1].split("&")[0] == str(foo / "icon.svg")
    assert info["name"] == "foo"
    assert info["app"].replace("\\", "/").endswith("apps/foo")


def test_describe_falls_back_to_the_root_icon(tmp_path, monkeypatch):
    from fused_render import recording_pill

    repo = _pill_repo(tmp_path, monkeypatch)
    (repo / "icon.svg").write_text(_SVG)

    info = recording_pill.describe(_pill_session(repo / "apps" / "foo" / "sub" / "page.html"))
    assert info["icon"] is not None
    assert info["name"] == "repo" and info["app"] == str(repo)


def test_describe_without_any_icon_keeps_the_root(tmp_path, monkeypatch):
    from fused_render import recording_pill

    repo = _pill_repo(tmp_path, monkeypatch)

    info = recording_pill.describe(_pill_session(repo / "apps" / "foo" / "index.html"))
    assert info["icon"] is None
    assert info["app"] == str(repo) and info["name"] == "repo"


def test_describe_with_no_page_is_the_generic_fused_pill():
    from fused_render import recording_pill

    info = recording_pill.describe(_pill_session(""))
    assert info["icon"] is None and info["name"] == "Fused" and info["app"] == ""


def test_a_resume_after_stop_never_reaches_the_backend(backend, client, home):
    """`stop` can pop the session between `_set_paused`'s `_live` and its backend
    call; a mic `resume` on the finished recorder would overwrite the take."""
    cid = _audio(client)["id"]
    capture.pause(cid)
    real_native = capture._native

    class Popping(real_native):
        def __enter__(self):
            out = super().__enter__()
            with capture._lock:
                capture._sessions.pop(cid, None)     # stop got there first
            return out

    capture._native = Popping
    try:
        with pytest.raises(capture.CaptureError, match="no such capture"):
            capture.resume(cid)
    finally:
        capture._native = real_native
    assert [k for k, _ in backend.paused] == ["pause"]


def test_pause_does_not_run_unserialised_when_the_native_lock_is_busy(
        backend, client, home, monkeypatch):
    cid = _audio(client)["id"]
    session = capture._live(cid)
    monkeypatch.setattr(capture, "STOP_LOCK_WAIT_S", 0.05)
    with capture._native():
        with pytest.raises(capture.CaptureError, match="busy"):
            capture.pause(cid)
    assert backend.paused == []
    assert session.pauses.paused is False


def test_a_stop_error_keeps_the_backends_warning(backend, client, home):
    backend.warn = "quit and reopen this app after granting Screen Recording"
    cid = _audio(client)["id"]

    def boom(handle):
        raise RuntimeError("the recording captured no audio")

    backend.stop = boom
    done = client.post(f"/api/capture/{cid}/stop", json={}, headers=H).json()
    assert done["warning"] == backend.warn
    assert done["error"].startswith(backend.warn)
    assert "captured no audio" in done["error"]
