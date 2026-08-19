"""Claude project-folder endpoints (server/routers/claude_sessions.py): the
exhaustive GET /api/claude-sessions and Home's newest-first, early-stopping GET
/api/claude-sessions/home. Both read the real folder from each transcript's
``cwd`` and drop folders no longer on disk.
"""
import json
import os
import shutil

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
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


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
    assert [f["path"] for f in data["folders"]] == [str(real)]


def test_sorted_by_latest_session_first(client, projects_dir, tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    _session(projects_dir, "proj-a", "s1", str(a), mtime=1000)
    _session(projects_dir, "proj-b", "s1", str(b), mtime=2000)
    data = client.get("/api/claude-sessions").json()
    assert [f["path"] for f in data["folders"]] == [str(b), str(a)]


def test_multiple_sessions_in_one_folder_collapse_to_one_row(client, projects_dir, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _session(projects_dir, "proj", "old", str(proj), mtime=1000)
    _session(projects_dir, "proj", "new", str(proj), mtime=5000)
    data = client.get("/api/claude-sessions").json()
    assert [f["path"] for f in data["folders"]] == [str(proj)]


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
        str(folders[19]), str(folders[18]), str(folders[17]),
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

    assert [f["path"] for f in data["folders"]] == [str(a), str(b), str(c)]
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

    assert [f["path"] for f in data["folders"]] == [str(proj)]
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

    assert [f["path"] for f in data["folders"]] == [str(proj)]
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
    assert [f["path"] for f in data["folders"]] == [str(hyphened)]
    exhaustive = client.get("/api/claude-sessions").json()
    assert sorted(f["path"] for f in exhaustive["folders"]) == sorted(
        [str(hyphened), str(nested)])


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
