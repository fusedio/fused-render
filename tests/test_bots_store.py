"""fused_render.bots.store: bot.json, events.jsonl, the usage ledger and
summary, the Markdown export, builds.json. conftest points FUSED_RENDER_HOME
at a tmp dir, so every path here lands under it."""
from _bots_conftest import *  # noqa: F401,F403 — FusedBot's conftest fixtures (app_home, client, …)
import json
import os
import threading
import time

import pytest

from fused_render.bots import paths as bpaths
from fused_render.bots import store


@pytest.fixture(autouse=True)
def _fresh_usage_cache():
    store.usage_reset_cache()
    yield
    store.usage_reset_cache()


# ------------------------------------------------------------------- meta ---
def test_paths_live_under_app_home(app_home):
    # The OpenBot `.fused/` layout, one-to-one, so a copy of that folder migrates.
    assert store.bot_dir("b1") == os.path.join(str(app_home), "bots", "data", "bots", "b1")
    assert bpaths.bot_cache_dir("b1") == os.path.join(str(app_home), "bots", "cache", "bots", "b1")
    assert bpaths.usage_path() == os.path.join(str(app_home), "bots", "data", "usage.jsonl")
    assert bpaths.builds_path() == os.path.join(str(app_home), "bots", "data", "builds.json")
    assert bpaths.slow_log_path() == os.path.join(str(app_home), "bots", "cache", "slow.jsonl")


def test_meta_round_trip_and_list_ids():
    assert store.list_ids() == []
    store.write_meta("b2", {"id": "b2", "name": "Two"})
    store.write_meta("b1", {"id": "b1", "name": "Ünï", "n": [1, 2]})
    os.makedirs(store.bot_dir("no-meta"))  # a folder without bot.json is not a bot
    assert store.read_meta("b1") == {"id": "b1", "name": "Ünï", "n": [1, 2]}
    assert store.list_ids() == ["b1", "b2"]
    store.write_meta("b1", {"id": "b1", "name": "Renamed"})
    assert store.read_meta("b1")["name"] == "Renamed"
    assert [n for n in os.listdir(store.bot_dir("b1")) if n.endswith(".tmp")] == []
    with pytest.raises(OSError):
        store.read_meta("missing")


def test_write_meta_is_safe_under_concurrent_writers():
    errors = []

    def writer(i):
        try:
            for j in range(30):
                store.write_meta("c", {"i": i, "j": j})
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert set(store.read_meta("c")) == {"i", "j"}
    assert [n for n in os.listdir(store.bot_dir("c")) if n.endswith(".tmp")] == []


def test_write_json_atomic_cleans_up_on_failure(tmp_path):
    p = str(tmp_path / "x.json")
    with pytest.raises(TypeError):
        store.write_json_atomic(p, {"bad": object()})
    assert os.listdir(tmp_path) == [] or os.listdir(tmp_path) == ["x.json"]
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


# ----------------------------------------------------------------- events ---
def test_events_append_count_and_since_by_line():
    p = store.events_path("b1")
    assert store.count_events(p) == 0
    assert list(store.iter_events(p)) == []
    assert store.events_since(p, 0) == []
    for i in range(1, 4):
        store.append_event(p, {"seq": i, "role": "user", "text": f"m{i}"})
    with open(p, "a") as f:
        f.write('{"torn": \n')  # a torn line still counts as a line
    store.append_event(p, {"seq": 2, "role": "done", "text": "from another writer, lower seq"})
    assert store.count_events(p) == 5
    assert [i for i, _ in store.iter_events(p)] == [0, 1, 2, 4]
    assert [e["text"] for e in store.events_since(p, 0)] == ["m1", "m2", "m3", "from another writer, lower seq"]
    assert [e["text"] for e in store.events_since(p, 2)] == ["m3", "from another writer, lower seq"]
    assert [e["text"] for e in store.events_since(p, 3)] == ["from another writer, lower seq"]
    assert store.events_since(p, 5) == []
    assert store.events_since(p, "junk") == store.events_since(p, 0)


# ------------------------------------------------------------------ usage ---
def _ledger(rows):
    p = bpaths.usage_path()
    with open(p, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return p


def _noon_today():
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))


