"""Tests for fused_render/codex_health.py, codex_login.py, codex_install.py and
the /api/codex/* router.

Same discipline as test_copilot_health.py: no test runs a real `codex`.
Resolution is driven through a fake tree plus a patched PATH; the one probe
that must actually be exercised end to end — the app-server sign-in handshake
— runs against a fake `codex` written in Python that speaks the three
JSON-RPC messages and answers `account/read` the way 0.163.0 was observed to
(docs/CODEX_BACKEND.md D10).
"""
import collections
import glob
import json
import ntpath
import os
import stat
import shutil
import sys
import tempfile
import textwrap
import time

import pytest

from fused_render import claude_health, codex_health, copilot_health

# The real function: the autouse fixture below replaces `candidates` per test.
_REAL_CANDIDATES = codex_health.candidates
_REAL_SHELL_PROBE = codex_health._shell_probe


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own shell home (own cache file), a PATH that inherits nothing, no
    override, an own CODEX_HOME — the suite runner's machine may well have a
    real, signed-in Codex."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv(codex_health.BIN_ENV, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(codex_health, "_shell_probe", lambda: None)
    # augmented_path() is built on claude_health's, whose candidate dirs
    # include ~/.local/bin and the Node prefixes — where a real codex lives.
    monkeypatch.setattr(claude_health, "candidates", lambda: ())
    monkeypatch.setattr(codex_health, "candidates", lambda: ())
    monkeypatch.setattr(codex_health, "_ADOPTED", None)


def _fake_cli(tmp_path, name="codex", executable=True):
    """A findable stand-in; never spawned by the resolve tests."""
    d = tmp_path / "fake-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\necho 'codex-cli 0.163.0'\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


# -- version -------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("codex-cli 0.163.0", (0, 163, 0)),
    ("codex-cli 0.163.0\n", (0, 163, 0)),
    ("0.1.2", (0, 1, 2)),
    ("", None),
    ("no version here", None),
])
def test_parse_version(text, want):
    assert codex_health.parse_version(text) == want


@pytest.mark.parametrize("stdout,stderr,want", [
    ("codex-cli 0.163.0\n", "", "0.163.0"),
    # the npm wrapper's warning names a temp dir with digits in it; the
    # banner is matched by name, so the warning never reads as the version
    ("codex-cli 0.163.0\n",
     'WARNING: proceeding ... (codex_home: AbsolutePathBuf("/tmp/claude-0/x"))\n',
     "0.163.0"),
    ("", "codex-cli 0.164.1\n", "0.164.1"),
    ("garbage\n", 'WARNING: /tmp/claude-0/x\n', None),
])
def test_probe_version_reads_the_banner(monkeypatch, stdout, stderr, want):
    class R:
        returncode = 0
    R.stdout, R.stderr = stdout, stderr
    monkeypatch.setattr(codex_health, "_run_probe", lambda *a, **k: R())
    assert codex_health.probe_version("/x/codex") == want


def test_the_declared_floor_admits_the_verified_version():
    assert codex_health.MIN_VERSION == "0.160.0"
    assert codex_health.is_outdated("0.163.0") is False
    assert codex_health.is_outdated("0.160.0") is False
    assert codex_health.is_outdated("0.159.9") is True
    assert codex_health.is_outdated(None) is False
    # not Claude's 2.0.0 floor by default
    assert codex_health.is_outdated("1.0.0") is False


# -- resolve order -------------------------------------------------------------


def test_the_candidate_list_is_the_contract_s_six_in_order():
    assert codex_health.BIN_ENV == "FUSED_RENDER_CODEX_BIN"
    assert codex_health.POSIX_CANDIDATES == (
        "~/.npm-global/bin/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        "~/.local/bin/codex",
        "~/.bun/bin/codex",
        "~/Library/pnpm/codex",
    )


