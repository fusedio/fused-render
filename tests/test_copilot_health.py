"""Tests for fused_render/copilot_health.py, copilot_login.py, copilot_install.py
and the /api/copilot/* router.

Same discipline as test_claude_health.py: no test runs a real `copilot`.
Resolution is driven through a fake tree plus a patched PATH; the one probe
that must actually be exercised end to end — the ACP sign-in handshake — runs
against a fake `copilot` written in Python that speaks the two JSON-RPC
messages and answers `session/new` the way 1.0.98 was observed to.
"""
import json
import os
import stat
import sys
import textwrap
import time

import pytest

from fused_render import claude_health, copilot_health


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Own shell home (own cache file), a PATH that inherits nothing, no
    override, no token in the environment — the suite runner's machine may
    well have a real, signed-in Copilot AND a real `gh` token."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv(copilot_health.BIN_ENV, raising=False)
    for name in copilot_health.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "copilot"))
    monkeypatch.setattr(copilot_health, "_shell_probe", lambda: None)
    # augmented_path() is built on claude_health's, whose candidate dirs
    # include ~/.local/bin — where this machine's real copilot lives.
    monkeypatch.setattr(claude_health, "candidates", lambda: ())
    monkeypatch.setattr(copilot_health, "candidates", lambda: ())
    # `gh auth token` would read the developer's real credential store.
    monkeypatch.setattr(copilot_health, "gh_token", lambda: None)
    monkeypatch.setattr(copilot_health, "_ADOPTED", None)


def _fake_cli(tmp_path, name="copilot", executable=True):
    """A findable stand-in; never spawned by the resolve tests."""
    d = tmp_path / "fake-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\necho 'GitHub Copilot CLI 1.0.98.'\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


# -- version -------------------------------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("GitHub Copilot CLI 1.0.98.", (1, 0, 98)),
    ("GitHub Copilot CLI 1.0.98.\nRun 'copilot update' to check for updates.",
     (1, 0, 98)),
    ("1.2.3", (1, 2, 3)),
    ("", None),
    ("no version here", None),
])
def test_parse_version(text, want):
    assert copilot_health.parse_version(text) == want


def test_probe_version_reads_the_banner(monkeypatch):
    class R:
        returncode, stdout, stderr = 0, "GitHub Copilot CLI 1.0.98.\nRun 'copilot update'\n", ""
    monkeypatch.setattr(copilot_health, "_run_probe", lambda *a, **k: R())
    assert copilot_health.probe_version("/x/copilot") == "1.0.98"


def test_the_declared_floor_admits_the_verified_version():
    assert copilot_health.is_outdated("1.0.98", copilot_health.MIN_VERSION) is False
    assert copilot_health.is_outdated("1.0.1", copilot_health.MIN_VERSION) is True
    assert copilot_health.is_outdated(None, copilot_health.MIN_VERSION) is False


# -- resolve order -------------------------------------------------------------


