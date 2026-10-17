"""Heads-up notes in the Claude chat (fused_render/claude_agent/headsup.py).

Covers the reply parser, the step counter over `out.jsonl`, the poll
scheduler (generate monkeypatched), the fork argv, the real spawn path against
a stub CLI, the route, the launch.json that `_start` leaves, and the pref.
"""
import importlib.util
import json
import os
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from fused_render.claude_agent import headsup
from fused_render.server import create_app
from fused_render.server.routers import tasks as tasks_mod

from _claude_stub_cli import write_stub_cli

FUSED = {"X-Fused": "1"}
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

FULL = """learn: The build quietly skips the tests.
tag: Heads up
explain:
**Tests are skipped**
* The agent set `SKIP_TESTS=1`, so **nothing** was verified.
* Run the suite yourself before merging.
"""


# ------------------------------------------------------------ parse_note

@pytest.mark.parametrize("text", ["learn: none", "Learn: None.", "learn: NONE!",
                                  "", None])
def test_parse_none_variants(text):
    assert headsup.parse_note(text) is None


def test_parse_full_note():
    note = headsup.parse_note(FULL)
    assert note == {
        "learn": "The build quietly skips the tests.",
        "tag": "Heads up",
        "title": "Tests are skipped",
        "bullets": ["The agent set `SKIP_TESTS=1`, so **nothing** was verified.",
                    "Run the suite yourself before merging."],
    }


def test_parse_default_tag_and_missing_pieces():
    no_tag = FULL.replace("tag: Heads up\n", "")
    assert headsup.parse_note(no_tag)["tag"] == "You should know"
    assert headsup.parse_note(FULL.replace("**Tests are skipped**\n", "")) is None
    assert headsup.parse_note(FULL.replace("learn: The build quietly skips the tests.\n", "")) is None
    assert headsup.parse_note("learn: x\ntag: Heads up\nexplain:\n**T**\n") is None


def test_parse_caps():
    text = ("learn: x\nexplain:\n**" + "t" * 300 + "**\n"
            + "".join("* " + "b" * 900 + "\n" for _ in range(9)))
    note = headsup.parse_note(text)
    assert len(note["bullets"]) == 6
    assert len(note["title"]) == 120
    assert all(len(b) == 400 for b in note["bullets"])


# ------------------------------------------------------------ main_steps

def _asst(n, **extra):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t%d" % i, "name": "Bash"} for i in range(n)]},
        **extra}


RESULT = {"type": "result", "subtype": "success"}


def _out(tmp_path, *rows):
    d = tmp_path / "run"
    d.mkdir(exist_ok=True)
    (d / "out.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows)
                                 + "{half-written")
    return str(d)


def test_main_steps_no_file(tmp_path):
    assert headsup.main_steps(str(tmp_path / "nope")) == (0, 0, True)


def test_main_steps_open_turn_ignores_subagents(tmp_path):
    d = _out(tmp_path, _asst(2), _asst(5, parent_tool_use_id="sub"),
             {"type": "rate_limit_event"}, _asst(3))
    assert headsup.main_steps(d) == (0, 5, True)


def test_main_steps_closed_and_next_open_turn(tmp_path):
    d = _out(tmp_path, _asst(4), _asst(3), RESULT)
    assert headsup.main_steps(d) == (0, 7, False)
    d = _out(tmp_path, _asst(4), RESULT, _asst(2))
    assert headsup.main_steps(d) == (1, 2, True)


