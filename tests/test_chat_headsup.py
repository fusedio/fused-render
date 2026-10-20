"""Heads-up notes in the Claude chat (fused_render/claude_agent/headsup.py).

Covers the reply parser, the step counter over `out.jsonl`, the poll
scheduler (generate monkeypatched), the fork argv, the real spawn path against
a stub CLI, the route, the launch.json that `_start` leaves, and the pref.
"""
import importlib.util
import json
import os
import shutil
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
    assert len(note["bullets"]) == 3
    assert len(note["title"]) == 120
    assert all(len(b) == 400 for b in note["bullets"])


def test_parse_one_bullet_is_valid():
    note = headsup.parse_note("learn: x\ntag: Heads up\nexplain:\n**T**\n* Only one.\n")
    assert note["bullets"] == ["Only one."]


def test_parse_five_bullets_keeps_first_three():
    text = "learn: x\nexplain:\n**T**\n" + "".join("* b%d\n" % i for i in range(5))
    assert headsup.parse_note(text)["bullets"] == ["b0", "b1", "b2"]


def test_prompt_asks_for_one_to_three_bullets():
    assert "1 to 3 bullets" in headsup.HEADSUP_PROMPT
    assert "2 to 5" not in headsup.HEADSUP_PROMPT


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
def fake_claude_store(tmp_path, monkeypatch):
    """Never read the real ~/.claude: point Claude Code's store at tmp_path."""
    store = tmp_path / "claude-store"
    store.mkdir()
    monkeypatch.setattr(headsup, "_claude_store_glob",
                        lambda: str(store / "cc-plugin-you-should-know_builtin-*.json"))
    return store


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
    # CI has no `claude` on PATH; never depend on the machine's install.
    # Tests that spawn a stub CLI point FUSED_RENDER_CLAUDE_BIN at it.
    monkeypatch.setattr(
        mod, "_claude_bin",
        lambda: os.environ.get("FUSED_RENDER_CLAUDE_BIN") or "/fake/bin/claude")
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


@pytest.mark.parametrize("cwd", ["gone", ""])
def test_build_fork_argv_none_without_a_real_cwd(agent, tmp_path, cwd):
    # --resume resolves the session from the cwd's project: no tempdir fallback.
    run_dir = _launch_run(agent, tmp_path, cwd=str(tmp_path / cwd) if cwd else "")
    assert headsup.build_fork_argv(agent, run_dir) is None


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


def test_route_400_without_run_id_and_no_pref(home):
    c = TestClient(create_app(start_dir=str(home)))
    assert "chat_headsup" not in c.get("/api/prefs").json()
    assert c.get("/api/claude-sessions/headsup").status_code == 400
    assert c.get("/api/claude-sessions/headsup?run_id=").status_code == 400


def test_route_polls_and_dismiss(home, agent, monkeypatch):
    c = TestClient(create_app(start_dir=str(home)))
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
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: None)
    assert c.get("/api/claude-sessions/headsup?run_id=r1").status_code == 503


# ------------------------------------------------------ persistence

def _offer(runs, gen):
    calls, answer = gen
    answer["note"] = dict(NOTE_FIXTURE)
    _rows(runs, _asst(7))
    return _wait(FakeAgent(runs), 0)


def test_note_survives_a_restart(runs, gen):
    first = _offer(runs, gen)
    assert first["state"] == "note"
    headsup._state.clear()                       # simulated server restart
    again = headsup.poll(FakeAgent(runs), "r1")
    assert again == first


def test_dismissed_note_stays_dismissed_after_restart(runs, gen):
    first = _offer(runs, gen)
    headsup.dismiss("r1", first["id"])
    headsup._state.clear()
    assert headsup.poll(FakeAgent(runs), "r1")["state"] != "note"
    headsup._state.clear()
    assert headsup._run_state("r1", str(runs / "r1"))["dismissed"] == {first["id"]}


def test_corrupt_headsup_json_is_ignored(runs, gen):
    (runs / "r1" / "headsup.json").write_text("{not json")
    assert headsup.poll(FakeAgent(runs), "r1")["state"] == "idle"
    (runs / "r1" / "headsup.json").write_text("[1, 2]")
    headsup._state.clear()
    assert headsup.poll(FakeAgent(runs), "r1")["state"] == "idle"


def test_expiry_after_restart_clears_and_persists(runs, gen):
    _offer(runs, gen)
    headsup._state.clear()
    _rows(runs, _asst(7), RESULT, _asst(1), RESULT, _asst(1))
    assert headsup.poll(FakeAgent(runs), "r1")["state"] == "idle"
    saved = json.loads((runs / "r1" / "headsup.json").read_text())
    assert saved["note"] is None
    headsup._state.clear()
    assert headsup.poll(FakeAgent(runs), "r1")["state"] == "idle"


