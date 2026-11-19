"""agent.py's specialist seams: `_claude_argv`, `_write_mcp_config`, `_start`, `_decide`, `main`.

With no `specialists.on` / `specialist.json` in the run dir (the flag off) the argv and mcp.json are exactly
main's. A task run gets pull and note, read rules for memory and skills, an ask rule for the breakout tool and the
folder's "call specialist_pull first" sentence; a specialist's own chat gets its launch spec. The `claude` CLI is
never run.
"""
import importlib.util
import json
import os
import sys
import types

import pytest

AGENT = os.path.join("fused_render", "claude_agent", "agent.py")
ORIGIN = "http://127.0.0.1:4321"
BREAKOUT_TOOL = "mcp__fused_render__specialist_breakout"


@pytest.fixture
def agent(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sp_argv_agent", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(mod, "_claude_bin", lambda: "/bin/claude")
    monkeypatch.setattr(mod, "_origin", lambda: ORIGIN)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "frhome"))
    return mod


@pytest.fixture
def rd(tmp_path):
    (tmp_path / "rd" / "perm").mkdir(parents=True)
    return tmp_path / "rd"


def _argv(agent, rd, **kw):
    args = dict(run_dir=str(rd), pane=False, cli_mode=None, session_id="", model="", effort="",
                extra_read_dirs=[], file="/work/proj/f.py", new_session_id="N")
    args.update(kw)
    return agent._claude_argv(**args)


def _after(argv, flag):
    return argv[argv.index(flag) + 1]


def _settings(argv):
    return json.loads(_after(argv, "--settings"))


def _mcp(rd):
    return json.loads((rd / "mcp.json").read_text())["mcpServers"]


# ---------------------------------------------------------------- flag off ---
@pytest.mark.parametrize("kw", [{}, {"breakout": True}, {"session_id": "S", "new_session_id": ""},
                                {"cli_mode": "acceptEdits"}])
def test_argv_identical_without_the_run_dir_files(agent, rd, monkeypatch, kw):
    argv = _argv(agent, rd, **kw)
    mcp = (rd / "mcp.json").read_text()
    # The same build with the seams forced off is byte-identical: they add nothing when the files are absent.
    monkeypatch.setattr(agent, "_read_run_json", lambda run_dir, name: None)
    assert _argv(agent, rd, **kw) == argv
    assert (rd / "mcp.json").read_text() == mcp
    assert "specialist" not in (json.dumps(argv) + mcp).lower()


def test_task_tools_need_an_origin(agent, rd, monkeypatch):
    (rd / "specialists.on").write_text(json.dumps({"root": "/ws/specialists", "here": []}))
    monkeypatch.setattr(agent, "_origin", lambda: "")
    assert "specialist" not in json.dumps(_argv(agent, rd))


# ------------------------------------------------------------- launch spec ---
SPEC = {"kind": "main",
        "mcp_servers": {"specialist_services": {"command": "py", "args": ["botmcp.py"]},
                        "fused_render": {"command": "evil"}},
        "append": "You are the Canvas specialist's main chat.", "policy_env": {}}


def test_a_spec_folds_servers_and_prompt(agent, rd):
    (rd / "specialist.json").write_text(json.dumps(SPEC))
    (rd / "specialists.on").write_text(json.dumps({"root": "/w/specialists", "here": []}))  # a spec wins
    argv = _argv(agent, rd, file="/w/specialists/canvas", cli_mode="auto")
    assert "ask" not in _settings(argv).get("permissions", {})
    assert "specialist_pull" not in json.dumps(argv)
    assert _after(argv, "--append-system-prompt").endswith(" You are the Canvas specialist's main chat.")
    servers = _mcp(rd)
    assert servers["specialist_services"]["args"] == ["botmcp.py"]
    assert servers["fused_render"]["command"] != "evil"  # ours wins a collision
    assert _after(argv, "--permission-mode") == "auto"  # the user's pill


def test_an_update_spec_carries_the_unattended_env(agent, rd):
    spec = dict(SPEC, kind="update", append="", policy_env={"FUSED_SPECIALIST_UNATTENDED": "1"})
    (rd / "specialist.json").write_text(json.dumps(spec))
    argv = _argv(agent, rd, file="/w/specialists/canvas", cli_mode="auto")
    assert _mcp(rd)["fused_render"]["env"]["FUSED_SPECIALIST_UNATTENDED"] == "1"
    assert _after(argv, "--permission-mode") == "auto"


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
    monkeypatch.setattr(agent.subprocess, "Popen", lambda cmd, **kw: _HostProc({}))
    monkeypatch.setattr(agent, "_backend_enabled", lambda b: True)

    def go(file, **kw):
        out = agent._start(file, "hello", kw.pop("session_id", ""), "", "", backend="claude", **kw)
        return out, os.path.join(agent.RUNS, out["run_id"]) if "run_id" in out else ""
    return go