def test_override_wins_and_is_reported_as_such(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv(copilot_health.BIN_ENV, "/nowhere/copilot")
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    assert copilot_health.resolve() == ("/nowhere/copilot", "override")


def test_path_beats_the_candidate_list(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    assert copilot_health.resolve() == (cli, "path")


def test_candidate_dirs_are_probed_when_path_is_stripped(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(copilot_health, "candidates", lambda: (cli,))
    assert copilot_health.resolve() == (cli, "candidate")


def test_a_non_executable_file_does_not_shadow_a_real_install(tmp_path, monkeypatch):
    dud = _fake_cli(tmp_path, executable=False)
    monkeypatch.setattr(copilot_health, "candidates", lambda: (dud,))
    assert copilot_health.resolve(allow_shell=False) == (None, None)


def test_a_shell_only_install_is_adopted_as_the_override(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setattr(copilot_health, "_shell_probe", lambda: cli)
    monkeypatch.setattr(copilot_health, "probe_version", lambda p: "1.0.98")
    monkeypatch.setattr(copilot_health, "_acp_probe", lambda p, t: True)
    snap = copilot_health._measure()
    assert snap["source"] == "shell" and snap["found"] is True
    assert os.environ.get(copilot_health.BIN_ENV) == cli
    # a vanished adoption is dropped, not reported as the user's stale override
    os.remove(cli)
    assert copilot_health.resolve(allow_shell=False) == (None, None)
    assert copilot_health.BIN_ENV not in os.environ


def test_candidate_list_names_only_copilot():
    assert all(c.endswith("/copilot") for c in copilot_health.candidates())


# -- the ACP sign-in probe -----------------------------------------------------


_FAKE_ACP = textwrap.dedent("""\
    #!{python}
    import json, os, sys, time, uuid
    mode = os.environ.get("FAKE_COPILOT_MODE", "ok")
    assert sys.argv[1:] == ["--acp", "--no-auto-update", "--disable-builtin-mcps"], sys.argv
    token = os.environ.get("COPILOT_GITHUB_TOKEN")
    # record what the probe handed us (the tests' tokens are dummies), and our
    # pid so a test can check the probe reaped us
    with open(os.environ["FAKE_COPILOT_LOG"], "w") as f:
        json.dump({{"token_set": bool(token), "token": token, "pid": os.getpid(),
                   "cwd": os.getcwd()}}, f)

    def session(sid):
        # 1.0.98 persists every ACP session here; the probe must delete it
        state = os.path.join(os.environ["COPILOT_HOME"], "session-state", sid)
        os.makedirs(state, exist_ok=True)
        open(os.path.join(state, "events.jsonl"), "w").close()
        return {{"sessionId": sid, "configOptions": []}}

    for line in sys.stdin:
        msg = json.loads(line)
        if msg["method"] == "initialize":
            print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"],
                              "result": {{"protocolVersion": 1}}}}), flush=True)
        elif msg["method"] == "session/new":
            if mode == "hang":
                time.sleep(60)
            print(json.dumps({{"jsonrpc": "2.0", "method": "session/update",
                              "params": {{"update": {{"sessionUpdate": "x"}}}}}}), flush=True)
            if mode == "gh-only":
                # no stored login: only an injected token signs this one in
                mode = "ok" if token else "auth"
            if mode in ("ok", "upper"):
                sid = str(uuid.uuid4())
                print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"],
                                  "result": session(sid.upper() if mode == "upper" else sid)}}),
                      flush=True)
            elif mode == "badid":
                print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {{"sessionId": "../victim"}}}}), flush=True)
            elif mode == "entitled":
                print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {{"code": -32603, "message":
                                            "You are not authorized to use this Copilot feature"}}}}), flush=True)
            elif mode == "auth":
                print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {{"code": -32000, "message": "Authentication required"}}}}), flush=True)
            elif mode == "other":
                print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"],
                                  "error": {{"code": -32603, "message": "disk full"}}}}), flush=True)
            elif mode == "garbage":
                print("not json at all", flush=True)
                sys.exit(1)
    # stdin EOF: a real ACP server exits here, which is what the probe's
    # close-then-wait reap order relies on. "hang" above never gets this far.
""")


@pytest.fixture
def fake_acp(tmp_path, monkeypatch):
    """A `copilot` that speaks ACP. The shebang is this interpreter: the
    isolation fixture's PATH has no python on it."""
    p = tmp_path / "acp-bin" / "copilot"
    p.parent.mkdir()
    p.write_text(_FAKE_ACP.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_COPILOT_LOG", str(log))

    def run(mode, token=None):
        monkeypatch.setenv("FAKE_COPILOT_MODE", mode)
        return copilot_health._acp_probe(str(p), token)

    run.log = lambda: json.loads(log.read_text())
    run.path = str(p)
    return run


def test_a_session_result_means_signed_in(fake_acp):
    assert fake_acp("ok") is True


def test_an_auth_required_error_means_signed_out(fake_acp):
    assert fake_acp("auth") is False


def test_any_other_error_is_unknown_never_false(fake_acp):
    assert fake_acp("other") is None


def test_unparseable_output_is_unknown(fake_acp):
    assert fake_acp("garbage") is None


def test_an_entitlement_error_is_unknown_not_signed_out(fake_acp):
    """"not authorized to use Copilot" is a seat problem; "sign in" would loop."""
    assert fake_acp("entitled") is None


@pytest.mark.parametrize("code,message,want", [
    (-32000, "Authentication required", False),
    (-32000, "", False),
    (-32603, "Authentication required", False),
    (-32603, "  Not logged in. Run copilot login.", False),
    (-32603, "You are not authorized to use this Copilot feature", None),
    (-32603, "authorized only for org members", None),
    (-32603, "author field missing", None),
    (-32603, "bad design", None),
    (-32603, "process got signal 15", None),
    (-32603, "login service unavailable", None),
])
def test_only_the_anchored_signed_out_error_classifies_false(code, message, want):
    reply = {"id": 2, "error": {"code": code, "message": message}}
    assert copilot_health._classify(reply) is want


def _assert_reaped(pid):
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_hung_server_is_unknown_and_is_killed(fake_acp, monkeypatch):
    monkeypatch.setattr(copilot_health, "_ACP_TIMEOUT_S", 1.5)
    t0 = time.time()
    assert fake_acp("hang") is None
    assert time.time() - t0 < 10  # bounded by the timeout + the kill wait
    _assert_reaped(fake_acp.log()["pid"])


def test_the_probe_cleans_up_its_tempdir_and_child(fake_acp, tmp_path):
    import glob
    import tempfile
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "fused-copilot-probe-*")))
    assert fake_acp("ok") is True
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "fused-copilot-probe-*")))
    assert after <= before
    _assert_reaped(fake_acp.log()["pid"])


