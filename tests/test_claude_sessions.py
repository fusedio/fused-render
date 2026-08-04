"""Session-transcript discovery and the related-parts pivots (D217).

`claude_sessions` reads Claude Code's own journals — another application's
private, evolving format — so the load-bearing behaviour is what survives
hostile input: torn lines, records shaped like nothing we know, relative
paths, ids that try to be paths. The fixtures write real transcripts into a
real (tmp) config dir rather than mocking reads, same discipline as the other
`claude_*` source tests.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from fused_render import claude_sessions, claude_uploads
from fused_render.server import create_app


@pytest.fixture(autouse=True)
def fresh_cache():
    """The parse cache is module-global and keyed by path; tmp paths never
    collide across tests, but a test must still start from a cold cache to
    mean what it says about parsing."""
    claude_sessions.invalidate_cache()
    yield
    claude_sessions.invalidate_cache()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A real (tmp) Claude config dir the module is pointed at."""
    config = tmp_path / "claude"
    (config / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    return config


def _user_line(cwd, text, sidechain=False):
    return json.dumps({
        "type": "user", "cwd": str(cwd), "isSidechain": sidechain,
        "timestamp": "2026-08-04T10:00:00.000Z",
        "message": {"role": "user", "content": text},
    })


def _write_line(path, ts="2026-08-04T10:05:00.000Z", tool="Write"):
    return json.dumps({
        "type": "assistant", "timestamp": ts,
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "writing"},
            {"type": "tool_use", "name": tool, "id": "t1",
             "input": {"file_path": str(path), "content": "…"}},
        ]},
    })


def _delta_line(parent, name, ts="2026-08-04T10:06:00.000Z"):
    return json.dumps({
        "type": "file-history-delta", "timestamp": ts,
        "trackingPath": f"repo-relative/{name}",
        "backup": {"realParentDir": str(parent), "backupFileName": "x@v1",
                   "version": 1, "backupTime": ts},
    })


def _transcript(store, session, lines, slug="-home-someone"):
    d = store / "projects" / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _saved(tmp_path, name, content="<title>Hi</title>"):
    out = tmp_path / "work"
    out.mkdir(exist_ok=True)
    f = out / name
    f.write_text(content, encoding="utf-8")
    return f


# ------------------------------------------------------------------- listing

def test_lists_viewable_files_a_session_wrote(store, tmp_path):
    page = _saved(tmp_path, "report.html")
    figure = _saved(tmp_path, "figure.png", "png-bytes")
    _transcript(store, "s1", [
        _user_line(tmp_path / "work", "make me a report"),
        _write_line(page),
        _write_line(figure, tool="Edit"),
    ])
    apps = claude_sessions.list_apps(str(tmp_path / "unused-workspace"))
    by_name = {a["name"]: a for a in apps}
    assert set(by_name) == {"report.html", "figure.png"}
    assert by_name["report.html"]["entry_html"] == str(page)
    assert by_name["report.html"]["title"] == "Hi"
    assert by_name["figure.png"]["entry_html"] is None
    assert by_name["figure.png"]["entry"] == str(figure)
    for app in apps:
        assert app["source"] == "claude-session"
        assert app["tag"] == "work", "tag is the session cwd's basename"
        assert app["path"] == app["entry"], "a FILE source: path IS the file"


def test_notebook_edits_are_seen_despite_their_own_path_key(store, tmp_path):
    """NotebookEdit names its target `notebook_path`, not `file_path` (raised
    in review) — with only the latter scanned, notebook edits were invisible
    to every pivot: the prefilter skipped the line before the parse could."""
    nb = _saved(tmp_path, "analysis.ipynb", "{}")
    _transcript(store, "s1", [
        _user_line(tmp_path / "work", "run the notebook"),
        json.dumps({
            "type": "assistant", "timestamp": "2026-08-04T10:05:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "NotebookEdit", "id": "t1",
                 "input": {"notebook_path": str(nb), "new_source": "…"}},
            ]},
        }),
    ])
    files = claude_sessions.session_files("s1")["files"]
    assert [f["path"] for f in files] == [str(nb)]
    assert claude_sessions.sessions_for_file(str(nb))[0]["writes"] == 1


