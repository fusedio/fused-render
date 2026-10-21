"""Every GitHub PR a Claude task has touched, collected from its transcript and
kept per session (routers/tasks.py `_note_prs`, tasks_store `record_prs`).

Nothing here reads the real ~/.claude — every path is under tmp_path.
"""
import json

import pytest
from fastapi.testclient import TestClient

from fused_render import schedule, tasks_store
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod
from tests.test_tasks_api import (T9, T10, _already_using, _assistant, _tasks,
                                  _user, _write_transcript)


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


def _numbers(prs):
    return [(p["repo"], p["number"]) for p in prs]


def _absorbed(*lines):
    rec = tasks_mod._new_scan()
    for line in lines:
        tasks_mod._absorb(rec, line)
    return rec


# ----------------------------------------------------------------- the scan


def test_a_new_scan_starts_with_no_prs():
    assert tasks_mod._new_scan()["prs"] == []


U1 = "https://github.com/acme/widgets/pull/12"


def _tool_use(tid, name, inp):
    return json.dumps({"type": "assistant", "message": {"role": "assistant",
        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})


def _tool_result(tid, text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}})


def test_bash_gh_pr_create_result_is_noted():
    rec = _absorbed(
        _tool_use("t1", "Bash", {"command": "gh pr create --fill"}),
        _tool_result("t1", U1 + "\n"))
    assert rec["prs"] == [{"repo": "acme/widgets", "number": 12, "url": U1}]


def test_a_pr_url_in_the_gh_command_itself_is_noted():
    rec = _absorbed(_tool_use("t1", "Bash", {"command": f"gh pr view {U1}"}))
    assert _numbers(rec["prs"]) == [("acme/widgets", 12)]


def test_non_gh_bash_result_is_not_noted():
    rec = _absorbed(
        _tool_use("t1", "Bash", {"command": "git log --oneline"}),
        _tool_result("t1", f"abc merge {U1}"))
    assert rec["prs"] == []


def test_read_tool_result_is_not_noted():
    rec = _absorbed(
        _tool_use("t1", "Read", {"file_path": "/x/fixture.py"}),
        _tool_result("t1", U1))
    assert rec["prs"] == []


def test_other_tool_use_inputs_are_not_noted():
    rec = _absorbed(
        _tool_use("t1", "Grep", {"pattern": U1}),
        _tool_use("t2", "Edit", {"file_path": "/x", "new_string": U1}))
    assert rec["prs"] == []


def test_sidechain_lines_are_not_noted():
    line = _assistant("see " + U1, T9)
    line["isSidechain"] = True
    side_cmd = json.loads(_tool_use("t1", "Bash", {"command": f"gh pr view {U1}"}))
    side_cmd["isSidechain"] = True
    rec = _absorbed(json.dumps(line), json.dumps(side_cmd))
    assert rec["prs"] == []


