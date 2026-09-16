"""`/api/run`'s own copy of the project queue's door (routers/run.py
`_folder_busy`). The chat asks `/api/tasks/queue/admit` before it spawns, but a
page whose copy of the pref was stale skipped the door and started a second run
in a busy folder (Akshil's QA, 2026-09-16). The server refuses that `start`/
`send` itself; only a LIVE run refuses, and a chat's own run is never refused.
"""
from __future__ import annotations

import pytest

from fused_render import project_queue
from fused_render.server.routers import run as run_router

AGENT = "/repo/fused_render/templates/claude/agent.py"


@pytest.fixture()
def gate(monkeypatch):
    state = {"enabled": True, "holder": None}
    monkeypatch.setattr(project_queue, "enabled", lambda: state["enabled"])
    monkeypatch.setattr(project_queue, "queue_key", lambda target: "/w/alpha" if target else "")
    monkeypatch.setattr(project_queue, "holder_for", lambda key, now=None: state["holder"])
    return state


def _params(**kw):
    p = {"action": "start", "_file": "/w/alpha/page.html", "session_id": "", "run_id": ""}
    p.update(kw)
    return p


def test_a_live_run_of_another_task_refuses_a_start(gate):
    gate["holder"] = {"kind": "run", "session_id": "sess-a", "run_id": "r-a", "task_key": "TASK-007"}
    assert "TASK-007" in run_router._folder_busy(AGENT, _params())
    assert run_router._folder_busy(AGENT, _params(action="send", session_id="sess-b", run_id="r-b"))


def test_the_chats_own_run_is_never_refused(gate):
    gate["holder"] = {"kind": "run", "session_id": "sess-a", "run_id": "r-a", "task_key": "TASK-007"}
    assert run_router._folder_busy(AGENT, _params(session_id="sess-a")) == ""
    assert run_router._folder_busy(AGENT, _params(action="send", run_id="r-a")) == ""


def test_an_announced_send_refuses_too(gate):
    # #1177's sent mark, or a scheduler claim: no process yet, but one is about
    # to be there. Another conversation's send is refused; the same session's
    # passes (a follow-up into the turn that was just announced).
    gate["holder"] = {"kind": "sending", "session_id": "sess-a", "run_id": "", "task_key": "sess-a"}
    assert run_router._folder_busy(AGENT, _params(session_id="sess-b"))
    assert run_router._folder_busy(AGENT, _params(session_id="sess-a")) == ""


def test_a_reservation_is_the_chats_own_admission_and_passes(gate):
    # An anonymous first send reserved a moment ago and now starts: no live
    # process, nothing to refuse.
    gate["holder"] = {"kind": "reserved", "session_id": "", "run_id": "", "task_key": ""}
    assert run_router._folder_busy(AGENT, _params()) == ""


def test_off_or_not_the_agent_or_not_a_send_is_always_open(gate):
    gate["holder"] = {"kind": "run", "session_id": "sess-a", "run_id": "r-a", "task_key": "TASK-007"}
    gate["enabled"] = False
    assert run_router._folder_busy(AGENT, _params()) == ""
    gate["enabled"] = True
    assert run_router._folder_busy("/repo/some/other/page.py", _params()) == ""
    assert run_router._folder_busy(AGENT, _params(action="poll")) == ""
    assert run_router._folder_busy(AGENT, _params(_file="")) == ""


def test_an_undecidable_gate_is_an_open_one(gate, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry unreadable")
    monkeypatch.setattr(project_queue, "holder_for", boom)
    assert run_router._folder_busy(AGENT, _params()) == ""
