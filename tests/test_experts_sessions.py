"""Experts with the harness stubbed (docs/experts.md): session rows and diffs, the write lock, compaction, the
fence flags, chat rows, and the task-quiet due rule with its retry cap."""
from _bots_conftest import *  # noqa: F401,F403 — app_home (a per-test home and workspace)
import os
import threading
import time

import pytest

from fused_render.bots import agent_engine, registry
from fused_render.experts import quiet, sessions, store


@pytest.fixture
def expert():
    return registry.create(name="Test Expert", kind="expert", expert={"ask_me_about": "testing things"})


def stub(monkeypatch, fn=None, answer="ok"):
    """sessions.harness -> `fn(bot, prompt, write)` (which may write memory.md) and a fixed successful result."""
    calls = []

    def harness(bot, prompt, write):
        calls.append({"prompt": prompt, "write": write})
        res = fn(bot, prompt, write) if fn else None
        return res or {"ok": True, "answer": answer, "error": "", "cost_usd": 0.01,
                       "transcript": [{"role": "prompt", "text": prompt}, {"role": "expert", "text": answer}]}
    monkeypatch.setattr(sessions, "harness", harness)
    return calls


def append_memory(bot, line):
    with open(store.memory_path(bot), "a", encoding="utf-8") as f:
        f.write(line + "\n")


# --------------------------------------------------------------------- rows ---
def test_layout_and_row_with_diff(expert, monkeypatch):
    assert store.memory(expert) == store.EMPTY_MEMORY
    assert os.path.isfile(os.path.join(store.expert_dir(expert), "instructions.md"))
    stub(monkeypatch, lambda b, p, w: append_memory(b, "- [1 · 2026-11-17] A fact."))
    row = sessions.run(expert, "task-quiet", "read this", {"session_id": "s1"})
    assert row["status"] == "done" and row["kind"] == "task-quiet" and row["ended"] >= row["started"]
    assert "+- [1 · 2026-11-17] A fact." in row["memory_diff"]
    assert row["trigger"] == {"session_id": "s1"} and row["cost_usd"] == 0.01
    listed = sessions.rows(expert)
    assert [r["id"] for r in listed] == [row["id"]] and "transcript" not in listed[0]
    assert sessions.row(expert, row["id"])["transcript"][0]["role"] == "prompt"


def test_ask_cannot_write_and_keeps_caller(expert, monkeypatch):
    calls = stub(monkeypatch, answer="Use pull.")
    row = sessions.ask(expert, "How?", "a claude task session", caller={"session_id": "s9", "run_id": "r9", "target": "/x"})
    assert row["answer"] == "Use pull." and row["memory_diff"] == "" and calls[0]["write"] is False
    assert row["trigger"] == {"asker": "a claude task session", "question": "How?", "session_id": "s9", "run_id": "r9"}


def test_failed_session_is_an_error_row(expert, monkeypatch):
    stub(monkeypatch, lambda b, p, w: {"ok": False, "error": "boom", "answer": "", "transcript": []})
    row = sessions.run(expert, "compact", "x")
    assert row["status"] == "error" and row["error"] == "boom"


def test_chat_row_only_when_memory_changed(expert):
    before = store.memory(expert)
    assert sessions.note_chat(expert, before, "hello") is None
    assert sessions.rows(expert) == []
    append_memory(expert, "- [1 · 2026-11-17] Told in chat.")
    row = sessions.note_chat(expert, before, "remember: told in chat " + "x" * 600)
    assert row["kind"] == "chat" and row["status"] == "done" and row["transcript"] == []
    assert "Told in chat" in row["memory_diff"] and len(row["trigger"]["message"]) == 500
    assert [r["kind"] for r in sessions.rows(expert)] == ["chat"]


def test_changes_and_edited_by_hand(expert, monkeypatch):
    assert sessions.edited_by_hand(expert) is False  # no session has written yet
    stub(monkeypatch, lambda b, p, w: append_memory(b, "- [1 · 2026-11-17] From a task.") if "learn" in p else None)
    a = sessions.run(expert, "task-quiet", "learn")
    sessions.run(expert, "task-quiet", "nothing new")  # no diff: not a change
    sessions.ask(expert, "q?")
    assert [c["id"] for c in sessions.changes(expert)] == [a["id"]]
    assert set(sessions.changes(expert)[0]) == {"id", "kind", "started", "trigger"}
    assert sessions.edited_by_hand(expert) is False
    append_memory(expert, "- [1 · 2026-11-17] Typed by the user.")
    assert sessions.edited_by_hand(expert) is True


