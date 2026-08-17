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


@pytest.fixture(autouse=True)
def clean_event_log(monkeypatch):
    """The event log and its delivery mark are process-global and in memory, so
    they would otherwise carry one test's events into the next one's assertions."""
    schedule._events.clear()
    monkeypatch.setattr(schedule, "_delivered", 0)
    yield
    schedule._events.clear()


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

def test_the_event_log_is_its_own_light_endpoint(client, target):
    """Separate from the listing for the reason mount-health's log is: the shell
    polls this one app-wide, forever, and it must not carry the page's payload."""
    schedule._emit(schedule.EVENT_MISSED,
                   {"id": "e1", "target": str(target), "message": "stale"},
                   "not sent")

    res = client.get("/api/schedule/events")
    assert res.status_code == 200
    body = res.json()
    assert list(body) == ["events"]  # the entries are NOT in here
    assert body["events"][0]["kind"] == schedule.EVENT_MISSED
    assert body["events"][0]["message"] == "stale"


def test_the_ack_drains_and_is_guarded(client, target):
    """A POST, not a drain-on-read: a GET with that side effect would let any
    page the user visits silently consume their notifications with a no-cors
    fetch, which is what the D3 header guard exists to refuse."""
    schedule._emit(schedule.EVENT_FAILED,
                   {"id": "e1", "target": str(target), "message": "boom"}, "why")
    event_id = client.get("/api/schedule/events").json()["events"][0]["id"]

    # unguarded: refused, and the notification survives
    assert client.post("/api/schedule/events/ack", json={"id": event_id}).status_code == 403
    assert len(client.get("/api/schedule/events").json()["events"]) == 1

    acked = client.post("/api/schedule/events/ack", headers=WRITE, json={"id": event_id})
    assert acked.status_code == 200
    assert acked.json()["delivered"] == event_id
    assert client.get("/api/schedule/events").json()["events"] == []


def test_the_ack_wants_a_real_event_id(client):
    for body in ({}, {"id": "3"}, {"id": None}, {"id": True}):
        res = client.post("/api/schedule/events/ack", headers=WRITE, json=body)
        assert res.status_code == 400, body
        assert "id" in res.json()["error"]


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


# -------------------------------------------------- the form's three fields

def test_the_forms_three_new_fields_survive_the_round_trip(client, target):
    """The regression this test exists for: `/api/schedule` takes a raw dict, so
    a field the endpoint does not name is silently dropped — no 400, just gone.
    All three of the form's newest controls went that way, which made the user's
    own title never persist, description write-only, and "New task each run" a
    checkbox that did nothing.

    The form reads them back under these exact names when it opens on an
    existing task, so the names are part of the contract rather than an internal
    detail."""
    created = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "pull news",
                                "repeats": "0 9 * * *",
                                "title": "Morning digest",
                                "description": "Reads the feeds",
                                "new_task_each_run": True})
    assert created.status_code == 200
    entry = created.json()["entry"]
    assert entry["title"] == "Morning digest"
    assert entry["description"] == "Reads the feeds"
    assert entry["new_task_each_run"] is True

    listed = client.get("/api/schedule").json()["entries"]
    template = next(e for e in listed if e["id"] == entry["id"])
    assert template["title"] == "Morning digest"
    # The occurrence carries the words and, because the box is ticked, opens its
    # own session rather than inheriting the template's thread.
    occurrence = next(e for e in listed if e.get("template_id") == entry["id"])
    assert occurrence["title"] == "Morning digest"
    assert occurrence["description"] == "Reads the feeds"
    assert occurrence["session_id"] == ""


def test_the_three_fields_are_optional_and_never_a_400(client, target):
    """The form omits them when blank or unticked, and a stray null must still
    schedule the message — they are labels and a threading preference, not
    inputs a request can be refused over."""
    for body in ({}, {"title": None, "description": None,
                      "new_task_each_run": None}):
        res = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "hi",
                                "delay_seconds": 600, **body})
        assert res.status_code == 200
        entry = res.json()["entry"]
        assert entry["title"] == ""
        assert entry["description"] == ""
        assert entry["new_task_each_run"] is False


def test_session_learned_survives_the_round_trip_and_is_never_invented(
        client, target):
    """The same drop, in the one place it costs a thread: an edit is cancel +
    re-create, so the re-create is where a learned session's provenance has to
    be re-stated. A router that quietly ignored `session_learned` would make
    every edit look like a chat handoff, and the next repeat would refuse the
    task's own thread."""
    kept = client.post("/api/schedule", headers=WRITE,
                       json={"target": str(target), "message": "pull news",
                             "repeats": "0 9 * * *",
                             "session_id": "sess-learned",
                             "session_learned": True})
    assert kept.status_code == 200
    entry = kept.json()["entry"]
    assert entry["session_learned"] is True
    occurrence = next(e for e in client.get("/api/schedule").json()["entries"]
                      if e.get("template_id") == entry["id"])
    assert occurrence["session_id"] == "sess-learned"
    assert occurrence["session_learned"] is True

    # A chat handoff says nothing, so nothing is claimed for it — and a stray
    # null is a missing opinion, not a 400.
    for body in ({}, {"session_learned": None}):
        res = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "hi",
                                "delay_seconds": 600,
                                "session_id": "sess-chat", **body})
        assert res.status_code == 200
        assert res.json()["entry"]["session_learned"] is False


# ----------------------------------------------------------------- recurring

def test_repeats_creates_a_template_and_lists_its_projection(client, target):
    res = client.post("/api/schedule", headers=WRITE,
                      json={"target": str(target), "message": "daily report",
                            "repeats": "0 * * * *"})
    assert res.status_code == 200
    entry = res.json()["entry"]
    assert entry["state"] == schedule.RECURRING
    assert entry["repeats"] == "0 * * * *"

    listed = client.get("/api/schedule").json()["entries"]
    template = next(e for e in listed if e["id"] == entry["id"])
    # The projection rides along on the GET — server-side cron math for the
    # calendar — and the materialized first occurrence is in the list too.
    assert len(template["upcoming"]) > 0
    assert any(e.get("template_id") == entry["id"] and e["state"] == "pending"
               for e in listed)


def test_repeats_rejects_bad_cron_and_extra_when(client, target):
    bad = client.post("/api/schedule", headers=WRITE,
                      json={"target": str(target), "message": "x",
                            "repeats": "every morning"})
    assert bad.status_code == 400
    assert "cron" in bad.json()["error"]

    both = client.post("/api/schedule", headers=WRITE,
                       json={"target": str(target), "message": "x",
                             "repeats": "0 * * * *", "delay_seconds": 60})
    assert both.status_code == 400
    assert "repeats" in both.json()["error"]


def test_restore_is_guarded_and_unskips_over_http(client, target):
    entry = client.post("/api/schedule", headers=WRITE,
                        json={"target": str(target), "message": "x",
                              "repeats": "0 * * * *"}).json()["entry"]
    listed = client.get("/api/schedule").json()["entries"]
    occurrence = next(e for e in listed if e.get("template_id") == entry["id"])
    client.post("/api/schedule/cancel", headers=WRITE, json={"id": occurrence["id"]})

    unguarded = client.post("/api/schedule/restore", json={"id": occurrence["id"]})
    assert unguarded.status_code == 403  # the D3 guard, same as every other write

    res = client.post("/api/schedule/restore", headers=WRITE,
                      json={"id": occurrence["id"]})
    assert res.status_code == 200
    assert res.json()["entry"]["state"] == "pending"

    again = client.post("/api/schedule/restore", headers=WRITE,
                        json={"id": occurrence["id"]})
    assert again.status_code == 404
