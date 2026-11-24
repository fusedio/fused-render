"""agent.py's expert seams: `_claude_argv`, `_write_mcp_config`, `_start`, `_decide`, `main`.

With no `experts.on` / `expert.json` in the run dir (the flag off) the argv and mcp.json are exactly
main's. A task run gets pull and note, read rules for memory and skills, an ask rule for the breakout tool and the
folder's "call expert_pull first" sentence; an expert's own chat gets its launch spec. The `claude` CLI is
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
BREAKOUT_TOOL = "mcp__fused_render__expert_breakout"


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
    assert "expert" not in (json.dumps(argv) + mcp).lower()


TASK = {"root": "/ws/experts", "all": [["Canvas", "Fused canvases"], ["Data", ""]], "here": []}


def test_a_task_s_prompt_names_the_experts_and_its_folder_s_one(agent, rd):
    (rd / "experts.on").write_text(json.dumps(TASK))
    prompt = _after(_argv(agent, rd), "--append-system-prompt")
    assert "knows one subject: Canvas (Fused canvases); Data. Call " in prompt and "Before anything" not in prompt
    (rd / "experts.on").write_text(json.dumps(dict(TASK, here=["Canvas"])))
    assert _after(_argv(agent, rd), "--append-system-prompt").endswith(
        'Before anything else call expert_pull with expert "Canvas".')


def test_task_tools_need_an_origin(agent, rd, monkeypatch):
    (rd / "experts.on").write_text(json.dumps(TASK))
    monkeypatch.setattr(agent, "_origin", lambda: "")
    assert "expert" not in json.dumps(_argv(agent, rd))


# ------------------------------------------------------------- launch spec ---
SPEC = {"kind": "main",
        "mcp_servers": {"expert_services": {"command": "py", "args": ["botmcp.py"]},
                        "fused_render": {"command": "evil"}},
        "append": "You are the Canvas expert's main chat.", "policy_env": {}}


def test_a_spec_folds_servers_and_prompt(agent, rd):
    (rd / "expert.json").write_text(json.dumps(SPEC))
    (rd / "experts.on").write_text(json.dumps(TASK))  # a spec wins
    argv = _argv(agent, rd, file="/w/experts/canvas", cli_mode="auto")
    assert "ask" not in _settings(argv).get("permissions", {})
    assert "expert_pull" not in json.dumps(argv)
    assert _after(argv, "--append-system-prompt").endswith(" You are the Canvas expert's main chat.")
    servers = _mcp(rd)
    assert servers["expert_services"]["args"] == ["botmcp.py"]
    assert servers["fused_render"]["command"] != "evil"  # ours wins a collision
    assert _after(argv, "--permission-mode") == "auto"  # the user's pill


def test_a_spec_s_gated_tools_ask_in_every_mode(agent, rd):
    gated = "mcp__expert_canvas__canvas_create"
    (rd / "expert.json").write_text(json.dumps(dict(SPEC, ask=[gated])))
    for mode in ("auto", "acceptEdits", None):
        argv = _argv(agent, rd, file="/w/experts/canvas", cli_mode=mode)
        assert _settings(argv)["permissions"]["ask"] == [gated]


def test_a_spec_can_hand_over_the_workbench_skills(agent, rd, monkeypatch):
    monkeypatch.setattr(agent, "_workbench_plugin_dir", lambda: "/plugins/workbench")
    (rd / "expert.json").write_text(json.dumps(SPEC))
    assert "/plugins/workbench" not in _argv(agent, rd, file="/w/experts/canvas")  # a home is no canvas clone
    (rd / "expert.json").write_text(json.dumps(dict(SPEC, workbench_skills=True)))
    argv = _argv(agent, rd, file="/w/experts/canvas")
    assert argv[argv.index("/plugins/workbench") - 1] == "--plugin-dir"


def test_an_update_spec_carries_the_unattended_env(agent, rd):
    spec = dict(SPEC, kind="update", append="", policy_env={"FUSED_EXPERT_UNATTENDED": "1"})
    (rd / "expert.json").write_text(json.dumps(spec))
    argv = _argv(agent, rd, file="/w/experts/canvas", cli_mode="auto")
    assert _mcp(rd)["fused_render"]["env"]["FUSED_EXPERT_UNATTENDED"] == "1"
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
    """Stand-ins for the server's experts modules, as agent.py reads them off `sys.modules`."""
    root = str(tmp_path / "ws" / "experts")
    os.makedirs(os.path.join(root, "canvas"))
    paths = types.ModuleType("paths")
    paths.root = lambda: root
    paths.is_inside_root = lambda p: os.path.abspath(p).startswith(root)
    store = types.ModuleType("store")
    store.all = lambda: [types.SimpleNamespace(name="Canvas", about="Fused canvases")]
    store.for_folder = lambda p: ["Canvas"] if p == str(tmp_path / "proj") else []
    launch = types.ModuleType("launch")
    launch.calls = []
    launch.spec = lambda key, file: launch.calls.append((key, file)) or dict(
        SPEC, kind=key.split(":")[1], home=file)
    for name, mod in (("paths", paths), ("store", store), ("launch", launch)):
        monkeypatch.setitem(sys.modules, "fused_render.experts." + name, mod)
    monkeypatch.setenv("FUSED_RENDER_EXPERTS", "1")
    return types.SimpleNamespace(root=root, home=os.path.join(root, "canvas"), launch=launch, store=store)


