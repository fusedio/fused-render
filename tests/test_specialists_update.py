"""The update: nothing waiting means no run; unread parts of tasks and breakouts become inbox notes; the start."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os
from types import SimpleNamespace

import pytest

from fused_render import claude_spawn
from fused_render.specialists import chat, notes, store, update


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
    # The main chat's recorded pick: the update runs on it.
    agent = SimpleNamespace(_session_settings=lambda sid: ("haiku", "low") if sid else ("", ""))
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: agent)
    return files, spawns


def test_the_prompt_is_wrapped_in_the_tag_the_chat_shows_as_update():
    assert update.PROMPT.startswith("<specialist-update>") and update.PROMPT.endswith("</specialist-update>")


def test_nothing_waiting_means_no_run(world):
    _, spawns = world
    s = make_specialist()
    assert update.due(s, 10 ** 10)
    assert update.start(s) is False and spawns == []
    assert not update.due(s, chat.load(s.home)["last_update"] + 60)


def test_unread_parts_become_notes_and_the_cursors_move(world, tmp_path):
    files, _ = world
    s = make_specialist()
    chat.record_task(s.id, "task1", str(tmp_path))
    chat.record_start(s.id, "breakout", "brk1")
    files["task1"] = [_row("user", "Fix the canvas"), _row("assistant", "Done: used parquet.")]
    files["brk1"] = [_row("user", "Why parquet?")]
    names = update.prepare(s)
    texts = sorted(n["text"] for n in notes.list(s))
    assert len(names) == 2 and texts[0].startswith("# From a breakout\n\n**User:** Why parquet?")
    assert texts[1] == f"# From a task in {tmp_path}\n\n**User:** Fix the canvas\n\n**Claude:** Done: used parquet.\n"
    for n in names:
        notes.delete(s, n)
    assert update.prepare(s) == []  # read already
    files["task1"].append(_row("user", "One more thing"))
    (name,) = update.prepare(s)
    assert "One more thing" in open(os.path.join(s.home, "inbox", name)).read()
    assert "Fix the canvas" not in open(os.path.join(s.home, "inbox", name)).read()


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
