"""sysmon/labels.py: command-line naming rules and the live-runs reader."""
import os

from fused_render import executor
from fused_render.sysmon import labels


def test_template_script_names_template_and_stem(tmp_path, monkeypatch):
    core = tmp_path / "core"
    (core / "map").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_CORE_TEMPLATES", str(core))
    script = str(core / "map" / "daemon.py")
    args = ["/usr/bin/python3.12", script]
    assert labels.command_name(args) == "map template › daemon"
    owner = labels.classify(1, args)
    assert owner.kind == "template" and owner.label == "map template › daemon"


def test_fused_render_module_keeps_existing_style():
    assert labels.command_name(["python", "-m", "fused_render.index.worker"]) == "fused-render index worker"


def test_project_venv_interpreter_names_project_and_script(tmp_path):
    py = tmp_path / "proj" / ".venv" / "bin" / "python"
    assert labels.command_name([str(py), "/x/y/serve.py"]) == "proj › serve.py"
    py2 = tmp_path / "other" / "venv" / "bin" / "python3"
    assert labels.command_name([str(py2), "run.py"]) == "other › run.py"


def test_shared_venv_interpreter_uses_parent_dir_and_script(tmp_path, monkeypatch):
    from fused_render import projectenv
    root = tmp_path / "venvs"
    monkeypatch.setattr(projectenv, "venvs_root", lambda: str(root))
    py = root / "abc123" / "bin" / "python"
    assert labels.command_name([str(py), "/w/tool/job.py"]) == "tool/job.py"


def test_dash_c_shows_collapsed_first_40_chars():
    code = "import   os\nprint(os.getpid())  # " + "x" * 80
    got = labels.command_name(["python3", "-c", code])
    assert got.startswith('python -c "import os print(os.getpid())')
    assert got.endswith('"') and len(got) <= len('python -c ""') + 40


def test_plain_script_uses_parent_dir_and_name():
    assert labels.command_name(["python3", "/a/b/tool.py"]) == "b/tool.py"
    assert labels.command_name(["/bin/zsh", "-l"]) == "zsh"
    assert labels.command_name(None) == "process"


def test_runs_reader_labels_live_child(monkeypatch):
    monkeypatch.setattr(executor, "active_runs",
                        lambda: {4242: ("/w/My App", "/w/My App/report.py")})
    out = {}
    labels._runs(out)
    assert out[4242].kind == "run"
    assert out[4242].label == "Run: My App › report.py"
    assert labels._runs in labels._READERS


def test_engine_under_templates_dir_is_named_template_engine(tmp_path, monkeypatch):
    core = tmp_path / "core"
    (core / "map").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_CORE_TEMPLATES", str(core))
    assert labels.engine_label(str(core / "map"), "e1") == "map template engine"
    assert labels.engine_label("/w/app", "e1") == "Engine: app"
    assert labels.engine_label("", "e1") == "Engine: e1"


def _p(pid, ppid, cpu, mem, label="Claude", fused=True):
    return {"pid": pid, "ppid": ppid, "label": label, "cpu": cpu, "mem": mem, "fused": fused}


def test_claude_summary_splits_fused_and_other_and_sums_subtrees():
    clis = [_p(10, 1, 5.0, 100, "Claude: a"), _p(20, 1, 1.0, 50, "Claude", fused=False)]
    procs = clis + [_p(11, 10, 20.0, 400, "node"), _p(12, 11, 10.0, 300, "tool"),
                    _p(21, 20, None, 25, "node")]
    got = labels.claude_summary(clis, procs)
    assert (got["total"], got["fused"], got["other"]) == (2, 1, 1)
    assert got["mem_bytes"] == 100 + 400 + 300 + 50 + 25
    assert got["cpu_pct"] == 5.0 + 20.0 + 10.0 + 1.0
    assert got["top"][0] == {"pid": 10, "label": "Claude: a", "mem": 800, "cpu": 35.0}
    assert labels.claude_summary([], [])["total"] == 0


def test_claude_summary_survives_a_ppid_cycle():
    a, b = _p(1, 2, 1.0, 1), _p(2, 1, 1.0, 1)
    assert labels.claude_summary([a], [a, b])["total"] == 1


