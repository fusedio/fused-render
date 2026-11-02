"""Bots v3 review fix-ups (PR #2057): the migrated Inbox index points at .fused/inbox, the Super Bot seed waits
for the v3 stamp, and one owner per WORKSPACE ticks routines (registry module docstring)."""
from _bots_conftest import *  # noqa: F401,F403 — app_home (autouse) etc.
import json
import os

import pytest

from fused_render.bots import migrate_v3, registry, store
from fused_render.bots import paths as bpaths


def _touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _legacy(app_home, bid, name, **meta):
    d = os.path.join(str(app_home), "bots", "data", "bots", bid)
    _touch(os.path.join(d, "bot.json"), json.dumps({"id": bid, "name": name, "created": 1.0, **meta}))
    _touch(os.path.join(d, "memory.md"), "remember me")
    return d


def _rows(old_inbox, app_dir):
    """Pre-v3 rows as bot.py _record_artifact wrote them (artifacts_dir was `<bots_root>/<slug>`), plus a build row
    naming an app folder and a torn line."""
    return "".join([
        json.dumps({"ts": 1.0, "name": "out.txt", "path": os.path.join(old_inbox, "20260101-0900-t1", "out.txt"),
                    "kind": "save", "size": 3, "task": "t1", "folder": "20260101-0900-t1"}) + "\n",
        "{not json\n",
        json.dumps({"ts": 2.0, "name": "myapp", "path": app_dir, "kind": "build", "size": 0, "task": "t2",
                    "folder": "apps", "title": "myapp"}) + "\n",
        json.dumps({"ts": 3.0, "name": "b.md", "path": os.path.join(old_inbox, "20260101-0900-t1", "b.md"),
                    "kind": "save", "size": 1, "task": "t1", "folder": "20260101-0900-t1"}) + "\n",
    ])


def _old_inbox(old, app_dir):
    _touch(os.path.join(old, "20260101-0900-t1", "out.txt"), "out")
    _touch(os.path.join(old, "20260101-0900-t1", "b.md"), "b")
    _touch(os.path.join(old, "index.jsonl"), _rows(old, app_dir))


def _check(bid, app_dir):
    inbox = bpaths.bot_inbox(bid)
    with open(os.path.join(inbox, "index.jsonl"), encoding="utf-8") as f:
        lines = f.readlines()
    assert lines[1] == "{not json\n"  # torn line kept verbatim
    assert json.loads(lines[2])["path"] == app_dir  # a build row is never rewritten
    arts = registry.get(bid).artifacts()
    saves = [r for r in arts if r["kind"] == "save"]
    assert sorted(r["name"] for r in saves) == ["b.md", "out.txt"]
    for r in saves:
        assert r["path"].startswith(inbox + os.sep) and os.path.isfile(r["path"])
    assert any(r["kind"] == "build" for r in arts)
    return lines


def test_migrated_inbox_rows_point_under_dot_fused_inbox(app_home, tmp_path):
    bid = "a1b2c3d4"
    root = bpaths.bots_root()
    old = os.path.join(root, "daily-digest")
    app_dir = os.path.join(os.environ["FUSED_RENDER_DIR"], "app", "myapp")
    os.makedirs(app_dir)
    _old_inbox(old, app_dir)
    _legacy(app_home, bid, "Daily Digest", artifacts_dir=old)
    r = migrate_v3.migrate_once()
    assert r["stamped"] is True and r["moved"] == 1
    lines = _check(bid, app_dir)
    # Idempotent: the bot's folder IS the old Inbox's name, so the new paths also start with the old prefix.
    idx = os.path.join(bpaths.bot_inbox(bid), "index.jsonl")
    assert migrate_v3._rewrite_index(idx, [old], bpaths.bot_inbox(bid)) is False
    with open(idx, encoding="utf-8") as f:
        assert f.readlines() == lines
    assert not os.path.exists(idx + ".tmp")


def test_staged_inbox_after_a_crash_is_rewritten_too(app_home):
    # A run that staged the Inbox to `.inbox-<id>/<name>/` and died: the rows still name `<bots_root>/<name>/`
    # (never the stage), and the rerun lifts and rewrites them.
    bid = "b1b2c3d4"
    root = bpaths.bots_root()
    old = os.path.join(root, "renamed-once")
    app_dir = os.path.join(os.environ["FUSED_RENDER_DIR"], "app", "myapp")
    os.makedirs(app_dir)
    _old_inbox(old, app_dir)
    os.makedirs(os.path.join(root, f".inbox-{bid}"))
    os.rename(old, os.path.join(root, f".inbox-{bid}", "renamed-once"))
    _legacy(app_home, bid, "Research", artifacts_dir=old)
    r = migrate_v3.migrate_once()
    assert r["stamped"] is True and r["moved"] == 1
    assert not os.path.exists(os.path.join(root, f".inbox-{bid}"))
    _check(bid, app_dir)