def test_a_session_that_changed_nothing_keeps_the_hand_edit_note(expert, monkeypatch):
    stub(monkeypatch, lambda b, p, w: append_memory(b, "- [1 · 2026-11-17] Learned.") if "learn" in p else None)
    sessions.run(expert, "task-quiet", "learn")
    append_memory(expert, "- [1 · 2026-11-17] Typed by the user.")
    assert sessions.edited_by_hand(expert) is True
    assert sessions.note_chat(expert, store.memory(expert), "hi") is None  # a chat "hi"
    sessions.run(expert, "task-quiet", "nothing new")
    assert sessions.edited_by_hand(expert) is True
    before = store.memory(expert)
    append_memory(expert, "- [1 · 2026-11-17] Told in chat.")
    sessions.note_chat(expert, before, "remember")  # a session that did change it: memory is as it left it
    assert sessions.edited_by_hand(expert) is False


# --------------------------------------------------------------------- lock ---
def _overlap(monkeypatch, expert, kinds):
    live, peak, guard = [0], [0], threading.Lock()

    def slow(b, p, w):
        with guard:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.3)
        with guard:
            live[0] -= 1
    stub(monkeypatch, slow)
    ts = [threading.Thread(target=sessions.run, args=(expert, k, "p")) for k in kinds]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    return peak[0]


def test_writers_never_overlap(expert, monkeypatch):
    assert _overlap(monkeypatch, expert, ["task-quiet", "compact", "task-quiet"]) == 1


def test_asks_run_in_parallel(expert, monkeypatch):
    assert _overlap(monkeypatch, expert, ["ask", "ask"]) == 2


def test_chat_waits_for_the_write_lock(expert):
    lock = store.write_lock(expert)
    lock.acquire()
    try:
        expert.stop_flag.set()
        assert agent_engine._expert_hold(expert) is None  # Stop while a side session holds the memory
        expert.stop_flag.clear()
        threading.Timer(0.3, lock.release).start()
        assert agent_engine._expert_hold(expert) is lock
    finally:
        lock.release()


