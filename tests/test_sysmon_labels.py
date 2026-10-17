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