# ----------------------------------------------------------- registry ---
@pytest.fixture
def quiet_owner(monkeypatch):
    """_become_owner without the router, the takeover walk, the import or the wake agent."""
    for name in ("_start_router", "_reconcile", "_import_fusedbot", "sync_wake"):
        monkeypatch.setattr(registry, name, lambda: None)
    monkeypatch.setattr(bpaths, "migrate_layout", lambda: None)


def _supers():
    from fused_render.bots import bot as bm
    return [b for b in store.list_ids() if bm.is_super(store.read_meta(b))]


def test_failed_migration_does_not_seed_a_second_super_bot(app_home, quiet_owner, monkeypatch):
    _legacy(app_home, "c1c2c3c4", "Super Bot", kind="super")
    real = registry._migrate_v3

    def boom():
        raise RuntimeError("migration failed")

    monkeypatch.setattr(registry, "_migrate_v3", boom)
    registry._become_owner()
    assert not os.path.exists(registry.seed_stamp_path())
    assert _supers() == []
    # The next start: the migration lifts the legacy Super Bot, and the seed sees it.
    registry._drop_routines_lock()
    monkeypatch.setattr(registry, "_migrate_v3", real)
    registry._become_owner()
    assert os.path.exists(migrate_v3.stamp_path())
    assert _supers() == ["c1c2c3c4"]


def test_failed_migration_then_success_seeds_once(app_home, quiet_owner, monkeypatch):
    _legacy(app_home, "d1d2d3d4", "Helper")
    real = registry._migrate_v3

    def boom():
        raise RuntimeError("migration failed")

    monkeypatch.setattr(registry, "_migrate_v3", boom)
    registry._become_owner()
    assert not os.path.exists(registry.seed_stamp_path()) and _supers() == []
    registry._drop_routines_lock()
    monkeypatch.setattr(registry, "_migrate_v3", real)
    registry._become_owner()
    assert os.path.exists(registry.seed_stamp_path())
    assert len(_supers()) == 1
    registry._drop_routines_lock()
    registry._become_owner()
    assert len(_supers()) == 1


def test_routines_tick_only_under_the_workspace_claim(app_home, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    ticks, ws_steps = [], []

    class Fake:
        id = "e1e2e3e4"

        def tick_routines(self):
            ticks.append(1)

    monkeypatch.setattr(registry, "all", lambda: [Fake()])
    monkeypatch.setattr(registry, "_sweep_handoffs", lambda: None)
    for name in ("_start_router", "_reconcile", "_import_fusedbot", "sync_wake"):
        monkeypatch.setattr(registry, name, lambda: None)
    monkeypatch.setattr(bpaths, "migrate_layout", lambda: None)
    for name in ("_migrate_v3", "_seed_super", "_sweep_browsers"):
        monkeypatch.setattr(registry, name, lambda n=name: ws_steps.append(n))
    path = os.path.join(bpaths.bots_root(), ".routines.lock")
    other = open(path, "a+", encoding="utf-8")  # another home's owner on the same workspace
    try:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert registry._elect()
        registry._pass()
        registry._pass()
        assert ticks == [] and ws_steps == []  # serves its home, never ticks or writes the workspace
        assert registry.owns()
    finally:
        fcntl.flock(other.fileno(), fcntl.LOCK_UN)
        other.close()
    registry._pass()  # the other owner quit: the claim is won on the next pass
    assert ticks == [1]
    assert ws_steps == ["_migrate_v3", "_seed_super", "_sweep_browsers"]
    registry._pass()
    assert ticks == [1, 1] and len(ws_steps) == 3  # the workspace steps ran once
    registry._drop_routines_lock()
    probe = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # released with the ownership
    finally:
        probe.close()


def test_rerun_after_lift_before_rewrite_still_rewrites(app_home):
    # A run that lifted the Inbox into the bot's folder (named like the old Inbox) and died before the rewrite and
    # the bot.json: the stage is gone and the pin is now the bot's folder, yet the rows still get rewritten.
    bid = "f1f2f3f4"
    root = bpaths.bots_root()
    old = os.path.join(root, "daily-digest")
    app_dir = os.path.join(os.environ["FUSED_RENDER_DIR"], "app", "myapp")
    os.makedirs(app_dir)
    inbox = os.path.join(old, ".fused", "inbox")
    _touch(os.path.join(inbox, "20260101-0900-t1", "out.txt"), "out")
    _touch(os.path.join(inbox, "20260101-0900-t1", "b.md"), "b")
    _touch(os.path.join(inbox, "index.jsonl"), _rows(old, app_dir))
    _touch(os.path.join(old, ".fused", ".bot-id"), bid)  # place() marked it as this bot's before the crash
    _legacy(app_home, bid, "Daily Digest", artifacts_dir=old)
    r = migrate_v3.migrate_once()
    assert r["stamped"] is True and r["moved"] == 1
    assert bpaths.bot_inbox(bid) == inbox
    _check(bid, app_dir)
