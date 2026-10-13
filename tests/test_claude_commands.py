"""SPEC C-2: the slash-command list.

`_poll` copies the CLI's `init` row (`slash_commands`, `terminal_slash_commands`,
`agents`, `skills`) into the run dir's `capabilities.json` and a per-workdir
state file, and `_commands(file)` answers the composer's menu from that state
file (`from: "cli"`) or, before any session in the folder has spoken, from a
scan of the `.claude` folders the CLI reads (`from: "disk"`).
"""
import importlib.util
import json
import os

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")


def _load():
    path = os.path.join(AGENT_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_cmds_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "fr-home"))
    return h


@pytest.fixture
def agent(home):
    return _load()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".git").mkdir(parents=True)
    cmds = r / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "a.md").write_text(
        "---\ndescription: \"Do the A thing\"\nallowed-tools: Bash\n---\nBody of a\n",
        encoding="utf-8")
    (cmds / "b.md").write_text("\n\n# Build the project\n\nmore\n", encoding="utf-8")
    (cmds / "ns").mkdir()
    (cmds / "ns" / "deep.md").write_text("deep one\n", encoding="utf-8")
    skill = r / ".claude" / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: deploy\ndescription: Ship it\n---\n# Deploy\n", encoding="utf-8")
    (r / ".claude" / "agents").mkdir()
    (r / ".claude" / "agents" / "reviewer.md").write_text("x", encoding="utf-8")
    sub = r / "app"
    sub.mkdir()
    return r


def _init_row(**extra):
    row = {"type": "system", "subtype": "init", "session_id": "s",
           "slash_commands": ["compact", "a", "deploy", "mystery", "vim", "/b"],
           "terminal_slash_commands": ["vim"],
           "agents": ["general-purpose", "reviewer"],
           "skills": [{"name": "deploy", "source": "projectSettings"},
                      {"name": "pdf", "source": "plugin", "plugin_name": "docs"}]}
    row.update(extra)
    return row


def _make_run(agent, tmp_path, target, rows):
    runs = tmp_path / "runs"
    run_dir = runs / "run1"
    (run_dir / "perm").mkdir(parents=True)
    (run_dir / "meta.json").write_text(json.dumps({"file": str(target)}),
                                       encoding="utf-8")
    (run_dir / "out.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    agent.RUNS = str(runs)
    agent._alive = lambda _d: True
    return run_dir


def _append(run_dir, rows):
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def test_init_row_is_captured_into_both_files(agent, tmp_path, repo):
    target = repo / "app"
    run_dir = _make_run(agent, tmp_path, target, [_init_row()])
    data = agent._poll("run1")
    assert data["commands_changed"] is True

    cap = json.loads((run_dir / "capabilities.json").read_text())
    assert cap["slash_commands"][:3] == ["compact", "a", "deploy"]
    assert cap["terminal_slash_commands"] == ["vim"]
    assert cap["agents"] == ["general-purpose", "reviewer"]
    assert cap["skills"][1] == {"name": "pdf", "source": "plugin",
                                "plugin_name": "docs"}
    state = agent._commands_state_path(str(target))
    assert state == os.path.join(str(tmp_path / "fr-home"), "commands",
                                 agent._munge(str(target)))
    assert json.loads(open(state).read())["slash_commands"] == cap["slash_commands"]


def test_commands_changed_is_reported_once_per_new_row(agent, tmp_path, repo):
    run_dir = _make_run(agent, tmp_path, repo / "app", [_init_row()])
    assert agent._poll("run1")["commands_changed"] is True
    assert agent._poll("run1")["commands_changed"] is False, \
        "the same init row re-read from the cursor is not news"
    _append(run_dir, [{"type": "system", "subtype": "commands_changed"}])
    assert agent._poll("run1")["commands_changed"] is True
    assert agent._commands(str(repo / "app"))["stale"] is True
    _append(run_dir, [_init_row()])
    assert agent._poll("run1")["commands_changed"] is True
    assert agent._commands(str(repo / "app"))["stale"] is False


def test_commands_from_the_state_file(agent, tmp_path, repo):
    target = repo / "app"
    _make_run(agent, tmp_path, target, [_init_row()])
    agent._poll("run1")
    res = agent._commands(str(target))
    assert res["from"] == "cli" and res["stale"] is False
    by_name = {c["name"]: c for c in res["commands"]}
    assert "vim" not in by_name, "terminal-only commands are dropped"
    assert list(by_name) == ["compact", "a", "deploy", "mystery", "b"]
    # Descriptions merged from the disk scan by name, else the built-in table.
    assert by_name["a"] == {"name": "a", "description": "Do the A thing",
                            "source": "project"}
    assert by_name["b"]["description"] == "Build the project"
    assert by_name["deploy"]["description"] == "Ship it"
    assert by_name["compact"]["source"] == "builtin"
    assert by_name["compact"]["description"]
    assert by_name["mystery"] == {"name": "mystery", "description": "",
                                  "source": "cli"}
    assert res["agents"] == ["general-purpose", "reviewer"]


def test_commands_from_a_disk_scan(agent, home, repo):
    user_cmds = home / ".claude" / "commands"
    user_cmds.mkdir(parents=True)
    (user_cmds / "mine.md").write_text("My own command\n", encoding="utf-8")
    (user_cmds / "a.md").write_text("user copy of a\n", encoding="utf-8")
    user_skill = home / ".claude" / "skills" / "notes"
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text("Take notes\n", encoding="utf-8")

    res = agent._commands(str(repo / "app"))
    assert res["from"] == "disk" and res["stale"] is False
    by_name = {c["name"]: c for c in res["commands"]}
    assert by_name["a"]["description"] == "Do the A thing", \
        "frontmatter wins, and the project copy shadows the user one"
    assert by_name["b"]["description"] == "Build the project", \
        "no frontmatter: the first non-empty line"
    assert by_name["ns:deep"]["description"] == "deep one"
    assert by_name["deploy"] == {"name": "deploy", "description": "Ship it",
                                 "source": "project"}
    assert by_name["mine"]["source"] == "user"
    assert by_name["notes"]["description"] == "Take notes"
    for builtin in ("compact", "context", "cost", "review", "init", "clear",
                    "help", "model", "effort", "worktree", "tasks"):
        assert by_name[builtin]["source"] == "builtin"
    assert "reviewer" in res["agents"]


def test_commands_for_a_file_target_uses_its_folder(agent, repo):
    f = repo / "app" / "page.html"
    f.write_text("<p>", encoding="utf-8")
    names = {c["name"] for c in agent._commands(str(f))["commands"]}
    assert {"a", "b", "deploy"} <= names


def test_main_dispatches_commands(agent, repo):
    assert agent.main("commands", file=str(repo / "app"))["from"] == "disk"
    assert "error" in agent.main("commands")


def test_commands_changed_once_across_a_tail_then_complete_row(agent, tmp_path, repo):
    run_dir = _make_run(agent, tmp_path, repo / "app", [])
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(_init_row()))          # no newline yet
    assert agent._poll("run1")["commands_changed"] is False
    assert (run_dir / "capabilities.json").exists(), "a tail row is still captured"
    with open(run_dir / "out.jsonl", "a", encoding="utf-8") as fh:
        fh.write("\n")
    assert agent._poll("run1")["commands_changed"] is True
    assert agent._poll("run1")["commands_changed"] is False


