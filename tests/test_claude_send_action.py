"""action=send: hand a follow-up to a live session's own inbox.

`_live_host` and `_send` are the pair Task 6's page will call before ever
falling back to `action=start` — this drives them against the same real
process tree `test_claude_session_host.py` does (a genuine `_start`, a real
session host, a stub `claude` binary), because the whole point of `_send` is
that the message it writes reaches an ALREADY-RUNNING process, not a mock.
"""
import importlib.util
import json
import os
import sys
import time

import pytest

from _claude_stub_cli import write_stub_cli

TEMPLATE_DIR = os.path.join("fused_render", "templates", "claude")


def _load_agent():
    path = os.path.join(TEMPLATE_DIR, "agent.py")
    spec = importlib.util.spec_from_file_location("claude_agent", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    mod = _load_agent()
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(mod, "RUNS", str(runs))
    return mod


_STUB = '''#!{python}
import json
import sys

def send(row):
    sys.stdout.write(json.dumps(row) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    row = json.loads(line)
    text = row["message"]["content"][0]["text"]
    send({{"type": "echo", "text": text}})
    send({{"type": "result", "session_id": "sess-stub", "result": "ok"}})
'''


@pytest.fixture()
def stub_cli(tmp_path):
    return write_stub_cli(tmp_path / "bin", _STUB.format(python=sys.executable))


@pytest.fixture()
def target(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    return str(f)


def _out_rows(run_dir):
    path = os.path.join(run_dir, "out.jsonl")
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def _wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _start(agent, monkeypatch, stub_cli, target, message="m1", read_dirs=None,
           **extra_env):
    monkeypatch.setenv("FUSED_RENDER_CLAUDE_BIN", stub_cli)
    for k, v in extra_env.items():
        monkeypatch.setenv(k, v)
    res = agent._start(target, message, "", "", "", has_pane=False,
                       extra_read_dirs=read_dirs or [])
    assert "run_id" in res, res
    return res["run_id"], os.path.join(agent.RUNS, res["run_id"])


def test_live_host_matches_a_running_session(agent, monkeypatch, stub_cli, target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target)
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))
    assert agent._live_host(target) == {"run_id": run_id}


def test_live_host_ignores_a_dead_session(agent, tmp_path):
    target = tmp_path / "orphan.txt"
    target.write_text("x")
    run_dir = os.path.join(agent.RUNS, "20260901-120000-aaa")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"file": str(target), "message": "hi", "mode": "prompt"}, f)
    # No host.json at all: nothing here ever spawned a host, or one already
    # reaped and removed it — either way, nothing to send into.
    assert agent._live_host(str(target)) == {"run_id": ""}


def test_send_writes_an_inbox_entry_the_stub_echoes(agent, monkeypatch, stub_cli,
                                                      target):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target, message="first")
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    result = agent._send(run_id, "second", "")
    assert result == {"sent": True}

    def both_echoed():
        texts = [r["text"] for r in _out_rows(run_dir) if r.get("type") == "echo"]
        return texts == ["first", "second"]

    assert _wait_for(both_echoed), _out_rows(run_dir)


def test_send_with_an_ungranted_dir_ends_the_session_and_asks_for_a_respawn(
        agent, monkeypatch, stub_cli, target, tmp_path):
    run_id, run_dir = _start(agent, monkeypatch, stub_cli, target, message="first")
    assert _wait_for(lambda: os.path.exists(os.path.join(run_dir, "host.json")))

    new_dir = tmp_path / "attachment"
    new_dir.mkdir()
    result = agent._send(run_id, "second", json.dumps([str(new_dir)]))
    assert result == {"respawn": True}
    # `_cancel`'s own marker — this IS the same tree-kill `action=cancel` uses.
    assert os.path.exists(os.path.join(run_dir, "cancelled"))
    assert _wait_for(lambda: not agent._alive(run_dir))


def test_send_against_a_dead_run_id_errors_instead_of_writing(agent, tmp_path):
    run_dir = os.path.join(agent.RUNS, "20260901-130000-bbb")
    os.makedirs(run_dir)
    result = agent._send("20260901-130000-bbb", "hello", "")
    assert "error" in result
    assert not os.path.isdir(os.path.join(run_dir, "inbox"))
