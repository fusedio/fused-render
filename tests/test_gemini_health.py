"""Tests for fused_render/gemini_health.py, gemini_login.py, gemini_install.py
and the /api/gemini/* router.

Same discipline as test_codex_health.py: no test runs a real `gemini`.
Resolution is driven through a fake tree plus a patched PATH; the probes that
must actually be exercised end to end — the ACP sign-in handshake and the ACP
`authenticate` sign-in — run against a fake `gemini` written in Python that
speaks the JSON-RPC messages and answers the way 0.65.0 was observed to
(docs/GEMINI_BACKEND.md D10).
"""
import collections
import json
import os
import stat
import sys
import textwrap
import time

import pytest

from fused_render import claude_health, codex_health, copilot_health, gemini_health


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own shell home (own cache file), a PATH that inherits nothing, no
    override, an own GEMINI_CLI_HOME, no key in the environment — the suite
    runner's machine may well have a real, signed-in Gemini."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv(gemini_health.BIN_ENV, raising=False)
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini-user"))
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
                 "NO_BROWSER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gemini_health, "_shell_probe", lambda: None)
    monkeypatch.setattr(claude_health, "candidates", lambda: ())
    monkeypatch.setattr(gemini_health, "candidates", lambda: ())
    monkeypatch.setattr(gemini_health, "_ADOPTED", None)
    from fused_render import gemini_login
    gemini_login.reset()


def _fake_cli(tmp_path, name="gemini", executable=True):
    """A findable stand-in; never spawned by the resolve tests."""
    d = tmp_path / "fake-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\necho '0.65.0'\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


def _gemini_dir():
    d = gemini_health.config_dir()
    os.makedirs(d, exist_ok=True)
    return d


def _settings(selected):
    with open(os.path.join(_gemini_dir(), "settings.json"), "w") as fh:
        json.dump({"security": {"auth": {"selectedType": selected}}}, fh)


# -- version -------------------------------------------------------------------


@pytest.mark.parametrize("stdout,stderr,want", [
    ("0.65.0\n", "", "0.65.0"),
    ("0.65.0", "npm WARN /tmp/claude-0/x\n", "0.65.0"),
    ("", "0.66.1\n", "0.66.1"),
    ("garbage\n", "", None),
])
def test_probe_version_reads_the_bare_number(monkeypatch, stdout, stderr, want):
    class R:
        returncode = 0
    R.stdout, R.stderr = stdout, stderr
    monkeypatch.setattr(gemini_health, "_run_probe", lambda *a, **k: R())
    assert gemini_health.probe_version("/x/gemini") == want


def test_the_declared_floor_is_the_verified_version():
    assert gemini_health.MIN_VERSION == "0.65.0"
    assert gemini_health.is_outdated("0.65.0") is False
    assert gemini_health.is_outdated("0.66.0") is False
    assert gemini_health.is_outdated("0.64.9") is True
    assert gemini_health.is_outdated(None) is False
    assert gemini_health.is_outdated("1.0.0") is False   # not Claude's 2.0.0 floor


# -- resolve order -------------------------------------------------------------


def test_the_candidate_list_is_the_contract_s_six_in_order():
    assert gemini_health.BIN_ENV == "FUSED_RENDER_GEMINI_BIN"
    assert gemini_health.POSIX_CANDIDATES == (
        "~/.npm-global/bin/gemini",
        "/opt/homebrew/bin/gemini",
        "/usr/local/bin/gemini",
        "~/.local/bin/gemini",
        "~/.bun/bin/gemini",
        "~/Library/pnpm/gemini",
    )


