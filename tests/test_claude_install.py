"""Tests for fused_render/claude_install.py and the three repair endpoints.

This module RUNS THINGS, which is what separates it from claude_health: a wrong
answer there is bad advice, a wrong answer here spawns a process that writes an
executable into the user's home. So the assertions cluster around the refusals —
what it will not do, and whether it says why in words the strip can show.

Nothing below spawns a real installer. `subprocess.Popen` is faked at the module
boundary and the health re-probe is stubbed, so what is under test is the state
machine and the guards, not the network.
"""
import subprocess
import time

import pytest

from fused_render import claude_health, claude_install


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """The record is module state shared by a whole suite, and the job registry
    outlives a test the same way."""
    claude_install.reset()
    monkeypatch.delenv("DISABLE_UPDATES", raising=False)
    # Reporting is best-effort in production and irrelevant here; stubbing it
    # keeps a test from depending on the registry's own sweeping rules.
    monkeypatch.setattr(claude_install.jobs, "upsert", lambda *a, **k: {})
    yield
    claude_install.reset()


class _FakeProc:
    """A child that prints `lines` and exits `code`."""

    def __init__(self, lines, code=0):
        self.stdout = iter(lines)
        self._code = code
        self.killed = False

    def wait(self, timeout=None):
        return self._code

    def kill(self):
        self.killed = True


def _run_install(monkeypatch, lines, code=0, health=None):
    """Drive one install to completion synchronously, returning the record."""
    monkeypatch.setattr(claude_install.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(lines, code))
    monkeypatch.setattr(claude_health, "summary_refreshed",
                        lambda: health if health is not None
                        else {"found": True, "version": "2.1.246"})
    claude_install._run("install", ["bash", "-c", "true"],
                        claude_health.INSTALL_COMMAND_POSIX)
    return claude_install.status()


# -- what actually runs -------------------------------------------------------


def test_the_install_argv_matches_the_platform_and_the_line_we_show(monkeypatch):
    """What the user is told will run and what runs have to be the same
    sentence, or the disclosure beside the button is decoration."""
    monkeypatch.setattr(claude_install.os, "name", "posix")
    argv, display = claude_install.install_argv()
    assert display == claude_health.INSTALL_COMMAND_POSIX
    assert argv[:2] == ["bash", "-c"]
    assert "claude.ai/install.sh" in argv[2]

    monkeypatch.setattr(claude_install.os, "name", "nt")
    argv, display = claude_install.install_argv()
    assert display == claude_health.INSTALL_COMMAND_WINDOWS
    assert argv[0] == "powershell"
    # -NoProfile, so a user's PowerShell profile cannot change what this does.
    assert "-NoProfile" in argv
    assert "claude.ai/install.ps1" in argv[-1]


def test_update_runs_the_binary_we_resolved_not_whatever_is_on_path():
    """On a machine with two installs, `claude` on the PATH and the one the app
    spawns are not always the same file — updating the other one would leave the
    app on the old version reporting success."""
    argv, display = claude_install.update_argv("/opt/two/claude")
    assert argv == ["/opt/two/claude", "update"]
    assert display == "claude update"


# -- the refusals -------------------------------------------------------------


def test_a_second_install_is_refused_rather_than_queued(monkeypatch):
    monkeypatch.setattr(claude_install.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda self: None})())
    claude_install.start("install")
    with pytest.raises(claude_install.InstallError, match="already running"):
        claude_install.start("install")


def test_an_unknown_action_is_refused():
    with pytest.raises(claude_install.InstallError, match="must be one of"):
        claude_install.start("uninstall")


def test_update_is_refused_when_there_is_nothing_to_update(monkeypatch):
    monkeypatch.setattr(claude_health, "resolve", lambda allow_shell=True: (None, None))
    with pytest.raises(claude_install.InstallError, match="install it first"):
        claude_install.start("update")


