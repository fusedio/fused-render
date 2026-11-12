"""A tool call the CLI's auto-mode classifier refused, approved after the fact:
detection on the segment, the `allow_denied` action, the one-shot PreToolUse
hook that honours it, and the `--settings` composition that wires the hook in.
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")
SERVER = os.path.join(AGENT_DIR, "permission_server.py")

DENIAL = ("Permission for this action was denied by the Claude Code auto mode "
          "classifier. Reason: %s. If you have other tasks that don't depend "
          "on this action, continue working on those.")


def _load(name):
    spec = importlib.util.spec_from_file_location(
        "claude_allow_" + name, os.path.join(AGENT_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    mod = _load("agent")
    mod.MODEL_WINDOWS = str(tmp_path / "model-windows.json")
    return mod


@pytest.fixture
def srv():
    return _load("permission_server")


def _call(tool_id, name, tool_input):
    return {"type": "assistant", "message": {"id": "m" + tool_id, "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def _result(tool_id, text, error=True):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": text,
         "is_error": error}]}}


def _write_run(agent, tmp_path, rows, run_id="run", chat="chat1", resumed=True):
    """A run of chat `chat` (resumed from it, or its own first run)."""
    run_dir = tmp_path / "runs" / run_id
    os.makedirs(run_dir / "perm", exist_ok=True)
    meta = {"resumed_from": chat} if resumed else {"session_id": chat}
    (run_dir / "meta.json").write_text(json.dumps(meta))
    with open(run_dir / "out.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    agent.RUNS = str(tmp_path / "runs")
    return run_dir


# ------------------------------------------------------------------ detection

@pytest.mark.parametrize("reason,expected", [
    ("[Production Deploy]", "Production Deploy"),
    ("The server-side auto mode classifier judged this action dangerous "
     "(it gave no explanation)", "The server-side auto mode classifier judged "
     "this action dangerous (it gave no explanation)"),
    ("", ""),
])
def test_classifier_denial_reason(agent, reason, expected):
    assert agent._classifier_denial(DENIAL % reason) == expected


def test_classifier_denial_without_the_tail_reads_to_the_end(agent):
    text = ("Permission for this action was denied by the Claude Code auto mode "
            "classifier. Reason: [Git Destructive]")
    assert agent._classifier_denial(text) == "Git Destructive"


@pytest.mark.parametrize("text", [
    "", None, "Exit code 1\nboom",
    "The user doesn't want to proceed with this tool use.",
    "Note: Permission for this action was denied by the Claude Code auto mode "
    "classifier. Reason: [X].",
])
def test_classifier_denial_ignores_every_other_text(agent, text):
    assert agent._classifier_denial(text) is None


def test_segment_carries_the_denial(agent):
    rows = [_call("t1", "Bash", {"command": "git push --force"}),
            _result("t1", DENIAL % "[Git Destructive]")]
    (seg,) = agent._segments_from_rows(rows)
    assert seg["status"] == "error"
    assert seg["denied"] == {"reason": "Git Destructive"}


def test_segment_without_a_denial_has_no_denied_field(agent):
    rows = [_call("t1", "Bash", {"command": "ls"}), _result("t1", "boom"),
            _call("t2", "Bash", {"command": "ls"}),
            _result("t2", DENIAL % "[X]", error=False)]
    segs = agent._segments_from_rows(rows)
    assert all("denied" not in s for s in segs)


def test_poll_carries_the_denial(agent, tmp_path):
    rows = [{"type": "user", "message": {"role": "user", "content": "go"}},
            _call("t1", "Bash", {"command": "git push --force"}),
            _result("t1", DENIAL % "[Git Destructive]")]
    _write_run(agent, tmp_path, rows)
    agent._alive = lambda _run_dir: True
    polled = agent._poll("run")
    assert [s["denied"] for s in polled["segments"] if s["kind"] == "tool"] == [
        {"reason": "Git Destructive"}]
    # The persisted-transcript reader goes through the same builder.
    (seg,) = agent._segments_from_rows(rows[1:])
    assert seg["denied"] == {"reason": "Git Destructive"}


def test_allowed_ids_mark_the_segment(agent, tmp_path):
    path = agent._chat_allowed_denied_path("chat1")
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as fh:
        json.dump(["t1"], fh)
    segs = [{"kind": "tool", "id": "t1", "denied": {"reason": "x"}},
            {"kind": "tool", "id": "t2", "denied": {"reason": "y"}}]
    agent._annotate_allowed(segs, "chat1")
    assert segs[0]["denied"].get("allowed") is True
    assert "allowed" not in segs[1]["denied"]


# -------------------------------------------------------------- allow action

def _store(agent, kind, chat="chat1"):
    import pathlib
    return pathlib.Path(agent._state_file("claude-sessions", kind, chat + ".json"))


def test_allow_denied_reads_the_call_server_side_and_answers_the_message(
        agent, tmp_path):
    run_dir = _write_run(agent, tmp_path, [
        _call("t1", "Bash", {"command": "git push --force origin main",
                             "description": "push", "timeout": 5}),
        _result("t1", DENIAL % "[Git Destructive]")])
    res = agent._allow_denied("chat1", "t1")
    assert res["allowed"] == "t1"
    (entry,) = json.loads(_store(agent, 'allow-once').read_text())
    assert entry["tool_name"] == "Bash" and entry["tool_use_id"] == "t1"
    assert entry["match_input"] == {"command": "git push --force origin main"}
    assert abs(entry["created"] - time.time()) < 60
    assert json.loads(_store(agent, 'allowed-denied').read_text()) == ["t1"]
    msg = res["message"]
    assert "Bash" in msg and "git push --force origin main" in msg
    assert "exactly the same input" in msg


def test_allow_denied_twice_keeps_one_grant(agent, tmp_path):
    run_dir = _write_run(agent, tmp_path, [
        _call("t1", "Bash", {"command": "ls"}), _result("t1", DENIAL % "[X]")])
    agent._allow_denied("chat1", "t1")
    agent._allow_denied("chat1", "t1")
    assert len(json.loads(_store(agent, 'allow-once').read_text())) == 1
    assert json.loads(_store(agent, 'allowed-denied').read_text()) == ["t1"]


def test_allow_denied_non_bash_matches_the_whole_input_minus_description(
        agent, tmp_path):
    run_dir = _write_run(agent, tmp_path, [
        _call("t1", "WebFetch", {"url": "http://x", "prompt": "p", "description": "d"}),
        _result("t1", DENIAL % "[Exfil]")])
    agent._allow_denied("chat1", "t1")
    (entry,) = json.loads(_store(agent, 'allow-once').read_text())
    assert entry["match_input"] == {"url": "http://x", "prompt": "p"}


@pytest.mark.parametrize("rows", [
    [_call("t1", "Bash", {"command": "ls"}), _result("t1", "Exit code 1")],
    [_call("t1", "Bash", {"command": "ls"}),
     _result("t1", DENIAL % "[X]", error=False)],
    [_call("t1", "Bash", {"command": "ls"})],
    [],
])
def test_allow_denied_refuses_what_the_classifier_did_not_refuse(
        agent, tmp_path, rows):
    run_dir = _write_run(agent, tmp_path, rows)
    assert "error" in agent._allow_denied("chat1", "t1")
    assert "error" in agent._allow_denied("chat1", "nope")
    assert not _store(agent, 'allow-once').exists()


def test_allow_denied_unknown_run_and_empty_id(agent, tmp_path):
    agent.RUNS = str(tmp_path)
    assert "error" in agent._allow_denied("missing", "t1")
    assert "error" in agent._allow_denied("../x", "t1")
    assert "error" in agent._allow_denied("", "t1")


def test_allow_denied_refuses_a_non_claude_run(agent, tmp_path):
    run_dir = _write_run(agent, tmp_path, [
        _call("t1", "Bash", {"command": "ls"}), _result("t1", DENIAL % "[X]")])
    (run_dir / "meta.json").write_text(json.dumps({"backend": "codex", "resumed_from": "chat1"}))
    assert "error" in agent._allow_denied("chat1", "t1")
    assert not _store(agent, 'allow-once').exists()


def test_allow_denied_finds_a_refusal_in_an_earlier_run_of_the_chat(
        agent, tmp_path):
    _write_run(agent, tmp_path, [
        _call("t1", "Bash", {"command": "rm -rf build"}),
        _result("t1", DENIAL % "[X]")], run_id="a-first", resumed=False)
    _write_run(agent, tmp_path, [_call("t2", "Bash", {"command": "ls"})],
               run_id="b-second")
    _write_run(agent, tmp_path, [
        _call("t1", "Bash", {"command": "other chat"}),
        _result("t1", DENIAL % "[X]")], run_id="c-other", chat="chat2")
    res = agent._allow_denied("chat1", "t1")
    assert res["allowed"] == "t1" and "rm -rf build" in res["message"]
    (entry,) = json.loads(_store(agent, "allow-once").read_text())
    assert entry["match_input"] == {"command": "rm -rf build"}
    assert "error" in agent._allow_denied("chat1", "t9")


def test_allow_denied_route_is_registered(agent):
    from fused_render.server.routers import claude_agent as router
    assert router.ACTIONS["allow_denied"] == "_allow_denied"
    assert callable(getattr(agent, "_allow_denied"))


# ---------------------------------------------------------------------- hook

def _run_hook(grants, payload):
    return subprocess.run(
        [sys.executable, "-I", SERVER, "--allow-once-hook=" + str(grants)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, timeout=30)


def _grant(tmp_path, *entries):
    (tmp_path / "allow-once.json").write_text(json.dumps(list(entries)))
    return tmp_path / "allow-once.json"


def _entry(name="Bash", match=None, tid="t1", created=None):
    return {"tool_name": name, "match_input": match or {"command": "make deploy"},
            "tool_use_id": tid, "created": time.time() if created is None else created}


def test_hook_allows_the_matching_call_once_and_consumes_it(tmp_path):
    _grant(tmp_path, _entry(), _entry(match={"command": "other"}, tid="t2"))
    call = {"tool_name": "Bash", "tool_input": {
        "command": "make deploy", "description": "reworded", "timeout": 9}}
    r = _run_hook(tmp_path / "allow-once.json", call)
    assert r.returncode == 0
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "allow"
    left = json.loads((tmp_path / "allow-once.json").read_text())
    assert [e["tool_use_id"] for e in left] == ["t2"]
    again = _run_hook(tmp_path / "allow-once.json", call)
    assert again.returncode == 0 and again.stdout == ""


def test_hook_removes_the_file_with_its_last_grant(tmp_path):
    _grant(tmp_path, _entry())
    _run_hook(tmp_path / "allow-once.json", {"tool_name": "Bash", "tool_input": {"command": "make deploy"}})
    assert not (tmp_path / "allow-once.json").exists()


def test_hook_other_tool_matches_the_whole_input_minus_description(tmp_path):
    _grant(tmp_path, _entry("WebFetch", {"url": "http://x", "prompt": "p"}))
    miss = _run_hook(tmp_path / "allow-once.json", {"tool_name": "WebFetch",
                                "tool_input": {"url": "http://y", "prompt": "p"}})
    assert miss.stdout == ""
    wrong_tool = _run_hook(tmp_path / "allow-once.json", {"tool_name": "Bash",
                                      "tool_input": {"url": "http://x", "prompt": "p"}})
    assert wrong_tool.stdout == ""
    hit = _run_hook(tmp_path / "allow-once.json", {"tool_name": "WebFetch", "tool_input": {
        "url": "http://x", "prompt": "p", "description": "d"}})
    assert json.loads(hit.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_hook_ignores_an_expired_grant(tmp_path):
    _grant(tmp_path, _entry(created=time.time() - 31 * 60))
    r = _run_hook(tmp_path / "allow-once.json", {"tool_name": "Bash", "tool_input": {"command": "make deploy"}})
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_name": 3}',
                                   '{"tool_input": {}}', "null"])
def test_hook_fails_open_on_malformed_input(tmp_path, stdin):
    _grant(tmp_path, _entry())
    r = _run_hook(tmp_path / "allow-once.json", stdin)
    assert r.returncode == 0 and r.stdout == ""
    assert (tmp_path / "allow-once.json").exists()


@pytest.mark.parametrize("content", ["", "{", '{"a": 1}', "[1, 2]"])
def test_hook_fails_open_on_a_bad_grant_file(tmp_path, content):
    (tmp_path / "allow-once.json").write_text(content)
    r = _run_hook(tmp_path / "allow-once.json", {"tool_name": "Bash", "tool_input": {"command": "x"}})
    assert r.returncode == 0 and r.stdout == ""


def test_hook_without_a_grant_file_is_silent(tmp_path):
    r = _run_hook(tmp_path / "allow-once.json", {"tool_name": "Bash", "tool_input": {"command": "x"}})
    assert r.returncode == 0 and r.stdout == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell guard")
def test_hook_command_skips_python_when_nothing_is_pending(agent, tmp_path):
    cmd = agent._allow_once_hook_cmd(str(tmp_path / "allow-once.json"))
    assert cmd.startswith("[ -s ") and "|| exit 0;" in cmd
    # No grant file: the guard exits before python is looked at.
    r = subprocess.run(["sh", "-c", cmd.replace(shlex.split(cmd.split(";", 1)[1])[0],
                                                "/nonexistent-python")],
                       input="{}", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell guard")
def test_hook_command_runs_the_hook_when_a_grant_is_pending(agent, tmp_path):
    _grant(tmp_path, _entry())
    r = subprocess.run(["sh", "-c", agent._allow_once_hook_cmd(
        str(tmp_path / "allow-once.json"))],
                       input=json.dumps({"tool_name": "Bash",
                                         "tool_input": {"command": "make deploy"}}),
                       capture_output=True, text=True, timeout=30)
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_agent_takes_the_grant_format_from_the_server(agent):
    mod = agent._permission_server_mod()
    assert mod.ALLOW_ONCE_TTL == 30 * 60
    assert agent._permission_server_mod() is mod
    for name in ("ALLOW_ONCE_FILE", "ALLOW_ONCE_TTL", "_allow_once_match_input"):
        assert not hasattr(agent, name)


def test_hook_rewrites_its_store_private(tmp_path):
    path = _grant(tmp_path, _entry(), _entry(match={"command": "other"}, tid="t2"))
    _run_hook(path, {"tool_name": "Bash", "tool_input": {"command": "make deploy"}})
    assert (path.stat().st_mode & 0o777) == 0o600 or os.name == "nt"


def test_hook_leaves_the_store_alone_on_a_miss(tmp_path):
    path = _grant(tmp_path, _entry())
    before = path.read_text()
    _run_hook(path, {"tool_name": "Bash", "tool_input": {"command": "nope"}})
    assert path.read_text() == before


# ------------------------------------------------------------ settings wiring

def _argv(agent, tmp_path, **kw):
    (tmp_path / "rd" / "perm").mkdir(parents=True, exist_ok=True)
    args = dict(run_dir=str(tmp_path / "rd"), pane=False, cli_mode=None,
                session_id="chat1", model="", effort="", extra_read_dirs=[],
                file="/tmp/f")
    args.update(kw)
    return agent._claude_argv(**args)


def _hooks(argv):
    assert argv.count("--settings") == 1
    return json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"]


def test_every_chat_gets_the_allow_once_hook(agent, tmp_path):
    (entry,) = _hooks(_argv(agent, tmp_path))
    assert entry["matcher"] == "*"
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    assert ("--allow-once-hook=" + agent._chat_allow_once_path("chat1")
            in hook["command"])


def test_a_chat_without_a_session_id_gets_no_hook(agent, tmp_path):
    argv = _argv(agent, tmp_path, session_id="")
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert "PreToolUse" not in settings.get("hooks", {})


def test_breakout_keeps_its_hook_and_gains_the_allow_once_one(agent, tmp_path):
    argv = _argv(agent, tmp_path, breakout=True)
    breakout, allow_once = _hooks(argv)
    assert breakout["matcher"] == "Write|Edit|MultiEdit|NotebookEdit|mcp__.*"
    assert "--breakout-hook=" in breakout["hooks"][0]["command"]
    assert breakout["hooks"][0]["timeout"] == 10
    assert "--allow-once-hook=" in allow_once["hooks"][0]["command"]
    assert "Bash" in json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]


def test_canvas_clone_keeps_its_hooks_and_gains_the_allow_once_one(
        agent, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_in_synced_clone", lambda f: True)
    argv = _argv(agent, tmp_path)
    settings = json.loads(argv[argv.index("--settings") + 1])["hooks"]
    assert len(settings["PreToolUse"]) == 2 and "PostToolUse" in settings
    assert "--allow-once-hook=" in settings["PreToolUse"][-1]["hooks"][0]["command"]