# ------------------------------------------------------- feedback memory

def _mem_file(runs):
    return runs.parent / "headsup_memory.json"


def _mem(runs):
    return json.loads(_mem_file(runs).read_text())


def _hold_note(runs, learn="Tests are skipped", nid="n1", offered=0):
    """Put a shown note into r1's state (after one poll registered the root)."""
    headsup.poll(FakeAgent(runs), "r1")
    with headsup._lock:
        st = headsup._run_state("r1")
        st["note"] = dict(NOTE_FIXTURE, learn=learn)
        st["note_id"] = nid
        st["offered_turn"] = offered


def _expire_once(runs, nid="n1"):
    _hold_note(runs, nid=nid)
    _rows(runs, _asst(1), RESULT, _asst(1), RESULT, _asst(1))
    assert headsup.poll(FakeAgent(runs), "r1") == {"state": "idle"}


def test_stored_note_lands_in_seen(runs, gen):
    _, answer = gen
    answer["note"] = dict(NOTE_FIXTURE, learn="Costs real money.")
    _rows(runs, _asst(7))
    assert _wait(FakeAgent(runs), 0)["state"] == "note"
    assert _mem(runs)["seen"] == ["Costs real money."]


def test_got_it_lands_in_known_and_dismiss_does_not(runs):
    _hold_note(runs, learn="Alpha thing", nid="a")
    headsup.dismiss("r1", "a", "dismiss")
    assert _mem(runs)["known"] == []
    _hold_note(runs, learn="Beta thing", nid="b")
    headsup.dismiss("r1", "b", "got_it")
    assert _mem(runs)["known"] == ["Beta thing"]
    _hold_note(runs, learn="Gamma thing", nid="c")
    headsup.dismiss("r1", "c", "bogus")          # unknown answer == dismiss
    assert _mem(runs)["known"] == ["Beta thing"]


def test_memory_lists_dedupe_and_cap(runs):
    _hold_note(runs, learn="Same line.", nid="a")
    headsup.dismiss("r1", "a", "got_it")
    _hold_note(runs, learn="same line", nid="b")
    headsup.dismiss("r1", "b", "got_it")
    assert len(_mem(runs)["known"]) == 1
    for i in range(60):
        _hold_note(runs, learn=f"line {i}", nid=f"x{i}")
        headsup.dismiss("r1", f"x{i}", "got_it")
    known = _mem(runs)["known"]
    assert len(known) == 50 and known[-1] == "line 59"


def _generate_prompt(agent, tmp_path, monkeypatch, out=FULL):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("HU_CALLS", str(log))
    monkeypatch.setenv("HU_OUT", out)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", write_stub_cli(
        tmp_path / "bin", _STUB.format(python=sys.executable)))
    run_dir = _launch_run(agent, tmp_path)
    note = headsup.generate(agent, run_dir)
    call = json.loads(log.read_text().splitlines()[0])
    text = json.loads(call["stdin"].strip())["message"]["content"][0]["text"]
    return note, text


def _write_mem(agent, **mem):
    base = {"seen": [], "known": [], "ignored_in_a_row": 0, "skip_checks": 0}
    base.update(mem)
    path = os.path.join(os.path.dirname(agent.RUNS), "headsup_memory.json")
    with open(path, "w") as fh:
        json.dump(base, fh)


@pytest.mark.skipif(os.name == "nt", reason="POSIX spawn path")
def test_prompt_includes_seen_and_known_sections_only_when_non_empty(
        agent, tmp_path, monkeypatch):
    _, text = _generate_prompt(agent, tmp_path, monkeypatch)
    assert text == headsup.HEADSUP_PROMPT
    _write_mem(agent, seen=["Seen one"], known=["Known one"])
    (tmp_path / "calls.jsonl").unlink()
    shutil.rmtree(os.path.join(agent.RUNS, "r1"))
    shutil.rmtree(tmp_path / "bin")
    _, text = _generate_prompt(agent, tmp_path, monkeypatch)
    assert text.startswith(headsup.HEADSUP_PROMPT)
    assert ("\nThese notes were already shown to the user recently. "
            "Do not offer them again:\n- Seen one") in text
    assert ("\nThe user said they already understood these topics. "
            "Do not offer them:\n- Known one") in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX spawn path")
@pytest.mark.parametrize("field", ["seen", "known"])
def test_reply_matching_seen_or_known_is_dropped(agent, tmp_path, monkeypatch, field):
    _write_mem(agent, **{field: ["THE build quietly skips the tests!"]})
    note, _ = _generate_prompt(agent, tmp_path, monkeypatch)
    assert note is None


