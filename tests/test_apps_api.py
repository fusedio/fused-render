"""The Home view's apps backend (server/routers/apps.py): GET /api/apps lists
the workspace's app folders (entry = the single direct-child .html), and
POST /api/apps/new scaffolds a folder from the app starter kit and optionally
starts a detached Claude session on its index.html.

Apps live two levels under the workspace: <workspace>/<tag>/<name>/. A tag is
any non-hidden top-level folder — there is no registry, so these tests cover
arbitrary tag names alongside "local" (where POST /api/apps/new always lands).

The spawn is stubbed at the module seam (_start_app_session) — no test here
launches a real claude.
"""
import json
import os
import stat
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import app_listing
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


def _app_dir(workspace, name, htmls=("index.html",), title=None, tag="local"):
    d = workspace / tag / name
    d.mkdir(parents=True)
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
    (workspace / "loose.html").write_text("<html></html>")    # a file, not a tag dir

    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert set(apps) == {"one", "none", "many"}
    assert apps["one"]["entry_html"] == str(workspace / "local" / "one" / "index.html")
    assert apps["none"]["entry_html"] is None
    assert apps["many"]["entry_html"] is None
    assert apps["one"]["path"] == str(workspace / "local" / "one")
    assert apps["one"]["tag"] == "local"


def test_tag_is_the_parent_folder_name(client, workspace):
    _app_dir(workspace, "widget", tag="examples")
    _app_dir(workspace, "widget2", tag="local")
    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}
    assert apps["widget"]["tag"] == "examples"
    assert apps["widget2"]["tag"] == "local"


def test_any_top_level_folder_is_a_tag_no_registry(client, workspace):
    _app_dir(workspace, "proj", tag="whatever-i-want")
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["tag"] == "whatever-i-want"


def test_sorted_by_tag_then_name(client, workspace):
    _app_dir(workspace, "b", tag="zzz")
    _app_dir(workspace, "a", tag="aaa")
    apps = client.get("/api/apps").json()["apps"]
    assert [(a["tag"], a["name"]) for a in apps] == [("aaa", "a"), ("zzz", "b")]


def test_hidden_dirs_and_hidden_htmls_are_skipped(client, workspace):
    hidden_tag_app = workspace / ".hidden-tag" / "app"
    hidden_tag_app.mkdir(parents=True)
    (hidden_tag_app / "index.html").write_text("<html></html>")
    _app_dir(workspace, ".hidden-app")  # hidden project dir inside a real tag
    v = _app_dir(workspace, "app", htmls=("view.html",))
    (v / ".draft.html").write_text("<html></html>")  # hidden: doesn't make it ambiguous

    apps = client.get("/api/apps").json()["apps"]
    assert [a["name"] for a in apps] == ["app"]
    assert apps[0]["entry_html"] == str(v / "view.html")


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
    d = workspace / "local" / "big"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<!--" + "x" * 5000 + "--><title>late</title>")
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["title"] is None


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="chmod-based unreadable dir needs POSIX non-root")
def test_unreadable_tag_dir_is_skipped_not_fatal(client, workspace):
    _app_dir(workspace, "ok")
    locked = workspace / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        apps = client.get("/api/apps").json()["apps"]
    finally:
        os.chmod(locked, stat.S_IRWXU)
    assert [a["name"] for a in apps] == ["ok"]  # unreadable tag dir skipped, no 500


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0,
                    reason="chmod-based unreadable dir needs POSIX non-root")
def test_unreadable_project_dir_is_skipped_not_fatal(client, workspace):
    _app_dir(workspace, "ok")
    locked = workspace / "local" / "locked"
    locked.mkdir()
    os.chmod(locked, 0)
    try:
        apps = client.get("/api/apps").json()["apps"]
    finally:
        os.chmod(locked, stat.S_IRWXU)
    assert [a["name"] for a in apps] == ["ok"]  # unreadable project dir skipped, no 500


def test_an_unreadable_project_dir_is_skipped_at_any_uid(client, workspace,
                                                         monkeypatch):
    """The same contract as above, without depending on file permissions.

    The chmod test is VACUOUS FOR ROOT — mode 0 does not stop uid 0 reading the
    directory, so it is skipped there and a developer (or a container) running
    as root gets no coverage of this path at all. That is how a regression
    shipped once: `app_entry` was called from inside `app_dict`, which swallowed
    the `OSError` and turned "skip this directory" into "list it with no entry".

    Raising from `app_entry` itself reproduces the condition deterministically,
    for every uid and on Windows too, and is what the listing actually has to
    survive.
    """
    _app_dir(workspace, "ok")
    (workspace / "local" / "locked").mkdir()

    real = app_listing.app_entry

    def refuse(path):
        if os.path.basename(path) == "locked":
            raise PermissionError(13, "Permission denied", path)
        return real(path)

    monkeypatch.setattr(app_listing, "app_entry", refuse)
    apps = client.get("/api/apps").json()["apps"]

    assert [a["name"] for a in apps] == ["ok"]


