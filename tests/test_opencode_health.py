"""Tests for fused_render/opencode_health.py, opencode_login.py,
opencode_install.py and the /api/opencode/* router.

Same discipline as test_gemini_health.py: no test runs a real `opencode`.
Resolution is driven through a fake tree plus a patched PATH; the probes
that must actually be exercised end to end — the ACP sign-in handshake and
the `auth login` child — run against tests/fake_opencode_health_cli.py, a stdlib
`opencode` that answers the way 1.19.5 was observed to
(docs/OPENCODE_BACKEND.md D10).
"""
import collections
import json
import ntpath
import os
import stat
import shutil
import sys
import time

import pytest

from fused_render import claude_health, codex_health, copilot_health, opencode_health

_REAL_CANDIDATES = opencode_health.candidates
_REAL_LOCAL_MODELS = opencode_health._local_models
_FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "fake_opencode_health_cli.py")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own shell home (own cache file), a PATH that inherits nothing, no
    override, an own OPENCODE_CONFIG_DIR, no local models — the suite
    runner's machine may well have a real, signed-in OpenCode."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv(opencode_health.BIN_ENV, raising=False)
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "opencode-user"))
    for name in ("OPENCODE_CONFIG_CONTENT", "OPENCODE_DISABLE_AUTOUPDATE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(opencode_health, "_shell_probe", lambda: None)
    monkeypatch.setattr(opencode_health, "_local_models", lambda: [])
    monkeypatch.setattr(claude_health, "candidates", lambda: ())
    monkeypatch.setattr(opencode_health, "candidates", lambda: ())
    monkeypatch.setattr(opencode_health, "_ADOPTED", None)
    from fused_render import opencode_login
    opencode_login.reset()


def _fake_cli(tmp_path, name="opencode", executable=True):
    """A findable stand-in; never spawned by the resolve tests."""
    d = tmp_path / "fake-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\necho '1.19.5'\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


def _fake_binary(where):
    """A runnable `opencode` that is tests/fake_opencode_acp.py behind a
    shebang naming this interpreter: the isolation fixture's PATH has no
    python on it."""
    where.parent.mkdir(exist_ok=True)
    where.write_text(f"#!{sys.executable}\nimport runpy\n"
                     f"runpy.run_path({_FAKE!r}, run_name='__main__')\n")
    where.chmod(where.stat().st_mode | stat.S_IXUSR)
    return str(where)


# -- version -------------------------------------------------------------------


@pytest.mark.parametrize("stdout,stderr,want", [
    ("1.19.5\n", "", "1.19.5"),
    ("1.19.5", "npm WARN /tmp/claude-0/x\n", "1.19.5"),
    ("", "1.20.1\n", "1.20.1"),
    ("garbage\n", "", None),
])
def test_probe_version_reads_the_bare_number(monkeypatch, stdout, stderr, want):
    class R:
        returncode = 0
    R.stdout, R.stderr = stdout, stderr
    monkeypatch.setattr(opencode_health, "_run_probe", lambda *a, **k: R())
    assert opencode_health.probe_version("/x/opencode") == want


def test_the_declared_floor_is_the_verified_minor():
    assert opencode_health.MIN_VERSION == "1.19.0"
    assert opencode_health.is_outdated("1.19.0") is False
    assert opencode_health.is_outdated("1.19.5") is False
    assert opencode_health.is_outdated("1.18.9") is True
    assert opencode_health.is_outdated(None) is False
    assert opencode_health.is_outdated("1.19.5", "2.0.0") is True   # not Claude's floor by default


# -- resolve order -------------------------------------------------------------


def test_the_candidate_list_is_the_contract_s_six_in_order():
    assert opencode_health.BIN_ENV == "FUSED_RENDER_OPENCODE_BIN"
    assert opencode_health.POSIX_CANDIDATES == (
        "~/.opencode/bin/opencode",
        "~/.npm-global/bin/opencode",
        "/opt/homebrew/bin/opencode",
        "/usr/local/bin/opencode",
        "~/.local/bin/opencode",
        "~/.bun/bin/opencode",
    )


def test_override_wins_and_is_reported_as_such(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(opencode_health.BIN_ENV, "/nowhere/opencode")
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    assert opencode_health.resolve() == ("/nowhere/opencode", "override")


def test_path_beats_the_candidate_list(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(opencode_health, "candidates", lambda: ("/nowhere/opencode",))
    assert opencode_health.resolve() == (cli, "path")


def test_candidate_dirs_are_probed_when_path_is_stripped(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(opencode_health, "candidates", lambda: (cli,))
    assert opencode_health.resolve() == (cli, "candidate")


def test_a_non_executable_file_does_not_shadow_a_real_install(tmp_path, monkeypatch):
    dud = _fake_cli(tmp_path, executable=False)
    monkeypatch.setattr(opencode_health, "candidates", lambda: (dud,))
    assert opencode_health.resolve(allow_shell=False) == (None, None)


def test_a_shell_only_install_is_adopted_as_the_override(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(opencode_health, "_shell_probe", lambda: cli)
    monkeypatch.setattr(opencode_health, "probe_version", lambda p: "1.19.5")
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_IN)
    snap = opencode_health._measure()
    assert (snap["path"], snap["source"]) == (cli, "shell")
    assert os.environ[opencode_health.BIN_ENV] == cli
    assert opencode_health.resolve() == (cli, "shell")


def test_a_user_override_is_never_replaced_by_an_adoption(monkeypatch):
    monkeypatch.setenv(opencode_health.BIN_ENV, "/mine/opencode")
    assert opencode_health.adopt("/found/opencode") is False
    assert os.environ[opencode_health.BIN_ENV] == "/mine/opencode"


def test_config_dir_is_opencode_config_dir_else_dot_config(tmp_path, monkeypatch):
    assert opencode_health.config_dir() == str(tmp_path / "opencode-user")
    monkeypatch.delenv("OPENCODE_CONFIG_DIR")
    assert opencode_health.config_dir() == os.path.expanduser("~/.config/opencode")


# -- the ACP sign-in probe -----------------------------------------------------

_MODEL_ROWS = [
    {"value": "anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5"},
    {"value": "fused/qwen3-8b", "name": "Qwen3 8B (local)"},
    {"value": "openai/gpt-5.5", "name": "GPT-5.5"},
    {"value": "fused/qwen3-8b", "name": "dup"},
    {"value": "fused/gemma-4-12b", "name": "Gemma 4 12B (local)"},
    {"name": "no value"}, "junk",
    {"value": "google/gemini-3-pro", "name": "Gemini 3 Pro"},
]
_SIGNED_IN = {"id": 2, "result": {
    "sessionId": "ses_7f3a2b1c9d",
    "configOptions": [
        {"id": "model", "type": "select", "currentValue": "anthropic/claude-sonnet-5-5",
         "options": _MODEL_ROWS},
        {"id": "mode", "type": "select", "currentValue": "build",
         "options": [{"value": "build", "name": "Build"}, {"value": "plan", "name": "Plan"}]},
    ]}}
_SIGNED_OUT = {"id": 2, "error": {"code": -32000, "message": "no provider configured"}}
_LOCAL_IDS = ["fused/qwen3-8b", "fused/gemma-4-12b"]


@pytest.fixture
def fake_acp(tmp_path, monkeypatch):
    """An `opencode` that speaks ACP."""
    path = _fake_binary(tmp_path / "acp-bin" / "opencode")
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_OPENCODE_LOG", str(log))

    def run(mode):
        monkeypatch.setenv("FAKE_OPENCODE_MODE", mode)
        return opencode_health._acp_probe(path)

    run.log = lambda: json.loads(log.read_text())
    run.path = path
    return run


def test_a_session_result_means_signed_in_and_carries_the_local_models(fake_acp):
    reply = fake_acp("in")
    assert opencode_health._classify(reply) is True
    # D11a: only the `fused/` rows; the other providers are counted, not kept.
    assert opencode_health._models_of(reply) == [
        {"id": "fused/qwen3-8b", "label": "Qwen3 8B (local)", "description": None},
        {"id": "fused/gemma-4-12b", "label": "Gemma 4 12B (local)", "description": None}]
    assert opencode_health._providers_seen_of(reply) == 4
    assert opencode_health._current_model_of(reply) == "anthropic/claude-sonnet-5-5"
    assert opencode_health._current_model_of(fake_acp("in-local")) == "fused/qwen3-8b"


@pytest.mark.parametrize("mode,want", [("out", False), ("other", None), ("internal", None),
                                       ("garbage", None), ("noise", True)])
def test_the_reply_classifies_as_the_contract_says(fake_acp, mode, want):
    assert opencode_health._classify(fake_acp(mode)) is want


def test_the_handshake_is_initialize_then_session_new_on_the_probe_dir(fake_acp):
    fake_acp("in")
    log = fake_acp.log()
    got = log["got"]
    assert log["argv"] == ["acp"]
    assert [m["method"] for m in got] == ["initialize", "session/new"]
    assert got[0]["params"]["protocolVersion"] == 1
    assert got[0]["params"]["clientCapabilities"] == {}
    assert got[1]["params"]["mcpServers"] == []
    probe_dir = got[1]["params"]["cwd"]
    assert probe_dir == opencode_health._probe_dir() and os.path.isdir(probe_dir)
    assert probe_dir.startswith(os.environ["FUSED_RENDER_HOME"])
    assert probe_dir.endswith("opencode-probe")
    assert log["cwd"] != probe_dir   # no chdir


def test_the_child_never_autoupdates_and_injects_no_config_without_local_models(
        fake_acp, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/leaks")
    fake_acp("in")
    env = fake_acp.log()["env"]
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"           # D19
    assert env["OPENCODE_CONFIG_CONTENT"] is None               # no local models
    assert env["OPENCODE_CONFIG_DIR"] == os.environ["OPENCODE_CONFIG_DIR"]
    assert env["PYTHONPATH"] is None


def test_the_probe_injects_the_fused_provider_when_local_models_exist(fake_acp, monkeypatch):
    """D20: with a local model installed the probe carries the `fused`
    provider (dead baseURL, no models), so "signed in" = a usable provider
    even with no credential at all."""
    monkeypatch.setattr(opencode_health, "_local_models", lambda: [{"id": "qwen3-8b"}])
    config = opencode_health.probe_config()
    assert config == {"provider": {"fused": {
        "npm": "@ai-sdk/openai-compatible", "name": "Fused Render (local)",
        "options": {"baseURL": "http://127.0.0.1:1/v1", "apiKey": "probe"},
        "models": {}}}}
    fake_acp("in")
    assert json.loads(fake_acp.log()["env"]["OPENCODE_CONFIG_CONTENT"]) == config


def test_a_missing_or_broken_opencode_config_module_reads_as_no_local_models(monkeypatch):
    """The catalog module is another workstream's: absent, half-written or
    raising, it reads as "no local models", never as a failed probe."""
    import types
    from fused_render import agent_backends
    monkeypatch.setattr(opencode_health, "_local_models", _REAL_LOCAL_MODELS)
    name = "fused_render.agent_backends.opencode_config"
    monkeypatch.delattr(agent_backends, "opencode_config", raising=False)
    monkeypatch.setitem(sys.modules, name, None)          # ImportError
    assert opencode_health._local_models() == []
    assert opencode_health.probe_config() is None

    def boom():
        raise RuntimeError("catalog not ready")
    monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(local_models=boom))
    assert opencode_health._local_models() == []
    monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(local_models=lambda: []))
    assert opencode_health._local_models() == []
    monkeypatch.setitem(sys.modules, name,
                        types.SimpleNamespace(local_models=lambda: [{"id": "qwen3-8b"}]))
    assert opencode_health._local_models() == [{"id": "qwen3-8b"}]
    assert opencode_health.probe_config() is not None


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
    monkeypatch.setattr(opencode_health, "_ACP_TIMEOUT_S", 0.5)
    monkeypatch.setattr(opencode_health, "_REAP_GRACE_S", 0.3)
    assert fake_acp(mode) is None
    _assert_reaped(fake_acp.log()["pid"])


@pytest.mark.parametrize("reply,want", [
    ({"result": {"sessionId": "ses_abc"}}, True),
    ({"result": {}}, None),
    ({"error": {"code": -32000, "message": "no provider configured"}}, False),
    ({"error": {"code": -32000, "message": "Provider not found: anthropic"}}, False),
    ({"error": {"code": -32000, "message": "Unauthorized"}}, False),
    ({"error": {"code": -32000, "message": "config file is not valid JSON"}}, None),
    ({"error": {"code": -32603, "message": "no provider configured"}}, None),
    ({"error": "nope"}, None),
    (None, None),
])
def test_classify_table(reply, want):
    assert opencode_health._classify(reply) is want


def test_signed_in_without_a_cli_is_unknown(monkeypatch):
    assert opencode_health.signed_in(None) is None
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_OUT)
    assert opencode_health.signed_in("/x/opencode") is False
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_IN)
    assert opencode_health.signed_in("/x/opencode") is True


def test_account_names_local_or_providers_by_the_current_model():
    assert opencode_health.account(True, "fused/qwen3-8b") == {
        "method": "local", "email": None, "org": None, "plan": None}
    assert opencode_health.account(True, "anthropic/claude-sonnet-5-5")["method"] == "providers"
    assert opencode_health.account(True, None)["method"] == "providers"
    assert opencode_health.account(False, "fused/x") is None
    assert opencode_health.account(None, "fused/x") is None


def test_normalize_models_keeps_fused_rows_and_drops_junk_and_duplicates():
    rows = opencode_health.normalize_models(_MODEL_ROWS)
    assert [r["id"] for r in rows] == _LOCAL_IDS
    everything = opencode_health.normalize_models(_MODEL_ROWS, keep=lambda m: True)
    assert [r["id"] for r in everything] == [
        "anthropic/claude-sonnet-5-5", "fused/qwen3-8b", "openai/gpt-5.5",
        "fused/gemma-4-12b", "google/gemini-3-pro"]
    assert opencode_health.normalize_models("nope") == []
    assert opencode_health._models_of({"result": {"configOptions": [
        {"id": "model", "options": [{"value": "openai/gpt-5.5"}]}]}}) is None
    assert opencode_health._models_of({"result": {"configOptions": []}}) is None
    assert opencode_health._providers_seen_of({"result": {}}) is None


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
    ours = set(opencode_health._public(opencode_health._measure(allow_shell=False)))
    assert theirs <= ours, theirs - ours
    codexs = set(codex_health._public(codex_health._measure(allow_shell=False)))
    # Gemini's extra key, plus the provider count for the UI.
    assert ours == codexs | {"current_model", "providers_seen"}


def test_a_missing_cli_snapshot(monkeypatch):
    snap = opencode_health._measure(allow_shell=False)
    assert snap["found"] is False and snap["signed_in"] is None
    assert snap["doctor"] is None and snap["account"] is None and snap["models"] is None
    assert snap["current_model"] is None and snap["providers_seen"] is None
    assert snap["install_command"] == "npm install -g opencode-ai"
    assert snap["install_method"] is None
    assert snap["config_dir"] == opencode_health.config_dir()
    assert snap["platform"] == sys.platform


def test_a_full_measure_against_the_fake_server(fake_acp, monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(fake_acp.path))
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "in-local")
    snap = opencode_health.summary_refreshed()
    assert snap["found"] is True and snap["source"] == "path"
    assert snap["version"] == "1.19.5" and snap["outdated"] is False
    assert snap["signed_in"] is True
    assert snap["account"] == {"method": "local", "email": None, "org": None, "plan": None}
    assert [m["id"] for m in snap["models"]] == _LOCAL_IDS
    assert snap["current_model"] == "fused/qwen3-8b"
    assert snap["providers_seen"] == 4
    assert "fingerprint" not in snap
    assert os.path.basename(opencode_health._cache_path()) == "opencode-health.json"
    assert [r["id"] for r in opencode_health.cached_models()] == _LOCAL_IDS
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "in")
    snap = opencode_health.summary_refreshed()
    assert snap["account"]["method"] == "providers"
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "out")
    snap = opencode_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None and snap["models"] is None


