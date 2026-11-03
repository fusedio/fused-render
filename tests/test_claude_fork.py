"""Fork from an earlier user prompt: `_fork_point`, the argv, and `_start`.

A fork resumes the OLD session truncated at the parent of the re-asked prompt
(`--resume-session-at`), into a NEW minted session (`--fork-session
--session-id`). The old transcript is never touched; the new run is not a
continuation of the old session, so `resumed_from` stays empty and the link
lives under `meta["forked_from"]`.
"""
import importlib.util
import json
import os
import re

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")
_UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _load(name):
    path = os.path.join(AGENT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_fork_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path, monkeypatch):
    mod = _load("agent")
    mod.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(mod, "_claude_bin", lambda: "/bin/claude")
    return mod


def _transcript(tmp_path, rows):
    p = tmp_path / "old.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return str(p)


ROWS = [
    {"type": "user", "uuid": "u1", "parentUuid": None, "message": {}},
    {"type": "assistant", "uuid": "a1", "parentUuid": "u1"},
    {"type": "attachment", "uuid": "t2", "parentUuid": "a1"},
    {"type": "user", "uuid": "u2", "parentUuid": "t2", "message": {}},
    {"type": "assistant", "uuid": "a2", "parentUuid": "u2"},
]


def test_fork_point_returns_parent(agent, tmp_path):
    assert agent._fork_point(_transcript(tmp_path, ROWS), "u2") == "t2"


def test_fork_point_null_parent_is_empty(agent, tmp_path):
    assert agent._fork_point(_transcript(tmp_path, ROWS), "u1") == ""


def test_fork_point_not_found(agent, tmp_path):
    path = _transcript(tmp_path, ROWS)
    assert agent._fork_point(path, "nope") is None
    # only a USER record counts
    assert agent._fork_point(path, "a1") is None
    assert agent._fork_point(str(tmp_path / "missing.jsonl"), "u2") is None


def _argv(agent, tmp_path, **kw):
    (tmp_path / "rd" / "perm").mkdir(parents=True, exist_ok=True)
    args = dict(run_dir=str(tmp_path / "rd"), pane=False, cli_mode=None, session_id="",
                model="", effort="", extra_read_dirs=[], file="/tmp/f")
    args.update(kw)
    return agent._claude_argv(**args)


def test_argv_fork_flags(agent, tmp_path):
    argv = _argv(agent, tmp_path, session_id="OLD", new_session_id="NEW", resume_at="P")
    assert argv[argv.index("--resume") + 1] == "OLD"
    assert argv[argv.index("--resume-session-at") + 1] == "P"
    assert "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == "NEW"
    assert "--resume-drops-turn" not in argv


def test_argv_resume_and_fresh_unchanged(agent, tmp_path):
    resume = _argv(agent, tmp_path, session_id="OLD")
    assert "--resume" in resume
    for flag in ("--fork-session", "--session-id", "--resume-session-at"):
        assert flag not in resume
    fresh = _argv(agent, tmp_path, new_session_id="NEW")
    assert "--resume" not in fresh and "--fork-session" not in fresh
    assert fresh[fresh.index("--session-id") + 1] == "NEW"


class _HostProc:
    pid = 4242

    def wait(self, timeout=None):
        return 0

    class _Stdin:
        def __init__(self, seen):
            self._seen, self._buf = seen, b""

        def write(self, data):
            self._buf += data

        def close(self):
            self._seen["req"] = json.loads(self._buf.decode("utf-8"))

    def __init__(self, seen):
        self.stdin = _HostProc._Stdin(seen)


@pytest.fixture
def start(agent, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    seen = {}
    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda cmd, **kw: _HostProc(seen))
    monkeypatch.setattr(agent, "_backend_for_session",
                        lambda sid, wd, hint="": "claude")
    holder = {"rows": ROWS}
    monkeypatch.setattr(agent, "_session_transcript_path",
                        lambda wd, sid: _transcript(tmp_path, holder["rows"]))

    def go(fork_at, session_id="OLD", backend="claude", **kw):
        out = agent._start(str(project), "new prompt", session_id, "", "",
                           backend=backend, fork_at=fork_at, **kw)
        run_dir = os.path.join(agent.RUNS, out["run_id"]) \
            if "run_id" in out else ""
        return out, seen.get("req"), run_dir

    return go


