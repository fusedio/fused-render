"""Tests for fused_render/claude_health.py and GET /api/claude/health.

The point of this module is to say something TRUE about the machine before
anything needs Claude Code, so the tests are mostly about the ways a health
report can lie: claiming an install is too old when the version could not be
read, claiming a macOS user is signed out because a file is missing, serving a
cached answer after the binary changed underneath it.

No test runs a real `claude`: resolution is driven through a fake tree plus a
patched PATH, and the version probe's one subprocess hop is patched at the
module boundary (the same discipline as test_server_ai.py).
"""
import json
import os

import pytest

from fused_render import claude_health


# Captured before any fixture stubs them, so a test that wants the REAL probe
# can ask for it by name. Cleaner than `monkeypatch.undo()`, which would drop
# the whole isolation fixture along with the one stub the test wants gone.
_REAL_SHELL_PROBE = claude_health._shell_probe
_REAL_AUTH_STATUS = claude_health._auth_status


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own shell home (so the cache file is its own) and a
    PATH/override/credential environment that inherits nothing from the machine
    running the suite — which may well have a real, signed-in Claude Code."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv(claude_health.BIN_ENV, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    # The two probes that would spawn something real: the login-shell probe
    # would source the SUITE RUNNER's own profile, and the auth probe would ask
    # the developer's actual Claude Code whether they are signed in. Both off by
    # default; the tests that care restore the real one by name.
    monkeypatch.setattr(claude_health, "_shell_probe", lambda: None)
    monkeypatch.setattr(claude_health, "_auth_status", lambda path: None)


def _fake_cli(tmp_path, name="claude", executable=True):
    """An executable stand-in for the CLI, in its own dir. Returns the path."""
    d = tmp_path / "fake-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\necho 2.1.220\n")
    p.chmod(0o755 if executable else 0o644)
    return str(p)


# -- version parsing and the floor -------------------------------------------


@pytest.mark.parametrize("text,want", [
    ("2.1.220", (2, 1, 220)),
    ("2.1.220 (Claude Code)", (2, 1, 220)),
    ("claude 1.0.88\n", (1, 0, 88)),
    ("  2.0  ", (2, 0)),
    ("", None),
    ("no digits here", None),
])
def test_parse_version(text, want):
    assert claude_health.parse_version(text) == want


def test_is_outdated_compares_numerically_not_lexically():
    # The bug a string compare would have: "2.1.9" > "2.1.10" lexically.
    assert claude_health.is_outdated("2.1.9", "2.1.10") is True
    assert claude_health.is_outdated("2.1.10", "2.1.9") is False


def test_is_outdated_zero_pads_a_shorter_version():
    """"2" is 2.0.0, not something below it — otherwise a CLI reporting a bare
    major would be called stale for having a short version string."""
    assert claude_health.is_outdated("2", "2.0.0") is False
    assert claude_health.is_outdated("2.0", "2.0.0") is False
    assert claude_health.is_outdated("1", "2.0.0") is True


def test_unreadable_version_is_never_outdated():
    """THE ASSERTION THAT MATTERS MOST HERE. A version we could not read says
    nothing about age, and answering True would put "your Claude Code is too
    old" in front of someone whose install is fine."""
    assert claude_health.is_outdated(None) is False
    assert claude_health.is_outdated("") is False
    assert claude_health.is_outdated("unknown") is False


def test_the_declared_floor_admits_the_verified_version():
    """MIN_VERSION must not reject the version the spawn line is verified
    against (server/ai.py's --tools= note pins that at 2.1.220)."""
    assert claude_health.is_outdated("2.1.220") is False
    assert claude_health.is_outdated("1.0.88") is True


# -- resolution ---------------------------------------------------------------