def test_snapshot_is_cached_then_served_from_disk(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(opencode_health, "probe_version",
                        lambda p: calls.append(p) or "1.19.5")
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_IN)
    first = opencode_health.summary()
    assert first["version"] == "1.19.5" and first["signed_in"] is True
    assert os.path.isfile(opencode_health._cache_path())
    second = opencode_health.summary()
    assert second == first and len(calls) == 1
    assert opencode_health.cached()["version"] == "1.19.5"


def test_an_upgraded_binary_or_a_moved_config_dir_invalidates_the_cache(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(opencode_health, "probe_version",
                        lambda p: calls.append(p) or "1.19.5")
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_IN)
    opencode_health.summary()
    os.utime(cli, (time.time() + 100, time.time() + 100))
    opencode_health.summary()
    assert len(calls) == 2
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert opencode_health.cached() is None
    opencode_health.summary()
    assert len(calls) == 3


def test_an_unknown_probe_carries_the_last_account_and_models_forward(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(opencode_health, "probe_version", lambda p: "1.19.5")
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_IN)
    assert opencode_health.summary()["account"]["method"] == "providers"
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: None)
    snap = opencode_health.summary_refreshed()
    assert snap["signed_in"] is None
    assert snap["account"]["method"] == "providers"
    assert [m["id"] for m in snap["models"]] == _LOCAL_IDS
    assert snap["current_model"] == "anthropic/claude-sonnet-5-5"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    assert opencode_health.summary_refreshed()["account"] is None


