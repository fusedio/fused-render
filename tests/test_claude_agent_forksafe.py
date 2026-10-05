"""The chat's session host is spawned WITHOUT a fork from inside the server.

`_start` used to run in a throwaway `/api/run` child and spawn the host with
`start_new_session=True`. It now runs in the server process itself
(fused_render/claude_agent), where libproj is resident with a live proj.db
handle and fork() runs PROJ's atfork child handler, which SIGSEGVs the child
before exec (test_worker_forksafe.py has the same constraint for the run
workers). CPython takes posix_spawn only when the Popen has close_fds=False,
no cwd, no start_new_session, no preexec_fn and an absolute argv[0] — so those
kwargs are pinned here, by capturing what `_start` actually passes. The host
then detaches ITSELF with os.setsid() before doing anything else, which is the
other half: without it the host would share the server's session and die with
it.
"""
import importlib.util
import io
import os
import sys

import pytest

AGENT_DIR = os.path.join("fused_render", "claude_agent")

posix_only = pytest.mark.skipif(os.name == "nt", reason="posix_spawn is POSIX-only")


def _load(name):
    path = os.path.join(AGENT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_forksafe_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _HostProc:
    pid = 4242

    class _Stdin:
        def write(self, data):
            pass

        def close(self):
            pass

    def __init__(self):
        self.stdin = _HostProc._Stdin()

    def wait(self):
        return 0


@pytest.fixture
def spawned(tmp_path, monkeypatch):
    """Run `_start` once and hand back the captured `(argv, kwargs)`."""
    agent = _load("agent")
    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    project = tmp_path / "proj"
    project.mkdir()
    calls = []

    def fake_popen(cmd, **kw):
        calls.append((cmd, kw))
        return _HostProc()

    monkeypatch.setattr(agent.subprocess, "Popen", fake_popen)
    out = agent._start(str(project), "hello", "", "", "")
    assert "error" not in out, out
    assert len(calls) == 1, "one Popen per start: the session host"
    return agent, calls[0]


@posix_only
def test_the_host_spawn_kwargs_are_exactly_the_posix_spawn_safe_set(spawned):
    agent, _ = spawned
    assert agent._HOST_SPAWN == {"close_fds": False}


@posix_only
def test_start_spawns_the_host_through_posix_spawn_not_fork(spawned):
    _agent, (argv, kw) = spawned
    # Absent, not merely falsy: each of these, when passed at all, is one more
    # thing to keep falsy forever; the contract is that `_start` never names them.
    for name in ("start_new_session", "preexec_fn", "cwd", "process_group",
                 "pass_fds", "user", "group", "extra_groups", "umask"):
        assert name not in kw, name
    assert kw["close_fds"] is False
    assert os.path.isabs(argv[0]), argv
    assert os.path.basename(argv[1]) == "session_host.py", argv


@posix_only
def test_the_host_detaches_itself_before_reading_its_request(monkeypatch):
    """os.setsid() is the host's FIRST act. The request read off stdin is the
    next, so a host that read first and crashed on a bad request would still
    be sitting in the server's session — and the order is what this pins."""
    host = _load("session_host")
    order = []

    class _Stop(Exception):
        pass

    class _Buf(io.BytesIO):
        def read(self, *a):
            order.append("stdin")
            raise _Stop()

    class _Stdin:
        buffer = _Buf()

    monkeypatch.setattr(host.os, "setsid", lambda: order.append("setsid"))
    # Recorded, never run: a real closerange(3, ...) here would close pytest's
    # own descriptors.
    monkeypatch.setattr(host.os, "closerange",
                        lambda lo, hi: order.append(("closerange", lo)))
    monkeypatch.setattr(host.sys, "stdin", _Stdin())
    monkeypatch.setattr(host, "_enable_faulthandler", lambda: None)
    with pytest.raises(_Stop):
        host.main()
    # The inherited server fds go right after the detach and before anything
    # else the host does (D1310): spawned with close_fds=False, it holds every
    # non-CLOEXEC fd the server had, and the CLI it spawns would inherit them.
    assert order == ["setsid", ("closerange", 3), "stdin"]


@posix_only
def test_the_inherited_fd_sweep_is_capped(monkeypatch):
    """RLIM_INFINITY or -1 from sysconf must not become a closerange over
    billions of descriptors."""
    host = _load("session_host")
    seen = []
    monkeypatch.setattr(host.os, "closerange", lambda lo, hi: seen.append((lo, hi)))
    for reported in (-1, 2 ** 62, 1024):
        monkeypatch.setattr(host.os, "sysconf", lambda name, v=reported: v)
        host._close_inherited_fds()
    assert seen == [(3, host._FD_CLOSE_CAP), (3, host._FD_CLOSE_CAP), (3, 1024)]


@posix_only
def test_a_host_that_is_already_a_session_leader_carries_on(monkeypatch):
    """EPERM from setsid means the process already leads a session; that is
    not a reason to abandon the chat."""
    host = _load("session_host")

    class _Stop(Exception):
        pass

    class _Buf(io.BytesIO):
        def read(self, *a):
            raise _Stop()

    class _Stdin:
        buffer = _Buf()

    def eperm():
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(host.os, "setsid", eperm)
    monkeypatch.setattr(host.os, "closerange", lambda lo, hi: None)
    monkeypatch.setattr(host.sys, "stdin", _Stdin())
    monkeypatch.setattr(host, "_enable_faulthandler", lambda: None)
    with pytest.raises(_Stop):
        host.main()


def test_the_cli_spawn_inside_the_host_still_detaches():
    """The CLI is spawned by the HOST (its own process, where fork is
    harmless), and `_cancel`'s killpg needs it to lead its own group — so
    `_DETACH` keeps start_new_session there, unlike `_HOST_SPAWN`."""
    agent = _load("agent")
    if sys.platform == "win32":
        assert "creationflags" in agent._DETACH
    else:
        assert agent._DETACH == {"start_new_session": True}
