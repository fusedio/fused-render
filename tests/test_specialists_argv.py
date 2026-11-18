"""agent.py's specialist seams (docs/specialists PLAN A.7): `_claude_argv`, `_write_mcp_config`, `_start`, `_decide`.

Flag off (no `specialist.json` / `specialists.on` in the run dir) the argv and mcp.json are exactly what they were;
a task run with the tools gets five pre-allowed tools, two hooks and the TASK_NOTE; a specialist's main chat or
breakout gets its launch spec (deny/ask/allow rules, extra servers, Stop hook, appended paragraph, policy env).
The `claude` CLI is never run.
"""
import importlib.util
import json
import os
import sys
import types

import pytest

AGENT = os.path.join("fused_render", "claude_agent", "agent.py")
ORIGIN = "http://127.0.0.1:4321"


def _load():
    spec = importlib.util.spec_from_file_location("sp_argv_agent", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def agent(tmp_path, monkeypatch):
    mod = _load()
    mod.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(mod, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(mod, "_origin", lambda: ORIGIN)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "frhome"))
    return mod


def _rd(tmp_path):
    rd = tmp_path / "rd"
    (rd / "perm").mkdir(parents=True, exist_ok=True)
    return rd


def _argv(agent, rd, **kw):
    args = dict(run_dir=str(rd), pane=False, cli_mode=None, session_id="", model="", effort="",
                extra_read_dirs=[], file="/work/proj/f.py", new_session_id="N")
    args.update(kw)
    return agent._claude_argv(**args)


def _settings(argv):
    return json.loads(argv[argv.index("--settings") + 1])


def _allowed(argv):
    return argv[argv.index("--allowed-tools") + 1].split(",")


def _prompt(argv):
    return argv[argv.index("--append-system-prompt") + 1]


def _mcp(rd):
    return json.loads((rd / "mcp.json").read_text())["mcpServers"]


# ---------------------------------------------------------------- flag off ---
@pytest.mark.parametrize("kw", [{}, {"breakout": True}, {"session_id": "S", "new_session_id": ""},
                                {"cli_mode": "acceptEdits"}])
def test_argv_identical_without_the_run_dir_files(agent, tmp_path, monkeypatch, kw):
    rd = _rd(tmp_path)
    argv = _argv(agent, rd, **kw)
    mcp = (rd / "mcp.json").read_text()
    # The same build with the seams forced off is byte-identical: they add nothing when the files are absent.
    monkeypatch.setattr(agent, "_specialist_spec", lambda run_dir: None)
    monkeypatch.setattr(agent, "_specialists_on", lambda run_dir, refresh=False: False)
    assert _argv(agent, rd, **kw) == argv
    assert (rd / "mcp.json").read_text() == mcp
    assert "specialist" not in json.dumps(argv).lower()
    assert "SPECIALIST" not in mcp


def test_write_mcp_config_defaults_unchanged(agent, tmp_path):
    rd = _rd(tmp_path)
    plain = open(agent._write_mcp_config(str(rd))).read()
    again = open(agent._write_mcp_config(str(rd), extra_servers=None, specialists=False, policy_env=None)).read()
    assert plain == again


