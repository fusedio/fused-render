"""GET /api/claude-sessions (server/routers/claude_sessions.py): project
folders holding Claude Code session transcripts, for the Explorer homepage's
"Claude sessions" tab — one row per real project folder (read from each
transcript's own `cwd`), newest session first, folders no longer on disk
dropped.
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