def test_override_wins_and_is_reported_as_such(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(gemini_health.BIN_ENV, "/nowhere/gemini")
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    assert gemini_health.resolve() == ("/nowhere/gemini", "override")


def test_path_beats_the_candidate_list(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(gemini_health, "candidates", lambda: ("/nowhere/gemini",))
    assert gemini_health.resolve() == (cli, "path")


def test_candidate_dirs_are_probed_when_path_is_stripped(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(gemini_health, "candidates", lambda: (cli,))
    assert gemini_health.resolve() == (cli, "candidate")


def test_a_non_executable_file_does_not_shadow_a_real_install(tmp_path, monkeypatch):
    dud = _fake_cli(tmp_path, executable=False)
    monkeypatch.setattr(gemini_health, "candidates", lambda: (dud,))
    assert gemini_health.resolve(allow_shell=False) == (None, None)


def test_a_shell_only_install_is_adopted_as_the_override(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(gemini_health, "_shell_probe", lambda: cli)
    monkeypatch.setattr(gemini_health, "probe_version", lambda p: "0.65.0")
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_IN)
    snap = gemini_health._measure()
    assert (snap["path"], snap["source"]) == (cli, "shell")
    assert os.environ[gemini_health.BIN_ENV] == cli
    # ...and the adoption resolves as "shell", never as a user override.
    assert gemini_health.resolve() == (cli, "shell")


def test_a_user_override_is_never_replaced_by_an_adoption(monkeypatch):
    monkeypatch.setenv(gemini_health.BIN_ENV, "/mine/gemini")
    assert gemini_health.adopt("/found/gemini") is False
    assert os.environ[gemini_health.BIN_ENV] == "/mine/gemini"


def test_config_dir_is_dot_gemini_under_gemini_cli_home_else_home(tmp_path, monkeypatch):
    assert gemini_health.config_dir() == str(tmp_path / "gemini-user" / ".gemini")
    monkeypatch.delenv("GEMINI_CLI_HOME")
    assert gemini_health.config_dir() == os.path.expanduser("~/.gemini")


# -- the credential's files ----------------------------------------------------


def test_the_method_comes_from_the_settings_file_then_the_environment(monkeypatch):
    assert gemini_health.selected_auth() is None
    assert gemini_health.method() is None
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert gemini_health.method() == "api_key"
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    assert gemini_health.method() == "vertex"
    for selected, want in (("oauth-personal", "google"), ("gemini-api-key", "api_key"),
                           ("vertex-ai", "vertex"), ("gateway", "gateway"),
                           ("something-new", "something-new")):
        _settings(selected)
        assert gemini_health.selected_auth() == selected
        assert gemini_health.method() == want
    with open(os.path.join(_gemini_dir(), "settings.json"), "w") as fh:
        fh.write("{not json")
    assert gemini_health.selected_auth() is None


def test_a_google_login_without_a_token_is_signed_out_without_a_spawn(monkeypatch):
    """The probe must never start the CLI's OAuth flow: with
    `oauth-personal` picked and no `oauth_creds.json`, the files answer."""
    _settings("oauth-personal")
    assert gemini_health.oauth_cached() is False
    monkeypatch.setattr(gemini_health, "_acp_probe",
                        lambda p: pytest.fail("a Google login with no token was probed"))
    assert gemini_health.signed_in("/x/gemini") is False
    assert gemini_health.account(False) is None
    # With a token on disk the CLI is asked.
    with open(os.path.join(_gemini_dir(), "oauth_creds.json"), "w") as fh:
        json.dump({"access_token": "x"}, fh)
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_IN)
    assert gemini_health.signed_in("/x/gemini") is True


def test_account_names_the_method_and_the_cached_google_email():
    _settings("oauth-personal")
    with open(os.path.join(_gemini_dir(), "google_accounts.json"), "w") as fh:
        json.dump({"active": "dev@example.com", "old": []}, fh)
    assert gemini_health.account(True) == {"method": "google", "email": "dev@example.com",
                                           "org": None, "plan": None}
    _settings("gemini-api-key")
    assert gemini_health.account(True) == {"method": "api_key", "email": None,
                                           "org": None, "plan": None}
    assert gemini_health.account(False) is None and gemini_health.account(None) is None
    os.remove(os.path.join(_gemini_dir(), "settings.json"))
    assert gemini_health.account(True)["method"] == "unknown"


# -- the ACP sign-in probe -----------------------------------------------------

_SIGNED_IN = {"id": 2, "result": {
    "sessionId": "3f2c6c1e-5b1a-4c2d-9e3f-0a1b2c3d4e5f",
    "modes": {"availableModes": [{"id": "default", "name": "Default"}],
              "currentModeId": "default"},
    "models": {"availableModels": [
        {"modelId": "auto", "name": "Auto", "description": "Gemini picks"},
        {"modelId": "gemini-3-pro-preview", "name": "Gemini 3 Pro"},
        {"modelId": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        {"modelId": "gemini-2.5-flash", "name": "dup"}, {"name": "no id"}, "junk"],
        "currentModelId": "auto"}}}
_SIGNED_OUT = {"id": 2, "error": {"code": -32000,
                                  "message": "Gemini API key is missing or not configured."}}


_FAKE_ACP = textwrap.dedent("""\
    #!{python}
    import json, os, signal, sys, time
    mode = os.environ.get("FAKE_GEMINI_MODE", "in")
    if sys.argv[1:] == ["--version"]:
        print("0.65.0")
        sys.exit(0)
    assert sys.argv[1:] == ["--acp"], sys.argv
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    got = []

    def log():
        with open(os.environ["FAKE_GEMINI_LOG"], "w") as f:
            json.dump({{"pid": os.getpid(), "cwd": os.getcwd(), "got": got,
                       "env": {{k: os.environ.get(k) for k in (
                           "NO_BROWSER", "GEMINI_CLI_HOME", "PYTHONPATH",
                           "GEMINI_CLI_TRUST_WORKSPACE")}}}}, f)

    def send(msg):
        print(json.dumps(msg), flush=True)

    log()
    for line in sys.stdin:
        msg = json.loads(line)
        got.append(msg)
        log()
        method = msg.get("method")
        if method == "initialize":
            send({{"jsonrpc": "2.0", "id": msg["id"], "result": {{
                "protocolVersion": 1, "agentInfo": {{"name": "gemini-cli", "version": "0.65.0"}},
                "authMethods": [{{"id": "oauth-personal", "name": "Log in with Google"}}]}}}})
        elif method == "session/new":
            if mode in ("hang", "stubborn"):
                time.sleep(60)
            if mode == "oauth":
                # The web flow under NO_BROWSER: prose on stdout, then a wait.
                print("Please visit the following URL to authorize the application:", flush=True)
                print("https://accounts.google.com/o/oauth2/v2/auth?state=X", flush=True)
                time.sleep(60)
            if mode == "garbage":
                print("not json at all", flush=True)
                sys.exit(1)
            if mode == "noise":
                send({{"jsonrpc": "2.0", "id": 2, "method": "session/request_permission", "params": {{}}}})
                send({{"jsonrpc": "2.0", "method": "session/update", "params": {{"update": {{"sessionUpdate": "available_commands_update"}}}}}})
            if mode == "out":
                send({{"jsonrpc": "2.0", "id": msg["id"], "error": {{"code": -32000,
                       "message": "Gemini API key is missing or not configured."}}}})
            elif mode == "auth":
                send({{"jsonrpc": "2.0", "id": msg["id"], "error": {{"code": -32000,
                       "message": "Authentication required."}}}})
            elif mode == "other":
                send({{"jsonrpc": "2.0", "id": msg["id"], "error": {{"code": -32000,
                       "message": "Workspace is not trusted."}}}})
            elif mode == "internal":
                send({{"jsonrpc": "2.0", "id": msg["id"], "error": {{"code": -32603,
                       "message": "Authentication required."}}}})
            else:
                send({{"jsonrpc": "2.0", "id": msg["id"], "result": {signed_in}}})
        elif method == "authenticate":
            if mode == "auth-fail":
                send({{"jsonrpc": "2.0", "id": msg["id"], "error": {{"code": -32000,
                       "message": "Authentication cancelled by user."}}}})
            elif mode == "auth-hang":
                time.sleep(60)
            else:
                print("Attempting to open authentication page in your browser.", flush=True)
                print("https://accounts.google.com/o/oauth2/v2/auth?state=SECRET", flush=True)
                send({{"jsonrpc": "2.0", "id": msg["id"], "result": None}})
    # stdin EOF: the real ACP server exits here.
""")


@pytest.fixture
def fake_acp(tmp_path, monkeypatch):
    """A `gemini` that speaks ACP. The shebang is this interpreter: the
    isolation fixture's PATH has no python on it."""
    p = tmp_path / "acp-bin" / "gemini"
    p.parent.mkdir()
    p.write_text(_FAKE_ACP.format(python=sys.executable,
                                  signed_in=json.dumps(_SIGNED_IN["result"])))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_GEMINI_LOG", str(log))

    def run(mode):
        monkeypatch.setenv("FAKE_GEMINI_MODE", mode)
        return gemini_health._acp_probe(str(p))

    run.log = lambda: json.loads(log.read_text())
    run.path = str(p)
    return run


def test_a_session_result_means_signed_in_and_carries_the_models(fake_acp):
    reply = fake_acp("in")
    assert gemini_health._classify(reply) is True
    assert gemini_health._models_of(reply) == [
        {"id": "auto", "label": "Auto", "description": "Gemini picks"},
        {"id": "gemini-3-pro-preview", "label": "Gemini 3 Pro", "description": None},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash", "description": None}]
    assert gemini_health._current_model_of(reply) == "auto"


@pytest.mark.parametrize("mode,want", [("out", False), ("auth", False), ("other", None),
                                       ("internal", None), ("garbage", None), ("noise", True)])
def test_the_reply_classifies_as_the_contract_says(fake_acp, mode, want):
    assert gemini_health._classify(fake_acp(mode)) is want


def test_the_handshake_is_initialize_then_session_new_on_the_probe_dir(fake_acp):
    fake_acp("in")
    got = fake_acp.log()["got"]
    assert [m["method"] for m in got] == ["initialize", "session/new"]
    assert got[0]["params"]["protocolVersion"] == 1
    assert got[1]["params"]["mcpServers"] == []
    probe_dir = got[1]["params"]["cwd"]
    assert probe_dir == gemini_health._probe_dir() and os.path.isdir(probe_dir)
    # ONE fixed dir under fused's own home, so Gemini's project registry
    # never grows by one entry a probe.
    assert probe_dir.startswith(os.environ["FUSED_RENDER_HOME"])
    assert fake_acp("in") and fake_acp.log()["got"][1]["params"]["cwd"] == probe_dir


def test_the_child_never_opens_a_browser_and_trusts_the_probe_dir(fake_acp, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/leaks")
    fake_acp("in")
    env = fake_acp.log()["env"]
    assert env["NO_BROWSER"] == "1"
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "true"
    assert env["GEMINI_CLI_HOME"] == os.environ["GEMINI_CLI_HOME"]
    assert env["PYTHONPATH"] is None
    assert fake_acp.log()["cwd"] != gemini_health._probe_dir()   # no chdir


def test_the_oauth_prompt_is_unknown_never_signed_out(fake_acp, monkeypatch):
    monkeypatch.setattr(gemini_health, "_ACP_TIMEOUT_S", 0.8)
    assert fake_acp("oauth") is None


def _assert_reaped(pid):
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("the probe's child is still alive")


@pytest.mark.parametrize("mode", ["hang", "stubborn"])
def test_a_hung_server_is_unknown_and_is_killed(fake_acp, monkeypatch, mode):
    monkeypatch.setattr(gemini_health, "_ACP_TIMEOUT_S", 0.5)
    monkeypatch.setattr(gemini_health, "_REAP_GRACE_S", 0.3)
    assert fake_acp(mode) is None
    _assert_reaped(fake_acp.log()["pid"])


@pytest.mark.parametrize("reply,want", [
    ({"result": {"sessionId": "abc"}}, True),
    ({"result": {}}, None),
    ({"error": {"code": -32000, "message": "Gemini API key is missing or not configured."}}, False),
    ({"error": {"code": -32000, "message": "Authentication required."}}, False),
    ({"error": {"code": -32000, "message": "invalid_grant"}}, False),
    ({"error": {"code": -32000, "message": "Workspace is not trusted."}}, None),
    ({"error": {"code": -32603, "message": "Authentication required."}}, None),
    ({"error": "nope"}, None),
    (None, None),
])
def test_classify_table(reply, want):
    assert gemini_health._classify(reply) is want


def test_signed_in_without_a_cli_is_unknown(monkeypatch):
    assert gemini_health.signed_in(None) is None
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_OUT)
    assert gemini_health.signed_in("/x/gemini") is False


def test_normalize_models_drops_junk_and_duplicates():
    rows = gemini_health.normalize_models(_SIGNED_IN["result"]["models"]["availableModels"])
    assert [r["id"] for r in rows] == ["auto", "gemini-3-pro-preview", "gemini-2.5-flash"]
    assert gemini_health.normalize_models("nope") == []
    assert gemini_health._models_of({"result": {"models": {"availableModels": []}}}) is None


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
    ours = set(gemini_health._public(gemini_health._measure(allow_shell=False)))
    assert theirs <= ours, theirs - ours
    codexs = set(codex_health._public(codex_health._measure(allow_shell=False)))
    # One extra key of our own: the model the session would open on.
    assert ours == codexs | {"current_model"}


def test_a_missing_cli_snapshot(monkeypatch):
    snap = gemini_health._measure(allow_shell=False)
    assert snap["found"] is False and snap["signed_in"] is None
    assert snap["doctor"] is None and snap["account"] is None and snap["models"] is None
    assert snap["install_command"] == "npm install -g @google/gemini-cli"
    assert snap["install_method"] is None
    assert snap["config_dir"] == gemini_health.config_dir()
    assert snap["platform"] == sys.platform


def test_a_full_measure_against_the_fake_server(fake_acp, monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(fake_acp.path))
    monkeypatch.setenv("FAKE_GEMINI_MODE", "in")
    _settings("oauth-personal")
    with open(os.path.join(_gemini_dir(), "oauth_creds.json"), "w") as fh:
        json.dump({"access_token": "x"}, fh)
    with open(os.path.join(_gemini_dir(), "google_accounts.json"), "w") as fh:
        json.dump({"active": "dev@example.com"}, fh)
    snap = gemini_health.summary_refreshed()
    assert snap["found"] is True and snap["source"] == "path"
    assert snap["version"] == "0.65.0" and snap["outdated"] is False
    assert snap["signed_in"] is True
    assert snap["account"] == {"method": "google", "email": "dev@example.com",
                               "org": None, "plan": None}
    assert [m["id"] for m in snap["models"]] == ["auto", "gemini-3-pro-preview", "gemini-2.5-flash"]
    assert snap["current_model"] == "auto"
    assert "fingerprint" not in snap
    assert os.path.basename(gemini_health._cache_path()) == "gemini-health.json"
    # The catalog's discovered list reads this very cache.
    assert [r["id"] for r in gemini_health.cached_models()] == [
        "auto", "gemini-3-pro-preview", "gemini-2.5-flash"]
    from fused_render.agent_backends import gemini as backend_gemini
    assert backend_gemini.GEMINI.model_ids(discovered=True) == (
        "auto", "gemini-3-pro-preview", "gemini-2.5-flash")
    monkeypatch.setenv("FAKE_GEMINI_MODE", "out")
    snap = gemini_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None and snap["models"] is None


def test_snapshot_is_cached_then_served_from_disk(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(gemini_health, "probe_version",
                        lambda p: calls.append(p) or "0.65.0")
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_IN)
    first = gemini_health.summary()
    assert first["version"] == "0.65.0" and first["signed_in"] is True
    assert os.path.isfile(gemini_health._cache_path())
    second = gemini_health.summary()
    assert second == first and len(calls) == 1
    assert gemini_health.cached()["version"] == "0.65.0"


def test_an_upgraded_binary_a_moved_home_or_a_new_key_invalidates_the_cache(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(gemini_health, "probe_version",
                        lambda p: calls.append(p) or "0.65.0")
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_IN)
    gemini_health.summary()
    os.utime(cli, (time.time() + 100, time.time() + 100))
    gemini_health.summary()
    assert len(calls) == 2
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "elsewhere"))
    assert gemini_health.cached() is None
    gemini_health.summary()
    assert len(calls) == 3
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-SECRET-VALUE")
    assert gemini_health.cached() is None
    gemini_health.summary()
    assert len(calls) == 4
    snap = json.loads(open(gemini_health._cache_path()).read())
    assert "AIza-SECRET-VALUE" not in json.dumps(snap)   # never the key's value


def test_an_unknown_probe_carries_the_last_account_and_models_forward(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    _settings("gemini-api-key")
    monkeypatch.setattr(gemini_health, "probe_version", lambda p: "0.65.0")
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_IN)
    assert gemini_health.summary()["account"]["method"] == "api_key"
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: None)
    snap = gemini_health.summary_refreshed()
    assert snap["signed_in"] is None
    assert snap["account"]["method"] == "api_key"
    assert [m["id"] for m in snap["models"]][:1] == ["auto"]
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "elsewhere"))
    assert gemini_health.summary_refreshed()["account"] is None


