"""Claude project-folder endpoints (server/routers/claude_sessions.py): the
exhaustive GET /api/claude-sessions and Home's newest-first, early-stopping GET
/api/claude-sessions/home. Both read the real folder from each transcript's
``cwd`` and drop folders no longer on disk.
"""
import json
import os
import shutil
import time

import pytest
from fastapi.testclient import TestClient

from fused_render._view_url_codec import canonical_fs_path
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as claude_sessions_mod


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(claude_sessions_mod, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _c(path) -> str:
    """A folder's expected wire form: both endpoints canonicalize `path` to
    forward slashes on the way out (see canonical_fs_path), even though the
    transcript's own `cwd` and this test's `str(Path(...))` are backslashed
    on Windows."""
    return canonical_fs_path(str(path))


def _session(projects_dir, encoded_dir, session_id, cwd, mtime=None):
    d = projects_dir / encoded_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(json.dumps({"cwd": cwd, "timestamp": "2026-01-01T00:00:00Z"}) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_groups_by_transcript_cwd_not_encoded_dirname(client, projects_dir, tmp_path):
    # A hyphen in the real folder name decodes wrong from the encoded dirname
    # (Claude Code turns both '/' and '-' into '-'), so the endpoint must use
    # the transcript's own cwd rather than decoding "-tmp-my-project".
    real = tmp_path / "my-project"
    real.mkdir()
    _session(projects_dir, "-tmp-my-project", "s1", str(real))
    data = client.get("/api/claude-sessions").json()
    assert [f["path"] for f in data["folders"]] == [_c(real)]


def test_sorted_by_latest_session_first(client, projects_dir, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    _session(projects_dir, "proj-a", "s1", str(a), mtime=1000)
    _session(projects_dir, "proj-b", "s1", str(b), mtime=2000)
    data = client.get("/api/claude-sessions").json()
    assert [f["path"] for f in data["folders"]] == [_c(b), _c(a)]


def test_multiple_sessions_in_one_folder_collapse_to_one_row(client, projects_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _session(projects_dir, "proj", "old", str(proj), mtime=1000)
    _session(projects_dir, "proj", "new", str(proj), mtime=5000)
    data = client.get("/api/claude-sessions").json()
    assert [f["path"] for f in data["folders"]] == [_c(proj)]


def test_folder_no_longer_on_disk_is_dropped(client, projects_dir, tmp_path):
    gone = tmp_path / "gone"  # never created on disk
    _session(projects_dir, "gone", "s1", str(gone))
    data = client.get("/api/claude-sessions").json()
    assert data["folders"] == []


def test_missing_projects_dir_is_empty_not_an_error(client, projects_dir):
    shutil.rmtree(projects_dir)
    assert client.get("/api/claude-sessions").json() == {"folders": []}


# --------------------------------------------------- Home's bounded hydration

def test_home_opens_only_enough_newest_transcripts_to_fill_the_row(
        client, projects_dir, tmp_path, monkeypatch):
    folders = []
    for i in range(20):
        folder = tmp_path / f"folder-{i:02d}"
        folder.mkdir()
        folders.append(folder)
        _session(projects_dir, f"project-{i:02d}", f"s{i}", str(folder), mtime=1000 + i)

    real_session_cwd = claude_sessions_mod._session_cwd
    opened = []

    def counted_session_cwd(path):
        opened.append(path)
        return real_session_cwd(path)

    monkeypatch.setattr(claude_sessions_mod, "_session_cwd", counted_session_cwd)
    data = client.get("/api/claude-sessions/home", params={"limit": 3}).json()

    assert [f["path"] for f in data["folders"]] == [
        _c(folders[19]), _c(folders[18]), _c(folders[17]),
    ]
    assert len(opened) == 3


def test_home_skips_duplicate_and_missing_folders_until_the_row_is_full(
        client, projects_dir, tmp_path, monkeypatch):
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    for folder in (a, b, c):
        folder.mkdir()
    missing = tmp_path / "missing"
    _session(projects_dir, "a", "new", str(a), mtime=600)
    _session(projects_dir, "a", "old", str(a), mtime=500)
    _session(projects_dir, "missing", "gone", str(missing), mtime=400)
    _session(projects_dir, "b", "one", str(b), mtime=300)
    _session(projects_dir, "c", "one", str(c), mtime=200)
    # This older valid folder proves parsing stops exactly when the row fills.
    older = tmp_path / "older"
    older.mkdir()
    _session(projects_dir, "older", "one", str(older), mtime=100)

    real_session_cwd = claude_sessions_mod._session_cwd
    opened = []

    def counted_session_cwd(path):
        opened.append(path)
        return real_session_cwd(path)

    monkeypatch.setattr(claude_sessions_mod, "_session_cwd", counted_session_cwd)
    data = client.get("/api/claude-sessions/home", params={"limit": 3}).json()

    assert [f["path"] for f in data["folders"]] == [_c(a), _c(b), _c(c)]
    # Four, not five: a/old is never opened. Its directory was already resolved
    # by a/new, and a directory name IS the encoded cwd, so the second file
    # could only have reproduced a folder already in the row.
    assert len(opened) == 4


def test_home_opens_one_transcript_per_project_directory(
        client, projects_dir, tmp_path, monkeypatch):
    """The row's cost is directories touched, not sessions held.

    A long-running project accumulates transcripts; every one of them records
    the same cwd, and each open reads the head of a file that can be megabytes.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    for i in range(10):
        _session(projects_dir, "proj", f"s{i}", str(proj), mtime=1000 + i)

    opened = []
    real_session_cwd = claude_sessions_mod._session_cwd

    def counted_session_cwd(path):
        opened.append(path)
        return real_session_cwd(path)

    monkeypatch.setattr(claude_sessions_mod, "_session_cwd", counted_session_cwd)
    data = client.get("/api/claude-sessions/home", params={"limit": 3}).json()

    assert [f["path"] for f in data["folders"]] == [_c(proj)]
    assert opened == [str(projects_dir / "proj" / "s9.jsonl")]


def test_home_falls_through_to_older_transcripts_of_an_unreadable_newest(
        client, projects_dir, tmp_path, monkeypatch):
    """A directory counts as resolved only once a cwd was actually read.

    The newest transcript is the one still being written, so a truncated first
    line is the normal way this happens - dropping the folder for it would hide
    the project the user is working in right now, which is the row's whole point.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    _session(projects_dir, "proj", "readable", str(proj), mtime=1000)
    headless = projects_dir / "proj" / "truncated.jsonl"
    headless.write_text('{"cwd": "/tmp/half-writ')  # no closing brace: unparseable
    os.utime(headless, (2000, 2000))

    opened = []
    real_session_cwd = claude_sessions_mod._session_cwd

    def counted_session_cwd(path):
        opened.append(path)
        return real_session_cwd(path)

    monkeypatch.setattr(claude_sessions_mod, "_session_cwd", counted_session_cwd)
    data = client.get("/api/claude-sessions/home", params={"limit": 3}).json()

    assert [f["path"] for f in data["folders"]] == [_c(proj)]
    assert opened == [str(headless), str(projects_dir / "proj" / "readable.jsonl")]


def test_home_collapses_a_collided_directory_to_its_newest_folder(
        client, projects_dir, tmp_path):
    """Two real folders can encode to one directory, since both the separator
    and a literal hyphen become "-". The row shows the newest of them; the
    exhaustive endpoint above is the one that reads every transcript and lists
    both."""
    hyphened = tmp_path / "my-project"
    nested = tmp_path / "my" / "project"
    hyphened.mkdir()
    nested.mkdir(parents=True)
    _session(projects_dir, "collide", "older", str(nested), mtime=1000)
    _session(projects_dir, "collide", "newer", str(hyphened), mtime=2000)

    data = client.get("/api/claude-sessions/home", params={"limit": 3}).json()
    assert [f["path"] for f in data["folders"]] == [_c(hyphened)]
    exhaustive = client.get("/api/claude-sessions").json()
    assert sorted(f["path"] for f in exhaustive["folders"]) == sorted(
        [_c(hyphened), _c(nested)])


def test_home_session_limit_is_capped_to_its_single_row(
        client, projects_dir, tmp_path, monkeypatch):
    for i in range(15):
        folder = tmp_path / f"folder-{i:02d}"
        folder.mkdir()
        _session(projects_dir, f"project-{i:02d}", f"s{i}", str(folder), mtime=1000 + i)

    real_session_cwd = claude_sessions_mod._session_cwd
    opened = 0

    def counted_session_cwd(path):
        nonlocal opened
        opened += 1
        return real_session_cwd(path)

    monkeypatch.setattr(claude_sessions_mod, "_session_cwd", counted_session_cwd)
    data = client.get("/api/claude-sessions/home", params={"limit": 999}).json()
    assert len(data["folders"]) == claude_sessions_mod.HOME_SESSION_LIMIT
    assert opened == claude_sessions_mod.HOME_SESSION_LIMIT


def test_home_missing_projects_dir_is_empty(client, projects_dir):
    shutil.rmtree(projects_dir)
    assert client.get("/api/claude-sessions/home").json() == {"folders": []}


# ── GET /api/claude-sessions/liveness — has this conversation moved? (D415) ──
#
# The claude chat's transcript-follower asks this on every lap for a session it
# is showing and has no run of: a turn driven from a terminal writes no run dir,
# so `live_run` cannot see it and the chat used to need a manual reload. One
# stat, plus `session_liveness`' own running rule — never a second opinion about
# what "mid-turn" means.

def _liveness(client, path):
    return client.get("/api/claude-sessions/liveness", params={"path": str(path)})


def test_liveness_reports_the_stat_the_page_watermarks_by(client, projects_dir,
                                                          tmp_path):
    path = _session(projects_dir, "proj", "s1", str(tmp_path), mtime=1000)
    body = _liveness(client, path).json()
    assert body["exists"] is True
    assert body["mtime"] == 1000
    assert body["size"] == path.stat().st_size
    # Both halves of the pair matter: a coarse clock can land two appends in one
    # mtime tick, and the size is what tells them apart.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user"}) + "\n")
    os.utime(path, (1000, 1000))
    assert _liveness(client, path).json()["size"] > body["size"]


def test_liveness_running_is_the_last_message_not_the_activity_window(
        client, projects_dir, tmp_path, monkeypatch):
    """`transcript_turn_open`, NOT the 45s window the Inbox badge shares. The
    window is right for a badge and wrong under an open conversation: a run
    started outside this app writes no turn-end record, so the window kept a
    shimmering line under a reply that had already landed. Pinned by which
    function is asked, because both answer a bool and only one is honest at the
    moment a turn ends."""
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    seen = {}

    def never(p, now):
        raise AssertionError("the activity window must not decide this")

    def fake(p, now):
        seen["path"] = p
        return True

    monkeypatch.setattr(claude_sessions_mod.session_liveness,
                        "transcript_running", never)
    monkeypatch.setattr(claude_sessions_mod.session_liveness,
                        "transcript_turn_open", fake)
    assert _liveness(client, path).json()["running"] is True
    assert seen["path"] == os.path.realpath(str(path))


def _rows(path, *rows):
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


def _now_ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _assistant(*blocks, ts=None, stop=None):
    message = {"role": "assistant", "content": [{"type": b} for b in blocks]}
    if stop is not None:
        message["stop_reason"] = stop
    return {"type": "assistant", "timestamp": ts or _now_ts(),
            "message": message}


def test_a_reply_that_landed_ends_the_turn_the_moment_it_lands(client,
                                                               projects_dir,
                                                               tmp_path):
    """The bug this rule replaced, stated as a test: the turn is over when the
    reply is written, not 45 seconds later."""
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    _rows(path, {"type": "user", "timestamp": "2026-01-01T00:00:00Z"},
          _assistant("text"),
          # ...and the records Claude Code drops afterwards do not revive it.
          {"type": "last-prompt"}, {"type": "mode"})
    assert _liveness(client, path).json()["running"] is False


def test_a_prompt_with_no_reply_yet_is_a_turn_in_flight(client, projects_dir,
                                                        tmp_path):
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    _rows(path, _assistant("text"),
          {"type": "user", "timestamp": _now_ts()})
    assert _liveness(client, path).json()["running"] is True


def test_a_stale_prompt_under_a_fresh_mtime_is_not_in_flight(client,
                                                             projects_dir,
                                                             tmp_path):
    """Housekeeping appends (an away summary, an ai-title) bump the file's
    mtime hours after a turn died, so the mtime gate alone let a long-dead
    user row shimmer again (D420). The deciding ROW's own timestamp is
    measured against the same stale ceiling."""
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    _rows(path, {"type": "user", "timestamp": "2026-01-01T00:00:00Z"},
          {"type": "ai-title", "aiTitle": "old thread"})
    assert _liveness(client, path).json()["running"] is False


def test_an_assistant_still_calling_tools_is_a_turn_in_flight(client,
                                                              projects_dir,
                                                              tmp_path):
    """Mid-turn is the common case for a long agent run, and the reply text that
    precedes a tool call must not read as the end of it."""
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    _rows(path, {"type": "user", "timestamp": _now_ts()},
          _assistant("text", "tool_use"))
    assert _liveness(client, path).json()["running"] is True


def test_a_pause_for_thought_is_not_the_end_of_the_turn(client, projects_dir,
                                                        tmp_path):
    """THE ROW IS A BLOCK, NOT A REPLY (D420, from the board wearing Done over
    a chat visibly running tools): the CLI writes each content block as its
    own assistant row, so mid-turn the newest message is a thinking-only or
    text-only row for the whole length of the tool call it precedes. Its
    `stop_reason: tool_use` is what says the turn goes on."""
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    _rows(path, {"type": "user", "timestamp": _now_ts()},
          _assistant("thinking", stop="tool_use"))
    assert _liveness(client, path).json()["running"] is True
    _rows(path, {"type": "user", "timestamp": _now_ts()},
          _assistant("text", stop="tool_use"))
    assert _liveness(client, path).json()["running"] is True


def test_a_reply_stamped_end_turn_is_over_whatever_its_blocks(client,
                                                              projects_dir,
                                                              tmp_path):
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    _rows(path, {"type": "user", "timestamp": _now_ts()},
          _assistant("text", stop="end_turn"))
    assert _liveness(client, path).json()["running"] is False


def test_a_turn_left_open_by_a_dead_process_does_not_shimmer_forever(
        client, projects_dir, tmp_path):
    """A terminal closed mid-reply leaves a user row as the file's last word,
    and no one is ever coming back to answer it. STALE_TAIL_SEC is the ceiling
    on how long that lie may stand — the same ceiling the window rule uses."""
    path = _session(projects_dir, "proj", "s1", str(tmp_path))
    _rows(path, {"type": "user", "timestamp": "2026-01-01T00:00:00Z"})
    old = time.time() - claude_sessions_mod.session_liveness.STALE_TAIL_SEC - 5
    os.utime(path, (old, old))
    assert _liveness(client, path).json()["running"] is False


def test_liveness_of_a_transcript_not_written_yet_is_not_an_error(
        client, projects_dir):
    """A chat can be open on a session whose first turn is still being written;
    the watch's next lap is where that gets noticed, not an exception here."""
    body = _liveness(client, projects_dir / "proj" / "unborn.jsonl").json()
    assert body == {"exists": False, "mtime": 0.0, "size": 0, "running": False}


def test_liveness_refuses_anything_outside_the_projects_tree(client, tmp_path,
                                                             projects_dir):
    """The parameter is a PATH, so the guard is the endpoint's whole security
    story: a stat of an arbitrary file is still a read of the filesystem."""
    outside = tmp_path / "secrets.jsonl"
    outside.write_text("{}")
    assert _liveness(client, outside).status_code == 400
    assert _liveness(client, projects_dir / "proj" / "s1.txt").status_code == 400
    escape = projects_dir / "proj" / ".." / ".." / "secrets.jsonl"
    assert _liveness(client, escape).status_code == 400
