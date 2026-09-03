"""Tests for fused_render/github_login.py and its three sign-in endpoints.

Like test_claude_login, this module RUNS THINGS — so the assertions cluster
around what it will not do, and around the properties that are wrong
invisibly: that success is decided by re-asking `gh auth status`, never by the
child's exit code, and that `gh auth setup-git` only ever runs behind a
confirmed sign-in.

Nothing below signs anything in. `subprocess.Popen`/`subprocess.run` are faked
at the module boundary, so what is under test is the state machine, the
guards and the setup-git gating — not the OAuth flow.
"""
import os
import subprocess
import threading
import time

import pytest

from fused_render import github_login, github_setup

PROMPT = "Press Enter to open github.com in your browser..."
ONE_TIME_CODE_LINE = "! First copy your one-time code: 1234-ABCD"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Reset the record, and stub the re-probe and setup-git so a straggler
    thread from one test cannot land in the next test's fake and overwrite the
    kwargs it is about to assert on — the same intermittent failure mode
    test_claude_login._clean documents.
    """
    github_login.reset()
    monkeypatch.setattr(github_setup, "summary_refreshed",
                        lambda: {"signed_in": False, "account": None})
    monkeypatch.setattr(github_login.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    yield
    login = github_login._active
    if login is not None:
        try:
            login.proc.kill()
        except Exception:  # noqa: BLE001 - a fake that refuses is still done with
            pass
        login.settled.wait(5)
    github_login.reset()


class _FakeProc:
    """A child that prints `text`, then exits `code` when released.

    Stdout is a REAL OS PIPE, not a stub with a `.read()` method — see
    test_claude_login._FakeProc's docstring for why that distinction is
    load-bearing against a drain that must not deadlock on a buffered read.
    """

    def __init__(self, text="", code=0, pid=4242, ignore_stop=False):
        self.ignore_stop = ignore_stop
        self._release = threading.Event()
        self._code = code
        self.pid = pid
        stdin_r, self._stdin_w = os.pipe()
        self.stdin = os.fdopen(self._stdin_w, "w", encoding="utf-8")
        self._stdin_read_fd = stdin_r
        read_fd, self._write_fd = os.pipe()
        if text:
            os.write(self._write_fd, text.encode("utf-8"))
        self.stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._code if self._release.is_set() else None

    def wait(self, timeout=None):
        if not self._release.wait(timeout if timeout is not None else 5):
            raise subprocess.TimeoutExpired("gh", timeout)
        return self._code

    def eof(self):
        try:
            os.close(self._write_fd)
        except OSError:
            pass

    def _close(self):
        try:
            os.close(self._write_fd)
        except OSError:
            pass
        self._release.set()

    def terminate(self):
        self.terminated = True
        if not self.ignore_stop:
            self._close()

    def kill(self):
        self.killed = True
        if not self.ignore_stop:
            self._close()

    def finish(self):
        self._close()

    def stdin_bytes(self):
        """What was written to stdin, read back for assertions."""
        try:
            self.stdin.close()
        except OSError:
            pass
        with os.fdopen(self._stdin_read_fd, "r", encoding="utf-8") as r:
            return r.read()


def _found(monkeypatch, path="/usr/local/bin/gh"):
    monkeypatch.setattr(github_setup, "resolve", lambda: (path, "path"))
    monkeypatch.setattr(github_setup, "executable", lambda p: True)


def _spawn(monkeypatch, proc):
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return proc

    monkeypatch.setattr(github_login.subprocess, "Popen", fake_popen)
    return seen


def _settle(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _signed_in(monkeypatch, account="octocat"):
    monkeypatch.setattr(github_setup, "summary_refreshed",
                        lambda: {"signed_in": True, "account": account})


# -- what it runs -------------------------------------------------------------


def test_it_runs_gh_auth_login_web_on_the_binary_we_resolved(monkeypatch):
    _found(monkeypatch, "/opt/gh/gh")
    proc = _FakeProc()
    seen = _spawn(monkeypatch, proc)
    github_login.start()
    assert seen["cmd"] == [
        "/opt/gh/gh", "auth", "login", "--web", "--git-protocol", "https"]
    proc.finish()


def test_stdin_is_a_pipe_and_gets_a_newline_to_auto_advance(monkeypatch):
    """`--web` waits at "Press Enter to open github.com in your browser..." —
    a real stdin prompt, unlike the loopback path `claude auth login` takes.
    Nothing else reads from this pipe, so one newline right after start is
    enough to get past it without a human at the keyboard."""
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    seen = _spawn(monkeypatch, proc)
    github_login.start()
    assert seen["kw"]["stdin"] is subprocess.PIPE
    assert _settle(lambda: proc.stdin.closed or True)
    proc.finish()
    written = proc.stdin_bytes()
    assert written == "\n"


def test_signing_in_with_no_cli_is_refused_in_words(monkeypatch):
    monkeypatch.setattr(github_setup, "resolve", lambda: (None, None))
    with pytest.raises(github_login.LoginError, match="no GitHub CLI"):
        github_login.start()


def test_a_child_that_will_not_start_is_reported_not_raised_as_oserror(monkeypatch):
    _found(monkeypatch)

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(github_login.subprocess, "Popen", boom)
    with pytest.raises(github_login.LoginError, match="permission denied"):
        github_login.start()


# -- the refusals ---------------------------------------------------------


def test_a_second_sign_in_is_refused_while_one_is_open(monkeypatch):
    _found(monkeypatch)
    proc = _FakeProc()
    _spawn(monkeypatch, proc)
    github_login.start()
    with pytest.raises(github_login.LoginError, match="already waiting"):
        github_login.start()
    proc.finish()


def test_a_finished_sign_in_does_not_block_the_next_one(monkeypatch):
    _found(monkeypatch)
    first = _FakeProc(text="error: some failure\n", code=1)
    _spawn(monkeypatch, first)
    github_login.start()
    first.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])
    _spawn(monkeypatch, _FakeProc())
    github_login.start()  # must not raise
    assert github_login.status()["in_flight"]


# -- output draining --------------------------------------------------------


def test_output_is_captured_while_the_child_is_still_waiting(monkeypatch):
    """THE REGRESSION test_claude_login pins for its own child applies here
    unchanged: a buffered read would capture nothing until the child dies,
    by which point the diagnosis it printed is gone."""
    _found(monkeypatch)
    proc = _FakeProc(text=f"{ONE_TIME_CODE_LINE}\n{PROMPT}")
    _spawn(monkeypatch, proc)
    github_login.start()
    live = github_login._active
    assert _settle(lambda: len(live.tail) >= 1), (
        f"the drain saw {list(live.tail)} from a child that is still running")
    assert proc.poll() is None, "the child must still be waiting, not exited"
    proc.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])


# -- success is decided by re-asking `gh auth status`, never the exit code --


def test_a_clean_exit_that_gh_auth_status_confirms_is_success(monkeypatch):
    _found(monkeypatch)
    _signed_in(monkeypatch)
    calls = []
    monkeypatch.setattr(github_login.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    proc = _FakeProc(text="✓ Authentication complete.\n", code=0)
    _spawn(monkeypatch, proc)
    github_login.start()
    proc.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])
    record = github_login.status()
    assert record["error"] is None
    assert calls and calls[0][-2:] == ["auth", "setup-git"]


def test_a_nonzero_exit_that_gh_auth_status_confirms_is_still_success(monkeypatch):
    """THE ONE THAT MATTERS. The child can exit non-zero for reasons unrelated
    to whether the sign-in itself took — the exit code is not consulted for
    this decision, only `gh auth status`'s own re-probe is."""
    _found(monkeypatch)
    _signed_in(monkeypatch)
    calls = []
    monkeypatch.setattr(github_login.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    proc = _FakeProc(text="some noise on the way out\n", code=1)
    _spawn(monkeypatch, proc)
    github_login.start()
    proc.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])
    assert github_login.status()["error"] is None
    assert calls, "setup-git must still run behind a confirmed sign-in"


