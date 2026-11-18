"""Experts with the harness stubbed (docs/experts.md): session rows and diffs, the write lock, compaction, the
fence flags, chat rows, and the task-learn due rule with its retry cap."""
from _bots_conftest import *  # noqa: F401,F403 — app_home (a per-test home and workspace)
import json
import os
import shutil
import threading
import time

import pytest

from fused_render.bots import agent_engine, registry
from fused_render.experts import learn, sessions, store


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
    row = sessions.run(expert, "task-learn", "read this", {"session_id": "s1"})
    assert row["status"] == "done" and row["kind"] == "task-learn" and row["ended"] >= row["started"]
    assert "+- [1 · 2026-11-17] A fact." in row["memory_diff"]
    assert row["trigger"] == {"session_id": "s1"} and row["cost_usd"] == 0.01
    listed = sessions.rows(expert)
    assert [r["id"] for r in listed] == [row["id"]] and "transcript" not in listed[0]
    assert sessions.row(expert, row["id"])["transcript"][0]["role"] == "prompt"


def test_ask_cannot_write_and_keeps_caller(expert, monkeypatch):
    calls = stub(monkeypatch, answer="Use pull.")
    row = sessions.ask(expert, "How?", "a claude task session", caller={"session_id": "s9", "run_id": "r9", "target": "/x"})
    assert row["answer"] == "Use pull." and row["memory_diff"] == "" and calls[0]["write"] is False
    assert store.where(expert) in calls[0]["prompt"]
    assert row["trigger"] == {"asker": "a claude task session", "question": "How?", "session_id": "s9", "run_id": "r9"}


def test_failed_session_is_an_error_row(expert, monkeypatch):
    stub(monkeypatch, lambda b, p, w: {"ok": False, "error": "boom", "answer": "", "transcript": []})
    row = sessions.run(expert, "dream", "x")
    assert row["status"] == "error" and row["error"] == "boom"


def test_chat_row_only_when_memory_changed(expert):
    before = store.memory(expert)
    assert sessions.note_chat(expert, before, "hello") is None
    assert sessions.rows(expert) == []
    append_memory(expert, "- [1 · 2026-11-17] Told in chat.")
    t0 = time.time() - 30  # the turn began before the row is noted at its end
    row = sessions.note_chat(expert, before, "remember: told in chat " + "x" * 600, t0)
    assert row["kind"] == "chat" and row["status"] == "done" and row["transcript"] == []
    assert row["started"] == t0 and row["started"] < row["ended"]
    assert "Told in chat" in row["memory_diff"] and len(row["trigger"]["message"]) == 500
    assert [r["kind"] for r in sessions.rows(expert)] == ["chat"]


def test_changes_and_edited_by_hand(expert, monkeypatch):
    assert sessions.edited_by_hand(expert) is False  # no session has written yet
    stub(monkeypatch, lambda b, p, w: append_memory(b, "- [1 · 2026-11-17] From a task.") if "learn" in p else None)
    a = sessions.run(expert, "task-learn", "learn")
    sessions.run(expert, "task-learn", "nothing new")  # no diff: not a change
    sessions.ask(expert, "q?")
    assert [c["id"] for c in sessions.changes(expert)] == [a["id"]]
    assert set(sessions.changes(expert)[0]) == {"id", "kind", "started", "trigger"}
    assert sessions.edited_by_hand(expert) is False
    append_memory(expert, "- [1 · 2026-11-17] Typed by the user.")
    assert sessions.edited_by_hand(expert) is True


def test_a_session_that_changed_nothing_keeps_the_hand_edit_note(expert, monkeypatch):
    stub(monkeypatch, lambda b, p, w: append_memory(b, "- [1 · 2026-11-17] Learned.") if "learn" in p else None)
    sessions.run(expert, "task-learn", "learn")
    append_memory(expert, "- [1 · 2026-11-17] Typed by the user.")
    assert sessions.edited_by_hand(expert) is True
    assert sessions.note_chat(expert, store.memory(expert), "hi") is None  # a chat "hi"
    sessions.run(expert, "task-learn", "nothing new")
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
    assert _overlap(monkeypatch, expert, ["task-learn", "dream", "task-learn"]) == 1


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


def test_a_write_over_the_limit_queues_one_dream(expert, monkeypatch):
    def body(b, p, w):
        if "over its limit" in p:
            with open(store.memory_path(b), "w", encoding="utf-8") as f:
                f.write(store.EMPTY_MEMORY)
        else:
            append_memory(b, "\n".join(f"- [1 · 2026-11-17] fact {i}" for i in range(250)))
    calls = stub(monkeypatch, body)
    sessions.run(expert, "task-learn", "learn")
    deadline = time.time() + 5
    while time.time() < deadline and not any(r["kind"] == "dream" and r["status"] == "done"
                                             for r in sessions.rows(expert)):
        time.sleep(0.05)
    kinds = [r["kind"] for r in sessions.rows(expert)]
    assert kinds.count("dream") == 1 and not sessions.over_limit(expert)
    assert sum("over its limit" in c["prompt"] for c in calls) == 1
    assert all(store.where(expert) in c["prompt"] for c in calls if "over its limit" in c["prompt"])
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
    # The chat (an mcp.json): the same fence, the bot MCP server and its permission prompt instead of NO_MCP.
    chat = agent_engine.argv("claude", "sonnet", "low", "/sp", "/mcp.json", fence=w)
    assert chat[chat.index("--permission-prompt-tool") + 1] == "mcp__bot__permission"
    assert chat[chat.index("--mcp-config") + 1] == "/mcp.json" and "--strict-mcp-config" in chat
    assert "--setting-sources=" in chat and chat[chat.index(w[-1]) + 1].startswith("--")


