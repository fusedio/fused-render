"""Learning: due from the inbox alone; a conversation's inbox entry is a pointer written once; a run is a fresh
session given exactly its entries; a clean end moves the start lines on; a failed one changes nothing."""
from _experts_conftest import *  # noqa: F401,F403
import json
import os
from types import SimpleNamespace

import pytest

from fused_render import claude_spawn
from fused_render.experts import chat, history, launch, notes, store, update

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
        return {"run_id": "run1", "session_id": "learn1"}

    monkeypatch.setattr(chat, "transcript", transcript)
    monkeypatch.setattr(store, "live_runs", lambda home: [])
    monkeypatch.setattr(claude_spawn, "spawn_helper", spawn)
    monkeypatch.setattr(claude_spawn, "record_session_when_ready", lambda agent, run_id: None)
    monkeypatch.setattr(update, "_watch", lambda home, run_id: None)  # tested on its own below
    monkeypatch.setattr(update, "_RUNNING", set())
    # What the chat's pills open on for a session: the run uses it.
    agent = SimpleNamespace(_defaults=lambda file, sid: {"model": "haiku" if sid else "", "effort": "low" if sid else ""})
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: agent)
    return files, spawns


def test_due_only_with_something_in_the_inbox(world):
    _, spawns = world
    s = make_expert()
    assert not update.due(s, 10 ** 10)
    assert update.start(s) is False and spawns == []
    notes.add(s, "t", 'Note from task "x"', "x")
    assert update.due(s, 10 ** 10)
    update.start(s)
    assert not update.due(s, 10 ** 10)  # running


def test_conversations_get_one_pointer_each(world, tmp_path):
    files, _ = world
    s = make_expert()
    chat.record_start(s.id, "main", "main1")
    files["main1"] = [_row("user", "<terminal-hint>{}</terminal-hint>\nRemember parquet"), _row("assistant", "Done.")]
    files["brk1"] = [_row("user", 'From 2 messages of their main chat titled "Fix the canvas". Why parquet?'),
                     _row("assistant", "Columnar.")]
    assert history.turn_ended(s.home, "main1") is True and history.turn_ended(s.home, "brk1") is True
    history.turn_ended(s.home, "brk1")  # one pointer per conversation
    by_kind = {n["kind"]: n for n in notes.list(s)}
    assert set(by_kind) == {"chat", "breakout"}
    assert by_kind["chat"]["title"] == "Remember parquet"  # the app's own blocks are not the user's words
    assert by_kind["breakout"]["title"] == "Fix the canvas"
    assert by_kind["breakout"]["text"].endswith(f"Transcript: {tmp_path / 'brk1.jsonl'}\nFrom line: 1\n")
    assert "Columnar" not in by_kind["breakout"]["text"]  # a pointer, not a copy

    files["task1"] = [_row("user", "Fix the canvas")]
    assert history.turn_ended(str(tmp_path), "task1") is False  # not one that used the expert: nothing
    update.point(s, "task1", str(tmp_path), "task")  # its first tool call
    assert history.turn_ended(str(tmp_path), "task1") is False  # the agent commits a task's turn itself
    assert sorted(n["kind"] for n in notes.list(s)) == ["breakout", "chat", "task"]


def test_a_run_is_a_fresh_session_given_exactly_its_entries(world, tmp_path):
    files, spawns = world
    s = make_expert()
    chat.record_start(s.id, "main", "main1")
    files["task1"] = [_row("user", "Fix it"), _row("assistant", "Fixed.")]
    update.point(s, "task1", str(tmp_path), "task")
    assert update.start(s) is True
    ((target, prompt, mode, kw),) = spawns
    (entry,) = notes.list(s)
    assert (target, mode) == (s.home, "auto") and f"- inbox/{entry['name']}" in prompt
    assert kw == {"model": "haiku", "effort": "low", "expert": f"{s.id}:update"}
    assert chat.load(s.home)["update"] == {"session": "learn1", "files": [entry["name"]], "lines": {"task1": 2}}
    assert launch.spec(f"{s.id}:main", s.home)["reason"] == "learning"  # the chat waits for it
    with pytest.raises(update.Busy):
        update.start(s)

    files["learn1"] = [_row("user", update.PROMPT), _row("assistant", "I added that parquet wins.")]
    history.turn_ended(s.home, "learn1")  # its clean end
    st = chat.load(s.home)
    assert notes.list(s) == [] and st["update"] is None
    assert st["sources"]["task1"] == {"path": str(tmp_path / "task1.jsonl"), "line": 2, "file": ""}
    assert "learn1" not in st["sources"] and st["main"] == "main1"  # never a conversation of its own

    files["task1"].append(_row("user", "One more thing"))
    history.turn_ended(str(tmp_path), "task1")
    (again,) = notes.list(s)
    assert again["text"].endswith("From line: 3\n")


def test_busy_is_refused(world, monkeypatch, client):
    s = make_expert()
    notes.add(s, "t", 'Note from task "x"', "x")
    monkeypatch.setattr(store, "live_runs", lambda home: [{"run_id": "r"}])
    with pytest.raises(update.Busy):
        update.start(s)
    status, _, _ = client.post(f"/api/experts/{s.id}/update", {})
    assert status == 409
    monkeypatch.setattr(store, "live_runs", lambda home: [])
    status, _, body = client.post(f"/api/experts/{s.id}/update", {})
    assert status == 202 and json.loads(body) == {"started": True}