def test_update_is_refused_on_a_homebrew_install_and_names_the_real_command(monkeypatch):
    """THE GUARD THE FEATURE EXISTS FOR. `claude update` answers "Claude is up to
    date!" on a managed install and changes nothing, so running it would spend a
    minute to tell the user their old CLI is current.

    The refusal text is the payload: it names the command that WOULD work, and
    the strip shows it verbatim."""
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: ("/opt/homebrew/bin/claude", "candidate"))
    monkeypatch.setattr(claude_health, "executable", lambda p: True)
    with pytest.raises(claude_install.InstallError) as e:
        claude_install.start("update")
    assert "would not change anything" in str(e.value)
    assert "brew upgrade claude-code" in str(e.value)


def test_update_is_refused_when_updates_are_switched_off(monkeypatch):
    monkeypatch.setenv("DISABLE_UPDATES", "1")
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: ("/home/u/.local/bin/claude", "candidate"))
    monkeypatch.setattr(claude_health, "executable", lambda p: True)
    with pytest.raises(claude_install.InstallError, match="DISABLE_UPDATES"):
        claude_install.start("update")


def test_update_runs_on_a_native_install(monkeypatch):
    started = []
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: ("/home/u/.local/bin/claude", "candidate"))
    monkeypatch.setattr(claude_health, "executable", lambda p: True)
    monkeypatch.setattr(claude_install.threading, "Thread",
                        lambda **kw: type("T", (), {
                            "start": lambda self: started.append(kw["args"])})())
    record = claude_install.start("update")
    assert record["state"] == "running"
    assert record["command"] == "claude update"
    assert started[0][1] == ["/home/u/.local/bin/claude", "update"]


def test_update_never_sources_the_login_shell(monkeypatch):
    """`resolve(allow_shell=False)`: this runs on a button press, and sourcing
    the user's whole profile would add seconds to it. Anything the shell probe
    would find was already adopted into the override by the measure that got us
    here."""
    seen = {}

    def _resolve(allow_shell=True):
        seen["allow_shell"] = allow_shell
        return None, None

    monkeypatch.setattr(claude_health, "resolve", _resolve)
    with pytest.raises(claude_install.InstallError):
        claude_install.start("update")
    assert seen["allow_shell"] is False


# -- the run itself -----------------------------------------------------------


def test_a_clean_install_finishes_and_keeps_the_output(monkeypatch):
    rec = _run_install(monkeypatch, ["downloading…\n", "installed 2.1.246\n"])
    assert rec["state"] == "done"
    assert "installed 2.1.246" in rec["output"]
    assert rec["error"] is None


def test_a_failing_install_keeps_the_child_s_own_words(monkeypatch):
    """VERBATIM, and this is the whole reason the output is surfaced at all: a
    403 from downloads.claude.ai and a proxy eating the TLS handshake are
    different documented problems with different documented fixes, and a
    reworded "install failed" throws both away."""
    rec = _run_install(
        monkeypatch,
        ["curl: (22) The requested URL returned error: 403\n"],
        code=1,
    )
    assert rec["state"] == "error"
    assert "403" in rec["output"]
    assert "exited with code 1" in rec["error"]


def test_an_install_that_leaves_nothing_runnable_is_a_failure(monkeypatch):
    """However happy its exit code was. Reporting success and letting the strip
    re-render the same "can't find Claude Code" card would be the app telling
    the user two contradictory things in the same second."""
    rec = _run_install(monkeypatch, ["all done\n"], code=0,
                       health={"found": False, "version": None})
    assert rec["state"] == "error"
    assert "still cannot be found" in rec["error"]


def test_a_re_probe_that_itself_fails_does_not_fail_the_install(monkeypatch):
    """A probe that raised is not evidence the install went wrong."""
    monkeypatch.setattr(claude_install.subprocess, "Popen",
                        lambda *a, **k: _FakeProc(["ok\n"], 0))

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(claude_health, "summary_refreshed", _boom)
    claude_install._run("install", ["bash", "-c", "true"], "…")
    assert claude_install.status()["state"] == "done"


def test_output_is_capped_to_a_tail(monkeypatch):
    """The tail carries the error; an unbounded buffer carries a memory leak on
    a chatty installer."""
    rec = _run_install(monkeypatch, [f"line {i}\n" for i in range(500)])
    assert len(rec["output"].splitlines()) == claude_install._OUTPUT_TAIL_LINES
    assert rec["output"].splitlines()[-1] == "line 499"