def test_override_wins_and_is_reported_as_such(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(codex_health.BIN_ENV, "/nowhere/codex")
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    assert codex_health.resolve() == ("/nowhere/codex", "override")


def test_path_beats_the_candidate_list(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(codex_health, "candidates", lambda: ("/nowhere/codex",))
    assert codex_health.resolve() == (cli, "path")


def test_candidate_dirs_are_probed_when_path_is_stripped(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(codex_health, "candidates", lambda: (cli,))
    assert codex_health.resolve() == (cli, "candidate")


def test_a_non_executable_file_does_not_shadow_a_real_install(tmp_path, monkeypatch):
    dud = _fake_cli(tmp_path, executable=False)
    monkeypatch.setattr(codex_health, "candidates", lambda: (dud,))
    assert codex_health.resolve(allow_shell=False) == (None, None)


def test_a_shell_only_install_is_adopted_as_the_override(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(codex_health, "_shell_probe", lambda: cli)
    monkeypatch.setattr(codex_health, "probe_version", lambda p: "0.163.0")
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_IN)
    snap = codex_health._measure()
    assert snap["source"] == "shell" and snap["found"] is True
    assert os.environ.get(codex_health.BIN_ENV) == cli
    # a vanished adoption is dropped, not reported as the user's stale override
    os.remove(cli)
    assert codex_health.resolve(allow_shell=False) == (None, None)
    assert codex_health.BIN_ENV not in os.environ


def test_a_user_override_is_never_replaced_by_an_adoption(monkeypatch):
    monkeypatch.setenv(codex_health.BIN_ENV, "/mine/codex")
    assert codex_health.adopt("/found/codex") is False
    assert os.environ[codex_health.BIN_ENV] == "/mine/codex"


def test_config_dir_is_codex_home_else_dot_codex(monkeypatch):
    assert codex_health.config_dir() == os.environ["CODEX_HOME"]
    monkeypatch.delenv("CODEX_HOME")
    assert codex_health.config_dir() == os.path.expanduser("~/.codex")


# -- the app-server sign-in probe ----------------------------------------------

#: The two `account/read` results 0.163.0 was observed to give (email made up).
_SIGNED_IN = {"account": {"type": "chatgpt", "email": "dev@example.com",
                          "planType": "go"},
              "requiresOpenaiAuth": True,
              "workspaceRouting": {"chatgptAccountId": "x",
                                   "backendOrigin": "https://chatgpt.com",
                                   "accountRoutingOverride": "NO_CONSTRAINT"}}
_SIGNED_OUT = {"account": None, "requiresOpenaiAuth": True, "workspaceRouting": None}


_FAKE_APP_SERVER = textwrap.dedent("""\
    #!{python}
    import json, os, signal, sys, time
    mode = os.environ.get("FAKE_CODEX_MODE", "in")
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.163.0")
        sys.exit(0)
    assert sys.argv[1:] == ["app-server"], sys.argv
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    got = []

    def log():
        # what the probe sent us, its env and our pid, so a test can check
        # the handshake order and that the probe reaped us
        with open(os.environ["FAKE_CODEX_LOG"], "w") as f:
            json.dump({{"pid": os.getpid(), "cwd": os.getcwd(), "got": got,
                       "codex_home": os.environ.get("CODEX_HOME"),
                       "pythonpath": os.environ.get("PYTHONPATH")}}, f)

    def send(msg):
        print(json.dumps(msg), flush=True)

    log()
    initialized = False
    for line in sys.stdin:
        msg = json.loads(line)
        got.append(msg)
        log()
        method = msg.get("method")
        if method == "initialize":
            if mode == "noise":
                # a server->client request that shares our reply's id
                send({{"id": 1, "method": "item/tool/requestUserInput", "params": {{}}}})
            send({{"id": msg["id"], "result": {{"userAgent": "fake/0.163.0",
                                               "codexHome": os.environ.get("CODEX_HOME"),
                                               "platformFamily": "unix",
                                               "platformOs": "linux"}}}})
            send({{"method": "configWarning", "params": {{"summary": "no bubblewrap"}}}})
        elif method == "initialized":
            initialized = True
        elif method == "account/read":
            if not initialized or msg.get("params") != {{"refreshToken": False}}:
                send({{"id": msg["id"], "error": {{"code": -32600, "message": "not initialized"}}}})
                continue
            if mode in ("hang", "stubborn"):
                time.sleep(60)
            if mode == "garbage":
                print("not json at all", flush=True)
                sys.exit(1)
            if mode == "noise":
                send({{"id": 2, "method": "account/chatgptAuthTokens/refresh", "params": {{}}}})
                send({{"method": "account/updated", "params": {{"authMode": "chatgpt"}}}})
            if mode == "error":
                send({{"id": msg["id"], "error": {{"code": -32603, "message": "token store locked"}}}})
                continue
            result = {{
                "in": {signed_in},
                "noise": {signed_in},
                "out": {signed_out},
                "apikey": {{"account": {{"type": "apiKey"}}, "requiresOpenaiAuth": True}},
                "provider": {{"account": None, "requiresOpenaiAuth": False}},
            }}[mode]
            send({{"id": msg["id"], "result": result}})
    # stdin EOF: the real app-server exits here (verified), which is what the
    # probe's close-then-wait reap order relies on.
""")


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    """A `codex` that speaks the app-server wire. The shebang is this
    interpreter: the isolation fixture's PATH has no python on it."""
    p = tmp_path / "server-bin" / "codex"
    p.parent.mkdir()
    p.write_text(_FAKE_APP_SERVER.format(python=sys.executable,
                                         signed_in=repr(_SIGNED_IN),
                                         signed_out=repr(_SIGNED_OUT)))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))

    def run(mode):
        monkeypatch.setenv("FAKE_CODEX_MODE", mode)
        return codex_health._app_server_probe(str(p))

    run.log = lambda: json.loads(log.read_text())
    run.path = str(p)
    return run


def test_a_signed_in_account_reads_true(fake_server):
    result = fake_server("in")
    assert codex_health._classify(result) is True
    assert codex_health.account(result) == {
        "method": "chatgpt", "email": "dev@example.com", "org": None, "plan": "go"}


def test_a_null_account_that_needs_auth_reads_false(fake_server):
    result = fake_server("out")
    assert codex_health._classify(result) is False
    assert codex_health.account(result) is None


def test_an_api_key_account_is_method_api_key(fake_server):
    result = fake_server("apikey")
    assert codex_health._classify(result) is True
    assert codex_health.account(result) == {
        "method": "api_key", "email": None, "org": None, "plan": None}


def test_the_handshake_is_initialize_initialized_then_account_read(fake_server):
    """`initialized` must come after the initialize reply and before any other
    request (contract §6.13); the fake refuses an `account/read` without it,
    and `refreshToken: false` keeps the read off the network."""
    assert fake_server("in") is not None
    got = fake_server.log()["got"]
    assert [m.get("method") for m in got] == ["initialize", "initialized", "account/read"]
    assert got[0]["params"]["clientInfo"]["name"] == "fused-render"
    assert "id" not in got[1]
    assert got[2]["params"] == {"refreshToken": False}


def test_a_server_request_with_our_id_is_not_mistaken_for_a_reply(fake_server):
    assert codex_health._classify(fake_server("noise")) is True


@pytest.mark.parametrize("mode", ["error", "garbage"])
def test_an_error_or_unparseable_reply_is_unknown_never_false(fake_server, mode):
    assert fake_server(mode) is None
    assert codex_health._classify(None) is None


@pytest.mark.parametrize("result,want", [
    (_SIGNED_IN, True),
    (_SIGNED_OUT, False),
    ({"account": None, "requiresOpenaiAuth": True}, False),
    ({"requiresOpenaiAuth": True}, False),                 # account omitted = null
    ({"account": {"type": "apiKey"}, "requiresOpenaiAuth": True}, True),
    ({"account": None, "requiresOpenaiAuth": False}, True),  # a provider needing no login
    ({"account": None}, None),
    ({"account": "chatgpt", "requiresOpenaiAuth": True}, None),
    ({}, None),
    (None, None),
    ([], None),
])
def test_only_a_null_account_that_requires_auth_classifies_false(result, want):
    assert codex_health._classify(result) is want


@pytest.mark.parametrize("acct,method", [
    ({"type": "chatgpt", "email": "a@b.c", "planType": "plus"}, "chatgpt"),
    ({"type": "apiKey"}, "api_key"),
    ({"type": "amazonBedrock"}, "bedrock"),
    ({"type": "somethingNew"}, "somethingNew"),
    ({}, None),
])
def test_account_method_maps_the_cli_s_account_type(acct, method):
    got = codex_health.account({"account": acct, "requiresOpenaiAuth": True})
    assert got["method"] == method and got["org"] is None
    assert set(got) == {"method", "email", "org", "plan"}


def _assert_reaped(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize("mode", ["hang", "stubborn"])
def test_a_hung_server_is_unknown_and_is_killed(fake_server, monkeypatch, mode):
    """"stubborn" ignores SIGTERM too: the reap escalates EOF → TERM → KILL."""
    monkeypatch.setattr(codex_health, "_RPC_TIMEOUT_S", 1.5)
    monkeypatch.setattr(codex_health, "_REAP_GRACE_S", 0.5)
    t0 = time.time()
    assert fake_server(mode) is None
    assert time.time() - t0 < 10  # bounded by the timeout + the reap steps
    _assert_reaped(fake_server.log()["pid"])


def test_the_probe_reaps_its_child_and_leaves_no_tempdir(fake_server):
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "fused-codex-probe-*")))
    assert codex_health._classify(fake_server("in")) is True
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "fused-codex-probe-*")))
    assert after <= before
    _assert_reaped(fake_server.log()["pid"])


