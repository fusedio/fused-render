"""GET /api/claude-artifacts (server/routers/claude_artifacts.py): the Artifacts
published from this machine's Claude Code sessions, recovered from the session
transcripts at ~/.claude/projects/<encoded-cwd>/*.jsonl — one row per hosted
url no matter how many times it was republished (or from how many sessions),
newest update first, with the author's description/favicon joined in from the
Artifact tool call and `exists` reporting whether the published local file is
still there.
"""
import json
import shutil

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_artifacts as claude_artifacts_mod
from fused_render.server import create_app

URL_A = "https://claude.ai/code/artifact/aaaaaaaa-0000-0000-0000-000000000000"
URL_B = "https://claude.ai/code/artifact/bbbbbbbb-0000-0000-0000-000000000000"


@pytest.fixture()
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(claude_artifacts_mod, "PROJECTS_DIR", str(d))
    # The per-transcript cache is module-level and keyed by absolute path, so a
    # tmp path can't collide with another test's — but reset it anyway so a test
    # that rewrites a transcript in place (same size, same mtime second) can't
    # be served a stale parse.
    claude_artifacts_mod.reset_cache()
    return d


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _frame_link(session_id, path, url, title, timestamp):
    return {"type": "frame-link", "sessionId": session_id, "path": str(path),
            "frameUrl": url, "title": title, "timestamp": timestamp}


def _publish(path, **extra):
    """An assistant record carrying one Artifact tool call, the only place the
    author's description/favicon appear."""
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Artifact",
         "input": {"file_path": str(path), **extra}}]}}


def _session(projects_dir, encoded_dir, session_id, cwd, records):
    d = projects_dir / encoded_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    lines = [json.dumps({"cwd": cwd, "sessionId": session_id,
                         "timestamp": "2026-01-01T00:00:00Z"})]
    lines += [r if isinstance(r, str) else json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_lists_publish_with_tool_call_metadata_merged(client, projects_dir, tmp_path):
    page = tmp_path / "page.html"
    page.write_text("<title>Bali</title>")
    _session(projects_dir, "-tmp-proj", "s1", "/tmp/proj", [
        _publish(page, favicon="\U0001f93f", description="Course picker."),
        _frame_link("s1", page, URL_A, "Bali Open Water", "2026-07-16T09:43:32.223Z"),
    ])
    artifacts = client.get("/api/claude-artifacts").json()["artifacts"]
    assert len(artifacts) == 1
    entry = artifacts[0]
    assert entry["file_path"] == str(page)
    assert entry["remote_url"] == URL_A
    assert entry["title"] == "Bali Open Water"
    assert entry["description"] == "Course picker."
    assert entry["favicon"] == "\U0001f93f"
    assert entry["session_id"] == "s1"
    assert entry["cwd"] == "/tmp/proj"
    assert entry["exists"] is True
    assert entry["created_at"] == entry["updated_at"] == pytest.approx(1784195012.223)


def test_republished_page_is_one_row_with_latest_title_and_full_span(
    client, projects_dir, tmp_path
):
    # Every redeploy writes another frame-link for the same frameUrl. That is one
    # artifact with a history: newest publish wins for what's shown, and the
    # dates span first publish to last.
    page = tmp_path / "page.html"
    page.write_text("x")
    _session(projects_dir, "-tmp-proj", "s1", "/tmp/proj", [
        _publish(page, description="first pass"),
        _frame_link("s1", page, URL_A, "First Title", "2026-07-16T09:00:00.000Z"),
        _publish(page, description="second pass", favicon="\U0001f422"),
        _frame_link("s1", page, URL_A, "Latest Title", "2026-07-16T11:00:00.000Z"),
    ])
    artifacts = client.get("/api/claude-artifacts").json()["artifacts"]
    assert len(artifacts) == 1
    entry = artifacts[0]
    assert entry["title"] == "Latest Title"
    # Latest tool call wins for the metadata too.
    assert entry["description"] == "second pass"
    assert entry["favicon"] == "\U0001f422"
    assert entry["created_at"] == pytest.approx(1784192400.0)
    assert entry["updated_at"] == pytest.approx(1784199600.0)


def test_exists_reports_whether_the_published_file_is_still_there(
    client, projects_dir, tmp_path
):
    # Artifacts are routinely published from scratchpads that get cleaned up.
    here = tmp_path / "here.html"
    here.write_text("<title>here</title>")
    gone = tmp_path / "gone.html"  # never created
    _session(projects_dir, "-tmp-proj", "s1", "/tmp/proj", [
        _frame_link("s1", here, URL_A, "Here", "2026-07-16T10:00:00.000Z"),
        _frame_link("s1", gone, URL_B, "Gone", "2026-07-16T09:00:00.000Z"),
    ])
    artifacts = client.get("/api/claude-artifacts").json()["artifacts"]
    assert {a["file_path"]: a["exists"] for a in artifacts} == {
        str(here): True, str(gone): False}


def test_missing_projects_dir_is_empty_not_an_error(client, projects_dir):
    shutil.rmtree(projects_dir)
    assert client.get("/api/claude-artifacts").json() == {"artifacts": []}


def test_malformed_lines_and_list_calls_are_skipped(client, projects_dir, tmp_path):
    page = tmp_path / "page.html"
    page.write_text("x")
    _session(projects_dir, "-tmp-proj", "s1", "/tmp/proj", [
        '{"type": "frame-link", "path": "/x", truncated',  # unparseable, skipped
        # An `action: "list"` call publishes nothing, and neither does a call
        # with no file_path — neither may contribute metadata.
        _publish(page, description="from a list call", action="list"),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Artifact",
             "input": {"action": "list", "description": "no file_path"}}]}},
        _publish(page, description="real publish"),
        _frame_link("s1", page, URL_A, "Real", "2026-07-16T10:00:00.000Z"),
    ])
    artifacts = client.get("/api/claude-artifacts").json()["artifacts"]
    assert [(a["remote_url"], a["description"]) for a in artifacts] == [
        (URL_A, "real publish")]