@pytest.mark.parametrize("n,skip", [(1, 0), (2, 0), (3, 1), (4, 2), (5, 4),
                                    (6, 8), (7, 16), (8, 16), (9, 16)])
def test_ignored_expiries_back_off(runs, n, skip):
    for i in range(n):
        _expire_once(runs, nid=f"n{i}")
        # next offer is a fresh note in a fresh turn count
        _rows(runs)
        headsup._steps_cache.clear()
    mem = _mem(runs)
    assert mem["ignored_in_a_row"] == n and mem["skip_checks"] == skip


def test_dismissed_then_expired_note_is_not_ignored(runs):
    _hold_note(runs)
    headsup.dismiss("r1", "n1", "dismiss")
    _rows(runs, _asst(1), RESULT, _asst(1), RESULT, _asst(1))
    headsup.poll(FakeAgent(runs), "r1")
    assert _mem(runs)["ignored_in_a_row"] == 0
    # even if a dismissed id is still held as the note, expiry must not count
    _hold_note(runs, nid="n2")
    with headsup._lock:
        headsup._run_state("r1")["dismissed"].add("n2")
    headsup.poll(FakeAgent(runs), "r1")
    assert _mem(runs)["ignored_in_a_row"] == 0


def test_any_answer_resets_both_counters(runs):
    for i in range(3):
        _expire_once(runs, nid=f"n{i}")
        headsup._steps_cache.clear()
    assert _mem(runs)["skip_checks"] == 1
    for answer in ("got_it", "dismiss", "chat"):
        _mem_file(runs).write_text(json.dumps(
            {"seen": [], "known": [], "ignored_in_a_row": 5, "skip_checks": 4}))
        _hold_note(runs, nid=answer)
        headsup.dismiss("r1", answer, answer)
        mem = _mem(runs)
        assert mem["ignored_in_a_row"] == 0 and mem["skip_checks"] == 0


def test_skip_checks_skips_a_due_check_and_decrements(runs, gen):
    calls, _ = gen
    agent = FakeAgent(runs)
    headsup.poll(agent, "r1")
    _mem_file(runs).write_text(json.dumps(
        {"seen": [], "known": [], "ignored_in_a_row": 3, "skip_checks": 1}))
    _rows(runs, _asst(6))
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    assert calls == []
    assert _mem(runs)["skip_checks"] == 0
    # the skipped bucket is not retried; the next bucket checks again
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    assert calls == []
    _rows(runs, _asst(12))
    assert headsup.poll(agent, "r1")["state"] == "checking"
    _wait(agent, None)
    assert len(calls) == 1


def test_corrupt_memory_file_is_empty(runs, gen):
    _mem_file(runs).write_text("{not json")
    assert headsup._mem_read(str(runs)) == {
        "seen": [], "known": [], "ignored_in_a_row": 0, "skip_checks": 0}
    _mem_file(runs).write_text("[1]")
    assert headsup._mem_read(str(runs))["seen"] == []
    # and it recovers on the next write
    _, answer = gen
    answer["note"] = dict(NOTE_FIXTURE, learn="After corruption")
    _rows(runs, _asst(7))
    assert _wait(FakeAgent(runs), 0)["state"] == "note"
    assert _mem(runs)["seen"] == ["After corruption"]


def test_route_passes_answer_through(home, monkeypatch):
    seen = []
    monkeypatch.setattr(headsup, "dismiss", lambda *a: seen.append(a))
    c = TestClient(create_app(start_dir=str(home)))
    c.post("/api/claude-sessions/headsup/dismiss",
           json={"run_id": "r1", "id": "abc", "answer": "got_it"}, headers=FUSED)
    c.post("/api/claude-sessions/headsup/dismiss",
           json={"run_id": "r1", "id": "abc"}, headers=FUSED)
    assert seen == [("r1", "abc", "got_it"), ("r1", "abc", "dismiss")]


@pytest.mark.skipif(os.name == "nt", reason="POSIX spawn path")
def test_claude_code_known_list_feeds_prompt_and_dedupe(
        agent, tmp_path, monkeypatch, fake_claude_store):
    (fake_claude_store / "cc-plugin-you-should-know_builtin-abc.json").write_text(
        json.dumps({"seen": [], "known": ["The build quietly skips the tests"],
                    "typedPastInARow": 9, "looksToSkip": 9}))
    note, text = _generate_prompt(agent, tmp_path, monkeypatch)
    assert note is None
    assert "- The build quietly skips the tests" in text
    # read-only: nothing of ours was written into Claude's file
    assert "skip_checks" not in (fake_claude_store / "cc-plugin-you-should-know_builtin-abc.json").read_text()


