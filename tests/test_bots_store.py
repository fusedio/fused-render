"""fused_render.bots.store: bot.json + state.json (the public/private split),
events.jsonl, the usage ledger and summary, the Markdown export, builds.json.
_bots_conftest points FUSED_RENDER_HOME and FUSED_RENDER_DIR at tmp dirs, so
every path here lands under one of them: a bot's folder under <ws>/bots/, the
ledgers under <home>/bots/."""
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
B1, B2 = "b1b1b1b1", "b2b2b2b2"  # ids are 8 hex (paths._ID_RE); a bot must be place()d before it is written


def test_paths_split_between_workspace_and_app_home(app_home):
    # Per-bot files live in the bot's folder under <workspace>/bots/ (public root, private .fused/); the
    # machine-wide ledgers stay under the app home (docs/bots.md §1).
    ws = os.environ["FUSED_RENDER_DIR"]
    with pytest.raises(LookupError):
        bpaths.bot_folder(B1)  # not placed yet
    folder = bpaths.place(B1, "Bot One")
    assert folder == os.path.join(ws, "bots", "bot-one") == bpaths.bot_folder(B1)
    assert store.meta_path(B1) == os.path.join(folder, "bot.json")
    assert store.bot_dir(B1) == bpaths.bot_private(B1) == os.path.join(folder, ".fused")
    assert store.state_path(B1) == os.path.join(folder, ".fused", "state.json")
    assert store.events_path(B1) == os.path.join(folder, ".fused", "events.jsonl")
    assert bpaths.bot_cache_dir(B1) == os.path.join(folder, ".fused", "cache")
    assert bpaths.bot_inbox(B1) == os.path.join(folder, ".fused", "inbox")
    with open(os.path.join(folder, ".gitignore"), encoding="utf-8") as f:
        assert f.read() == ".fused/\n"
    assert bpaths.legacy_data_dir() == os.path.join(str(app_home), "bots", "data", "bots")
    assert bpaths.legacy_cache_dir() == os.path.join(str(app_home), "bots", "cache", "bots")
    assert bpaths.usage_path() == os.path.join(str(app_home), "bots", "data", "usage.jsonl")
    assert bpaths.builds_path() == os.path.join(str(app_home), "bots", "data", "builds.json")
    assert bpaths.slow_log_path() == os.path.join(str(app_home), "bots", "cache", "slow.jsonl")


def _tmp_files(bid):
    return [n for d in (bpaths.bot_folder(bid), store.bot_dir(bid)) for n in os.listdir(d) if n.endswith(".tmp")]


def test_meta_round_trip_and_list_ids():
    assert store.list_ids() == []
    bpaths.place(B2, "Two")
    assert store.list_ids() == []  # placed but not written yet: not a bot
    store.write_meta(B2, {"id": B2, "name": "Two"})
    bpaths.place(B1, "Ünï")
    store.write_meta(B1, {"id": B1, "name": "Ünï", "n": [1, 2]})
    os.makedirs(os.path.join(bpaths.bots_root(), "no-meta"))  # a folder without bot.json is not a bot
    assert store.read_meta(B1) == {"id": B1, "name": "Ünï", "n": [1, 2]}
    assert store.list_ids() == [B1, B2]
    store.write_meta(B1, {"id": B1, "name": "Renamed"})
    assert store.read_meta(B1)["name"] == "Renamed"
    assert _tmp_files(B1) == []
    with pytest.raises(OSError):
        store.read_meta("missing")
    with pytest.raises(OSError):
        store.read_meta("deadbeef")  # 8 hex, but no folder carries it


def test_write_meta_splits_public_and_private_halves():
    """bot.json (public, PUBLIC_KEYS) is a git-facing file: indented, sorted, newline-terminated, and rewritten
    only when its text changes; everything else lands in .fused/state.json; routines split by id the same way."""
    bpaths.place(B1, "Split")
    routine = {"id": "r1", "task": "check", "kind": "every", "minutes": 30, "enabled": True, "created": 1.0,
               "last": 5.0, "runs": 2, "fails": 0}
    meta = {"id": B1, "name": "Split", "model": "sonnet", "status": "idle", "browser_id": B1, "created": 1.0,
            "routines": [routine], "apps": []}
    store.write_meta(B1, meta)
    with open(store.meta_path(B1), encoding="utf-8") as f:
        text = f.read()
    public = json.loads(text)
    assert set(public) == {k for k in meta if k in store.PUBLIC_KEYS} and {"id", "name", "routines"} <= set(public)
    assert set(public["routines"][0]) <= store.ROUTINE_PUBLIC_KEYS and "last" not in public["routines"][0]
    assert text == store.public_json(public) and text.endswith("\n") and "\n  " in text  # indent=2, sorted
    with open(store.state_path(B1), encoding="utf-8") as f:
        private = json.load(f)
    priv_row = {"id": "r1", **{k: v for k, v in routine.items() if k not in store.ROUTINE_PUBLIC_KEYS}}
    priv_keys = {k: v for k, v in meta.items() if k not in store.PUBLIC_KEYS}
    assert private == {**priv_keys, "routines": [priv_row]} and "last" in priv_row and "status" in priv_keys
    assert store.read_meta(B1) == meta  # merged back into ONE dict, routine run state included
    # a runtime save that changes only private keys leaves bot.json untouched (git-clean folder)
    before = os.stat(store.meta_path(B1))
    store.write_meta(B1, {**meta, "status": "working", "step": 3})
    after = os.stat(store.meta_path(B1))
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert store.read_meta(B1)["status"] == "working"
    # a private routine row whose public twin is gone is dropped on read
    store.write_meta(B1, {**meta, "routines": []})
    assert store.read_meta(B1)["routines"] == []
    # the public file always carries the id it is saved under
    store.write_meta(B1, {"name": "No id"})
    assert json.load(open(store.meta_path(B1), encoding="utf-8"))["id"] == B1


