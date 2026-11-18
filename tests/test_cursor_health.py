"""Tests for fused_render/cursor_health.py, cursor_login.py, cursor_install.py
and the /api/cursor/* router.

Same discipline as test_gemini_health.py: no test runs a real `cursor-agent`.
Resolution is driven through a fake tree plus a patched PATH; the probes that
must actually be exercised end to end — `--version`, `status`, `models` and
the `login` child — run against tests/fake_cursor_health_cli.py, a stdlib
`cursor-agent` that answers the way 2026.11.13 was observed to.
"""
import collections
import json
import os
import shutil
import stat
import sys
import time

import pytest

from fused_render import claude_health, codex_health, copilot_health, cursor_health

_FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "fake_cursor_health_cli.py")
_VERSION = "2026.11.13-eacade1"
# Resolved at import: the autouse fixture empties PATH before any test runs.
_BASH = shutil.which("bash") or "/bin/bash"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own shell home (own cache file), a PATH that inherits nothing, no
    override, an own CURSOR_CONFIG_DIR, no key in the environment — the suite
    runner's machine may well have a real, signed-in Cursor."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv(cursor_health.BIN_ENV, raising=False)
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path / "cursor-user"))
    for name in ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "NO_OPEN_BROWSER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cursor_health, "_shell_probe", lambda: None)
    monkeypatch.setattr(claude_health, "candidates", lambda: ())
    monkeypatch.setattr(cursor_health, "candidates", lambda: ())
    monkeypatch.setattr(cursor_health, "_ADOPTED", None)
    from fused_render import cursor_login
    cursor_login.reset()


def _fake_cli(tmp_path, name="cursor-agent", version=_VERSION, executable=True):
    """A findable stand-in whose `--version` prints `version`."""
    d = tmp_path / "fake-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(f"#!/bin/sh\necho '{version}'\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


def _real_fake(tmp_path, monkeypatch, mode="in", where="cursor-agent"):
    """The full fake CLI on PATH as `cursor-agent`, in `mode`."""
    p = tmp_path / "real-bin" / where
    p.parent.mkdir(exist_ok=True)
    p.write_text(f"#!{sys.executable}\nimport runpy\n"
                 f"runpy.run_path({_FAKE!r}, run_name='__main__')\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_CURSOR_MODE", mode)
    monkeypatch.setenv("FAKE_CURSOR_LOG", str(tmp_path / "cli.log"))
    monkeypatch.setenv("FAKE_CURSOR_STATE", str(tmp_path / "cli.state"))
    monkeypatch.setenv("PATH", str(p.parent))
    return str(p)


# -- version -------------------------------------------------------------------


@pytest.mark.parametrize("stdout,stderr,want", [
    ("2026.11.13-eacade1\n", "", "2026.11.13-eacade1"),
    ("2026.11.13-eacade1", "warn /tmp/claude-0/x\n", "2026.11.13-eacade1"),
    ("", "2026.12.01-abc123\n", "2026.12.01-abc123"),
    ("1.2.3\n", "", None),
    ("garbage\n", "", None),
])
def test_probe_version_reads_the_dated_build(monkeypatch, stdout, stderr, want):
    class R:
        returncode = 0
    R.stdout, R.stderr = stdout, stderr
    monkeypatch.setattr(cursor_health, "_run_probe", lambda *a, **k: R())
    assert cursor_health.probe_version("/x/cursor-agent") == want


def test_the_floor_compares_by_date():
    assert cursor_health.is_outdated(cursor_health.MIN_VERSION + "-abc") is False
    assert cursor_health.is_outdated("2099.01.01-abc") is False
    assert cursor_health.is_outdated("2020.01.01-abc") is True
    assert cursor_health.is_outdated(None) is False


# -- resolve order -------------------------------------------------------------


def test_the_candidate_list_covers_the_installer_dirs():
    assert cursor_health.BIN_ENV == "FUSED_RENDER_CURSOR_BIN"
    cands = cursor_health.POSIX_CANDIDATES
    assert cands[0] == "~/.local/bin/cursor-agent"
    assert "~/.cursor/bin/cursor-agent" in cands
    # `agent` is the generic fallback: strictly after every `cursor-agent`.
    names = [os.path.basename(c) for c in cands]
    assert names == sorted(names, key=lambda n: n == "agent")
    assert "agent" in names