def _meta(run_dir):
    with open(os.path.join(run_dir, "meta.json")) as fh:
        return json.load(fh)


def test_start_fork_resumes_at_parent(agent, start):
    out, req, run_dir = start("u2")
    assert "error" not in out, out
    new = out["session_id"]
    assert _UUID4.match(new) and new != "OLD"
    assert req["session_id"] == "OLD" and req["resume_at"] == "t2"
    assert req["new_session_id"] == new
    meta = _meta(run_dir)
    assert meta["forked_from"] == "OLD" and meta["fork_at"] == "u2"
    assert meta["session_id"] == new
    assert meta["resumed_from"] == ""


def test_start_fork_null_parent_is_fresh_chat(agent, start):
    out, req, run_dir = start("u1")
    assert "error" not in out, out
    new = out["session_id"]
    assert _UUID4.match(new) and new != "OLD"
    assert req["session_id"] == "" and "resume_at" not in req
    meta = _meta(run_dir)
    assert meta["forked_from"] == "OLD" and "fork_at" not in meta
    assert meta["session_id"] == new and meta["resumed_from"] == ""


def test_start_fork_missing_uuid_refused(agent, start):
    out, _, _ = start("ghost")
    assert out["reason"] == "fork_target_missing"
    assert not os.path.isdir(agent.RUNS) or not os.listdir(agent.RUNS)


def test_start_fork_copilot_refused(agent, start, monkeypatch):
    monkeypatch.setattr(agent, "_backend_for_session",
                        lambda sid, wd, hint="": "copilot")
    monkeypatch.setattr(agent, "_backend_enabled", lambda b: True)
    out, _, _ = start("u2")
    assert out["reason"] == "unsupported"
    assert "Claude" in out["error"]
    assert not os.path.isdir(agent.RUNS) or not os.listdir(agent.RUNS)


# ----------------------------------------------------------------- breakout
#
# A breakout is a fork that is read-only by policy and ephemeral: it forks the
# session (at a message, or at the tip when `fork_at` is empty), runs in
# `default` permission mode whatever the page asked for, loses the `fused` CLI
# pre-allowance, and tells the permission bridge where its one writable
# directory is.


def test_argv_breakout_forks_at_tip(agent, tmp_path):
    argv = _argv(agent, tmp_path, session_id="OLD", new_session_id="NEW",
                 breakout=True)
    assert argv[argv.index("--resume") + 1] == "OLD"
    assert "--fork-session" in argv
    assert argv[argv.index("--session-id") + 1] == "NEW"
    assert "--resume-session-at" not in argv


def test_argv_breakout_keeps_resume_at_for_a_message(agent, tmp_path):
    argv = _argv(agent, tmp_path, session_id="OLD", new_session_id="NEW",
                 resume_at="P", breakout=True)
    assert argv[argv.index("--resume-session-at") + 1] == "P"


