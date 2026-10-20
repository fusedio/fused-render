"""`POST /api/tasks/seen` and the `seen` map on `GET /api/tasks/pulse` (D1386).

The sidebar's "seen it finished" map moved off localStorage
(`fused-render:tasks-seen`): the desktop app walks ports 1777..1787 then an
ephemeral one, each a new origin with empty storage, so the map was lost on
relaunch. It now lives in tasks `read.json` as the top-level `__seen_done__`
key, a whole-map replace. Nothing here reads the real ~/.claude.
"""
import pytest
from fastapi.testclient import TestClient

from fused_render import tasks_store
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod

H = {"X-Fused": "1"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture(autouse=True)
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()
    yield
    tasks_mod.reset_cache()
    sessions_mod._HEAD_CACHE.clear()


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    from fused_render import schedule_wake
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _pulse_seen(client):
    r = client.get("/api/tasks/pulse")
    assert r.status_code == 200, r.text
    return r.json()["seen"]


def test_the_pulse_carries_an_empty_map_before_anything_is_seen(client):
    assert _pulse_seen(client) == {}


def test_seen_round_trips_through_the_pulse_and_replaces_whole(client):
    r = client.post("/api/tasks/seen", json={"seen": {"sess-a": 1755300000.5, "sess-b": 7}}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "seen": {"sess-a": 1755300000.5, "sess-b": 7}}
    assert _pulse_seen(client) == {"sess-a": 1755300000.5, "sess-b": 7}

    # Whole-map replace: a key the client dropped is gone, not merged back.
    r = client.post("/api/tasks/seen", json={"seen": {"sess-b": 9}}, headers=H)
    assert r.json() == {"ok": True, "seen": {"sess-b": 9}}
    assert _pulse_seen(client) == {"sess-b": 9}

    r = client.post("/api/tasks/seen", json={"seen": {}}, headers=H)
    assert r.json() == {"ok": True, "seen": {}}
    assert _pulse_seen(client) == {}


def test_seen_needs_the_x_fused_header(client):
    r = client.post("/api/tasks/seen", json={"seen": {"a": 1}})
    assert r.status_code == 403
    assert _pulse_seen(client) == {}


@pytest.mark.parametrize("body", [
    {},
    {"seen": None},
    {"seen": [1, 2]},
    {"seen": "a"},
    {"seen": {"": 1}},
    {"seen": {"a": "5"}},
    {"seen": {"a": True}},
    {"seen": {"a": None}},
    [1],
])
def test_bad_shapes_are_400(client, body):
    r = client.post("/api/tasks/seen", json=body, headers=H)
    assert r.status_code == 400, r.text
    assert _pulse_seen(client) == {}


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_400(client, token):
    r = client.post("/api/tasks/seen", content=('{"seen": {"a": %s}}' % token).encode(),
                    headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text


def test_the_seen_map_and_the_read_marks_share_read_json_without_touching(client):
    """Top-level key, not a task record: the per-message marks and their
    compaction never see it, and a mark never drops it (one `_update` lock)."""
    tasks_store.set_seen_done({"sess-a": 5})
    tasks_store.mark_read_many("sess-a", ["MSG-001", "MSG-002"])
    state = tasks_store.read_state()
    assert state[tasks_store.SEEN_KEY] == {"sess-a": 5}
    assert state["sess-a"]["read_floor"] == 2
    assert tasks_store.read_count(state, "sess-a", 3) == 2
    assert tasks_store.seen_done() == {"sess-a": 5}

    # Replacing the map leaves the task's marks alone, and erasing the task's
    # read record leaves the map alone.
    tasks_store.set_seen_done({"sess-a": 6})
    assert tasks_store.read_state()["sess-a"]["read_floor"] == 2
    tasks_store.forget_session("sess-a")
    assert "sess-a" not in tasks_store.read_state()
    assert tasks_store.seen_done() == {"sess-a": 6}


def test_a_corrupt_stored_map_reads_as_its_valid_entries(state_dir):
    (state_dir / tasks_store.READ_FILE).write_text(
        '{"__seen_done__": {"a": 1, "b": "x", "": 2, "c": true}}', encoding="utf-8")
    assert tasks_store.seen_done() == {"a": 1}
    (state_dir / tasks_store.READ_FILE).write_text('{"__seen_done__": [1]}', encoding="utf-8")
    assert tasks_store.seen_done() == {}