def test_override_wins_and_is_reported_as_such(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(cursor_health.BIN_ENV, "/nowhere/cursor-agent")
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    assert cursor_health.resolve() == ("/nowhere/cursor-agent", "override")


def test_path_beats_the_candidate_list(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(cursor_health, "candidates", lambda: ("/nowhere/cursor-agent",))
    assert cursor_health.resolve() == (cli, "path")


def test_candidate_dirs_are_probed_when_path_is_stripped(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(cursor_health, "candidates", lambda: (cli,))
    assert cursor_health.resolve() == (cli, "candidate")


def test_a_non_executable_file_does_not_shadow_a_real_install(tmp_path, monkeypatch):
    dud = _fake_cli(tmp_path, executable=False)
    monkeypatch.setattr(cursor_health, "candidates", lambda: (dud,))
    assert cursor_health.resolve(allow_shell=False) == (None, None)


def test_agent_is_a_fallback_only_and_must_look_like_cursor(tmp_path, monkeypatch):
    """`agent` is a generic name: taken only when no `cursor-agent` resolves
    and only when `--version` prints a dated Cursor build."""
    impostor = _fake_cli(tmp_path, name="agent", version="1.2.3")
    monkeypatch.setenv("PATH", os.path.dirname(impostor))
    assert cursor_health.resolve(allow_shell=False) == (None, None)
    real = _fake_cli(tmp_path, name="agent")
    assert cursor_health.resolve(allow_shell=False) == (real, "path")
    # A real `cursor-agent` always wins over `agent`.
    other = tmp_path / "other-bin"
    other.mkdir()
    named = other / "cursor-agent"
    named.write_text("#!/bin/sh\necho x\n")
    named.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([os.path.dirname(real), str(other)]))
    assert cursor_health.resolve(allow_shell=False) == (str(named), "path")


def test_agent_in_a_candidate_dir_is_verified_too(tmp_path, monkeypatch):
    impostor = _fake_cli(tmp_path, name="agent", version="1.2.3")
    monkeypatch.setattr(cursor_health, "candidates", lambda: (impostor,))
    assert cursor_health.resolve(allow_shell=False) == (None, None)
    real = _fake_cli(tmp_path, name="agent")
    assert cursor_health.resolve(allow_shell=False) == (real, "candidate")


def test_a_shell_only_install_is_adopted_as_the_override(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(cursor_health, "_shell_probe", lambda: cli)
    monkeypatch.setattr(cursor_health, "probe_version", lambda p: _VERSION)
    monkeypatch.setattr(cursor_health, "_status", lambda p: _STATUS_IN)
    monkeypatch.setattr(cursor_health, "_list_models", lambda p: None)
    snap = cursor_health._measure()
    assert (snap["path"], snap["source"]) == (cli, "shell")
    assert os.environ[cursor_health.BIN_ENV] == cli
    # ...and the adoption resolves as "shell", never as a user override.
    assert cursor_health.resolve() == (cli, "shell")


def test_a_user_override_is_never_replaced_by_an_adoption(monkeypatch):
    monkeypatch.setenv(cursor_health.BIN_ENV, "/mine/cursor-agent")
    assert cursor_health.adopt("/found/cursor-agent") is False
    assert os.environ[cursor_health.BIN_ENV] == "/mine/cursor-agent"


def test_config_dir_is_cursor_config_dir_else_dot_cursor(tmp_path, monkeypatch):
    assert cursor_health.config_dir() == str(tmp_path / "cursor-user")
    monkeypatch.delenv("CURSOR_CONFIG_DIR")
    assert cursor_health.config_dir() == os.path.expanduser("~/.cursor")


# -- status parsing ------------------------------------------------------------

_STATUS_IN = {"signed_in": True, "email": "dev@example.com"}


@pytest.mark.parametrize("out,code,want", [
    ("\n Not logged in\n", 0, {"signed_in": False, "email": None}),
    ("not logged in", 0, {"signed_in": False, "email": None}),
    ("Error: Authentication required", 1, {"signed_in": False, "email": None}),
    ("\x1b[32m✓\x1b[0m Logged in as dev@example.com", 0,
     {"signed_in": True, "email": "dev@example.com"}),
    ("Signed in\nAccount: Dev.Name+x@corp.example.io (Pro)", 0,
     {"signed_in": True, "email": "Dev.Name+x@corp.example.io"}),
    ("Logged in", 0, {"signed_in": True, "email": None}),
    # A failure that is no sign-out says nothing about the credential.
    ("Error: getaddrinfo ENOTFOUND api2.cursor.sh", 1, None),
    ("503 Service Unavailable", 1, None),
    ("", 2, None),
])
def test_parse_status_table(out, code, want):
    assert cursor_health.parse_status(out, code) == want


def test_status_runs_the_status_subcommand(tmp_path, monkeypatch):
    cli = _real_fake(tmp_path, monkeypatch, "in")
    assert cursor_health._status(cli) == _STATUS_IN
    log = json.loads((tmp_path / "cli.log").read_text())
    assert log["argv"] == ["status"]
    # A health read never opens a browser.
    assert log["env"]["NO_OPEN_BROWSER"] == "1"
    monkeypatch.setenv("FAKE_CURSOR_MODE", "out")
    assert cursor_health._status(cli) == {"signed_in": False, "email": None}
    monkeypatch.setenv("FAKE_CURSOR_MODE", "out-exit")
    assert cursor_health._status(cli) == {"signed_in": False, "email": None}


def test_a_hung_status_is_unknown_not_signed_out(tmp_path, monkeypatch):
    cli = _real_fake(tmp_path, monkeypatch, "hang")
    monkeypatch.setattr(cursor_health, "_STATUS_TIMEOUT_S", 0.5)
    assert cursor_health._status(cli) is None
    assert cursor_health.signed_in(cli) is None


def test_signed_in_without_a_cli_is_unknown():
    assert cursor_health.signed_in(None) is None


def test_an_api_key_in_the_environment_is_signed_in_without_a_status_call(monkeypatch):
    monkeypatch.setattr(cursor_health, "_status",
                        lambda p: pytest.fail("a key in the environment was status-probed"))
    for name in ("CURSOR_API_KEY", "CURSOR_AUTH_TOKEN"):
        monkeypatch.setenv(name, "k")
        assert cursor_health.signed_in("/x/cursor-agent") is True
        assert cursor_health.account(True) == {"method": "api_key", "email": None,
                                               "org": None, "plan": None}
        monkeypatch.delenv(name)


def test_account_names_the_email():
    assert cursor_health.account(True, "dev@example.com") == {
        "method": "login", "email": "dev@example.com", "org": None, "plan": None}
    assert cursor_health.account(True)["email"] is None
    assert cursor_health.account(False) is None and cursor_health.account(None) is None


# -- models --------------------------------------------------------------------


def test_parse_models_is_tolerant_of_decoration():
    text = ("Loading models…\n\nAvailable models\n\n"
            "auto - Auto (default)\n"
            "  • composer-1 — Composer 1\n"
            "* gpt-5: GPT-5\n"
            "\x1b[1msonnet-4.5\x1b[0m - Claude 4.5 Sonnet (current)\n"
            "sonnet-4.5-thinking\n"
            "auto - dup\n\n"
            "Tip: use --model <id> to switch.")
    rows, current = cursor_health.parse_models(text)
    assert [r["id"] for r in rows] == ["auto", "composer-1", "gpt-5", "sonnet-4.5",
                                       "sonnet-4.5-thinking"]
    assert rows[0]["label"] == "Auto" and rows[1]["label"] == "Composer 1"
    assert rows[3]["label"] == "Claude 4.5 Sonnet"
    assert rows[4]["label"] == "sonnet-4.5-thinking"
    assert current == "sonnet-4.5"


def test_parse_models_bare_ids_and_empty():
    rows, current = cursor_health.parse_models("auto\ngpt-5\n")
    assert [r["id"] for r in rows] == ["auto", "gpt-5"] and current is None
    assert cursor_health.parse_models("") == ([], None)
    assert cursor_health.parse_models("Loading models…\n") == ([], None)


def test_normalize_models_drops_junk_and_duplicates():
    out = cursor_health.normalize_models([
        {"id": "auto", "label": "Auto", "description": "Cursor picks"},
        {"modelId": "gpt-5", "name": "GPT-5"},
        {"id": "gpt-5"}, {"label": "no id"}, "junk", None])
    assert out == [{"id": "auto", "label": "Auto", "description": "Cursor picks"},
                   {"id": "gpt-5", "label": "GPT-5", "description": None}]
    assert cursor_health.normalize_models(None) == []


def test_models_come_from_the_models_subcommand(tmp_path, monkeypatch):
    cli = _real_fake(tmp_path, monkeypatch, "in")
    rows, current = cursor_health._list_models(cli)
    assert [r["id"] for r in rows] == ["auto", "composer-1", "gpt-5", "sonnet-4.5"]
    assert current == "sonnet-4.5"
    assert json.loads((tmp_path / "cli.log").read_text())["argv"] == ["models"]
    monkeypatch.setenv("FAKE_CURSOR_MODE", "models-fail")
    assert cursor_health._list_models(cli) is None


# -- snapshot shape and cache --------------------------------------------------


def _quiet_claude(monkeypatch):
    for name in ("_shell_probe", "_auth_status", "_doctor"):
        monkeypatch.setattr(claude_health, name, lambda *a, **k: None)
    monkeypatch.setattr(claude_health, "probe_version", lambda p: None)


def test_snapshot_keys_match_the_other_agents(monkeypatch):
    _quiet_claude(monkeypatch)
    for mod in (copilot_health, codex_health):
        monkeypatch.setattr(mod, "_shell_probe", lambda: None)
        monkeypatch.setattr(mod, "candidates", lambda: ())
    theirs = set(claude_health._public(claude_health._measure(allow_shell=False)))
    ours = set(cursor_health._public(cursor_health._measure(allow_shell=False)))
    assert theirs <= ours, theirs - ours
    codexs = set(codex_health._public(codex_health._measure(allow_shell=False)))
    # One extra key of our own: the model the CLI marks current.
    assert ours == codexs | {"current_model"}


def test_a_missing_cli_snapshot():
    snap = cursor_health._measure(allow_shell=False)
    assert snap["found"] is False and snap["signed_in"] is None
    assert snap["doctor"] is None and snap["account"] is None and snap["models"] is None
    assert snap["install_command"] == "curl https://cursor.com/install -fsS | bash"
    assert snap["install_method"] is None
    assert snap["config_dir"] == cursor_health.config_dir()
    assert snap["platform"] == sys.platform


def test_a_full_measure_against_the_fake_cli(tmp_path, monkeypatch):
    _real_fake(tmp_path, monkeypatch, "in")
    snap = cursor_health.summary_refreshed()
    assert snap["found"] is True and snap["source"] == "path"
    assert snap["version"] == _VERSION and snap["outdated"] is False
    assert snap["signed_in"] is True
    assert snap["account"] == {"method": "login", "email": "dev@example.com",
                               "org": None, "plan": None}
    assert [m["id"] for m in snap["models"]] == [
        "auto", "composer-1", "gpt-5", "sonnet-4.5"]
    assert snap["current_model"] == "sonnet-4.5"
    assert "fingerprint" not in snap
    assert os.path.basename(cursor_health._cache_path()) == "cursor-health.json"
    # The catalog's discovered list reads this very cache.
    assert [r["id"] for r in cursor_health.cached_models()] == [
        "auto", "composer-1", "gpt-5", "sonnet-4.5"]
    # Signed out: no account, and `models` is never attempted.
    monkeypatch.setenv("FAKE_CURSOR_MODE", "out")
    snap = cursor_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None and snap["models"] is None
    assert json.loads((tmp_path / "cli.log").read_text())["argv"] == ["status"]


def test_signed_in_with_a_failing_model_list_keeps_the_last_list(tmp_path, monkeypatch):
    _real_fake(tmp_path, monkeypatch, "in")
    cursor_health.summary_refreshed()
    monkeypatch.setenv("FAKE_CURSOR_MODE", "models-fail")
    snap = cursor_health.summary_refreshed()
    assert snap["signed_in"] is True
    assert [m["id"] for m in snap["models"]][:1] == ["auto"]


def test_snapshot_is_cached_then_served_from_disk(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(cursor_health, "probe_version",
                        lambda p: calls.append(p) or _VERSION)
    monkeypatch.setattr(cursor_health, "_status", lambda p: _STATUS_IN)
    monkeypatch.setattr(cursor_health, "_list_models", lambda p: None)
    first = cursor_health.summary()
    assert first["version"] == _VERSION and first["signed_in"] is True
    assert os.path.isfile(cursor_health._cache_path())
    second = cursor_health.summary()
    assert second == first and len(calls) == 1
    assert cursor_health.cached()["version"] == _VERSION


def test_an_upgraded_binary_a_moved_home_or_a_new_key_invalidates_the_cache(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(cursor_health, "probe_version",
                        lambda p: calls.append(p) or _VERSION)
    monkeypatch.setattr(cursor_health, "_status", lambda p: _STATUS_IN)
    monkeypatch.setattr(cursor_health, "_list_models", lambda p: None)
    cursor_health.summary()
    os.utime(cli, (time.time() + 100, time.time() + 100))
    cursor_health.summary()
    assert len(calls) == 2
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert cursor_health.cached() is None
    cursor_health.summary()
    assert len(calls) == 3
    monkeypatch.setenv("CURSOR_API_KEY", "key-SECRET-VALUE")
    assert cursor_health.cached() is None
    cursor_health.summary()
    assert len(calls) == 4
    snap = json.loads(open(cursor_health._cache_path()).read())
    assert "key-SECRET-VALUE" not in json.dumps(snap)   # never the key's value


def test_an_unknown_probe_carries_the_last_account_and_models_forward(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(cursor_health, "probe_version", lambda p: _VERSION)
    monkeypatch.setattr(cursor_health, "_status", lambda p: _STATUS_IN)
    monkeypatch.setattr(cursor_health, "_list_models",
                        lambda p: ([{"id": "auto", "label": "Auto",
                                     "description": None}], "auto"))
    assert cursor_health.summary()["account"]["email"] == "dev@example.com"
    monkeypatch.setattr(cursor_health, "_status", lambda p: None)
    snap = cursor_health.summary_refreshed()
    assert snap["signed_in"] is None
    assert snap["account"]["email"] == "dev@example.com"
    assert [m["id"] for m in snap["models"]] == ["auto"]
    assert snap["current_model"] == "auto"
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert cursor_health.summary_refreshed()["account"] is None


def test_a_definite_sign_out_does_not_carry_the_account(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(cursor_health, "probe_version", lambda p: _VERSION)
    monkeypatch.setattr(cursor_health, "_status", lambda p: _STATUS_IN)
    monkeypatch.setattr(cursor_health, "_list_models",
                        lambda p: ([{"id": "auto", "label": "Auto",
                                     "description": None}], None))
    cursor_health.summary()
    monkeypatch.setattr(cursor_health, "_status",
                        lambda p: {"signed_in": False, "email": None})
    snap = cursor_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None and snap["models"] is None


def test_refresh_re_probes_and_a_corrupt_cache_is_tolerated():
    os.makedirs(os.path.dirname(cursor_health._cache_path()), exist_ok=True)
    with open(cursor_health._cache_path(), "w") as f:
        f.write("{not json")
    assert cursor_health.summary()["found"] is False
    assert cursor_health.summary_refreshed()["found"] is False
    assert cursor_health.cached() is not None


# -- install method and the update offer ---------------------------------------

_CURL = "curl https://cursor.com/install -fsS | bash"


@pytest.mark.parametrize("path,method,updatable,command", [
    ("/home/x/.local/bin/cursor-agent", "curl", True, _CURL),
    ("/Users/x/.cursor/bin/cursor-agent", "curl", True, _CURL),
    ("/opt/homebrew/bin/cursor-agent", "brew", False, "brew upgrade --cask cursor-cli"),
    ("/srv/tools/cursor-agent", "other", None, _CURL),
    (None, None, None, _CURL),
])
def test_install_method_and_update_plan(path, method, updatable, command):
    assert cursor_health.install_method(path) == method
    plan = cursor_health.update_plan(method)
    assert plan["updatable"] is updatable
    assert plan["command"] == command


def test_the_install_command_is_the_curl_installer():
    assert cursor_health.install_command() == cursor_health.INSTALL_COMMAND_POSIX == _CURL


# -- login ---------------------------------------------------------------------


def test_login_refuses_without_a_cli():
    from fused_render import cursor_login
    with pytest.raises(cursor_login.LoginError):
        cursor_login.start()


def test_login_runs_cursor_agent_login_under_a_pty_with_a_browser_and_re_probes(
        tmp_path, monkeypatch):
    from fused_render import cursor_login
    monkeypatch.setenv("NO_OPEN_BROWSER", "1")   # the health probe's, not ours
    _real_fake(tmp_path, monkeypatch, "login-ok")
    probes = []
    monkeypatch.setattr(cursor_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    cursor_login.reset()
    rec = cursor_login.start()
    assert rec["in_flight"] is True
    login = cursor_login._active
    assert login.settled.wait(10)
    st = cursor_login.status()
    assert st["in_flight"] is False and st["error"] is None
    assert probes == [1]
    log = json.loads((tmp_path / "cli.log").read_text())
    assert log["argv"] == ["login"]
    assert log["tty"] is True
    assert log["env"]["NO_OPEN_BROWSER"] is None
    # The printed URL line is captured for the strip's fallback link.
    assert st["detail"]["url"].startswith("https://cursor.com/")
    assert cursor_login.cancel()["canceled"] is False
    cursor_login.reset()


def test_a_failed_login_reports_the_childs_last_real_line_and_does_not_re_probe(
        tmp_path, monkeypatch):
    from fused_render import cursor_login
    _real_fake(tmp_path, monkeypatch, "login-fail")
    probes = []
    monkeypatch.setattr(cursor_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    cursor_login.reset()
    cursor_login.start()
    assert cursor_login._active.settled.wait(10)
    err = cursor_login.status()["error"]
    assert err == "Authentication timed out."
    assert "SECRET" not in err
    assert probes == []
    cursor_login.reset()


def test_a_second_login_is_refused_while_one_waits_and_cancel_stops_it(tmp_path, monkeypatch):
    from fused_render import cursor_login
    _real_fake(tmp_path, monkeypatch, "login-hang")
    cursor_login.reset()
    cursor_login.start()
    with pytest.raises(cursor_login.LoginError, match="already waiting"):
        cursor_login.start()
    out = cursor_login.cancel()
    assert out["canceled"] is True and out["in_flight"] is False and out["error"] is None
    assert cursor_login._active.proc.poll() is not None
    cursor_login.reset()


def test_login_failure_sentence_skips_urls_and_prompts():
    from fused_render import cursor_login
    login = cursor_login._Login(proc=None, started_at=0,
                                tail=collections.deque(maxlen=40))
    for line in ("Opening browser to log in...",
                 "https://cursor.com/loginDeepControl?challenge=SECRET",
                 "Waiting for authentication...",
                 "Authentication timed out."):
        cursor_login._note_line(login, line)
    assert cursor_login._failure(login, 1) == "Authentication timed out."
    login = cursor_login._Login(proc=None, started_at=0, tail=collections.deque(maxlen=40))
    assert cursor_login._failure(login, 1) == \
        "the sign-in ended without signing in (exit code 1)"


# -- install -------------------------------------------------------------------


def _fake_shell_tools(tmp_path, monkeypatch):
    """bash and curl on a PATH that has nothing else; curl's output is the
    "installer", which the real bash then runs."""
    d = tmp_path / "tools-bin"
    d.mkdir()
    bash = d / "bash"
    bash.symlink_to(_BASH)
    curl = d / "curl"
    curl.write_text("#!/bin/sh\necho 'echo installing cursor'\n")
    curl.chmod(0o755)
    monkeypatch.setenv("PATH", str(d))
    return str(bash)


def test_install_has_its_own_slot_and_argv(tmp_path, monkeypatch):
    from fused_render import claude_install, codex_install, copilot_install, cursor_install
    assert cursor_install.JOB_ID == "sys:cursor-install"
    assert cursor_install.JOB_ID not in (claude_install.JOB_ID, copilot_install.JOB_ID,
                                         codex_install.JOB_ID)
    bash = _fake_shell_tools(tmp_path, monkeypatch)
    argv, display = cursor_install.install_argv()
    assert argv[0] == bash and argv[1] == "-c"
    assert cursor_health.INSTALL_COMMAND_POSIX in argv[2]
    assert "pipefail" in argv[2]
    assert display == cursor_health.INSTALL_COMMAND_POSIX
    assert cursor_install.update_argv("/x/cursor-agent") == (argv, display)


def test_install_refuses_without_curl_or_on_windows(tmp_path, monkeypatch):
    from fused_render import cursor_install
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    with pytest.raises(cursor_install.InstallError, match="curl"):
        cursor_install.install_argv()
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(cursor_install.InstallError, match="Windows"):
        cursor_install.install_argv()


def _wait_install():
    from fused_render import cursor_install
    for _ in range(100):
        if cursor_install.status()["state"] != "running":
            break
        time.sleep(0.05)
    return cursor_install.status()


def test_install_runs_the_installer_to_done_and_re_probes(tmp_path, monkeypatch):
    from fused_render import cursor_install
    cursor_install.reset()
    _fake_shell_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(cursor_health, "summary_refreshed",
                        lambda: {"found": True, "version": _VERSION})
    monkeypatch.setattr(cursor_install, "_report", lambda s: None)
    rec = cursor_install.start("install")
    assert rec["state"] == "running" and rec["command"] == _CURL
    st = _wait_install()
    assert st["state"] == "done", st
    assert "installing cursor" in st["output"]
    assert st["detail"] == f"Cursor CLI {_VERSION}"
    cursor_install.reset()


def test_install_fails_when_the_installer_fails_or_nothing_is_found(tmp_path, monkeypatch):
    from fused_render import cursor_install
    cursor_install.reset()
    _fake_shell_tools(tmp_path, monkeypatch)
    monkeypatch.setattr(cursor_install, "_report", lambda s: None)
    # A curl that fails must fail the pipeline, not be masked by bash's exit 0.
    (tmp_path / "tools-bin" / "curl").write_text("#!/bin/sh\nexit 22\n")
    cursor_install.start("install")
    st = _wait_install()
    assert st["state"] == "error" and "exited with code" in st["error"]
    cursor_install.reset()
    (tmp_path / "tools-bin" / "curl").write_text("#!/bin/sh\necho true\n")
    monkeypatch.setattr(cursor_health, "summary_refreshed", lambda: {"found": False})
    cursor_install.start("install")
    st = _wait_install()
    assert st["state"] == "error" and "still cannot be found" in st["error"]
    cursor_install.reset()


# -- the router ----------------------------------------------------------------


def _cursor_switch(monkeypatch, on):
    from fused_render.agent_backends import registry
    real = registry.is_enabled
    monkeypatch.setattr(registry, "is_enabled",
                        lambda b: on if b == "cursor" else real(b))


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.cursor_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_MUTATING = ["/api/cursor/health/refresh", "/api/cursor/install",
             "/api/cursor/login", "/api/cursor/login/cancel",
             "/api/cursor/link-path"]


def test_endpoint_answers_the_snapshot_and_posts_need_the_header(monkeypatch):
    _cursor_switch(monkeypatch, True)
    monkeypatch.setattr(cursor_health, "summary",
                        lambda: {"found": True, "version": _VERSION})
    monkeypatch.setattr(cursor_health, "summary_refreshed", lambda: {"found": False})
    c = _client()
    assert c.get("/api/cursor/health").json() == {"found": True, "version": _VERSION}
    for route in _MUTATING:
        assert c.post(route).status_code == 403, route


def test_the_default_switch_is_off_and_gates_every_spawn(monkeypatch):
    """Cursor is off until the user turns it on; no route may spawn it."""
    from fused_render import cursor_install, cursor_login
    from fused_render.agent_backends import registry
    assert registry.is_enabled("cursor") is False

    def boom(*a, **k):
        raise AssertionError("a disabled Cursor was probed or spawned")
    for mod, name in ((cursor_health, "summary"), (cursor_health, "summary_refreshed"),
                      (cursor_health, "_measure"), (cursor_health, "add_to_shell_path"),
                      (cursor_install, "start"), (cursor_login, "start")):
        monkeypatch.setattr(mod, name, boom)
    c = _client()
    assert c.get("/api/cursor/health").json() is None
    for route in _MUTATING:
        if route == "/api/cursor/login/cancel":
            continue
        r = c.post(route, headers={"X-Fused": "1"})
        assert r.status_code == 400, route
        assert r.json() == registry.disabled_error("cursor")
    # Cancel is never gated: it only signals our own child.
    assert c.post("/api/cursor/login/cancel", headers={"X-Fused": "1"}).json()["canceled"] is False


def test_the_server_app_mounts_the_cursor_routes():
    from fused_render.server import app as server_app
    from fused_render.server.routers.cursor_health import router
    assert server_app.cursor_health_router is router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert paths == {"/api/cursor/health", "/api/cursor/health/refresh",
                     "/api/cursor/install", "/api/cursor/link-path",
                     "/api/cursor/login", "/api/cursor/login/cancel"}