def test_a_first_poll_captures_only_the_last_init(agent, tmp_path, repo, monkeypatch):
    captured = []
    run_dir = _make_run(agent, tmp_path, repo / "app", [
        _init_row(slash_commands=["one"]), {"type": "result", "result": "x"},
        _init_row(slash_commands=["two"])])
    real = agent._capture_commands
    monkeypatch.setattr(agent, "_capture_commands",
                        lambda rd, row: (captured.append(row["slash_commands"]),
                                         real(rd, row)))
    agent._poll("run1")
    assert captured == [["two"]]
    assert json.loads((run_dir / "capabilities.json").read_text())["slash_commands"] == ["two"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFOs only")
def test_a_fifo_named_like_a_command_is_not_opened(agent, repo):
    os.mkfifo(str(repo / ".claude" / "commands" / "trap.md"))
    os.mkfifo(str(repo / ".claude" / "skills" / "deploy" / "x.md"))
    names = {c["name"] for c in agent._commands(str(repo / "app"))["commands"]}
    assert "trap" not in names and "a" in names
    assert agent._md_description(str(repo / ".claude" / "commands" / "trap.md")) == ""


def test_symlinked_project_command_dirs_are_not_walked(agent, tmp_path, repo):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "commands").mkdir(parents=True)
    (elsewhere / "commands" / "far.md").write_text("far away\n", encoding="utf-8")
    (repo / ".claude" / "commands" / "linked").symlink_to(elsewhere / "commands")
    names = {c["name"] for c in agent._commands(str(repo / "app"))["commands"]}
    assert "linked:far" not in names, "a symlinked subdirectory is not followed"

    other = tmp_path / "repo2"
    (other / ".git").mkdir(parents=True)
    (other / ".claude").symlink_to(elsewhere)
    names = {c["name"] for c in agent._commands(str(other))["commands"]}
    assert "far" not in names, "a symlinked project .claude is not walked"


def test_the_walk_is_capped_by_directory_count(agent, tmp_path, monkeypatch):
    r = tmp_path / "deep"
    (r / ".git").mkdir(parents=True)
    base = r / ".claude" / "commands"
    for i in range(10):
        (base / ("d%02d" % i)).mkdir(parents=True)
        (base / ("d%02d" % i) / "c.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(agent, "_COMMAND_DIR_CAP", 3)
    names = [c["name"] for c in agent._commands(str(r))["commands"]
             if c["source"] == "project"]
    assert len(names) == 2   # the root plus two subdirectories