def test_main_steps_incremental_appends(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    out = d / "out.jsonl"
    out.write_text(json.dumps(_asst(2)) + "\n")
    assert headsup.main_steps(str(d)) == (0, 2, True)
    # A half-written trailing line stays unconsumed until it completes.
    with open(out, "a") as fh:
        fh.write(json.dumps(_asst(3))[:20])
    assert headsup.main_steps(str(d)) == (0, 2, True)
    with open(out, "a") as fh:
        fh.write(json.dumps(_asst(3))[20:] + "\n")
    assert headsup.main_steps(str(d)) == (0, 5, True)
    with open(out, "a") as fh:
        fh.write(json.dumps(RESULT) + "\n")
    assert headsup.main_steps(str(d)) == (0, 5, False)
    with open(out, "a") as fh:
        fh.write(json.dumps(_asst(1)) + "\n")
    assert headsup.main_steps(str(d)) == (1, 1, True)


def test_main_steps_incremental_reads_only_new_bytes(tmp_path, monkeypatch):
    d = tmp_path / "run"
    d.mkdir()
    out = d / "out.jsonl"
    out.write_text(json.dumps(_asst(2)) + "\n")
    headsup.main_steps(str(d))
    with open(out, "a") as fh:
        fh.write(json.dumps(_asst(1)) + "\n")
    seen = []
    real_loads = json.loads
    monkeypatch.setattr(headsup.json, "loads",
                        lambda line, *a, **k: (seen.append(line), real_loads(line))[1])
    assert headsup.main_steps(str(d)) == (0, 3, True)
    assert len(seen) == 1


def test_main_steps_truncation_resets(tmp_path):
    d = tmp_path / "run"
    d.mkdir()
    out = d / "out.jsonl"
    out.write_text(json.dumps(_asst(4)) + "\n" + json.dumps(RESULT) + "\n")
    assert headsup.main_steps(str(d)) == (0, 4, False)
    out.write_text(json.dumps(_asst(1)) + "\n")
    assert headsup.main_steps(str(d)) == (0, 1, True)
    # Rewritten at the same size with different content also resets.
    out.write_text(json.dumps(_asst(2)) + "\n")
    assert headsup.main_steps(str(d)) == (0, 2, True)


# ------------------------------------------------------------------ poll

class FakeAgent:
    def __init__(self, runs):
        self.RUNS = str(runs)

    @staticmethod
    def _bad_id(value):
        return not value or value.startswith(".") or any(c in value for c in "/\\:")

    @staticmethod
    def _not_a_turn_row(row):
        return row.get("type") in ("rate_limit_event", "stream_event")


@pytest.fixture(autouse=True)
def fresh_state():
    headsup._state.clear()
    headsup._steps_cache.clear()
    yield
    headsup._state.clear()
    headsup._steps_cache.clear()


@pytest.fixture
def runs(tmp_path):
    root = tmp_path / "runs"
    (root / "r1").mkdir(parents=True)
    return root


def _rows(runs, *rows):
    (runs / "r1" / "out.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows))


def _wait(agent, expect, rid="r1"):
    for _ in range(200):
        got = headsup.poll(agent, rid)
        if got["state"] != "checking":
            return got
        time.sleep(0.01)
    raise AssertionError("still checking")


@pytest.fixture
def gen(monkeypatch):
    calls = []
    answer = {"note": None}

    def fake(agent, run_dir):
        calls.append(run_dir)
        return answer["note"]

    monkeypatch.setattr(headsup, "generate", fake)
    return calls, answer


def test_poll_unknown_run_and_bad_id(runs):
    agent = FakeAgent(runs)
    assert headsup.poll(agent, "../x") == {"state": "idle"}
    assert headsup.poll(agent, "ghost") == {"state": "idle"}


def test_poll_schedule_buckets(runs, gen):
    calls, _ = gen
    agent = FakeAgent(runs)
    _rows(runs, _asst(5))
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    _rows(runs, _asst(6))
    assert headsup.poll(agent, "r1") == {"state": "checking"}
    assert _wait(agent, None) == {"state": "idle"}
    assert len(calls) == 1
    # same bucket again: no new check
    _rows(runs, _asst(8))
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    assert len(calls) == 1
    # next bucket
    _rows(runs, _asst(12))
    assert headsup.poll(agent, "r1")["state"] == "checking"
    _wait(agent, None)
    assert len(calls) == 2


def test_poll_note_blocks_further_checks_until_dismissed(runs, gen):
    calls, answer = gen
    answer["note"] = {"learn": "x", "tag": "Heads up", "title": "T", "bullets": ["b"]}
    agent = FakeAgent(runs)
    _rows(runs, _asst(6))
    headsup.poll(agent, "r1")
    got = _wait(agent, None)
    assert got["state"] == "note" and got["note"]["title"] == "T"
    nid = got["id"]
    # Polling again keeps returning the card, and no second check starts.
    _rows(runs, _asst(12))
    assert headsup.poll(agent, "r1")["id"] == nid
    assert len(calls) == 1
    headsup.dismiss("r1", nid)
    # Dismissed, but a note was already offered this turn: no more checks.
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    assert len(calls) == 1
    # A new turn can check again.
    _rows(runs, _asst(6), RESULT, _asst(6))
    assert headsup.poll(agent, "r1")["state"] == "checking"
    _wait(agent, None)
    assert len(calls) == 2


def test_poll_final_check_at_turn_end(runs, gen):
    calls, _ = gen
    agent = FakeAgent(runs)
    # Turn closes with 7 steps and no check ever ran (the page was away).
    _rows(runs, _asst(7), RESULT)
    assert headsup.poll(agent, "r1")["state"] == "checking"
    _wait(agent, None)
    assert len(calls) == 1
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    assert len(calls) == 1


def test_poll_no_final_check_when_a_check_already_ran(runs, gen):
    calls, _ = gen
    agent = FakeAgent(runs)
    _rows(runs, _asst(6))
    headsup.poll(agent, "r1")
    _wait(agent, None)
    _rows(runs, _asst(6), RESULT)
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    assert len(calls) == 1


def test_poll_never_raises(runs, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(headsup, "_poll", boom)
    assert headsup.poll(FakeAgent(runs), "r1") == {"state": "idle"}


def test_poll_note_expires_two_turns_later(runs, gen):
    calls, answer = gen
    agent = FakeAgent(runs)
    answer["note"] = dict(NOTE_FIXTURE)
    _rows(runs, _asst(7))
    assert _wait(agent, 0)["state"] == "note"
    _rows(runs, _asst(7), RESULT, _asst(1))          # turn 1: still offered
    assert headsup.poll(agent, "r1")["state"] == "note"
    _rows(runs, _asst(7), RESULT, _asst(1), RESULT, _asst(1))  # turn 2: expired
    assert headsup.poll(agent, "r1")["state"] == "idle"
    assert headsup._state["r1"]["note"] is None


def test_worker_discards_note_when_a_new_turn_began(runs, monkeypatch):
    agent = FakeAgent(runs)
    _rows(runs, _asst(7))

    def fake(agent_, run_dir):
        _rows(runs, _asst(7), RESULT, _asst(1))      # user moved on mid-check
        return dict(NOTE_FIXTURE)

    monkeypatch.setattr(headsup, "generate", fake)
    _wait(agent, 0)
    assert headsup._state["r1"]["note"] is None


NOTE_FIXTURE = {"learn": "x", "tag": "Heads up", "title": "T", "bullets": ["b"]}


def test_state_is_lru_bounded():
    with headsup._lock:
        for i in range(80):
            headsup._run_state("run%d" % i)
    assert len(headsup._state) == 64
    assert "run0" not in headsup._state and "run79" in headsup._state


# -------------------------------------------------------- real agent module

@pytest.fixture
def agent(tmp_path, monkeypatch):
    path = os.path.join("fused_render", "claude_agent", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_headsup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr(mod, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: mod)
    return mod


def _launch_run(agent, tmp_path, **over):
    run_dir = os.path.join(agent.RUNS, "r1")
    os.makedirs(run_dir)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    launch = {"file": str(work), "cwd": str(work), "pane": False,
              "cli_mode": "acceptEdits", "session_id": "", "model": "opus",
              "effort": "high", "extra_read_dirs": [], "new_session_id": SID,
              "backend": "claude"}
    launch.update(over)
    with open(os.path.join(run_dir, "launch.json"), "w") as fh:
        json.dump(launch, fh)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"session_id": SID}, fh)
    return run_dir


def test_build_fork_argv(agent, tmp_path):
    run_dir = _launch_run(agent, tmp_path)
    fork = headsup.build_fork_argv(agent, run_dir)
    try:
        argv = fork.argv
        assert argv[argv.index("--resume") + 1] == SID
        for flag in ("--fork-session", "--no-session-persistence"):
            assert flag in argv
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert argv[argv.index("--max-turns") + 1] == "1"
        assert "--tools" not in argv and "--disallowedTools" not in argv
        assert "--session-id" not in argv
        assert "--model" in argv and "--effort" in argv
        mcp = argv[argv.index("--mcp-config") + 1]
        assert not mcp.startswith(run_dir)
        assert os.path.dirname(mcp) == fork.dir
        assert fork.cwd == str(tmp_path / "work")
    finally:
        import shutil
        shutil.rmtree(fork.dir, ignore_errors=True)


def test_build_fork_argv_disables_hooks(agent, tmp_path):
    fork = headsup.build_fork_argv(agent, _launch_run(agent, tmp_path))
    try:
        argv = fork.argv
        assert argv[argv.index("--settings") + 1] == '{"disableAllHooks": true}'
    finally:
        import shutil
        shutil.rmtree(fork.dir, ignore_errors=True)


def test_build_fork_argv_prefers_host_json_model_and_effort(agent, tmp_path):
    run_dir = _launch_run(agent, tmp_path)
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        json.dump({"model": "sonnet", "effort": "low"}, fh)
    fork = headsup.build_fork_argv(agent, run_dir)
    try:
        argv = fork.argv
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[argv.index("--effort") + 1] == "low"
    finally:
        import shutil
        shutil.rmtree(fork.dir, ignore_errors=True)


@pytest.mark.parametrize("host", ["{not json", '{"model": 5, "effort": ""}', "[]"])
def test_build_fork_argv_bad_host_json_uses_launch(agent, tmp_path, host):
    run_dir = _launch_run(agent, tmp_path)
    with open(os.path.join(run_dir, "host.json"), "w") as fh:
        fh.write(host)
    fork = headsup.build_fork_argv(agent, run_dir)
    try:
        argv = fork.argv
        assert argv[argv.index("--model") + 1] == "opus"
        assert argv[argv.index("--effort") + 1] == "high"
    finally:
        import shutil
        shutil.rmtree(fork.dir, ignore_errors=True)


def test_build_fork_argv_none_cases(agent, tmp_path):
    assert headsup.build_fork_argv(agent, str(tmp_path / "missing")) is None
    run_dir = _launch_run(agent, tmp_path, backend="copilot")
    assert headsup.build_fork_argv(agent, run_dir) is None


def test_build_fork_argv_cwd_falls_back(agent, tmp_path):
    run_dir = _launch_run(agent, tmp_path, cwd=str(tmp_path / "gone"))
    fork = headsup.build_fork_argv(agent, run_dir)
    import shutil, tempfile
    shutil.rmtree(fork.dir, ignore_errors=True)
    assert fork.cwd == tempfile.gettempdir()


_STUB = """#!{python}
import json, os, sys
data = sys.stdin.read()
with open(os.environ["HU_CALLS"], "a") as fh:
    fh.write(json.dumps(dict(argv=sys.argv[1:], cwd=os.getcwd(), stdin=data)) + "\\n")
sys.stdout.write(json.dumps({{"type": "result", "subtype": "success",
                              "is_error": False,
                              "result": os.environ["HU_OUT"]}}) + "\\n")
"""


@pytest.mark.skipif(os.name == "nt", reason="POSIX spawn path")
def test_generate_spawns_in_cwd_and_parses(agent, tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("HU_CALLS", str(log))
    monkeypatch.setenv("HU_OUT", FULL)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", write_stub_cli(
        tmp_path / "bin", _STUB.format(python=sys.executable)))
    run_dir = _launch_run(agent, tmp_path)
    note = headsup.generate(agent, run_dir)
    assert note["title"] == "Tests are skipped"
    call = json.loads(log.read_text().splitlines()[0])
    assert os.path.realpath(call["cwd"]) == os.path.realpath(tmp_path / "work")
    assert "--fork-session" in call["argv"]
    msg = json.loads(call["stdin"].strip())
    assert "learn: none" in msg["message"]["content"][0]["text"]
    # the fork's private dir is cleaned up
    mcp = call["argv"][call["argv"].index("--mcp-config") + 1]
    assert not os.path.exists(os.path.dirname(mcp))


def test_generate_failed_run_is_none(agent, tmp_path, monkeypatch):
    stub = "#!{python}\nimport sys\nsys.exit(3)\n".format(python=sys.executable)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", write_stub_cli(tmp_path / "bin", stub))
    run_dir = _launch_run(agent, tmp_path)
    assert headsup.generate(agent, run_dir) is None


# ---------------------------------------------------------------- routes

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_ENGINE", raising=False)
    return tmp_path


def test_route_disabled_by_default_then_400(home):
    c = TestClient(create_app(start_dir=str(home)))
    assert c.get("/api/claude-sessions/headsup?run_id=r1").json() == {"state": "disabled"}
    assert c.put("/api/prefs", json={"chat_headsup_enabled": True},
                 headers=FUSED).status_code == 200
    assert c.get("/api/claude-sessions/headsup").status_code == 400
    assert c.get("/api/claude-sessions/headsup?run_id=").status_code == 400


def test_route_enabled_polls_and_dismiss(home, agent, monkeypatch):
    c = TestClient(create_app(start_dir=str(home)))
    c.put("/api/prefs", json={"chat_headsup_enabled": True}, headers=FUSED)
    assert c.get("/api/claude-sessions/headsup?run_id=ghost").json() == {"state": "idle"}
    with headsup._lock:
        st = headsup._run_state("r1")
        st["note"] = {"learn": "x", "tag": "Heads up", "title": "T", "bullets": ["b"]}
        st["note_id"] = "abc"
    os.makedirs(os.path.join(agent.RUNS, "r1"))
    got = c.get("/api/claude-sessions/headsup?run_id=r1").json()
    assert got["state"] == "note" and got["id"] == "abc"
    assert c.post("/api/claude-sessions/headsup/dismiss",
                  json={"run_id": "r1", "id": "abc"}, headers=FUSED).json() == {"ok": True}
    assert c.get("/api/claude-sessions/headsup?run_id=r1").json() == {"state": "idle"}


def test_route_503_without_agent(home, monkeypatch):
    c = TestClient(create_app(start_dir=str(home)))
    c.put("/api/prefs", json={"chat_headsup_enabled": True}, headers=FUSED)
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: None)
    assert c.get("/api/claude-sessions/headsup?run_id=r1").status_code == 503


# ----------------------------------------------------------------- prefs

def test_pref_round_trip(home):
    c = TestClient(create_app(start_dir=str(home)))
    assert c.get("/api/prefs").json()["chat_headsup"] == {"enabled": False}
    assert c.put("/api/prefs", json={"chat_headsup_enabled": "yes"},
                 headers=FUSED).status_code == 400
    r = c.put("/api/prefs", json={"chat_headsup_enabled": True}, headers=FUSED)
    assert r.json()["chat_headsup"] == {"enabled": True}
    assert c.get("/api/prefs").json()["chat_headsup"] == {"enabled": True}
    c.put("/api/prefs", json={"chat_headsup_enabled": False}, headers=FUSED)
    assert c.get("/api/prefs").json()["chat_headsup"] == {"enabled": False}