def test_write_meta_is_safe_under_concurrent_writers():
    errors = []

    c = "cccccccc"
    bpaths.place(c, "C")

    def writer(i):
        try:
            for j in range(30):
                store.write_meta(c, {"i": i, "j": j, "name": f"n{i}"})
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert set(store.read_meta(c)) == {"id", "i", "j", "name"}  # write_meta stamps the id
    assert _tmp_files(c) == []


def test_write_json_atomic_cleans_up_on_failure(tmp_path):
    p = str(tmp_path / "x.json")
    with pytest.raises(TypeError):
        store.write_json_atomic(p, {"bad": object()})
    assert os.listdir(tmp_path) == [] or os.listdir(tmp_path) == ["x.json"]
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


# ----------------------------------------------------------------- events ---
def test_events_append_count_and_since_by_line():
    bpaths.place(B1, "One")
    p = store.events_path(B1)
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
    bpaths.place(B1, "Alpha")
    store.write_meta(B1, {"id": B1, "name": "Alpha"})
    bpaths.place("1d1e1d1e", "Idle")
    store.write_meta("1d1e1d1e", {"id": "1d1e1d1e", "name": "Idle"})
    _ledger([
        {"ts": now - 60, "bot": B1, "name": "old name", "model": "sonnet", "origin": "manual", "task": "t1", "ok": True},
        {"ts": now - 2 * 3600, "bot": B1, "name": "", "model": "haiku", "origin": "routine", "task": "t1", "ok": False},
        {"ts": now - 13 * 3600, "bot": B1, "name": "", "model": "sonnet", "origin": "manual", "task": "t2", "ok": True},
        {"ts": now - 30, "bot": "gone", "name": "Old Bot", "model": "opus", "origin": "weird", "task": "t3", "ok": True},
        {"ts": now - 2 * 86400, "bot": "ghost", "model": "opus", "origin": "manual", "task": "t4", "ok": False},
        {"ts": now - 8 * 86400, "bot": B1, "model": "opus", "origin": "manual", "task": "stale", "ok": True},
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
    assert set(bots) == {B1, "gone", "_deleted"}
    assert bots[B1] == {"id": B1, "name": "Alpha", "today": 2, "week": 3, "models": {"sonnet": 2, "haiku": 1},
                        "errors": 1, "last": now - 60, "live": True}
    assert bots["gone"]["name"] == "Old Bot" and bots["gone"]["live"] is False and bots["gone"]["today"] == 1
    assert bots["_deleted"]["name"] == "Deleted bots" and bots["_deleted"]["errors"] == 1
    assert [b["id"] for b in s["bots"]] == [B1, "gone", "_deleted"]  # by today, then week


def test_usage_log_writes_line_and_resets_cache():
    bpaths.place(B1, "Alpha")
    store.write_meta(B1, {"id": B1, "name": "Alpha"})
    first = store.usage_summary()
    assert first["today"] == 0
    assert store.usage_summary() is first  # cached
    store.usage_log(B1, "sonnet", "manual", "x" * 200, True, name="Alpha")
    store.usage_log(B1, "opus", "routine", "t", False, cost=0.0123, input_tokens=4567)
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
# migrate_layout moves a 0.11.x home into the LEGACY per-bot layout (<home>/bots/data/bots/<id>), which is no
# longer where a bot lives: migrate_v3 (below) lifts it from there. So these tests read the legacy tree directly.
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
    assert os.path.isfile(os.path.join(bpaths.legacy_data_dir(), "b1", "bot.json"))
    assert store.list_ids() == []  # the legacy tree is not a bot folder: migrate_v3 lifts it into <ws>/bots/
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
    # a pass with nothing to move marks the install: later folders of any name are never swept
    assert os.path.isfile(os.path.join(base, ".layout-v2"))
    os.makedirs(os.path.join(base, "cache", "future-thing"))
    os.makedirs(os.path.join(base, "cache", "deadbeef"))  # no such bot: not a bot's cache either
    assert bpaths.migrate_layout() == 0
    assert os.path.isdir(os.path.join(base, "cache", "future-thing")) and os.path.isdir(os.path.join(base, "cache", "deadbeef"))


def test_migrate_layout_never_overwrites(app_home):
    base = os.path.join(str(app_home), "bots")
    _touch(os.path.join(base, "data", "b1", "bot.json"), '{"id": "b1", "name": "old"}')
    _touch(os.path.join(base, "data", "bots", "b1", "bot.json"), '{"id": "b1", "name": "new"}')
    _touch(os.path.join(base, "usage.jsonl"), "old\n")
    _touch(os.path.join(base, "data", "usage.jsonl"), "new\n")
    assert bpaths.migrate_layout() == 0
    with open(os.path.join(bpaths.legacy_data_dir(), "b1", "bot.json"), encoding="utf-8") as f:
        assert json.load(f)["name"] == "new"
    assert os.path.isfile(os.path.join(base, "data", "b1", "bot.json"))
    with open(bpaths.usage_path(), encoding="utf-8") as f:
        assert f.read() == "new\n"


def test_migrate_layout_on_a_fresh_home_is_a_noop(app_home):
    assert bpaths.migrate_layout() == 0
    assert not os.path.exists(os.path.join(str(app_home), "bots"))


# ------------------------------------------------------------ migrate_v3 ---
# BUILD_SPEC §4: the legacy home-side per-bot trees lift into ~/Fused/bots/<folder>/, backup first. The legacy
# tree is built by hand here (store.write_meta writes the NEW layout, so it cannot seed a legacy fixture).
from fused_render.bots import migrate_v3  # noqa: E402
from fused_render.shell import storage as _storage  # noqa: E402


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A per-test workspace: conftest sets FUSED_RENDER_DIR once per session, and these tests list ~/Fused/bots."""
    root = tmp_path / "fused"
    root.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(root))
    reset = getattr(bpaths, "_reset_folders", None)
    if callable(reset):
        reset()
    return str(root)


def _legacy(app_home, bid, name, inbox=None, **meta):
    """One legacy bot: data/bots/<id>/{bot.json, events.jsonl, memory.md, skills/, files/, profile/},
    cache/bots/<id>/shot.png, and optionally an old Inbox folder (pinned as artifacts_dir) with one task."""
    base = os.path.join(str(app_home), "bots")
    d = os.path.join(base, "data", "bots", bid)
    m = {"id": bid, "name": name, "model": "sonnet", "instructions": "be useful", "created": 1.0,
         "allow_rules": [{"kind": "terminal", "key": "ls"}], **meta}
    if inbox:
        m["artifacts_dir"] = inbox
        _touch(os.path.join(inbox, "t1", "out.txt"), f"{bid} result")
        _touch(os.path.join(inbox, "index.jsonl"), '{"task": "t1"}\n')
    _touch(os.path.join(d, "bot.json"), json.dumps(m))
    _touch(os.path.join(d, "events.jsonl"), '{"kind": "user"}\n')
    _touch(os.path.join(d, "memory.md"), "remember me")
    _touch(os.path.join(d, "skills", "greet.md"), "# Greet\ntrigger: hello\n1. wave")
    _touch(os.path.join(d, "files", "a.txt"), "file")
    _touch(os.path.join(d, "profile", "Default", "Cookies"), "c")
    _touch(os.path.join(d, "profile", "x.lock"), "")
    os.symlink("host-123", os.path.join(d, "profile", "SingletonLock"))
    _touch(os.path.join(base, "cache", "bots", bid, "shot.png"), "png")
    return d


def _backups():
    base = os.path.join(_storage.base_home_dir(), "backup", "bot-migration-v3")
    return sorted(os.listdir(base)) if os.path.isdir(base) else []


def _walk_count(top):
    n = size = 0
    for dp, dns, fns in os.walk(top):
        for f in fns:
            n += 1
            size += os.lstat(os.path.join(dp, f)).st_size
        for dn in dns:
            if os.path.islink(os.path.join(dp, dn)):
                n += 1
                size += os.lstat(os.path.join(dp, dn)).st_size
    return n, size


def test_migrate_v3_backs_up_then_moves_into_the_bot_folder(app_home, ws):
    bid = "a1b2c3d4"
    inbox = os.path.join(ws, "bots", "daily-digest")
    _legacy(app_home, bid, "Daily Digest", inbox=inbox)
    r = migrate_v3.migrate_once()
    assert r["moved"] == 1 and r["skipped"] == [] and r["stamped"] is True

    # backup: both roots, Chrome's runtime files skipped, a manifest whose counts match what is on disk
    dest = r["backup"]
    assert os.path.dirname(dest) == os.path.join(_storage.base_home_dir(), "backup", "bot-migration-v3")
    with open(os.path.join(dest, "manifest.json"), encoding="utf-8") as f:
        man = json.load(f)
    assert man["version"] == 3 and [e["id"] for e in man["entries"]] == [bid]
    assert man["entries"][0]["src_inbox"] == inbox
    assert man["entries"][0]["dst_folder"] == inbox
    files = [_walk_count(os.path.join(dest, s)) for s in ("home-bots", "fused-bots")]
    assert man["files"] == sum(f[0] for f in files) and man["bytes"] == sum(f[1] for f in files)
    bk = os.path.join(dest, "home-bots", "data", "bots", bid)
    assert os.path.isfile(os.path.join(bk, "bot.json")) and os.path.isfile(os.path.join(bk, "profile", "Default", "Cookies"))
    assert not os.path.lexists(os.path.join(bk, "profile", "SingletonLock"))
    assert not os.path.exists(os.path.join(bk, "profile", "x.lock"))
    assert os.path.isfile(os.path.join(dest, "home-bots", "cache", "bots", bid, "shot.png"))
    assert os.path.isfile(os.path.join(dest, "fused-bots", "daily-digest", "t1", "out.txt"))

    # the folder: public bot.json, skills beside it, everything else under .fused/
    folder = inbox  # the old Inbox folder of the bot's name is adopted
    with open(os.path.join(folder, "bot.json"), encoding="utf-8") as f:
        public = json.load(f)
    assert set(public) <= store.PUBLIC_KEYS and public["id"] == bid and public["name"] == "Daily Digest"
    assert public["apps"] == []
    priv = os.path.join(folder, ".fused")
    with open(os.path.join(priv, "state.json"), encoding="utf-8") as f:
        assert "allow_rules" in json.load(f)
    meta = store.read_meta(bid)
    assert "artifacts_dir" not in meta and meta["allow_rules"] == [{"kind": "terminal", "key": "ls"}]
    assert os.path.isfile(os.path.join(folder, "skills", "greet.md"))
    assert os.path.isfile(os.path.join(folder, ".gitignore"))
    for rel in ("events.jsonl", "memory.md", os.path.join("files", "a.txt"), os.path.join("profile", "Default", "Cookies"),
                os.path.join("inbox", "t1", "out.txt"), os.path.join("inbox", "index.jsonl"),
                os.path.join("cache", "shot.png")):
        assert os.path.isfile(os.path.join(priv, rel)), rel
    assert not os.path.exists(os.path.join(folder, "t1")) and not os.path.exists(os.path.join(folder, "index.jsonl"))

    # stamp written, legacy per-bot trees gone, machine-level home state untouched
    assert os.path.isfile(os.path.join(bpaths.root(), ".layout-v3"))
    assert not os.path.exists(os.path.join(str(app_home), "bots", "data", "bots"))
    assert not os.path.exists(os.path.join(str(app_home), "bots", "cache", "bots"))
    assert store.list_ids() == [bid]

    # second run: a no-op, no second backup
    assert migrate_v3.migrate_once() == {"moved": 0, "skipped": [], "backup": None, "stamped": True}
    assert len(_backups()) == 1


def test_migrate_v3_fresh_install_stamps_without_backup(app_home, ws):
    r = migrate_v3.migrate_once()
    assert r == {"moved": 0, "skipped": [], "backup": None, "stamped": True}
    assert _backups() == []
    assert os.path.isfile(os.path.join(bpaths.root(), ".layout-v3"))


def test_migrate_v3_skips_an_id_already_placed(app_home, ws):
    bid, other = "a1b2c3d4", "b1b2c3d4"
    _touch(os.path.join(ws, "bots", "foo", "bot.json"), json.dumps({"id": bid, "name": "Foo"}))
    _legacy(app_home, bid, "Foo")
    _legacy(app_home, other, "Other")
    r = migrate_v3.migrate_once()
    assert [x["id"] for x in r["skipped"]] == [bid] and r["moved"] == 1 and r["stamped"] is True
    assert r["skipped"][0]["reason"].startswith("already at")
    with open(os.path.join(ws, "bots", "foo", "bot.json"), encoding="utf-8") as f:
        assert json.load(f) == {"id": bid, "name": "Foo"}  # never overwritten
    assert os.path.isfile(os.path.join(ws, "bots", "other", "bot.json"))
    # the skipped bot's legacy copy is in the backup (and removed from the legacy tree with the stamp)
    assert os.path.isfile(os.path.join(r["backup"], "home-bots", "data", "bots", bid, "bot.json"))


def test_migrate_v3_name_clash_gets_an_id_suffix(app_home, ws):
    _touch(os.path.join(ws, "bots", "helper", "bot.json"), json.dumps({"id": "ffff0000", "name": "Helper"}))
    _legacy(app_home, "abcd1234", "Helper")
    r = migrate_v3.migrate_once()
    assert r["moved"] == 1 and r["stamped"] is True
    folder = os.path.join(ws, "bots", "helper-abcd")
    assert json.load(open(os.path.join(folder, "bot.json"), encoding="utf-8"))["id"] == "abcd1234"
    assert json.load(open(os.path.join(ws, "bots", "helper", "bot.json"), encoding="utf-8"))["id"] == "ffff0000"


def test_migrate_v3_pinned_inboxes_stay_with_their_bot(app_home, ws):
    # "e…" was renamed Bar after pinning foo/; "d…" is named Foo but pinned foo-d000/ (foo was taken then). d sorts
    # first, so place() adopts foo/ for it while foo/ still holds e's tasks: they must still end up with e.
    e_inbox = os.path.join(ws, "bots", "foo")
    d_inbox = os.path.join(ws, "bots", "foo-d000")
    _legacy(app_home, "e0000001", "Bar", inbox=e_inbox)
    _legacy(app_home, "d0000001", "Foo", inbox=d_inbox)
    r = migrate_v3.migrate_once()
    assert r["moved"] == 2 and r["stamped"] is True
    d_folder, e_folder = os.path.join(ws, "bots", "foo"), os.path.join(ws, "bots", "bar")
    with open(os.path.join(d_folder, ".fused", "inbox", "t1", "out.txt"), encoding="utf-8") as f:
        assert f.read() == "d0000001 result"
    with open(os.path.join(e_folder, ".fused", "inbox", "t1", "out.txt"), encoding="utf-8") as f:
        assert f.read() == "e0000001 result"
    assert not os.path.exists(os.path.join(d_folder, "t1"))
    assert not os.path.exists(d_inbox)


def test_migrate_v3_backup_failure_leaves_everything(app_home, ws, monkeypatch):
    bid = "a1b2c3d4"
    inbox = os.path.join(ws, "bots", "daily-digest")
    d = _legacy(app_home, bid, "Daily Digest", inbox=inbox)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(migrate_v3.shutil, "copytree", boom)
    r = migrate_v3.migrate_once()
    assert r["stamped"] is False and r["moved"] == 0
    assert not os.path.exists(os.path.join(bpaths.root(), ".layout-v3"))
    assert os.path.isfile(os.path.join(d, "bot.json")) and os.path.isfile(os.path.join(d, "memory.md"))
    assert os.path.isfile(os.path.join(str(app_home), "bots", "cache", "bots", bid, "shot.png"))
    assert os.path.isfile(os.path.join(inbox, "t1", "out.txt"))
    assert not os.path.exists(os.path.join(inbox, "bot.json")) and not os.path.exists(os.path.join(inbox, ".fused"))


def test_migrate_v3_short_backup_moves_nothing(app_home, ws, monkeypatch):
    d = _legacy(app_home, "a1b2c3d4", "Daily Digest")
    real = migrate_v3._lenient_copy

    def drop_memory(src, dst, **k):
        return dst if src.endswith("memory.md") else real(src, dst, **k)
    monkeypatch.setattr(migrate_v3, "_lenient_copy", drop_memory)
    r = migrate_v3.migrate_once()
    assert r["stamped"] is False and r["moved"] == 0 and r["backup"]
    assert os.path.isfile(os.path.join(d, "memory.md")) and os.path.isfile(os.path.join(d, "bot.json"))
    assert not os.path.exists(os.path.join(bpaths.root(), ".layout-v3"))


def test_migrate_v3_seeds_the_apps_manifest(app_home, ws):
    apps = bpaths.apps_root()
    _touch(os.path.join(apps, "my_app", "index.html"), '<meta name="fused-app" />')
    _touch(os.path.join(apps, "built", "index.html"), '<meta name="fused-app" />')
    _legacy(app_home, "a1b2c3d4", "Seeded", trusted_apps=["my-app"],
            builds=[{"dir": os.path.join(apps, "built")}, {"dir": os.path.join(apps, "gone")}])
    assert migrate_v3.migrate_once()["moved"] == 1
    rows = store.read_meta("a1b2c3d4")["apps"]
    assert [r["folder"] for r in rows] == ["my_app", "built"]
    assert all(set(r) == {"folder", "name", "app_id", "source"} and r["source"].get("kind") for r in rows)


def test_migrate_v3_reuses_a_verified_backup_of_the_same_tree(app_home, ws, monkeypatch):
    _legacy(app_home, "a1b2c3d4", "Daily Digest")
    real = migrate_v3._move_bot

    def fail(*a, **k):
        raise RuntimeError("simulated move failure")
    monkeypatch.setattr(migrate_v3, "_move_bot", fail)
    first = migrate_v3.migrate_once()
    assert first["stamped"] is False and first["backup"] and len(_backups()) == 1
    monkeypatch.setattr(migrate_v3, "_move_bot", real)  # not undo(): that would drop app_home's env too
    second = migrate_v3.migrate_once()
    assert second["stamped"] is True and second["moved"] == 1
    assert second["backup"] == first["backup"] and len(_backups()) == 1


def test_migrate_v3_parks_unplaceable_bots_and_stamps(app_home, ws):
    bad = _legacy(app_home, "a1b2c3d4", "Broken")
    _touch(os.path.join(bad, "bot.json"), "{not json")
    _legacy(app_home, "not-hex", "Odd Id")
    _legacy(app_home, "b1b2c3d4", "Good")
    r = migrate_v3.migrate_once()
    assert r["stamped"] is True and r["moved"] == 1
    reasons = {x["id"]: x["reason"] for x in r["skipped"]}
    assert reasons == {"a1b2c3d4": "unreadable bot.json", "not-hex": "id is not 8 hex"}
    park = os.path.join(ws, "bots", ".unmigrated")
    with open(os.path.join(park, "a1b2c3d4", "bot.json"), encoding="utf-8") as f:
        assert f.read() == "{not json"
    assert os.path.isfile(os.path.join(park, "a1b2c3d4", "cache", "shot.png"))
    assert os.path.isfile(os.path.join(park, "not-hex", "memory.md"))
    assert os.path.isfile(os.path.join(r["backup"], "home-bots", "data", "bots", "a1b2c3d4", "bot.json"))
    assert store.list_ids() == ["b1b2c3d4"]  # the dot-folder is invisible to the folder index
    assert not os.path.exists(os.path.join(str(app_home), "bots", "data", "bots"))


def test_migrate_v3_lifts_legacy_bots_that_appear_after_the_stamp(app_home, ws):
    assert migrate_v3.migrate_once()["stamped"] is True  # fresh install
    _legacy(app_home, "a1b2c3d4", "Late Import")  # e.g. fusedbot_import on a later start
    r = migrate_v3.migrate_once()
    assert r["moved"] == 1 and r["stamped"] is True and r["backup"]
    assert store.list_ids() == ["a1b2c3d4"]
    with open(os.path.join(bpaths.root(), ".layout-v3"), encoding="utf-8") as f:
        assert json.load(f)["moved"] == 1
    assert not os.path.exists(os.path.join(str(app_home), "bots", "data", "bots"))


# ---- the safety model: nothing deleted that is not in a verified backup; a bot counts as moved only when drained
def test_migrate_v3_backup_is_verified_before_the_first_destination_write(app_home, ws, monkeypatch):
    _legacy(app_home, "a1b2c3d4", "Daily Digest", inbox=os.path.join(ws, "bots", "daily-digest"))
    seen = []
    real_move, real_place = migrate_v3._move_tree, bpaths.place

    def spy_move(*a, **k):
        seen.append(("move", [b for b in _backups() if os.path.isfile(os.path.join(_storage.base_home_dir(), "backup", "bot-migration-v3", b, "manifest.json"))]))
        return real_move(*a, **k)

    def spy_place(*a, **k):
        seen.append(("place", [b for b in _backups() if os.path.isfile(os.path.join(_storage.base_home_dir(), "backup", "bot-migration-v3", b, "manifest.json"))]))
        return real_place(*a, **k)
    monkeypatch.setattr(migrate_v3, "_move_tree", spy_move)
    monkeypatch.setattr(bpaths, "place", spy_place)
    assert migrate_v3.migrate_once()["stamped"] is True
    assert seen and all(verified for _, verified in seen)  # a verified manifest existed before every write


def test_migrate_v3_clash_goes_to_leftovers_and_legacy_is_drained(app_home, ws):
    bid = "a1b2c3d4"
    _legacy(app_home, bid, "Daily Digest")
    # the destination already holds a memory.md of its own (a half-made folder from an older attempt)
    _touch(os.path.join(ws, "bots", "daily-digest", ".fused", "memory.md"), "newer")
    r = migrate_v3.migrate_once()
    assert r["moved"] == 1 and r["stamped"] is True
    folder = os.path.join(ws, "bots", "daily-digest")
    with open(os.path.join(folder, ".fused", "memory.md"), encoding="utf-8") as f:
        assert f.read() == "newer"  # never overwritten
    with open(os.path.join(folder, ".fused", "legacy-leftovers", "memory.md"), encoding="utf-8") as f:
        assert f.read() == "remember me"  # the legacy file kept, beside, not deleted
    assert not os.path.exists(os.path.join(str(app_home), "bots", "data", "bots"))


def test_migrate_v3_does_not_reuse_a_backup_of_a_changed_tree(app_home, ws, monkeypatch):
    d = _legacy(app_home, "a1b2c3d4", "Daily Digest")
    real = migrate_v3._move_bot
    monkeypatch.setattr(migrate_v3, "_move_bot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    first = migrate_v3.migrate_once()
    assert first["stamped"] is False and len(_backups()) == 1
    # same file count and byte sum, different content (a same-size rewrite) -> a new backup, not a reuse
    with open(os.path.join(d, "memory.md"), "w", encoding="utf-8") as f:
        f.write("remember YOU")
    os.utime(os.path.join(d, "memory.md"), ns=(1, 1))
    monkeypatch.setattr(migrate_v3, "_move_bot", real)  # not undo(): that would drop app_home's env too
    second = migrate_v3.migrate_once()
    assert second["stamped"] is True and second["backup"] != first["backup"] and len(_backups()) == 2
    with open(os.path.join(second["backup"], "home-bots", "data", "bots", "a1b2c3d4", "memory.md"), encoding="utf-8") as f:
        assert f.read() == "remember YOU"


def test_migrate_v3_failed_or_partial_backups_are_never_reused(app_home, ws, monkeypatch):
    _legacy(app_home, "a1b2c3d4", "Daily Digest")
    real = migrate_v3.shutil.copytree
    monkeypatch.setattr(migrate_v3.shutil, "copytree", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    first = migrate_v3.migrate_once()
    assert first["stamped"] is False and os.path.isfile(os.path.join(first["backup"], "failed.json"))
    monkeypatch.setattr(migrate_v3.shutil, "copytree", real)  # not undo(): that would drop app_home's env too
    second = migrate_v3.migrate_once()
    assert second["stamped"] is True and second["backup"] != first["backup"]
    assert os.path.isdir(first["backup"])  # the failed one is left on disk (never pruned), just not trusted


def test_migrate_v3_cross_device_copy_failure_keeps_the_source(app_home, ws, monkeypatch):
    d = _legacy(app_home, "a1b2c3d4", "Daily Digest")
    real_rename = os.rename

    def exdev(src, dst, *a, **k):
        if src.endswith("memory.md"):
            raise OSError(18, "Cross-device link")  # EXDEV
        return real_rename(src, dst, *a, **k)
    monkeypatch.setattr(migrate_v3.os, "rename", exdev)
    monkeypatch.setattr(migrate_v3.shutil, "copy2", lambda *a, **k: (_ for _ in ()).throw(OSError("I/O error")))
    r = migrate_v3.migrate_once()
    assert r["stamped"] is False and r["moved"] == 0
    assert os.path.isfile(os.path.join(d, "memory.md")) and os.path.isfile(os.path.join(d, "bot.json"))
    assert os.path.isdir(os.path.join(str(app_home), "bots", "data", "bots"))  # nothing removed


def test_migrate_v3_already_placed_bot_keeps_its_legacy_files_as_leftovers(app_home, ws):
    bid = "a1b2c3d4"
    _legacy(app_home, bid, "Daily Digest")
    folder = bpaths.place(bid, "Daily Digest")  # another home sharing ~/Fused migrated it first
    store.write_meta(bid, {"id": bid, "name": "Daily Digest"})
    r = migrate_v3.migrate_once()
    assert r["moved"] == 0 and r["stamped"] is True and r["skipped"][0]["id"] == bid
    kept = os.path.join(folder, ".fused", "legacy-leftovers")
    assert any(os.path.isfile(os.path.join(dp, "memory.md")) for dp, _, _ in os.walk(kept))
    assert not os.path.exists(os.path.join(str(app_home), "bots", "data", "bots"))


def test_migrate_v3_shared_pin_leaves_the_inbox_in_place(app_home, ws):
    shared = os.path.join(ws, "bots", "shared")
    _legacy(app_home, "a0000001", "Alpha", inbox=shared)
    _legacy(app_home, "b0000001", "Beta")
    # Beta pins the same folder by hand (an old rename collision)
    with open(os.path.join(str(app_home), "bots", "data", "bots", "b0000001", "bot.json"), encoding="utf-8") as f:
        m = json.load(f)
    m["artifacts_dir"] = shared
    with open(os.path.join(str(app_home), "bots", "data", "bots", "b0000001", "bot.json"), "w", encoding="utf-8") as f:
        json.dump(m, f)
    r = migrate_v3.migrate_once()
    assert r["moved"] == 2 and r["stamped"] is True
    assert os.path.isfile(os.path.join(shared, "t1", "out.txt"))  # nobody's: left where it was
    assert any("shared with" in x["reason"] for x in r["skipped"])


def test_browser_sweep_waits_for_the_v3_stamp(app_home, ws, monkeypatch):
    from fused_render.bots import browsers, registry
    calls = []
    monkeypatch.setattr(browsers, "sweep", lambda bots: calls.append(bots) or 0)
    monkeypatch.setattr(registry, "all", lambda: [])
    registry._sweep_browsers()
    assert calls == []  # no stamp yet: a legacy bot's browser would read as an orphan
    _touch(os.path.join(bpaths.root(), ".layout-v3"), "{}")
    registry._sweep_browsers()
    assert len(calls) == 1


def test_migrate_v3_symlinked_legacy_dir_is_parked_not_emptied(app_home, ws, tmp_path):
    # A symlink where a bot folder should be: the backup would hold the LINK, so moving through it would empty a
    # tree the backup never copied. It is parked as a link and the target is untouched.
    _legacy(app_home, "a1b2c3d4", "Real")
    target = tmp_path / "elsewhere"
    _touch(str(target / "bot.json"), json.dumps({"id": "b2b2b2b2", "name": "Linked"}))
    _touch(str(target / "memory.md"), "keep me")
    os.symlink(str(target), os.path.join(str(app_home), "bots", "data", "bots", "b2b2b2b2"))
    r = migrate_v3.migrate_once()
    assert r["moved"] == 1 and r["stamped"] is True
    assert (target / "memory.md").read_text() == "keep me" and (target / "bot.json").exists()
    assert store.list_ids() == ["a1b2c3d4"]
    parked = os.path.join(ws, "bots", ".unmigrated")
    assert any("b2b2b2b2" in n for dp, dns, fns in os.walk(parked) for n in dns + fns)


def test_migrate_v3_never_adopts_a_users_folder(app_home, ws):
    _legacy(app_home, "a1b2c3d4", "Notes")
    _touch(os.path.join(ws, "bots", "notes", "my-essay.txt"), "mine")  # not an Inbox: no index.jsonl, not task-shaped
    r = migrate_v3.migrate_once()
    assert r["moved"] == 1 and r["stamped"] is True
    assert sorted(os.listdir(os.path.join(ws, "bots", "notes"))) == ["my-essay.txt"]  # untouched
    folder = bpaths.bot_folder("a1b2c3d4")
    assert os.path.basename(folder) == "notes-a1b2"


def test_migrate_v3_reserved_names_in_an_inbox_are_kept_as_leftovers(app_home, ws):
    inbox = os.path.join(ws, "bots", "super-bot")
    _legacy(app_home, "a1b2c3d4", "Super Bot", inbox=inbox)
    _touch(os.path.join(inbox, "README.md"), "written by a task")  # pre-v3 Super Bot ran claude in its Inbox
    r = migrate_v3.migrate_once()
    assert r["moved"] == 1 and r["stamped"] is True
    folder = os.path.join(ws, "bots", "super-bot")
    with open(os.path.join(folder, ".fused", "legacy-leftovers", "inbox", "README.md"), encoding="utf-8") as f:
        assert f.read() == "written by a task"
    assert not os.path.exists(os.path.join(folder, "README.md"))
    assert os.path.isfile(os.path.join(folder, ".fused", "inbox", "t1", "out.txt"))


# ------------------------------------------------------- is_bot_workdir ---
# The Tasks page drops a transcript whose cwd is a bot's own run folder (routers/tasks `_bot_session`): a bot's
# conversation is a CLI session on disk, not a task. v3 moved those folders under <ws>/bots/<folder>/.fused/.
def test_is_bot_workdir_hides_v3_runs_and_legacy_cwds_but_not_user_folders(app_home, ws, tmp_path):
    folder = os.path.join(ws, "bots", "super-bot")
    _touch(os.path.join(folder, "bot.json"), json.dumps({"id": "abcd1234", "name": "Super Bot"}))
    os.makedirs(os.path.join(folder, ".fused", "inbox", "20260101-0900-x"))
    os.makedirs(os.path.join(folder, "skills"))
    base = _storage.base_home_dir()
    hidden = [
        os.path.join(folder, ".fused", "cache"),                       # an ordinary bot's run
        os.path.join(folder, ".fused", "inbox"),                       # Super Bot's run
        os.path.join(folder, ".fused", "inbox", "20260101-0900-x"),    # nested under the Inbox
        os.path.join(base, "bots", "cache", "bots", "abcd1234"),       # legacy cache dir (pre-v3 transcripts)
        os.path.join(base, "branches", "dev", "bots", "cache", "bots", "abcd1234"),
        os.path.join(ws, "bots", "old-inbox"),                         # legacy Inbox, folder since moved away
        # legacy Super Bot Inbox that migrate_v3 adopted as this bot's folder (it has a bot.json now): its
        # pre-migration transcripts still name it as their cwd and must stay hidden.
        folder,
    ]
    shown = [
        os.path.join(ws, "canvas", "x"),
        os.path.join(ws, "app", "x"),
        os.path.join(ws, "bots"),
        os.path.join(folder, "skills"),                                # no bot run works in the public half
        os.path.join(ws, "projects", "bots", "cache", "bots", "x"),    # a substring is never enough
        "",
        None,
    ]
    assert [p for p in hidden if not bpaths.is_bot_workdir(p)] == []
    assert [p for p in shown if bpaths.is_bot_workdir(p)] == []
    # The CLI may record the resolved path: a symlinked workspace still hides the run.
    link = str(tmp_path / "ws-link")
    os.symlink(ws, link)
    assert bpaths.is_bot_workdir(os.path.join(link, "bots", "super-bot", ".fused", "inbox", "20260101-0900-x"))
    assert not bpaths.is_bot_workdir(os.path.join(link, "canvas", "x"))


def test_is_bot_workdir_after_migrate_v3_hides_the_adopted_inbox(app_home, ws):
    # The end-to-end shape of the regression: a pre-v3 Super Bot ran in ~/Fused/bots/<slug>; migrate_v3 adopts
    # that folder and writes bot.json into it. Old transcripts naming the old Inbox (and the old cache) stay hidden.
    bid = "a1b2c3d4"
    inbox = os.path.join(ws, "bots", "daily-digest")
    _legacy(app_home, bid, "Daily Digest", inbox=inbox)
    legacy_cache = os.path.join(str(app_home), "bots", "cache", "bots", bid)
    assert migrate_v3.migrate_once()["moved"] == 1
    assert os.path.isfile(os.path.join(inbox, "bot.json")) and not os.path.exists(legacy_cache)
    assert bpaths.is_bot_workdir(inbox)
    assert bpaths.is_bot_workdir(legacy_cache)
    assert bpaths.is_bot_workdir(bpaths.bot_cache_dir(bid)) and bpaths.bot_cache_dir(bid).startswith(inbox + os.sep)
    assert bpaths.is_bot_workdir(bpaths.bot_inbox(bid))
