"""Record 60s: the recorder thread, its on-disk bundle, retention and the endpoints."""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import netlog
from fused_render.server import create_app, perfrecord

HDRS = {"X-Fused": "1"}


@pytest.fixture()
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(perfrecord, "recordings_dir", lambda: str(tmp_path / "perf-recordings"))
    monkeypatch.setattr(perfrecord, "_snapshot", lambda: {"t": time.time(), "verdict": {"level": "ok"}})
    monkeypatch.setattr(perfrecord, "_procs", lambda: [{"pid": 1, "label": "x"}])
    return perfrecord.Recorder(duration=0.12, interval=0.03)


def test_recording_writes_bundle_and_forces_watch(rec, tmp_path):
    r = netlog.get().begin("GET", "/api/fs/list", "path=/secret", {"user-agent": "x"}, "127.0.0.1")
    netlog.get().finish(r, status=200)
    assert rec.start()["recording"] is True
    assert netlog.get().watched()
    assert rec.start()["already"] is True          # a second start does not stack
    rec.join(5)
    st = rec.status()
    assert st["recording"] is False and len(st["recordings"]) == 1
    path = os.path.join(rec_dir(tmp_path), st["recordings"][0]["name"])
    data = json.load(open(path))
    assert len(data["snapshots"]) >= 2
    assert data["procs_start"] == data["procs_end"] == [{"pid": 1, "label": "x"}]
    assert any(q["path"] == "/api/fs/list?path" for q in data["requests"])
    assert "secret" not in json.dumps(data)
    assert {"groups", "sockets"} <= set(data)


def rec_dir(tmp_path):
    return str(tmp_path / "perf-recordings")


def test_retention_keeps_last_ten(rec, tmp_path):
    d = rec_dir(tmp_path)
    os.makedirs(d)
    for i in range(12):
        open(os.path.join(d, f"2020-01-01T00-00-{i:02d}.json"), "w").write("{}")
    rec.start()
    rec.join(5)
    names = sorted(os.listdir(d))
    assert len(names) == 10 and "2020-01-01T00-00-00.json" not in names


def test_status_reports_seconds_left_while_recording(rec):
    rec.duration = 5
    rec.start()
    assert 0 < rec.status()["seconds_left"] <= 5
    rec.stop()
    rec.join(5)


def test_endpoints_are_guarded(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    monkeypatch.setattr(perfrecord, "recordings_dir", lambda: str(tmp_path / "pr"))
    client = TestClient(create_app(start_dir=str(tmp_path)))
    assert client.post("/api/system/record").status_code in (400, 403)
    assert client.get("/api/system/record").json()["recording"] is False
    monkeypatch.setattr(perfrecord._RECORDER, "duration", 0.05)
    monkeypatch.setattr(perfrecord._RECORDER, "interval", 0.02)
    assert client.post("/api/system/record", headers=HDRS).json()["recording"] is True
    perfrecord._RECORDER.join(5)
    assert len(client.get("/api/system/record").json()["recordings"]) == 1
