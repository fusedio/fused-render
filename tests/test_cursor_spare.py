"""Cursor warm spares: `cursor_host.py` in spare mode (spawn the CLI, run
`initialize` and a throwaway `session/new`, write `ready`, wait on stdin for
the real request) and agent.py's side of it (`_prewarm_cursor`,
`_claim_cursor_spare`, `_hand_to_cursor_spare`, the `_defaults` trigger).

Driven against tests/fake_cursor_acp.py, never a real `cursor-agent`. The
suite-wide autouse fixture in conftest.py sets FUSED_CURSOR_PREWARM=0; the
tests that start spares turn it back on for themselves, and every spare they
start is closed (stdin EOF, wait) in a finalizer.
"""
import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time

import pytest

AGENT_DIR = os.path.abspath(os.path.join("fused_render", "claude_agent"))
HOST = os.path.join(AGENT_DIR, "cursor_host.py")
AGENT = os.path.join(AGENT_DIR, "agent.py")
FAKE = os.path.abspath(os.path.join("tests", "fake_cursor_acp.py"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wait_for(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _rows(path):
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _pid_gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def _kill(pid):
    for fn in (os.killpg, os.kill):
        try:
            fn(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    mod = _load("claude_agent_for_cursor_spare_tests", AGENT)
    runs = tmp_path / "runs"
    runs.mkdir()
    mod.RUNS = str(runs)
    return mod


# ------------------------------------------------------------ host, end to end

class SpareHost:
    """A `cursor_host.py` process started in spare mode over a fake CLI."""

    def __init__(self, agent, tmp_path, name="a", fake_args=()):
        self.agent = agent
        self.tmp = tmp_path
        self.cwd = str(tmp_path / ("proj-" + name))
        os.makedirs(self.cwd, exist_ok=True)
        self.spare_dir = str(tmp_path / ("spare-" + name))
        os.mkdir(self.spare_dir, 0o700)
        self.log = os.path.join(self.spare_dir, "fake.log")
        argv = [sys.executable, FAKE, "acp", "--log", self.log] + list(fake_args)
        env = dict(os.environ)
        env.update({"FUSED_CLAUDE_HOST_IDLE_REAP_SECONDS": "5",
                    "FUSED_CLAUDE_HOST_DRAIN_INTERVAL_SECONDS": "0.05",
                    "FUSED_RENDER_PERMISSION_TIMEOUT": "20"})
        self.argv = argv
        self.err = open(os.path.join(self.spare_dir, "host.err.log"), "ab")
        self.proc = subprocess.Popen([sys.executable, HOST], stdin=subprocess.PIPE,
                                     stderr=self.err, env=env)
        self.proc.stdin.write((json.dumps({
            "spare": True, "agent": AGENT, "argv": argv, "cwd": self.cwd,
            "spare_dir": self.spare_dir}) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        self.run_dir = ""

    @property
    def ready(self):
        return os.path.exists(os.path.join(self.spare_dir, "ready"))

    def wait_ready(self):
        assert _wait_for(lambda: self.ready), \
            "spare never became ready\nhost.err: %s" % _read(
                os.path.join(self.spare_dir, "host.err.log"))

    def cli_pid(self):
        try:
            with open(os.path.join(self.spare_dir, "pid")) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError):
            return 0

    def methods(self):
        return [e["msg"]["method"] for e in _rows(self.log)
                if "msg" in e and e["msg"].get("method")]

    def adopt(self, message="hi"):
        """Hand the spare a real request (a run dir like `_start`'s)."""
        agent = self.agent
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
        self.run_dir = os.path.join(agent.RUNS, run_id)
        os.mkdir(self.run_dir, 0o700)
        os.mkdir(os.path.join(self.run_dir, "inbox"), 0o700)
        with open(os.path.join(self.run_dir, "meta.json"), "w") as fh:
            json.dump({"resumed_from": "", "file": self.cwd, "backend": "cursor"}, fh)
        open(os.path.join(self.run_dir, "err.log"), "w").close()
        agent._write_inbox_entry(self.run_dir, message)
        req = {"agent": AGENT, "run_dir": self.run_dir, "file": self.cwd,
               "cwd": self.cwd, "pane": False, "cli_mode": "default",
               "session_id": "", "model": "auto", "effort": "",
               "extra_read_dirs": [], "new_session_id": "", "argv": self.argv,
               "backend": "cursor"}
        self.proc.stdin.write(json.dumps(req).encode("utf-8") + b"\n")
        self.proc.stdin.flush()
        self.proc.stdin.close()

    def out(self):
        return _rows(os.path.join(self.run_dir, "out.jsonl"))

    def results(self):
        return [r for r in self.out() if r.get("type") == "result"]

    def wait_result(self):
        assert _wait_for(lambda: self.results()), \
            "no result row\nout: %s\nhost.err: %s" % (
                self.out(), _read(os.path.join(self.run_dir, "host.err.log")))

    def close(self):
        pids = {self.proc.pid, self.cli_pid()}
        try:
            with open(os.path.join(self.run_dir, "pid")) as fh:
                pids.add(int(fh.read().strip()))
        except (OSError, ValueError):
            pass
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=10 if not self.run_dir else 0.1)
        except subprocess.TimeoutExpired:
            pass
        for pid in pids:
            if pid:
                _kill(pid)
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        self.err.close()


@pytest.fixture()
def spare_host(agent, tmp_path):
    made = []

    def _make(**kw):
        h = SpareHost(agent, tmp_path, name=str(len(made)), **kw)
        made.append(h)
        return h

    yield _make
    for h in made:
        h.close()


def test_a_spare_serves_a_real_chat_after_one_initialize_and_two_session_news(spare_host):
    h = spare_host(fake_args=["--stderr-on-prompt", "CLI-SAYS-HELLO"])
    h.wait_ready()
    # The warm-up is all there is until a request arrives.
    assert h.methods().count("initialize") == 1
    assert h.methods().count("session/new") == 1
    h.adopt()
    h.wait_result()
    result = h.results()[0]
    assert not result.get("is_error"), result
    init = next(r for r in h.out() if r.get("subtype") == "init")
    assert init["backend"] == "cursor"
    assert h.methods().count("initialize") == 1
    assert h.methods().count("session/new") == 2
    # The real session is the second one, not the throwaway.
    news = [e["sent"]["result"]["sessionId"] for e in _rows(h.log)
            if "sent" in e and isinstance(e["sent"].get("result"), dict)
            and "sessionId" in e["sent"]["result"]]
    assert len(news) == 2 and init["session_id"] == news[1] != news[0]
    # CLI stderr written after adoption lands in the run's err.log.
    err = os.path.join(h.run_dir, "err.log")
    assert _wait_for(lambda: "CLI-SAYS-HELLO" in _read(err)), _read(err)


def test_a_spare_that_gets_stdin_eof_leaves_no_cli_behind(spare_host):
    h = spare_host()
    h.wait_ready()
    pid = h.cli_pid()
    assert pid and not _pid_gone(pid)
    h.proc.stdin.close()
    assert _wait_for(lambda: h.proc.poll() is not None), "spare host did not exit"
    assert h.proc.returncode == 0
    assert _wait_for(lambda: _pid_gone(pid)), "the spare's CLI outlived its host"


def test_a_spare_whose_warm_up_fails_exits_without_ready(spare_host):
    h = spare_host(fake_args=["--signed-out"])
    assert _wait_for(lambda: h.proc.poll() is not None), "spare host did not exit"
    assert not h.ready
    # The CLI was stopped politely (SIGTERM), not left running.
    assert _wait_for(lambda: any(e.get("sigterm") for e in _rows(h.log)))


def test_an_update_for_the_throwaway_session_never_reaches_the_real_chat(spare_host):
    h = spare_host(fake_args=["--stale-update"])
    h.wait_ready()
    h.adopt()
    h.wait_result()
    # Premise: the fake did send the stale chunk (before the real ones).
    assert any("STALE" in json.dumps(e.get("sent", {})) for e in _rows(h.log))
    assert "STALE" not in json.dumps(h.out())
    assert "Hello world" in json.dumps(h.out())


def test_an_idle_spare_whose_cli_dies_withdraws_ready_before_exiting(spare_host):
    h = spare_host()
    h.wait_ready()
    pid = h.cli_pid()
    assert pid
    _kill(pid)
    assert _wait_for(lambda: h.proc.poll() is not None), "spare host did not exit"
    # `agent._claim_cursor_spare` requires `ready`, so a gone file = no claim.
    assert not h.ready


def test_only_the_throwaway_session_id_is_dropped_from_updates():
    host_mod = _load("cursor_host_for_spare_update_filter", HOST)
    seen = []
    h = object.__new__(host_mod._Host)
    h.lock = threading.RLock()
    h.sid = "real-sid"
    h.throwaway_sid = "throwaway-sid"
    h.replaying = False
    h.replay_gap = False
    h.last_update_at = 0.0
    h._on_chunk = lambda kind, text: seen.append(text)

    def push(sid, text):
        h._on_notification("session/update", {"sessionId": sid, "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}}})

    push("throwaway-sid", "DROPPED")
    push("some-third-sid", "KEPT")
    assert seen == ["KEPT"]


# ----------------------------------------------------------- agent.py: claim

class _Sleeper:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdin=subprocess.PIPE)

    def stop(self):
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        self.proc.kill()
        self.proc.wait()


@pytest.fixture()
def sleepers():
    made = []

    def _make():
        s = _Sleeper()
        made.append(s)
        return s.proc

    yield _make
    for s in made:
        s.stop()


def _entry(agent, proc, tmp_path, name, argv, ready=True):
    d = tmp_path / ("sp-" + name)
    d.mkdir()
    if ready:
        (d / "ready").write_text("")
    return {"proc": proc, "argv": argv, "dir": str(d), "born": time.monotonic(),
            "timer": threading.Timer(120, lambda: None)}


@pytest.fixture()
def clean_registry(agent):
    yield
    with agent._CURSOR_SPARES_LOCK:
        agent._CURSOR_SPARES.clear()


def test_claim_hands_out_a_ready_same_cwd_same_argv_spare_exactly_once(
        agent, tmp_path, sleepers, clean_registry):
    cwd = tmp_path / "proj"
    cwd.mkdir()
    proc = sleepers()
    argv = ["/x/cursor-agent", "acp"]
    agent._CURSOR_SPARES[os.path.realpath(cwd)] = _entry(agent, proc, tmp_path, "a", argv)
    assert agent._claim_cursor_spare(str(cwd), argv) is proc
    assert agent._claim_cursor_spare(str(cwd), argv) is None


def test_claim_refuses_other_cwd_other_argv_unready_and_dead_spares(
        agent, tmp_path, sleepers, clean_registry):
    cwd = tmp_path / "proj"
    other = tmp_path / "other"
    cwd.mkdir()
    other.mkdir()
    key = os.path.realpath(cwd)
    argv = ["/x/cursor-agent", "acp"]
    agent._CURSOR_SPARES[key] = _entry(agent, sleepers(), tmp_path, "a", argv)
    assert agent._claim_cursor_spare(str(other), argv) is None
    assert agent._claim_cursor_spare(str(cwd), ["/y/cursor-agent", "acp"]) is None
    assert key in agent._CURSOR_SPARES  # a mismatch leaves the spare in place
    # Not ready yet: left registered, for the next chat.
    agent._CURSOR_SPARES[key] = _entry(agent, sleepers(), tmp_path, "b", argv, ready=False)
    assert agent._claim_cursor_spare(str(cwd), argv) is None
    assert key in agent._CURSOR_SPARES
    # Dead: dropped.
    dead = sleepers()
    dead.kill()
    dead.wait()
    agent._CURSOR_SPARES[key] = _entry(agent, dead, tmp_path, "c", argv)
    assert agent._claim_cursor_spare(str(cwd), argv) is None
    assert key not in agent._CURSOR_SPARES


# --------------------------------------------------------- agent.py: prewarm

def _fake_binary(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport runpy, sys\n"
                    f"sys.argv[0] = {FAKE!r}\n"
                    f"runpy.run_path({FAKE!r}, run_name='__main__')\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.fixture()
def prewarm(agent, tmp_path, monkeypatch):
    """`_prewarm_cursor` switched on, over the fake CLI; every spare it
    starts is retired (stdin EOF) and waited for at teardown."""
    monkeypatch.setenv("FUSED_CURSOR_PREWARM", "1")
    binary = _fake_binary(tmp_path / "bin" / "cursor-agent")
    monkeypatch.setattr(agent, "_cursor_bin", lambda: binary)
    monkeypatch.setattr(agent, "_backend_enabled", lambda backend: True)
    seen = []
    real_popen = subprocess.Popen

    def _popen(*a, **kw):
        p = real_popen(*a, **kw)
        seen.append(p)
        return p

    monkeypatch.setattr(agent.subprocess, "Popen", _popen)
    yield agent, binary
    monkeypatch.undo()
    with agent._CURSOR_SPARES_LOCK:
        entries = list(agent._CURSOR_SPARES.values())
        agent._CURSOR_SPARES.clear()
    for e in entries:
        agent._close_cursor_spare(e)
    for p in seen:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def _folder(tmp_path, name):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    return str(d)


def test_prewarm_starts_one_spare_per_cwd_that_becomes_ready_and_claimable(
        prewarm, tmp_path):
    agent, binary = prewarm
    folder = _folder(tmp_path, "proj")
    agent._prewarm_cursor(folder)
    key = os.path.realpath(folder)
    entry = agent._CURSOR_SPARES[key]
    agent._prewarm_cursor(folder)
    assert agent._CURSOR_SPARES[key] is entry and len(agent._CURSOR_SPARES) == 1
    assert _wait_for(lambda: os.path.exists(os.path.join(entry["dir"], "ready")))
    assert agent._claim_cursor_spare(folder, [binary, "acp"]) is entry["proc"]
    assert key not in agent._CURSOR_SPARES


def test_prewarm_keeps_at_most_two_spares_and_retires_the_oldest(prewarm, tmp_path):
    agent, _ = prewarm
    a, b, c = (_folder(tmp_path, n) for n in "abc")
    agent._prewarm_cursor(a)
    first = agent._CURSOR_SPARES[os.path.realpath(a)]["proc"]
    agent._prewarm_cursor(b)
    agent._prewarm_cursor(c)
    assert set(agent._CURSOR_SPARES) == {os.path.realpath(b), os.path.realpath(c)}
    # The retired one's host closes its stdin-EOF'd CLI and exits.
    assert _wait_for(lambda: first.poll() is not None)


def test_an_unclaimed_spare_is_retired_when_its_ttl_runs_out(prewarm, tmp_path, monkeypatch):
    agent, _ = prewarm
    monkeypatch.setattr(agent, "_CURSOR_SPARE_TTL_SECONDS", 0.2)
    folder = _folder(tmp_path, "proj")
    agent._prewarm_cursor(folder)
    entry = agent._CURSOR_SPARES[os.path.realpath(folder)]
    assert _wait_for(lambda: not agent._CURSOR_SPARES)
    assert _wait_for(lambda: entry["proc"].poll() is not None)


def test_retiring_a_claimed_spare_leaves_it_alone(prewarm, tmp_path):
    agent, binary = prewarm
    folder = _folder(tmp_path, "proj")
    agent._prewarm_cursor(folder)
    entry = agent._CURSOR_SPARES[os.path.realpath(folder)]
    assert _wait_for(lambda: os.path.exists(os.path.join(entry["dir"], "ready")))
    claimed = agent._claim_cursor_spare(folder, [binary, "acp"])
    agent._retire_cursor_spare(os.path.realpath(folder), entry)
    assert claimed.poll() is None and not claimed.stdin.closed
    claimed.stdin.close()
    assert _wait_for(lambda: claimed.poll() is not None)


def test_prewarm_is_off_with_the_env_opt_out(prewarm, tmp_path, monkeypatch):
    agent, _ = prewarm
    for value in ("0", "false", "off", "no", "OFF"):
        monkeypatch.setenv("FUSED_CURSOR_PREWARM", value)
        agent._prewarm_cursor(_folder(tmp_path, "proj"))
        assert not agent._CURSOR_SPARES, value
    monkeypatch.delenv("FUSED_CURSOR_PREWARM")
    assert agent._cursor_prewarm_enabled()


def test_prewarm_skips_a_missing_binary_and_a_signed_out_cli(prewarm, tmp_path, monkeypatch):
    agent, _ = prewarm
    folder = _folder(tmp_path, "proj")
    monkeypatch.setattr(agent, "_cursor_bin", lambda: str(tmp_path / "gone"))
    agent._prewarm_cursor(folder)
    assert not agent._CURSOR_SPARES

    class _Health:
        @staticmethod
        def cached():
            return {"found": True, "broken": False, "signed_in": False}

    monkeypatch.setattr(agent, "_cursor_bin", lambda: _fake_binary(tmp_path / "b2" / "c"))
    monkeypatch.setitem(sys.modules, "fused_render.cursor_health", _Health)
    agent._prewarm_cursor(folder)
    assert not agent._CURSOR_SPARES


# ---------------------------------------------------- agent.py: _start, _defaults

def test_start_falls_back_to_a_cold_host_when_the_claimed_spares_pipe_is_broken(
        agent, tmp_path, sleepers, clean_registry, monkeypatch):
    folder = _folder(tmp_path, "proj")
    page = os.path.join(folder, "index.html")
    with open(page, "w") as fh:
        fh.write("<html></html>")
    monkeypatch.setattr(agent, "_cursor_bin", lambda: "/x/cursor-agent")
    monkeypatch.setattr(agent, "_terminal_tools_supported", lambda: False)
    monkeypatch.setattr(agent, "_backend_enabled", lambda backend: True)
    argv = ["/x/cursor-agent", "acp"]
    monkeypatch.setattr(agent, "_cursor_argv", lambda *a, **k: list(argv))
    proc = sleepers()
    proc.stdin.close()  # the spare's pipe is already broken
    agent._CURSOR_SPARES[os.path.realpath(folder)] = _entry(
        agent, proc, tmp_path, "a", argv)

    spawned = []

    class _Cold:
        pid = 0
        stdin = type("W", (), {"write": lambda self, b: spawned.append(b),
                               "close": lambda self: None})()

        def wait(self):
            return 0

    monkeypatch.setattr(agent.subprocess, "Popen",
                        lambda *a, **kw: spawned.append(("popen", a[0])) or _Cold())
    out = agent._start(page, "hello", "", "auto", "", backend="cursor")
    assert out.get("run_id"), out
    assert any(isinstance(x, tuple) and x[0] == "popen" for x in spawned)
    assert os.path.exists(os.path.join(agent.RUNS, out["run_id"], "host.err.log"))
    assert not agent._CURSOR_SPARES  # the broken spare was consumed, not reused


def test_start_hands_the_chat_to_a_ready_spare_instead_of_spawning(
        agent, tmp_path, clean_registry, monkeypatch):
    folder = _folder(tmp_path, "proj")
    page = os.path.join(folder, "index.html")
    with open(page, "w") as fh:
        fh.write("<html></html>")
    monkeypatch.setattr(agent, "_cursor_bin", lambda: "/x/cursor-agent")
    monkeypatch.setattr(agent, "_terminal_tools_supported", lambda: False)
    monkeypatch.setattr(agent, "_backend_enabled", lambda backend: True)
    argv = ["/x/cursor-agent", "acp"]
    monkeypatch.setattr(agent, "_cursor_argv", lambda *a, **k: list(argv))
    written = []

    class _Pipe:
        def write(self, b):
            written.append(b)

        def flush(self):
            pass

        def close(self):
            pass

    class _Spare:
        pid = 4242
        stdin = _Pipe()

        def poll(self):
            return None

    agent._CURSOR_SPARES[os.path.realpath(folder)] = _entry(
        agent, _Spare(), tmp_path, "a", argv)

    def _no_popen(*a, **kw):
        raise AssertionError("a ready spare must be used, not a cold host")

    monkeypatch.setattr(agent.subprocess, "Popen", _no_popen)
    out = agent._start(page, "hello", "", "auto", "", backend="cursor")
    assert out.get("run_id"), out
    req = json.loads(written[0].decode("utf-8"))
    assert written[0].endswith(b"\n")
    assert req["run_dir"] == os.path.join(agent.RUNS, out["run_id"])
    assert req["cwd"] == folder and req["argv"] == argv
    assert os.path.exists(os.path.join(req["run_dir"], "host.err.log"))


def test_defaults_prewarms_only_for_the_cursor_backend(agent, tmp_path, monkeypatch):
    folder = _folder(tmp_path, "proj")
    calls = []
    monkeypatch.setattr(agent, "_prewarm_cursor", lambda f: calls.append(f))
    d = agent._defaults(folder, backend="cursor")
    assert d["backend"] == "cursor" and calls == [folder]
    agent._defaults(folder, backend="codex")
    agent._defaults(folder)
    assert calls == [folder]


def test_defaults_skips_the_prewarm_when_the_session_already_has_a_live_host(
        agent, tmp_path, monkeypatch):
    folder = _folder(tmp_path, "proj")
    calls = []
    monkeypatch.setattr(agent, "_prewarm_cursor", lambda f: calls.append(f))
    monkeypatch.setattr(agent, "_backend_for_session", lambda *a, **k: "cursor")
    monkeypatch.setattr(agent, "_live_host", lambda f, s="", **k: {"run_id": "r1"})
    agent._defaults(folder, session_id="11111111-1111-4111-8111-111111111111")
    assert calls == []
    monkeypatch.setattr(agent, "_live_host", lambda f, s="", **k: {"run_id": ""})
    agent._defaults(folder, session_id="11111111-1111-4111-8111-111111111111")
    assert calls == [folder]


def test_a_prewarm_failure_never_changes_what_defaults_answers(agent, tmp_path, monkeypatch):
    folder = _folder(tmp_path, "proj")

    def _boom(f):
        raise RuntimeError("no")

    monkeypatch.setattr(agent, "_prewarm_cursor", _boom)
    assert agent._defaults(folder, backend="cursor")["backend"] == "cursor"