def test_usage_summary_math():
    now = _noon_today()
    store.write_meta("b1", {"id": "b1", "name": "Alpha"})
    store.write_meta("idle", {"id": "idle", "name": "Idle"})
    _ledger([
        {"ts": now - 60, "bot": "b1", "name": "old name", "model": "sonnet", "origin": "manual", "task": "t1", "ok": True},
        {"ts": now - 2 * 3600, "bot": "b1", "name": "", "model": "haiku", "origin": "routine", "task": "t1", "ok": False},
        {"ts": now - 13 * 3600, "bot": "b1", "name": "", "model": "sonnet", "origin": "manual", "task": "t2", "ok": True},
        {"ts": now - 30, "bot": "gone", "name": "Old Bot", "model": "opus", "origin": "weird", "task": "t3", "ok": True},
        {"ts": now - 2 * 86400, "bot": "ghost", "model": "opus", "origin": "manual", "task": "t4", "ok": False},
        {"ts": now - 8 * 86400, "bot": "b1", "model": "opus", "origin": "manual", "task": "stale", "ok": True},
    ])
    with open(bpaths.usage_path(), "a") as f:
        f.write("not json\n[1, 2]\n")
    s = store.usage_summary(now=now)
    assert set(s) == {"today", "hour", "errors", "origin", "bots", "tasks", "hours", "days"}
    assert s["today"] == 3 and s["hour"] == 2 and s["errors"] == 1
    assert s["origin"] == {"routine": 1, "manual": 2}  # unknown origins count as manual
    assert s["tasks"] == {"t1": 2, "t3": 1}
    # hours: 24 buckets, oldest -> newest (the last one is the current hour).
    assert len(s["hours"]) == 24 and sum(s["hours"]) == 4
    assert s["hours"][23] == 2 and s["hours"][21] == 1 and s["hours"][10] == 1
    # days: the last 7 calendar days, oldest first, today last.
    assert [d["day"] for d in s["days"]][-1] == time.strftime("%Y-%m-%d", time.localtime(now))
    assert len(s["days"]) == 7 and [d["n"] for d in s["days"]][-3:] == [1, 1, 3]
    bots = {b["id"]: b for b in s["bots"]}
    assert set(bots) == {"b1", "gone", "_deleted"}
    assert bots["b1"] == {"id": "b1", "name": "Alpha", "today": 2, "week": 3, "models": {"sonnet": 2, "haiku": 1},
                          "errors": 1, "last": now - 60, "live": True}
    assert bots["gone"]["name"] == "Old Bot" and bots["gone"]["live"] is False and bots["gone"]["today"] == 1
    assert bots["_deleted"]["name"] == "Deleted bots" and bots["_deleted"]["errors"] == 1
    assert [b["id"] for b in s["bots"]] == ["b1", "gone", "_deleted"]  # by today, then week


def test_usage_log_writes_line_and_resets_cache():
    store.write_meta("b1", {"id": "b1", "name": "Alpha"})
    first = store.usage_summary()
    assert first["today"] == 0
    assert store.usage_summary() is first  # cached
    store.usage_log("b1", "sonnet", "manual", "x" * 200, True, name="Alpha")
    store.usage_log("b1", "opus", "routine", "t", False, cost=0.0123, input_tokens=4567)
    lines = [json.loads(line) for line in open(bpaths.usage_path())]
    assert set(lines[0]) == {"ts", "bot", "name", "model", "origin", "task", "ok"}
    assert len(lines[0]["task"]) == 80 and lines[0]["name"] == "Alpha"
    assert lines[1]["cost"] == 0.0123 and lines[1]["input_tokens"] == 4567 and lines[1]["ok"] is False
    s = store.usage_summary()
    assert s is not first and s["today"] == 2 and s["errors"] == 1


def test_usage_summary_prunes_stale_lines():
    now = time.time()
    rows = [{"ts": now - 9 * 86400, "bot": "x", "ok": True}] * (store.USAGE_PRUNE_AT + 1)
    rows.append({"ts": now - 10, "bot": "x", "name": "X", "ok": True})
    _ledger(rows)
    s = store.usage_summary(now=now)
    assert s["today"] >= 0 and s["hour"] == 1
    kept = open(bpaths.usage_path()).read().splitlines()
    assert len(kept) == 1 and json.loads(kept[0])["name"] == "X"


def test_usage_log_is_thread_safe():
    def go():
        for _ in range(50):
            store.usage_log("b", "m", "manual", "t", True)
    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = open(bpaths.usage_path()).read().splitlines()
    assert len(lines) == 200 and all(json.loads(line)["bot"] == "b" for line in lines)


# ----------------------------------------------------------------- export ---
def test_export_markdown(tmp_path):
    p = str(tmp_path / "events.jsonl")
    ts = time.mktime((2026, 10, 2, 9, 5, 0, 0, 0, -1))
    for ev in [
        {"role": "user", "text": "find flights  ", "ts": ts},
        {"role": "thought", "text": "searching"},
        {"role": "note", "text": "Used Fares › search"},
        {"role": "action", "text": "goto kayak.com", "result": "ok, now at https://kayak.com\nmore"},
        {"role": "question", "text": "Which day?", "ts": ts},
        {"role": "approval", "text": "click Buy?", "ts": ts},
        {"role": "error", "text": "timeout"},
        {"role": "system", "text": "paused"},
        {"role": "done", "text": "Found 3.", "ts": ts},
        {"role": "unknown", "text": "ignored"},
    ]:
        store.append_event(p, ev)
    md = store.export_markdown("Travel", p)
    assert md == (
        "# Travel — transcript\n\n"
        "**You** · 2026-10-02 09:05\n\nfind flights\n\n"
        "_searching_\n\n"
        "> _Used Fares › search_\n\n"
        "- `goto kayak.com` → ok, now at https://kayak.com\n"
        "**Bot asked** · 2026-10-02 09:05\n\nWhich day?\n\n"
        "**Bot asked** · 2026-10-02 09:05\n\nclick Buy?\n\n"
        "> ⚠ timeout\n\n"
        "> paused\n\n"
        "**Bot finished** · 2026-10-02 09:05\n\nFound 3.\n\n"
    )
    assert store.export_markdown("", str(tmp_path / "none.jsonl")) == "# Bot — transcript\n\n"


