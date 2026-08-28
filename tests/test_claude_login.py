"""Tests for fused_render/claude_login.py and the three sign-in endpoints.

Like test_claude_install, this module RUNS THINGS — so the assertions cluster
around what it will not do, and around the two properties that are wrong
invisibly: that a failed sign-in keeps the child's own diagnosis, and that
nothing the child printed is ever written down.

Nothing below signs anything in. `subprocess.Popen` is faked at the module
boundary, so what is under test is the state machine, the guards and the output
discipline — not the OAuth flow.
"""
import os
import subprocess
import threading
import time

import pytest

from fused_render import claude_health, claude_login

# The authorize line the real CLI prints, trimmed. It is here because it is the
# thing that must NOT come back out of this module: it carries the `state` nonce
# and the PKCE challenge, and it is also the bulk of a healthy child's output.
AUTHORIZE_LINE = (
    "If the browser didn't open, visit: https://claude.com/cai/oauth/authorize"
    "?code=true&client_id=9d1c250a&state=SECRETSTATE&code_challenge=SECRETPKCE"
)
PROMPT = "Paste code here if prompted > "
_PROMPT = "Paste code here"


@pytest.fixture(autouse=True)
def _clean():
    claude_login.reset()
    yield
    claude_login.reset()


class _FakeProc:
    """A child that prints `text`, then exits `code` when released.

    Its stdout is A REAL OS PIPE, not a stub with a `read` method, and that is
    load-bearing. A stub that hands back the whole string on the first call
    passes against a drain that cannot actually work: `stdout.read(n)` on a
    buffered stream waits for n CHARACTERS, so against the real CLI — which
    prints ~700 and then waits on a human — it returns nothing until the child
    dies. A real pipe reproduces that, so the fake cannot flatter the code.

    The write end stays open until release, so the drain sees a child that has
    spoken and is now waiting, which is the resting state of a healthy sign-in.
    """

    def __init__(self, text="", code=0, pid=4242, ignore_stop=False):
        self.ignore_stop = ignore_stop
        self._release = threading.Event()
        self._code = code
        self.pid = pid
        self.stdin = None
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
            raise subprocess.TimeoutExpired("claude", timeout)
        return self._code

    def eof(self):
        """Close stdout without exiting — a child that said its piece and stayed."""
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


def _found(monkeypatch, path="/usr/local/bin/claude"):
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: (path, "candidate"))
    monkeypatch.setattr(claude_health, "executable", lambda p: True)


def _spawn(monkeypatch, proc):
    seen = {}

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        seen["kw"] = kw
        return proc

    monkeypatch.setattr(claude_login.subprocess, "Popen", fake_popen)
    return seen