def test_meta_thinking_and_attachment_lines_are_not_noted():
    meta = _user("see " + U1, T9)
    meta["isMeta"] = True
    think = {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": U1}]}}
    att = {"type": "attachment", "attachment": {"text": U1}}
    rec = _absorbed(*(json.dumps(x) for x in (meta, think, att)))
    assert rec["prs"] == []


def test_user_string_content_is_noted():
    line = json.dumps({"type": "user", "message": {"role": "user", "content": U1}})
    assert _numbers(_absorbed(line)["prs"]) == [("acme/widgets", 12)]


def test_assistant_text_and_user_lines_are_noted_in_first_seen_order():
    a = json.dumps(_assistant("opened https://github.com/acme/widgets/pull/12", T9))
    u = json.dumps(_user("also see https://github.com/acme/api/pull/7", T10))
    rec = _absorbed(a, u)
    assert _numbers(rec["prs"]) == [("acme/widgets", 12), ("acme/api", 7)]


def test_duplicates_and_sub_pages_do_not_repeat():
    a = json.dumps(_assistant(
        "https://github.com/acme/widgets/pull/12 and "
        "https://github.com/acme/widgets/pull/12/files and "
        "https://github.com/Acme/Widgets/pull/12 and "
        "https://github.com/acme/widgets/pull/123", T9))
    rec = _absorbed(a)
    assert _numbers(rec["prs"]) == [("acme/widgets", 12), ("acme/widgets", 123)]


def test_tool_use_ids_are_remembered_boundedly():
    rec = tasks_mod._new_scan()
    for i in range(300):
        tasks_mod._absorb(rec, _tool_use(f"t{i}", "Bash", {"command": "gh pr list"}))
    assert 0 < len(rec["gh_pr_tool_ids"]) <= 200
    assert "t299" in rec["gh_pr_tool_ids"]


def test_old_unversioned_stored_prs_are_ignored():
    state = {"s": {"prs": [{"repo": "a/b", "number": 1}]}}
    assert tasks_store.session_prs(state, "s") == []
    state = {"s": {"prs": [{"repo": "a/b", "number": 1}], "prs_v": 2}}
    assert _numbers(tasks_store.session_prs(state, "s")) == [("a/b", 1)]


def test_record_prs_replaces_an_unversioned_list():
    tasks_store._update(tasks_store.SETTINGS_FILE, lambda st: (
        None, st.__setitem__("s1", {"prs": [{"repo": "x/y", "number": 9}]}) or True))
    tasks_store.record_prs("s1", [{"repo": "a/b", "number": 1, "url": ""}])
    got = tasks_store.session_prs(tasks_store.settings_state(), "s1")
    assert _numbers(got) == [("a/b", 1)]


def test_a_line_without_a_pr_url_is_untouched():
    rec = _absorbed(json.dumps(_user("see github.com/acme/widgets issues", T9)))
    assert rec["prs"] == []
    assert rec["count"] == 1, "the existing prompt logic still ran"


def test_a_pr_line_still_feeds_the_existing_logic():
    rec = _absorbed(json.dumps(_user(
        "review https://github.com/acme/widgets/pull/3", T9)))
    assert rec["count"] == 1
    assert len(rec["prs"]) == 1


def test_the_list_is_capped():
    text = " ".join(f"https://github.com/a/b/pull/{n}" for n in range(1, 80))
    rec = _absorbed(json.dumps(_assistant(text, T9)))
    assert len(rec["prs"]) == 50


# ------------------------------------------------------------------- store


def test_record_prs_roundtrip_and_merge():
    first = [{"repo": "a/b", "number": 1, "url": "x"}]
    assert tasks_store.record_prs("s1", first)[0]["number"] == 1
    merged = tasks_store.record_prs("s1", [
        {"repo": "A/B", "number": 1, "url": "x"},
        {"repo": "a/b", "number": 2, "url": "y"}])
    assert _numbers(merged) == [("a/b", 1), ("a/b", 2)]
    got = tasks_store.session_prs(tasks_store.settings_state(), "s1")
    assert _numbers(got) == [("a/b", 1), ("a/b", 2)]
    assert got[1]["url"] == "https://github.com/a/b/pull/2"


def test_record_prs_empty_session_id_writes_nothing():
    assert tasks_store.record_prs("", [{"repo": "a/b", "number": 1, "url": ""}]) == []
    assert tasks_store.settings_state() == {}


def test_session_prs_drops_malformed_entries():
    state = {"s": {"prs_v": 2, "prs": [{"repo": "a/b", "number": 1}, {"repo": 3, "number": 2},
                           {"repo": "a/b", "number": "x"}, "junk"]}}
    assert _numbers(tasks_store.session_prs(state, "s")) == [("a/b", 1)]
    assert tasks_store.session_prs(state, "other") == []


def test_other_writers_keep_the_prs():
    tasks_store.record_prs("s1", [{"repo": "a/b", "number": 1, "url": ""}])
    tasks_store.record_settings("s1", model="opus")
    tasks_store.record_recap("s1", "u1", "text")
    tasks_store.record_title("s1", "mine")
    got = tasks_store.session_prs(tasks_store.settings_state(), "s1")
    assert _numbers(got) == [("a/b", 1)]


def test_forget_session_drops_the_prs():
    tasks_store.record_prs("s1", [{"repo": "a/b", "number": 1, "url": ""}])
    tasks_store.forget_session("s1")
    assert tasks_store.session_prs(tasks_store.settings_state(), "s1") == []


# --------------------------------------------------------------------- row


def test_the_row_carries_prs_oldest_first(client, projects_dir, state_dir):
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [
        _user("ship it", T9),
        _assistant("opened https://github.com/acme/widgets/pull/12", T10),
        _assistant("and https://github.com/acme/widgets/pull/14", T10),
    ])
    row = _tasks(client)[0]
    assert _numbers(row["prs"]) == [("acme/widgets", 12), ("acme/widgets", 14)]
    assert row["prs"][0]["url"] == "https://github.com/acme/widgets/pull/12"


def test_a_row_without_prs_has_an_empty_list(client, projects_dir, state_dir):
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("hi", T9), _assistant("hello", T10)])
    assert _tasks(client)[0]["prs"] == []


def test_prs_survive_a_truncated_transcript(client, projects_dir, state_dir):
    _already_using(state_dir)
    path = _write_transcript(projects_dir, "sess-a", "/home/me/proj", [
        _user("ship it", T9),
        _assistant("opened https://github.com/acme/widgets/pull/12", T10)])
    assert _numbers(_tasks(client)[0]["prs"]) == [("acme/widgets", 12)]
    # The CLI rewrote the file shorter (a compaction): the scan starts over and
    # no longer sees the URL, but the stored list still does.
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [_user("hi", T9)])
    tasks_mod.reset_cache()
    assert path.exists()
    assert _numbers(_tasks(client)[0]["prs"]) == [("acme/widgets", 12)]


def test_a_new_pr_is_appended_to_the_stored_ones(client, projects_dir, state_dir):
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [
        _user("ship it", T9),
        _assistant("https://github.com/acme/widgets/pull/12", T10)])
    _tasks(client)
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [
        _user("ship it", T9),
        _assistant("https://github.com/acme/widgets/pull/12", T10),
        _assistant("https://github.com/acme/widgets/pull/15", T10)])
    tasks_mod.reset_cache()
    assert _numbers(_tasks(client)[0]["prs"]) == [
        ("acme/widgets", 12), ("acme/widgets", 15)]


def test_row_hash_changes_with_prs(client, projects_dir, state_dir):
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/home/me/proj",
                      [_user("hi", T9), _assistant("hello", T10)])
    before = _tasks(client)[0]["row_hash"]
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [
        _user("hi", T9),
        _assistant("hello https://github.com/acme/widgets/pull/1", T10)])
    tasks_mod.reset_cache()
    assert _tasks(client)[0]["row_hash"] != before


def test_the_pulse_does_not_carry_prs(client, projects_dir, state_dir):
    _already_using(state_dir)
    _write_transcript(projects_dir, "sess-a", "/home/me/proj", [
        _user("hi", T9),
        _assistant("https://github.com/acme/widgets/pull/1", T10)])
    rows = client.get("/api/tasks/pulse").json()["tasks"]
    assert rows and "prs" not in rows[0]
