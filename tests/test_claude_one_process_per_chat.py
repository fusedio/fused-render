"""One `claude` per conversation: a resume ends an IDLE host on the same
session — whatever target it was started for — before it spawns, and leaves a
host mid-turn alone (`agent._end_idle_hosts`, called from `_start`).

Most of this fakes the run dirs and the process calls; the last test drives the
real session host against the stub CLI, because "the old process is gone
before the new one starts" is a fact about processes, not about mocks.
"""
import importlib.util
import json
import os
import sys
import threading
import time

import pytest

from _claude_stub_cli import write_stub_cli

AGENT_DIR = os.path.join("fused_render", "claude_agent")
SID = "sess-1"


def _load(name):
    path = os.path.join(AGENT_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location("claude_oneproc_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load("agent")
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


class _HostProc:
    """Stands in for the session_host.py process `_start` Popens."""
    pid = 4242

    class _Stdin:
        def write(self, data):
            pass

        def close(self):
            pass

    stdin = _Stdin()

    def wait(self, timeout=None):
        return 0


@pytest.fixture()
def fake_spawn(agent, monkeypatch):
    """Popen never runs anything; records what `_kill_tree` was asked to end,
    and makes a killed host read dead from then on (what the real host does by
    removing host.json)."""
    spawned, killed = [], []
    dead = set()

    def popen(cmd, **kw):
        # Every host this test ended must already be gone by now.
        spawned.append(sorted(killed))
        assert all(d in dead for d in killed)
        return _HostProc()

    def kill_tree(run_dir):
        killed.append(os.path.basename(run_dir))
        dead.add(os.path.basename(run_dir))

    monkeypatch.setattr(agent.subprocess, "Popen", popen)
    monkeypatch.setattr(agent, "_kill_tree", kill_tree)
    monkeypatch.setattr(agent, "_host_alive",
                        lambda run_dir: os.path.basename(run_dir) not in dead
                        and os.path.exists(os.path.join(run_dir, "host.json")))
    return {"spawned": spawned, "killed": killed}


def _run(agent, name, file, resumed_from=SID, turn_open=False, tasks=False,
         inbox=False):
    """A run dir with a live host.json and an out.jsonl whose last row says
    whether a turn is open (`_turn_state` reads it for real)."""
    run_dir = os.path.join(agent.RUNS, name)
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"file": str(file), "message": "hi",
                   "resumed_from": resumed_from, "mode": "prompt"}, f)
    with open(os.path.join(run_dir, "host.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": 1, "session_id": resumed_from}, f)
    rows = [{"type": "system", "subtype": "init", "session_id": resumed_from},
            {"type": "result", "session_id": resumed_from}]
    if tasks:
        rows.append({"type": "system", "subtype": "background_tasks_changed",
                     "tasks": [{"task_id": "t1"}]})
    if turn_open:
        rows.append({"type": "assistant", "message": {"content": []}})
    with open(os.path.join(run_dir, "out.jsonl"), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))
    if inbox:
        agent._write_inbox_entry(run_dir, "queued follow-up")
    return run_dir


@pytest.fixture()
def files(tmp_path):
    a = tmp_path / "proj"
    a.mkdir()
    b = a / "notes.md"
    b.write_text("x")
    return str(a), str(b)


def test_idle_host_on_another_file_is_ended_before_the_resume_spawns(
        agent, fake_spawn, files):
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)

    res = agent._start(folder, "follow-up", SID, "", "", has_pane=False)

    assert res["session_id"] == SID
    assert fake_spawn["killed"] == ["20261014-100000-aaaaaa"]
    assert fake_spawn["spawned"] == [["20261014-100000-aaaaaa"]]
    # Quiet: no Stop marker on a reply that finished fine, and the host is told
    # not to announce an `exited` the queue would match against the new run.
    assert not os.path.exists(os.path.join(old, "cancelled"))
    assert os.path.exists(os.path.join(old, "superseded"))
    assert agent._stopped_last(note, SID) is False


@pytest.mark.parametrize("busy", [{"turn_open": True}, {"tasks": True},
                                  {"inbox": True}])
def test_a_host_with_live_work_is_left_alone(agent, fake_spawn, files, busy):
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note, **busy)

    res = agent._start(folder, "follow-up", SID, "", "", has_pane=False)

    assert "run_id" in res
    assert fake_spawn["killed"] == []
    assert not os.path.exists(os.path.join(old, "superseded"))


def test_a_host_for_another_session_is_untouched(agent, fake_spawn, files):
    folder, note = files
    _run(agent, "20261014-100000-aaaaaa", note, resumed_from="sess-other")

    agent._start(folder, "follow-up", SID, "", "", has_pane=False)
    assert fake_spawn["killed"] == []