def test_classify_template_matches_classify_for_templates_and_skips_the_session_lookup(tmp_path, monkeypatch):
    core = tmp_path / "core"
    (core / "map").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_CORE_TEMPLATES", str(core))
    monkeypatch.setattr(labels, "_claude_session_name",
                        lambda pid: (_ for _ in ()).throw(AssertionError("session lookup")))
    args = ["/usr/bin/python3.12", str(core / "map" / "daemon.py")]
    owner = labels.classify_template(args)
    assert owner.kind == "template" and owner.label == "map template › daemon"
    assert labels.classify_template(["/usr/local/bin/claude", "--x"]) is None
    assert labels.classify_template(["/usr/bin/python3", "/tmp/_child.py"]) is None
    assert labels.classify_template(["/bin/ls"]) is None and labels.classify_template(None) is None


def test_classify_labels_a_warm_run_spare_idle(monkeypatch):
    """A pre-spawned interpreter (SPEC PY-20) is named for what it is, not left
    as an anonymous `python -c ...` / bare `_child.py`; once claimed it leaves
    the spare set and gets the ordinary run label."""
    from fused_render import warm_run

    args = ["/usr/bin/python3", "/pkg/_child.py"]
    monkeypatch.setattr(warm_run, "spare_pids", lambda: [4242])
    assert labels.classify(4242, args).label == "Warm Python (idle)"
    assert labels.classify(4243, args).label == "Run: Python"
    assert labels.classify(4242, ["/usr/bin/python3", "-c", "x"]).label == "Warm Python (idle)"


# ---- Claude trees named by task ----------------------------------------------------

def test_task_label_is_id_and_trimmed_title():
    assert labels.task_label({"task_id": "TASK-075", "title": "Continue after usage limit"}) \
        == "TASK-075 · Continue after usage limit"
    long = labels.task_label({"task_id": "TASK-1", "title": "word " * 30})
    assert long.startswith("TASK-1 · word") and long.endswith("…")
    assert len(long) == len("TASK-1 · ") + 48
    assert labels.task_label({"task_id": "", "title": "Just a title"}) == "Just a title"


def test_task_index_reads_the_snapshot_by_key_and_session(monkeypatch):
    from fused_render.server.routers import tasks as tasks_router
    rows = [{"key": "k1", "session_id": "s1", "task_id": "TASK-1", "title": "a"}]
    monkeypatch.setattr(tasks_router, "snapshot_rows", lambda: rows)
    monkeypatch.setattr(labels, "_task_cache", None)
    assert labels._task_for(["", "s1"])["task_id"] == "TASK-1"
    assert labels._task_for(["k1"])["task_id"] == "TASK-1"
    assert labels._task_for(["nope"]) is None


def test_task_index_without_a_listing_names_nothing(monkeypatch):
    from fused_render.server.routers import tasks as tasks_router

    def boom():
        raise RuntimeError("no listing")
    monkeypatch.setattr(tasks_router, "snapshot_rows", boom)
    monkeypatch.setattr(labels, "_task_cache", None)
    assert labels._task_for(["s1"]) is None


def _child_label(args, parent_part=""):
    root = labels.Owner("claude", "TASK-075 · Fix it", {}, base="TASK-075 · Fix it")
    parent = root if not parent_part else labels.Owner(
        "claude", "x", {}, base=root.base, part=parent_part)
    return labels.claude_child(parent, args).label