def test_the_probe_does_not_chdir_the_child(fake_server):
    """No `cwd=`: that forces fork()+exec in the server (claude_spawn.py)."""
    fake_server("in")
    assert fake_server.log()["cwd"] == os.getcwd()


def test_the_child_gets_codex_home_as_found_and_no_python_path(fake_server, monkeypatch):
    """The user's own env, CODEX_HOME untouched (D10) — the packaged app's
    PYTHONPATH scrubbed, nothing invented."""
    monkeypatch.setenv("PYTHONPATH", "/bundle/lib")
    fake_server("in")
    seen = fake_server.log()
    assert seen["codex_home"] == os.environ["CODEX_HOME"]
    assert seen["pythonpath"] is None


def test_signed_in_without_a_cli_is_unknown(monkeypatch):
    assert codex_health.signed_in(None) is None
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_OUT)
    assert codex_health.signed_in("/x/codex") is False


# -- snapshot shape and cache --------------------------------------------------


def _quiet_claude(monkeypatch):
    for name in ("_shell_probe", "_auth_status", "_doctor"):
        monkeypatch.setattr(claude_health, name, lambda *a, **k: None)
    monkeypatch.setattr(claude_health, "probe_version", lambda p: None)


def test_snapshot_keys_match_claude_s_and_copilot_s(monkeypatch):
    _quiet_claude(monkeypatch)
    monkeypatch.setattr(copilot_health, "_shell_probe", lambda: None)
    monkeypatch.setattr(copilot_health, "candidates", lambda: ())
    theirs = set(claude_health._public(claude_health._measure(allow_shell=False)))
    ours = set(codex_health._public(codex_health._measure(allow_shell=False)))
    assert theirs <= ours, theirs - ours
    copilots = set(copilot_health._public(copilot_health._measure(allow_shell=False)))
    assert ours == copilots