def test_a_fresh_chat_ends_nothing(agent, fake_spawn, files):
    folder, note = files
    _run(agent, "20261014-100000-aaaaaa", note)

    agent._start(folder, "new chat", "", "", "", has_pane=False)
    assert fake_spawn["killed"] == []


def test_a_failing_scan_never_blocks_the_spawn(agent, fake_spawn, files,
                                               monkeypatch):
    folder, note = files
    _run(agent, "20261014-100000-aaaaaa", note)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    # Inside the per-run guard…
    monkeypatch.setattr(agent, "_turn_state", boom)
    assert "run_id" in agent._start(folder, "m", SID, "", "", has_pane=False)
    # …and the whole helper raising.
    monkeypatch.setattr(agent, "_end_idle_hosts", boom)
    assert "run_id" in agent._start(folder, "m", SID, "", "", has_pane=False)
    assert len(fake_spawn["spawned"]) == 2


def test_a_kill_that_never_lands_gives_the_marker_back(agent, files,
                                                        monkeypatch):
    """L1: a host the kill did not reach must not keep `superseded` — it would
    live on with its `exited` muted and every send refused."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    monkeypatch.setattr(agent, "_kill_tree", lambda run_dir: None)
    monkeypatch.setattr(agent, "_host_alive", lambda run_dir: True)
    monkeypatch.setattr(agent, "_SUPERSEDE_WAIT_SEC", 0.2)
    t0 = time.monotonic()
    assert agent._end_idle_hosts(SID) == []
    assert time.monotonic() - t0 < 3.0
    assert not os.path.exists(os.path.join(old, "superseded"))


def _pending_echo(run_dir, offset=None):
    if offset is None:
        offset = os.path.getsize(os.path.join(run_dir, "out.jsonl"))
    with open(os.path.join(run_dir, "pending_echo"), "w", encoding="utf-8") as f:
        f.write(str(offset))


def test_a_drained_follow_up_not_yet_echoed_is_live_work(agent, fake_spawn,
                                                         files):
    """M1: the host already drained a `_send` into the CLI's stdin (inbox
    empty), the CLI has not echoed it, out.jsonl still ends on a `result`."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    _pending_echo(old)
    assert agent._turn_state(old) == (False, False)

    agent._start(folder, "follow-up", SID, "", "", has_pane=False)
    assert fake_spawn["killed"] == []
    assert not os.path.exists(os.path.join(old, "superseded"))


def test_an_echoed_follow_up_no_longer_holds_the_host(agent, fake_spawn, files):
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    _pending_echo(old)
    # The echo landed and that turn has since finished too: idle again.
    with open(os.path.join(old, "out.jsonl"), "a", encoding="utf-8") as f:
        for row in ({"type": "user", "message": {"role": "user", "content": [
                        {"type": "text", "text": "follow-up"}]}},
                    {"type": "result", "session_id": SID}):
            f.write(json.dumps(row) + "\n")

    agent._start(folder, "follow-up", SID, "", "", has_pane=False)
    assert fake_spawn["killed"] == ["20261014-100000-aaaaaa"]


def test_a_send_landing_after_the_marker_calls_the_kill_off(
        agent, fake_spawn, files, monkeypatch):
    """M2: marker first, then a second look — a follow-up written between the
    first look and the marker is seen, and the host is left alone."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    real_busy = agent._host_busy
    looks = []

    def host_busy(run_dir):
        looks.append(os.path.exists(os.path.join(run_dir, "superseded")))
        if looks[-1]:  # the second look, after the marker: a send has landed
            agent._write_inbox_entry(run_dir, "raced in")
        return real_busy(run_dir)

    monkeypatch.setattr(agent, "_host_busy", host_busy)
    agent._start(folder, "follow-up", SID, "", "", has_pane=False)
    assert looks == [False, True]
    assert fake_spawn["killed"] == []
    assert not os.path.exists(os.path.join(old, "superseded"))


def test_live_host_skips_a_superseded_host(agent, files, monkeypatch):
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    monkeypatch.setattr(agent, "_host_alive", lambda run_dir: True)
    assert agent._live_host(note, SID) == {"run_id": "20261014-100000-aaaaaa"}
    open(os.path.join(old, "superseded"), "w").close()
    assert agent._live_host(note, SID) == {"run_id": ""}


def _live_json(run_dir):
    with open(os.path.join(run_dir, "host.json"), "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "session_id": SID, "read_dirs": [],
                   "effort": "", "model": "", "mode": ""}, f)


def _inbox_left(run_dir):
    try:
        return [n for n in os.listdir(os.path.join(run_dir, "inbox"))
                if n.endswith(".json")]
    except OSError:
        return []


def test_send_into_a_host_being_superseded_respawns(agent, files, monkeypatch):
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    _live_json(old)
    open(os.path.join(old, "superseded"), "w").close()
    monkeypatch.setattr(agent, "_host_alive", lambda run_dir: False)

    assert agent._send("20261014-100000-aaaaaa", "hi") == {"respawn": True}
    assert _inbox_left(old) == []


def test_a_supersede_that_lands_after_the_write_takes_the_entry_back(
        agent, files, monkeypatch):
    """M2: the marker appears after `_send`'s first check, the host is killed:
    the entry it wrote would be stranded after `sent` — so it is taken back
    (with its `pending_echo`) and the caller told to respawn."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    _live_json(old)
    real_write = agent._write_inbox_entry
    gone = []

    def write(run_dir, message):
        real_write(run_dir, message)
        open(os.path.join(run_dir, "superseded"), "w").close()
        gone.append(True)  # …and the kill lands

    monkeypatch.setattr(agent, "_write_inbox_entry", write)
    monkeypatch.setattr(agent, "_host_alive", lambda run_dir: not gone)

    assert agent._send("20261014-100000-aaaaaa", "hi") == {"respawn": True}
    assert _inbox_left(old) == []
    assert not os.path.exists(os.path.join(old, "pending_echo"))