# ----------------------------------------------------------------- builds ---
def test_builds_read_write():
    assert store.builds_read() == []
    items = [{"entryId": "e1", "name": "A", "dir": "/x/a", "createdAt": 1}, {"entryId": "e2", "name": "B"}]
    store.builds_write(items)
    assert store.builds_read() == items
    assert json.load(open(bpaths.builds_path())) == items
    with pytest.raises(ValueError):
        store.builds_write({"builds": items})
    with open(bpaths.builds_path(), "w") as f:
        f.write("{broken")
    assert store.builds_read() == []
    with open(bpaths.builds_path(), "w") as f:
        json.dump({"builds": items + ["junk"]}, f)
    assert store.builds_read() == items


# ------------------------------------------------------- 0.11.x layout ---
def _touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def test_migrate_layout_moves_a_0_11_home(app_home):
    base = os.path.join(str(app_home), "bots")
    _touch(os.path.join(base, "data", "b1", "bot.json"), '{"id": "b1"}')
    _touch(os.path.join(base, "data", "b1", "profile", "Cookies"))
    _touch(os.path.join(base, "data", "notabot", "readme.txt"))  # no bot.json: left alone
    _touch(os.path.join(base, "cache", "b1", "shot.png"))
    _touch(os.path.join(base, "cache", "slow.jsonl"))
    _touch(os.path.join(base, "usage.jsonl"))
    _touch(os.path.join(base, "builds.json"), "[]")
    _touch(os.path.join(base, "imessage.lock"))
    _touch(os.path.join(base, "dock.json"), "{}")

    assert bpaths.migrate_layout() == 4  # b1 data, b1 cache, usage, builds
    assert os.path.isfile(os.path.join(base, "data", "bots", "b1", "profile", "Cookies"))
    assert os.path.isfile(os.path.join(base, "cache", "bots", "b1", "shot.png"))
    assert os.path.isfile(os.path.join(base, "data", "usage.jsonl"))
    assert os.path.isfile(os.path.join(base, "data", "builds.json"))
    assert os.path.isdir(os.path.join(base, "data", "notabot"))
    assert os.path.isfile(os.path.join(base, "cache", "slow.jsonl"))
    assert os.path.isfile(os.path.join(base, "dock.json"))
    assert not os.path.exists(os.path.join(base, "imessage.lock"))
    assert not os.path.exists(os.path.join(base, "data", "b1"))
    assert store.list_ids() == ["b1"]
    assert bpaths.migrate_layout() == 0  # idempotent


def test_migrate_layout_leaves_the_browsers_folders_alone(app_home):
    """cache/browsers/<id>/session.json and data/browsers/<id>/profile (browsers.py) are the new layout, not
    0.11.x bot folders: a boot must not move them under bots/ (that lost the session and wedged the launch)."""
    base = os.path.join(app_home, "bots")
    sess = os.path.join(base, "cache", "browsers", "b1", "session.json")
    prof = os.path.join(base, "data", "browsers", "b1", "profile")
    os.makedirs(os.path.dirname(sess)); open(sess, "w").write("{}")
    os.makedirs(prof)
    assert bpaths.migrate_layout() == 0
    assert os.path.isfile(sess) and os.path.isdir(prof)
    assert not os.path.exists(os.path.join(base, "cache", "bots", "browsers"))


def test_migrate_layout_never_overwrites(app_home):
    base = os.path.join(str(app_home), "bots")
    _touch(os.path.join(base, "data", "b1", "bot.json"), '{"id": "b1", "name": "old"}')
    _touch(os.path.join(base, "data", "bots", "b1", "bot.json"), '{"id": "b1", "name": "new"}')
    _touch(os.path.join(base, "usage.jsonl"), "old\n")
    _touch(os.path.join(base, "data", "usage.jsonl"), "new\n")
    assert bpaths.migrate_layout() == 0
    assert store.read_meta("b1")["name"] == "new"
    assert os.path.isfile(os.path.join(base, "data", "b1", "bot.json"))
    with open(bpaths.usage_path(), encoding="utf-8") as f:
        assert f.read() == "new\n"


def test_migrate_layout_on_a_fresh_home_is_a_noop(app_home):
    assert bpaths.migrate_layout() == 0
    assert not os.path.exists(os.path.join(str(app_home), "bots"))