def test_a_definite_sign_out_does_not_carry_the_account(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(opencode_health, "probe_version", lambda p: "1.19.5")
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_IN)
    opencode_health.summary()
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: _SIGNED_OUT)
    snap = opencode_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None and snap["models"] is None


def test_refresh_re_probes_and_a_corrupt_cache_is_tolerated(monkeypatch):
    monkeypatch.setattr(opencode_health, "_acp_probe", lambda p: None)
    os.makedirs(os.path.dirname(opencode_health._cache_path()), exist_ok=True)
    with open(opencode_health._cache_path(), "w") as f:
        f.write("{not json")
    assert opencode_health.summary()["found"] is False
    assert opencode_health.summary_refreshed()["found"] is False
    assert opencode_health.cached() is not None


# -- install method and the update offer ---------------------------------------


_NPM = "npm install -g opencode-ai"


@pytest.mark.parametrize("path,method,updatable,command", [
    ("/Users/x/.npm-global/bin/opencode", "npm", True, _NPM),
    ("/home/x/.nvm/versions/node/v22.1.0/bin/opencode", "npm", True, _NPM),
    ("/opt/homebrew/lib/node_modules/opencode-ai/bin/opencode", "npm", True, _NPM),
    ("/opt/homebrew/bin/opencode", "brew", False, "brew upgrade opencode"),
    ("/home/x/.opencode/bin/opencode", "curl", False,
     "curl -fsSL https://opencode.ai/install | bash"),
    ("/srv/tools/opencode", "other", None, _NPM),
    (None, None, None, _NPM),
])
def test_install_method_and_update_plan(path, method, updatable, command):
    assert opencode_health.install_method(path) == method
    plan = opencode_health.update_plan(method)
    assert plan["updatable"] is updatable
    assert plan["command"] == command