def test_the_probe_deletes_the_session_it_created(fake_acp):
    """Every `session/new` persists `session-state/<uuid>/`, which `copilot
    --resume` would list; the probe's must not outlive it. Only that one."""
    state = os.path.join(os.environ["COPILOT_HOME"], "session-state")
    mine = os.path.join(state, "0b9d5a1e-6f2c-4c1a-9f3e-2d7b8c4a5e61")
    os.makedirs(mine)
    assert fake_acp("ok") is True
    assert os.listdir(state) == [os.path.basename(mine)]


def test_an_upper_case_session_id_is_deleted_too(fake_acp):
    assert fake_acp("upper") is True
    state = os.path.join(os.environ["COPILOT_HOME"], "session-state")
    assert os.listdir(state) == []


def test_a_session_id_that_is_not_a_uuid_is_never_joined_onto_a_path(fake_acp):
    victim = os.path.join(os.environ["COPILOT_HOME"], "victim")
    os.makedirs(victim)
    assert fake_acp("badid") is True
    assert os.path.isdir(victim)


def test_a_user_set_copilot_token_is_never_overridden(fake_acp, monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "user_tok")
    assert fake_acp("ok", token="gho_gh") is True
    assert fake_acp.log()["token"] == "user_tok"


def test_the_gh_token_reaches_the_child_and_nothing_else(fake_acp):
    """D10: the probe runs the way the host will spawn — with the gh token in
    COPILOT_GITHUB_TOKEN — and the token value is never written anywhere."""
    assert fake_acp("ok", token="gho_secret") is True
    assert fake_acp.log()["token_set"] is True
    assert fake_acp("ok") is True
    assert fake_acp.log()["token_set"] is False


def test_the_probe_does_not_chdir_the_child(fake_acp):
    """No `cwd=`: that forces fork()+exec in the server (claude_spawn.py).
    The tempdir travels only in the RPC params."""
    fake_acp("ok")
    assert fake_acp.log()["cwd"] == os.getcwd()