def test_a_child_that_will_not_start_is_reported_not_raised(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no bash on this machine")

    monkeypatch.setattr(claude_install.subprocess, "Popen", _boom)
    claude_install._run("install", ["bash", "-c", "true"], "…")
    rec = claude_install.status()
    assert rec["state"] == "error"
    assert "no bash" in rec["error"]


def test_a_run_that_overruns_is_killed_and_says_so(monkeypatch):
    monkeypatch.setattr(claude_install, "TIMEOUT_S", 0)
    proc = _FakeProc(["still going\n", "and going\n"])
    monkeypatch.setattr(claude_install.subprocess, "Popen", lambda *a, **k: proc)
    claude_install._run("install", ["bash", "-c", "true"], "…")
    rec = claude_install.status()
    assert rec["state"] == "error"
    assert "was stopped" in rec["error"]
    assert proc.killed is True


def test_a_wait_that_times_out_is_caught(monkeypatch):
    class _Hanging(_FakeProc):
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("claude", timeout or 1)

    proc = _Hanging([])
    monkeypatch.setattr(claude_install.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(claude_health, "summary_refreshed", lambda: {"found": True})
    claude_install._run("install", ["bash", "-c", "true"], "…")
    assert claude_install.status()["state"] == "error"
    assert proc.killed is True


# -- the endpoints ------------------------------------------------------------


def _client():
    """Just this router on a bare app — the same idiom test_claude_health.py
    uses. Building the whole server would drag in a start dir, the template
    registry and the shell build for three endpoints that touch none of them."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.claude_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_install_endpoint_refuses_a_blind_cross_origin_post():
    """It spawns a process that downloads and writes an executable into the
    user's home — the last thing an unguarded POST may start."""
    resp = _client().post("/api/claude/install", json={"action": "install"})
    assert resp.status_code in (400, 403)


def test_the_status_endpoint_is_a_read_and_needs_no_guard():
    resp = _client().get("/api/claude/install")
    assert resp.status_code == 200
    assert resp.json()["state"] == "idle"


def test_a_refused_update_comes_back_as_409_with_the_reason(monkeypatch):
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: ("/opt/homebrew/bin/claude", "candidate"))
    monkeypatch.setattr(claude_health, "executable", lambda p: True)
    resp = _client().post("/api/claude/install", json={"action": "update"},
                          headers={"X-Fused": "1"})
    assert resp.status_code == 409
    assert "brew upgrade claude-code" in resp.text


def test_doctor_endpoint_reports_a_cli_that_will_not_diagnose_itself(monkeypatch):
    """Two probes have now failed to get a word out of this binary. That is the
    finding, and it is reported as one rather than dressed up as a diagnosis."""
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: ("/opt/x/claude", "candidate"))
    monkeypatch.setattr(claude_health, "executable", lambda p: True)
    monkeypatch.setattr(claude_health, "_doctor", lambda p: None)
    resp = _client().post("/api/claude/doctor", headers={"X-Fused": "1"})
    body = resp.json()
    assert body["ok"] is False
    assert "would not run its own diagnostics" in body["error"]


def test_doctor_endpoint_returns_the_parsed_report(monkeypatch):
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: ("/opt/x/claude", "candidate"))
    monkeypatch.setattr(claude_health, "executable", lambda p: True)
    monkeypatch.setattr(claude_health, "_doctor", lambda p: claude_health.parse_doctor(
        "Running: native (2.1.246)\n\n1 warnings found\n"
        "- Leftover npm global installation at /opt/n/claude\n"
        "  Fix: Run: npm -g uninstall @anthropic-ai/claude-code\n"))
    body = _client().post("/api/claude/doctor", headers={"X-Fused": "1"}).json()
    assert body["ok"] is True
    assert body["doctor"]["install_method"] == "native"
    assert body["doctor"]["warnings"][0]["fix"].startswith("Run: npm")