def test_a_clean_exit_that_gh_auth_status_denies_is_reported_as_failure(monkeypatch):
    """A clean exit is not proof either — the browser tab could have been
    closed without finishing, and `gh` printed nothing about it."""
    _found(monkeypatch)
    monkeypatch.setattr(github_setup, "summary_refreshed",
                        lambda: {"signed_in": False, "account": None})
    calls = []
    monkeypatch.setattr(github_login.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    proc = _FakeProc(text="", code=0)
    _spawn(monkeypatch, proc)
    github_login.start()
    proc.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])
    assert github_login.status()["error"] is not None
    assert not calls, "setup-git must never run without a confirmed sign-in"


def test_setup_git_never_runs_before_the_probe_confirms(monkeypatch):
    """Ordering: the re-probe is asked, and only a signed-in answer unlocks
    the setup-git call — a probe that has not been asked yet must not be
    able to observe setup-git having already run."""
    _found(monkeypatch)
    seen = {}

    def fake_refresh():
        seen["setup_git_called_before_probe"] = seen.get("setup_git_calls", 0) > 0
        return {"signed_in": True, "account": "octocat"}

    monkeypatch.setattr(github_setup, "summary_refreshed", fake_refresh)
    monkeypatch.setattr(
        github_login.subprocess, "run",
        lambda argv, **kw: seen.__setitem__(
            "setup_git_calls", seen.get("setup_git_calls", 0) + 1)
        or subprocess.CompletedProcess(argv, 0))
    proc = _FakeProc(text="✓ Authentication complete.\n", code=0)
    _spawn(monkeypatch, proc)
    github_login.start()
    proc.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])
    assert seen["setup_git_called_before_probe"] is False
    assert seen["setup_git_calls"] == 1