def test_flag_off_starts_record_nothing_and_refuse_expert_keys(agent, start, modules, monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_EXPERTS", "0")
    out, run_dir = start(str(tmp_path / "proj"))
    assert "error" not in out
    assert not {"experts.on", "expert.json"} & set(os.listdir(run_dir))
    assert start(modules.home, expert="canvas:main")[0]["reason"] == "experts_disabled"
    assert "error" not in start(modules.home)[0]  # a home is an ordinary folder with the flag off


def test_a_task_records_the_root_and_its_folders_experts(agent, start, modules, tmp_path):
    _, run_dir = start(str(tmp_path / "proj"))
    assert json.load(open(os.path.join(run_dir, "experts.on"))) == {
        "root": modules.root, "all": [["Canvas", "Fused canvases"]], "here": ["Canvas"]}
    modules.store.all = lambda: []
    _, run_dir = start(str(tmp_path / "proj"))
    assert not os.path.exists(os.path.join(run_dir, "experts.on"))


def test_a_plain_start_in_a_home_is_refused(agent, start, modules):
    out, _ = start(modules.home)
    assert out == {"error": "This folder is an expert's home; open it from the Bots page.",
                   "reason": "expert_home"}


def test_main_chat_writes_its_spec(agent, start, modules):
    out, run_dir = start(modules.home, expert="canvas:main")
    assert modules.launch.calls == [("canvas:main", modules.home)]
    assert json.load(open(os.path.join(run_dir, "expert.json")))["kind"] == "main"
    assert not os.path.exists(os.path.join(run_dir, "experts.on"))


def test_a_expert_breakout_is_not_research_only(agent, start, modules):
    out, run_dir = start(modules.home, expert="canvas:breakout", breakout=True)
    assert "error" not in out, out
    meta = json.load(open(os.path.join(run_dir, "meta.json")))
    assert "breakout" not in meta
    assert json.load(open(os.path.join(run_dir, "expert.json")))["kind"] == "breakout"
    # still a fresh breakout: no resume of an unrecorded session
    assert start(modules.home, expert="canvas:breakout", breakout=True,
                 session_id="s1")[0]["reason"] == "breakout_fresh_only"


def test_launch_refusals_pass_through(agent, start, modules):
    modules.launch.spec = lambda key, file: {"error": "That expert no longer exists", "reason": "no_expert"}
    assert start(modules.home, expert="gone:main")[0] == {"error": "That expert no longer exists",
                                                             "reason": "no_expert"}


def test_breakout_is_on_with_experts(agent, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_EXPERTS", "1")
    assert agent._breakout_pref()
    monkeypatch.setenv("FUSED_RENDER_EXPERTS", "0")
    assert not agent._breakout_pref()


def test_main_passes_main_and_breakout_keys_but_never_an_update(agent, monkeypatch):
    seen = []
    monkeypatch.setattr(agent, "_start", lambda *a, **kw: seen.append(kw["expert"]) or {})
    for key in ("canvas:main", "canvas:breakout", "canvas:update"):
        agent.main(action="start", file="/x", message="m", expert=key)
    assert seen == ["canvas:main", "canvas:breakout", ""]


def test_a_message_waits_while_its_expert_learns(agent, monkeypatch):
    import sys
    import types

    run_dir = os.path.join(agent.RUNS, "main1")
    os.makedirs(run_dir)
    for name, data in (("expert.json", {"kind": "main"}), ("meta.json", {"file": "/w/experts/canvas"})):
        with open(os.path.join(run_dir, name), "w") as fh:
            json.dump(data, fh)
    learn = types.SimpleNamespace(LEARNING="learning", learning=lambda home: home == "/w/experts/canvas")
    monkeypatch.setitem(sys.modules, "fused_render.experts.update", learn)
    assert agent._send("main1", "hello") == {"error": "learning"}


def test_status_line_names_the_expert(agent):
    assert agent._tool_detail("mcp__fused_render__expert_note", {"expert": "Canvas"}) == "Canvas"
    assert agent._tool_detail("mcp__fused_render__expert_pull", {}) == ""
    assert agent._tool_detail("mcp__other__x", {"expert": "Canvas"}) == ""


@pytest.mark.parametrize("theirs, commits", [(True, 0), (False, 1), (ValueError("git"), 1)])
def test_a_turn_end_commits_the_task_unless_the_experts_took_it(agent, monkeypatch, theirs, commits):
    """A failure in the experts' turn-end work never costs a turn its own commit."""
    run_dir = os.path.join(agent.RUNS, "run-1")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w") as fh:
        json.dump({"file": "/f", "message": "m", "backend": "claude"}, fh)
    with open(os.path.join(run_dir, "out.jsonl"), "w") as fh:
        fh.write(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}) + "\n")
    history = types.ModuleType("history")

    def turn_ended(target, session_id):
        if isinstance(theirs, Exception):
            raise theirs
        return theirs

    history.turn_ended = turn_ended
    monkeypatch.setitem(sys.modules, "fused_render.experts.history", history)
    calls = []
    monkeypatch.setattr(agent, "_commit_turn", lambda *a: calls.append(a))
    assert agent._poll("run-1")["done"] and len(calls) == commits