def test_account_method_names_the_credential_the_host_will_use(monkeypatch):
    assert copilot_health.account(True, via_gh=True) == {
        "method": "gh", "email": None, "org": None, "plan": None}
    assert copilot_health.account(True, via_gh=False)["method"] == "copilot"
    monkeypatch.setenv("GH_TOKEN", "x")
    assert copilot_health.account(True, via_gh=False)["method"] == "token"
    assert copilot_health.account(False, via_gh=True) is None
    assert copilot_health.account(None, via_gh=True) is None


def test_gh_token_is_never_in_the_snapshot(fake_acp, monkeypatch):
    monkeypatch.setenv("PATH", os.path.dirname(fake_acp.path))
    monkeypatch.setattr(copilot_health, "gh_token", lambda: "gho_secret")
    monkeypatch.setattr(copilot_health, "probe_version", lambda p: "1.0.98")
    monkeypatch.setenv("FAKE_COPILOT_MODE", "gh-only")
    snap = copilot_health.summary_refreshed()
    assert snap["signed_in"] is True
    assert snap["account"]["method"] == "gh"
    assert "gho_secret" not in json.dumps(snap)
    with open(copilot_health._cache_path()) as f:
        assert "gho_secret" not in f.read()


# The two-step probe: stored creds (or the user's own token) first, the gh
# token only after a definite False. `_acp_probe` is recorded, not spawned.


def _two_step(monkeypatch, first, second=None, gh="gho_gh"):
    calls, gh_calls = [], []

    def probe(path, token):
        calls.append(token)
        return first if token is None else second

    monkeypatch.setattr(copilot_health, "_acp_probe", probe)
    monkeypatch.setattr(copilot_health, "gh_token",
                        lambda: gh_calls.append(1) or gh)
    signed, via_gh = copilot_health._sign_in("/x/copilot")
    return signed, copilot_health.account(signed, via_gh), calls, gh_calls


def test_a_working_stored_login_never_asks_gh(monkeypatch):
    signed, acct, calls, gh_calls = _two_step(monkeypatch, first=True)
    assert signed is True and acct["method"] == "copilot"
    assert calls == [None] and gh_calls == []


def test_a_working_user_token_is_method_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "user_tok")
    signed, acct, calls, gh_calls = _two_step(monkeypatch, first=True)
    assert signed is True and acct["method"] == "token"
    assert calls == [None] and gh_calls == []


def test_signed_out_falls_back_to_the_gh_token(monkeypatch):
    signed, acct, calls, _ = _two_step(monkeypatch, first=False, second=True)
    assert signed is True and acct["method"] == "gh"
    assert calls == [None, "gho_gh"]


def test_a_gh_account_without_copilot_leaves_signed_out_standing(monkeypatch):
    signed, acct, calls, _ = _two_step(monkeypatch, first=False, second=False)
    assert signed is False and acct is None
    assert calls == [None, "gho_gh"]


def test_a_second_probe_that_times_out_is_unknown_not_signed_out(monkeypatch):
    signed, _acct, calls, _ = _two_step(monkeypatch, first=False, second=None)
    assert signed is None
    assert calls == [None, "gho_gh"]


@pytest.mark.parametrize("first,second", [(False, None), (None, None)])
def test_an_unknown_probe_carries_the_last_account_forward(tmp_path, monkeypatch,
                                                            first, second):
    """One slow read must not flip a gh-only machine to "Sign in", nor drop
    `method == "gh"`, agent.py's gate for injecting the token."""
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(copilot_health, "probe_version", lambda p: "1.0.98")
    monkeypatch.setattr(copilot_health, "gh_token", lambda: "gho_gh")
    monkeypatch.setattr(copilot_health, "_acp_probe",
                        lambda p, t: t is not None)
    assert copilot_health.summary()["account"]["method"] == "gh"
    monkeypatch.setattr(copilot_health, "_acp_probe",
                        lambda p, t: first if t is None else second)
    snap = copilot_health.summary_refreshed()
    assert snap["signed_in"] is None
    assert snap["account"]["method"] == "gh"
    # ...and from the same credential store only
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "elsewhere"))
    assert copilot_health.summary_refreshed()["account"] is None