def test_claude_lists_union_and_corrupt_ignored(fake_claude_store):
    assert headsup._claude_lists() == ([], [])
    (fake_claude_store / "cc-plugin-you-should-know_builtin-1.json").write_text(
        json.dumps({"seen": ["a", 5], "known": ["k1"]}))
    (fake_claude_store / "cc-plugin-you-should-know_builtin-2.json").write_text("{bad")
    (fake_claude_store / "cc-plugin-you-should-know_builtin-3.json").write_text(
        json.dumps({"seen": "oops", "known": ["k2"]}))
    assert headsup._claude_lists() == (["a"], ["k1", "k2"])


# ------------------------------------------------- checked turns persist

def test_checked_turn_is_not_rechecked_after_restart(runs, gen):
    calls, _ = gen
    agent = FakeAgent(runs)
    _rows(runs, _asst(7), RESULT)
    assert headsup.poll(agent, "r1")["state"] == "checking"
    _wait(agent, None)
    assert len(calls) == 1
    headsup._state.clear()                       # simulated restart / eviction
    assert headsup.poll(agent, "r1") == {"state": "idle"}
    assert len(calls) == 1


def test_checked_set_is_saved_validated_and_capped(runs):
    path = str(runs / "r1" / "headsup.json")
    st = headsup._run_state("r1", str(runs / "r1"))
    st["checked"] = {(t, 1) for t in range(300)}
    headsup._save(st)
    saved = json.loads((runs / "r1" / "headsup.json").read_text())["checked"]
    assert len(saved) == 200
    assert sorted(saved)[0] == [100, 1]
    (runs / "r1" / "headsup.json").write_text(json.dumps(
        {"checked": [[1, 2], [True, 1], ["a", 1], [1], [3, 4, 5], "x", [2, -1], [1.5, 1]]}))
    assert headsup._load(path)["checked"] == {(1, 2), (2, -1)}


# --------------------------------------------- timeout kills the process tree

@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_timeout_kills_the_process_group(agent, tmp_path, monkeypatch):
    import subprocess as sp
    killed = []

    class FakePopen:
        pid = 4242
        returncode = None

        def __init__(self, *a, **kw):
            self.calls = 0

        def communicate(self, input=None, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise sp.TimeoutExpired("x", timeout)
            return "", None

        def kill(self):
            killed.append("kill")

    monkeypatch.setattr(headsup.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(headsup.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    run_dir = _launch_run(agent, tmp_path)
    assert headsup.generate(agent, run_dir) is None
    import signal
    assert killed == [(4242, signal.SIGKILL)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_timeout_kills_a_real_grandchild(agent, tmp_path, monkeypatch):
    pidfile = tmp_path / "grandchild.pid"
    stub = f"""#!{sys.executable}
import subprocess, sys, time
sys.stdin.read()
gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(pidfile)!r}, "w").write(str(gc.pid))
time.sleep(60)
"""
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", write_stub_cli(tmp_path / "bin", stub))
    monkeypatch.setattr(headsup, "HEADSUP_TIMEOUT", 3.0)
    run_dir = _launch_run(agent, tmp_path)
    gc = None
    try:
        assert headsup.generate(agent, run_dir) is None
        gc = int(pidfile.read_text())
        for _ in range(100):
            try:
                os.kill(gc, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("grandchild survived the timeout")
    finally:
        if gc is None and pidfile.exists():
            gc = int(pidfile.read_text())
        if gc:
            try:
                os.kill(gc, 9)
            except OSError:
                pass


def test_spawn_argv_posix_shape_is_the_interpreter_setsid_wrapper():
    if os.name == "nt":
        pytest.skip("POSIX shape")
    argv, kw = headsup._spawn_argv(["/x/claude", "-p"], "/work")
    assert kw is None
    assert argv[:3] == [sys.executable, "-B", "-I"]
    assert argv[3] == "-c" and "setsid" in argv[4]
    assert argv[5:] == ["/work", "/x/claude", "-p"]


def test_fork_follows_the_parents_recorded_strict_mcp(agent, tmp_path):
    """The prompt-cache prefix includes the MCP tool roster, so a fork must be
    as lean as the session it forks — and no leaner."""
    fork = headsup.build_fork_argv(agent, _launch_run(agent, tmp_path, strict_mcp=True))
    try:
        assert "--strict-mcp-config" in fork.argv
    finally:
        import shutil
        shutil.rmtree(fork.dir, ignore_errors=True)
    # An older host's launch.json has no key at all: not strict.
    fork = headsup.build_fork_argv(agent, _launch_run_again(agent, tmp_path))
    try:
        assert "--strict-mcp-config" not in fork.argv
    finally:
        import shutil
        shutil.rmtree(fork.dir, ignore_errors=True)


def _launch_run_again(agent, tmp_path):
    import shutil
    shutil.rmtree(os.path.join(agent.RUNS, "r1"))
    return _launch_run(agent, tmp_path)