def test_a_supersede_called_off_still_sends(agent, files, monkeypatch):
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    _live_json(old)
    marker = os.path.join(old, "superseded")
    open(marker, "w").close()
    monkeypatch.setattr(agent, "_host_alive", lambda run_dir: True)
    threading.Timer(0.2, os.remove, (marker,)).start()

    assert agent._send("20261014-100000-aaaaaa", "hi") == {"sent": True}
    assert len(_inbox_left(old)) == 1


def test_a_superseded_host_drains_nothing(tmp_path):
    host, agent = _load("session_host"), _load("agent")
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir)
    agent._write_inbox_entry(run_dir, "late")
    open(os.path.join(run_dir, "superseded"), "w").close()

    class _Stdin:
        written = []
        def write(self, d): self.written.append(d)
        def flush(self): pass

    stdin = _Stdin()
    host._drain_inbox(agent, run_dir, stdin)
    assert stdin.written == [] and _inbox_left(run_dir)
    os.remove(os.path.join(run_dir, "superseded"))
    host._drain_inbox(agent, run_dir, stdin)
    assert len(stdin.written) == 1 and _inbox_left(run_dir) == []


def test_a_superseded_host_does_not_announce_exited(tmp_path, monkeypatch):
    host, agent = _load("session_host"), _load("agent")
    monkeypatch.setattr(host, "_DRAIN_INTERVAL_SECONDS", 0.01)
    posted = []
    monkeypatch.setattr(host, "_post_event",
                        lambda rd, kind, **k: posted.append(kind))
    monkeypatch.setattr(host, "_turn_state_if_grown", lambda a, rd, c: (False, False))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "host.json").write_text("{}")
    (run_dir / "superseded").write_text(str(time.time()))

    class _Stdin:
        def write(self, d): pass
        def flush(self): pass
        def close(self): pass

    class _Cli:
        stdin = _Stdin()
        def poll(self): return -15  # killed: the loop never runs
        def wait(self, timeout=None): pass

    host._reap_loop(agent, str(run_dir), _Cli(), str(run_dir / "host.json"))
    assert "exited" not in posted
    assert not (run_dir / "host.json").exists()


# ------------------------------------------------------- the real process tree

_STUB = '''#!{python}
import json
import sys

sid = "sess-stub"
for flag in ("--session-id", "--resume"):
    if flag in sys.argv:
        sid = sys.argv[sys.argv.index(flag) + 1]

for line in sys.stdin:
    if not line.strip():
        continue
    sys.stdout.write(json.dumps({{"type": "result", "session_id": sid}}) + "\\n")
    sys.stdout.flush()
'''


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.mark.skipif(os.name == "nt",
                    reason="claude session host does not start on Windows yet (#979)")