def test_checkpoint_deltas_count_too_not_just_write_tools(store, tmp_path):
    """A file-history-delta names a file changed by ANY tool — a session that
    edited a page through some future tool still gets its card."""
    page = _saved(tmp_path, "notebook.html")
    _transcript(store, "s1", [
        _user_line(tmp_path / "work", "tweak it"),
        _delta_line(page.parent, page.name),
    ])
    apps = claude_sessions.list_apps(str(tmp_path / "unused"))
    assert [a["name"] for a in apps] == ["notebook.html"]


def test_what_is_refused(store, tmp_path):
    """Source code, repo furniture, hidden trees, vanished files, relative
    paths, and files in stores other sources own — none earn a card."""
    kept = _saved(tmp_path, "kept.md", "# real report")
    code = _saved(tmp_path, "script.py", "print()")
    readme = _saved(tmp_path, "README.md", "# furniture")
    hidden_dir = tmp_path / ".secrets"
    hidden_dir.mkdir()
    hidden = hidden_dir / "page.html"
    hidden.write_text("x", encoding="utf-8")
    gone = tmp_path / "work" / "deleted.html"  # never created
    workspace = tmp_path / "Fused"
    inside_ws = workspace / "local" / "app" / "index.html"
    inside_ws.parent.mkdir(parents=True)
    inside_ws.write_text("x", encoding="utf-8")
    in_config = store / "CLAUDE.md"
    in_config.write_text("x", encoding="utf-8")
    _transcript(store, "s1", [
        _user_line(tmp_path / "work", "do things"),
        _write_line(kept), _write_line(code), _write_line(readme),
        _write_line(hidden), _write_line(gone), _write_line(inside_ws),
        _write_line(in_config),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "relative/report.html"}}]}}),
    ])
    apps = claude_sessions.list_apps(str(workspace))
    assert [a["name"] for a in apps] == ["kept.md"]


def test_a_file_two_sessions_wrote_is_one_card(store, tmp_path):
    page = _saved(tmp_path, "shared.html")
    _transcript(store, "s1", [_user_line(tmp_path, "a"), _write_line(page)])
    _transcript(store, "s2", [_user_line(tmp_path, "b"), _write_line(page)],
                slug="-other-slug")
    assert len(claude_sessions.list_apps(str(tmp_path / "unused"))) == 1


def test_garbage_lines_cost_a_line_never_the_transcript(store, tmp_path):
    page = _saved(tmp_path, "ok.html")
    _transcript(store, "s1", [
        "not json at all {{{",
        json.dumps(["a", "list", "not", "a", "dict"]),
        json.dumps({"type": "assistant", "message": "not-a-dict",
                    "timestamp": 12345}),
        json.dumps({"type": "file-history-delta", "trackingPath": None,
                    "backup": "nope"}),
        _write_line(page),
        '{"torn tail from a live session":',
    ])
    apps = claude_sessions.list_apps(str(tmp_path / "unused"))
    assert [a["name"] for a in apps] == ["ok.html"]


def test_the_cap_stops_the_walk_not_just_the_output(store, tmp_path, monkeypatch):
    pages = [_saved(tmp_path, f"p{i}.html") for i in range(5)]
    _transcript(store, "s1",
                [_user_line(tmp_path, "x")] + [_write_line(p) for p in pages])
    monkeypatch.setattr(claude_sessions, "MAX_APPS", 1)
    checked = []
    real = claude_sessions._viewable
    monkeypatch.setattr(claude_sessions, "_viewable",
                        lambda p: checked.append(p) or real(p))
    apps = claude_sessions.list_apps(str(tmp_path / "unused"))
    assert len(apps) == 1
    # islice takes the cap, the lookahead next() takes one more — the other
    # three candidates are never even name-checked, let alone stat'd.
    assert len(checked) == 2, checked