@pytest.fixture
def modules(monkeypatch, tmp_path):
    """Stand-ins for the server's specialists modules, as agent.py reads them off `sys.modules`."""
    root = str(tmp_path / "ws" / "specialists")
    os.makedirs(os.path.join(root, "canvas"))
    paths = types.ModuleType("paths")
    paths.root = lambda: root
    paths.is_inside_root = lambda p: os.path.abspath(p).startswith(root)
    store = types.ModuleType("store")
    store.all = lambda: ["Canvas"]
    store.for_folder = lambda p: ["Canvas"] if p == str(tmp_path / "proj") else []
    launch = types.ModuleType("launch")
    launch.calls = []
    launch.spec = lambda key, file: launch.calls.append((key, file)) or dict(
        SPEC, kind=key.split(":")[1], home=file)
    for name, mod in (("paths", paths), ("store", store), ("launch", launch)):
        monkeypatch.setitem(sys.modules, "fused_render.specialists." + name, mod)
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    return types.SimpleNamespace(root=root, home=os.path.join(root, "canvas"), launch=launch, store=store)


def test_flag_off_starts_record_nothing_and_refuse_specialist_keys(agent, start, modules, monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    out, run_dir = start(str(tmp_path / "proj"))
    assert "error" not in out
    assert not {"specialists.on", "specialist.json"} & set(os.listdir(run_dir))
    assert start(modules.home, specialist="canvas:main")[0]["reason"] == "specialists_disabled"
    assert "error" not in start(modules.home)[0]  # a home is an ordinary folder with the flag off


def test_a_task_records_the_root_and_its_folders_specialists(agent, start, modules, tmp_path):
    _, run_dir = start(str(tmp_path / "proj"))
    assert json.load(open(os.path.join(run_dir, "specialists.on"))) == {"root": modules.root, "here": ["Canvas"]}
    modules.store.all = lambda: []
    _, run_dir = start(str(tmp_path / "proj"))
    assert not os.path.exists(os.path.join(run_dir, "specialists.on"))


def test_a_plain_start_in_a_home_is_refused(agent, start, modules):
    out, _ = start(modules.home)
    assert out == {"error": "This folder is a specialist's home; open it from the Bots page.",
                   "reason": "specialist_home"}


def test_main_chat_writes_its_spec(agent, start, modules):
    out, run_dir = start(modules.home, specialist="canvas:main")
    assert modules.launch.calls == [("canvas:main", modules.home)]
    assert json.load(open(os.path.join(run_dir, "specialist.json")))["kind"] == "main"
    assert not os.path.exists(os.path.join(run_dir, "specialists.on"))


def test_a_specialist_breakout_is_not_research_only(agent, start, modules):
    out, run_dir = start(modules.home, specialist="canvas:breakout", breakout=True)
    assert "error" not in out, out
    meta = json.load(open(os.path.join(run_dir, "meta.json")))
    assert "breakout" not in meta
    assert json.load(open(os.path.join(run_dir, "specialist.json")))["kind"] == "breakout"
    # still a fresh breakout: no resume of an unrecorded session
    assert start(modules.home, specialist="canvas:breakout", breakout=True,
                 session_id="s1")[0]["reason"] == "breakout_fresh_only"


def test_launch_refusals_pass_through(agent, start, modules):
    modules.launch.spec = lambda key, file: {"error": "That specialist no longer exists", "reason": "no_specialist"}
    assert start(modules.home, specialist="gone:main")[0] == {"error": "That specialist no longer exists",
                                                             "reason": "no_specialist"}


def test_breakout_is_on_with_specialists(agent, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "1")
    assert agent._breakout_pref()
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    assert not agent._breakout_pref()


def test_main_passes_main_and_breakout_keys_but_never_an_update(agent, monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "_start", lambda *a, **kw: seen.append(kw["specialist"]) or {})
    for key in ("canvas:main", "canvas:breakout", "canvas:update"):
        agent.main(action="start", file="/x", message="m", specialist=key)
    assert seen == ["canvas:main", "canvas:breakout", ""]


def test_a_message_waits_while_its_specialist_learns(agent, monkeypatch):
    import sys
    import types

    run_dir = os.path.join(agent.RUNS, "main1")
    os.makedirs(run_dir)
    for name, data in (("specialist.json", {"kind": "main"}), ("meta.json", {"file": "/w/specialists/canvas"})):
        with open(os.path.join(run_dir, name), "w") as fh:
            json.dump(data, fh)
    learn = types.SimpleNamespace(LEARNING="learning", learning=lambda home: home == "/w/specialists/canvas")
    monkeypatch.setitem(sys.modules, "fused_render.specialists.update", learn)
    assert agent._send("main1", "hello") == {"error": "learning"}


def test_status_line_names_the_specialist(agent):
    assert agent._tool_detail("mcp__fused_render__specialist_note", {"specialist": "Canvas"}) == "Canvas"
    assert agent._tool_detail("mcp__fused_render__specialist_pull", {}) == ""
    assert agent._tool_detail("mcp__other__x", {"specialist": "Canvas"}) == ""