def test_real_hosts_one_process_per_session(agent, tmp_path, monkeypatch, files):
    folder, note = files
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN",
                       write_stub_cli(tmp_path / "bin",
                                      _STUB.format(python=sys.executable)))
    monkeypatch.delenv("FUSED_RENDER_ORIGIN", raising=False)
    # Returns the moment the host is gone; generous only for a loaded box.
    monkeypatch.setattr(agent, "_SUPERSEDE_WAIT_SEC", 20.0)

    first = agent._start(note, "hello", "", "", "", has_pane=False)
    sid = first["session_id"]
    old_dir = os.path.join(agent.RUNS, first["run_id"])
    assert _wait_for(lambda: agent._host_alive(old_dir)
                     and agent._turn_state(old_dir) == (False, False))

    # The same conversation from another surface: a different target.
    second = agent._start(folder, "again", sid, "", "", has_pane=False)
    assert second["session_id"] == sid
    assert not agent._host_alive(old_dir), "the old host must be gone first"
    assert not os.path.exists(os.path.join(old_dir, "cancelled"))
    assert agent._poll(first["run_id"], note)["error"] in ("", None)

    new_dir = os.path.join(agent.RUNS, second["run_id"])
    assert _wait_for(lambda: agent._host_alive(new_dir))
    agent._cancel(second["run_id"], interrupt_first=False)
    assert _wait_for(lambda: not agent._host_alive(new_dir))


# ------------------------------------------------ round 2: marker ownership

def _stale_marker(run_dir):
    with open(os.path.join(run_dir, "superseded"), "w", encoding="utf-8") as f:
        f.write(str(time.time() - 600))


def test_a_stale_marker_on_a_live_host_holds_nothing(agent, files, monkeypatch):
    """A pass that never finished (the server died between marker and kill)
    must not hold the host's follow-ups forever."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    _live_json(old)
    _stale_marker(old)
    monkeypatch.setattr(agent, "_host_alive", lambda run_dir: True)

    t0 = time.monotonic()
    assert agent._send("20261014-100000-aaaaaa", "hi") == {"sent": True}
    # A fresh marker is waited out for ~4 s; a stale one not at all.
    assert time.monotonic() - t0 < 3.0, "a stale marker must not be waited out"
    assert len(_inbox_left(old)) == 1
    assert not os.path.exists(os.path.join(old, "superseded"))

    _stale_marker(old)
    assert agent._live_host(note, SID) == {"run_id": "20261014-100000-aaaaaa"}
    assert not os.path.exists(os.path.join(old, "superseded"))


def test_a_stale_marker_does_not_shut_the_hosts_inbox(tmp_path):
    host, agent = _load("session_host"), _load("agent")
    run_dir = str(tmp_path / "run")
    os.makedirs(run_dir)
    agent._write_inbox_entry(run_dir, "late")
    _stale_marker(run_dir)

    class _Stdin:
        def __init__(self): self.written = []
        def write(self, d): self.written.append(d)
        def flush(self): pass

    stdin = _Stdin()
    host._drain_inbox(agent, run_dir, stdin)
    assert len(stdin.written) == 1 and _inbox_left(run_dir) == []
    assert not os.path.exists(os.path.join(run_dir, "superseded"))


def test_a_marker_another_pass_holds_is_left_to_it(agent, fake_spawn, files):
    """Two ending passes on one host: the one that did not create the marker
    neither kills nor removes it."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    with open(os.path.join(old, "superseded"), "w", encoding="utf-8") as f:
        f.write(str(time.time()))

    agent._start(folder, "follow-up", SID, "", "", has_pane=False)
    assert fake_spawn["killed"] == []
    assert os.path.exists(os.path.join(old, "superseded"))


def test_losing_the_exclusive_create_race_leaves_the_host_alone(
        agent, fake_spawn, files, monkeypatch):
    """The window the freshness check cannot close: the other pass creates the
    marker between that check and this pass's own create."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    marker = os.path.join(old, "superseded")

    def fresh(run_dir):
        with open(marker, "w", encoding="utf-8") as f:  # the other pass, now
            f.write(str(time.time()))
        return False

    monkeypatch.setattr(agent, "_supersede_fresh", fresh)
    assert agent._end_idle_hosts(SID) == []
    assert fake_spawn["killed"] == []
    assert os.path.exists(marker), "the other pass's marker is not ours to remove"


def test_called_off_while_the_host_reaps_itself_is_sent_not_respawned(
        agent, files, monkeypatch):
    """The supersede is called off and the host's own idle reap lands in the
    same tick: its final drain delivered the message, so `respawn` would send
    it twice. The marker decides, not the liveness."""
    folder, note = files
    old = _run(agent, "20261014-100000-aaaaaa", note)
    marker = os.path.join(old, "superseded")
    with open(marker, "w", encoding="utf-8") as f:
        f.write(str(time.time()))
    alive = [True]

    def both():
        os.remove(marker)
        alive[0] = False

    monkeypatch.setattr(agent, "_host_alive", lambda run_dir: alive[0])
    threading.Timer(0.15, both).start()
    assert agent._superseded_outcome(old) is False
