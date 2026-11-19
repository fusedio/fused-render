"""A specialist's home: its layout, names and slugs, settings, delete to trash, and the shipped Canvas seed."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os
import subprocess

import pytest

from fused_render.specialists import paths, seed, store


@needs_git
def test_create_lays_out_an_ordinary_claude_code_project():
    s = make_specialist("Canvas", "Fused canvases")
    h = s.home
    assert h == os.path.join(paths.root(), "canvas") and paths.home(s.id) == h
    charter = open(os.path.join(h, "CLAUDE.md")).read()
    assert "You are the Canvas specialist, an expert on Fused canvases." in charter and "@memory.md" in charter
    assert open(os.path.join(h, "memory.md")).read() == ""
    assert open(os.path.join(h, ".gitignore")).read() == ".specialist/\n"  # inbox/ is tracked
    assert json.load(open(os.path.join(h, ".claude", "settings.json"))) == {"autoMemoryEnabled": False}
    assert os.path.isdir(os.path.join(h, "inbox")) and os.path.isdir(os.path.join(h, ".claude", "skills"))
    assert json.load(open(os.path.join(h, "specialist.json"))) == {
        "id": s.id, "name": "Canvas", "about": "Fused canvases", "folder": "", "face": None}
    log = subprocess.run(["git", "-C", h, "log", "--format=%s"], capture_output=True, text=True).stdout
    assert log.split() == ["Created"]


def test_names_slugs_and_fields(tmp_path):
    a = make_specialist("My Canvas!")
    assert os.path.basename(a.home) == "my-canvas"
    b = make_specialist("My-Canvas", "x")
    assert os.path.basename(b.home) == "my-canvas-2"
    with pytest.raises(store.Taken):
        make_specialist("my canvas!")
    with pytest.raises(store.Invalid):
        make_specialist("  ")
    with pytest.raises(store.Invalid):
        make_specialist("Other", "")
    with pytest.raises(store.Invalid):
        make_specialist("Other", "x", str(tmp_path / "missing"))
    assert store.find("MY CANVAS!").id == a.id and store.find(b.id).id == b.id
    assert [s.name for s in store.all()] == ["My Canvas!", "My-Canvas"]


def test_for_folder(tmp_path):
    (tmp_path / "proj" / "sub").mkdir(parents=True)
    make_specialist("Canvas", "x", str(tmp_path / "proj"))
    make_specialist("Other", "y")
    assert store.for_folder(str(tmp_path / "proj" / "sub")) == ["Canvas"]
    assert store.for_folder(str(tmp_path / "proj")) == ["Canvas"]
    assert store.for_folder(str(tmp_path / "projx")) == []


def test_update_rewrites_the_charter_and_keeps_its_subject():
    s = store.create("Canvas", "canvases", subject="Some subject text.")
    store.update(s.id, name="Canvasser", about="all canvases", face={"shape": "circle"})
    charter = open(os.path.join(s.home, "CLAUDE.md")).read()
    assert "You are the Canvasser specialist, an expert on all canvases." in charter
    assert charter.endswith("## Subject\nSome subject text.\n")
    assert store.get(s.id).face == {"shape": "circle"}
    with pytest.raises(store.Invalid):
        store.update(s.id, folder="/no/such/folder")
    with pytest.raises(KeyError):
        store.update("nope", name="x")


def test_delete_ends_its_sessions_and_moves_the_home_to_trash(monkeypatch):
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    s = make_specialist()
    ended = []
    monkeypatch.setattr(agent, "_hosts_in", lambda folder: ["r1", "r2"] if folder == s.home else [])
    monkeypatch.setattr(agent, "_cancel_run", lambda run_id, interrupt_first=True: ended.append(run_id))
    store.delete(s.id)
    assert ended == ["r1", "r2"] and not os.path.exists(s.home) and store.get(s.id) is None
    trash = os.listdir(os.path.join(paths.state_root(), "trash"))
    assert len(trash) == 1 and trash[0].startswith(s.id + "-")


def test_end_idle_hosts_leaves_a_live_turn_and_marks_no_stop(monkeypatch, tmp_path):
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    ended = []

    def cancel(run_id, interrupt_first=True):
        ended.append(run_id)
        (tmp_path / run_id).mkdir()
        (tmp_path / run_id / "cancelled").write_text("1")

    monkeypatch.setattr(agent, "RUNS", str(tmp_path))
    monkeypatch.setattr(agent, "_hosts_in", lambda folder: ["live", "idle"])
    monkeypatch.setattr(agent, "_live_runs_in", lambda folder: iter([{"run_id": "live", "sessions": set()}]))
    monkeypatch.setattr(agent, "_cancel_run", cancel)
    store.end_idle_hosts("/any")
    assert ended == ["idle"]
    assert not (tmp_path / "idle" / "cancelled").exists()  # the chat would show "Stopped." under its last reply


def test_seed_installs_canvas_once_when_the_root_is_first_made(monkeypatch):
    seed.seed()
    (canvas,) = store.all()
    assert canvas.name == "Canvas" and canvas.face["shape"] == "hexagon"
    skills = os.listdir(os.path.join(canvas.home, ".claude", "skills"))
    assert "running-udfs" in skills and len(skills) == 6
    text = open(os.path.join(canvas.home, ".claude", "skills", "running-udfs", "SKILL.md")).read()
    assert text.startswith('---\nname: running-udfs\ndescription: "Running UDFs on Fused. Use when the work involves:')
    assert "## Subject\nYou know how Fused canvases" in open(os.path.join(canvas.home, "CLAUDE.md")).read()
    from fused_render.claude_agent import agent_module

    monkeypatch.setattr(agent_module(), "_hosts_in", lambda folder: [])
    store.delete(canvas.id)
    seed.seed()
    assert store.all() == []
