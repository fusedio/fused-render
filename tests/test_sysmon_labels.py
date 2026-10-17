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
