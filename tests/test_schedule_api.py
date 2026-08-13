"""Scheduled Claude messages — the HTTP surface (server/routers/schedule.py).

GET /api/schedule lists, POST /api/schedule schedules, POST /api/schedule/cancel
withdraws. The model's own rules are covered in test_schedule.py; what is tested
here is what only this layer does: the D3 write guard, the mount refusal, the
two ways to say *when*, and ValueError arriving as a 400.

Nothing here spawns a real claude — no test lets a message come due.
"""
import pytest
from fastapi.testclient import TestClient

from fused_render import schedule, schedule_wake
from fused_render.server import create_app
from fused_render.shell import mounts as mounts_mod

WRITE = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    return d


# --------------------------------------------------------------------- guard

def test_the_writes_carry_the_d3_guard(client, target):
    """Both POSTs: one schedules an unattended agent turn, the other stops one.
    Neither may be fired blind by a foreign page."""
    unguarded = client.post("/api/schedule",
                            json={"target": str(target), "message": "hi",
                                  "delay_seconds": 600})
    assert unguarded.status_code == 403
    assert client.post("/api/schedule/cancel", json={"id": "x"}).status_code == 403
    # the read is open, like every other read endpoint
    assert client.get("/api/schedule").status_code == 200


# ------------------------------------------------------------------ creating

def test_schedule_then_list_then_cancel(client, target):
    created = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "ship it",
                                "delay_seconds": 900})
    assert created.status_code == 200
    entry = created.json()["entry"]
    assert entry["state"] == schedule.PENDING

    listed = client.get("/api/schedule").json()
    assert [e["id"] for e in listed["entries"]] == [entry["id"]]
    # the UI cannot explain a `missed` entry without the bound, so it rides along
    assert listed["max_late_seconds"] == schedule.max_late_seconds()
    assert "auto" in listed["permission_modes"]

    cancelled = client.post("/api/schedule/cancel", headers=WRITE,
                            json={"id": entry["id"]})
    assert cancelled.status_code == 200
    assert cancelled.json()["entry"]["state"] == schedule.CANCELLED


def test_due_and_delay_are_exclusive(client, target):
    """Both, or neither, and the request cannot half-mean each."""
    both = client.post("/api/schedule", headers=WRITE,
                       json={"target": str(target), "message": "hi",
                             "delay_seconds": 60, "due": "2030-01-01T00:00:00Z"})
    assert both.status_code == 400
    assert "exactly one" in both.json()["error"]

    neither = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "hi"})
    assert neither.status_code == 400


def test_an_explicit_due_time_is_accepted(client, target):
    res = client.post("/api/schedule", headers=WRITE,
                      json={"target": str(target), "message": "hi",
                            "due": "2038-01-19T03:14:07Z"})
    assert res.status_code == 200
    assert res.json()["entry"]["due"] == "2038-01-19T03:14:07+00:00"


@pytest.mark.parametrize("body,expect", [
    # `TARGET` is substituted with the real target dir; a case that omits the key
    # is testing exactly that omission.
    ({"message": "hi", "delay_seconds": 60}, "target"),
    ({"target": "  ", "message": "hi", "delay_seconds": 60}, "target"),
    ({"target": "/nope/nope", "message": "hi", "delay_seconds": 60}, "no such file"),
    ({"target": "TARGET", "message": "   ", "delay_seconds": 60}, "message"),
    ({"target": "TARGET", "delay_seconds": 60}, "message"),
    ({"target": "TARGET", "message": "hi", "delay_seconds": -5}, "positive"),
    ({"target": "TARGET", "message": "hi", "delay_seconds": "soon"}, "number"),
    ({"target": "TARGET", "message": "hi", "due": "whenever"}, "ISO 8601"),
])
def test_bad_requests_are_400s_that_say_why(client, target, body, expect):
    body = {k: (str(target) if v == "TARGET" else v) for k, v in body.items()}
    res = client.post("/api/schedule", headers=WRITE, json=body)
    assert res.status_code == 400
    assert expect in res.json()["error"]
    assert schedule.list_entries() == []  # a refused request stores nothing


def test_a_mount_backed_target_is_refused(client, target, monkeypatch):
    """The refusal the claude template's own gate exists for: the bytes under a
    mount come from a remote over FUSE, and a scheduled turn is an agent turned
    loose on the path. Scheduling one would route around that gate."""
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: str(p).startswith(str(target)))

    res = client.post("/api/schedule", headers=WRITE,
                      json={"target": str(target), "message": "hi",
                            "delay_seconds": 60})
    assert res.status_code == 400
    assert "remote mount" in res.json()["error"]
    assert schedule.list_entries() == []  # nothing stored


# ----------------------------------------------------------------- cancelling

def test_cancelling_an_unknown_id_is_a_404(client):
    res = client.post("/api/schedule/cancel", headers=WRITE, json={"id": "nope"})
    assert res.status_code == 404
    assert client.post("/api/schedule/cancel", headers=WRITE,
                       json={}).status_code == 400


# -------------------------------------------------------------------- wiring

def test_the_loop_is_not_started_by_building_the_app(tmp_path, monkeypatch):
    """A startup event, not the create_app body. The loop SENDS things, and its
    first tick fires everything overdue — under the create_app body every test
    that builds an app would spawn whatever the developer's store held."""
    started = []
    monkeypatch.setattr(schedule, "start", lambda: started.append(True))

    create_app(start_dir=str(tmp_path))
    assert started == []

    with TestClient(create_app(start_dir=str(tmp_path))):
        pass
    assert started == [True]