def test_start_records_nothing_with_the_flag_off(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    _install_modules(monkeypatch, tmp_path, any_exist=True)
    out, req, run_dir = start(str(tmp_path / "proj"))
    assert "error" not in out
    assert not os.path.exists(os.path.join(run_dir, "specialists.on"))
    assert not os.path.exists(os.path.join(run_dir, "specialist.json"))


# ------------------------------------------------------------- task tools ---
def test_task_run_gets_tools_hooks_and_note(agent, tmp_path):
    rd = _rd(tmp_path)
    (rd / "specialists.on").write_text("1")
    argv = _argv(agent, rd, cli_mode="auto")
    allowed = _allowed(argv)
    for t in agent.SPECIALIST_TOOLS:
        assert f"mcp__fused_render__{t}" in allowed
    hooks = _settings(argv)["hooks"]
    for event, arg in (("UserPromptSubmit", "prompt"), ("PostToolUseFailure", "failure")):
        (group,) = [g for g in hooks[event] if "specialist_hook" in json.dumps(g)]
        assert group["matcher"] == "*"
        cmd = group["hooks"][0]["command"]
        assert cmd.endswith(" " + arg) and "-I" in cmd and group["hooks"][0]["timeout"] == 5
    assert "SPECIALISTS: the user keeps specialists" in _prompt(argv)
    ours = _mcp(rd)["fused_render"]
    assert ours["env"]["FUSED_RENDER_SPECIALIST_ORIGIN"] == ORIGIN
    assert ours["timeout"] >= (agent.SPECIALIST_OFFER_CEILING + 60) * 1000
    # no permission rules of a specialist home
    assert "permissions" not in _settings(argv) or "deny" not in _settings(argv)["permissions"]


def test_task_run_may_read_what_a_pull_points_to(agent, tmp_path):
    rd = _rd(tmp_path)
    root = str(tmp_path / "ws" / "specialists")
    (rd / "specialists.on").write_text(root)
    allowed = _allowed(_argv(agent, rd))
    wire = "//" + root.lstrip("/")
    assert {f"Read({wire}/*/memory.md)", f"Read({wire}/*/knowledge/**)",
            f"Read({wire}/*/.claude/skills/**)"} <= set(allowed)
    # nothing else of the root: not inbox/, .specialist/, said.md, the root itself
    assert not [r for r in allowed if root in r and not r.endswith(("/*/memory.md)", "/*/knowledge/**)",
                                                                     "/*/.claude/skills/**)"))]
    # an old marker ("1") grants no reads
    (rd / "specialists.on").write_text("1")
    assert not [r for r in _allowed(_argv(agent, rd)) if "/*/" in r]


def test_start_records_the_root_for_the_read_rules(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    root, _ = _install_modules(monkeypatch, tmp_path, any_exist=True)
    out, _, run_dir = start(str(tmp_path / "proj"))
    assert open(os.path.join(run_dir, "specialists.on")).read() == os.path.normpath(root)


def test_start_in_a_home_without_specialist_is_refused(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    root, _ = _install_modules(monkeypatch, tmp_path)
    home = os.path.join(root, "canvas")
    os.makedirs(home)
    out, _, _ = start(home)
    assert out["reason"] == "specialist_home"
    monkeypatch.setattr(agent, "_breakout_pref", lambda: True)
    assert start(home, breakout=True)[0]["reason"] == "specialist_home"
    assert not os.path.isdir(agent.RUNS) or not os.listdir(agent.RUNS)
    # flag off: unchanged (a plain start runs)
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert "error" not in start(home)[0]


def test_task_note_never_says_expert(agent):
    assert "expert" not in agent._specialists_note().lower()


def test_task_tools_need_an_origin(agent, tmp_path, monkeypatch):
    rd = _rd(tmp_path)
    (rd / "specialists.on").write_text("1")
    monkeypatch.setattr(agent, "_origin", lambda: "")
    argv = _argv(agent, rd)
    assert "mcp__fused_render__specialist_list" not in _allowed(argv)


# ------------------------------------------------------------ launch spec ---
def _spec(kind="breakout"):
    home = "/w/specialists/canvas"
    return {"deny": [f"Edit(/{home}/memory.md)", f"Edit({home}/memory.md)"],
            "ask": ["mcp__specialist_services"],
            "allow": ["mcp__specialist__note", "Read(//work/proj/**)"],
            "mcp_servers": {"specialist": {"command": "py", "args": ["sessmcp.py"]},
                            "fused_render": {"command": "evil"}},
            "hooks": {"Stop": [{"hooks": [{"specialist_hook": "stop"}]}]},
            "append": "This is a breakout from the task T; memory and skills are locked; use the note tool.",
            "policy_env": {"FUSED_SPECIALIST_POLICY": f"{ORIGIN}|abc|{kind}"}}


def test_breakout_spec_shapes_the_argv(agent, tmp_path):
    rd = _rd(tmp_path)
    (rd / "specialist.json").write_text(json.dumps(_spec()))
    (rd / "specialists.on").write_text("1")  # ignored: a spec wins, never both
    argv = _argv(agent, rd, file="/w/specialists/canvas", cli_mode="auto")
    settings = _settings(argv)
    assert "Edit(//w/specialists/canvas/memory.md)" in settings["permissions"]["deny"]
    assert settings["permissions"]["ask"] == ["mcp__specialist_services"]
    stop = settings["hooks"]["Stop"][0]["hooks"][0]
    assert stop["type"] == "command" and stop["command"].endswith(" stop") and "specialist_hook" in stop["command"]
    # every turn in a home starts with the prompt hook (history.before_turn), none of the task hooks
    (prompt_group,) = settings["hooks"]["UserPromptSubmit"]
    assert prompt_group["hooks"][0]["command"].endswith(" prompt")
    assert "PostToolUseFailure" not in settings["hooks"]
    assert not [r for r in _allowed(argv) if "/*/knowledge/**" in r]
    allowed = _allowed(argv)
    assert "mcp__specialist__note" in allowed and "Read(//work/proj/**)" in allowed
    assert "mcp__fused_render__specialist_pull" not in allowed
    assert _prompt(argv).endswith(" This is a breakout from the task T; memory and skills are locked; use the note tool.")
    assert "SPECIALISTS: the user keeps" not in _prompt(argv)
    servers = _mcp(rd)
    assert servers["specialist"]["args"] == ["sessmcp.py"]
    assert servers["fused_render"]["command"] != "evil"  # ours wins a collision
    env = servers["fused_render"]["env"]
    assert env["FUSED_SPECIALIST_POLICY"] == f"{ORIGIN}|abc|breakout"
    assert "FUSED_RENDER_SPECIALIST_ORIGIN" not in env
    assert argv[argv.index("--permission-mode") + 1] == "auto"  # the user's pill, unchanged


def test_spec_hooks_never_alias_and_merge_with_others(agent, tmp_path):
    rd = _rd(tmp_path)
    spec = _spec("main")
    (rd / "specialist.json").write_text(json.dumps(spec))
    a = _argv(agent, rd, file="/w/specialists/canvas")
    b = _argv(agent, rd, file="/w/specialists/canvas")
    assert a == b


# ------------------------------------------------------------------ _start ---
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
    (tmp_path / "proj").mkdir(exist_ok=True)
    seen = {}
    monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: _HostProc(seen))
    monkeypatch.setattr(agent, "_backend_enabled", lambda b: True)

    def go(file, specialist="", backend="claude", **kw):
        out = agent._start(file, "hello", "", "", "", backend=backend, specialist=specialist, **kw)
        run_dir = os.path.join(agent.RUNS, out["run_id"]) if "run_id" in out else ""
        return out, seen.get("req"), run_dir
    return go


def _install_modules(monkeypatch, tmp_path, any_exist=True, spec_answer=None):
    root = str(tmp_path / "ws" / "specialists")
    store = types.ModuleType("fused_render.specialists.store")
    store.any_exist = lambda: any_exist
    launch = types.ModuleType("fused_render.specialists.launch")
    launch.root = lambda: root
    calls = []

    def spec(key, file):
        calls.append((key, file))
        return spec_answer if spec_answer is not None else _spec()
    launch.spec = spec
    monkeypatch.setitem(sys.modules, "fused_render.specialists.store", store)
    monkeypatch.setitem(sys.modules, "fused_render.specialists.launch", launch)
    return root, calls


def test_start_refuses_specialist_when_off(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    _install_modules(monkeypatch, tmp_path)
    out, _, _ = start(str(tmp_path / "proj"), specialist="abc:main")
    assert out == {"error": "Specialists are off", "reason": "specialists_disabled"}
    assert not os.path.isdir(agent.RUNS) or not os.listdir(agent.RUNS)


def test_start_refuses_specialist_on_other_backends_and_breakout(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    _install_modules(monkeypatch, tmp_path)
    out, _, _ = start(str(tmp_path / "proj"), specialist="abc:main", backend="copilot")
    assert out["reason"] == "unsupported"
    monkeypatch.setattr(agent, "_breakout_pref", lambda: True)
    out, _, _ = start(str(tmp_path / "proj"), specialist="abc:main", breakout=True)
    assert out["reason"] == "unsupported"


def test_start_passes_launch_refusal(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    _, calls = _install_modules(monkeypatch, tmp_path, spec_answer={
        "error": "That folder is not the specialist's home.", "reason": "not_home"})
    out, _, _ = start(str(tmp_path / "proj"), specialist="abc:main")
    assert out == {"error": "That folder is not the specialist's home.", "reason": "not_home"}
    assert calls == [("abc:main", str(tmp_path / "proj"))]


def test_start_writes_the_spec_and_meta(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    root, _ = _install_modules(monkeypatch, tmp_path)
    home = os.path.join(root, "canvas")
    os.makedirs(home)
    out, req, run_dir = start(home, specialist="abc:breakout:b-0123456789ab", permission_mode="auto")
    assert "error" not in out, out
    assert json.load(open(os.path.join(run_dir, "specialist.json"))) == _spec()
    meta = json.load(open(os.path.join(run_dir, "meta.json")))
    assert meta["specialist"] == {"id": "abc", "kind": "breakout", "breakout_id": "b-0123456789ab"}
    assert not os.path.exists(os.path.join(run_dir, "specialists.on"))
    assert req["cli_mode"] == agent._backend_mode("claude", agent.PERMISSION_MODES["auto"])


def test_start_task_records_tools_when_live(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    root, _ = _install_modules(monkeypatch, tmp_path, any_exist=True)
    out, _, run_dir = start(str(tmp_path / "proj"))
    assert os.path.isfile(os.path.join(run_dir, "specialists.on"))
    # inside a home: never the task tools (a breakout cannot open a breakout)
    os.makedirs(os.path.join(root, "canvas"))
    out, _, run_dir = start(os.path.join(root, "canvas"))
    assert not os.path.exists(os.path.join(run_dir, "specialists.on"))
    # no specialist yet: no tools
    _install_modules(monkeypatch, tmp_path, any_exist=False)
    out, _, run_dir = start(str(tmp_path / "proj"))
    assert not os.path.exists(os.path.join(run_dir, "specialists.on"))


def test_start_research_breakout_gets_no_tools(agent, tmp_path, monkeypatch, start):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    _install_modules(monkeypatch, tmp_path, any_exist=True)
    monkeypatch.setattr(agent, "_breakout_pref", lambda: True)
    out, _, run_dir = start(str(tmp_path / "proj"), breakout=True)
    assert "error" not in out, out
    assert not os.path.exists(os.path.join(run_dir, "specialists.on"))


def test_main_passes_specialist(agent, monkeypatch):
    seen = {}
    monkeypatch.setattr(agent, "_start", lambda *a, **kw: seen.update(kw) or {"ok": 1})
    agent.main(action="start", file="/x", message="m", specialist="abc:main")
    assert seen["specialist"] == "abc:main"


# ------------------------------------------------------------------ _decide ---
def _parked(agent, tool):
    rd = os.path.join(agent.RUNS, "run1")
    os.makedirs(os.path.join(rd, "perm"))
    with open(os.path.join(rd, "perm", "req1.req.json"), "w") as fh:
        json.dump({"id": "req1", "tool": tool, "input": {"specialist": "abc", "problem": "p"}}, fh)
    return rd


BRIEF = json.dumps({"problem": "Sync stalls", "tried": "retry", "look_at": "canvas.toml"})


def test_decide_carries_the_edited_brief(agent, monkeypatch):
    rd = _parked(agent, "mcp__fused_render__specialist_breakout")
    monkeypatch.setattr(agent, "_alive", lambda run_dir: True)
    out = agent._decide("run1", "req1", "allow", "once", answers=BRIEF)
    assert out["brief"] == json.loads(BRIEF) and out["decision"] == "allow"
    on_disk = json.load(open(os.path.join(rd, "perm", "req1.res.json")))
    assert on_disk["brief"]["problem"] == "Sync stalls"


@pytest.mark.parametrize("answers", ["", "nope", json.dumps({"problem": "x"}),
                                     json.dumps({"problem": "x" * 1001, "tried": "", "look_at": ""})])
def test_decide_bad_brief_is_dropped(agent, monkeypatch, answers):
    _parked(agent, "mcp__fused_render__specialist_breakout")
    monkeypatch.setattr(agent, "_alive", lambda run_dir: True)
    out = agent._decide("run1", "req1", "allow", "once", answers=answers)
    assert out["decision"] == "allow" and "brief" not in out


def test_decide_brief_only_for_the_offer_tool(agent, monkeypatch):
    _parked(agent, "Bash")
    monkeypatch.setattr(agent, "_alive", lambda run_dir: True)
    out = agent._decide("run1", "req1", "allow", "once", answers=BRIEF)
    assert "brief" not in out
    assert set(out) == {"decided", "decision", "scope", "mode", "answers"}


def test_specialists_pref_env_wins(agent, tmp_path, monkeypatch):
    home = tmp_path / "prefs"
    home.mkdir()
    (home / "prefs.json").write_text(json.dumps({"specialists_enabled": True}))
    monkeypatch.setattr(agent, "_home_dir", lambda: str(home))
    monkeypatch.delenv("FUSED_RENDER_SPECIALISTS", raising=False)
    assert agent._specialists_pref()
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert not agent._specialists_pref()
    (home / "prefs.json").write_text(json.dumps({"specialists_enabled": "yes"}))
    monkeypatch.delenv("FUSED_RENDER_SPECIALISTS")
    assert not agent._specialists_pref()


def test_status_line_names_the_specialist():
    """The task's status line can say "Asking the Canvas specialist…": the specialist tools' detail is the
    `specialist` argument; other tools are unchanged."""
    from fused_render.claude_agent import agent_module

    agent = agent_module()
    for tool in ("specialist_check", "specialist_pull", "specialist_note", "specialist_breakout"):
        assert agent._tool_detail("mcp__fused_render__" + tool, {"specialist": "Canvas", "stage": "plan"}) == "Canvas"
    assert agent._tool_detail("mcp__fused_render__specialist_list", {}) == ""
    assert agent._tool_detail("mcp__fused_render__specialist_pull", {"specialist": 3}) == ""
    assert agent._tool_detail("mcp__other__x", {"specialist": "Canvas"}) == ""
    assert agent._tool_detail("Grep", {"pattern": "foo"}) == "foo"