def test_missing_workspace_lists_empty(client, workspace):
    os.rmdir(workspace)
    assert client.get("/api/apps").json() == {"apps": []}


def test_entry_is_reported_alongside_entry_html(client, workspace):
    """Both keys, same file — the shell reads `entry` and needs it to be there.

    `entry` is "the file a card opens"; `entry_html` is the narrower "this entry
    is a renderable page". They coincide for a workspace app, and an entry-less
    folder reports null under both rather than omitting either key.
    """
    _app_dir(workspace, "withentry")
    (workspace / "local" / "bare").mkdir()

    apps = {a["name"]: a for a in client.get("/api/apps").json()["apps"]}

    assert apps["withentry"]["entry"] == apps["withentry"]["entry_html"]
    assert apps["withentry"]["entry"].endswith("index.html")
    assert apps["bare"]["entry"] is None and apps["bare"]["entry_html"] is None


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
    dest = workspace / "local" / "demo"
    assert body["path"] == str(dest)
    assert body["entry_html"] == str(dest / "index.html")
    assert body["session_started"] is False
    assert called == []  # empty prompt: no spawn attempt at all
    assert (dest / "index.html").is_file()
    assert (dest / "CLAUDE.md").is_file()
    # the starter kit's entry is a valid single-entry app: it lists back
    apps = client.get("/api/apps").json()["apps"]
    assert apps[0]["entry_html"] == body["entry_html"]
    assert apps[0]["tag"] == "local"


def test_new_app_has_no_dot_claude_and_syncs_user_skills(
    client, workspace, tmp_path, monkeypatch
):
    """D185: the app folder itself carries no .claude/; creating an app
    installs the canonical skills at Claude Code's user level instead."""
    from fused_render import user_skills

    claude_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))
    monkeypatch.setattr(apps_mod, "_start_app_session", lambda e, p: (None, "x"))
    client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)

    assert not (workspace / "local" / "demo" / ".claude").exists()
    for name in user_skills.SKILLS:
        skill = claude_dir / "skills" / name
        assert (skill / "SKILL.md").is_file()
        assert (skill / user_skills._MARKER).is_file()


def test_new_app_with_prompt_starts_a_session(client, workspace, monkeypatch):
    seen = {}

    def fake_start(app_dir, prompt):
        seen["target"] = app_dir
        seen["prompt"] = prompt
        return "run-42", None

    monkeypatch.setattr(apps_mod, "_start_app_session", fake_start)
    r = client.post("/api/apps/new",
                    json={"name": "demo", "prompt": "build a todo app"},
                    headers=HDRS)
    assert r.json()["session_started"] is True
    assert r.json()["session_error"] is None
    assert r.json()["run_id"] == "run-42"   # the UI can attach to the live run
    # The scaffolding session starts on the app FOLDER (claude_split agent),
    # so its sidecar lands where the split view lists sessions from.
    assert seen["target"] == str(workspace / "local" / "demo")
    assert seen["prompt"] == "build a todo app"


def test_spawn_failure_does_not_fail_creation_and_says_why(client, workspace, monkeypatch):
    monkeypatch.setattr(apps_mod, "_start_app_session",
                        lambda e, p: (None, "claude CLI not found"))
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": "hi"}, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["session_started"] is False
    assert r.json()["run_id"] is None
    assert r.json()["session_error"] == "claude CLI not found"
    assert (workspace / "local" / "demo" / "index.html").is_file()


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "a/b", "a\\b", ".hidden", " .hidden", " a/b ", None, 7],
)
def test_bad_names_are_rejected(client, workspace, bad):
    r = client.post("/api/apps/new", json={"name": bad, "prompt": ""}, headers=HDRS)
    assert r.status_code == 400
    assert not any((workspace).iterdir())


def test_collision_is_409_for_dirs_and_files(client, workspace):
    _app_dir(workspace, "taken")
    (workspace / "local" / "afile").write_text("x")
    for name in ("taken", "afile"):
        r = client.post("/api/apps/new", json={"name": name, "prompt": ""}, headers=HDRS)
        assert r.status_code == 409, name


