"""The Home view's apps backend (server/routers/apps.py): GET /api/apps lists
the workspace's app folders (entry = the single direct-child .html), and
POST /api/apps/new scaffolds a folder from the app starter kit and optionally
starts a detached Claude session on its index.html.

The spawn is stubbed at the module seam (_start_app_session) — no test here
launches a real claude.
"""
import os
import stat

import pytest
from fastapi.testclient import TestClient

from fused_render.server import create_app
from fused_render.server.routers import apps as apps_mod


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    fdir = tmp_path / "Fused"
    fdir.mkdir()
    monkeypatch.setenv("FUSED_RENDER_DIR", str(fdir))
    return fdir


@pytest.fixture()
def client(tmp_path, workspace):
    return TestClient(create_app(start_dir=str(tmp_path)))


def _app_dir(workspace, name, htmls=("index.html",), title=None):
    d = workspace / name
    d.mkdir()
    for i, h in enumerate(htmls):
        body = "<html><body>hi</body></html>"
        if title is not None and i == 0:
            body = f"<html><head><title>{title}</title></head></html>"
        (d / h).write_text(body)
    return d


# -------------------------------------------------------------------- listing

def test_lists_only_top_level_dirs_with_entry_resolution(client, workspace):
    _app_dir(workspace, "one")                                # exactly one html
    _app_dir(workspace, "none", htmls=())                     # zero htmls
    _app_dir(workspace, "many", htmls=("a.html", "b.html"))   # ambiguous
    (workspace / "loose.html").write_text("<html></html>")    # a file, not an app

    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert set(apps) == {"one", "none", "many"}
    assert apps["one"]["entry_html"] == str(workspace / "one" / "index.html")
    assert apps["none"]["entry_html"] is None
    assert apps["many"]["entry_html"] is None
    assert apps["one"]["path"] == str(workspace / "one")


def test_hidden_dirs_and_hidden_htmls_are_skipped(client, workspace):
    _app_dir(workspace, ".hidden")
    d = _app_dir(workspace, "app", htmls=("view.html",))
    (d / ".draft.html").write_text("<html></html>")  # hidden: doesn't make it ambiguous

    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["app"]
    assert apps[0]["entry_html"] == str(d / "view.html")


def test_entry_match_is_non_recursive(client, workspace):
    d = _app_dir(workspace, "app", htmls=())
    (d / "sub").mkdir()
    (d / "sub" / "index.html").write_text("<html></html>")
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["entry_html"] is None


def test_sorted_case_insensitively(client, workspace):
    for name in ("beta", "Alpha", "gamma"):
        _app_dir(workspace, name)
    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["Alpha", "beta", "gamma"]


def test_title_parsed_from_entry_head(client, workspace):
    _app_dir(workspace, "titled", title="My  Fancy\n App")
    _app_dir(workspace, "untitled")
    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert apps["titled"]["title"] == "My Fancy App"  # whitespace collapsed
    assert apps["untitled"]["title"] is None


def test_title_beyond_first_4kb_is_null_not_an_error(client, workspace):
    d = workspace / "big"
    d.mkdir()
    (d / "index.html").write_text("<!--" + "x" * 5000 + "--><title>late</title>")
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["title"] is None


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="chmod-based unreadable dir needs POSIX non-root")
def test_unreadable_dir_is_skipped_not_fatal(client, workspace):
    _app_dir(workspace, "ok")
    locked = workspace / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        apps = client.get("/api/apps").json()["apps"]
    finally:
        os.chmod(locked, stat.S_IRWXU)
    assert [a["name"] for a in apps] == ["ok"]  # unreadable dir skipped, no 500


def test_missing_workspace_lists_empty(client, workspace):
    os.rmdir(workspace)
    assert client.get("/api/apps").json() == {"apps": []}


# ------------------------------------------------------------------- creation

HDRS = {"X-Fused": "1"}


def test_new_app_requires_the_fused_header(client):
    r = client.post("/api/apps/new", json={"name": "x", "prompt": ""})
    assert r.status_code == 403


def test_new_app_happy_path_no_prompt(client, workspace, monkeypatch):
    called = []
    monkeypatch.setattr(apps_mod, "_start_app_session",
                        lambda entry, prompt: called.append((entry, prompt)) or True)
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)
    assert r.status_code == 200
    body = r.json()
    dest = workspace / "demo"
    assert body["path"] == str(dest)
    assert body["entry_html"] == str(dest / "index.html")
    assert body["session_started"] is False
    assert called == []  # empty prompt: no spawn attempt at all
    assert (dest / "index.html").is_file()
    assert (dest / "CLAUDE.md").is_file()
    # the starter kit's entry is a valid single-entry app: it lists back
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["entry_html"] == body["entry_html"]


