"""The queue: rows in the ONE bottom-right card for work about to run or running now.

It used to be a card of its own stacked on top of the download manager, and it is
not any more — "this queue and notification thing should be same no? why duplicate
popups? just replace the queue -> thinking -> done" (Akshil, 2026-08-17). One
container, one header, one count, one lifecycle inside it:

    queued -> starting -> running -> finished / failed

Two halves to test, and they are tested in the two ways they can be:

* the ENDPOINT it reads (`GET /api/schedule/queue`), against a live app — what is
  in each of its three lists, and above all what is NOT: a message scheduled for
  later today is not queued, it is scheduled ("show me the queued that are like
  in the current time or past time, not future time").
* the COMPONENTS, structurally, like the template suites: that the link a row
  offers is the app's own `explorerUrl` rather than a path assembled in the card,
  that a refusal is spoken rather than swallowed, that an empty card is no card at
  all, and that there is exactly ONE card in that corner.

The row-level rules (ordering, dedupe, the header count, what Cancel all counts)
are unit-tested in frontend/src/shell/queue-dock-lib.test.ts, where they are pure.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_spawn, schedule, schedule_wake
from fused_render.server import create_app

WRITE = {"X-Fused": "1"}

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FRONT = os.path.join(_ROOT, "frontend", "src")
# The queue half (rows, polling, Cancel all) and the card it fills (plate, header,
# count, one list, collapse, Clear).
_DOCK = os.path.join(_FRONT, "shell", "QueueDock.tsx")
_CARD = os.path.join(_FRONT, "platform", "ui", "DownloadManager.tsx")
_HOST = os.path.join(_FRONT, "platform", "ui", "NotificationHost.tsx")
_CSS = os.path.join(_FRONT, "styles", "notifications.css")


def _at(seconds: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """A store, a wake stub and a clean event log per test — the same isolation
    test_schedule_queue.py sets up, for the same process-global reasons."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_SCHEDULE_MAX_LATE", raising=False)
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)
    schedule._events.clear()
    schedule._watched.clear()
    yield
    schedule._events.clear()
    schedule._watched.clear()


