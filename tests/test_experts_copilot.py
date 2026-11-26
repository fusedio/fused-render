"""Experts on GitHub Copilot (docs/experts.md §16): agent.py's Copilot seams and the experts package's half.

A Copilot task gets pull and note (never the breakout tool); an expert's own session on Copilot gets its
charter with the memory inlined, its skills, its launch spec's servers, and a host request saying which tools
ask in every mode and whether anybody is there. Copilot mints the session id after the start, so the main
chat's record and a learning run's are written at the first poll. The `copilot` CLI is never run; the host's
side of the gate is in test_copilot_host.py.
"""
import importlib.util
import json
import os
import sys
import types

import pytest

from _experts_conftest import *  # noqa: F401,F403
from fused_render.experts import chat, launch, update

AGENT = os.path.join("fused_render", "claude_agent", "agent.py")
ORIGIN = "http://127.0.0.1:4321"
TASK = {"root": "/ws/experts", "all": [["Canvas", "Fused canvases"]], "here": ["Canvas"]}
SPEC = {"kind": "main", "append": "This is your main chat with the user. Use mcp__expert_canvas__show.",
        "policy_env": {},
        "mcp_servers": {"expert_services": {"command": "py", "args": ["botmcp.py"], "alwaysLoad": True}}}


@pytest.fixture
def agent(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sp_copilot_agent", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.RUNS = str(tmp_path / "runs")
    mod.SHOTS = str(tmp_path / "shots")
    monkeypatch.setattr(mod, "_copilot_bin", lambda: "/bin/copilot")
    monkeypatch.setattr(mod, "_origin", lambda: ORIGIN)
    monkeypatch.setattr(mod, "_gh_token", lambda *a, **k: "")
    monkeypatch.delenv("FUSED_RENDER_FUSED_CLI_DIR", raising=False)
    return mod


@pytest.fixture
def rd(tmp_path):
    (tmp_path / "rd" / "perm").mkdir(parents=True)
    return tmp_path / "rd"


@pytest.fixture
def home(tmp_path):
    """An expert's home as `home.lay_out` leaves it, with one line of memory and one skill."""
    h = tmp_path / "experts" / "canvas"
    (h / ".claude" / "skills" / "tiles").mkdir(parents=True)
    (h / ".claude" / "skills" / "tiles" / "SKILL.md").write_text("---\nname: tiles\n---\nUse XYZ tiles.\n")
    (h / "CLAUDE.md").write_text("# The Canvas expert\nYou are the Canvas expert.\n\n@memory.md\n")
    (h / "memory.md").write_text("- [2 · 2026-11-20] Parquet wins.\n")
    return h


def _argv(agent, rd, file, mode=None):
    return agent._copilot_argv(str(rd), False, mode, "", "", "", [], str(file))


def _all(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def _body(rd):
    return (rd / "plugin" / "agents" / "fused.agent.md").read_text()


def _mcp(rd):
    return json.loads((rd / "mcp.json").read_text())["mcpServers"]


# -------------------------------------------------------------------- argv ---
def test_a_copilot_run_with_no_expert_files_carries_nothing_of_them(agent, rd, tmp_path):
    argv = _argv(agent, rd, tmp_path)
    assert "expert" not in (json.dumps(argv) + _body(rd)).lower()


def test_a_copilot_task_gets_pull_and_note_in_its_own_spelling(agent, rd, tmp_path):
    (rd / "experts.on").write_text(json.dumps(TASK))
    argv = _argv(agent, rd, tmp_path)
    assert _mcp(rd)["fused_render"]["env"]["FUSED_RENDER_EXPERT_ORIGIN"] == ORIGIN
    assert "--no-approve" in _mcp(rd)["fused_render"]["args"]
    assert _all(argv, "--allow-tool") == ["fused_render(expert_pull)", "fused_render(expert_note)"]
    body = _body(rd)
    assert "knows one subject: Canvas (Fused canvases). Call fused_render-expert_pull" in body
    assert body.rstrip().endswith('Before anything else call expert_pull with expert "Canvas".')
    # A breakout is Claude's alone: neither offered nor named.
    assert "expert_breakout" not in body and "mcp__" not in body


def test_task_tools_need_an_origin(agent, rd, tmp_path, monkeypatch):
    (rd / "experts.on").write_text(json.dumps(TASK))
    monkeypatch.setattr(agent, "_origin", lambda: "")
    assert "expert" not in (json.dumps(_argv(agent, rd, tmp_path)) + _body(rd)).lower()


def test_an_expert_session_gets_its_charter_memory_servers_and_skills(agent, rd, home):
    (rd / "expert.json").write_text(json.dumps(SPEC))
    (rd / "experts.on").write_text(json.dumps(TASK))  # a spec wins
    argv = _argv(agent, rd, home, "auto")
    body = _body(rd)
    assert body.index("You are the Canvas expert.") < body.index("- [2 · 2026-11-20] Parquet wins.")
    assert "@memory.md" not in body
    assert body.rstrip().endswith("This is your main chat with the user. Use expert_canvas-show.")
    assert "expert_pull" not in json.dumps(argv) + body
    servers = _mcp(rd)
    # Copilot's never-defer key, not Claude's, on the spec's servers.
    assert servers["expert_services"] == {"command": "py", "args": ["botmcp.py"], "deferTools": "never"}
    assert "FUSED_RENDER_EXPERT_ORIGIN" not in servers["fused_render"]["env"]
    assert (rd / "plugin" / "skills" / "tiles" / "SKILL.md").read_text().endswith("Use XYZ tiles.\n")


def test_a_learning_run_may_read_the_folders_of_its_transcripts(agent, rd, home, tmp_path):
    (tmp_path / "tx").mkdir()
    spec = dict(SPEC, kind="update", read_dirs=[str(tmp_path / "tx"), str(tmp_path / "gone")])
    (rd / "expert.json").write_text(json.dumps(spec))
    adds = _all(_argv(agent, rd, home, "auto"), "--add-dir")
    assert str(tmp_path / "tx") in adds and str(tmp_path / "gone") not in adds


def test_the_charter_without_an_import_line_still_carries_the_memory(agent, home):
    (home / "CLAUDE.md").write_text("# Hand-edited\n")
    assert agent._expert_charter(str(home)).endswith("- [2 · 2026-11-20] Parquet wins.\n--- end of memory.md ---")
    (home / "CLAUDE.md").write_text("")
    assert agent._expert_charter(str(home)) == ""


# ------------------------------------------------------------------ _start ---
class _HostProc:
    pid = 4242

    def __init__(self, seen):
        self.stdin = types.SimpleNamespace(write=lambda data: seen.update(raw=data), close=lambda: None)

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def start(agent, tmp_path, monkeypatch):
    (tmp_path / "proj").mkdir()
    seen = {}
    monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: _HostProc(seen))
    monkeypatch.setattr(agent, "_backend_enabled", lambda b: True)

    def go(file, backend="copilot", **kw):
        seen.clear()
        out = agent._start(str(file), "hello", kw.pop("session_id", ""), "", "", backend=backend, **kw)
        return out, os.path.join(agent.RUNS, out["run_id"]) if "run_id" in out else "", \
            json.loads(seen["raw"]) if "raw" in seen else {}
    return go


@pytest.fixture
def modules(monkeypatch, home):
    """Stand-ins for the server's experts modules, as agent.py reads them off `sys.modules`."""
    root = str(home.parent)
    paths = types.ModuleType("paths")
    paths.root = lambda: root
    paths.is_inside_root = lambda p: os.path.abspath(p).startswith(root)
    store = types.ModuleType("store")
    store.all = lambda: [types.SimpleNamespace(name="Canvas", about="Fused canvases")]
    store.for_folder = lambda p: []
    lm = types.ModuleType("launch")
    lm.minted_calls = []
    lm.spec = lambda key, file: dict(SPEC, kind=key.split(":")[1])
    lm.minted = lambda key, sid: lm.minted_calls.append((key, sid))
    for name, mod in (("paths", paths), ("store", store), ("launch", lm)):
        monkeypatch.setitem(sys.modules, "fused_render.experts." + name, mod)
    monkeypatch.setenv("FUSED_RENDER_EXPERTS", "1")
    return types.SimpleNamespace(root=root, launch=lm)


def test_a_copilot_task_records_the_experts(agent, start, modules, tmp_path):
    out, run_dir, _ = start(tmp_path / "proj")
    assert "error" not in out, out
    assert json.load(open(os.path.join(run_dir, "experts.on")))["all"] == [["Canvas", "Fused canvases"]]


def test_a_main_chat_on_copilot_tells_the_host_what_asks(agent, start, modules, home):
    modules.launch.spec = lambda key, file: dict(SPEC, ask=["mcp__expert_canvas__canvas_create"])
    out, run_dir, req = start(home, expert="canvas:main")
    assert "error" not in out, out
    assert out["session_id"] == ""   # Copilot mints it
    assert json.load(open(os.path.join(run_dir, "meta.json")))["expert"] == "canvas:main"
    assert req["expert"] == {"ask": ["expert_canvas-canvas_create"], "unattended": False}
    assert json.load(open(os.path.join(run_dir, "launch.json")))["expert"] == req["expert"]


def test_a_learning_run_on_copilot_is_unattended(agent, start, modules, home):
    modules.launch.spec = lambda key, file: dict(SPEC, kind="update", policy_env={"FUSED_EXPERT_UNATTENDED": "1"})
    _, _, req = start(home, expert="canvas:update", permission_mode="auto")
    assert req["expert"] == {"ask": [], "unattended": True} and req["cli_mode"] == "auto"


def test_a_claude_expert_session_is_unchanged(agent, start, modules, home, monkeypatch):
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    out, run_dir, req = start(home, backend="claude", expert="canvas:main")
    assert "error" not in out, out
    assert "expert" not in req and "expert" not in json.load(open(os.path.join(run_dir, "meta.json")))


@pytest.mark.parametrize("backend", ["codex", "gemini", "opencode", "cursor"])
def test_any_other_agent_is_refused(agent, start, modules, home, backend):
    out, _, _ = start(home, backend=backend, expert="canvas:main")
    assert out["reason"] == "unsupported" and "Claude or GitHub Copilot" in out["error"]


def test_an_expert_breakout_stays_claude_s(agent, start, modules, home):
    out, _, _ = start(home, expert="canvas:breakout", breakout=True)
    assert out == {"error": "breakout is only available for Claude chats", "reason": "unsupported",
                   "backend": "copilot"}


def test_the_minted_id_is_reported_once(agent, modules, rd):
    meta = {"expert": "canvas:main", "backend": "copilot"}
    agent._expert_minted(str(rd), meta, "")
    assert modules.launch.minted_calls == []
    agent._expert_minted(str(rd), meta, "sid-1")
    agent._expert_minted(str(rd), meta, "sid-1")
    assert modules.launch.minted_calls == [("canvas:main", "sid-1")]
    agent._expert_minted(str(rd), {"backend": "copilot"}, "sid-2")   # not an expert's run
    assert len(modules.launch.minted_calls) == 1


# ------------------------------------------------------ the experts package ---
def test_minted_records_a_main_chat_and_a_learning_run():
    s = make_expert()
    launch.minted(f"{s.id}:main", "m1")
    assert chat.load(s.home)["main"] == "m1"
    launch.minted(f"{s.id}:update", "u0")   # no run in progress: nothing to record
    assert chat.load(s.home)["update"] is None
    chat.change(s.home, lambda d: d.update(update={"session": "", "files": [], "lines": {}}))
    launch.minted(f"{s.id}:update", "u1")
    launch.minted(f"{s.id}:update", "u2")   # the first id stays
    assert chat.load(s.home)["update"]["session"] == "u1"
    launch.minted(f"{s.id}:breakout", "b1")
    launch.minted("nobody:main", "x")
    assert chat.load(s.home)["main"] == "m1"


def _fake_agent(enabled, default="claude", sessions=None):
    return types.SimpleNamespace(
        EXPERT_BACKENDS=("claude", "copilot"), _backend_enabled=lambda b: b in enabled,
        _default_backend_pref=lambda: default,
        _defaults=lambda file, sid: {"model": "gpt-5.4", "effort": "high", "backend": (sessions or {}).get(sid, "claude")})


@pytest.mark.parametrize("enabled, default, session, want", [
    (("claude", "copilot"), "claude", None, "claude"),
    (("claude", "copilot"), "copilot", None, "copilot"),    # the user's default agent
    (("claude", "copilot"), "codex", None, "claude"),       # a default that cannot run an expert
    (("copilot",), "claude", None, "copilot"),              # Claude switched off
    (("claude", "copilot"), "claude", "s-cop", "copilot"),  # the session's own agent
    (("claude",), "claude", "s-cop", "claude"),             # … unless it is switched off
    ((), "claude", None, "claude"),
])
def test_the_agent_a_home_runs_on(tmp_path, enabled, default, session, want):
    agent = _fake_agent(enabled, default, {"s-cop": "copilot"})
    assert chat.backend(str(tmp_path), session, agent) == want


def test_the_copilot_prompt_reads_the_transcripts_itself():
    cop, claude = update.prompt_for("copilot"), update.prompt_for("claude")
    assert claude == update.PROMPT and "start one Agent with model haiku" in claude
    assert "Agent with model" not in cop and "one after another" in cop
    # Both search a transcript written with or without a space after each separator.
    for p in (cop, claude):
        assert "'\"type\": ?\"text\", ?\"text\": ?\".{1,300}'" in p
    assert update.prompt_for("codex") == claude