@pytest.mark.parametrize("first,gh,env", [
    (False, None, {}),                                # no gh token
    (None, "gho_gh", {}),                             # unknown is not False
    (False, "gho_gh", {"COPILOT_GITHUB_TOKEN": "u"}),  # theirs wins, gh never asked
    (False, "gho_gh", {"GH_TOKEN": "gho_gh"}),        # gh just echoes GH_TOKEN
])
def test_no_second_probe(monkeypatch, first, gh, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    signed, acct, calls, gh_calls = _two_step(monkeypatch, first=first,
                                              second=True, gh=gh)
    assert calls == [None] and signed is first
    if "COPILOT_GITHUB_TOKEN" in env:
        assert gh_calls == []


def test_signed_in_runs_the_same_two_steps(monkeypatch):
    monkeypatch.setattr(copilot_health, "_acp_probe",
                        lambda p, t: t is not None)
    monkeypatch.setattr(copilot_health, "gh_token", lambda: "gho_gh")
    assert copilot_health.signed_in("/x/copilot") is True


def test_signed_in_from_an_env_token_when_the_cli_is_missing(monkeypatch):
    assert copilot_health.signed_in(None) is None
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    assert copilot_health.signed_in(None) is True


def test_gh_token_resolves_gh_through_github_setup(monkeypatch):
    """Not `which`: the app's own `gh` lives under the shell home."""
    from fused_render import github_setup
    calls = []
    monkeypatch.setattr(github_setup, "resolve", lambda: ("/app/bin/gh", "candidate"))
    monkeypatch.setattr(copilot_health, "executable", lambda p: True)

    class R:
        returncode, stdout, stderr = 0, "gho_abc\n", ""

    def fake_run(argv, **kw):
        calls.append(argv)
        return R()
    monkeypatch.setattr(copilot_health.subprocess, "run", fake_run)
    real = _REAL_GH_TOKEN
    assert real() == "gho_abc"
    assert calls == [["/app/bin/gh", "auth", "token"]]


_REAL_GH_TOKEN = copilot_health.gh_token


# -- snapshot shape and cache --------------------------------------------------


def _quiet_claude(monkeypatch):
    for name in ("_shell_probe", "_auth_status", "_doctor"):
        monkeypatch.setattr(claude_health, name, lambda *a, **k: None)
    monkeypatch.setattr(claude_health, "probe_version", lambda p: None)


def test_snapshot_keys_are_a_superset_of_claude_s(monkeypatch):
    _quiet_claude(monkeypatch)
    theirs = set(claude_health._public(claude_health._measure(allow_shell=False)))
    ours = set(copilot_health._public(copilot_health._measure(allow_shell=False)))
    assert theirs <= ours, theirs - ours


def test_a_missing_cli_snapshot(monkeypatch):
    snap = copilot_health._measure(allow_shell=False)
    assert snap["found"] is False and snap["signed_in"] is None
    assert snap["doctor"] is None and snap["account"] is None
    assert snap["install_command"] == copilot_health.INSTALL_COMMAND_POSIX
    assert snap["config_dir"] == os.environ["COPILOT_HOME"]
    assert snap["platform"] == sys.platform


def test_snapshot_is_cached_then_served_from_disk(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(copilot_health, "probe_version",
                        lambda p: calls.append(p) or "1.0.98")
    monkeypatch.setattr(copilot_health, "_acp_probe", lambda p, t: True)
    first = copilot_health.summary()
    assert first["version"] == "1.0.98" and first["signed_in"] is True
    assert os.path.isfile(copilot_health._cache_path())
    second = copilot_health.summary()
    assert second == first and len(calls) == 1
    assert copilot_health.cached()["version"] == "1.0.98"
    assert "fingerprint" not in first


def test_an_upgraded_binary_or_a_moved_home_invalidates_the_cache(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(copilot_health, "probe_version",
                        lambda p: calls.append(p) or "1.0.98")
    monkeypatch.setattr(copilot_health, "_acp_probe", lambda p, t: True)
    copilot_health.summary()
    os.utime(cli, (time.time() + 100, time.time() + 100))
    copilot_health.summary()
    assert len(calls) == 2
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "elsewhere"))
    copilot_health.summary()
    assert len(calls) == 3


def test_a_token_variable_appearing_invalidates_the_cache(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    calls = []
    monkeypatch.setattr(copilot_health, "probe_version",
                        lambda p: calls.append(p) or "1.0.98")
    monkeypatch.setattr(copilot_health, "_acp_probe", lambda p, t: True)
    assert copilot_health.summary()["account"]["method"] == "copilot"
    monkeypatch.setenv("GH_TOKEN", "gho_user_value")
    assert copilot_health.summary()["account"]["method"] == "token"
    assert len(calls) == 2
    with open(copilot_health._cache_path()) as f:
        disk = f.read()
    assert "GH_TOKEN" in disk and "gho_user_value" not in disk


def test_refresh_re_probes_and_a_corrupt_cache_is_tolerated(tmp_path, monkeypatch):
    monkeypatch.setattr(copilot_health, "_acp_probe", lambda p, t: None)
    os.makedirs(os.path.dirname(copilot_health._cache_path()), exist_ok=True)
    with open(copilot_health._cache_path(), "w") as f:
        f.write("{not json")
    assert copilot_health.summary()["found"] is False
    assert copilot_health.summary_refreshed()["found"] is False
    assert copilot_health.cached() is not None


# -- install method and the update offer ---------------------------------------


@pytest.mark.parametrize("path,method,updatable", [
    (os.path.expanduser("~/.local/bin/copilot"), "native", True),
    ("/opt/homebrew/bin/copilot", "brew", False),
    ("/usr/local/Caskroom/copilot-cli/1.0.98/copilot", "brew", False),
    ("/Users/x/.npm-global/bin/copilot", "npm", False),
    ("/usr/bin/copilot", "system", False),
    ("/usr/local/bin/copilot", None, None),
    ("/opt/node22/bin/copilot", None, None),
])
def test_install_method_and_update_plan(path, method, updatable):
    assert copilot_health.install_method(path) == method
    plan = copilot_health.update_plan(method)
    assert plan["updatable"] is updatable
    if updatable is False and method != "system":
        assert plan["command"] and "copilot" in plan["command"]


def test_the_install_command_is_the_script():
    assert copilot_health.install_command().startswith("curl -fsSL https://gh.io/copilot-install")


# -- login ---------------------------------------------------------------------


def test_login_strips_token_vars_from_the_child_env(monkeypatch):
    from fused_render import copilot_login
    monkeypatch.setenv("GH_TOKEN", "x")
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "y")
    env = copilot_login._child_env()
    assert not any(n in env for n in copilot_health.TOKEN_ENV)


def test_login_refuses_without_a_cli():
    from fused_render import copilot_login
    with pytest.raises(copilot_login.LoginError):
        copilot_login.start()


def test_login_notes_a_device_code_and_hides_the_authorize_url(tmp_path, monkeypatch):
    """The browser-flow URL carries OAuth state and is never surfaced; a
    device code is — the user has to type it."""
    import collections
    from fused_render import copilot_login
    login = copilot_login._Login(proc=None, started_at=0,
                                 tail=collections.deque(maxlen=10))
    copilot_login._note_device_code(
        login, "If it doesn't open automatically, visit: "
               "https://github.com/login/oauth/authorize?state=S&code_challenge=C")
    assert login.detail is None
    copilot_login._note_device_code(
        login, "Enter the code ABCD-1234 at https://github.com/login/device")
    assert login.detail == {"code": "ABCD-1234", "url": "https://github.com/login/device"}
    login.tail.extend(["Opening your browser to authenticate...",
                       "https://github.com/login/oauth/authorize?state=S",
                       "Waiting for authorization...",
                       "Error: access denied by the user"])
    assert copilot_login._failure(login, 1) == "Error: access denied by the user"


def test_login_runs_the_cli_and_re_probes_on_a_clean_exit(tmp_path, monkeypatch):
    from fused_render import copilot_login
    cli = tmp_path / "login-bin" / "copilot"
    cli.parent.mkdir()
    cli.write_text(f"#!{sys.executable}\nimport sys\nassert sys.argv[1:] == ['login']\n"
                   "print('Waiting for authorization...')\n")
    cli.chmod(0o755)
    monkeypatch.setenv("PATH", str(cli.parent))
    probes = []
    monkeypatch.setattr(copilot_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    copilot_login.reset()
    rec = copilot_login.start()
    assert rec["in_flight"] is True and rec["detail"] is None
    login = copilot_login._active
    assert login.settled.wait(10)
    assert login.proc.stdin.closed
    assert copilot_login.status()["in_flight"] is False
    assert copilot_login.status()["error"] is None
    assert probes == [1]
    assert copilot_login.cancel()["canceled"] is False
    copilot_login.reset()


# -- install -------------------------------------------------------------------


def test_install_has_its_own_slot_and_argv(monkeypatch):
    from fused_render import claude_install, copilot_install
    assert copilot_install.JOB_ID != claude_install.JOB_ID
    assert copilot_install.JOB_ID.startswith("sys:")
    argv, display = copilot_install.install_argv()
    assert argv == ["bash", "-c", copilot_health.INSTALL_COMMAND_POSIX]
    assert display == copilot_health.INSTALL_COMMAND_POSIX
    assert copilot_install.update_argv("/x/copilot") == (["/x/copilot", "update"], "copilot update")


def test_update_refuses_a_managed_install(tmp_path, monkeypatch):
    from fused_render import copilot_install
    copilot_install.reset()
    with pytest.raises(copilot_install.InstallError, match="install it first"):
        copilot_install.start("update")
    monkeypatch.setattr(copilot_health, "resolve",
                        lambda allow_shell=True: ("/opt/homebrew/bin/copilot", "path"))
    monkeypatch.setattr(copilot_health, "executable", lambda p: True)
    with pytest.raises(copilot_install.InstallError, match="brew upgrade --cask copilot-cli"):
        copilot_install.start("update")
    with pytest.raises(copilot_install.InstallError):
        copilot_install.start("reboot")


def test_install_runs_to_done_and_re_probes(tmp_path, monkeypatch):
    from fused_render import copilot_install
    copilot_install.reset()
    monkeypatch.setattr(copilot_install, "install_argv",
                        lambda: (["/bin/sh", "-c", "echo installing; echo done"], "fake"))
    monkeypatch.setattr(copilot_health, "summary_refreshed",
                        lambda: {"found": True, "version": "1.0.98"})
    monkeypatch.setattr(copilot_install, "_report", lambda s: None)
    rec = copilot_install.start("install")
    assert rec["state"] == "running"
    for _ in range(100):
        if copilot_install.status()["state"] != "running":
            break
        time.sleep(0.05)
    st = copilot_install.status()
    assert st["state"] == "done", st
    assert st["detail"] == "Copilot CLI 1.0.98"
    assert "installing" in st["output"]
    copilot_install.reset()


# -- the router ----------------------------------------------------------------


def _copilot_switch(monkeypatch, on):
    """The D17 Preferences switch. Off is ALSO the real default, which
    `test_the_default_switch_is_off_and_gates_every_spawn` reads unpatched."""
    from fused_render.agent_backends import registry
    real = registry.is_enabled
    monkeypatch.setattr(registry, "is_enabled",
                        lambda b: on if b == "copilot" else real(b))


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.copilot_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_endpoint_answers_the_snapshot(monkeypatch):
    _copilot_switch(monkeypatch, True)
    monkeypatch.setattr(copilot_health, "summary",
                        lambda: {"found": True, "version": "1.0.98"})
    assert _client().get("/api/copilot/health").json() == {"found": True, "version": "1.0.98"}


_MUTATING = ["/api/copilot/health/refresh", "/api/copilot/install",
             "/api/copilot/login", "/api/copilot/login/cancel",
             "/api/copilot/link-path"]


@pytest.mark.parametrize("route", _MUTATING)
def test_mutating_posts_require_the_fused_header(monkeypatch, route):
    _copilot_switch(monkeypatch, True)
    monkeypatch.setattr(copilot_health, "summary_refreshed", lambda: {"found": False})
    assert _client().post(route).status_code == 403


def _no_spawn(monkeypatch):
    """Every entry that could spawn or write, made to fail the test."""
    from fused_render import copilot_install, copilot_login

    def boom(*a, **k):
        raise AssertionError("a disabled Copilot was probed or spawned")
    for mod, name in ((copilot_health, "summary"), (copilot_health, "summary_refreshed"),
                      (copilot_health, "_measure"), (copilot_health, "add_to_shell_path"),
                      (copilot_install, "start"), (copilot_login, "start"),
                      (copilot_login, "cancel")):
        monkeypatch.setattr(mod, name, boom)


def test_the_default_switch_is_off_and_gates_every_spawn(monkeypatch):
    """D17: Copilot is off until the user turns it on; no route may spawn it.
    Unpatched — this reads the isolated home's (absent) prefs.json."""
    from fused_render.agent_backends import registry
    assert registry.is_enabled("copilot") is False
    _no_spawn(monkeypatch)
    c = _client()
    assert c.get("/api/copilot/health").json() is None  # cold cache: null
    for route in _MUTATING:
        r = c.post(route, headers={"X-Fused": "1"})
        assert r.status_code == 400, route
        assert r.json() == registry.disabled_error("copilot")
        assert r.json()["reason"] == "backend_disabled"


def test_disabled_health_serves_the_cache_never_a_measure(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(cli))
    monkeypatch.setattr(copilot_health, "probe_version", lambda p: "1.0.98")
    monkeypatch.setattr(copilot_health, "_acp_probe", lambda p, t: True)
    copilot_health.summary()  # measured while the switch was on
    _copilot_switch(monkeypatch, False)
    _no_spawn(monkeypatch)
    snap = _client().get("/api/copilot/health").json()
    assert snap["version"] == "1.0.98" and snap["signed_in"] is True
    assert "fingerprint" not in snap


def test_refresh_install_and_login_round_trip(monkeypatch):
    from fused_render import copilot_install, copilot_login
    _copilot_switch(monkeypatch, True)
    monkeypatch.setattr(copilot_health, "summary_refreshed", lambda: {"found": False})
    monkeypatch.setattr(copilot_install, "start",
                        lambda action: (_ for _ in ()).throw(copilot_install.InstallError("nope " + action)))
    copilot_login.reset()
    copilot_install.reset()
    c = _client()
    h = {"X-Fused": "1"}
    assert c.post("/api/copilot/health/refresh", headers=h).json() == {"found": False}
    r = c.post("/api/copilot/install", headers=h, json={"action": "update"})
    assert r.status_code == 409 and r.json()["error"] == "nope update"
    assert c.get("/api/copilot/install").json()["state"] == "idle"
    assert c.post("/api/copilot/login", headers=h).status_code == 409  # no CLI
    assert c.get("/api/copilot/login").json()["in_flight"] is False
    assert c.post("/api/copilot/login/cancel", headers=h).json()["canceled"] is False
    assert c.post("/api/copilot/link-path", headers=h).status_code == 409
    assert not any("doctor" in getattr(r, "path", "") for r in c.app.routes)