def test_argv_breakout_forces_default_mode_and_drops_fused_cli(
        agent, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_fused_cli_dir", lambda: "/x/bin")
    plain = _argv(agent, tmp_path, cli_mode="acceptEdits")
    assert plain[plain.index("--permission-mode") + 1] == "acceptEdits"
    assert "Bash(fused:*)" in plain[plain.index("--allowed-tools") + 1]
    argv = _argv(agent, tmp_path, cli_mode="acceptEdits", breakout=True)
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert "Bash(fused:*)" not in argv[argv.index("--allowed-tools") + 1]
    # No mode at all still pins default for a breakout.
    argv = _argv(agent, tmp_path, cli_mode=None, breakout=True)
    assert argv[argv.index("--permission-mode") + 1] == "default"


def test_argv_breakout_hands_the_bridge_the_handoff_dir(agent, tmp_path):
    argv = _argv(agent, tmp_path, session_id="OLD", new_session_id="NEW",
                 breakout=True)
    with open(argv[argv.index("--mcp-config") + 1]) as fh:
        env = json.load(fh)["mcpServers"][agent.PERMISSION_SERVER]["env"]
    assert env["FUSED_BREAKOUT_HANDOFF_DIR"] == os.path.join(
        agent._workdir("/tmp/f"), ".claude", "handoffs")
    plain = _argv(agent, tmp_path / "other")
    with open(plain[plain.index("--mcp-config") + 1]) as fh:
        env = json.load(fh)["mcpServers"][agent.PERMISSION_SERVER]["env"]
    assert "FUSED_BREAKOUT_HANDOFF_DIR" not in env


def test_breakout_prompt_names_the_handoff_dir(agent):
    plain = agent._appended_prompt("/tmp/f", False)
    assert "breakout" not in plain.lower()
    text = agent._appended_prompt("/tmp/f", False, breakout=True)
    assert "breakout" in text.lower()
    assert os.path.join(agent._workdir("/tmp/f"), ".claude", "handoffs") in text


def test_start_breakout_at_tip(agent, start):
    out, req, run_dir = start("", breakout=True)
    assert "error" not in out, out
    new = out["session_id"]
    assert _UUID4.match(new) and new != "OLD"
    assert req["session_id"] == "OLD" and "resume_at" not in req
    assert req["new_session_id"] == new and req["breakout"] is True
    meta = _meta(run_dir)
    assert meta["breakout"] is True and meta["forked_from"] == "OLD"
    assert meta["session_id"] == new and meta["resumed_from"] == ""
    assert "fork_at" not in meta


def test_start_breakout_at_a_message(agent, start):
    out, req, run_dir = start("u2", breakout=True)
    assert "error" not in out, out
    assert req["resume_at"] == "t2" and req["breakout"] is True
    assert _meta(run_dir)["breakout"] is True


def test_start_without_breakout_leaves_no_flag(agent, start):
    out, req, run_dir = start("u2")
    assert "breakout" not in _meta(run_dir) and "breakout" not in req


def test_start_breakout_needs_a_session(agent, start):
    out, _, _ = start("", session_id="", breakout=True)
    assert out["reason"] == "unsupported"
    assert not os.path.isdir(agent.RUNS) or not os.listdir(agent.RUNS)


def test_start_breakout_copilot_refused(agent, start, monkeypatch):
    monkeypatch.setattr(agent, "_backend_for_session",
                        lambda sid, wd, hint="": "copilot")
    monkeypatch.setattr(agent, "_backend_enabled", lambda b: True)
    out, _, _ = start("", breakout=True)
    assert out["reason"] == "unsupported"
    assert "Claude" in out["error"]
    assert not os.path.isdir(agent.RUNS) or not os.listdir(agent.RUNS)


def test_main_forwards_breakout(agent, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent, "_start", lambda *a, **kw: seen.update(kw) or {})
    agent.main(action="start", file="/tmp/f", message="m", session_id="S",
               breakout="1")
    assert seen["breakout"] is True
    seen.clear()
    agent.main(action="start", file="/tmp/f", message="m", session_id="S")
    assert seen["breakout"] is False


def test_start_breakout_resume_continues_without_forking(agent, start):
    out, req, run_dir = start("", session_id="BRK", breakout=True,
                              breakout_resume=True)
    assert "error" not in out, out
    assert req["session_id"] == "BRK" and req["new_session_id"] == ""
    assert "resume_at" not in req and req["breakout"] is True
    meta = _meta(run_dir)
    assert meta["breakout"] is True and "forked_from" not in meta
    assert meta["resumed_from"] == "BRK"


def test_main_breakout_resume_param(agent, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent, "_start", lambda *a, **kw: seen.update(kw) or {})
    agent.main(action="start", file="/tmp/f", message="m", session_id="S",
               breakout="resume")
    assert seen["breakout"] is True and seen["breakout_resume"] is True
