"""The in-server chat backend's edges that only exist because it runs IN the
server now (D1310).

`_start` spawns the session host posix_spawn-style into the SERVER's process
group, and the host only detaches itself (`os.setsid()`) once its interpreter
is up. That opens windows the old throwaway-child spawn never had:

* a Stop that lands before the host's setsid — `_kill_tree`'s killpg finds no
  group (the pid file still names the host, which leads none yet), and the
  host must notice the `cancelled` marker `_cancel` wrote first rather than go
  on to spawn a CLI nobody will ever stop;
* a host that dies before reading its request — the stdin write raises, and
  the reaper thread must already be running or the host is a zombie for the
  server's lifetime;
* a `git` with no absolute path — a bare name takes CPython's fork path, which
  SIGSEGVs with libproj resident;
* a request cancelled while its `start` is still running in the pool — the
  folder's owner must still be filed when the work lands;
* a Stop queued behind a saturated poll pool, spending its budget waiting.
"""
import asyncio
import importlib.util
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import types

import pytest

from fused_render import claude_agent
from fused_render.claude_agent import gate, pool
from fused_render.server.routers import claude_agent as router

AGENT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "fused_render", "claude_agent")

def _load(name):
    path = os.path.join(AGENT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_cancel_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------- a Stop before the detach

def test_a_host_whose_run_was_cancelled_before_it_detached_spawns_nothing(tmp_path):
    """The real host, as a real process: the marker is already there when it
    reads its request, so it returns without loading the agent or spawning a
    CLI. The agent path is deliberately bogus — reaching `_load_agent` at all
    would fail loudly instead of passing quietly."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "cancelled").write_text("1")
    req = {"agent": str(tmp_path / "no-such-agent.py"), "run_dir": str(run_dir)}
    proc = subprocess.run(
        [sys.executable, os.path.join(AGENT_DIR, "session_host.py")],
        input=json.dumps(req).encode(), capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert b"cancelled before the CLI spawned" in proc.stderr
    assert not (run_dir / "out.jsonl").exists(), "no CLI was ever spawned"
    assert not (run_dir / "host.json").exists()


def test_kill_tree_signals_the_host_when_it_leads_no_group_yet(tmp_path, monkeypatch):
    """killpg on a pid that is not a group leader is ESRCH even though the
    process is alive. Then the process itself gets the SIGTERM."""
    agent = _load("agent")
    (tmp_path / "pid").write_text("4242")
    sent = []

    def killpg(pid, sig):
        sent.append(("killpg", pid, sig))
        raise ProcessLookupError(3, "No such process")

    monkeypatch.setattr(agent.os, "killpg", killpg)
    monkeypatch.setattr(agent.os, "kill", lambda pid, sig: sent.append(("kill", pid, sig)))
    agent._kill_tree(str(tmp_path))
    assert sent == [("killpg", 4242, signal.SIGTERM), ("kill", 4242, signal.SIGTERM)]


def test_kill_tree_does_not_fall_back_when_the_group_was_signalled(tmp_path, monkeypatch):
    agent = _load("agent")
    (tmp_path / "pid").write_text("4242")
    sent = []
    monkeypatch.setattr(agent.os, "killpg", lambda pid, sig: sent.append("killpg"))
    monkeypatch.setattr(agent.os, "kill", lambda pid, sig: sent.append("kill"))
    agent._kill_tree(str(tmp_path))
    assert sent == ["killpg"]


# ------------------------------------------------- no absolute git, no commit

def test_commit_turn_skips_rather_than_spawn_a_bare_git(tmp_path, monkeypatch):
    agent = _load("agent")
    ws = tmp_path / "ws"
    app = ws / "tag" / "app"
    (app / ".git").mkdir(parents=True)
    (app / "f.txt").write_text("x")
    monkeypatch.setattr(agent, "_workspace_dir", lambda: str(ws))
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    ran = []
    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(agent.subprocess, "Popen", lambda *a, **k: ran.append(a))
    agent._commit_turn(str(app / "f.txt"), "msg")
    assert ran == [], "no git on PATH is the no-git case, never argv[0]='git'"


def test_commit_turn_spawns_git_by_absolute_path(tmp_path, monkeypatch):
    agent = _load("agent")
    ws = tmp_path / "ws"
    app = ws / "tag" / "app"
    (app / ".git").mkdir(parents=True)
    (app / "f.txt").write_text("x")
    monkeypatch.setattr(agent, "_workspace_dir", lambda: str(ws))
    import shutil
    git = "/opt/bin/git"
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: git)
    argv0 = []

    def fake_run(cmd, *a, **k):
        argv0.append(cmd[0])
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    agent._commit_turn(str(app / "f.txt"), "msg")
    assert argv0 and set(argv0) == {git}


# ------------------------------------------- a host that dies before stdin

def test_a_host_that_died_before_its_request_is_reaped_and_reported(tmp_path, monkeypatch):
    """The reaper is started before the request is written, so a host that is
    already gone (BrokenPipeError on the write) is still waited on, and the
    caller gets an `{"error"}` rather than an exception the router would turn
    into a 500."""
    agent = _load("agent")
    agent.RUNS = str(tmp_path / "runs")
    monkeypatch.setattr(agent, "_claude_bin", lambda: "/bin/claude")
    project = tmp_path / "proj"
    project.mkdir()
    reaped = threading.Event()
    seen = {}

    class _Stdin:
        def write(self, data):
            # The reaper must already be parked in wait() by the time the
            # write is attempted; give it a moment to get scheduled.
            seen["reaper_first"] = reaped.wait(5)
            raise BrokenPipeError(32, "Broken pipe")

        def close(self):
            raise BrokenPipeError(32, "Broken pipe")

    class _Proc:
        pid = 4242
        stdin = _Stdin()

        def wait(self):
            reaped.set()
            return 1

    monkeypatch.setattr(agent.subprocess, "Popen", lambda *a, **k: _Proc())
    out = agent._start(str(project), "hello", "", "", "")
    assert seen["reaper_first"] is True
    assert set(out) == {"error"}, out
    assert "session host" in out["error"]


# --------------------------------------------- a request cancelled mid-start

def test_a_cancelled_start_still_files_its_owner_when_the_work_lands(monkeypatch):
    """The client hung up while `_start` was still running in the pool. The
    CancelledError propagates (the request is gone), but `_file_owner` must
    still run once the work finishes, or the `admit:` placeholder its gate
    minted is never released."""
    release = threading.Event()
    started = threading.Event()
    filed = []
    landed = threading.Event()

    def main(action="", file=""):
        started.set()
        release.wait(10)
        return {"run_id": "r-1", "session_id": "s-1"}
    main_sig = inspect.signature(main)

    def recording_main(*a, **kw):
        return main(*a, **kw)
    recording_main.__signature__ = main_sig
    monkeypatch.setattr(claude_agent, "agent_module",
                        lambda: types.SimpleNamespace(main=recording_main))
    monkeypatch.setattr(gate, "_folder_busy", lambda params, body=None: "")

    def file_owner(params, envelope, body=None, late=False):
        filed.append(envelope)
        landed.set()
    monkeypatch.setattr(gate, "_file_owner", file_owner)

    request = types.SimpleNamespace(state=types.SimpleNamespace())

    async def go():
        task = asyncio.ensure_future(router.api_claude_agent(
            request, body={"action": "start", "file": "/w/a.html"}, x_fused="1"))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert filed == [], "nothing is filed while the work is still running"
    release.set()
    assert landed.wait(10), "the late filing never ran"
    assert filed == [{"ok": True, "result": {"run_id": "r-1", "session_id": "s-1"}}]


def test_run_hands_the_future_to_on_cancel_and_reraises():
    got = []
    gate_ev = threading.Event()

    async def go():
        task = asyncio.ensure_future(router._run(
            "poll", 30, lambda: gate_ev.wait(10), {}, on_cancel=got.append))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    gate_ev.set()
    assert len(got) == 1 and hasattr(got[0], "add_done_callback")


# ------------------------------------------------------- the control lane

def test_stop_allow_and_app_state_ride_their_own_lane():
    # `canvas_shot` joined the control lane with #1924: the page's answer to
    # a blocked `canvas_screenshot` tool call must not queue behind polls.
    control = ("cancel", "decide", "allow_denied", "app_state", "canvas_shot", "canvas_shot_claim")
    assert set(control) == set(pool.CONTROL_ACTIONS)
    for action in control:
        assert pool.pool_for(action) is pool.CONTROL_POOL, action
    for action in set(router.ACTIONS) - set(control) - set(pool.SIDE_POOLS):
        assert pool.pool_for(action) is pool.POOL, action
    for action, lane in pool.SIDE_POOLS.items():
        assert pool.pool_for(action) is lane, action
        assert lane is not pool.POOL and lane is not pool.CONTROL_POOL
    assert pool.CONTROL_POOL is not pool.POOL
    assert pool.CONTROL_POOL._max_workers == 2
    assert pool.POOL._max_workers == 8


def test_a_stop_runs_while_the_poll_pool_is_saturated():
    """Every main-pool worker busy; a `cancel` still runs at once."""
    hold = threading.Event()
    busy = [pool.POOL.submit(hold.wait, 10) for _ in range(pool.POOL._max_workers + 2)]
    try:
        fut = pool.pool_for("cancel").submit(lambda: "stopped")
        assert fut.result(timeout=5) == "stopped"
    finally:
        hold.set()
        for f in busy:
            f.result(timeout=10)


def _entry(run_dir, text, uid, hint="\n\n<terminal-hint>x</terminal-hint>"):
    from fused_render.claude_agent import agent

    agent._write_inbox_entry(str(run_dir), text + hint, uid, typed=text)


def test_still_queued_text_resolves_uuids_to_typed_words_without_the_hint(tmp_path):
    """The CLI's names stay as named; `still_queued_text` maps each canonical
    uuid to the words the user typed, not the composed turn."""
    from fused_render.claude_agent import agent

    uid = "36422243-e3e0-4d9a-bd54-aad6c0de909b"
    other = "11111111-2222-3333-4444-555555555555"
    _entry(tmp_path, "say hi", uid)
    out = agent._still_queued_text(str(tmp_path), [uid, other, "plain words"])
    assert out == {uid: "say hi"}


def test_still_queued_text_keeps_two_same_words_entries_apart(tmp_path):
    from fused_render.claude_agent import agent

    a, b = "aaaaaaaa-2222-3333-4444-555555555555", "bbbbbbbb-2222-3333-4444-555555555555"
    _entry(tmp_path, "again", a)
    _entry(tmp_path, "again", b)
    assert agent._still_queued_text(str(tmp_path), [a, b]) == {a: "again", b: "again"}


def test_still_queued_text_resolves_beyond_sixteen_entries(tmp_path):
    from fused_render.claude_agent import agent

    ids = ["%08x-2222-3333-4444-555555555555" % i for i in range(1, 31)]
    for n, uid in enumerate(ids):
        _entry(tmp_path, "msg %d" % n, uid)
    assert agent._still_queued_text(str(tmp_path), [ids[0]]) == {ids[0]: "msg 0"}


def test_still_queued_text_ignores_loose_words(tmp_path):
    from fused_render.claude_agent import agent

    assert agent._still_queued_text(str(tmp_path), ["continue", "go on please"]) == {}


def test_typed_words_are_one_file_per_uuid_and_never_a_shared_map(tmp_path):
    from fused_render.claude_agent import agent

    a, b = "aaaaaaaa-2222-3333-4444-555555555555", "bbbbbbbb-2222-3333-4444-555555555555"
    _entry(tmp_path, "one", a)
    _entry(tmp_path, "two", b)
    folder = tmp_path / "inbox_typed"
    assert sorted(p.name for p in folder.iterdir()) == [a, b]
    assert not (tmp_path / "inbox_typed.json").exists()
    assert (folder / a).read_text(encoding="utf-8") == "one"


def test_explicit_typed_words_win_over_the_composed_message(tmp_path):
    """The page sends its own words next to the composed wire (app-state and
    attachment markers); `send` and `start` both take them."""
    from fused_render.claude_agent import agent

    for fn in (agent._send, agent._start, agent.main):
        assert "typed" in inspect.signature(fn).parameters
    uid = "cccccccc-2222-3333-4444-555555555555"
    agent._write_inbox_entry(str(tmp_path), "<live-app-state>x</live-app-state>\nhello", uid,
                             typed="hello")
    assert agent._still_queued_text(str(tmp_path), [uid]) == {uid: "hello"}