@pytest.fixture()
def target(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def spawned(monkeypatch):
    calls = []

    def fake_spawn(target, prompt, permission_mode, session_id=""):
        calls.append({"message": prompt, "session_id": session_id})
        return {"run_id": f"r-{len(calls)}"}

    monkeypatch.setattr(claude_spawn, "spawn_helper", fake_spawn)
    monkeypatch.setattr(schedule, "_watch_turn", lambda entry, run_id: None)
    return calls


# --------------------------------------------------------------- the endpoint


def test_the_dock_shows_past_due_only(client, target):
    """The line the whole feature turns on. Two messages, one overdue and one for
    later today: only the overdue one is queued, because "queued" means the
    scheduler is about to take it — not "scheduled at some point"."""
    overdue = client.post("/api/schedule", headers=WRITE,
                          json={"target": str(target), "message": "overdue",
                                "due": _at(-600).isoformat()}).json()["entry"]
    client.post("/api/schedule", headers=WRITE,
                json={"target": str(target), "message": "this afternoon",
                      "due": _at(4 * 3600).isoformat()})

    body = client.get("/api/schedule/queue").json()
    assert [e["id"] for e in body["queued"]] == [overdue["id"]]
    assert [e["message"] for e in body["queued"]] == ["overdue"]


def test_a_live_turn_is_listed_with_what_a_link_needs(client, target, spawned):
    """`live` is what the dock draws for a run already going, and it exists for
    one reason: a run parked on a permission prompt was visible in the job
    registry and unreachable, because a job row knows a title and a status line
    and not WHERE the session is. These entries carry the target and the session
    the turn landed in."""
    entry = schedule.create(str(target), "get me something", _at(-60))
    schedule.tick()

    body = client.get("/api/schedule/queue").json()
    live = body["live"]
    assert [e["id"] for e in live] == [entry["id"]]
    assert live[0]["target"] == str(target)
    assert live[0]["state"] == "sent"
    # and it is out of the queue: it has been claimed and spawned
    assert body["queued"] == []


def test_a_finished_turn_leaves_the_dock(client, target, spawned):
    """`live` is work IN FLIGHT, not history. A turn that ended has a `turn`
    verdict written on it, and the dock is a picture of what is about to happen —
    the job registry's row is where the outcome is reported."""
    entry = schedule.create(str(target), "already done", _at(-60))
    schedule.tick()
    schedule._update(entry["id"], turn="ok")

    assert client.get("/api/schedule/queue").json()["live"] == []


def test_the_queue_read_stays_open_and_changes_nothing(client, target):
    """Merely LOOKING at the queue must not change what runs — the tick owns every
    state change. And the read is unguarded, like every other read."""
    client.post("/api/schedule", headers=WRITE,
                json={"target": str(target), "message": "overdue",
                      "due": _at(-600).isoformat()})
    before = schedule.list_entries()
    assert client.get("/api/schedule/queue").status_code == 200
    assert client.get("/api/schedule/queue").status_code == 200
    assert schedule.list_entries() == before


# --------------------------------------------------------------- the component


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def dock() -> str:
    return _read(_DOCK)


@pytest.fixture(scope="module")
def card() -> str:
    return _read(_CARD)


def test_every_row_offers_the_way_there(dock):
    """The feature request underneath the whole card: a run parked on a permission
    prompt could not be found. The link is `explorerUrl` — the app's one answer to
    "open this session" — never a path assembled in this component."""
    assert "explorerUrl(entry.target," in dock
    assert "Open in Explorer" in dock
    assert "/explorer/view" not in dock, "the URL must not be re-derived here"
    # the session the turn LANDED in first, the one it was told to resume second
    assert "entry.claude_session_id || entry.session_id" in dock


def test_the_card_reads_the_server_s_queue_and_filters_nothing(dock):
    """What counts as queued is the server's answer. A client-side filter on due
    time would be a second definition of the word, free to drift from the one the
    scheduler acts on."""
    assert "getScheduleQueue()" in dock
    assert "/api/schedule\"" not in dock, "the listing is not this card's feed"
    assert "Date.now()" not in dock, "the card must not decide what is due"


def test_a_refusal_is_spoken(dock):
    """`cancelQueued` answers with two lists because partial success is the normal
    outcome — cancelling races the claim. Dropping the refused half teaches the
    user the button lies."""
    assert "cancelOutcome(" in dock
    assert "r.refused" in dock
    assert 'cancelQueued("all")' in dock, "Cancel all lives here and nowhere else"


def test_an_empty_card_is_no_card(card):
    """Not an empty state, not a header saying "nothing queued": a picture of work
    in progress has nothing to draw when there is none. The rule is the CARD's now
    and applies once — both halves have to be empty, because a queue row with no
    jobs is still work worth a card, and a job with no queue row always was."""
    assert "if (jobs.length === 0 && queued === 0) return null;" in card


def test_there_is_one_card_not_two(dock, card):
    """The whole point of the merge. The queue contributes ROWS and numbers; the
    plate, the header, the count, the collapse and Clear are the card's, and the
    queue no longer draws any of them."""
    # no second plate, no second header, no second list, no second count
    css = _read(_CSS)
    for gone in ("q-host", "q-head", "q-rows", "q-summary"):
        assert gone not in dock, f"the queue still draws its own {gone}"
        assert f".{gone} {{" not in css, f"the second card's .{gone} rule survives"
    # the rows land in the card's one list, above the job rows
    assert "{queue?.rows}" in card
    assert card.index("{queue?.rows}") < card.index("jobs.map((job)")
    # and the one header count is told about them
    assert "jobsSummary(jobs, count)" in card


def test_the_column_owns_where_it_sits(dock, card):
    """Placement belongs to NotificationHost — neither the queue nor the card
    positions itself, exactly like the server card below them."""
    for gone in ("position: fixed", "position:fixed", "zIndex", "z-index"):
        assert gone not in dock
        assert gone not in card


def test_the_shell_composes_the_card_and_the_host_places_it(dock):
    """platform may not import shell (frontend/scripts/check-boundaries.mjs) and a
    queue row has to speak explorerUrl, which lives in shell. So the dependency
    runs the way the boundary allows: shell imports the card and fills its `queue`
    slot, and the host takes the composed thing as its ONE activity entry.

    Omitted, the bare manager stands in: platform does not come to depend on a
    shell that may not be there."""
    assert 'import DownloadManager from "@platform/ui/DownloadManager"' in dock
    host = _read(_HOST)
    assert "activity?: ReactNode" in host
    assert "{!IS_EMBED && (activity ?? <DownloadManager />)}" in host
    # one entry in the column, not two stacked cards
    assert host.count("<DownloadManager />") == 1