def test_a_missing_cli_snapshot(monkeypatch):
    snap = codex_health._measure(allow_shell=False)
    assert snap["found"] is False and snap["signed_in"] is None
    assert snap["doctor"] is None and snap["account"] is None
    assert snap["install_command"] == "npm install -g @openai/codex"
    assert snap["install_method"] is None
    assert snap["config_dir"] == os.environ["CODEX_HOME"]
    assert snap["platform"] == sys.platform


def test_a_full_measure_against_the_fake_server(fake_server, monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(fake_server.path))
    monkeypatch.setenv("FAKE_CODEX_MODE", "in")
    snap = codex_health.summary_refreshed()
    assert snap["found"] is True and snap["source"] == "path"
    assert snap["version"] == "0.163.0" and snap["outdated"] is False
    assert snap["signed_in"] is True
    assert snap["account"] == {"method": "chatgpt", "email": "dev@example.com",
                               "org": None, "plan": "go"}
    assert "fingerprint" not in snap
    assert os.path.basename(codex_health._cache_path()) == "codex-health.json"
    monkeypatch.setenv("FAKE_CODEX_MODE", "out")
    snap = codex_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None


def test_snapshot_is_cached_then_served_from_disk(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(codex_health, "probe_version",
                        lambda p: calls.append(p) or "0.163.0")
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_IN)
    first = codex_health.summary()
    assert first["version"] == "0.163.0" and first["signed_in"] is True
    assert os.path.isfile(codex_health._cache_path())
    second = codex_health.summary()
    assert second == first and len(calls) == 1
    assert codex_health.cached()["version"] == "0.163.0"
    assert "fingerprint" not in first


def test_an_upgraded_binary_or_a_moved_home_invalidates_the_cache(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(codex_health, "probe_version",
                        lambda p: calls.append(p) or "0.163.0")
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_IN)
    codex_health.summary()
    os.utime(cli, (time.time() + 100, time.time() + 100))
    codex_health.summary()
    assert len(calls) == 2
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert codex_health.cached() is None
    codex_health.summary()
    assert len(calls) == 3


def test_an_unknown_probe_carries_the_last_account_forward(tmp_path, monkeypatch):
    """One slow read must not flip the strip to "Sign in" nor blank the
    signed-in row — and the carried account comes from the same CODEX_HOME."""
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(codex_health, "probe_version", lambda p: "0.163.0")
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_IN)
    assert codex_health.summary()["account"]["method"] == "chatgpt"
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: None)
    snap = codex_health.summary_refreshed()
    assert snap["signed_in"] is None
    assert snap["account"]["method"] == "chatgpt"
    assert snap["account"]["plan"] == "go"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert codex_health.summary_refreshed()["account"] is None


def test_a_definite_sign_out_does_not_carry_the_account(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(codex_health, "probe_version", lambda p: "0.163.0")
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_IN)
    codex_health.summary()
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_OUT)
    snap = codex_health.summary_refreshed()
    assert snap["signed_in"] is False and snap["account"] is None


def test_refresh_re_probes_and_a_corrupt_cache_is_tolerated(monkeypatch):
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: None)
    os.makedirs(os.path.dirname(codex_health._cache_path()), exist_ok=True)
    with open(codex_health._cache_path(), "w") as f:
        f.write("{not json")
    assert codex_health.summary()["found"] is False
    assert codex_health.summary_refreshed()["found"] is False
    assert codex_health.cached() is not None


# -- install method and the update offer ---------------------------------------


_NPM = "npm install -g @openai/codex"