def _settle(predicate, timeout=5.0):
    """Wait for the worker thread to reach a state. Threads, not sleeps."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# -- what it runs -------------------------------------------------------------


def test_it_runs_auth_login_on_the_binary_we_resolved(monkeypatch):
    """Not `claude` off PATH: the app's PATH is not the user's, which is the
    whole reason claude_health resolves a path in the first place."""
    _found(monkeypatch, "/opt/claude/claude")
    proc = _FakeProc()
    seen = _spawn(monkeypatch, proc)
    claude_login.start()
    assert seen["cmd"] == ["/opt/claude/claude", "auth", "login"]
    proc.finish()


def test_stdin_is_a_pipe_so_the_paste_fallback_stays_possible(monkeypatch):
    """DEVNULL would close a door the loopback path does not need but the
    headless one does, and buys nothing for it."""
    _found(monkeypatch)
    proc = _FakeProc()
    seen = _spawn(monkeypatch, proc)
    claude_login.start()
    assert seen["kw"]["stdin"] is subprocess.PIPE
    proc.finish()


def test_the_child_gets_the_augmented_path(monkeypatch):
    """The CLI has to find its own runtime, and a GUI-launched app's PATH is as
    short of `node` as it is of `claude`."""
    _found(monkeypatch)
    monkeypatch.setattr(claude_health, "augmented_path", lambda: "/augmented")
    proc = _FakeProc()
    seen = _spawn(monkeypatch, proc)
    claude_login.start()
    assert seen["kw"]["env"]["PATH"] == "/augmented"
    proc.finish()


def test_it_goes_through_the_cmd_hop_behind_a_windows_shim(monkeypatch):
    """npm installs `claude` as a .cmd shim CreateProcess cannot run — and a
    signed-out npm install on Windows is exactly what this button is for."""
    monkeypatch.setattr(claude_health.os, "name", "nt")
    _found(monkeypatch, r"C:\npm\claude.cmd")
    proc = _FakeProc()
    seen = _spawn(monkeypatch, proc)
    claude_login.start()
    assert isinstance(seen["cmd"], str)
    assert seen["kw"]["shell"] is True
    proc.finish()


# -- the refusals -------------------------------------------------------------


def test_a_second_sign_in_is_refused_while_one_is_open(monkeypatch):
    """Two loopback listeners and two authorizations racing. The honest answer
    is that a browser window is already waiting."""
    _found(monkeypatch)
    proc = _FakeProc()
    _spawn(monkeypatch, proc)
    claude_login.start()
    with pytest.raises(claude_login.LoginError, match="already waiting"):
        claude_login.start()
    proc.finish()


def test_a_finished_sign_in_does_not_block_the_next_one(monkeypatch):
    """The refusal is about a LIVE child, not about the record. A user whose
    first attempt failed must be able to press the button again."""
    _found(monkeypatch)
    first = _FakeProc(text="Login failed: nope\n", code=1)
    _spawn(monkeypatch, first)
    claude_login.start()
    first.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    _spawn(monkeypatch, _FakeProc())
    claude_login.start()  # must not raise
    assert claude_login.status()["in_flight"]


def test_signing_in_with_no_cli_is_refused_in_words(monkeypatch):
    monkeypatch.setattr(claude_health, "resolve",
                        lambda allow_shell=True: (None, None))
    with pytest.raises(claude_login.LoginError, match="no Claude Code"):
        claude_login.start()


def test_a_child_that_will_not_start_is_reported_not_raised_as_oserror(monkeypatch):
    _found(monkeypatch)

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(claude_login.subprocess, "Popen", boom)
    with pytest.raises(claude_login.LoginError, match="permission denied"):
        claude_login.start()


# -- how it ends --------------------------------------------------------------


def test_output_is_captured_while_the_child_is_still_waiting(monkeypatch):
    """THE REGRESSION. The drain must deliver lines from a child that has spoken
    and is now blocked on a human — that is the state a sign-in spends all its
    time in. A buffered `stdout.read(n)` waits for n characters and so captures
    nothing until the child dies, by which point the record has already fallen
    back to a generic sentence and the CLI's own diagnosis is gone. Caught
    against the real CLI, which captured zero lines; pinned here.
    """
    _found(monkeypatch)
    proc = _FakeProc(text=f"Opening browser to sign in…\n{AUTHORIZE_LINE}\n{PROMPT}")
    _spawn(monkeypatch, proc)
    claude_login.start()
    live = claude_login._active
    assert _settle(lambda: len(live.tail) >= 2), (
        f"the drain saw {list(live.tail)} from a child that is still running")
    assert proc.poll() is None, "the child must still be waiting, not exited"
    # The trailing paste prompt is deliberately NOT here yet: it carries no
    # newline, so it is held as a fragment and flushed at EOF. Phase 2 is what
    # needs to see it before then, and will read the fragment rather than wait
    # for a newline that never comes.
    assert not any(_PROMPT in line for line in live.tail)
    proc.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    assert any(_PROMPT in line for line in live.tail), (
        "the held fragment must still be flushed once the child is done")


def test_a_failed_sign_in_keeps_the_child_s_own_diagnosis(monkeypatch):
    """`Login failed: Request failed with status code 400` is the loopback
    exchange rejecting the code — the only diagnosis on offer."""
    _found(monkeypatch)
    proc = _FakeProc(
        text=f"Opening browser to sign in…\n{AUTHORIZE_LINE}\n"
             "Login failed: Request failed with status code 400\n",
        code=1,
    )
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: claude_login.status()["error"] is not None)
    assert claude_login.status()["error"] == (
        "Login failed: Request failed with status code 400")


def test_the_authorize_url_never_leaves_the_module(monkeypatch):
    """THE ONE THAT MATTERS. The tail is kept in memory to derive one sentence;
    the line carrying the `state` nonce and the PKCE challenge is not a failure
    and must never be surfaced as one. Excluded by shape, so a future line
    ordering cannot leak it."""
    _found(monkeypatch)
    proc = _FakeProc(text=f"Opening browser to sign in…\n{AUTHORIZE_LINE}\n{PROMPT}",
                     code=1)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: claude_login.status()["error"] is not None)
    record = claude_login.status()
    blob = repr(record)
    assert "SECRETSTATE" not in blob
    assert "SECRETPKCE" not in blob
    assert "://" not in blob
    # Nor is the record a place output is published at all.
    assert "output" not in record


def test_a_failure_printed_onto_the_prompt_line_is_still_found(monkeypatch):
    """THE SHAPE THE REAL CLI PRODUCES. The prompt carries no newline, so the
    failure the loopback exchange reports lands on the SAME line as it. Caught
    live: skipping any line that mentioned the prompt threw the diagnosis away
    with it, and the record blamed `Opening browser to sign in…` instead."""
    _found(monkeypatch)
    proc = _FakeProc(
        text=f"Opening browser to sign in…\n{AUTHORIZE_LINE}\n"
             f"{PROMPT}Login failed: Request failed with status code 400\n",
        code=1,
    )
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    assert claude_login.status()["error"] == (
        "Login failed: Request failed with status code 400")


def test_the_paste_prompt_is_never_mistaken_for_an_error(monkeypatch):
    """It is where a HEALTHY child sits, so it is never why one failed."""
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT, code=1)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: claude_login.status()["error"] is not None)
    assert "Paste code" not in claude_login.status()["error"]
    assert "exit code 1" in claude_login.status()["error"]


def test_a_clean_exit_reports_no_error_and_claims_no_success(monkeypatch):
    """The child exits 0 having written a credential this process never sees.
    Whether it worked is `claude auth status`'s answer, not this record's."""
    _found(monkeypatch)
    proc = _FakeProc(text=f"{AUTHORIZE_LINE}\n", code=0)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    record = claude_login.status()
    assert record["error"] is None
    assert "signed_in" not in record


def test_an_abandoned_sign_in_is_killed_by_the_watchdog(monkeypatch):
    """The child never gives up on its own — with no browser it sits at the
    prompt forever — so the timer is the only thing that ends this."""
    _found(monkeypatch)
    monkeypatch.setattr(claude_login, "LOGIN_TIMEOUT_S", 0.05)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    claude_login.start()
    assert _settle(lambda: proc.killed)
    assert _settle(lambda: claude_login.status()["error"] is not None)
    assert "not finished" in claude_login.status()["error"]


def test_on_windows_the_whole_process_tree_is_stopped(monkeypatch):
    """Behind a .cmd shim the direct child is cmd.exe, and TerminateProcess on
    it leaves the node process holding the loopback listener and our stdout
    pipe — so the drain never sees EOF and the record never settles. Cancel and
    the watchdog would both wedge the button, on exactly the npm-on-Windows
    install this button exists to serve."""
    monkeypatch.setattr(claude_login.os, "name", "nt")
    killed = []
    monkeypatch.setattr(claude_login.subprocess, "run",
                        lambda argv, **kw: killed.append(argv))
    proc = _FakeProc(text=PROMPT, pid=9182)
    claude_login._stop(claude_login._Login(proc=proc, started_at=0.0, tail=[]),
                       force=False)
    assert killed == [["taskkill", "/T", "/F", "/PID", "9182"]]
    # And NOT the plain terminate, which is the thing that does not work here.
    assert proc.terminated is False
    proc.finish()


def test_off_windows_the_child_itself_is_signalled(monkeypatch):
    """`shell=True` never happens on POSIX, so the child IS the CLI and a
    signal reaches it — no taskkill, and terminate stays the polite form."""
    monkeypatch.setattr(claude_login.os, "name", "posix")
    proc = _FakeProc(text=PROMPT)
    login = claude_login._Login(proc=proc, started_at=0.0, tail=[])
    claude_login._stop(login, force=False)
    assert proc.terminated is True and proc.killed is False
    proc2 = _FakeProc(text=PROMPT)
    claude_login._stop(claude_login._Login(proc=proc2, started_at=0.0, tail=[]),
                       force=True)
    assert proc2.killed is True


def test_a_clean_sign_in_re_probes_health_on_the_server(monkeypatch):
    """Leaving the refresh to the strip made it conditional on the strip still
    being mounted: sign in, walk away, come back to a fresh strip reading a
    60-second-old "signed out" and offering a second browser flow."""
    _found(monkeypatch)
    probes = []
    monkeypatch.setattr(claude_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    proc = _FakeProc(text=f"{AUTHORIZE_LINE}\n", code=0)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    assert probes == [1]


def test_the_re_probe_happens_before_the_record_settles(monkeypatch):
    """Ordering, not decoration: anything that can observe `in_flight: false`
    must find the snapshot behind it already re-measured, or the polling client
    reads the stale one in the gap."""
    _found(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        claude_health, "summary_refreshed",
        lambda: seen.setdefault("in_flight_during_probe",
                                claude_login.status()["in_flight"]) or {})
    proc = _FakeProc(text=f"{AUTHORIZE_LINE}\n", code=0)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    assert seen["in_flight_during_probe"] is True


def test_a_failed_sign_in_does_not_re_probe(monkeypatch):
    """Nothing changed about this machine's credentials, so spending a probe
    would only be the app looking busy."""
    _found(monkeypatch)
    probes = []
    monkeypatch.setattr(claude_health, "summary_refreshed",
                        lambda: probes.append(1) or {})
    proc = _FakeProc(text="Login failed: nope\n", code=1)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    assert probes == []


def test_a_re_probe_that_itself_fails_does_not_wedge_the_record(monkeypatch):
    """A failed probe is not a failed sign-in — and must never leave the button
    stuck behind a record that never settles."""
    _found(monkeypatch)

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(claude_health, "summary_refreshed", boom)
    proc = _FakeProc(text=f"{AUTHORIZE_LINE}\n", code=0)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.finish()
    assert _settle(lambda: not claude_login.status()["in_flight"])
    assert claude_login.status()["error"] is None


def test_taskkill_missing_falls_back_to_signalling_the_child(monkeypatch):
    """Killing cmd.exe alone is worth little behind a shim, but it beats not
    trying — and the fallback must not raise out of a cancel."""
    monkeypatch.setattr(claude_login.os, "name", "nt")

    def no_taskkill(*a, **k):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(claude_login.subprocess, "run", no_taskkill)
    proc = _FakeProc(text=PROMPT)
    claude_login._stop(claude_login._Login(proc=proc, started_at=0.0, tail=[]),
                       force=False)
    assert proc.terminated is True
    proc.finish()


def test_a_child_that_will_not_reap_is_not_called_a_login_timeout(monkeypatch):
    """stdout is at EOF but the process record has not gone. It said its piece,
    so this is NOT the ten-minute watchdog and must not be labelled as one —
    the record quotes the child's own last words instead."""
    _found(monkeypatch)
    monkeypatch.setattr(claude_login, "REAP_TIMEOUT_S", 0.05)
    proc = _FakeProc(text="Login failed: nope\n", ignore_stop=True)
    _spawn(monkeypatch, proc)
    claude_login.start()
    proc.eof()  # stdout closes; the child stays
    assert _settle(lambda: not claude_login.status()["in_flight"])
    error = claude_login.status()["error"]
    assert error == "Login failed: nope"
    assert "not finished within" not in error


