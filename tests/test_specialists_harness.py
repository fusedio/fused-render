"""Side sessions (check / reviewer / nightly) with `_spawn` stubbed: argv fence, rows, timeouts, restart (X)."""
from _specialists_conftest import *  # noqa: F401,F403
import json
import os
import sys
import textwrap
import threading
import time

from fused_render.specialists import harness


def test_argv_check_is_read_only_and_loads_the_project(app_home, monkeypatch, tmp_path):
    s = make_specialist()
    calls = stub_side(monkeypatch, answer="Looks right.")
    task = tmp_path / "task"
    task.mkdir()
    row = harness.run_side(s, "check", "review this", trigger={"session_id": "t1"}, read_dirs=[str(task)])
    assert row["status"] == "done" and row["answer"] == "Looks right." and row["kind"] == "check"
    argv = calls[0]["argv"]
    assert "--setting-sources=project" in argv and "--setting-sources=" not in argv
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "--permission-prompt-tool" not in argv and "--no-session-persistence" in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--effort") + 1] == "low"
    rules = argv[argv.index("--allowedTools") + 1:]
    assert f"Read(/{task}/**)" in rules and not any(r.startswith("Edit(") for r in rules)
    assert calls[0]["cwd"] == s.home and calls[0]["prompt"] == "review this"
    assert calls[0]["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert calls[0]["timeout_s"] == 600.0


def test_argv_nightly_writes_only_its_set_and_reads_services(app_home, monkeypatch):
    s = make_specialist()
    from fused_render.specialists import store

    s = store.update(s.id, model="opus", effort="high")
    calls = stub_side(monkeypatch)
    monkeypatch.setattr(harness, "_services_servers",
                        lambda spec, kind: {"specialist_services": {"command": "py", "args": []}})
    harness.run_side(s, "nightly", "go", trigger={}, write_globs=["memory.md", "knowledge/**"], services=True)
    argv = calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write"
    rules = argv[argv.index("--allowedTools") + 1:]
    assert f"Edit(/{s.home}/memory.md)" in rules and f"Edit(/{s.home}/knowledge/**)" in rules
    assert "mcp__specialist_services" in rules
    assert "specialist_services" in json.loads(argv[argv.index("--mcp-config") + 1])["mcpServers"]
    assert argv[argv.index("--model") + 1] == "opus" and argv[argv.index("--effort") + 1] == "high"
    assert calls[0]["timeout_s"] == 1800.0


def test_glob_chars_folder_not_granted():
    assert harness.read_rules(["/a/b[1]"]) == []
    assert harness.read_rules(["/a/b"]) == ["Read(//a/b/**)"]


def test_rows_and_error_row(app_home, monkeypatch):
    s = make_specialist()
    stub_side(monkeypatch, lambda call: {"ok": False, "error": "boom", "answer": "", "transcript": []})
    row = harness.run_side(s, "reviewer", "x", trigger={"receipt": "r-1"})
    assert row["status"] == "error" and row["error"] == "boom"
    listed = harness.rows(s.id)
    assert [r["id"] for r in listed] == [row["id"]] and "transcript" not in listed[0]
    assert harness.row(s.id, row["id"])["trigger"] == {"receipt": "r-1"}
    assert harness.row(s.id, "../../etc") is None


def test_missing_claude_is_an_error_row(app_home):
    s = make_specialist()
    row = harness.run_side(s, "check", "x", trigger={})
    assert row["status"] == "error" and row["error"]


def test_interrupted_row_after_restart(app_home):
    s = make_specialist()
    harness.save_row(s.id, {"id": "0000000000001-abcdef", "kind": "nightly", "status": "running",
                            "started": 1.0, "trigger": {}})
    assert harness.row(s.id, "0000000000001-abcdef")["status"] == "interrupted"
    assert harness.rows(s.id)[0]["status"] == "interrupted"


def test_running_row_reads_running_while_live(app_home, monkeypatch):
    s = make_specialist()
    seen = {}

    def fn(call):
        seen["row"] = harness.row(s.id, call["row_id"])
        return "ok"
    stub_side(monkeypatch, fn)
    harness.run_side(s, "check", "x", trigger={})
    assert seen["row"]["status"] == "running"


def test_extract_block():
    t = "noise <<<SUMMARY\nOutcome: solved\nSUMMARY>>> more <<<NOTE\nkind: gap\nNOTE>>>"
    assert harness.extract_block(t, "SUMMARY") == "Outcome: solved"
    assert harness.extract_block(t, "NOTE") == "kind: gap"
    assert harness.extract_block(t, "REVIEW") is None and harness.extract_block("", "X") is None
    assert harness.extract_block("<<<A\nfirst\nA>>> <<<A\nsecond\nA>>>", "A") == "second"


def test_timeout_and_shutdown_with_a_real_process(app_home, monkeypatch, tmp_path):
    """The real `_spawn` against a fake `claude`: a hung one is killed at the timeout; shutdown closes."""
    s = make_specialist()
    fake = tmp_path / "claude"
    fake.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import sys, json, time
        prompt = sys.stdin.read()
        if "hang" in prompt:
            time.sleep(30)
        print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "hi"}}]}}}}))
        print(json.dumps({{"type": "result", "subtype": "success", "result": "answer: " + prompt, "total_cost_usd": 0.5}}))
        """))
    fake.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_SPECIALIST_CLAUDE", str(fake))
    row = harness.run_side(s, "check", "ping", trigger={})
    assert row["status"] == "done" and row["answer"] == "answer: ping" and row["cost_usd"] == 0.5
    assert {"role": "specialist", "text": "hi"} in harness.row(s.id, row["id"])["transcript"]
    t0 = time.time()
    row = harness.run_side(s, "check", "hang", trigger={}, timeout_s=1)
    assert row["status"] == "error" and "no answer after 1 s" in row["error"] and time.time() - t0 < 10
    try:
        harness.shutdown(0.5)
        row = harness.run_side(s, "check", "ping", trigger={})
        assert row["status"] == "error" and row["error"] == harness.CLOSED
    finally:
        harness.reopen()


def test_parallel_cap(app_home, monkeypatch):
    s = make_specialist()
    gate = threading.Event()
    live, peak = [0], [0]
    lock = threading.Lock()

    def fn(call):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        gate.wait(5)
        with lock:
            live[0] -= 1
        return "ok"
    stub_side(monkeypatch, fn)
    ts = [threading.Thread(target=harness.run_side, args=(s, "check", "x"), kwargs={"trigger": {}}) for _ in range(5)]
    for t in ts:
        t.start()
    time.sleep(0.5)
    assert peak[0] == harness.PARALLEL
    gate.set()
    for t in ts:
        t.join(10)
    assert len(harness.rows(s.id)) == 5


def test_slot_wait_fits_inside_a_check():
    assert harness.SLOT_WAIT_S == 30.0


def test_rows_never_recreate_a_deleted_home(app_home):
    from fused_render.specialists import chat, store

    s = make_specialist()
    store.delete(s.id)
    harness.save_row(s.id, {"kind": "check", "status": "done", "started": 1.0, "trigger": {}})
    chat.clear(s.id)
    assert not os.path.exists(s.home)


def test_a_second_app_start_in_one_process_reopens_the_harness(app_home, monkeypatch, tmp_path):
    """`shutdown()` is one-way. An app's shutdown handlers followed by another app start in the same process (the
    test suite, a restarted lifespan) used to leave every side session answering CLOSED for good."""
    import asyncio

    from fused_render.server import create_app
    from fused_render.specialists import scheduler

    monkeypatch.setattr(scheduler, "start", lambda: None)  # no thread here
    stub_side(monkeypatch, answer="fine")
    s = make_specialist()
    app = create_app(start_dir=str(tmp_path))
    by_name = {f.__name__: f for f in app.state.startup_handlers + app.state.shutdown_handlers}
    asyncio.run(by_name["_shutdown_specialists"]())
    assert harness.closed()
    assert harness.run_side(s, "check", "ping", trigger={})["error"] == harness.CLOSED
    asyncio.run(by_name["_startup_specialists"]())
    assert not harness.closed()
    row = harness.run_side(s, "check", "ping", trigger={})
    assert row["status"] == "done" and row["answer"] == "fine"


def test_switching_the_flag_on_reopens_a_closed_harness(app_home, monkeypatch):
    from fused_render.shell import prefs
    from fused_render.specialists import scheduler

    monkeypatch.setattr(scheduler, "start", lambda: None)
    harness.shutdown(0.1)
    try:
        prefs._specialists_switched(True)
        assert not harness.closed()
    finally:
        harness.reopen()
