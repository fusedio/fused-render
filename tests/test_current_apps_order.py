"""The desk lists by RECENCY (owner, 2026-10-23): newest task activity under
each folder first, a row with no tasks by when it was added, ties in stored
(newest-added-first) order. No user order, no drag, no order route. A legacy
oldest-first file reads reversed until its first write stamps it."""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import current_apps
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server import create_app
from fused_render.shell import storage

FUSED = {"X-Fused": "1"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_DIR", str(tmp_path / "Fused"))
    dirs = []
    for name in ("a", "b", "c"):
        d = tmp_path / "apps" / name
        d.mkdir(parents=True)
        dirs.append(canonical_fs_path(str(d)).rstrip("/"))
    return tmp_path, dirs


def _paths():
    return [a["path"] for a in current_apps.list_apps()]


def test_add_inserts_at_top(env):
    _, (a, b, c) = env
    for p in (a, b, c):
        assert current_apps.add(p) is True
    assert _paths() == [c, b, a]
    assert current_apps.add(a) is False  # already there: keeps its place
    assert _paths() == [c, b, a]
    raw = json.loads(open(os.path.join(storage.home_dir(), "current_apps.json")).read())
    assert raw["order"] == current_apps.ORDER_STAMP


def test_observe_inserts_newest_on_top(env, monkeypatch):
    _, (a, b, c) = env
    current_apps.add(a)
    folders = {"pb": b, "pc": c}
    monkeypatch.setattr(current_apps, "app_dir_for", lambda p: folders.get(p))
    # The listing arrives newest first: c's task is newer than b's.
    rows = [{"key": "k2", "project": "pc", "status": "done"},
            {"key": "k1", "project": "pb", "status": "done"}]
    current_apps.observe(rows)
    assert _paths() == [c, b, a]


def test_legacy_file_reads_reversed_then_is_stamped(env):
    _, (a, b, c) = env
    path = os.path.join(storage.home_dir(), "current_apps.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:  # today's added order: oldest first, no stamp
        json.dump({"apps": [{"path": a, "addedAt": "t", "openedAt": 1.0},
                            {"path": b, "addedAt": "t", "openedAt": 1.0}], "seen": []}, f)
    assert _paths() == [b, a]
    current_apps.add(c)
    assert _paths() == [c, b, a]
    raw = json.loads(open(path).read())
    assert raw["order"] == current_apps.ORDER_STAMP
    assert [x["path"] for x in raw["apps"]] == [c, b, a]


def test_lists_by_newest_task_activity(env, monkeypatch):
    """A task happening under an older app lifts it above the newer ones; a
    running task counts; an archived one does not; a row with no live task
    falls back to when it was added — and opening an app moves nothing."""
    _, (a, b, c) = env
    for p in (a, b, c):
        current_apps.add(p)
    assert _paths() == [c, b, a]
    # Activity is matched by containment (`is_under`): a task on a subfolder
    # of the app is the app's.
    now = current_apps._now_epoch()
    rows = [{"key": "ka", "project": a, "status": "done", "happened_at": now + 10},
            {"key": "kb", "project": b + "/sub", "status": "in_progress", "happened_at": now + 5}]
    current_apps.observe(rows)
    assert _paths() == [a, b, c]
    assert {x["path"]: x["active_at"] for x in current_apps.list_apps()} == {
        a: now + 10, b: now + 5, c: 0.0}
    # Opening is not activity: the list stands.
    current_apps.mark_opened(c, now + 100)
    assert _paths() == [a, b, c]
    # Archiving a's task drops its activity; it falls back to added (oldest).
    rows[0]["status"] = "archived"
    current_apps.observe(rows)
    assert _paths() == [b, c, a]
    # OLD activity still outranks a fresh `addedAt`: a task that finished long
    # ago under c (discovered just now, so its addedAt is newer) sorts c by
    # that task, under b's live one — not by the discovery time.
    rows.append({"key": "kc", "project": c, "status": "done", "happened_at": now - 1000})
    current_apps.observe(rows)
    assert _paths() == [b, a, c]


def test_get_lists_by_recency_and_there_is_no_order_route(env):
    tmp_path, (a, b, c) = env
    for p in (a, b, c):
        current_apps.add(p)
    current_apps.observe([{"key": "ka", "project": a, "status": "done",
                           "happened_at": current_apps._now_epoch() + 10}])
    client = TestClient(create_app(start_dir=str(tmp_path)))
    body = client.get("/api/current-apps").json()
    assert [x["path"] for x in body["apps"]] == [a, c, b]
    r = client.post("/api/current-apps/order", json={"paths": [b, c, a]}, headers=FUSED)
    assert r.status_code in (404, 405)


def test_reading_the_task_anywhere_clears_the_desks_dot(env):
    """One unread state (owner, 2026-11-11): the desk's dot lights for a task
    that finished since the app was opened AND is still unread; a task read
    in the peek, the rail or the chat (its row's `unread` falls to 0) takes
    the dot with it on the next pass. A row without a count keeps the old
    finished-since-opened rule."""
    _, (a, _b, _c) = env
    current_apps.add(a)
    now = current_apps._now_epoch()
    current_apps.mark_opened(a, now)
    row = {"key": "ka", "project": a, "status": "done", "happened_at": now + 10, "unread": 2}
    current_apps.observe([row])
    assert current_apps.list_apps()[0]["unread"] is True
    row["unread"] = 0
    current_apps.observe([row])
    assert current_apps.list_apps()[0]["unread"] is False
    # No count at all: the older rule, finished since opened.
    del row["unread"]
    current_apps.observe([row])
    assert current_apps.list_apps()[0]["unread"] is True
    # Opening the app still clears it, read or not.
    row["unread"] = 3
    current_apps.mark_opened(a, now + 20)
    current_apps.observe([row])
    assert current_apps.list_apps()[0]["unread"] is False