@pytest.mark.parametrize("path,method,updatable,command", [
    ("/Users/x/.npm-global/bin/codex", "npm", True, _NPM),
    ("/home/x/.nvm/versions/node/v22.1.0/bin/codex", "npm", True, _NPM),
    ("/home/x/.bun/bin/codex", "npm", True, _NPM),
    # npm under a Homebrew Node: npm owns it, NOT brew (npm is tested first)
    ("/opt/homebrew/lib/node_modules/@openai/codex/bin/codex.js", "npm", True, _NPM),
    ("/opt/homebrew/bin/codex", "brew", False, "brew upgrade codex"),
    ("/usr/local/Cellar/codex/0.163.0/bin/codex", "brew", False, "brew upgrade codex"),
    ("/srv/tools/codex", "other", None, _NPM),
    (None, None, None, _NPM),
])
def test_install_method_and_update_plan(path, method, updatable, command):
    assert codex_health.install_method(path) == method
    plan = codex_health.update_plan(method)
    assert plan["updatable"] is updatable
    assert plan["command"] == command
    if updatable is False:
        assert plan["manager"] == "brew" and plan["reason"]


def test_install_method_follows_an_npm_symlink(tmp_path):
    """`/usr/local/bin/codex` is as often an npm link into node_modules as a
    Homebrew one; the realpath decides."""
    target = tmp_path / "prefix" / "lib" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    target.parent.mkdir(parents=True)
    target.write_text("")
    link = tmp_path / "prefix" / "bin" / "codex"
    link.parent.mkdir()
    link.symlink_to(target)
    assert codex_health.install_method(str(link)) == "npm"


def test_the_install_command_is_npm():
    assert codex_health.install_command() == codex_health.INSTALL_COMMAND_POSIX == _NPM


# -- login ---------------------------------------------------------------------

#: `codex login --device-auth`'s stdout, verbatim from 0.163.0 — escapes and all.
_DEVICE_OUTPUT = (
    "\n"
    "Welcome to Codex [v\x1b[90m0.163.0\x1b[0m]\n"
    "\x1b[90mOpenAI's command-line coding agent\x1b[0m\n"
    "\n"
    "Follow these steps to sign in with ChatGPT using device code authorization:\n"
    "\n"
    "1. Open this link in your browser and sign in to your account\n"
    "   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\n"
    "\n"
    "2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\n"
    "   \x1b[94m9N23-GJGFW\x1b[0m\n"
    "\n"
    "\x1b[90mContinue only if you started this login in Codex. If a website or "
    "another person gave you this code, cancel.\x1b[0m\n"
)


def _login(lines):
    from fused_render import codex_login
    login = codex_login._Login(proc=None, started_at=0,
                               tail=collections.deque(maxlen=40))
    for line in lines:
        plain = codex_login._plain(line)
        if plain.strip():
            login.tail.append(plain)
            codex_login._note_line(login, plain)
    return login


def test_login_parses_the_device_code_and_url_through_the_escapes():
    login = _login(_DEVICE_OUTPUT.split("\n"))
    assert login.detail == {"code": "9N23-GJGFW",
                            "url": "https://auth.openai.com/codex/device"}
    assert not any("\x1b" in line for line in login.tail)
    assert login.succeeded is False


def test_login_never_surfaces_an_authorize_url_or_its_state():
    """A URL with a query string is the browser flow's, carrying OAuth state;
    neither it nor a code-shaped run inside it reaches the UI."""
    login = _login(["https://auth.openai.com/codex/device?state=ABCD-EFGHI"])
    assert login.detail is None


def test_the_record_carries_the_code_never_the_tail(tmp_path, monkeypatch):
    """The endpoint answers in_flight/started_at/error/detail only — the
    child's own words stay in memory — and the health snapshot never learns
    the code."""
    from fused_render import codex_login
    login = _login(_DEVICE_OUTPUT.split("\n"))
    rec = codex_login._record(login)
    assert set(rec) == {"in_flight", "started_at", "error", "detail"}
    assert "Continue only if" not in json.dumps(rec)
    snap = codex_health._measure(allow_shell=False)
    assert "9N23-GJGFW" not in json.dumps(snap)


def test_login_failure_names_the_child_s_last_real_line():
    lines = _DEVICE_OUTPUT.split("\n") + [
        "\x1b[31mError logging in with device code: device code expired\x1b[0m"]
    login = _login(lines)
    from fused_render import codex_login
    assert codex_login._failure(login, 1) == \
        "Error logging in with device code: device code expired"
    # nothing but the instructions: the generic sentence, never the code
    login = _login(_DEVICE_OUTPUT.split("\n"))
    assert codex_login._failure(login, 1) == \
        "the sign-in ended without signing in (exit code 1)"


def test_login_refuses_without_a_cli():
    from fused_render import codex_login
    with pytest.raises(codex_login.LoginError):
        codex_login.start()