def test_override_wins_and_is_reported_as_such(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    monkeypatch.setenv(claude_health.BIN_ENV, "/opt/custom/claude")
    assert claude_health.resolve() == ("/opt/custom/claude", "override")


def test_a_stale_override_is_reported_not_silently_replaced(tmp_path, monkeypatch):
    """A pointing-at-nothing override is a real finding — it is exactly why a
    session will not start — so it must not be papered over by a working install
    that other code paths (which trust the override blindly) would never use."""
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    monkeypatch.setenv(claude_health.BIN_ENV, str(tmp_path / "gone" / "claude"))
    path, source = claude_health.resolve()
    assert source == "override"
    assert path != bin_path
    # ...and it must not be called usable.
    monkeypatch.setattr(claude_health, "probe_version", lambda p: None)
    assert claude_health._measure()["found"] is False


def test_path_beats_the_candidate_list(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    assert claude_health.resolve() == (bin_path, "path")


def test_candidate_dirs_are_probed_when_path_is_stripped(tmp_path, monkeypatch):
    """A Finder/Dock-launched .app inherits the supervisor's PATH, not a
    shell's, so the known install dirs are all that is left."""
    home = tmp_path / "userhome"
    (home / ".bun" / "bin").mkdir(parents=True)
    cli = home / ".bun" / "bin" / "claude"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    monkeypatch.setattr(claude_health.os.path, "expanduser",
                        lambda p: p.replace("~", str(home), 1))
    monkeypatch.setattr(claude_health.os, "name", "posix")
    # ~/.bun/bin is the case that used to resolve for the Claude-config tab and
    # not for fused.ai — the divergence the shared list closes.
    assert claude_health.resolve() == (str(cli), "candidate")


def test_a_non_executable_file_does_not_shadow_a_real_install(tmp_path, monkeypatch):
    """isfile alone was not enough: a non-executable file in an earlier
    candidate dir would win and then fail to spawn."""
    home = tmp_path / "userhome"
    (home / ".local" / "bin").mkdir(parents=True)
    dud = home / ".local" / "bin" / "claude"
    dud.write_text("not executable")
    dud.chmod(0o644)
    (home / ".bun" / "bin").mkdir(parents=True)
    real = home / ".bun" / "bin" / "claude"
    real.write_text("#!/bin/sh\n")
    real.chmod(0o755)
    monkeypatch.setattr(claude_health.os.path, "expanduser",
                        lambda p: p.replace("~", str(home), 1))
    monkeypatch.setattr(claude_health.os, "name", "posix")
    assert claude_health.resolve()[0] == str(real)


def test_nothing_installed_resolves_to_nothing():
    assert claude_health.resolve() == (None, None)


def test_shell_probe_is_the_last_resort_and_is_labelled(monkeypatch):
    """A binary only the login shell can see is a DIFFERENT diagnosis from a
    missing one: the app's own PATH is the problem, and the fix is the override
    rather than another install. `source` is how the UI can say so."""
    monkeypatch.setattr(claude_health, "_shell_probe", lambda: "/opt/volta/bin/claude")
    assert claude_health.resolve() == ("/opt/volta/bin/claude", "shell")
    # ...and it is skippable, because it costs seconds.
    assert claude_health.resolve(allow_shell=False) == (None, None)


def test_shell_probe_scrubs_the_bundled_interpreter_vars(monkeypatch):
    """The packaged app exports PYTHONHOME/PYTHONPATH for its own interpreter;
    a child that inherits them and is not that interpreter dies with
    "No module named 'encodings'"."""
    monkeypatch.setenv("PYTHONHOME", "/bundle/python")
    monkeypatch.setenv("PYTHONPATH", "/bundle/lib")
    monkeypatch.setenv("SHELL", "/bin/sh")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs.get("env") or {})

        class R:
            stdout = ""
        return R()

    monkeypatch.setattr(claude_health.subprocess, "run", fake_run)
    _REAL_SHELL_PROBE()  # the isolation fixture stubs the module attribute
    assert "PYTHONHOME" not in seen
    assert "PYTHONPATH" not in seen


def test_augmented_path_appends_install_dirs_without_duplicating(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    parts = claude_health.augmented_path().split(os.pathsep)
    assert parts[0] == "/usr/bin"
    assert len(parts) == len(set(parts))
    assert "/opt/homebrew/bin" in parts


# -- the version probe --------------------------------------------------------


def test_probe_version_reads_stdout(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        class R:
            returncode, stdout, stderr = 0, "2.1.220 (Claude Code)\n", ""
        return R()

    monkeypatch.setattr(claude_health.subprocess, "run", fake_run)
    assert claude_health.probe_version("/x/claude") == "2.1.220"


def test_probe_version_falls_back_to_stderr(monkeypatch):
    def fake_run(argv, **kwargs):
        class R:
            returncode, stdout, stderr = 0, "", "2.0.5\n"
        return R()

    monkeypatch.setattr(claude_health.subprocess, "run", fake_run)
    assert claude_health.probe_version("/x/claude") == "2.0.5"


@pytest.mark.parametrize("outcome", [
    {"returncode": 1, "stdout": "2.1.220", "stderr": ""},   # exited non-zero
    {"returncode": 0, "stdout": "", "stderr": ""},           # said nothing
    {"returncode": 0, "stdout": "nope", "stderr": ""},       # nothing parseable
])
def test_probe_version_is_none_when_it_would_not_tell_us(monkeypatch, outcome):
    def fake_run(argv, **kwargs):
        return type("R", (), outcome)()

    monkeypatch.setattr(claude_health.subprocess, "run", fake_run)
    assert claude_health.probe_version("/x/claude") is None


def test_probe_version_survives_a_hung_or_missing_binary(monkeypatch):
    import subprocess as sp

    def boom(argv, **kwargs):
        raise sp.TimeoutExpired(argv, 1)

    monkeypatch.setattr(claude_health.subprocess, "run", boom)
    assert claude_health.probe_version("/x/claude") is None

    monkeypatch.setattr(claude_health.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert claude_health.probe_version("/x/claude") is None


def test_a_windows_cmd_shim_goes_through_cmd_exe(monkeypatch):
    """npm installs claude as a .cmd, which CreateProcess cannot run directly.
    Without the hop both probes OSError on every npm-installed Windows CLI and
    report everything as unknown."""
    monkeypatch.setattr(claude_health.os, "name", "nt")
    cmd = claude_health._probe_cmd(r"C:\Users\A B\npm\claude.cmd", "auth", "status")
    assert isinstance(cmd, str)
    # every element quoted, so a space in the path cannot re-split the line
    assert cmd == r'"C:\Users\A B\npm\claude.cmd" "auth" "status"'
    # ...and an .exe on the same platform stays a plain argv list
    assert claude_health._probe_cmd(r"C:\npm\claude.exe", "--version") == [
        r"C:\npm\claude.exe", "--version"]


def test_a_posix_path_is_never_shelled(monkeypatch):
    monkeypatch.setattr(claude_health.os, "name", "posix")
    # a POSIX file that merely ENDS in .cmd is still exec'd directly
    assert claude_health._probe_cmd("/home/a/claude.cmd", "--version") == [
        "/home/a/claude.cmd", "--version"]


def test_a_quote_in_the_path_is_refused_not_misquoted(monkeypatch):
    monkeypatch.setattr(claude_health.os, "name", "nt")
    with pytest.raises(ValueError):
        claude_health._probe_cmd('C:\\ev"il\\claude.cmd', "auth", "status")
    # and the probes turn that into "unknown" rather than a 500
    monkeypatch.setattr(claude_health, "_auth_status", _REAL_AUTH_STATUS)
    assert claude_health.probe_version('C:\\ev"il\\claude.cmd') is None
    assert claude_health.signed_in('C:\\ev"il\\claude.cmd') is None


def test_the_version_probe_never_forks(monkeypatch):
    """close_fds=False is what keeps CPython on posix_spawn: a fork() with
    libproj resident in the server runs PROJ's atfork handler into a SIGSEGV
    before exec (rc -11, no output). Same discipline as every other subprocess
    in the package."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)

        class R:
            returncode, stdout, stderr = 0, "2.1.220", ""
        return R()

    monkeypatch.setattr(claude_health.subprocess, "run", fake_run)
    claude_health.probe_version("/x/claude")
    assert seen["close_fds"] is False
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"
    assert seen["timeout"] > 0


# -- sign-in ------------------------------------------------------------------


def _auth_says(monkeypatch, stdout, returncode=0, stderr=""):
    """Put the REAL `_auth_status` back (the isolation fixture stubs it) over a
    fake `claude auth status`, so the parse itself is what's under test."""
    monkeypatch.setattr(claude_health, "_auth_status", _REAL_AUTH_STATUS)

    def fake_run(argv, **kwargs):
        assert argv[1:] == ["auth", "status"], argv
        return type("R", (), {"returncode": returncode, "stdout": stdout,
                              "stderr": stderr})()

    monkeypatch.setattr(claude_health.subprocess, "run", fake_run)


def test_the_cli_is_asked_and_believed(monkeypatch):
    """`claude auth status` is the only party that actually knows, and it knows
    on every platform."""
    _auth_says(monkeypatch, '{"loggedIn": false, "authMethod": "none"}')
    assert claude_health.signed_in("/x/claude") is False
    _auth_says(monkeypatch, '{"loggedIn": true, "authMethod": "oauth_token"}')
    assert claude_health.signed_in("/x/claude") is True


def test_the_cli_beats_local_evidence(monkeypatch, tmp_path):
    """A credentials file left behind by a since-revoked login must not
    outvote the CLI saying it is signed out."""
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _auth_says(monkeypatch, '{"loggedIn": false}')
    assert claude_health.signed_in("/x/claude") is False


@pytest.mark.parametrize("stdout,rc,stderr", [
    # a CLI too old for the subcommand: nothing on stdout, error on stderr
    ("", 1, "error: unknown command 'status'"),
    ("not json at all", 0, ""),
    ('{"loggedIn": "yes"}', 0, ""),     # right key, wrong type
    ('{"authMethod": "none"}', 0, ""),  # no loggedIn at all
    ("[]", 0, ""),                      # JSON, but not an object
])
def test_an_unreadable_answer_is_unknown_never_false(monkeypatch, stdout, rc, stderr):
    """Guessing "signed out" from output we could not read is how a signed-in
    user gets told to go and sign in."""
    _auth_says(monkeypatch, stdout, returncode=rc, stderr=stderr)
    assert claude_health.signed_in("/x/claude") is None


def test_a_hung_or_missing_cli_is_unknown(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(claude_health, "_auth_status", _REAL_AUTH_STATUS)
    monkeypatch.setattr(claude_health.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired("claude", 1)))
    assert claude_health.signed_in("/x/claude") is None
    monkeypatch.setattr(claude_health.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
    assert claude_health.signed_in("/x/claude") is None


def test_signed_in_from_a_credentials_file_when_the_cli_cannot_be_asked(tmp_path, monkeypatch):
    cfg = tmp_path / "claude"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    assert claude_health.signed_in() is True


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"])
def test_signed_in_from_an_env_token(monkeypatch, name):
    monkeypatch.setenv(name, "sk-whatever")
    assert claude_health.signed_in() is True


def test_a_blank_env_token_is_not_a_credential(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert claude_health.signed_in() is not True


def test_absent_local_evidence_is_never_false_on_any_platform(monkeypatch):
    """THE REGRESSION THIS REPLACED A PLATFORM RULE TO PREVENT.

    This used to conclude "no credentials file on Linux/Windows, therefore
    signed out". Measured on a container that is demonstrably logged in
    (`claude auth status` → loggedIn: true) with no credentials file and no
    token in the environment, that rule answered False: the credential arrived
    on an inherited file descriptor. Absence of a file is not evidence of being
    signed out, on any platform — only the CLI's own answer is.
    """
    for platform in ("linux", "win32", "darwin"):
        monkeypatch.setattr(claude_health.os, "name",
                            "nt" if platform == "win32" else "posix")
        assert claude_health.signed_in() is None, platform


def test_a_missing_binary_is_not_asked_for_its_auth_state(monkeypatch):
    """Nothing to spawn, so nothing to ask — and the answer stays unknown
    rather than becoming a False nobody established."""
    spawned = []
    monkeypatch.setattr(claude_health.subprocess, "run",
                        lambda *a, **k: spawned.append(a) or None)
    monkeypatch.setattr(claude_health, "resolve", lambda **k: (None, None))
    snap = claude_health._measure()
    assert snap["found"] is False
    assert snap["signed_in"] is None
    assert spawned == []


def test_config_dir_prefers_claude_code_s_own_variable(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/a")
    monkeypatch.setenv("CLAUDE_DIR", "/b")
    assert claude_health.config_dir() == "/a"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert claude_health.config_dir() == "/b"


# -- the cached snapshot ------------------------------------------------------


def test_snapshot_is_cached_then_served_from_disk(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    calls = []
    monkeypatch.setattr(claude_health, "probe_version",
                        lambda p: calls.append(p) or "2.1.220")

    first = claude_health.snapshot()
    assert first["found"] is True and first["version"] == "2.1.220"
    assert os.path.isfile(claude_health._cache_path())

    second = claude_health.snapshot()
    assert second["version"] == "2.1.220"
    assert len(calls) == 1, "a warm cache must not re-probe"


def test_an_upgraded_binary_invalidates_the_cache(tmp_path, monkeypatch):
    """`claude update` rewrites the file in place, so mtime is how an upgrade
    announces itself — without this the cache would keep reporting the old
    version (and a stale `outdated`) forever."""
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    versions = iter(["2.0.1", "2.1.220"])
    monkeypatch.setattr(claude_health, "probe_version", lambda p: next(versions))

    assert claude_health.snapshot()["version"] == "2.0.1"
    os.utime(bin_path, (1, 1))  # an in-place upgrade
    assert claude_health.snapshot()["version"] == "2.1.220"


def test_a_new_override_invalidates_the_cache(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    monkeypatch.setattr(claude_health, "probe_version", lambda p: "2.1.220")
    assert claude_health.snapshot()["source"] == "path"
    monkeypatch.setenv(claude_health.BIN_ENV, "/opt/custom/claude")
    assert claude_health.snapshot()["source"] == "override"


def test_refresh_re_probes_even_on_a_valid_cache(tmp_path, monkeypatch):
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    calls = []
    monkeypatch.setattr(claude_health, "probe_version",
                        lambda p: calls.append(p) or "2.1.220")
    claude_health.snapshot()
    claude_health.snapshot(refresh=True)
    assert len(calls) == 2


def test_refresh_answers_with_the_measurement_it_just_took(tmp_path, monkeypatch):
    """"Check again" must never answer with the snapshot it was pressed to get
    past.

    An unwritable home is tolerated by design, so a refresh that re-read through
    the cache would serve the STALE file — the user installs Claude Code, presses
    the button, and is told again that it is missing (Bugbot #621).
    """
    bin_path = _fake_cli(tmp_path)
    versions = iter(["1.0.88", "2.1.220"])
    monkeypatch.setattr(claude_health, "probe_version", lambda p: next(versions))

    # First measurement: nothing on PATH, an old CLI. This one lands in the cache.
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    assert claude_health.summary()["version"] == "1.0.88"

    # Now the cache cannot be updated — and the refresh must still answer fresh.
    monkeypatch.setattr(claude_health.storage, "write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    refreshed = claude_health.summary_refreshed()
    assert refreshed["version"] == "2.1.220"
    assert refreshed["outdated"] is False
    # and it is still the endpoint's shape, not the internal one
    assert "fingerprint" not in refreshed


def test_refresh_does_not_probe_twice(tmp_path, monkeypatch):
    """The old form measured, then re-read through summary() — which probed a
    second time whenever there was no cache to read."""
    bin_path = _fake_cli(tmp_path)
    monkeypatch.setenv("PATH", os.path.dirname(bin_path))
    calls = []
    monkeypatch.setattr(claude_health, "probe_version",
                        lambda p: calls.append(p) or "2.1.220")
    claude_health.summary_refreshed()
    assert len(calls) == 1


def test_a_corrupt_cache_is_re_measured_not_raised(tmp_path, monkeypatch):
    """A cache is disposable and entirely re-derivable, so a damaged one has
    nothing to recover and nothing to report. (User DATA is the opposite case
    and is not what lives in this file.)"""
    monkeypatch.setattr(claude_health, "probe_version", lambda p: None)
    os.makedirs(os.path.dirname(claude_health._cache_path()), exist_ok=True)
    with open(claude_health._cache_path(), "w") as f:
        f.write("{ this is not json")
    assert claude_health.snapshot()["found"] is False


def test_an_unwritable_home_still_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_health.storage, "write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(claude_health, "probe_version", lambda p: None)
    assert "found" in claude_health.snapshot()


def test_summary_withholds_the_fingerprint(tmp_path, monkeypatch):
    """It is cache bookkeeping, and it carries the machine's whole PATH — which
    has no business in a browser."""
    monkeypatch.setattr(claude_health, "probe_version", lambda p: None)
    summary = claude_health.summary()
    assert "fingerprint" not in summary
    assert "found" in summary and "min_version" in summary
    # and it must survive a JSON round trip, being an HTTP payload
    assert json.loads(json.dumps(summary))["min_version"] == claude_health.MIN_VERSION


def test_warm_in_background_never_raises(monkeypatch):
    monkeypatch.setattr(claude_health, "snapshot",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    claude_health.warm_in_background()  # must not propagate


# -- the endpoint -------------------------------------------------------------


def _client():
    from starlette.testclient import TestClient

    from fused_render.server.routers.claude_health import router
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_endpoint_answers_the_snapshot(monkeypatch):
    monkeypatch.setattr(claude_health, "summary",
                        lambda: {"found": True, "version": "2.1.220"})
    body = _client().get("/api/claude/health").json()
    assert body == {"found": True, "version": "2.1.220"}


def test_refresh_requires_the_fused_header(monkeypatch):
    called = []
    monkeypatch.setattr(claude_health, "summary_refreshed",
                        lambda: called.append(1) or {"found": False})
    client = _client()
    assert client.post("/api/claude/health/refresh").status_code != 200
    assert called == []
    ok = client.post("/api/claude/health/refresh", headers={"X-Fused": "1"})
    assert ok.status_code == 200 and called == [1]


def test_the_health_endpoint_is_not_on_api_config():
    """/api/config is read on every page load and by the status-banner poll;
    these facts are backed by process spawns and must stay off it."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "fused_render",
                            "server", "routers", "config.py")).read()
    assert "claude_health" not in src