def test_fence_skips_a_folder_with_glob_characters(expert, tmp_path):
    ok, odd = tmp_path / "ok", tmp_path / "odd[1]"
    ok.mkdir()
    odd.mkdir()
    expert.meta["folders"] = [str(ok), str(odd)]
    w = sessions.fence(expert, write=False)
    assert f"Read(//{str(ok).lstrip('/')}/**)" in w and not any("odd[1]" in x for x in w)


def _canvases(expert, tmp_path, monkeypatch, envs=("prod", "unstable", "custom")):
    """A canvases base with one folder per environment, granted to the expert, and the `fused` wrapper on."""
    from fused_render import canvases, fusedcli
    base = tmp_path / "canvas"
    for env in envs:
        (base / env / "user").mkdir(parents=True)
    monkeypatch.setattr(canvases, "canvases_base", lambda: str(base))
    monkeypatch.setenv(fusedcli.CLI_DIR_ENV, str(tmp_path / "fused-bin"))
    expert.meta["folders"] = [str(base)]
    return base


def test_fence_lets_a_chat_and_an_ask_run_udfs_per_environment(expert, tmp_path, monkeypatch):
    base = _canvases(expert, tmp_path, monkeypatch)
    assert store.run_envs(expert) == [("prod", str(base / "prod")), ("unstable", str(base / "unstable"))]  # never custom
    bash = ["Bash(fused workbench --env prod run:*)", "Bash(fused workbench --env prod udf-schema:*)",
            "Bash(fused workbench --env prod whoami)", "Bash(fused workbench --env unstable run:*)",
            "Bash(fused workbench --env unstable udf-schema:*)", "Bash(fused workbench --env unstable whoami)",
            "Bash(fused workbench --help)", "Bash(fused workbench run --help)",
            "Bash(fused workbench udf-schema --help)"]
    chat = sessions.fence(expert, write=True, run=True)  # a routine's chat turn: nobody at the card
    assert chat[1] == "Read,Glob,Grep,Edit,Write,Bash" and [x for x in chat if x.startswith("Bash(")] == bash
    ask = sessions.fence(expert, write=False, run=True)
    assert ask[1] == "Read,Glob,Grep,Edit,Write,Bash" and [x for x in ask if x.startswith("Bash(")] == bash
    assert "--permission-prompt-tool" not in chat + ask and "--disallowedTools" not in chat + ask
    # The scratch folder of throwaway UDF files is the one write an ask has; memory.md stays the chat's and the writers'.
    scratch = f"Edit(//{store.udfs_dir(expert).lstrip('/')}/**)"
    mem = f"Edit(//{store.memory_path(expert).lstrip('/')})"
    assert os.path.isdir(store.udfs_dir(expert)) and store.udfs_dir(expert).endswith(os.path.join(".fused", "udfs"))
    assert [x for x in ask if x.startswith("Edit(")] == [scratch]
    assert [x for x in chat if x.startswith("Edit(")] == [mem, scratch]
    assert chat[2:4] == ask[2:4] == ["--permission-mode", "default"] and "--settings" not in chat + ask
    # task-learn and dream (and any caller that does not ask for it) get no Bash at all
    for flags in (sessions.fence(expert, write=True), sessions.fence(expert, write=False)):
        assert "Bash" not in flags[1] and not any(x.startswith("Bash(") for x in flags)


def test_the_attended_chat_runs_in_auto_mode_and_every_edit_asks(expert, tmp_path, monkeypatch):
    _canvases(expert, tmp_path, monkeypatch, envs=("prod",))
    auto = sessions.fence(expert, write=True, run=True, auto=True)
    assert auto[:5] == ["--tools", "Read,Glob,Grep,Edit,Write,Bash", "--permission-mode", "auto", "--settings"]
    settings = json.loads(auto[5])
    # An ask rule beats an allow rule and auto mode's own approval of edits in the working directory: every edit
    # reaches the chat's permission tool, so no Edit allow rule is passed.
    assert settings["permissions"] == {"ask": ["Edit(//**)"]} and not any(x.startswith("Edit(") for x in auto)
    mode = settings["autoMode"]
    assert all(mode[k][0] == "$defaults" and len(mode[k]) > 1 for k in ("environment", "allow", "soft_deny"))
    assert "fused workbench --env <env> run" in mode["allow"][1] and store.expert_dir(expert) in mode["soft_deny"][1]
    assert auto[6] == "--allowedTools" and "Bash(fused workbench --env prod run:*)" in auto
    chat = agent_engine.argv("claude", "sonnet", "low", "/sp", "/mcp.json", fence=auto)
    assert chat[chat.index("--permission-prompt-tool") + 1] == "mcp__bot__permission"
    # What that tool lets through without a card: memory.md and files inside the scratch folder, by real path.
    d, scratch = store.expert_dir(expert), store.udfs_dir(expert)
    assert store.free_write(expert, store.memory_path(expert)) and store.free_write(expert, "memory.md", d)
    assert store.free_write(expert, os.path.join(scratch, "a.py")) and store.free_write(expert, ".fused/udfs/b/c.py", d)
    for other in ("instructions.md", "skills/a.md", "notes.md", ".fused/expert/read.json", ".fused/udfs/../x.py",
                  ".fused/udfs", "/etc/hosts", "", None):
        assert not store.free_write(expert, other, d), other
    os.symlink(str(tmp_path), os.path.join(scratch, "out"))
    assert not store.free_write(expert, os.path.join(scratch, "out", "x.py"))  # a symlink out of the folder
    monkeypatch.delenv(__import__("fused_render.fusedcli", fromlist=["x"]).CLI_DIR_ENV)  # may not run: no scratch
    assert not store.free_write(expert, os.path.join(scratch, "a.py")) and store.free_write(expert, "memory.md", d)
    plain = sessions.fence(expert, write=True, run=True, auto=True)
    assert plain[1] == "Read,Glob,Grep,Edit,Write" and plain[3] == "auto"