def test_new_app_carries_the_authoring_skill(client, workspace, monkeypatch):
    monkeypatch.setattr(apps_mod, "_start_app_session", lambda e, p: False)
    client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)
    skill = workspace / "demo" / ".claude" / "skills" / "fused-render-authoring"
    assert (skill / "SKILL.md").is_file()


def test_new_app_with_prompt_starts_a_session(client, workspace, monkeypatch):
    seen = {}

    def fake_start(entry_html, prompt):
        seen["entry"] = entry_html
        seen["prompt"] = prompt
        return True

    monkeypatch.setattr(apps_mod, "_start_app_session", fake_start)
    r = client.post("/api/apps/new",
                    json={"name": "demo", "prompt": "build a todo app"},
                    headers=HDRS)
    assert r.json()["session_started"] is True
    assert seen["entry"] == str(workspace / "demo" / "index.html")
    assert seen["prompt"] == "build a todo app"


def test_spawn_failure_does_not_fail_creation(client, workspace, monkeypatch):
    monkeypatch.setattr(apps_mod, "_start_app_session", lambda e, p: False)
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": "hi"}, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["session_started"] is False
    assert (workspace / "demo" / "index.html").is_file()


@pytest.mark.parametrize("bad", ["", "  ", "a/b", "a\\b", ".hidden", None, 7])
def test_bad_names_are_rejected(client, workspace, bad):
    r = client.post("/api/apps/new", json={"name": bad, "prompt": ""}, headers=HDRS)
    assert r.status_code == 400
    assert not any((workspace).iterdir())


def test_collision_is_409_for_dirs_and_files(client, workspace):
    _app_dir(workspace, "taken")
    (workspace / "afile").write_text("x")
    for name in ("taken", "afile"):
        r = client.post("/api/apps/new", json={"name": name, "prompt": ""}, headers=HDRS)
        assert r.status_code == 409, name


def test_partial_copy_is_cleaned_up(client, workspace, monkeypatch):
    def boom(src, dst, **kw):
        os.makedirs(dst)
        (workspace / "demo" / "index.html").write_text("partial")
        raise OSError("disk full")

    monkeypatch.setattr(apps_mod.shutil, "copytree", boom)
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)
    assert r.status_code == 400
    assert not (workspace / "demo").exists()


# --------------------------------------------- the stdin path through agent.py

def test_agent_start_stdin_mode_keeps_message_out_of_argv(tmp_path, monkeypatch):
    """The spawn the apps API relies on: message_via_stdin writes the prompt as
    a stream-json user line in the run dir and wires it as the process stdin —
    the prompt string must appear nowhere in argv."""
    import importlib.util
    import json as jsonlib

    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_stdin", path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)

    target = tmp_path / "index.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["stdin"] = kwargs["stdin"]
        return type("P", (), {"pid": 4242})()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    secret = "build me a $(rm -rf /) app"
    run_id = agent._start(str(target), secret, "", "", "",
                          message_via_stdin=True)["run_id"]

    assert secret not in " ".join(seen["cmd"])
    assert seen["cmd"][seen["cmd"].index("--input-format") + 1] == "stream-json"
    # stdin is the run-dir file holding exactly one stream-json user message
    stdin_file = os.path.join(agent.RUNS, run_id, "stdin.jsonl")
    assert seen["stdin"].name == stdin_file
    row = jsonlib.loads(open(stdin_file, encoding="utf-8").read())
    assert row["message"]["content"][0]["text"] == secret


def test_agent_start_default_still_passes_message_in_argv(tmp_path, monkeypatch):
    """The template path is unchanged: no stdin file, message after -p."""
    import importlib.util

    path = os.path.join("fused_render", "templates", "claude", "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent_argv", path)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)

    target = tmp_path / "index.html"
    target.write_text("<html></html>")
    monkeypatch.setattr(agent, "RUNS", str(tmp_path / "runs"))
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    seen = {}

    def fake_popen(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["stdin"] = kwargs["stdin"]
        return type("P", (), {"pid": 4242})()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    run_id = agent._start(str(target), "hello", "", "", "")["run_id"]
    assert seen["cmd"][seen["cmd"].index("-p") + 1] == "hello"
    assert seen["stdin"] == agent.subprocess.DEVNULL
    assert not os.path.exists(os.path.join(agent.RUNS, run_id, "stdin.jsonl"))