def _login_cli(tmp_path, monkeypatch, body):
    cli = tmp_path / "login-bin" / "codex"
    cli.parent.mkdir()
    cli.write_text(f"#!{sys.executable}\nimport sys, time, signal\n"
                   "assert sys.argv[1:] == ['login', '--device-auth'], sys.argv\n"
                   + body)
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(cli.parent))
    return cli


@pytest.mark.parametrize("tail,code", [
    ("print('Successfully logged in')\n", 0),
    # the success line counts even if the wrapper's exit status is lost
    ("print('Successfully logged in', flush=True)\nsys.exit(3)\n", 3),
])
def test_login_runs_the_cli_and_re_probes_on_success(tmp_path, monkeypatch, tail, code):
    from fused_render import codex_login
    _login_cli(tmp_path, monkeypatch,
               f"sys.stdout.write({_DEVICE_OUTPUT!r})\nsys.stdout.flush()\n" + tail)
    probes = []
    monkeypatch.setattr(codex_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    codex_login.reset()
    rec = codex_login.start()
    assert rec["in_flight"] is True
    login = codex_login._active
    assert login.settled.wait(10)
    assert login.proc.stdin is None  # DEVNULL: nothing to close
    st = codex_login.status()
    assert st["in_flight"] is False and st["error"] is None
    assert st["detail"] == {"code": "9N23-GJGFW",
                            "url": "https://auth.openai.com/codex/device"}
    assert probes == [1]
    assert codex_login.cancel()["canceled"] is False
    codex_login.reset()


def test_a_failed_login_reports_and_does_not_re_probe(tmp_path, monkeypatch):
    from fused_render import codex_login
    _login_cli(tmp_path, monkeypatch,
               "print('Error logging in with device code: denied')\nsys.exit(1)\n")
    probes = []
    monkeypatch.setattr(codex_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    codex_login.reset()
    codex_login.start()
    assert codex_login._active.settled.wait(10)
    assert codex_login.status()["error"] == "Error logging in with device code: denied"
    assert probes == []
    codex_login.reset()


def test_a_second_login_is_refused_while_one_waits(tmp_path, monkeypatch):
    from fused_render import codex_login
    _login_cli(tmp_path, monkeypatch, "time.sleep(60)\n")
    codex_login.reset()
    codex_login.start()
    with pytest.raises(codex_login.LoginError, match="already waiting"):
        codex_login.start()
    assert codex_login.cancel()["canceled"] is True
    codex_login.reset()


def test_cancel_escalates_to_kill_when_term_is_ignored(tmp_path, monkeypatch):
    from fused_render import codex_login
    _login_cli(tmp_path, monkeypatch,
               "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
               "print('ready', flush=True)\ntime.sleep(60)\n")
    monkeypatch.setattr(codex_login, "CANCEL_GRACE_S", 0.5)
    codex_login.reset()
    codex_login.start()
    login = codex_login._active
    for _ in range(100):  # until the child has installed its handler
        if any("ready" in line for line in login.tail):
            break
        time.sleep(0.05)
    out = codex_login.cancel()
    assert out["canceled"] is True and out["in_flight"] is False
    assert out["error"] is None
    assert login.proc.poll() is not None
    codex_login.reset()


# -- install -------------------------------------------------------------------


def _fake_npm(tmp_path, monkeypatch):
    npm = tmp_path / "npm-bin" / "npm"
    npm.parent.mkdir()
    npm.write_text("#!/bin/sh\necho npm \"$@\"\n")
    npm.chmod(0o755)
    monkeypatch.setenv("PATH", str(npm.parent))
    return str(npm)


def test_install_has_its_own_slot_and_argv(tmp_path, monkeypatch):
    from fused_render import claude_install, codex_install, copilot_install
    assert codex_install.JOB_ID == "sys:codex-install"
    assert codex_install.JOB_ID not in (claude_install.JOB_ID, copilot_install.JOB_ID)
    npm = _fake_npm(tmp_path, monkeypatch)
    argv, display = codex_install.install_argv()
    assert argv == [npm, "install", "-g", "@openai/codex"]
    assert display == codex_health.INSTALL_COMMAND_POSIX
    assert codex_install.update_argv("/x/codex") == (argv, display)


def test_install_without_npm_is_refused_up_front():
    from fused_render import codex_install
    codex_install.reset()
    with pytest.raises(codex_install.InstallError, match="install Node.js first"):
        codex_install.start("install")
    assert codex_install.status()["state"] == "idle"


def test_update_refuses_a_missing_or_brew_install(monkeypatch):
    from fused_render import codex_install
    codex_install.reset()
    with pytest.raises(codex_install.InstallError, match="install it first"):
        codex_install.start("update")
    monkeypatch.setattr(codex_health, "resolve",
                        lambda allow_shell=True: ("/opt/homebrew/bin/codex", "path"))
    monkeypatch.setattr(codex_health, "executable", lambda p: True)
    with pytest.raises(codex_install.InstallError, match="brew upgrade codex"):
        codex_install.start("update")
    with pytest.raises(codex_install.InstallError):
        codex_install.start("reboot")


def _wait_install():
    from fused_render import codex_install
    for _ in range(100):
        if codex_install.status()["state"] != "running":
            break
        time.sleep(0.05)
    return codex_install.status()


def test_install_runs_npm_to_done_and_re_probes(tmp_path, monkeypatch):
    from fused_render import codex_install
    codex_install.reset()
    _fake_npm(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_health, "summary_refreshed",
                        lambda: {"found": True, "version": "0.163.0"})
    monkeypatch.setattr(codex_install, "_report", lambda s: None)
    rec = codex_install.start("install")
    assert rec["state"] == "running" and rec["command"] == _NPM
    st = _wait_install()
    assert st["state"] == "done", st
    assert st["detail"] == "Codex CLI 0.163.0"
    assert "npm install -g @openai/codex" in st["output"]
    codex_install.reset()


def test_an_npm_install_updates_through_the_same_line(tmp_path, monkeypatch):
    from fused_render import codex_install
    codex_install.reset()
    _fake_npm(tmp_path, monkeypatch)
    monkeypatch.setattr(codex_health, "resolve",
                        lambda allow_shell=True: ("/u/.npm-global/bin/codex", "path"))
    monkeypatch.setattr(codex_health, "executable", lambda p: True)
    monkeypatch.setattr(codex_health, "summary_refreshed",
                        lambda: {"found": True, "version": "0.164.0"})
    monkeypatch.setattr(codex_install, "_report", lambda s: None)
    assert codex_install.start("update")["action"] == "update"
    st = _wait_install()
    assert st["state"] == "done" and st["detail"] == "Codex CLI 0.164.0"
    codex_install.reset()


# -- the router ----------------------------------------------------------------


def _codex_switch(monkeypatch, on):
    """The D17 Preferences switch. Off is ALSO the real default, which
    `test_the_default_switch_is_off_and_gates_every_spawn` reads unpatched."""
    from fused_render.agent_backends import registry
    real = registry.is_enabled
    monkeypatch.setattr(registry, "is_enabled",
                        lambda b: on if b == "codex" else real(b))


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.codex_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_endpoint_answers_the_snapshot(monkeypatch):
    _codex_switch(monkeypatch, True)
    monkeypatch.setattr(codex_health, "summary",
                        lambda: {"found": True, "version": "0.163.0"})
    assert _client().get("/api/codex/health").json() == {"found": True, "version": "0.163.0"}


_MUTATING = ["/api/codex/health/refresh", "/api/codex/install",
             "/api/codex/login", "/api/codex/login/cancel",
             "/api/codex/link-path"]


@pytest.mark.parametrize("route", _MUTATING)
def test_mutating_posts_require_the_fused_header(monkeypatch, route):
    _codex_switch(monkeypatch, True)
    monkeypatch.setattr(codex_health, "summary_refreshed", lambda: {"found": False})
    assert _client().post(route).status_code == 403


def _no_spawn(monkeypatch):
    """Every entry that could spawn or write, made to fail the test."""
    from fused_render import codex_install, codex_login

    def boom(*a, **k):
        raise AssertionError("a disabled Codex was probed or spawned")
    for mod, name in ((codex_health, "summary"), (codex_health, "summary_refreshed"),
                      (codex_health, "_measure"), (codex_health, "add_to_shell_path"),
                      (codex_install, "start"), (codex_login, "start"),
                      (codex_login, "cancel")):
        monkeypatch.setattr(mod, name, boom)


def test_the_default_switch_is_off_and_gates_every_spawn(monkeypatch):
    """D17: Codex is off until the user turns it on; no route may spawn it.
    Unpatched — this reads the isolated home's (absent) prefs.json."""
    from fused_render.agent_backends import registry
    assert registry.get("codex").id == "codex"
    assert registry.is_enabled("codex") is False
    _no_spawn(monkeypatch)
    c = _client()
    assert c.get("/api/codex/health").json() is None  # cold cache: null
    for route in _MUTATING:
        if route == "/api/codex/login/cancel":
            continue
        r = c.post(route, headers={"X-Fused": "1"})
        assert r.status_code == 400, route
        assert r.json() == registry.disabled_error("codex")
        assert r.json()["reason"] == "backend_disabled"
        assert r.json()["backend"] == "codex"


def test_cancel_still_stops_a_sign_in_after_codex_is_turned_off(tmp_path, monkeypatch):
    """Cancel signals only our own child, so the switch never blocks it: a
    sign-in started with Codex on must stay stoppable once it is off."""
    from fused_render import codex_login
    _login_cli(tmp_path, monkeypatch,
               f"sys.stdout.write({_DEVICE_OUTPUT!r})\nsys.stdout.flush()\ntime.sleep(60)\n")
    codex_login.reset()
    _codex_switch(monkeypatch, True)
    assert _client().post("/api/codex/login", headers={"X-Fused": "1"}).json()["in_flight"] is True
    for _ in range(100):
        if _client().get("/api/codex/login").json()["detail"]:
            break
        time.sleep(0.05)
    assert _client().get("/api/codex/login").json()["detail"]["code"] == "9N23-GJGFW"
    _codex_switch(monkeypatch, False)
    c = _client()
    assert c.post("/api/codex/login/cancel").status_code == 403  # X-Fused still applies
    r = c.post("/api/codex/login/cancel", headers={"X-Fused": "1"})
    assert r.status_code == 200 and r.json()["canceled"] is True
    assert r.json()["in_flight"] is False
    assert codex_login._active.proc.poll() is not None
    codex_login.reset()


def test_disabled_health_serves_the_cache_never_a_measure(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(codex_health, "probe_version", lambda p: "0.163.0")
    monkeypatch.setattr(codex_health, "_app_server_probe", lambda p: _SIGNED_IN)
    codex_health.summary()  # measured while the switch was on
    _codex_switch(monkeypatch, False)
    _no_spawn(monkeypatch)
    snap = _client().get("/api/codex/health").json()
    assert snap["version"] == "0.163.0" and snap["signed_in"] is True
    assert "fingerprint" not in snap


def test_refresh_install_and_login_round_trip(monkeypatch):
    from fused_render import codex_install, codex_login
    _codex_switch(monkeypatch, True)
    monkeypatch.setattr(codex_health, "summary_refreshed", lambda: {"found": False})
    monkeypatch.setattr(codex_install, "start",
                        lambda action: (_ for _ in ()).throw(codex_install.InstallError("nope " + action)))
    codex_login.reset()
    codex_install.reset()
    c = _client()
    h = {"X-Fused": "1"}
    assert c.post("/api/codex/health/refresh", headers=h).json() == {"found": False}
    r = c.post("/api/codex/install", headers=h, json={"action": "update"})
    assert r.status_code == 409 and r.json()["error"] == "nope update"
    assert c.get("/api/codex/install").json()["state"] == "idle"
    assert c.post("/api/codex/login", headers=h).status_code == 409  # no CLI
    assert c.get("/api/codex/login").json()["in_flight"] is False
    assert c.post("/api/codex/login/cancel", headers=h).json()["canceled"] is False
    assert c.post("/api/codex/link-path", headers=h).status_code == 409


def test_the_server_app_mounts_the_codex_routes():
    from fused_render.server import app as server_app
    from fused_render.server.routers.codex_health import router
    assert server_app.codex_health_router is router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert paths == {"/api/codex/health", "/api/codex/health/refresh",
                     "/api/codex/install", "/api/codex/link-path",
                     "/api/codex/login", "/api/codex/login/cancel"}


# -- Windows discovery -----------------------------------------------------------


class _FakeWinreg:
    """Just enough of `winreg`: two keys, each holding one Path value."""
    HKEY_CURRENT_USER = "HKCU"
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, user=None, machine=None, error=None):
        self.values = {"HKCU": user, "HKLM": machine}
        self.error = error

    def OpenKey(self, root, sub):
        if self.error or self.values[root] is None:
            raise self.error or OSError("no such key")
        outer = self

        class _Key:
            def __enter__(self_):
                return root

            def __exit__(self_, *exc):
                return False
        return _Key()

    def QueryValueEx(self, key, name):
        assert name == "Path"
        return self.values[key], 2


def test_windows_augmented_path_adds_registry_dirs_deduped(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os.path, "normcase", str.lower)
    monkeypatch.setattr(os.path, "expandvars", ntpath.expandvars)
    monkeypatch.setenv("FAKE_ROOT", "/Root")
    monkeypatch.setenv("PATH", os.pathsep.join(["/Node", "/Other"]))
    fake = _FakeWinreg(
        user=os.pathsep.join(["%FAKE_ROOT%/npm", "/node"]),
        machine=os.pathsep.join(["/Program Files/nodejs", "/OTHER", ""]),
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    parts = codex_health.augmented_path().split(os.pathsep)
    assert parts == ["/Node", "/Other", "/Root/npm",
                     "/Program Files/nodejs"]


def test_windows_registry_path_swallows_oserror(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(error=OSError("denied")))
    assert claude_health._windows_registry_path() == []


def test_registry_path_is_empty_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(user="X", machine="Y"))
    assert claude_health._windows_registry_path() == []