def test_tool_call_without_a_frame_link_is_not_listed(client, projects_dir, tmp_path):
    # The frame-link is the authoritative publish record; without one there is
    # no hosted url, so there is nothing to list.
    page = tmp_path / "page.html"
    page.write_text("x")
    _session(projects_dir, "-tmp-proj", "s1", "/tmp/proj", [
        _publish(page, description="never landed"),
    ])
    assert client.get("/api/claude-artifacts").json() == {"artifacts": []}


def test_same_url_published_from_two_sessions_collapses_to_one_row(
    client, projects_dir, tmp_path
):
    # An artifact can be updated from a later session via the tool's `url`
    # param, so the dedupe has to run across transcripts, not just within one.
    first = tmp_path / "first.html"
    first.write_text("x")
    second = tmp_path / "second.html"
    second.write_text("x")
    _session(projects_dir, "-tmp-a", "old", "/tmp/a", [
        _publish(first, description="original"),
        _frame_link("old", first, URL_A, "Original", "2026-07-16T09:00:00.000Z"),
    ])
    _session(projects_dir, "-tmp-b", "new", "/tmp/b", [
        _publish(second, description="updated elsewhere"),
        _frame_link("new", second, URL_A, "Updated", "2026-07-17T09:00:00.000Z"),
    ])
    artifacts = client.get("/api/claude-artifacts").json()["artifacts"]
    assert len(artifacts) == 1
    entry = artifacts[0]
    # Latest publish owns every display field, including which session/cwd it
    # is now being worked on from; created_at is still the first publish.
    assert entry["title"] == "Updated"
    assert entry["description"] == "updated elsewhere"
    assert entry["file_path"] == str(second)
    assert entry["session_id"] == "new"
    assert entry["cwd"] == "/tmp/b"
    assert entry["created_at"] == pytest.approx(1784192400.0)
    assert entry["updated_at"] == pytest.approx(1784278800.0)


def test_sorted_newest_update_first(client, projects_dir, tmp_path):
    page = tmp_path / "page.html"
    page.write_text("x")
    _session(projects_dir, "-tmp-a", "s1", "/tmp/a", [
        _frame_link("s1", page, URL_A, "Older", "2026-07-16T09:00:00.000Z"),
    ])
    _session(projects_dir, "-tmp-b", "s2", "/tmp/b", [
        _frame_link("s2", page, URL_B, "Newer", "2026-07-20T09:00:00.000Z"),
    ])
    artifacts = client.get("/api/claude-artifacts").json()["artifacts"]
    assert [a["title"] for a in artifacts] == ["Newer", "Older"]


def test_unchanged_transcript_is_not_reparsed_but_exists_stays_live(
    client, projects_dir, tmp_path, monkeypatch
):
    # The per-file cache is what makes a repeat request cheap against a
    # hundreds-of-MB transcript store; `exists` is deliberately outside it.
    page = tmp_path / "page.html"
    page.write_text("x")
    _session(projects_dir, "-tmp-proj", "s1", "/tmp/proj", [
        _frame_link("s1", page, URL_A, "Cached", "2026-07-16T10:00:00.000Z"),
    ])
    assert client.get("/api/claude-artifacts").json()["artifacts"][0]["exists"] is True

    calls = []
    real_parse = claude_artifacts_mod._parse_transcript
    monkeypatch.setattr(
        claude_artifacts_mod, "_parse_transcript",
        lambda p: (calls.append(p), real_parse(p))[1])
    page.unlink()
    artifacts = client.get("/api/claude-artifacts").json()["artifacts"]
    assert calls == []
    assert artifacts[0]["exists"] is False