# --------------------------------------------------------------- compaction ---
def test_compaction_threshold(expert, monkeypatch):
    def fill(n_lines, width=10):
        with open(store.memory_path(expert), "w", encoding="utf-8") as f:
            f.write("\n".join("x" * width for _ in range(n_lines)) + "\n")
    fill(sessions.LIMIT_LINES)
    assert not sessions.over_limit(expert)
    fill(sessions.LIMIT_LINES + 1)
    assert sessions.over_limit(expert)
    fill(10, sessions.LIMIT_BYTES // 10 + 50)
    assert sessions.over_limit(expert)


def test_a_write_over_the_limit_queues_one_compact(expert, monkeypatch):
    def body(b, p, w):
        if "over its limit" in p:
            with open(store.memory_path(b), "w", encoding="utf-8") as f:
                f.write(store.EMPTY_MEMORY)
        else:
            append_memory(b, "\n".join(f"- [1 · 2026-11-17] fact {i}" for i in range(250)))
    calls = stub(monkeypatch, body)
    sessions.run(expert, "task-quiet", "learn")
    deadline = time.time() + 5
    while time.time() < deadline and not any(r["kind"] == "compact" and r["status"] == "done"
                                             for r in sessions.rows(expert)):
        time.sleep(0.05)
    kinds = [r["kind"] for r in sessions.rows(expert)]
    assert kinds.count("compact") == 1 and not sessions.over_limit(expert)
    assert sum("over its limit" in c["prompt"] for c in calls) == 1
    assert sessions.after_write(expert) is False  # under the limit now


# -------------------------------------------------------------------- fence ---
def test_fence_flags(expert, tmp_path):
    granted = tmp_path / "granted"
    granted.mkdir()
    expert.meta["folders"] = [str(granted)]
    w = sessions.fence(expert, write=True)
    assert w[:4] == ["--tools", "Read,Glob,Grep,Edit,Write", "--permission-mode", "default"]
    mem = store.memory_path(expert)
    assert f"Edit(//{mem.lstrip('/')})" in w and f"Read(//{str(granted).lstrip('/')}/**)" in w
    assert "--permission-prompt-tool" not in w
    r = sessions.fence(expert, write=False)
    assert r[1] == "Read,Glob,Grep" and not any(x.startswith("Edit(") for x in r)
    cmd = agent_engine.argv("claude", "sonnet", "low", "/sp", "", fence=w)
    assert "--tools=" not in cmd and not any("mcp__" in x for x in cmd)
    assert cmd[-len(agent_engine.NO_MCP):] == agent_engine.NO_MCP
    assert cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    # The rule list is variadic: it is followed by another flag.
    assert cmd[cmd.index(w[-1]) + 1].startswith("--")


def test_fence_skips_a_folder_with_glob_characters(expert, tmp_path):
    ok, odd = tmp_path / "ok", tmp_path / "odd[1]"
    ok.mkdir()
    odd.mkdir()
    expert.meta["folders"] = [str(ok), str(odd)]
    w = sessions.fence(expert, write=False)
    assert f"Read(//{str(ok).lstrip('/')}/**)" in w and not any("odd[1]" in x for x in w)


def test_side_sessions_leave_no_cli_transcript(expert, monkeypatch):
    from fused_render.bots import claude_cli
    seen = {}

    class Proc:
        pid, returncode = 0, 0

        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd

        def communicate(self, data, timeout=None):
            return b'{"type":"result","subtype":"success","result":"ok"}\n', b""
    monkeypatch.setattr(claude_cli, "runnable", lambda: "claude")
    monkeypatch.setattr(sessions.subprocess, "Popen", Proc)
    assert sessions.harness(expert, "q", False)["answer"] == "ok"
    assert "--no-session-persistence" in seen["cmd"]
    assert "--no-session-persistence" not in agent_engine.argv("claude", "m", "low", "/sp", "",
                                                               fence=sessions.fence(expert, True))


def test_expert_turn_message_and_system_prompt(expert):
    text, sent = agent_engine.first_message(expert, "What is X?", past=["user: hi"], conv={})
    assert "CONVERSATION SO FAR" in text and "user: hi" in text and text.endswith("MESSAGE: What is X?")
    assert f"Today is {sessions.today()}." in text and sent == {}
    text, _ = agent_engine.first_message(expert, "Next?", conv={"session_id": "abc"})
    assert text == f"Today is {sessions.today()}.\n\nMESSAGE: Next?"
    assert agent_engine.system_prompt_for(expert, "/x") == store.instructions(expert)


def _plan_fakes(expert, monkeypatch, conv):
    notes, rolled = [], []
    monkeypatch.setattr(expert, "conversation", lambda: conv)
    monkeypatch.setattr(expert, "transcript_lines", lambda since=0, *a, **k: ["user: x"] * 10)
    monkeypatch.setattr(expert, "past_conversation", lambda *a, **k: ["user: earlier"])
    monkeypatch.setattr(expert, "summarize_conversation", lambda *a, **k: pytest.fail("a model call for an expert"))
    monkeypatch.setattr(expert, "conversation_rollover", lambda summary, h="": rolled.append(summary))
    monkeypatch.setattr(expert, "emit", lambda role, text, **k: notes.append(text))
    return notes, rolled


def test_over_budget_does_not_roll_an_expert_over(expert, monkeypatch):
    notes, rolled = _plan_fakes(expert, monkeypatch, {"session_id": "old", "tokens": 10 ** 9, "prompt_hash": "h"})
    _conv, resume = agent_engine._conversation_plan(expert, agent_engine.DEFAULT_MODEL, "h")
    assert resume == "old" and rolled == [] and notes == []


def test_an_expert_rollover_makes_no_model_call(expert, monkeypatch):
    # Its instructions changed: a resumed session cannot take the new system prompt, so a fresh one, from the digest.
    notes, rolled = _plan_fakes(expert, monkeypatch, {"session_id": "old", "tokens": 10, "prompt_hash": "before"})
    _conv, resume = agent_engine._conversation_plan(expert, agent_engine.DEFAULT_MODEL, "after")
    assert resume is None and rolled == ["user: earlier"] and notes == []


def test_an_expert_chat_leaves_compaction_to_the_cli(expert, monkeypatch):
    from fused_render.bots import claude_cli
    seen = []

    def spawn(cmd, cwd):
        seen.append(cmd)
        raise RuntimeError("stop here")
    monkeypatch.setattr(claude_cli, "runnable", lambda: "claude")
    monkeypatch.setattr(agent_engine, "_spawn", spawn)
    agent_engine.run(expert, "hi")
    assert seen and "--autocompact" not in seen[0] and "--resume" not in seen[0]
    # An ordinary bot still holds the CLI's compaction off until fused's rollover.
    assert "--autocompact" in agent_engine.argv("claude", "m", "low", "/sp", "/mcp", window=200000)


# -------------------------------------------------------------- task-quiet ---
class FakeTasks:
    def __init__(self, tasks):
        self.tasks = tasks

    def _collect(self):
        return {t["session_id"]: t for t in self.tasks}

    def _place(self, task):
        task["target"] = "/work"


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    """One task session `s1` whose transcript file is `path`; the rendered turns are the file's lines."""
    path = tmp_path / "s1.jsonl"
    path.write_text("turn 1\n")
    monkeypatch.setattr(quiet, "_tasks", lambda: FakeTasks([{"session_id": "s1", "path": str(path)}]))
    monkeypatch.setattr(quiet, "caller", lambda run_id: {"run_id": run_id, "session_id": "s1" if run_id == "r1" else "",
                                                         "backend": "claude", "target": "/work"})
    monkeypatch.setattr(quiet, "_last_active", lambda p, m: m)

    def md(session, since=0):
        lines = open(session["path"]).read().splitlines()
        return "\n".join("USER| " + ln for ln in lines[since:]), len(lines)
    monkeypatch.setattr(quiet, "transcript_markdown", md)
    return path


def _age(path, s=3600):
    t = time.time() - s
    os.utime(path, (t, t))


def test_due_rule_and_only_the_unread_part(expert, monkeypatch, transcript):
    calls = stub(monkeypatch)
    quiet.record_use(expert, "r1")
    assert quiet._load(expert)["sessions"]["s1"]["run_ids"] == ["r1"]
    assert quiet.run_pass()["sessions"] == 0  # not idle yet
    _age(transcript)
    assert quiet.run_pass()["sessions"] == 1
    assert "USER| turn 1" in calls[-1]["prompt"] and "update your memory" in calls[-1]["prompt"].lower()
    row = sessions.rows(expert)[0]
    size = os.path.getsize(transcript)
    assert row["kind"] == "task-quiet" and row["trigger"] == {
        "session_id": "s1", "run_id": "r1", "target": "/work", "backend": "claude", "from_size": 0, "to_size": size}
    assert quiet.run_pass()["sessions"] == 0  # read at this size
    with open(transcript, "a") as f:
        f.write("turn 2\n")
    _age(transcript)
    assert quiet.run_pass()["sessions"] == 1
    assert "turn 2" in calls[-1]["prompt"] and "turn 1" not in calls[-1]["prompt"]  # a resumed task: only its new part


def test_idle_env_override(monkeypatch):
    monkeypatch.setenv(quiet.IDLE_ENV, "30")
    assert quiet.idle_s() == 30
    monkeypatch.setenv(quiet.IDLE_ENV, "nope")
    assert quiet.idle_s() == quiet.IDLE_S == 600


def test_pending_run_resolves_later(expert, monkeypatch, transcript):
    stub(monkeypatch)
    quiet.record_use(expert, "r2")  # a fresh chat: its session is not known yet
    st = quiet._load(expert)
    assert "r2" in st["pending"] and st["sessions"] == {}
    monkeypatch.setattr(quiet, "caller", lambda run_id: {"run_id": run_id, "session_id": "s1",
                                                         "backend": "claude", "target": "/work"})
    _age(transcript)
    assert quiet.run_pass()["sessions"] == 1
    st = quiet._load(expert)
    assert st["pending"] == {} and st["sessions"]["s1"]["run_ids"] == ["r2"]


def test_three_failed_attempts_then_give_up_until_it_grows(expert, monkeypatch, transcript):
    calls = stub(monkeypatch, lambda b, p, w: {"ok": False, "error": "harness down", "answer": "", "transcript": []})
    quiet.record_use(expert, "r1")
    _age(transcript)
    for _ in range(3):
        assert quiet.run_pass() == {"sessions": 1, "errors": 1}
    assert quiet.run_pass()["sessions"] == 0 and len(calls) == 3
    rec = quiet._load(expert)["sessions"]["s1"]
    assert rec["size"] == 0 and rec["fails"]["count"] == 3  # nothing recorded as read
    with open(transcript, "a") as f:
        f.write("turn 2\n")
    _age(transcript)
    stub(monkeypatch)
    assert quiet.run_pass()["sessions"] == 1
    rec = quiet._load(expert)["sessions"]["s1"]
    assert rec["size"] == os.path.getsize(transcript) and "fails" not in rec


def test_a_busy_expert_is_not_a_failed_attempt(expert, monkeypatch, transcript):
    calls = stub(monkeypatch)
    monkeypatch.setattr(sessions, "_timeout", lambda: 0.01)
    quiet.record_use(expert, "r1")
    _age(transcript)
    lock = store.write_lock(expert)
    lock.acquire()  # a long chat turn holds the memory
    try:
        for _ in range(4):
            assert quiet.run_pass() == {"sessions": 1, "errors": 1}
    finally:
        lock.release()
    assert calls == [] and "fails" not in quiet._load(expert)["sessions"]["s1"]
    assert all(r["error"] == sessions.BUSY for r in sessions.rows(expert))
    assert quiet.run_pass() == {"sessions": 1, "errors": 0} and len(calls) == 1


def test_tick_skips_without_the_workspace_claim(monkeypatch):
    from fused_render.experts import scheduler
    monkeypatch.setattr(registry, "owns", lambda: True)
    monkeypatch.setattr(registry, "workspace_claimed", lambda: False)
    monkeypatch.setattr(quiet, "run_pass", lambda **k: pytest.fail("a pass without the workspace"))
    out = scheduler.tick()
    assert out["ran"] is False and "workspace" in out["skipped"]