def test_parses_once_and_reparses_when_the_transcript_grows(store, tmp_path, monkeypatch):
    page = _saved(tmp_path, "a.html")
    path = _transcript(store, "s1", [_user_line(tmp_path, "x"), _write_line(page)])
    parses = []
    real = claude_sessions._parse
    monkeypatch.setattr(claude_sessions, "_parse",
                        lambda p: parses.append(p) or real(p))
    claude_sessions.list_sessions()
    claude_sessions.list_apps(str(tmp_path / "unused"))
    claude_sessions.list_sessions()
    assert len(parses) == 1, "steady state is a stat per transcript, not a parse"
    other = _saved(tmp_path, "b.html")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_write_line(other) + "\n")
    assert len(claude_sessions.list_apps(str(tmp_path / "unused"))) == 2
    assert len(parses) == 2, "growth moves (mtime, size) and invalidates"


# ------------------------------------------------------------- related parts

def test_sessions_index_carries_the_first_HUMAN_prompt(store, tmp_path):
    _transcript(store, "s1", [
        _user_line(tmp_path, "agent prompt", sidechain=True),
        _user_line(tmp_path, "  the real   question  "),
    ])
    sessions = claude_sessions.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session"] == "s1"
    assert sessions[0]["prompt"] == "the real question"
    assert sessions[0]["cwd"] == str(tmp_path)


def test_session_files_lists_everything_with_a_viewable_hint(store, tmp_path):
    page = _saved(tmp_path, "out.html")
    code = _saved(tmp_path, "tool.py", "x = 1")
    _transcript(store, "s1", [
        _user_line(tmp_path / "work", "build"),
        _write_line(page), _write_line(code), _write_line(code, tool="Edit"),
    ])
    data = claude_sessions.session_files("s1")
    assert data["session"] == "s1"
    files = {f["path"]: f for f in data["files"]}
    assert set(files) == {str(page), str(code)}
    assert files[str(page)]["viewable"] is True
    assert files[str(code)]["viewable"] is False, "listed, hinted, not hidden"
    assert files[str(code)]["writes"] == 2
    assert all(f["exists"] for f in data["files"])


def test_a_crafted_session_id_matches_nothing(store, tmp_path):
    _transcript(store, "s1", [_user_line(tmp_path, "x")])
    assert claude_sessions.session_files("nope") is None
    # Matching, not joining: an id shaped like a traversal can only ever fail
    # to match a filename stem — there is no path for it to escape into.
    assert claude_sessions.session_files("../../../etc/passwd") is None
    assert claude_sessions.session_files("s1/../s1") is None


def test_file_to_sessions_and_back(store, tmp_path):
    page = _saved(tmp_path, "page.html")
    _transcript(store, "s1", [_user_line(tmp_path, "make it"), _write_line(page)])
    _transcript(store, "s2", [_user_line(tmp_path, "other work")], slug="-b")
    hits = claude_sessions.sessions_for_file(str(page))
    assert [h["session"] for h in hits] == ["s1"]
    assert hits[0]["writes"] == 1
    assert str(page) in {f["path"]
                         for f in claude_sessions.session_files("s1")["files"]}


def test_related_merges_sessions_with_checkpointed_versions(store, tmp_path):
    """The git-history-like payload: transcripts say WHO touched the file,
    the file-history store says WHAT it held. The store is fabricated with the
    real key rule (`file_history.path_hash`) so this cannot drift from the
    module that owns the semantics."""
    page = _saved(tmp_path, "page.html", "current content")
    _transcript(store, "s1", [_user_line(tmp_path, "edit"), _write_line(page)])
    fh = claude_sessions._file_history()
    version_dir = store / "file-history" / "s1"
    version_dir.mkdir(parents=True)
    (version_dir / f"{fh.path_hash(str(page))}@v1").write_text(
        "older content", encoding="utf-8")
    rel = claude_sessions.related(str(page))
    assert rel["exists"] is True
    assert [s["session"] for s in rel["sessions"]] == ["s1"]
    assert len(rel["versions"]) == 1
    v = rel["versions"][0]
    assert (v["session"], v["version"], v["differs"]) == ("s1", 1, True)


def test_related_degrades_to_empty_without_any_store(store, tmp_path):
    rel = claude_sessions.related(str(tmp_path / "never-touched.html"))
    assert rel == {"file": str(tmp_path / "never-touched.html"),
                   "exists": False, "sessions": [], "versions": []}


# ------------------------------------------------------------------- uploads

