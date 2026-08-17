"""GET /api/claude-sessions/summaries (server/routers/claude_sessions.py):
one row per Claude Code session for the React shell's Schedule page — name,
folder, start/last-active times, live "running" flag and triage status,
newest activity first.

Mirrors the bundled sessions inbox app (core_apps/sessions), so the rules
under test are its rules: the 45s running window, housekeeping tail entries
that bump the file mtime without being activity, and the session_names.json /
triage.json overlays.
"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as claude_sessions_mod


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(claude_sessions_mod, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "fused-render-home" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(claude_sessions_mod, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_head_cache():
    # The head cache is module-level and keyed by path; tmp_path keeps tests
    # from colliding, but clearing keeps them order-independent regardless.
    claude_sessions_mod._HEAD_CACHE.clear()
    yield
    claude_sessions_mod._HEAD_CACHE.clear()


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _write(projects_dir, encoded_dir, session_id, entries, mtime=None):
    d = projects_dir / encoded_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _user(text, ts):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(text, ts):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _tool_result(ts, tool_use_id="t1"):
    """A user-role entry that only carries a tool result — Claude Code records
    these as type=user, but they are not something the human typed."""
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}]}}


def _turn_duration(ts):
    return {"type": "system", "subtype": "turn_duration", "timestamp": ts,
            "durationMs": 1234}


STALE = 1735689600.0  # 2025-01-01, comfortably outside the 45s running window


def _get(client):
    r = client.get("/api/claude-sessions/summaries")
    assert r.status_code == 200
    return r.json()["sessions"]


def test_lists_sessions_named_by_first_prompt_newest_activity_first(
        client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(projects_dir, "proj", "older", [
        {"cwd": str(proj), "type": "user", "timestamp": "2026-08-16T08:00:00Z",
         "message": {"role": "user", "content": "Fix the login bug"}},
        _assistant("on it", "2026-08-16T08:05:00Z"),
    ], mtime=STALE)
    _write(projects_dir, "proj", "newer", [
        {"cwd": str(proj), "type": "user", "timestamp": "2026-08-16T09:00:00Z",
         "message": {"role": "user", "content": "Ship the release"}},
        _assistant("done", "2026-08-16T09:30:00Z"),
    ], mtime=STALE)

    sessions = _get(client)
    assert [s["session_id"] for s in sessions] == ["newer", "older"]
    assert [s["name"] for s in sessions] == ["Ship the release", "Fix the login bug"]
    assert [s["cwd"] for s in sessions] == [str(proj), str(proj)]

    newer, older = sessions
    assert newer["started_at"] == "2026-08-16T09:00:00+00:00"
    assert newer["last_active"] == "2026-08-16T09:30:00+00:00"
    assert older["started_at"] == "2026-08-16T08:00:00+00:00"
    assert older["last_active"] == "2026-08-16T08:05:00+00:00"
    assert older["running"] is False


def test_long_first_prompt_is_truncated_to_140_chars(client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_user("x" * 500, "2026-08-16T08:00:00Z")},
    ], mtime=STALE)
    assert _get(client)[0]["name"] == "x" * 140


def test_session_with_no_user_message_falls_back_to_placeholder(
        client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_assistant("resumed", "2026-08-16T08:00:00Z")},
    ], mtime=STALE)
    assert _get(client)[0]["name"] == "(no user message)"


def test_custom_name_wins_over_first_prompt(client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_user("Fix the login bug", "2026-08-16T08:00:00Z")},
    ], mtime=STALE)
    (state_dir / "session_names.json").write_text(json.dumps({"s1": "Auth rewrite"}))
    assert _get(client)[0]["name"] == "Auth rewrite"


def test_triage_status_overlays_the_default(client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    for sid in ("filed", "bogus"):
        _write(projects_dir, "proj", sid, [
            {"cwd": str(proj), **_user("hi", "2026-08-16T08:00:00Z")},
        ], mtime=STALE)
    (state_dir / "triage.json").write_text(json.dumps({
        "filed": {"status": "archived"},
        "bogus": {"status": "not-a-real-status"},
    }))

    by_id = {s["session_id"]: s for s in _get(client)}
    assert by_id["filed"]["status"] == "archived"
    # an unrecognized status is ignored rather than passed through to the UI
    assert by_id["bogus"]["status"] == "done"


def test_stale_session_defaults_to_done_and_not_running(
        client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_user("hi", "2026-08-16T08:00:00Z")},
    ], mtime=STALE)
    session = _get(client)[0]
    assert session["running"] is False
    assert session["status"] == "done"


def test_fresh_activity_is_running_and_defaults_to_in_progress(
        client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_user("hi", "2026-08-16T08:00:00Z")},
        _assistant("working", now_iso),
    ])
    session = _get(client)[0]
    assert session["running"] is True
    assert session["status"] == "in_progress"


def test_housekeeping_tail_is_not_activity_even_with_a_fresh_file(
        client, projects_dir, state_dir, tmp_path):
    """A turn_duration entry appended after the turn ends bumps the file mtime
    but means the opposite of "running" — the turn just finished."""
    proj = tmp_path / "proj"
    proj.mkdir()
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_user("hi", "2026-08-16T08:00:00Z")},
        _assistant("working", now_iso),
        _turn_duration(now_iso),
    ])  # freshly written, so the mtime alone would say "running"

    session = _get(client)[0]
    assert session["running"] is False
    assert session["status"] == "done"
    # last_active still reports the last *real* entry, skipping housekeeping
    assert session["last_active"].startswith(now_iso[:-1])


def test_session_without_any_timestamp_is_skipped(client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(projects_dir, "proj", "untimed", [
        {"cwd": str(proj), "type": "summary", "summary": "no timestamps here"},
    ], mtime=STALE)
    _write(projects_dir, "proj", "timed", [
        {"cwd": str(proj), **_user("hi", "2026-08-16T08:00:00Z")},
    ], mtime=STALE)
    assert [s["session_id"] for s in _get(client)] == ["timed"]


def test_tool_result_only_user_entries_do_not_become_the_title(
        client, projects_dir, state_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_assistant("resuming", "2026-08-16T08:00:00Z")},
        _tool_result("2026-08-16T08:00:01Z"),
        _user("Actually, revert that", "2026-08-16T08:00:02Z"),
    ], mtime=STALE)
    session = _get(client)[0]
    assert session["name"] == "Actually, revert that"
    # the tool_result entry is still a real (non-housekeeping) transcript
    # line, so it doesn't affect where the session starts
    assert session["started_at"] == "2026-08-16T08:00:00+00:00"


def test_cwd_falls_back_to_the_decoded_project_dirname(
        client, projects_dir, state_dir):
    # No cwd recorded anywhere in the transcript: all that's left is the
    # (lossily) encoded directory name.
    _write(projects_dir, "-tmp-myproj", "s1", [
        _user("hi", "2026-08-16T08:00:00Z"),
    ], mtime=STALE)
    assert _get(client)[0]["cwd"] == "/tmp/myproj"


def test_missing_projects_dir_and_state_are_empty_not_an_error(
        client, projects_dir, state_dir):
    # No transcripts, no session_names.json, no triage.json.
    assert client.get("/api/claude-sessions/summaries").json() == {"sessions": []}


def test_head_cache_picks_up_a_prompt_that_arrives_later(
        client, projects_dir, state_dir, tmp_path):
    """The head parse is cached, but a session whose first user message hadn't
    landed yet must not be stuck on the placeholder name forever."""
    proj = tmp_path / "proj"
    proj.mkdir()
    path = _write(projects_dir, "proj", "s1", [
        {"cwd": str(proj), **_assistant("thinking", "2026-08-16T08:00:00Z")},
    ], mtime=STALE)
    assert _get(client)[0]["name"] == "(no user message)"

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_user("Now a real prompt", "2026-08-16T08:01:00Z")) + "\n")
    os.utime(path, (STALE, STALE))

    session = _get(client)[0]
    assert session["name"] == "Now a real prompt"
    assert session["last_active"] == "2026-08-16T08:01:00+00:00"


def test_head_cache_does_not_serve_a_replaced_transcript(
        client, projects_dir, state_dir, tmp_path):
    """Transcripts are append-only, so the cached head stays valid as the file
    grows — but a file that shrank is a different file and must be re-read,
    including when it shrinks back to a size seen earlier."""
    proj = tmp_path / "proj"
    proj.mkdir()
    first = {"cwd": str(proj), **_user("Original prompt", "2026-08-16T08:00:00Z")}
    path = _write(projects_dir, "proj", "s1", [first], mtime=STALE)
    assert _get(client)[0]["name"] == "Original prompt"

    # grow (cache still valid), then replace with content that happens to be
    # exactly the original length — the shrink must still be noticed
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_assistant("working", "2026-08-16T08:01:00Z")) + "\n")
    os.utime(path, (STALE, STALE))
    assert _get(client)[0]["name"] == "Original prompt"

    replaced = {"cwd": str(proj), **_user("Replaced prompt", "2026-08-16T09:00:00Z")}
    path.write_text(json.dumps(replaced) + "\n")
    assert len(json.dumps(replaced)) == len(json.dumps(first))  # same size on disk
    os.utime(path, (STALE, STALE))

    session = _get(client)[0]
    assert session["name"] == "Replaced prompt"
    assert session["started_at"] == "2026-08-16T09:00:00+00:00"


# -- POST /api/claude-sessions/triage — the Board's drag writes ---------------
# The shell's kanban drags a chat card between In Progress / Done / Archive;
# the write lands in the SAME triage.json the Inbox owns, merge-not-replace.

def test_triage_write_sets_status_and_reads_back(projects_dir, state_dir, client):
    _write(projects_dir, "-p", "s1",
           [_user("hi", "2026-08-16T09:00:00Z")], mtime=STALE)
    r = client.post("/api/claude-sessions/triage",
                    json={"session_id": "s1", "status": "archived"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _get(client)[0]["status"] == "archived"


def test_triage_write_preserves_the_records_other_keys(state_dir, client):
    """`status` and its stamp are the only fields this endpoint touches — a note
    the user typed in the Inbox must survive a drag on the Board."""
    path = os.path.join(str(state_dir), "triage.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"s1": {"status": "done", "note": "keep me", "read": "1"}}, f)
    r = client.post("/api/claude-sessions/triage",
                    json={"session_id": "s1", "status": "in_progress"})
    assert r.status_code == 200
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)["s1"]
    assert rec["note"] == "keep me"
    assert rec["read"] == "1"
    assert rec["status"] == "in_progress"
    # Stamped, so `_pin_holds` can tell this deliberate pin from an automatic
    # one. The set of keys is asserted whole so a future field has to be a
    # decision rather than a leak.
    assert set(rec) == {"status", "note", "read", "at"}
    assert float(rec["at"]) > 0


def test_triage_write_refuses_a_status_it_does_not_speak(state_dir, client):
    r = client.post("/api/claude-sessions/triage",
                    json={"session_id": "s1", "status": "upcoming"})
    assert r.status_code == 400
    assert not os.path.exists(os.path.join(str(state_dir), "triage.json"))


def test_triage_write_refuses_a_blank_session_id(state_dir, client):
    r = client.post("/api/claude-sessions/triage",
                    json={"session_id": "  ", "status": "done"})
    assert r.status_code == 400
