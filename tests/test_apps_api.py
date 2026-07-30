"""The Home view's apps backend (server/routers/apps.py): GET /api/apps lists
the workspace's app folders (entry = the single direct-child .html), and
POST /api/apps/new scaffolds a folder from the app starter kit and optionally
starts a detached Claude session on its index.html.

The spawn is stubbed at the module seam (_start_app_session) — no test here
launches a real claude.
"""
import json
import os
import stat
import time

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
                        lambda entry, prompt: called.append((entry, prompt)) or ("r-1", None))
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
    monkeypatch.setattr(apps_mod, "_start_app_session", lambda e, p: (None, "x"))
    client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)
    skill = workspace / "demo" / ".claude" / "skills" / "fused-render-authoring"
    assert (skill / "SKILL.md").is_file()


def test_new_app_with_prompt_starts_a_session(client, workspace, monkeypatch):
    seen = {}

    def fake_start(entry_html, prompt):
        seen["entry"] = entry_html
        seen["prompt"] = prompt
        return "run-42", None

    monkeypatch.setattr(apps_mod, "_start_app_session", fake_start)
    r = client.post("/api/apps/new",
                    json={"name": "demo", "prompt": "build a todo app"},
                    headers=HDRS)
    assert r.json()["session_started"] is True
    assert r.json()["session_error"] is None
    assert r.json()["run_id"] == "run-42"   # the UI can attach to the live run
    assert seen["entry"] == str(workspace / "demo" / "index.html")
    assert seen["prompt"] == "build a todo app"


def test_spawn_failure_does_not_fail_creation_and_says_why(client, workspace, monkeypatch):
    monkeypatch.setattr(apps_mod, "_start_app_session",
                        lambda e, p: (None, "claude CLI not found"))
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": "hi"}, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["session_started"] is False
    assert r.json()["run_id"] is None
    assert r.json()["session_error"] == "claude CLI not found"
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


# ------------------------------------------------------------------ updated_at

def test_updated_at_tracks_direct_children_not_just_the_dir(client, workspace):
    """Editing a file in place doesn't move the dir's own mtime — updated_at
    must still reflect it (max over dir + direct children)."""
    d = _app_dir(workspace, "app")
    dir_mtime = os.stat(d).st_mtime
    future = dir_mtime + 1000
    os.utime(d / "index.html", (future, future))
    apps = client.get("/api/apps").json()["apps"]
    # abs=, not the default rel=1e-6: on an epoch value (~1.8e9) the relative
    # tolerance is nearly half an hour, so `approx(future)` would happily
    # accept the dir's own untouched mtime and assert nothing at all.
    assert apps[0]["updated_at"] == pytest.approx(future, abs=0.01)


def test_updated_at_is_a_float_and_present_for_entryless_apps(client, workspace):
    _app_dir(workspace, "empty", htmls=())
    apps = client.get("/api/apps").json()["apps"]
    assert isinstance(apps[0]["updated_at"], float)


# ---------------------------------------------------- the fork-safe spawn seam

def test_spawn_runs_agent_start_in_a_helper_subprocess_not_in_process(
        tmp_path, workspace, monkeypatch):
    """The live-bug regression: calling agent._start inside the server process
    fork()s with libproj resident and SIGSEGVs the child before exec (PROJ's
    pthread_atfork handler; same crash test_worker_forksafe.py pins for the
    executor). The spawn must therefore happen via a helper subprocess — and
    that helper's own Popen must stay on the posix_spawn path (close_fds=False,
    no cwd, no start_new_session) with the prompt on stdin, not argv."""
    entry = workspace / "app" / "index.html"
    entry.parent.mkdir()
    entry.write_text("<html></html>")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return type("R", (), {"returncode": 0,
                              "stdout": '{"run_id": "r-1"}', "stderr": ""})()

    monkeypatch.setattr(apps_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(apps_mod, "_claude_agent", lambda: None)
    started_threads = []
    monkeypatch.setattr(apps_mod.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: started_threads.append(kw)})())

    run_id, err = apps_mod._start_app_session(str(entry), "secret prompt $(boom)")
    assert (run_id, err) == ("r-1", None)

    # a real python -c helper, not claude itself, and prompt over stdin only
    assert seen["cmd"][0] == apps_mod.sys.executable
    assert "secret prompt" not in " ".join(seen["cmd"])
    import json as jsonlib
    req = jsonlib.loads(seen["kwargs"]["input"])
    assert req["message"] == "secret prompt $(boom)"
    assert req["file"] == str(entry)
    # unattended: nobody polls `decide` for a session started from a POST, so
    # the strict default mode would park the first tool call until the
    # permission timeout denied it — boilerplate, silently.
    assert req["permission_mode"] == "auto"
    # posix_spawn preconditions on the helper spawn (the crash was fork+exec)
    assert seen["kwargs"]["close_fds"] is False
    assert "cwd" not in seen["kwargs"]
    assert "start_new_session" not in seen["kwargs"]
    assert started_threads  # the sidecar-recording poll thread was kicked off


def test_spawn_helper_failure_reports_why(tmp_path, workspace, monkeypatch):
    def fake_run(cmd, **kwargs):
        return type("R", (), {"returncode": 1, "stdout": "",
                              "stderr": "boom\nFileNotFoundError: claude"})()

    monkeypatch.setattr(apps_mod.subprocess, "run", fake_run)
    run_id, err = apps_mod._start_app_session("/x/index.html", "hi")
    assert run_id is None
    assert "FileNotFoundError: claude" in err


@pytest.mark.skipif(os.name == "nt", reason="/bin/sh stub claude is POSIX-only")
def test_spawn_really_delivers_the_prompt_to_the_claude_process(
        tmp_path, workspace, monkeypatch):
    """The regression the mocked tests could never catch.

    Everything below _start_app_session is real here — the helper subprocess,
    agent._start, the detached spawn — with only `claude` itself replaced by a
    shell stub that records the argv and stdin it was handed. The live bug was
    precisely that this whole path produced nothing: the helper's absence meant
    the fork() SIGSEGV'd before exec, so claude never ran at all and the app
    stayed boilerplate. Asserting the stub RAN and SAW the prompt is what pins
    that; a stub that is never executed writes no files and fails here."""
    entry = workspace / "app" / "index.html"
    entry.parent.mkdir()
    entry.write_text("<html></html>")
    argv_log, stdin_log = tmp_path / "argv.txt", tmp_path / "stdin.txt"
    stub = tmp_path / "claude"
    stub.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{argv_log}"\ncat > "{stdin_log}"\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", str(stub))

    run_id, err = apps_mod._start_app_session(str(entry), "hello from the test")
    assert err is None and run_id, err

    # the spawn is detached, so wait for the stub to finish writing
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not stdin_log.exists():
        time.sleep(0.1)
    assert stdin_log.exists(), "claude was never executed (the live bug)"

    argv = argv_log.read_text().splitlines()
    # the prompt reached the process, over stdin as one stream-json user line
    assert "hello from the test" not in "\n".join(argv)   # never in argv
    row = json.loads(stdin_log.read_text())
    assert row["message"]["content"][0]["text"] == "hello from the test"
    # ...and it can act on it unattended: prompt-tool wired AND a mode that
    # doesn't park every tool call on a card nobody is watching for.
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert argv[argv.index("--permission-prompt-tool") + 1].startswith("mcp__")
    assert argv[argv.index("--input-format") + 1] == "stream-json"


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