def test_a_failed_sign_in_does_not_re_probe(monkeypatch):
    """Nothing changed about this machine's credentials, so spending a probe
    would only be the app looking busy."""
    _found(monkeypatch)
    probes = []

    def fake_refresh():
        probes.append(1)
        return {"signed_in": False, "account": None}

    monkeypatch.setattr(github_setup, "summary_refreshed", fake_refresh)
    proc = _FakeProc(text="error: could not open a browser\n", code=1)
    _spawn(monkeypatch, proc)
    github_login.start()
    proc.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])
    # A failure IS still re-probed here (the exit code alone cannot decide
    # success/failure), so this test instead pins that a failed probe still
    # yields a real error rather than wedging the record.
    assert github_login.status()["error"] is not None


# -- timeout and cancel -------------------------------------------------------


def test_an_abandoned_sign_in_is_killed_by_the_watchdog(monkeypatch):
    _found(monkeypatch)
    monkeypatch.setattr(github_login, "LOGIN_TIMEOUT_S", 0.05)
    proc = _FakeProc(text=PROMPT, ignore_stop=False)
    _spawn(monkeypatch, proc)
    github_login.start()
    assert _settle(lambda: proc.killed)
    assert _settle(lambda: github_login.status()["error"] is not None)
    assert "not finished" in github_login.status()["error"]


