"""Cursor CLI as the sixth agent (docs/CURSOR_BACKEND.md): agent.py's
Cursor plumbing (the binary ladder, the bare argv and the instructions file
it writes, the spawn env, the terminal line, the prefs gate, the error
words), `cursor_health` (the `status --format json` sign-in probe, the ACP
model list, the snapshot), `cursor_login`, `cursor_install`, the one-shot
the recap runs, and the /api/cursor/* router.

No test runs a real `cursor-agent`: the probes that must run end to end run
against tests/fake_cursor_health_cli.py (and, for `acp`, the
tests/fake_cursor_acp.py it hands over to).
"""
import importlib.util
import json
import os
import stat
import sys
import time

import pytest

from fused_render import claude_health, cursor_health

AGENT_PATH = os.path.abspath(os.path.join("fused_render", "claude_agent", "agent.py"))
_FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_cursor_health_cli.py")


def _load_agent():
    spec = importlib.util.spec_from_file_location("claude_agent_cursor_test", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_HOME_DIR", str(tmp_path / "home-branch"))
    (tmp_path / "home").mkdir()
    (tmp_path / "home-branch").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    for name in (cursor_health.BIN_ENV, cursor_health.API_KEY_ENV, "NO_OPEN_BROWSER",
                 "FUSED_RENDER_FUSED_CLI_DIR", "FUSED_RENDER_ORIGIN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cursor_health, "_shell_probe", lambda: None)
    monkeypatch.setattr(claude_health, "candidates", lambda: ())
    monkeypatch.setattr(cursor_health, "candidates", lambda: ())
    monkeypatch.setattr(cursor_health, "_ADOPTED", None)
    from fused_render import cursor_install, cursor_login
    cursor_login.reset()
    cursor_install.reset()


@pytest.fixture()
def agent(tmp_path):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    mod.RUNS = str(runs)
    mod.SHOTS = str(tmp_path / "shots")
    return mod


def _fake_binary(where):
    """A runnable `cursor-agent` behind a shebang naming this interpreter."""
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(f"#!{sys.executable}\nimport runpy, sys\n"
                     f"sys.argv[0] = {_FAKE!r}\n"
                     f"runpy.run_path({_FAKE!r}, run_name='__main__')\n")
    where.chmod(where.stat().st_mode | stat.S_IXUSR)
    return str(where)


def _exe(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, 0o755)
    return path


# ----------------------------------------------------------------- agent.py

def test_the_binary_ladder(agent, tmp_path, monkeypatch):
    on_path = _exe(str(tmp_path / "path" / "cursor-agent"))
    monkeypatch.setenv("PATH", str(tmp_path / "path"))
    assert agent._cursor_bin() == on_path
    forced = _exe(str(tmp_path / "forced" / "cursor-agent"))
    monkeypatch.setenv("FUSED_RENDER_CURSOR_BIN", forced)
    assert agent._cursor_bin() == forced
    monkeypatch.setenv("FUSED_RENDER_CURSOR_BIN", str(tmp_path / "gone"))
    assert agent._cursor_bin() == on_path
    monkeypatch.delenv("FUSED_RENDER_CURSOR_BIN")
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    own = _exe(str(tmp_path / "h" / ".local" / "bin" / "cursor-agent"))
    monkeypatch.setattr(agent, "_CURSOR_CANDIDATES",
                        tuple(c for c in agent._CURSOR_CANDIDATES if c.startswith("~")))
    assert agent._cursor_bin() == own
    os.remove(own)
    # The installer's bare `agent` alias is never picked up.
    _exe(str(tmp_path / "nothing" / "agent"))
    with pytest.raises(FileNotFoundError) as exc:
        agent._cursor_bin()
    assert "curl https://cursor.com/install -fsS | bash" in str(exc.value)


def test_the_candidates_mirror_cursor_health(agent):
    assert agent._CURSOR_CANDIDATES == cursor_health.POSIX_CANDIDATES


def test_the_argv_is_bare_and_the_prompt_lands_in_the_run_dir(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_cursor_bin", lambda: "/x/cursor-agent")
    monkeypatch.setattr(agent, "_terminal_tools_supported", lambda: False)
    proj = tmp_path / "proj"
    proj.mkdir()
    page = proj / "index.html"
    page.write_text("<html></html>")
    extra = tmp_path / "refs"
    extra.mkdir()
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir()
    argv = agent._cursor_argv(str(run_dir), True, "auto", "", "gpt-5.5", "high",
                              [str(extra)], str(page))
    # Nothing but the subcommand: no --force, no --model (the host sets it
    # live), no effort.
    assert argv == ["/x/cursor-agent", "acp"]
    body = (run_dir / "cursor" / "instructions.md").read_text()
    # The pane tool is named the way Cursor shows it to the model: bare.
    assert "mcp__fused_render__" not in body
    assert "`app_state`" in body or "app_state" in body
    assert str(extra) in body
    # A pane exists, so the MCP config the host hands `session/new` is there.
    assert (run_dir / "mcp.json").exists()


def test_the_spawn_env(agent, tmp_path, monkeypatch):
    cli = str(tmp_path / "fused-bin")
    monkeypatch.setenv("FUSED_RENDER_FUSED_CLI_DIR", cli)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", cli, "/bin"]))
    monkeypatch.setenv("CLAUDE_CODE_SHELL_PREFIX", "/x")
    monkeypatch.setenv("CURSOR_API_KEY", "key_123")
    env = agent._spawn_env("chat", backend="cursor")
    assert env["PATH"].split(os.pathsep)[0] == cli
    assert not any(k.startswith("CLAUDE_CODE_") for k in env)
    assert env["CURSOR_API_KEY"] == "key_123"
    assert env["NO_OPEN_BROWSER"] == "1"
    monkeypatch.setenv("NO_OPEN_BROWSER", "0")
    assert agent._spawn_env("", backend="cursor")["NO_OPEN_BROWSER"] == "0"


def test_the_terminal_command(agent, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    f = proj / "a.py"
    f.write_text("x")
    out = agent._terminal_command(str(f), backend="cursor")
    assert out["backend"] == "cursor" and out["command"].endswith("&& cursor-agent")


def test_the_prefs_gate(agent, tmp_path):
    prefs = tmp_path / "home-branch" / "prefs.json"
    assert not agent._backend_enabled("cursor")
    prefs.write_text(json.dumps({"cursor_enabled": True, "claude_enabled": False}))
    assert agent._backend_enabled("cursor") and not agent._backend_enabled("claude")
    prefs.write_text(json.dumps({"cursor_enabled": False, "claude_enabled": False}))
    # Every switch off reads as Claude on.
    assert agent._backend_enabled("claude") and not agent._backend_enabled("cursor")


def test_the_error_words(agent):
    signed_out = agent._account_error("Authentication required. Please run "
                                      "'cursor-agent login' first", "cursor")
    assert signed_out.startswith("Cursor CLI isn't signed in.")
    limit = agent._account_error("429 Too Many Requests", "cursor")
    assert limit.startswith("Your Cursor plan's usage limit was reached.")
    assert agent._account_error("something else", "cursor") == "something else"


def test_a_plan_mode_send_is_refused_for_cursor(agent):
    from fused_render.agent_backends import cursor
    assert "plan" not in cursor.MODES
    assert cursor.CURSOR.caps.plan_mode is False


# ------------------------------------------------------------ cursor_health

@pytest.mark.parametrize("body,want", [
    ({"status": "authenticated"}, True),
    ({"status": "unauthenticated"}, False),
    ({"status": "partially-authenticated"}, False),
    ({"status": "error", "message": "x"}, None),
    (None, None),
])
def test_status_classifies(body, want):
    assert cursor_health._classify(body) is want


def test_an_api_key_signs_the_cli_in(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "k")
    assert cursor_health._classify({"status": "unauthenticated"}) is True
    acct = cursor_health.account(True, {"status": "unauthenticated"})
    assert acct["method"] == "api_key"


def test_the_account_names_email_and_team():
    acct = cursor_health.account(True, {"status": "authenticated", "userInfo": {
        "email": "dev@example.com", "teamName": "Acme"}})
    assert acct == {"method": "cursor", "email": "dev@example.com", "org": "Acme",
                    "plan": None}
    assert cursor_health.account(False, {}) is None


def test_normalize_models():
    rows = [{"value": "auto", "name": "Auto"}, {"value": "gpt-5.5", "name": "GPT-5.5"},
            {"value": "gpt-5.5", "name": "dup"}, "junk", {"name": "no id"}]
    assert cursor_health.normalize_models(rows) == [
        {"id": "auto", "label": "Auto", "description": None},
        {"id": "gpt-5.5", "label": "GPT-5.5", "description": None}]


def test_the_version_is_the_release_date(tmp_path, monkeypatch):
    path = _fake_binary(tmp_path / "bin" / "cursor-agent")
    assert cursor_health.probe_version(path) == "2026.11.19"
    assert not cursor_health.is_outdated("2026.11.19")
    assert cursor_health.is_outdated("2025.10.1")


def test_a_signed_in_measure_carries_the_account_and_the_models(tmp_path, monkeypatch):
    path = _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("FAKE_CURSOR_STATUS", "in")
    log = tmp_path / "cli.log"
    monkeypatch.setenv("FAKE_CURSOR_LOG", str(log))
    snap = cursor_health.summary_refreshed()
    assert snap["found"] is True and snap["path"] == path
    assert snap["version"] == "2026.11.19"
    assert snap["signed_in"] is True
    assert snap["account"]["email"] == "dev@example.com"
    assert [m["id"] for m in snap["models"]] == ["auto", "composer-2", "gpt-5.5",
                                                 "claude-4.6-sonnet"]
    assert snap["install_command"] == "curl https://cursor.com/install -fsS | bash"
    # Every probe child runs with the browser held shut.
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert all(r["no_open_browser"] == "1" for r in rows)
    # The catalog serves the account's list (auto first).
    from fused_render.agent_backends import cursor
    ids = [m.id for m in cursor.discovered_models(snap)]
    assert ids == ["auto", "composer-2", "gpt-5.5", "claude-4.6-sonnet"]
    assert "gpt-5.5" in cursor.CURSOR.model_ids(discovered=True)


def test_a_signed_out_measure_says_so_and_lists_nothing(tmp_path, monkeypatch):
    _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("FAKE_CURSOR_STATUS", "out")
    snap = cursor_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None
    assert snap["models"] is None


def test_an_unreadable_status_carries_the_last_account(tmp_path, monkeypatch):
    _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("FAKE_CURSOR_STATUS", "in")
    cursor_health.summary_refreshed()
    monkeypatch.setenv("FAKE_CURSOR_STATUS", "garbage")
    snap = cursor_health.summary_refreshed()
    assert snap["signed_in"] is None
    assert snap["account"]["email"] == "dev@example.com"
    assert snap["models"]


def test_install_method_and_update_plan():
    home = os.path.expanduser("~")
    assert cursor_health.install_method(
        home + "/.local/share/cursor-agent/versions/2026.11.19-f005e9d/cursor-agent") == "installer"
    assert cursor_health.install_method("/opt/homebrew/bin/cursor-agent") == "brew"
    assert cursor_health.install_method("/srv/cursor-agent") == "other"
    assert cursor_health.update_plan("installer")["updatable"] is True
    assert cursor_health.update_plan("brew")["updatable"] is False


def test_nothing_missing_is_found():
    snap = cursor_health.summary_refreshed()
    assert snap["found"] is False and snap["signed_in"] is None


# ---------------------------------------------------------------- login

def test_login_runs_the_browser_flow_and_re_probes(tmp_path, monkeypatch):
    from fused_render import cursor_login
    _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    log = tmp_path / "cli.log"
    monkeypatch.setenv("FAKE_CURSOR_LOG", str(log))
    probed = []
    monkeypatch.setattr(cursor_health, "summary_refreshed", lambda: probed.append(1) or {})
    rec = cursor_login.start()
    assert rec["in_flight"] is True and rec["detail"] is None
    deadline = time.monotonic() + 10
    while cursor_login.status()["in_flight"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert cursor_login.status()["error"] is None
    assert probed == [1]
    rows = [json.loads(ln) for ln in log.read_text().splitlines()]
    login = next(r for r in rows if r["argv"] == ["login"])
    # The one child that must be ALLOWED to open the browser.
    assert login["no_open_browser"] is None


def test_a_failed_login_never_names_the_url(tmp_path, monkeypatch):
    from fused_render import cursor_login
    _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("FAKE_CURSOR_LOGIN", "fail")
    cursor_login.start()
    deadline = time.monotonic() + 10
    while cursor_login.status()["in_flight"] and time.monotonic() < deadline:
        time.sleep(0.05)
    err = cursor_login.status()["error"]
    assert err == "Login failed: timed out"
    assert "challenge" not in json.dumps(cursor_login.status())


def test_login_refuses_without_a_cli():
    from fused_render import cursor_login
    with pytest.raises(cursor_login.LoginError):
        cursor_login.start()


# -------------------------------------------------------------- install

def test_the_install_and_update_lines():
    from fused_render import cursor_install
    argv, display = cursor_install.install_argv()
    assert argv == ["bash", "-c", "curl https://cursor.com/install -fsS | bash"]
    assert display == cursor_health.INSTALL_COMMAND_POSIX
    assert cursor_install.update_argv("/p/cursor-agent") == (
        ["/p/cursor-agent", "update"], "cursor-agent update")


# -------------------------------------------------------------- one-shot

def test_the_recap_one_shot(tmp_path, monkeypatch, agent):
    from fused_render import agent_oneshot
    path = _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setattr(agent, "_cursor_bin", lambda: path)
    log = tmp_path / "cli.log"
    monkeypatch.setenv("FAKE_CURSOR_LOG", str(log))
    spec = agent_oneshot.prepare("cursor", "", "Be brief.", "Summarise", agent=agent)
    workspace = spec.argv[spec.argv.index("--workspace") + 1]
    assert spec.argv[1:8] == ["-p", "--output-format", "json", "--mode", "ask", "--trust",
                              "--workspace"]
    # `auto` is Cursor's own default: no --model for it.
    assert "--model" not in spec.argv
    assert os.path.isdir(workspace)
    reply = agent_oneshot.run_sync(spec, 20)
    assert reply == "Recap: %d" % len("Be brief.\n\nSummarise")
    assert not os.path.exists(workspace)   # cleaned up


def test_a_one_shot_error_is_typed(tmp_path, monkeypatch, agent):
    from fused_render import agent_oneshot
    path = _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setattr(agent, "_cursor_bin", lambda: path)
    monkeypatch.setenv("FAKE_CURSOR_ONESHOT", "error")
    spec = agent_oneshot.prepare("cursor", "gpt-5.5", "", "x", agent=agent)
    assert spec.argv[spec.argv.index("--model") + 1] == "gpt-5.5"
    with pytest.raises(agent_oneshot.OneShotError) as exc:
        agent_oneshot.run_sync(spec, 20)
    assert exc.value.type_ == "ai_unavailable"


def test_a_tools_one_shot_is_refused(agent):
    from fused_render import agent_oneshot
    with pytest.raises(agent_oneshot.OneShotError):
        agent_oneshot.prepare("cursor", "", "", "x", agent=agent,
                              tools_mcp={"command": "x", "args": []})


# ---------------------------------------------------------------- router

def _switch(monkeypatch, on):
    from fused_render.agent_backends import registry
    real = registry.is_enabled
    monkeypatch.setattr(registry, "is_enabled", lambda b: on if b == "cursor" else real(b))


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.cursor_health import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_MUTATING = ["/api/cursor/health/refresh", "/api/cursor/install", "/api/cursor/login",
             "/api/cursor/login/cancel", "/api/cursor/link-path"]


def test_the_endpoint_answers_and_posts_need_the_header(monkeypatch):
    _switch(monkeypatch, True)
    monkeypatch.setattr(cursor_health, "summary", lambda: {"found": True})
    monkeypatch.setattr(cursor_health, "summary_refreshed", lambda: {"found": False})
    c = _client()
    assert c.get("/api/cursor/health").json() == {"found": True}
    for route in _MUTATING:
        assert c.post(route).status_code == 403, route
    assert c.post("/api/cursor/health/refresh", headers={"X-Fused": "1"}).json() == {
        "found": False}
    assert c.get("/api/cursor/install").json()["state"] == "idle"
    assert c.get("/api/cursor/login").json()["in_flight"] is False


def test_off_by_default_and_nothing_spawns(monkeypatch):
    from fused_render import cursor_install, cursor_login
    from fused_render.agent_backends import registry
    assert registry.is_enabled("cursor") is False

    def boom(*a, **k):
        raise AssertionError("a disabled Cursor was probed or spawned")
    for mod, name in ((cursor_health, "summary"), (cursor_health, "summary_refreshed"),
                      (cursor_health, "_measure"), (cursor_install, "start"),
                      (cursor_login, "start")):
        monkeypatch.setattr(mod, name, boom)
    c = _client()
    assert c.get("/api/cursor/health").json() is None
    for route in _MUTATING:
        if route == "/api/cursor/login/cancel":
            continue
        r = c.post(route, headers={"X-Fused": "1"})
        assert r.status_code == 400 and r.json() == registry.disabled_error("cursor")
