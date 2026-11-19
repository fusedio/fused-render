"""Specialists core: home layout, create/list/find/update/delete (scenario C), the Canvas seed (B), chat clear/fold
(E) and the queue/tasks seams."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os

import pytest

from fused_render.specialists import chat, home, paths, seed, store


def test_layout_is_a_claude_code_project(app_home):
    s = make_specialist("Canvas Pro", "Fused canvases", triggers={"folders": ["/work/x"], "keywords": ["canvas"]})
    h = s.home
    assert os.path.basename(h) == "canvas-pro" and os.path.dirname(h) == paths.root()
    md = open(os.path.join(h, "CLAUDE.md")).read()
    assert md.startswith("# The Canvas Pro specialist")
    assert "You are the Canvas Pro specialist. Your subject: Fused canvases." in md
    assert "expert" not in md.lower()
    assert "except the change list the nightly update is told to write" in md
    assert "find its owner in people.md, draft the exact message" in md
    assert md.endswith("<!-- fused:imports -->\n@memory.md\n@said.md\n")
    assert json.load(open(os.path.join(h, ".claude", "settings.json"))) == {"autoMemoryEnabled": False}
    assert open(os.path.join(h, ".gitignore")).read() == ".specialist/\ninbox/\nsaid.md\n"
    for d in ("knowledge", ".claude/skills", "inbox", ".specialist"):
        assert os.path.isdir(os.path.join(h, d)), d
    assert json.load(open(os.path.join(h, "triggers.json"))) == {"folders": ["/work/x"], "keywords": ["canvas"]}
    man = json.load(open(os.path.join(h, "specialist.json")))
    assert man["id"] == s.id and man["name"] == "Canvas Pro" and "triggers" not in man
    assert "- (none)" in open(os.path.join(h, "said.md")).read()


def test_create_list_find_update_delete(app_home):
    assert store.all() == [] and not store.any_exist()
    a = make_specialist("Canvas")
    b = make_specialist("Canvas!")  # different name, same slug -> suffix
    assert os.path.basename(b.home) == "canvas-2"
    assert [s.id for s in store.all()] == [a.id, b.id] and store.any_exist()
    assert store.find(a.id).id == a.id and store.find("  canvas ").id == a.id and store.find("nobody") is None
    with pytest.raises(store.Invalid):
        store.create("CANVAS", "x")
    with pytest.raises(store.Invalid):
        store.create("General", "x")
    s = store.update(a.id, subject="new", model="opus", effort="high", triggers={"keywords": ["k"]})
    assert (s.subject, s.model, s.effort, s.triggers) == ("new", "opus", "high", {"folders": [], "keywords": ["k"]})
    with pytest.raises(store.Invalid):
        store.update(a.id, effort="loud")
    with pytest.raises(store.Invalid):
        store.update(a.id, triggers={"folders": ["relative/path"]})
    # an open breakout record is marked deleted, the home moves to trash
    os.makedirs(os.path.join(a.home, ".specialist", "breakouts"))
    with open(os.path.join(a.home, ".specialist", "breakouts", "b-1.json"), "w") as f:
        json.dump({"id": "b-1", "status": "open"}, f)
    store.delete(a.id)
    assert store.get(a.id) is None and not os.path.exists(a.home)
    trash = os.path.join(paths.state_root(), "trash")
    (moved,) = os.listdir(trash)
    rec = json.load(open(os.path.join(trash, moved, ".specialist", "breakouts", "b-1.json")))
    assert rec["status"] == "deleted"
    assert [s.id for s in store.all()] == [b.id]


def test_paths_predicates(app_home, monkeypatch):
    s = make_specialist()
    assert paths.spec_of_path(s.home) == s.id
    assert paths.spec_of_path(os.path.join(s.home, "knowledge", "x.md")) == s.id
    assert paths.spec_of_path(paths.state_root()) is None
    assert paths.spec_of_path("/elsewhere") is None
    assert paths.flag_on_and_inside(s.home)
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert not paths.flag_on_and_inside(s.home)


def test_seed_once_copies_canvas_never_moves(app_home):
    src = os.path.join(seed.EXPERTS_SHIPPED, "canvas-reference", "skills")
    before = sorted(os.listdir(src))
    assert seed.seed() == ["canvas"]
    assert seed.seed() == []
    assert sorted(os.listdir(src)) == before  # copied, not moved
    (s,) = store.all()
    assert s.name == "Canvas" and s.shipped == "canvas"
    names = [k["name"] for k in home.skills(s.home)]
    assert names == sorted(n[:-3] for n in before)
    sk = open(home.skill_path(s.home, "running-udfs")).read()
    assert sk.startswith("---\nname: running-udfs\ndescription: \"Running UDFs on Fused. Use when")
    # polish 4: the experts' files say "this expert"; a specialist never calls itself that
    for k in home.skills(s.home):
        assert "expert" not in open(home.skill_path(s.home, k["name"])).read().lower(), k["name"]
    assert "this specialist's sources" in open(home.skill_path(s.home, "edges-and-folders")).read()
    md = open(os.path.join(s.home, "CLAUDE.md")).read()
    assert "## Subject" in md and "remote engine" in md and "memory-archive" not in md and "expert_ask" not in md
    assert open(os.path.join(s.home, "memory.md")).read() == home.MEMORY_EMPTY
    # deleted stays deleted
    store.delete(s.id)
    assert seed.seed() == [] and store.all() == []


def test_chat_record_and_clear_folds(app_home):
    s = make_specialist()
    assert chat.state(s.id) == {"current": None, "folded": []}
    chat.record_start(s.id, "sess-1")
    chat.record_start(s.id, "sess-1")  # a resume changes nothing
    assert chat.state(s.id)["current"] == "sess-1"
    out = chat.clear(s.id)
    assert out["current"] is None and [f["session_id"] for f in out["folded"]] == ["sess-1"]
    assert out["folded"][0]["ended"]
    chat.record_start(s.id, "sess-2")
    chat.record_start(s.id, "sess-3")  # a new session while one is current folds the old one
    st = chat.state(s.id)
    assert st["current"] == "sess-3" and [f["session_id"] for f in st["folded"]] == ["sess-1", "sess-2"]
    assert chat.kind_of(s.id, "sess-2") == "main" and chat.kind_of(s.id, "nope") == ""
    assert chat.clear(s.id)["current"] is None and chat.clear(s.id)["current"] is None  # twice is fine


def test_queue_key_is_empty_inside_a_home_only_with_the_flag(app_home, monkeypatch, tmp_path):
    from fused_render import project_queue

    s = make_specialist()
    other = tmp_path / "proj"
    other.mkdir()
    project_queue.reset_cache()
    assert project_queue.queue_key(s.home) == ""
    assert project_queue.queue_key(os.path.join(s.home, "memory.md")) == ""
    assert project_queue.queue_key(str(other)) != ""
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    project_queue.reset_cache()
    assert project_queue.queue_key(s.home) != ""
    project_queue.reset_cache()


def test_tasks_collect_hides_home_sessions(app_home, monkeypatch):
    from fused_render import tasks_store
    from fused_render.server.routers import tasks

    s = make_specialist()
    monkeypatch.setattr(tasks_store, "head", lambda path: (s.home if path == "in" else "/work/proj",) + ("",) * 5)
    assert tasks._specialist_session({"path": "in"})
    assert not tasks._specialist_session({"path": "out"})
    assert not tasks._specialist_session({"path": ""})
    # R6: hidden with the flag off too (the root exists), in a breakout's workroom too
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert tasks._specialist_session({"path": "in"})
    work = os.path.join(s.home, ".specialist", "breakouts", "b-1", "work")
    monkeypatch.setattr(tasks_store, "head", lambda path: (work,) + ("",) * 5)
    assert tasks._specialist_session({"path": "w"})


def test_tasks_collect_hides_nothing_without_a_root(app_home, monkeypatch):
    """R6: a user who never turned specialists on has no root, so a folder that merely sits where it would be is
    still listed."""
    from fused_render import tasks_store
    from fused_render.server.routers import tasks
    from fused_render.specialists import paths

    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert not os.path.isdir(paths.root())
    inside = os.path.join(paths.root(), "x")
    monkeypatch.setattr(tasks_store, "head", lambda path: (inside,) + ("",) * 5)
    assert not tasks._specialist_session({"path": "in"})
    assert not os.path.isdir(paths.root())  # and nothing was created


def test_delete_refused_while_the_nightly_job_runs(app_home, monkeypatch):
    from fused_render.specialists import nightly

    s = make_specialist()
    monkeypatch.setattr(nightly, "running", lambda spec_id: True)
    with pytest.raises(store.Busy, match="nightly update is running"):
        store.delete(s.id)
    assert os.path.isdir(s.home)


def test_session_folder_lists_skip_homes(app_home, monkeypatch, tmp_path):
    """Live run: the main chat's home led the New task folder picker's recents (`/api/claude-sessions`), and a
    task started there is refused. Both folder lists skip a home with the flag on, and keep it with it off."""
    import json as _json

    from fused_render.server.routers import claude_sessions

    s = make_specialist()
    other = tmp_path / "proj"
    other.mkdir()
    root = tmp_path / "projects"
    for i, cwd in enumerate((s.home, str(other))):
        d = root / f"p{i}"
        d.mkdir(parents=True)
        (d / "x.jsonl").write_text(_json.dumps({"type": "user", "cwd": cwd, "sessionId": "x"}) + "\n")
    monkeypatch.setattr(claude_sessions, "projects_roots", lambda: [("claude", str(root))])

    def paths(r):
        return {f["path"] for f in r["folders"]}

    assert paths(claude_sessions.api_claude_sessions()) == {str(other)}
    assert paths(claude_sessions.api_home_claude_sessions()) == {str(other)}
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert paths(claude_sessions.api_claude_sessions()) == {s.home, str(other)}


def test_a_new_task_in_a_home_is_refused_before_any_row_exists(client, monkeypatch):
    """QA (privileges) defect 5: the refusal happens at task creation (no entry, so no broken Tasks row), with the
    sentence the start used to answer. Flag off: the doors are main's."""
    import json as _json

    from fused_render import schedule

    s = make_specialist()
    before = list(schedule.list_entries()) if hasattr(schedule, "list_entries") else None
    sentence = "This folder is a specialist's home; open it from the Specialists page."
    for path, body in (("/api/schedule", {"target": s.home, "message": "hi", "delay_seconds": 60}),
                       ("/api/tasks/create", {"target": os.path.join(s.home, "knowledge"), "prompt": "hi"})):
        status, _, raw = client.post(path, body)
        assert (status, _json.loads(raw)) == (400, {"error": sentence, "reason": "specialist_home"}), path
    if before is not None:
        assert list(schedule.list_entries()) == before
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    status, _, raw = client.post("/api/tasks/create", {"target": s.home, "prompt": "hi", "due": "nope"})
    assert "specialist_home" not in raw.decode()