def test_the_install_command_is_npm():
    assert opencode_health.install_command() == opencode_health.INSTALL_COMMAND_POSIX == _NPM


# -- login ---------------------------------------------------------------------


def _login_cli(tmp_path, monkeypatch, mode):
    path = _fake_binary(tmp_path / "login-bin" / "opencode")
    monkeypatch.setenv("FAKE_OPENCODE_LOG", str(tmp_path / "login.log"))
    monkeypatch.setenv("FAKE_OPENCODE_MODE", mode)
    monkeypatch.setenv("PATH", os.path.dirname(path))
    return path


def test_login_refuses_without_a_cli():
    from fused_render import opencode_login
    with pytest.raises(opencode_login.LoginError):
        opencode_login.start()


def test_login_runs_auth_login_on_a_tty_and_re_probes(tmp_path, monkeypatch):
    """`opencode auth login` under a pty; a clean exit is re-probed, never
    inferred; the record tells the strip to finish in the terminal."""
    from fused_render import opencode_login
    _login_cli(tmp_path, monkeypatch, "login-ok")
    probes = []
    monkeypatch.setattr(opencode_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    opencode_login.reset()
    rec = opencode_login.start()
    assert rec["in_flight"] is True
    assert rec["detail"] == opencode_login.DETAIL
    assert "terminal" in rec["detail"]["message"]
    login = opencode_login._active
    assert login.settled.wait(10)
    st = opencode_login.status()
    assert st["in_flight"] is False and st["error"] is None
    assert set(st) == {"in_flight", "started_at", "error", "detail"}
    assert probes == [1]
    log = json.loads((tmp_path / "login.log").read_text())
    assert log["argv"] == ["auth", "login"]
    assert log["tty"] is True
    assert log["env"]["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    assert opencode_login.cancel()["canceled"] is False
    opencode_login.reset()


def test_a_failed_login_reports_the_last_real_line_and_does_not_re_probe(
        tmp_path, monkeypatch):
    from fused_render import opencode_login
    _login_cli(tmp_path, monkeypatch, "login-fail")
    probes = []
    monkeypatch.setattr(opencode_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    opencode_login.reset()
    opencode_login.start()
    assert opencode_login._active.settled.wait(10)
    st = opencode_login.status()
    assert st["error"] == "Error: provider refused the key"
    assert "SECRET" not in json.dumps(st)
    assert probes == []
    opencode_login.reset()


def test_a_second_login_is_refused_while_one_waits_and_cancel_stops_it(tmp_path, monkeypatch):
    from fused_render import opencode_login
    _login_cli(tmp_path, monkeypatch, "login-hang")
    opencode_login.reset()
    opencode_login.start()
    with pytest.raises(opencode_login.LoginError, match="already waiting"):
        opencode_login.start()
    out = opencode_login.cancel()
    assert out["canceled"] is True and out["in_flight"] is False and out["error"] is None
    assert opencode_login._active.proc.poll() is not None
    assert opencode_login._active.master_fd is None
    opencode_login.reset()


def test_login_failure_names_the_childs_last_real_line():
    from fused_render import opencode_login
    login = opencode_login._Login(proc=None, started_at=0,
                                  tail=collections.deque(maxlen=40))
    for line in ("│  Select provider",
                 "Enter your API key: sk-SECRET",
                 "https://console.example.com/keys?state=SECRET",
                 "Error: provider refused the key"):
        opencode_login._note_line(login, line)
    assert opencode_login._failure(login, 1) == "Error: provider refused the key"
    login = opencode_login._Login(proc=None, started_at=0, tail=collections.deque(maxlen=40))
    opencode_login._note_line(login, "Enter your API key: sk-SECRET")
    assert opencode_login._failure(login, 1) == \
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
    from fused_render import (claude_install, codex_install, copilot_install,
                              gemini_install, opencode_install)
    assert opencode_install.JOB_ID == "sys:opencode-install"
    assert opencode_install.JOB_ID not in (claude_install.JOB_ID, copilot_install.JOB_ID,
                                           codex_install.JOB_ID, gemini_install.JOB_ID)
    npm = _fake_npm(tmp_path, monkeypatch)
    argv, display = opencode_install.install_argv()
    assert argv == [npm, "install", "-g", "opencode-ai"]
    assert display == opencode_health.INSTALL_COMMAND_POSIX
    assert opencode_install.update_argv("/x/opencode") == (argv, display)


def test_install_refuses_without_npm():
    from fused_render import opencode_install
    with pytest.raises(opencode_install.InstallError, match="npm"):
        opencode_install.install_argv()


def _wait_install():
    from fused_render import opencode_install
    for _ in range(100):
        if opencode_install.status()["state"] != "running":
            break
        time.sleep(0.05)
    return opencode_install.status()


def test_install_runs_npm_to_done_and_re_probes(tmp_path, monkeypatch):
    from fused_render import opencode_install
    opencode_install.reset()
    _fake_npm(tmp_path, monkeypatch)
    monkeypatch.setattr(opencode_health, "summary_refreshed",
                        lambda: {"found": True, "version": "1.19.5"})
    monkeypatch.setattr(opencode_install, "_report", lambda s: None)
    rec = opencode_install.start("install")
    assert rec["state"] == "running" and rec["command"] == _NPM
    st = _wait_install()
    assert st["state"] == "done", st
    assert st["detail"] == "OpenCode 1.19.5"
    opencode_install.reset()


def test_an_update_of_a_curl_or_brew_install_is_refused_with_the_managers_line(
        tmp_path, monkeypatch):
    from fused_render import opencode_install
    opencode_install.reset()
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(opencode_health, "install_method", lambda p: "curl")
    with pytest.raises(opencode_install.InstallError, match="opencode.ai/install"):
        opencode_install.start("update")
    assert opencode_install.status()["state"] == "idle"


# -- the router ----------------------------------------------------------------


def _opencode_switch(monkeypatch, on):
    from fused_render.agent_backends import registry
    real = registry.is_enabled
    monkeypatch.setattr(registry, "is_enabled",
                        lambda b: on if b == "opencode" else real(b))


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.opencode_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


_MUTATING = ["/api/opencode/health/refresh", "/api/opencode/install",
             "/api/opencode/login", "/api/opencode/login/cancel",
             "/api/opencode/link-path"]


def test_endpoint_answers_the_snapshot_and_posts_need_the_header(monkeypatch):
    _opencode_switch(monkeypatch, True)
    monkeypatch.setattr(opencode_health, "summary",
                        lambda: {"found": True, "version": "1.19.5"})
    monkeypatch.setattr(opencode_health, "summary_refreshed", lambda: {"found": False})
    c = _client()
    assert c.get("/api/opencode/health").json() == {"found": True, "version": "1.19.5"}
    for route in _MUTATING:
        assert c.post(route).status_code == 403, route
    assert c.post("/api/opencode/health/refresh", headers={"X-Fused": "1"}).json() == {
        "found": False}
    assert c.get("/api/opencode/install").json()["state"] == "idle"
    assert c.get("/api/opencode/login").json()["in_flight"] is False


def test_the_default_switch_is_off_and_gates_every_spawn(monkeypatch):
    """D17: OpenCode is off until the user turns it on; no route may spawn it."""
    from fused_render import opencode_install, opencode_login
    from fused_render.agent_backends import registry
    assert registry.is_enabled("opencode") is False

    def boom(*a, **k):
        raise AssertionError("a disabled OpenCode was probed or spawned")
    for mod, name in ((opencode_health, "summary"), (opencode_health, "summary_refreshed"),
                      (opencode_health, "_measure"), (opencode_health, "add_to_shell_path"),
                      (opencode_install, "start"), (opencode_login, "start")):
        monkeypatch.setattr(mod, name, boom)
    c = _client()
    assert c.get("/api/opencode/health").json() is None
    for route in _MUTATING:
        if route == "/api/opencode/login/cancel":
            continue
        r = c.post(route, headers={"X-Fused": "1"})
        assert r.status_code == 400, route
        assert r.json() == registry.disabled_error("opencode")
    # Cancel is never gated: it only signals our own child.
    assert c.post("/api/opencode/login/cancel",
                  headers={"X-Fused": "1"}).json()["canceled"] is False


def test_the_server_app_mounts_the_opencode_routes():
    from fused_render.server import app as server_app
    from fused_render.server.routers.opencode_health import router
    assert server_app.opencode_health_router is router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert paths == {"/api/opencode/health", "/api/opencode/health/refresh",
                     "/api/opencode/install", "/api/opencode/link-path",
                     "/api/opencode/login", "/api/opencode/login/cancel"}


# -- Windows discovery -----------------------------------------------------------


def test_opencode_windows_candidate_found_with_stale_path(tmp_path, monkeypatch):
    npm = tmp_path / "appdata" / "npm"
    npm.mkdir(parents=True)
    shim = npm / "opencode.cmd"
    shim.write_text("@echo off\n")
    shim.chmod(0o755)  # os.access(X_OK) on macOS
    (tmp_path / "unrelated").mkdir()
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(sys, "platform", "win32")
    # shutil.which on win32 reaches for _winapi, which a POSIX host lacks.
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    # POSIX host: expand %VAR% like ntpath, and spell the separators as "/".
    monkeypatch.setattr(os.path, "expandvars", ntpath.expandvars)
    monkeypatch.setattr(opencode_health, "WINDOWS_CANDIDATES", tuple(
        c.replace("\\", "/") for c in opencode_health.WINDOWS_CANDIDATES))
    monkeypatch.setattr(opencode_health, "candidates", _REAL_CANDIDATES)
    monkeypatch.setenv("PATH", str(tmp_path / "unrelated"))
    assert opencode_health.resolve(allow_shell=False) == (str(shim), "candidate")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert _REAL_CANDIDATES() is opencode_health.POSIX_CANDIDATES