def test_a_cancel_the_child_ignores_is_reported_honestly(monkeypatch):
    """It says it cancelled, because it asked. It does not claim the child is
    gone when it is still there — the record keeps saying in flight, which is
    the true thing and what keeps the poll alive."""
    _found(monkeypatch)
    monkeypatch.setattr(claude_login, "CANCEL_GRACE_S", 0.05)
    proc = _FakeProc(text=PROMPT, ignore_stop=True)
    _spawn(monkeypatch, proc)
    claude_login.start()
    record = claude_login.cancel()
    assert record["canceled"] is True
    assert record["in_flight"] is True
    proc.finish()


def test_cancel_terminates_and_settles_the_record(monkeypatch):
    """`terminate`, not `kill`: the child owns a loopback socket and is a step
    from a credential store. And the record comes back settled, so the strip
    stops polling a sign-in the user just abandoned."""
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    claude_login.start()
    record = claude_login.cancel()
    assert proc.terminated is True
    assert record["canceled"] is True
    assert record["in_flight"] is False
    # A cancel is not a failure — there is nothing to tell the user about it.
    assert record["error"] is None


def test_cancelling_nothing_is_not_an_error(monkeypatch):
    record = claude_login.cancel()
    assert record["canceled"] is False
    assert record["in_flight"] is False