def test_a_failed_run_changes_nothing(world):
    s = make_expert()
    note = notes.add(s, "t", 'Note from task "x"', "x")
    chat.change(s.home, lambda d: d.update(update={"session": "learn1", "files": [note], "lines": {}}))
    with open(os.path.join(s.home, "memory.md"), "w") as f:
        f.write("Half done.\n")
    update._RUNNING.add(s.home)
    WATCH(s.home, "run1")  # the turn ended without its clean commit
    assert open(os.path.join(s.home, "memory.md")).read() == "" and [n["name"] for n in notes.list(s)] == [note]
    st = chat.load(s.home)
    assert st["update"] is None and st["last_update"] > 0 and not update.learning(s.home)


def test_a_turn_the_watch_gave_up_on_is_stopped_before_the_restore(world, monkeypatch):
    s = make_expert()
    chat.change(s.home, lambda d: d.update(update={"session": "learn1", "files": ["n.md"], "lines": {}}))
    events = []
    agent = claude_spawn.load_agent()
    monkeypatch.setattr(agent, "_cancel_run", lambda run_id, interrupt_first=True: events.append(("stop", run_id)),
                        raising=False)
    monkeypatch.setattr(store, "live_runs", lambda home: [{"run_id": "run1"}])  # still going after the hour
    monkeypatch.setattr(history, "restore", lambda home: events.append(("restore", home)))
    WATCH(s.home, "run1")
    assert events == [("stop", "run1"), ("restore", s.home)]


def test_a_run_the_server_lost_is_settled_as_failed(world, monkeypatch):
    s = make_expert()
    note = notes.add(s, "t", 'Note from task "x"', "x")
    chat.change(s.home, lambda d: d.update(update={"session": "learn1", "files": [note], "lines": {}}))
    with open(os.path.join(s.home, "memory.md"), "w") as f:
        f.write("Half done.\n")
    monkeypatch.setattr(store, "live_runs", lambda home: [{"run_id": "run1"}])
    update.settle(s)  # still going: left alone
    assert chat.load(s.home)["update"] is not None
    monkeypatch.setattr(store, "live_runs", lambda home: [])
    update.settle(s)  # gone after a restart: as a failed run
    assert open(os.path.join(s.home, "memory.md")).read() == "" and [n["name"] for n in notes.list(s)] == [note]
    assert chat.load(s.home)["update"] is None


def test_a_run_goes_to_the_agent_its_chat_is_on(world, monkeypatch, tmp_path):
    """A Copilot expert learns on Copilot: its own prompt, its chat's model, and a session id that arrives later
    (`launch.minted`), which is the one its clean end is matched to."""
    files, spawns = world
    agent = SimpleNamespace(
        EXPERT_BACKENDS=("claude", "copilot"), _backend_enabled=lambda b: True, _default_backend_pref=lambda: "claude",
        _defaults=lambda file, sid: {"model": "gpt-5.4", "effort": "high", "backend": "copilot" if sid else "claude"})
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: agent)
    monkeypatch.setattr(claude_spawn, "spawn_helper",
                        lambda target, prompt, mode, **kw: spawns.append((prompt, kw)) or {"run_id": "r", "session_id": ""})
    s = make_expert()
    chat.record_start(s.id, "main", "main1")
    files["task1"] = [_row("user", "Fix it"), _row("assistant", "Fixed.")]
    update.point(s, "task1", str(tmp_path), "task")
    assert launch.spec(f"{s.id}:update", s.home)["read_dirs"] == [str(tmp_path)]   # where the transcript is
    assert update.start(s) is True
    ((prompt, kw),) = spawns
    assert kw == {"model": "gpt-5.4", "effort": "high", "expert": f"{s.id}:update", "backend": "copilot"}
    assert "Agent with model haiku" not in prompt and "one after another" in prompt
    assert chat.load(s.home)["update"]["session"] == ""
    launch.minted(f"{s.id}:update", "learn-c")
    files["learn-c"] = [_row("user", prompt), _row("assistant", "I added that parquet wins.")]
    history.turn_ended(s.home, "learn-c")
    assert notes.list(s) == [] and chat.load(s.home)["update"] is None


def test_a_run_falls_to_an_agent_that_is_on_with_its_own_model(world, monkeypatch):
    """The chat was on Copilot and Copilot is switched off since: the run is Claude's, on Claude's default."""
    _, spawns = world
    agent = SimpleNamespace(
        EXPERT_BACKENDS=("claude", "copilot"), _backend_enabled=lambda b: b == "claude",
        _default_backend_pref=lambda: "claude",
        _defaults=lambda file, sid: {"model": "gpt-5.4", "effort": "high", "backend": "copilot"})
    monkeypatch.setattr(claude_spawn, "load_agent", lambda: agent)
    s = make_expert()
    chat.record_start(s.id, "main", "main1")
    notes.add(s, "t", 'Note from task "x"', "x")
    assert update.start(s) is True
    assert spawns[0][3] == {"model": update.DEFAULT_MODEL, "effort": "", "expert": f"{s.id}:update"}