def test_a_definite_sign_out_does_not_carry_the_account(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(gemini_health, "probe_version", lambda p: "0.65.0")
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_IN)
    gemini_health.summary()
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: _SIGNED_OUT)
    snap = gemini_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None and snap["models"] is None


def test_refresh_re_probes_and_a_corrupt_cache_is_tolerated(monkeypatch):
    monkeypatch.setattr(gemini_health, "_acp_probe", lambda p: None)
    os.makedirs(os.path.dirname(gemini_health._cache_path()), exist_ok=True)
    with open(gemini_health._cache_path(), "w") as f:
        f.write("{not json")
    assert gemini_health.summary()["found"] is False
    assert gemini_health.summary_refreshed()["found"] is False
    assert gemini_health.cached() is not None


# -- install method and the update offer ---------------------------------------


_NPM = "npm install -g @google/gemini-cli"


@pytest.mark.parametrize("path,method,updatable,command", [
    ("/Users/x/.npm-global/bin/gemini", "npm", True, _NPM),
    ("/home/x/.nvm/versions/node/v22.1.0/bin/gemini", "npm", True, _NPM),
    ("/opt/homebrew/lib/node_modules/@google/gemini-cli/dist/index.js", "npm", True, _NPM),
    ("/opt/homebrew/bin/gemini", "brew", False, "brew upgrade gemini-cli"),
    ("/srv/tools/gemini", "other", None, _NPM),
    (None, None, None, _NPM),
])
def test_install_method_and_update_plan(path, method, updatable, command):
    assert gemini_health.install_method(path) == method
    plan = gemini_health.update_plan(method)
    assert plan["updatable"] is updatable
    assert plan["command"] == command