# -- the endpoints ------------------------------------------------------------


def _client():
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from fused_render.server.routers.claude_health import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_login_endpoint_refuses_a_blind_cross_origin_post():
    """It spawns a process AND opens a browser window on the user's desktop."""
    resp = _client().post("/api/claude/login")
    assert resp.status_code in (400, 403)


def test_cancel_endpoint_refuses_a_blind_cross_origin_post():
    resp = _client().post("/api/claude/login/cancel")
    assert resp.status_code in (400, 403)


def test_the_login_status_endpoint_is_a_read_and_needs_no_guard():
    resp = _client().get("/api/claude/login")
    assert resp.status_code == 200
    assert resp.json()["in_flight"] is False


def test_a_refused_second_sign_in_comes_back_as_409_with_the_reason(monkeypatch):
    """The 409's text is the whole value: the window the user needs is already
    open behind the app."""
    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    claude_login.start()
    resp = _client().post("/api/claude/login", headers={"X-Fused": "1"})
    assert resp.status_code == 409
    assert "already waiting" in resp.text
    proc.finish()


def test_the_login_endpoint_does_not_share_the_install_slot(monkeypatch):
    """A sign-in waits on a person. Parking it in the install record would block
    a genuine install behind an open browser window, and would put the sign-in
    in the download manager where it cannot be cancelled."""
    from fused_render import claude_install

    _found(monkeypatch)
    proc = _FakeProc(text=PROMPT)
    _spawn(monkeypatch, proc)
    claude_login.start()
    assert claude_install.status()["state"] == "idle"
    assert claude_install.running() is False
    proc.finish()
