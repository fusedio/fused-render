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

    def go(fork_at, session_id="OLD", backend="claude"):
        out = agent._start(str(project), "new prompt", session_id, "", "",
                           backend=backend, fork_at=fork_at)
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