def test_claude_descendants_read_as_task_and_part():
    base = "TASK-075 · Fix it › "
    assert _child_label(["npm exec next-devtools-mcp@latest", "", ""]) == base + "MCP next-devtools"
    assert _child_label(["npm exec @accesslint/mcp@latest"]) == base + "MCP accesslint"
    assert _child_label(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/w"]) \
        == base + "MCP filesystem"
    assert _child_label(["node", "/u/.npm/_npx/ab/node_modules/.bin/accesslint-mcp"]) \
        == base + "MCP accesslint"
    # npm's own node child keeps its parent's name, it does not chain.
    assert _child_label(["node", "/x/server.js"], "MCP next-devtools") == base + "MCP next-devtools"
    assert _child_label(["/app/python", "/lib/fused_render/claude_agent/permission_server.py"]) \
        == base + "permissions"
    assert _child_label(["/usr/bin/git", "status"]) == base + "git"


def test_claude_bash_tool_shells_read_as_shell_and_leaf_command():
    base = "TASK-075 · Fix it › "
    wrapper = ["/bin/sh", "/lib/fused_render/claude_shell_prefix.sh",
               "source /u/.claude/shell-snapshots/s.sh && eval 'cd /w/repo && sleep 300' < /dev/null && pwd -P >| /tmp/c"]
    assert _child_label(wrapper) == base + "shell: sleep 300"
    for cd in ('cd "/w/my project"', "cd /w/my\\ project"):
        quoted = ["/bin/sh", "/lib/fused_render/claude_shell_prefix.sh",
                  f"eval '{cd} && sleep 300' < /dev/null"]
        assert _child_label(quoted) == base + "shell: sleep 300"
    zsh = ["/bin/zsh", "-c", "source /u/.claude/shell-snapshots/s.sh; true"]
    assert _child_label(zsh) == base + "shell"
    assert _child_label(["tee", "-a", "/tmp/fused_render_claude-501/claude-cmds/s/1.out"]) \
        == base + "shell"
    assert _child_label(["sleep", "300"], "shell") == base + "shell: sleep 300"
    assert _child_label(["python3", "-c", "x"], "shell: sleep 300") == base + "shell: python3 -c x"


def test_each_app_one_shot_claude_is_named_for_its_launcher(monkeypatch):
    """merge_job.resolver_argv and contrib.ai._run_cli are not the titles helper."""
    import json
    from fused_render.contrib import merge_job
    monkeypatch.setattr(labels, "_claude_session_name", lambda pid: "")
    resolver = merge_job.resolver_argv("/u/claude", "/tmp/box")
    assert labels.classify(7, resolver).label == labels.MERGE_LABEL
    contrib = ["/u/claude", "-p", "--no-session-persistence", "--max-turns", "1", "--tools=",
               "--setting-sources=", "--strict-mcp-config", "--mcp-config",
               json.dumps({"mcpServers": {}}), "--model", "m", "--system-prompt", "s"]
    assert labels.classify(8, contrib).label == labels.CONTRIB_LABEL
    recap = ["/u/claude", "-p", "--no-session-persistence", "--max-turns", "1", "--tools", "",
             "--model", "haiku", "--setting-sources=", "--strict-mcp-config", "--system-prompt", "s"]
    assert labels.classify(9, recap).label == labels.HELPER_LABEL


def test_the_apps_one_shot_claude_is_the_helper(monkeypatch):
    monkeypatch.setattr(labels, "_claude_session_name", lambda pid: "")
    helper = labels.classify(5, ["/u/claude", "-p", "--model", "claude-haiku", "--tools=",
                                 "--no-session-persistence"])
    assert (helper.kind, helper.label, helper.ref) == ("claude", labels.HELPER_LABEL, {"helper": True})
    fork = labels.classify(6, ["/u/claude", "--fork-session", "--no-session-persistence"])
    assert fork.label == "Claude CLI" and not fork.ref.get("helper")


def test_bg_jobs_prefer_the_task_row_then_the_run_dir(tmp_path):
    import json
    (tmp_path / "bg_tasks.json").write_text(json.dumps({"tasks": {
        "a": {"id": "a", "description": "poll CI", "status": "running"},
        "b": {"id": "b", "description": "done one", "status": "completed"}}}))
    assert labels._bg_jobs(str(tmp_path), {}) == [{"id": "a", "description": "poll CI"}]
    row = {"background_jobs": {"count": 1, "jobs": [{"id": "z", "title": "watch PR"}]}}
    assert labels._bg_jobs(str(tmp_path), row) == [{"id": "z", "description": "watch PR"}]
    assert labels._bg_jobs(str(tmp_path), {"background_jobs": None, "task_id": "T"}) == []


def _row(pid, ppid, mem, cpu=1.0):
    return {"pid": pid, "ppid": ppid, "memBytes": mem, "cpuPct": cpu, "startedAt": 1000.0 + pid}


def test_claude_tasks_rolls_each_tree_up_largest_first():
    done = labels.Owner("claude", "TASK-2 · b", {"run_id": "r2", "task": {
        "task_id": "TASK-2", "state": "done", "bg_jobs": [{"id": "x", "description": "poll"}]}},
        base="TASK-2 · b")
    running = labels.Owner("claude", "TASK-1 · a", {"run_id": "r1", "task": {
        "task_id": "TASK-1", "state": "running", "bg_jobs": []}}, base="TASK-1 · a")
    # host 10 -> cli 11 -> mcp 12 -> node 13; host 20 -> cli 21 -> shell 22; helper 30.
    procs = [_row(10, 1, 100), _row(11, 10, 200), _row(12, 11, 50), _row(13, 12, 50, None),
             _row(20, 1, 100), _row(21, 20, 900), _row(22, 21, 10),
             _row(30, 1, 120, 0.5), _row(40, 1, 5000)]
    roots = {10: running, 11: running, 20: done, 21: done}
    got = labels.claude_tasks(roots, {30: labels.HELPER_LABEL}, procs)
    assert [t["root_pid"] for t in got] == [20, 10, 30]
    t2, t1, helper = got
    assert (t2["mem_bytes"], t2["pids"], t2["done_alive"]) == (1010, [20, 21, 22], True)
    assert t2["bg_jobs"] == [{"id": "x", "description": "poll"}] and t2["label"] == "TASK-2 · b"
    assert (t1["mem_bytes"], t1["cpu_pct"], t1["done_alive"]) == (400, 3.0, False)
    assert t1["started_at"] == 1010.0
    assert (helper["helper"], helper["label"], helper["key"]) == (True, labels.HELPER_LABEL, "")
    assert labels.claude_tasks({}, {}, []) == []


def test_a_claude_run_is_labelled_by_the_task_its_host_session_belongs_to(tmp_path, monkeypatch):
    """A resumed run's meta.json names only the session it resumed; host.json
    carries the live one, which is the session the task row holds now."""
    import json
    from fused_render import project_queue
    from fused_render.server.routers import tasks as tasks_router
    run = tmp_path / "20261029-1"
    run.mkdir()
    (run / "pid").write_text("501")
    (run / "host.json").write_text(json.dumps({"pid": 500, "session_id": "s-new", "model": "sonnet"}))
    meta = {"file": "/w/fused-render", "resumed_from": "s-old", "message": "hi"}
    monkeypatch.setattr(project_queue, "agent_module", lambda: object())
    monkeypatch.setattr(project_queue, "scan_runs", lambda agent: [
        {"run_id": run.name, "run_dir": str(run), "meta": meta, "sessions": {"s-old"}}])
    monkeypatch.setattr(labels, "_is_run_process", lambda b, pid, d: True)
    monkeypatch.setattr(labels, "_claude_session_id", lambda pid: "")
    monkeypatch.setattr(labels, "_claude_session_row", lambda pid: {})
    monkeypatch.setattr(labels, "_task_cache", None)
    monkeypatch.setattr(tasks_router, "snapshot_rows", lambda: [{
        "key": "s-first", "session_id": "s-new", "task_id": "TASK-075", "title": "Continue",
        "status": "done", "project": "/w/fused-render", "target": "/w/fused-render",
        "background_jobs": {"count": 1, "jobs": [{"id": "b", "title": "poll"}]}}])

    class B:
        def is_zombie(self, pid): return False
        def proc_sample(self, pid): return object()
        def parent_pid(self, pid): return 500
        def proc_args(self, pid): return ["/py", "/lib/claude_agent/session_host.py"]

    out = labels._claude_runs(B())
    assert set(out) == {500, 501}
    owner = out[501]
    assert owner.label == owner.base == "TASK-075 · Continue"
    task = owner.ref["task"]
    assert (task["task_id"], task["state"], task["model"], task["folder"]) == \
        ("TASK-075", "done", "sonnet", "fused-render")
    assert task["bg_jobs"] == [{"id": "b", "description": "poll"}]
    assert owner.ref["run_id"] == run.name


def test_a_resumed_run_is_not_named_for_the_task_it_resumed(tmp_path, monkeypatch):
    """`resumed_from` is the parent conversation — possibly another task. The
    run's own sessions (live pid, host.json, meta, launch.json) win over it."""
    import json
    from fused_render import project_queue
    from fused_render.server.routers import tasks as tasks_router
    run = tmp_path / "20261029-2"
    run.mkdir()
    (run / "pid").write_text("601")
    (run / "launch.json").write_text(json.dumps({"session_id": "", "new_session_id": "s-child"}))
    meta = {"file": "/w/x", "resumed_from": "s-parent"}
    monkeypatch.setattr(project_queue, "agent_module", lambda: object())
    monkeypatch.setattr(project_queue, "scan_runs", lambda agent: [
        {"run_id": run.name, "run_dir": str(run), "meta": meta,
         "sessions": {"s-parent", "s-child"}}])
    monkeypatch.setattr(labels, "_is_run_process", lambda b, pid, d: True)
    monkeypatch.setattr(labels, "_claude_session_id", lambda pid: "")
    monkeypatch.setattr(labels, "_claude_session_row", lambda pid: {})
    monkeypatch.setattr(labels, "_task_cache", None)
    monkeypatch.setattr(tasks_router, "snapshot_rows", lambda: [
        {"key": "s-parent", "session_id": "s-parent", "task_id": "TASK-001", "title": "Parent",
         "status": "done"},
        {"key": "s-child", "session_id": "s-child", "task_id": "TASK-002", "title": "Child",
         "status": "upcoming"}])

    class B:
        def is_zombie(self, pid): return False
        def proc_sample(self, pid): return object()
        def parent_pid(self, pid): return 1
        def proc_args(self, pid): return []

    owner = labels._claude_runs(B())[601]
    assert owner.label == "TASK-002 · Child"
    # Neither running nor waiting, tree alive: an amber "still alive" row.
    assert owner.ref["task"]["state"] == "idle"
    # With no session of its own known yet, the run is NOT named for its parent.
    (run / "launch.json").write_text("{}")
    monkeypatch.setattr(project_queue, "scan_runs", lambda agent: [
        {"run_id": run.name, "run_dir": str(run), "meta": meta, "sessions": {"s-parent"}}])
    monkeypatch.setattr(labels, "_task_cache", None)
    assert not labels._claude_runs(B())[601].label.startswith("TASK-001")
    monkeypatch.setattr(labels, "_claude_session_id", lambda pid: "s-parent")
    monkeypatch.setattr(labels, "_task_cache", None)
    assert labels._claude_runs(B())[601].label == "TASK-001 · Parent"   # the live CLI's word wins


def test_shell_labels_are_redacted_where_they_are_made():
    base = "TASK-075 · Fix it › "
    got = _child_label(["curl", "-H", "Authorization: Bearer sk-abc123", "https://u:pw@h/x"], "shell")
    assert "sk-abc123" not in got and "u:pw" not in got and got.startswith(base + "shell: curl")
    wrapper = ["/bin/sh", "/lib/fused_render/claude_shell_prefix.sh",
               "eval 'API_TOKEN=s3cret ./run.sh' < /dev/null"]
    assert "s3cret" not in _child_label(wrapper)


def test_a_helper_inside_a_run_tree_is_not_its_own_entry():
    run = labels.Owner("claude", "TASK-1 · a", {"run_id": "r", "task": {"task_id": "TASK-1",
                       "state": "running", "bg_jobs": []}}, base="TASK-1 · a")
    procs = [_row(10, 1, 100), _row(11, 10, 100), _row(12, 11, 50), _row(30, 1, 70)]
    got = labels.claude_tasks({10: run, 11: run}, {12: labels.HELPER_LABEL, 30: labels.HELPER_LABEL}, procs)
    assert [(t["root_pid"], t["helper"]) for t in got] == [(10, False), (30, True)]
    assert got[0]["mem_bytes"] == 250
