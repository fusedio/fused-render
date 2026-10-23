"""Listening ports of a task's session processes (Workspace scope, unit 4).

Off the list payload on purpose: one lsof per row per rebuild is too slow and a
port opening never touches the transcript. The Workspace asks for the rows it is
showing, through `POST /api/tasks/ports`, and the answer is cached ~10 s."""
from __future__ import annotations

import os
import shutil
import socket

import pytest
from fastapi.testclient import TestClient

from fused_render.server import task_ports
from fused_render.server.app import create_app
from fused_render.sysmon import detail


@pytest.fixture(autouse=True)
def _fresh_cache():
    task_ports.reset_cache()
    yield
    task_ports.reset_cache()


@pytest.mark.skipif(shutil.which("lsof") is None, reason="no lsof")
def test_lsof_ports_sees_a_real_listener_for_a_list_of_pids():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        port = s.getsockname()[1]
        assert port in detail.lsof_ports([os.getpid(), 1])
    finally:
        s.close()


def test_lsof_ports_of_nothing_spawns_nothing(monkeypatch):
    monkeypatch.setattr(detail.subprocess, "run",
                        lambda *a, **k: pytest.fail("spawned for no pids"))
    assert detail.lsof_ports([]) == []


def _runs(*pairs):
    return [{"run_id": f"r-{sid}", "run_dir": f"/runs/{sid}", "sessions": [sid]}
            for sid, _ in pairs]


class _Agent:
    def _alive(self, run_dir):
        return not run_dir.endswith("dead")


def test_ports_for_one_task_is_one_lsof_over_its_whole_tree(monkeypatch):
    calls = []
    monkeypatch.setattr(task_ports, "_run_pid", lambda run_dir: 100)
    monkeypatch.setattr(task_ports, "_tree", lambda pid: [pid, 101, 102])
    monkeypatch.setattr(task_ports.detail, "lsof_ports",
                        lambda pids: calls.append(list(pids)) or [5173, 8000])
    out = task_ports.ports_for(["s1"], _Agent(), _runs(("s1", 0)))
    assert out == {"s1": [5173, 8000]}
    assert calls == [[100, 101, 102]]


def test_a_task_without_a_live_run_has_no_entry(monkeypatch):
    monkeypatch.setattr(task_ports.detail, "lsof_ports",
                        lambda pids: pytest.fail("asked lsof for a dead run"))
    runs = [{"run_id": "r", "run_dir": "/runs/dead", "sessions": ["s1"]}]
    assert task_ports.ports_for(["s1", "unknown"], _Agent(), runs) == {}


def test_answers_are_cached_for_the_ttl(monkeypatch):
    n = []
    monkeypatch.setattr(task_ports, "_run_pid", lambda run_dir: 100)
    monkeypatch.setattr(task_ports, "_tree", lambda pid: [pid])
    monkeypatch.setattr(task_ports.detail, "lsof_ports",
                        lambda pids: n.append(1) or [3000])
    clock = [1000.0]
    monkeypatch.setattr(task_ports.time, "monotonic", lambda: clock[0])
    for _ in range(2):
        task_ports.ports_for(["s1"], _Agent(), _runs(("s1", 0)))
    assert len(n) == 1
    clock[0] += task_ports.TTL_S + 1
    task_ports.ports_for(["s1"], _Agent(), _runs(("s1", 0)))
    assert len(n) == 2


def test_no_sysmon_backend_means_no_ports(monkeypatch):
    monkeypatch.setattr(task_ports.sysmon, "backend", None)
    assert task_ports.ports_for(["s1"], _Agent(), _runs(("s1", 0))) == {}


def test_the_endpoint_answers_ports_by_key(tmp_path, monkeypatch):
    from fused_render.server.routers import tasks as tasks_mod
    client = TestClient(create_app(start_dir=str(tmp_path)))
    monkeypatch.setattr(tasks_mod, "_agent_module", lambda: object())
    monkeypatch.setattr(tasks_mod.project_queue, "scan_runs",
                        lambda agent=None, limit=None: [])
    monkeypatch.setattr(task_ports, "ports_for",
                        lambda ids, agent, runs: {"s1": [5173]})
    r = client.post("/api/tasks/ports", headers={"X-Fused": "1"},
                    json={"ids": ["s1", "s2"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"ports": {"s1": [5173]}}


def test_the_endpoint_is_guarded(tmp_path):
    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/tasks/ports", json={"ids": ["s1"]})
    assert r.status_code in (400, 403)