def test_partial_copy_is_cleaned_up(client, workspace, monkeypatch):
    def boom(src, dst, **kw):
        os.makedirs(dst)
        (workspace / "local" / "demo" / "index.html").write_text("partial")
        raise OSError("disk full")

    monkeypatch.setattr(apps_mod.shutil, "copytree", boom)
    r = client.post("/api/apps/new", json={"name": "demo", "prompt": ""}, headers=HDRS)
    assert r.status_code == 400
    assert not (workspace / "local" / "demo").exists()


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


# --------------------------- landing the creator in the running claude session

# Creating an app with a prompt starts a session the user never sees unless the
# post-create navigation opens the entry file's CLAUDE-template chat attached to
# that run. Three sources have to agree for that to work, and none of them can
# see the other two: HomeHero.tsx builds the URL, registry.json makes "claude" a
# selectable mode for .html, and the claude template's boot re-attaches from the
# `run` param. These tests pin the three ends of that contract.

def _repo_text(*parts):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *parts), encoding="utf-8") as fh:
        return fh.read()


def test_home_navigates_into_the_claude_chat_for_the_started_run():
    home = _repo_text("frontend", "src", "apps", "builder", "HomeHero.tsx")
    # Folder-first: the scaffolding session runs via the claude_split agent on
    # the app FOLDER, so the re-attach must land in the split view (same runs
    # dir, same .claude-split.json sidecar) — not the file-scoped claude mode.
    assert '_mode: "claude_split"' in home, "post-create nav must select the claude_split mode"
    assert "claudeChatUrl(res.path" in home, "…on the app folder, not entry_html"
    assert "run: runId" in home, "…and attach to the run the POST just started"
    # the run_id is what gates it: no session (no prompt) -> the default view
    assert "if (res.run_id) navigateUrl(claudeChatUrl(" in home


def test_claude_is_a_selectable_mode_for_html():
    registry = json.loads(_repo_text("fused_render", "templates", "registry.json"))
    assert "claude" in registry[".html"]


def test_claude_template_boots_into_chat_from_a_bare_run_param():
    """The page must resume a run it did not start itself: its boot reads the
    `run` param, enters chat, and polls — no session_id needed (the sidecar
    entry lands seconds later, once claude reports its id)."""
    page = _repo_text("fused_render", "templates", "claude", "template.html")
    assert 'fused.params.get("run")' in page
    assert "await resumeRun(run_id)" in page


def test_run_param_survives_the_shell_runtime():
    """`run` must be an ordinary view param: the runtime hides every
    `_`-prefixed name from templates (isReserved), so a reserved-looking name
    would read back as undefined and the chat would boot to its home card."""
    assert not "run".startswith("_")
    runtime = _repo_text("fused_render", "static", "runtime.js")
    assert 'if (key.startsWith("_")) return true;' in runtime


def test_poll_serves_a_run_started_by_the_server(tmp_path, workspace, monkeypatch):
    """The crux: agent._poll is the page's re-attach path, and it must answer
    for a run the SERVER spawned (the POST) exactly as for one the page did —
    same runs dir, same meta, so the page replays the user's prompt and streams
    the reply. Pinned against a real spawn (stub claude) rather than a mock."""
    if os.name == "nt":
        pytest.skip("/bin/sh stub claude is POSIX-only")
    entry = workspace / "app" / "index.html"
    entry.parent.mkdir()
    entry.write_text("<html></html>")
    stub = tmp_path / "claude"
    # the stream-json rows poll parses: an init row carrying the session id, a
    # streamed text delta, and the terminating result row
    stub.write_text(
        '#!/bin/sh\ncat > /dev/null\n'
        'printf \'%s\\n\' \'{"type":"system","subtype":"init","session_id":"sid-live"}\'\n'
        'printf \'%s\\n\' \'{"type":"stream_event","event":{"type":'
        '"content_block_delta","delta":{"type":"text_delta","text":"on it"}}}\'\n'
        'printf \'%s\\n\' \'{"type":"result","subtype":"success",'
        '"session_id":"sid-live","result":"on it"}\'\nexit 0\n')
    stub.chmod(0o755)
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", str(stub))

    run_id, err = apps_mod._start_app_session(str(entry), "make it red")
    assert err is None and run_id, err

    agent = apps_mod._claude_agent()
    deadline = time.monotonic() + 30
    data = {}
    while time.monotonic() < deadline:
        data = agent._poll(run_id)
        if data.get("done"):
            break
        time.sleep(0.2)
    assert data.get("done"), data
    # the page renders `message` as the user turn and `text` as the reply
    assert data["message"] == "make it red"
    assert "on it" in (data.get("text") or "")
    assert data.get("session_id") == "sid-live"
    # ...and the session lists in the entry file's sidecar, so a later visit
    # without a `run` param still finds the conversation
    assert agent._sessions(str(entry))["sessions"][0]["id"] == "sid-live"