def test_uploads_list_with_the_hex_prefix_stripped(store, tmp_path):
    up = store / "uploads" / "sess-1"
    up.mkdir(parents=True)
    (up / "d87044df-tree.txt").write_text("pasted listing", encoding="utf-8")
    (up / "plain.csv").write_text("a,b", encoding="utf-8")
    (up / "0abc12-binary.exe").write_text("x", encoding="utf-8")
    (up / ".hidden.png").write_text("x", encoding="utf-8")
    apps = claude_uploads.list_apps()
    assert {(a["name"], a["tag"], a["source"]) for a in apps} == {
        ("tree.txt", "uploads", "claude-upload"),
        ("plain.csv", "uploads", "claude-upload"),
    }


def test_uploads_absent_store_is_the_normal_empty(store):
    assert claude_uploads.list_apps() == []


def test_uploads_cap_keeps_the_NEWEST_not_the_alphabetically_first(
        store, tmp_path, monkeypatch):
    """Raised in review: an islice over the name-ordered walk kept whichever
    attachments sorted first and dropped the ones pasted yesterday. The cap's
    job is recency, so it trims after the walk, newest kept — and says so."""
    up = store / "uploads" / "s1"
    up.mkdir(parents=True)
    for i, name in enumerate(["aaa-old.png", "mmm-mid.png", "zzz-new.png"]):
        f = up / name
        f.write_text("x", encoding="utf-8")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))  # zzz is the newest
    monkeypatch.setattr(claude_uploads, "MAX_UPLOADS", 2)
    kept = {a["name"] for a in claude_uploads.list_apps()}
    assert kept == {"zzz-new.png", "mmm-mid.png"}


# ------------------------------------------------------------------ the API

def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    return TestClient(create_app(start_dir=str(tmp_path)))


def test_endpoints_round_trip(store, tmp_path, monkeypatch):
    page = _saved(tmp_path, "page.html")
    _transcript(store, "s1", [_user_line(tmp_path, "hello"), _write_line(page)])
    client = _client(tmp_path, monkeypatch)

    sessions = client.get("/api/claude/sessions").json()["sessions"]
    assert [s["session"] for s in sessions] == ["s1"]

    files = client.get("/api/claude/sessions/s1/files").json()
    assert [f["path"] for f in files["files"]] == [str(page)]

    assert client.get("/api/claude/sessions/absent/files").status_code == 404

    rel = client.get("/api/claude/related", params={"path": str(page)}).json()
    assert rel["file"] == str(page)
    assert [s["session"] for s in rel["sessions"]] == ["s1"]

    assert client.get("/api/claude/related",
                      params={"path": "relative/x.html"}).status_code == 400
    assert client.get("/api/claude/related", params={"path": ""}).status_code == 400


def test_listing_gates_and_merges_the_new_sources(store, tmp_path, monkeypatch):
    """GET /api/apps carries claude-session and claude-upload apps, and each
    pref switches ONLY its own source off."""
    page = _saved(tmp_path, "made.html")
    _transcript(store, "s1", [_user_line(tmp_path / "work", "x"), _write_line(page)])
    up = store / "uploads" / "s1"
    up.mkdir(parents=True)
    (up / "ab12cd-shot.png").write_text("png", encoding="utf-8")
    client = _client(tmp_path, monkeypatch)

    def sources():
        return {a["source"] for a in client.get("/api/apps").json()["apps"]}

    assert {"claude-session", "claude-upload"} <= sources()
    client.put("/api/prefs", json={"discover_claude_sessions": False},
               headers={"X-Fused": "1"})
    assert "claude-session" not in sources()
    assert "claude-upload" in sources(), "the toggles are independent"
    client.put("/api/prefs", json={"discover_claude_uploads": False},
               headers={"X-Fused": "1"})
    assert "claude-upload" not in sources()


def test_prefs_report_the_new_sources_availability(store, tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    discovery = client.get("/api/prefs").json()["discovery"]
    assert discovery["claude_sessions"] == {"enabled": True, "available": True}
    # `store` has no uploads dir yet, so the toggle would change nothing —
    # exactly what `available` exists to tell the page.
    assert discovery["claude_uploads"] == {"enabled": True, "available": False}