def test_fence_has_no_bash_without_an_environment_folder_or_the_cli(expert, tmp_path, monkeypatch):
    from fused_render import fusedcli
    base = _canvases(expert, tmp_path, monkeypatch, envs=("custom",))
    assert store.run_envs(expert) == [] and "Bash" not in sessions.fence(expert, write=True, run=True)[1]
    (base / "prod").mkdir()
    assert [e for e, _ in store.run_envs(expert)] == ["prod"]
    expert.meta["folders"] = [str(base / "prod" / "nope")]  # the environment's folder is not inside a granted one
    assert store.run_envs(expert) == []
    expert.meta["folders"] = [str(base)]
    monkeypatch.delenv(fusedcli.CLI_DIR_ENV)  # no `fused` wrapper: nothing to run
    assert store.run_envs(expert) == [] and "Bash" not in sessions.fence(expert, write=False, run=True)[1]


def test_side_sessions_run_udfs_only_in_an_ask(expert, tmp_path, monkeypatch):
    from fused_render.bots import claude_cli
    _canvases(expert, tmp_path, monkeypatch, envs=("prod",))
    cmds = []

    class Proc:
        pid, returncode = 0, 0

        def __init__(self, cmd, **kw):
            cmds.append(cmd)

        def communicate(self, data, timeout=None):
            use = {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash", "input": {"command": "fused workbench --env prod whoami"}}]}}
            return (json.dumps(use) + '\n{"type":"result","subtype":"success","result":"ok"}\n').encode(), b""
    monkeypatch.setattr(claude_cli, "runnable", lambda: "claude")
    monkeypatch.setattr(sessions.subprocess, "Popen", Proc)
    res = sessions.harness(expert, "q", False)  # an ask
    assert "Read,Glob,Grep,Edit,Write,Bash" in cmds[-1] and "Bash(fused workbench --env prod run:*)" in cmds[-1]
    assert cmds[-1][cmds[-1].index("--permission-mode") + 1] == "default"  # nobody to ask: never auto mode
    assert [x for x in cmds[-1] if x.startswith("Edit(")] == [f"Edit(//{store.udfs_dir(expert).lstrip('/')}/**)"]
    assert {"role": "tool", "text": "Bash fused workbench --env prod whoami"} in res["transcript"]  # the row shows it
    sessions.harness(expert, "q", True)  # a task-learn or a dream
    assert "Read,Glob,Grep,Edit,Write" in cmds[-1] and not any(x.startswith("Bash(") for x in cmds[-1])
    assert [x for x in cmds[-1] if x.startswith("Edit(")] == [f"Edit(//{store.memory_path(expert).lstrip('/')})"]
    assert cmds[-1][cmds[-1].index("--permission-mode") + 1] == "default"


def test_only_a_chat_and_an_ask_are_told_how_to_run(expert, tmp_path, monkeypatch):
    base = _canvases(expert, tmp_path, monkeypatch, envs=("prod",))
    note = (f"copies under {base / 'prod'} are environment `prod`, run with "
            "`fused workbench --env prod run --engine remote <canvas> <udf>`")
    assert note not in store.where(expert) and note in store.where(expert, run=True)
    assert "You may run UDFs on Fused: copies" in store.where(expert, run=True)  # no such skill in its folder yet
    skill = os.path.join(store.expert_dir(expert), "skills", store.RUN_SKILL)
    os.makedirs(os.path.dirname(skill), exist_ok=True)
    open(skill, "w").close()
    assert f"You may run UDFs on Fused (read {skill} first)" in store.where(expert, run=True)
    calls = stub(monkeypatch)
    sessions.ask(expert, "does it run?")
    assert note in calls[-1]["prompt"]
    text, _ = agent_engine.first_message(expert, "run it", past=[], conv={})
    assert note in text
    sessions.dream(expert)
    assert note not in calls[-1]["prompt"] and store.where(expert) in calls[-1]["prompt"]


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
    assert f"Today is {sessions.today()}." in text and list(sent) == ["where"]  # what it was told, by hash
    where = f"Your folder is {store.expert_dir(expert)}; your memory is {store.memory_path(expert)}."
    assert store.where(expert) == where and where in text  # a fresh session is told where its memory is
    text, _ = agent_engine.first_message(expert, "Next?", conv={"session_id": "abc", "sent": sent})
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
    monkeypatch.setenv("FUSED_RENDER_ORIGIN", "http://127.0.0.1:9")  # the chat's mcp.json (its approval cards) names it
    agent_engine.run(expert, "hi")
    assert seen and "--autocompact" not in seen[0] and "--resume" not in seen[0]
    # An ordinary bot still holds the CLI's compaction off until fused's rollover.
    assert "--autocompact" in agent_engine.argv("claude", "m", "low", "/sp", "/mcp", window=200000)


# -------------------------------------------------------------- task-learn ---
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
    monkeypatch.setattr(learn, "_tasks", lambda: FakeTasks([{"session_id": "s1", "path": str(path)}]))
    monkeypatch.setattr(learn, "caller", lambda run_id: {"run_id": run_id, "session_id": "s1" if run_id == "r1" else "",
                                                         "backend": "claude", "target": "/work"})
    monkeypatch.setattr(learn, "_last_active", lambda p, m: m)

    def md(session, since=0, bot=None):
        lines = open(session["path"]).read().splitlines()
        return "\n".join("USER| " + ln for ln in lines[since:]), len(lines)
    monkeypatch.setattr(learn, "transcript_markdown", md)
    return path


def _age(path, s=3600):
    t = time.time() - s
    os.utime(path, (t, t))


def test_due_rule_and_only_the_unread_part(expert, monkeypatch, transcript):
    calls = stub(monkeypatch)
    learn.record_use(expert, "r1")
    assert learn._load(expert)["sessions"]["s1"]["run_ids"] == ["r1"]
    assert learn.run_pass()["sessions"] == 0  # not idle yet
    _age(transcript)
    assert learn.run_pass()["sessions"] == 1
    assert "USER| turn 1" in calls[-1]["prompt"] and "update your memory" in calls[-1]["prompt"].lower()
    assert store.where(expert) in calls[-1]["prompt"]  # the absolute paths: never a guessed memory.md
    row = sessions.rows(expert)[0]
    size = os.path.getsize(transcript)
    assert row["kind"] == "task-learn" and row["trigger"] == {
        "session_id": "s1", "run_id": "r1", "target": "/work", "backend": "claude", "from_size": 0, "to_size": size}
    assert learn.run_pass()["sessions"] == 0  # read at this size
    with open(transcript, "a") as f:
        f.write("turn 2\n")
    _age(transcript)
    assert learn.run_pass()["sessions"] == 1
    assert "turn 2" in calls[-1]["prompt"] and "turn 1" not in calls[-1]["prompt"]  # a resumed task: only its new part


def test_idle_env_override(monkeypatch):
    monkeypatch.setenv(learn.IDLE_ENV, "30")
    assert learn.idle_s() == 30
    monkeypatch.setenv(learn.IDLE_ENV, "nope")
    assert learn.idle_s() == learn.IDLE_S == 1200


def test_pending_run_resolves_later(expert, monkeypatch, transcript):
    stub(monkeypatch)
    learn.record_use(expert, "r2")  # a fresh chat: its session is not known yet
    st = learn._load(expert)
    assert "r2" in st["pending"] and st["sessions"] == {}
    monkeypatch.setattr(learn, "caller", lambda run_id: {"run_id": run_id, "session_id": "s1",
                                                         "backend": "claude", "target": "/work"})
    _age(transcript)
    assert learn.run_pass()["sessions"] == 1
    st = learn._load(expert)
    assert st["pending"] == {} and st["sessions"]["s1"]["run_ids"] == ["r2"]


def test_three_failed_attempts_then_give_up_until_it_grows(expert, monkeypatch, transcript):
    calls = stub(monkeypatch, lambda b, p, w: {"ok": False, "error": "harness down", "answer": "", "transcript": []})
    learn.record_use(expert, "r1")
    _age(transcript)
    for _ in range(3):
        assert learn.run_pass() == {"sessions": 1, "errors": 1}
    assert learn.run_pass()["sessions"] == 0 and len(calls) == 3
    rec = learn._load(expert)["sessions"]["s1"]
    assert rec["size"] == 0 and rec["fails"]["count"] == 3  # nothing recorded as read
    with open(transcript, "a") as f:
        f.write("turn 2\n")
    _age(transcript)
    stub(monkeypatch)
    assert learn.run_pass()["sessions"] == 1
    rec = learn._load(expert)["sessions"]["s1"]
    assert rec["size"] == os.path.getsize(transcript) and "fails" not in rec


def test_a_busy_expert_does_not_use_up_the_pass(expert, monkeypatch):
    stub(monkeypatch)
    sids = ("s1", "s2", "s3")
    store.write_json(learn._path(expert), {"sessions": {s: {"run_ids": [], "size": 0, "lines": 0} for s in sids}})
    monkeypatch.setattr(learn, "_tasks", lambda: type("T", (), {"_collect": staticmethod(
        lambda: {s: {"session_id": s} for s in sids})})())
    monkeypatch.setattr(learn, "_session_of", lambda t, tasks: {
        "session_id": t["session_id"], "size": 10, "last_active": 0, "target": "/work", "backend": "claude"})
    monkeypatch.setattr(learn, "transcript_markdown", lambda session, since=0, bot=None: ("USER| hi", 1))
    with store.write_lock(expert):  # a chat turn holds the memory: one refusal, the expert's other tasks wait
        assert learn.run_pass(limit=2) == {"sessions": 1, "errors": 1}
    assert learn.run_pass(limit=2) == {"sessions": 2, "errors": 0}  # the limit counts harnesses started


def test_a_busy_expert_is_not_a_failed_attempt(expert, monkeypatch, transcript):
    calls = stub(monkeypatch)
    learn.record_use(expert, "r1")
    _age(transcript)
    lock = store.write_lock(expert)
    lock.acquire()  # a long chat turn holds the memory
    try:
        for _ in range(4):
            assert learn.run_pass() == {"sessions": 1, "errors": 1}
    finally:
        lock.release()
    assert calls == [] and "fails" not in learn._load(expert)["sessions"]["s1"]
    assert all(r["error"] == sessions.BUSY for r in sessions.rows(expert))
    assert learn.run_pass() == {"sessions": 1, "errors": 0} and len(calls) == 1


def test_tick_skips_without_the_workspace_claim(monkeypatch):
    from fused_render.experts import scheduler
    monkeypatch.setattr(registry, "owns", lambda: True)
    monkeypatch.setattr(registry, "workspace_claimed", lambda: False)
    monkeypatch.setattr(learn, "run_pass", lambda **k: pytest.fail("a pass without the workspace"))
    out = scheduler.tick()
    assert out["ran"] is False and "workspace" in out["skipped"]


# ------------------------------------------------------------ review fixes ---
def test_an_ask_never_records_a_memory_diff(expert, monkeypatch):
    # A chat or task-learn writing while the ask runs (an ask takes no write lock) is not the ask's change.
    stub(monkeypatch, lambda b, p, w: append_memory(b, "- [1 · 2026-11-17] Written by a concurrent chat."))
    row = sessions.ask(expert, "q?")
    assert row["status"] == "done" and row["memory_diff"] == "" and sessions.changes(expert) == []


def test_a_shrunk_transcript_is_read_again_whole(expert, monkeypatch, transcript):
    calls = stub(monkeypatch)
    transcript.write_text("turn 1\nturn 2\nturn 3\n")
    learn.record_use(expert, "r1")
    _age(transcript)
    assert learn.run_pass()["sessions"] == 1
    assert learn.run_pass()["sessions"] == 0  # unchanged size: skipped
    transcript.write_text("summary\n")  # rewritten / compacted: smaller than what was read
    _age(transcript)
    assert learn.run_pass()["sessions"] == 1
    assert "USER| summary" in calls[-1]["prompt"]
    assert sessions.rows(expert)[0]["trigger"]["from_size"] == 0
    rec = learn._load(expert)["sessions"]["s1"]
    assert rec["size"] == os.path.getsize(transcript) and rec["lines"] == 1
    assert learn.run_pass()["sessions"] == 0


def test_a_failed_task_learn_that_changed_memory_counts_as_read(expert, monkeypatch, transcript):
    def edit_then_time_out(b, p, w):
        append_memory(b, "- [1 · 2026-11-17] Learned before the timeout.")
        return {"ok": False, "error": "no answer after 600 s", "answer": "", "transcript": []}
    calls = stub(monkeypatch, edit_then_time_out)
    learn.record_use(expert, "r1")
    _age(transcript)
    assert learn.run_pass() == {"sessions": 1, "errors": 1}
    rec = learn._load(expert)["sessions"]["s1"]
    assert rec["size"] == os.path.getsize(transcript) and "fails" not in rec
    assert learn.run_pass()["sessions"] == 0 and len(calls) == 1  # not retried: counts would be raised twice


def test_after_shutdown_no_session_or_dream_starts(expert, monkeypatch):
    from fused_render.experts import scheduler
    calls = stub(monkeypatch)
    sessions.shutdown()
    try:
        row = sessions.run(expert, "ask", "q")
        assert row["status"] == "error" and row["error"] == sessions.CLOSED and calls == []
        with open(store.memory_path(expert), "w", encoding="utf-8") as f:
            f.write("x\n" * (sessions.LIMIT_LINES + 1))
        assert sessions.after_write(expert) is False  # no dream queued after shutdown
        scheduler.start()  # the server starting again reopens
        assert sessions.closed() is False
    finally:
        scheduler.shutdown(0)
        sessions.reopen()
    assert sessions.run(expert, "ask", "q")["status"] == "done"


def test_bots_shutdown_ends_the_experts_before_the_claim_goes(monkeypatch):
    seen = []

    class Proc:
        def wait(self, timeout=None):
            return 0
    proc = Proc()
    monkeypatch.setattr(sessions, "_signal", lambda p, sig: seen.append("kill"))
    monkeypatch.setattr(registry, "_drop_routines_lock", lambda: seen.append(("claim", sessions.closed())))
    with sessions._GUARD:
        sessions._PROCS.add(proc)
    try:
        registry.shutdown(budget_s=0.1)
    finally:
        with sessions._GUARD:
            sessions._PROCS.discard(proc)
    assert seen == ["kill", ("claim", True)]  # the harness is ended before the claim goes
    registry.reset_for_tests()  # a later test must be able to run sessions again
    assert sessions.closed() is False


def test_an_expert_never_falls_back_to_the_steps_engine(expert, monkeypatch):
    import sys

    import fused_render.bots as bots_pkg
    from fused_render.bots import steps_engine
    monkeypatch.delattr(bots_pkg, "agent_engine")
    monkeypatch.setitem(sys.modules, "fused_render.bots.agent_engine", None)  # the import raises
    monkeypatch.setattr(steps_engine, "run", lambda *a, **k: pytest.fail("the steps engine ran an expert"))
    said = []
    monkeypatch.setattr(expert, "emit", lambda role, text, **k: said.append((role, text)))
    assert expert.start_task("hi") is False
    assert ("error", "An expert needs the agent engine (Claude Code), which could not load.") in said
    assert expert.meta.get("status") == "idle" and (expert.thread is None or not expert.thread.is_alive())


# ------------------------------------------------------- shutdown races ---
class _PausedProc:
    """A harness process whose spawn (Popen) is paused until `go` is set."""
    entered, go, made = threading.Event(), threading.Event(), []

    def __init__(self, cmd, **kw):
        type(self).entered.set()
        assert type(self).go.wait(5)
        self.pid, self.returncode, self.signals, self.ended = 0, -15, [], threading.Event()
        type(self).made.append(self)

    def communicate(self, data, timeout=None):
        assert self.ended.wait(5)  # runs until signalled
        return b"", b""

    def wait(self, timeout=None):
        return self.returncode


def test_shutdown_while_a_spawn_is_paused_still_ends_it(expert, monkeypatch):
    from fused_render.bots import claude_cli
    monkeypatch.setattr(claude_cli, "runnable", lambda: "claude")
    monkeypatch.setattr(sessions.subprocess, "Popen", _PausedProc)
    monkeypatch.setattr(sessions, "_signal", lambda p, sig: (p.signals.append(sig), p.ended.set()))
    _PausedProc.entered.clear(), _PausedProc.go.clear(), _PausedProc.made.clear()
    res = []
    spawn = threading.Thread(target=lambda: res.append(sessions.harness(expert, "q", False)))
    spawn.start()
    try:
        assert _PausedProc.entered.wait(5)
        stop = threading.Thread(target=sessions.shutdown)
        stop.start()
        time.sleep(0.2)
        assert stop.is_alive()  # it cannot snapshot the processes mid-spawn
        _PausedProc.go.set()
        stop.join(5)
        spawn.join(5)
        assert [p.signals for p in _PausedProc.made] == [[sessions.signal.SIGTERM]]  # tracked, so ended
        assert res[0]["error"] == sessions.CLOSED and not sessions._PROCS
        # Once closed, a harness is never spawned at all.
        _PausedProc.entered.clear()
        assert sessions.harness(expert, "q", False)["error"] == sessions.CLOSED and not _PausedProc.entered.is_set()
    finally:
        _PausedProc.go.set()
        sessions.reopen()


def test_shutdown_waits_for_every_harness_within_one_budget(monkeypatch):
    """Processes that ignore SIGTERM: signalled together, waited for together, then SIGKILLed; never one budget
    each."""
    class Stubborn:
        def __init__(self):
            self.signals = []

        def wait(self, timeout=None):
            if sessions._SIGKILL in self.signals:
                return -9
            time.sleep(max(0.0, timeout or 0))
            raise sessions.subprocess.TimeoutExpired("claude", timeout)
    procs = [Stubborn() for _ in range(4)]
    monkeypatch.setattr(sessions, "_signal", lambda p, sig: p.signals.append(sig))
    with sessions._GUARD:
        sessions._PROCS.update(procs)
    t0 = time.monotonic()
    try:
        sessions.shutdown(budget_s=0.4)
    finally:
        with sessions._GUARD:
            sessions._PROCS.difference_update(procs)
        sessions.reopen()
    assert time.monotonic() - t0 < 1.2  # one shared wait, not 4 x the budget
    assert all(p.signals == [sessions.signal.SIGTERM, sessions._SIGKILL] for p in procs)


def test_prompts_name_the_granted_folders_by_their_real_paths(expert, tmp_path):
    """A relocated workspace: the canvas copies are not at the default path the skills name, so the prompt says
    where the granted folder really is (a read at the default path would be refused)."""
    assert "You may also read" not in store.where(expert)
    granted = tmp_path / "ws" / "canvas"
    granted.mkdir(parents=True)
    expert.meta["folders"] = [str(granted)]
    assert store.where(expert).endswith(f" You may also read: {granted}.")


def _call(expert_name, tool="mcp__fused__expert_ask"):
    return {"role": "assistant", "segments": [{"kind": "tool", "name": tool, "input": {"expert": expert_name}}]}


def test_a_long_transcript_is_cut_around_the_calls_to_this_expert(expert, monkeypatch):
    other = type("B", (), {"id": "other"})()
    monkeypatch.setattr(store, "find", lambda key: expert if key == "me" else other if key == "them" else None)
    pad = lambda tag, n: [{"role": "user", "text": f"{tag} {i} " + "x" * 80} for i in range(n)]  # noqa: E731
    turns = pad("a", 300) + [_call("me")] + pad("b", 300) + [_call("them")] + pad("c", 300) + [_call("me")] + pad("d", 300)
    lines, calls = learn._lines(turns, expert)
    assert calls == [300, 902]  # the other expert's call is not an anchor
    md = learn.fit(lines, calls, 6000)
    assert len(md) <= 6000 + 3 * (len(learn.CUT) + 1)
    kept = md.splitlines()
    assert kept[0] == learn.CUT and kept[-1] == learn.CUT and kept.count(learn.CUT) == 3
    assert sum(ln.startswith("TOOL| ") for ln in kept) == 2          # both calls survive
    assert all(ln == learn.CUT or ln[:6] in ("USER| ", "TOOL| ") for ln in kept)   # whole lines only
    n = lambda tag: sum(ln.startswith(f"USER| {tag} ") for ln in kept)  # noqa: E731
    assert n("c") < n("b") and n("a") < n("b") and n("a") < n("d")   # more after each call than before it
    assert abs((n("a") + n("b")) - (n("c") + n("d"))) <= 3            # the budget is shared between the calls
    # short enough: untouched; no call to this expert: the end is kept, as before
    assert learn.fit(lines[:5], [], 6000) == "\n".join(lines[:5])
    tail = learn.fit(lines, [], 6000)
    assert tail.startswith(learn.CUT) and tail.endswith(lines[-1]) and len(tail) <= 6000 + len(learn.CUT) + 1
    # a last line longer than the budget: no half line without its speaker tag
    assert learn.fit(lines + ["USER| " + "y" * 7000], [], 6000) == learn.CUT


def test_the_learn_prompt_asks_for_the_experts_own_subject_only(expert):
    p = learn.prompt_for(expert, "USER| hi")
    assert "only what concerns the subject your instructions give you" in p and learn.CUT in p


def test_a_running_task_is_read_five_minutes_after_its_last_call(expert, monkeypatch, transcript):
    calls = stub(monkeypatch)
    assert learn.AFTER_CALL_S == 300 and learn.after_call_s() == 300
    t0 = time.time()

    def call_at(ts):
        with monkeypatch.context() as m:
            m.setattr(learn.time, "time", lambda: ts)
            learn.record_use(expert, "r1")
        assert learn._load(expert)["sessions"]["s1"]["called_at"] == ts
    call_at(t0 - 250)
    # the transcript was written just now: the task is running, and the idle pass would not read it
    assert learn.run_pass(now=t0 + 49)["sessions"] == 0
    call_at(t0 - 100)  # a second call starts the wait again: both are read in one session
    assert learn.run_pass(now=t0 + 51)["sessions"] == 0
    assert learn.run_pass(now=t0 + 201)["sessions"] == 1
    assert "may still be running" in calls[-1]["prompt"] and "gone quiet" not in calls[-1]["prompt"]
    rec = learn._load(expert)["sessions"]["s1"]
    assert "called_at" not in rec and rec["lines"] == 1
    # the task keeps working with no new call: nothing until it has been idle (the sweep)
    with open(transcript, "a") as f:
        f.write("turn 2\n")
    assert learn.run_pass(now=time.time() + 600)["sessions"] == 0
    _age(transcript)
    assert learn.run_pass()["sessions"] == 1
    assert "gone quiet" in calls[-1]["prompt"] and "USER| turn 2" in calls[-1]["prompt"] and "turn 1" not in calls[-1]["prompt"]


def test_a_failed_read_of_a_running_task_gives_up_the_after_call_wait(expert, monkeypatch, transcript):
    stub(monkeypatch, lambda *a: {"ok": False, "error": "boom", "answer": "", "transcript": []})
    learn.record_use(expert, "r1")
    later = time.time() + 301
    assert learn.run_pass(now=later) == {"sessions": 1, "errors": 1}
    with open(transcript, "a") as f:   # it grew, as a running task does: no retry every pass
        f.write("turn 2\n")
    assert learn.run_pass(now=later + 60)["sessions"] == 0


def test_the_line_cursor_reads_a_turn_that_kept_growing(expert, monkeypatch):
    turn = {"role": "assistant", "segments": [{"kind": "text", "text": "one\ntwo"}]}
    hist = {"turns": [{"role": "user", "text": "go"}, turn]}
    monkeypatch.setattr(learn, "_agent", lambda: type("A", (), {"_history": staticmethod(lambda *a, **k: hist)})())
    session = {"session_id": "s1", "target": "/work", "path": ""}
    md, n = learn.transcript_markdown(session, 0, expert)
    assert md == "USER| go\nAGENT| one\nAGENT| two" and n == 3
    turn["segments"].append({"kind": "text", "text": "three"})   # the same turn, still running
    assert learn.transcript_markdown(session, n, expert) == ("AGENT| three", 4)
    assert learn.transcript_markdown(session, 9, expert)[0].startswith("USER| go")  # shorter than the cursor: whole


def test_a_call_read_without_its_outcome_anchors_the_next_slice(expert, monkeypatch):
    monkeypatch.setattr(store, "find", lambda key: expert)
    monkeypatch.setattr(learn, "TRANSCRIPT_CAP", 2000)
    pad = lambda tag, n: [{"role": "user", "text": f"{tag} {i} " + "x" * 80} for i in range(n)]  # noqa: E731
    hist = {"turns": pad("a", 3) + [_call("me")] + pad("b", 200) + [_call("me")] + pad("c", 3)}
    monkeypatch.setattr(learn, "_agent", lambda: type("A", (), {"_history": staticmethod(lambda *a, **k: hist)})())
    session = {"session_id": "s1", "target": "/work", "path": ""}
    md, _n = learn.transcript_markdown(session, 4, expert)   # the last read ended at the first call's line
    assert md.startswith("USER| b 0 ") and "USER| b 1 " in md  # what came of that call is kept, not cut
    assert md.count(learn.CUT) == 1 and md.count("TOOL| ") == 1 and "USER| c 2 " in md
    far, _n = learn.transcript_markdown(session, 60, expert)  # that call is long past: only the second anchors
    assert far.startswith(learn.CUT) and "USER| b 60 " not in far


def test_a_change_made_while_a_session_writes_is_not_a_hand_edit(expert, monkeypatch):
    seen = []

    def mid_turn(b, p, w):
        append_memory(b, "- [1 · 2026-11-17] Learned.")
        seen.append((sessions.writing(b), sessions.edited_by_hand(b)))   # what the memory tab reads mid-turn
    stub(monkeypatch, mid_turn)
    sessions.run(expert, "task-learn", "learn")       # leaves the mark
    sessions.run(expert, "task-learn", "learn more")  # differs from the mark until this session ends
    assert seen == [(True, False), (True, False)]
    assert (sessions.writing(expert), sessions.edited_by_hand(expert)) == (False, False)


def test_how_to_run_is_stated_once_and_the_rest_is_the_skill(expert, tmp_path, monkeypatch):
    from fused_render.bots import agent_engine
    _canvases(expert, tmp_path, monkeypatch)
    note = store.run_note(expert)
    assert "fused workbench --env prod run --engine remote" in note and "approve" not in note
    # Short, apart from the paths it names (a temp dir here, and a longer one on CI).
    assert len(note.replace(str(tmp_path), "")) < 400
    calls = stub(monkeypatch)
    sessions.ask(expert, "How?")
    fresh, sent = agent_engine._expert_message(expert, "run it", past=[], conv={})
    resumed, _ = agent_engine._expert_message(expert, "run it", past=[], conv={"session_id": "s", "sent": sent})
    assert note in calls[0]["prompt"] and note in fresh and store.where(expert, run=True) in fresh
    assert resumed == f"Today is {sessions.today()}.\n\nMESSAGE: run it"  # nothing restated on a resumed session
    # ... until what it was told stops being true: a folder granted (or taken away) since is said on the next turn.
    more = tmp_path / "granted-later"
    more.mkdir()
    monkeypatch.setattr(store, "read_folders", lambda bot, was=store.read_folders: was(bot) + [str(more)])
    told, sent2 = agent_engine._expert_message(expert, "run it", past=[], conv={"session_id": "s", "sent": sent})
    assert store.where(expert, run=True) in told and str(more) in told and sent2 != sent
    assert str(more) not in agent_engine._expert_message(expert, "x", past=[], conv={"session_id": "s", "sent": sent2})[0]
    # What happens outside the rules is the shipped skill's to say, for both kinds of session.
    skill = open(os.path.join(os.path.dirname(store.__file__), "shipped", "canvas-reference", "skills",
                              store.RUN_SKILL), encoding="utf-8").read()
    assert "Approve / Deny card" in skill and "nobody can approve it" in skill and "run --help" in skill
    assert ".fused/udfs/" in skill and store.udfs_dir(expert) in note  # the scratch folder: the skill, and its path once
    monkeypatch.delenv(__import__("fused_render.fusedcli", fromlist=["x"]).CLI_DIR_ENV)
    assert store.run_note(expert) == "" and "run UDFs" not in agent_engine._expert_message(expert, "hi", past=[], conv={})[0]
    assert store.run_note(expert) == "" and "run UDFs" not in agent_engine._expert_message(expert, "hi", past=[], conv={"session_id": "s"})[0]


def test_a_session_that_ends_after_its_expert_was_deleted_writes_nothing(expert, monkeypatch):
    """Deleting an expert removes its folder; a side session still running then must not make
    `<folder>/.fused/expert/sessions/` again with its final row."""
    def gone(bot, prompt, write):
        bot.deleted = True
        shutil.rmtree(store.expert_dir(bot))
        return {"ok": True, "answer": "late", "transcript": []}
    monkeypatch.setattr(sessions, "harness", gone)
    row = sessions.ask(expert, "q")
    assert row["status"] == "done" and not os.path.exists(store.expert_dir(expert))


def test_a_redirected_memory_file_or_scratch_folder_is_not_written_without_a_card(expert, tmp_path, monkeypatch):
    """`free_write` answers the chat's edits of memory.md and the scratch folder without a card. When either is
    a symlink to somewhere outside the expert's folder, the place it points at is not one of the two."""
    _canvases(expert, tmp_path, monkeypatch)
    d, scratch, mem = store.expert_dir(expert), store.udfs_dir(expert), store.memory_path(expert)
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "notes.md").write_text("mine")
    assert store.free_write(expert, mem) and store.free_write(expert, os.path.join(scratch, "a.py"))
    os.remove(mem)
    os.symlink(str(outside / "notes.md"), mem)
    assert not store.free_write(expert, mem) and not store.free_write(expert, "memory.md", d)
    assert not store.free_write(expert, str(outside / "notes.md"))
    os.rmdir(scratch)
    os.symlink(str(outside / "dir"), scratch)
    assert not store.free_write(expert, os.path.join(scratch, "a.py"))
    assert not store.free_write(expert, str(outside / "dir" / "a.py"))
