"""The update: due from the inbox alone; a breakout's or a task's turn end writes its inbox file once; the start."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os
from types import SimpleNamespace

import pytest

from fused_render import claude_spawn
from fused_render.specialists import chat, history, notes, store, update

WATCH = update._watch  # the world fixture stubs it


def _row(kind, text):
    return json.dumps({"type": kind, "timestamp": "2026-11-19T10:00:00Z",
                       "message": {"content": [{"type": "text", "text": text}]}})


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Transcripts by session id (`files[sid] = [rows]`), no live run, and a recorded spawn."""
    files, spawns = {}, []

    def transcript(target, sid):
        path = tmp_path / f"{sid}.jsonl"
        path.write_text("".join(r + "\n" for r in files.get(sid, [])))
        return str(path)

    def spawn(target, prompt, mode, **kw):
        spawns.append((target, prompt, mode, kw))
        return {"run_id": "run1", "session_id": "new-main"}

    monkeypatch.setattr(chat, "transcript", transcript)
    monkeypatch.setattr(store, "live_runs", lambda home: [])
    monkeypatch.setattr(store, "end_idle_hosts", lambda home: None)
    monkeypatch.setattr(claude_spawn, "spawn_helper", spawn)
    monkeypatch.setattr(claude_spawn, "record_session_when_ready", lambda agent, run_id: None)
    monkeypatch.setattr(update, "_watch", lambda home, run_id: None)  # tested on its own below
    monkeypatch.setattr(update, "_RUNNING", set())
    # The main chat's recorded pick: the update runs on it.
    agent = SimpleNamespace(_session_settings=lambda sid: ("haiku", "low") if sid else ("", ""))
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: agent)
    return files, spawns


def test_due_only_with_something_in_the_inbox(world):
    _, spawns = world
    s = make_specialist()
    assert not update.due(s, 10 ** 10)
    assert update.start(s) is False and spawns == []
    notes.add(s, "t", "A note", "x")
    assert update.due(s, 10 ** 10)
    update.start(s)
    assert not update.due(s, chat.load(s.home)["last_update"] + 60)


def test_turn_ends_write_breakouts_and_tasks_to_the_inbox_once(world, tmp_path):
    files, _ = world
    s = make_specialist()
    chat.record_start(s.id, "breakout", "brk1")
    files["brk1"] = [_row("user", 'From 2 messages of their main chat titled "Fix the \\"big\\" canvas". Why parquet?'),
                     _row("assistant", "Columnar.")]
    assert history.turn_ended(s.home, "brk1") is True  # a turn in the home: committed there
    (brk,) = notes.list(s)
    assert brk["text"].startswith('# Breakout from "Fix the "big" canvas"') and "**Claude:** Columnar." in brk["text"]
    history.turn_ended(s.home, "brk1")
    assert notes.list(s)[0]["text"] == brk["text"]  # nothing written twice
    files["brk1"].append(_row("user", "And CSV?"))
    history.turn_ended(s.home, "brk1")
    assert notes.list(s)[0]["text"].endswith("**User:** And CSV?\n\n")

    files["task1"] = [_row("user", "Fix the canvas"), _row("assistant", "Done: used parquet.")]
    assert history.turn_ended(str(tmp_path), "task1") is False  # no specialist used: nothing
    assert len(notes.list(s)) == 1
    chat.record_task(s.id, "task1", str(tmp_path))
    assert history.turn_ended(str(tmp_path), "task1") is False  # the agent commits a task's turn itself
    task = next(n for n in notes.list(s) if n["name"] != brk["name"])
    assert task["text"] == '# Task "Fix the canvas"\n\n\n**User:** Fix the canvas\n\n**Claude:** Done: used parquet.\n\n'
    notes.delete(s, task["name"])  # an update read it
    files["task1"].append(_row("user", "One more thing"))
    history.turn_ended(str(tmp_path), "task1")
    new = next(n for n in notes.list(s) if n["name"] != brk["name"])
    assert "One more thing" in new["text"] and "Done: used parquet" not in new["text"]


def test_start_resumes_the_main_chat_unattended(world):
    _, spawns = world
    s = make_specialist()
    notes.add(s, "task1", "A note", "Use parquet.")
    assert update.start(s) is True
    ((target, prompt, mode, kw),) = spawns
    assert (target, mode) == (s.home, "acceptEdits")
    assert prompt == update.PROMPT.format(files=f"- inbox/{notes.list(s)[0]['name']}")
    assert kw == {"session_id": "", "model": "", "effort": "", "isolate_mcp": True, "specialist": f"{s.id}:update"}
    st = chat.load(s.home)
    assert st["main"] == "new-main" and len(st["update_files"]) == 1  # a fresh update becomes the main chat
    chat.record_start(s.id, "main", "new-main")
    notes.add(s, "task1", "Another", "x")
    with pytest.raises(update.Busy):  # the first one has not ended
        update.start(s)
    update._RUNNING.clear()
    update.start(s)
    assert (spawns[-1][3]["session_id"], spawns[-1][3]["model"], spawns[-1][3]["effort"]) == ("new-main", "haiku", "low")


def test_busy_is_refused(world, monkeypatch, client):
    s = make_specialist()
    notes.add(s, "t", "A note", "x")
    monkeypatch.setattr(store, "live_runs", lambda home: [{"run_id": "r"}])
    with pytest.raises(update.Busy):
        update.start(s)
    status, _, _ = client.post(f"/api/specialists/{s.id}/update", {})
    assert status == 409
    monkeypatch.setattr(store, "live_runs", lambda home: [])
    status, _, body = client.post(f"/api/specialists/{s.id}/update", {})
    assert status == 202 and json.loads(body) == {"started": True}


def test_a_failed_update_changes_nothing(world):
    s = make_specialist()
    note = notes.add(s, "t", "A note", "x")
    chat.change(s.home, lambda d: d.update(update_files=[note]))
    with open(os.path.join(s.home, "memory.md"), "w") as f:
        f.write("Half done.\n")
    update._RUNNING.add(s.home)
    WATCH(s.home, "run1")  # the turn ended without its clean commit
    assert open(os.path.join(s.home, "memory.md")).read() == "" and [n["name"] for n in notes.list(s)] == [note]
    assert chat.load(s.home)["update_files"] == [] and s.home not in update._RUNNING