def test_the_install_command_is_npm():
    assert gemini_health.install_command() == gemini_health.INSTALL_COMMAND_POSIX == _NPM


# -- login ---------------------------------------------------------------------


def _login_cli(tmp_path, monkeypatch, mode):
    p = tmp_path / "login-bin" / "gemini"
    p.parent.mkdir(exist_ok=True)
    p.write_text(_FAKE_ACP.format(python=sys.executable,
                                  signed_in=json.dumps(_SIGNED_IN["result"])))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_GEMINI_LOG", str(tmp_path / "login.log"))
    monkeypatch.setenv("FAKE_GEMINI_MODE", mode)
    monkeypatch.setenv("PATH", str(p.parent))
    return p


def test_login_refuses_without_a_cli():
    from fused_render import gemini_login
    with pytest.raises(gemini_login.LoginError):
        gemini_login.start()


def test_login_authenticates_over_acp_with_a_browser_and_re_probes(tmp_path, monkeypatch):
    """`initialize` → `authenticate {methodId: oauth-personal}`; a result is
    a sign-in (re-probed, never inferred); the authorize URL and its state
    stay in memory; NO_BROWSER is NOT set for this child."""
    from fused_render import gemini_login
    monkeypatch.setenv("NO_BROWSER", "1")   # the health probe's, not ours
    _login_cli(tmp_path, monkeypatch, "in")
    probes = []
    monkeypatch.setattr(gemini_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    gemini_login.reset()
    rec = gemini_login.start()
    assert rec["in_flight"] is True and rec["detail"] is None
    login = gemini_login._active
    assert login.settled.wait(10)
    st = gemini_login.status()
    assert st["in_flight"] is False and st["error"] is None and st["detail"] is None
    assert probes == [1]
    log = json.loads((tmp_path / "login.log").read_text())
    assert [m["method"] for m in log["got"]] == ["initialize", "authenticate"]
    assert log["got"][1]["params"] == {"methodId": "oauth-personal"}
    assert log["env"]["NO_BROWSER"] is None
    assert "SECRET" not in json.dumps(st)
    assert gemini_login.cancel()["canceled"] is False
    gemini_login.reset()


def test_a_refused_authenticate_reports_the_clis_sentence_and_does_not_re_probe(
        tmp_path, monkeypatch):
    from fused_render import gemini_login
    _login_cli(tmp_path, monkeypatch, "auth-fail")
    probes = []
    monkeypatch.setattr(gemini_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    gemini_login.reset()
    gemini_login.start()
    assert gemini_login._active.settled.wait(10)
    assert gemini_login.status()["error"] == "Authentication cancelled by user."
    assert probes == []
    gemini_login.reset()


def test_a_second_login_is_refused_while_one_waits_and_cancel_stops_it(tmp_path, monkeypatch):
    from fused_render import gemini_login
    _login_cli(tmp_path, monkeypatch, "auth-hang")
    gemini_login.reset()
    gemini_login.start()
    with pytest.raises(gemini_login.LoginError, match="already waiting"):
        gemini_login.start()
    out = gemini_login.cancel()
    assert out["canceled"] is True and out["in_flight"] is False and out["error"] is None
    assert gemini_login._active.proc.poll() is not None
    gemini_login.reset()


def test_login_failure_names_the_childs_last_real_line():
    from fused_render import gemini_login
    login = gemini_login._Login(proc=None, started_at=0,
                                tail=collections.deque(maxlen=40))
    for line in ("Attempting to open authentication page in your browser.",
                 "https://accounts.google.com/o/oauth2/v2/auth?state=SECRET",
                 "Waiting for authentication...",
                 "Authentication timed out after 5 minutes."):
        gemini_login._note_line(login, line)
    assert gemini_login._failure(login, 1) == "Authentication timed out after 5 minutes."
    login = gemini_login._Login(proc=None, started_at=0, tail=collections.deque(maxlen=40))
    assert gemini_login._failure(login, 1) == \
        "the sign-in ended without signing in (exit code 1)"


# -- install -------------------------------------------------------------------


def _fake_npm(tmp_path, monkeypatch):
    npm = tmp_path / "npm-bin" / "npm"
    npm.parent.mkdir()
    npm.write_text("#!/bin/sh\necho npm \"$@\"\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(npm.parent))
    return str(npm)


def test_install_has_its_own_slot_and_argv(tmp_path, monkeypatch):
    from fused_render import claude_install, codex_install, copilot_install, gemini_install
    assert gemini_install.JOB_ID == "sys:gemini-install"
    assert gemini_install.JOB_ID not in (claude_install.JOB_ID, copilot_install.JOB_ID,
                                         codex_install.JOB_ID)
    npm = _fake_npm(tmp_path, monkeypatch)
    argv, display = gemini_install.install_argv()
    assert argv == [npm, "install", "-g", "@google/gemini-cli"]
    assert display == gemini_health.INSTALL_COMMAND_POSIX
    assert gemini_install.update_argv("/x/gemini") == (argv, display)


def _wait_install():
    from fused_render import gemini_install
    for _ in range(100):
        if gemini_install.status()["state"] != "running":
            break
        time.sleep(0.05)
    return gemini_install.status()


def test_install_runs_npm_to_done_and_re_probes(tmp_path, monkeypatch):
    from fused_render import gemini_install
    gemini_install.reset()
    _fake_npm(tmp_path, monkeypatch)
    monkeypatch.setattr(gemini_health, "summary_refreshed",
                        lambda: {"found": True, "version": "0.65.0"})
    monkeypatch.setattr(gemini_install, "_report", lambda s: None)
    rec = gemini_install.start("install")
    assert rec["state"] == "running" and rec["command"] == _NPM
    st = _wait_install()
    assert st["state"] == "done", st
    assert st["detail"] == "Gemini CLI 0.65.0"
    gemini_install.reset()


# -- the router ----------------------------------------------------------------


def _gemini_switch(monkeypatch, on):
    from fused_render.agent_backends import registry
    real = registry.is_enabled
    monkeypatch.setattr(registry, "is_enabled",
                        lambda b: on if b == "gemini" else real(b))


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.gemini_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_MUTATING = ["/api/gemini/health/refresh", "/api/gemini/install",
             "/api/gemini/login", "/api/gemini/login/cancel",
             "/api/gemini/link-path"]


def test_endpoint_answers_the_snapshot_and_posts_need_the_header(monkeypatch):
    _gemini_switch(monkeypatch, True)
    monkeypatch.setattr(gemini_health, "summary",
                        lambda: {"found": True, "version": "0.65.0"})
    monkeypatch.setattr(gemini_health, "summary_refreshed", lambda: {"found": False})
    c = _client()
    assert c.get("/api/gemini/health").json() == {"found": True, "version": "0.65.0"}
    for route in _MUTATING:
        assert c.post(route).status_code == 403, route


def test_the_default_switch_is_off_and_gates_every_spawn(monkeypatch):
    """D17: Gemini is off until the user turns it on; no route may spawn it."""
    from fused_render import gemini_install, gemini_login
    from fused_render.agent_backends import registry
    assert registry.is_enabled("gemini") is False

    def boom(*a, **k):
        raise AssertionError("a disabled Gemini was probed or spawned")
    for mod, name in ((gemini_health, "summary"), (gemini_health, "summary_refreshed"),
                      (gemini_health, "_measure"), (gemini_health, "add_to_shell_path"),
                      (gemini_install, "start"), (gemini_login, "start")):
        monkeypatch.setattr(mod, name, boom)
    c = _client()
    assert c.get("/api/gemini/health").json() is None
    for route in _MUTATING:
        if route == "/api/gemini/login/cancel":
            continue
        r = c.post(route, headers={"X-Fused": "1"})
        assert r.status_code == 400, route
        assert r.json() == registry.disabled_error("gemini")
    # Cancel is never gated: it only signals our own child.
    assert c.post("/api/gemini/login/cancel", headers={"X-Fused": "1"}).json()["canceled"] is False


def test_the_server_app_mounts_the_gemini_routes():
    from fused_render.server import app as server_app
    from fused_render.server.routers.gemini_health import router
    assert server_app.gemini_health_router is router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert paths == {"/api/gemini/health", "/api/gemini/health/refresh",
                     "/api/gemini/install", "/api/gemini/link-path",
                     "/api/gemini/login", "/api/gemini/login/cancel"}