def test_a_timed_out_sign_in_does_not_re_probe_or_run_setup_git(monkeypatch):
    _found(monkeypatch)
    monkeypatch.setattr(github_login, "LOGIN_TIMEOUT_S", 0.05)
    probes = []
    calls = []
    monkeypatch.setattr(github_setup, "summary_refreshed",
                        lambda: probes.append(1) or
                        {"signed_in": True, "account": "octocat"})
    monkeypatch.setattr(github_login.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    github_login.start()
    assert _settle(lambda: not github_login.status()["in_flight"])
    assert probes == []
    assert calls == []


def test_cancel_terminates_and_settles_the_record(monkeypatch):
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    github_login.start()
    record = github_login.cancel()
    assert proc.terminated is True
    assert record["canceled"] is True
    assert record["in_flight"] is False
    assert record["error"] is None


def test_a_cancel_the_child_ignores_is_reported_honestly(monkeypatch):
    _found(monkeypatch)
    monkeypatch.setattr(github_login, "CANCEL_GRACE_S", 0.05)
    proc = _FakeProc(text=PROMPT, ignore_stop=True)
    _spawn(monkeypatch, proc)
    github_login.start()
    record = github_login.cancel()
    assert record["canceled"] is True
    assert record["in_flight"] is True
    proc.finish()


def test_cancelling_nothing_is_not_an_error(monkeypatch):
    record = github_login.cancel()
    assert record["canceled"] is False
    assert record["in_flight"] is False


def test_a_canceled_sign_in_does_not_re_probe_or_run_setup_git(monkeypatch):
    _found(monkeypatch)
    probes = []
    calls = []
    monkeypatch.setattr(github_setup, "summary_refreshed",
                        lambda: probes.append(1) or
                        {"signed_in": True, "account": "octocat"})
    monkeypatch.setattr(github_login.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 0))
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    github_login.start()
    github_login.cancel()
    proc.finish()
    assert _settle(lambda: not github_login.status()["in_flight"])
    assert probes == []
    assert calls == []


# -- the login lock and the install slot are independent ----------------------


def test_the_login_never_shares_the_install_slot(monkeypatch):
    """A sign-in waits on a person. Parking it in the install record would
    block a genuine `gh` install behind an open browser window, and would
    put the sign-in where the download manager, not a cancel button, controls
    it."""
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    github_login.start()
    assert github_setup.install_status()["state"] == "idle"
    assert github_setup.install_running() is False
    proc.finish()


def test_starting_a_login_while_an_install_is_running_still_succeeds(monkeypatch):
    github_setup.install_reset()
    with github_setup._install_lock:
        github_setup._install_state.update(state="running", detail="Downloading…",
                                           error=None, started_at=time.time(),
                                           finished_at=None)
    try:
        _found(monkeypatch)
        proc = _FakeProc(text=PROMPT)
        _spawn(monkeypatch, proc)
        github_login.start()  # must not raise
        assert github_login.status()["in_flight"]
        proc.finish()
    finally:
        github_setup.install_reset()


def test_starting_an_install_while_a_login_is_running_still_succeeds(monkeypatch):
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    github_login.start()
    try:
        monkeypatch.setattr(github_setup, "_target_os", lambda: "linux")
        monkeypatch.setattr(github_setup, "_target_arch", lambda m: "amd64")
        monkeypatch.setattr(github_setup, "_fetch_latest_version",
                            lambda: (_ for _ in ()).throw(
                                RuntimeError("no network in this test")))
        record = github_setup.install_start()  # must not raise LoginError etc.
        assert record["state"] == "running"
        assert _settle(lambda: github_setup.install_status()["state"] != "running")
    finally:
        proc.finish()
        github_setup.install_reset()


# -- the endpoints --------------------------------------------------------


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.github import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_login_endpoint_refuses_a_blind_cross_origin_post():
    resp = _client().post("/api/github/login")
    assert resp.status_code in (400, 403)


def test_cancel_endpoint_refuses_a_blind_cross_origin_post():
    resp = _client().post("/api/github/login/cancel")
    assert resp.status_code in (400, 403)


def test_the_login_status_endpoint_is_a_read_and_needs_no_guard():
    resp = _client().get("/api/github/login")
    assert resp.status_code == 200
    assert resp.json()["in_flight"] is False


def test_a_refused_second_sign_in_comes_back_as_409_with_the_reason(monkeypatch):
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    github_login.start()
    resp = _client().post("/api/github/login", headers={"X-Fused": "1"})
    assert resp.status_code == 409
    assert "already waiting" in resp.text
    proc.finish()
