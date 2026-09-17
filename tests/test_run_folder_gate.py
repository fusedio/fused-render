"""`/api/run`'s own copy of the project queue's door (routers/run.py
`_folder_busy`). The chat asks `/api/tasks/queue/admit` before it spawns, but a
page whose copy of the pref was stale skipped the door and started a second run
in a busy folder (Akshil's QA, 2026-09-16). The server refuses that `start`/
`send` itself, and a chat's own run is never refused.

WHO OWNS THE FOLDER IS THE MANAGER'S ANSWER (PR 2, 2026-09-17), the same record
the admission next door reads a moment earlier. This used to re-derive it from
the runs tree, the registry and the scheduler store — a second answer to one
question, which could differ from the admission in the same second.
"""
from __future__ import annotations

import pytest

from fused_render import project_queue, queue_manager
from fused_render.server.routers import run as run_router

AGENT = "/repo/fused_render/templates/claude/agent.py"


class _Manager:
    """One folder, one owner — the two reads this door makes."""

    def __init__(self):
        self.owners: dict[str, dict] = {}

    def own(self, folder, task, session_id="", run_id=""):
        self.owners[folder] = {"task": task, "session_id": session_id,
                               "run_id": run_id}
        return self

    def owner(self, folder):
        return self.owners.get(folder)

    def is_free(self, folder, task_key=""):
        owner = self.owners.get(folder)
        return owner is None or (bool(task_key)
                                 and str(owner.get("task") or "") == task_key)


@pytest.fixture()
def gate(monkeypatch):
    state = {"enabled": True, "manager": _Manager()}
    monkeypatch.setattr(project_queue, "enabled", lambda: state["enabled"])
    monkeypatch.setattr(project_queue, "queue_key",
                        lambda target: "/w/alpha" if target else "")
    queue_manager.reset_for_tests(state["manager"])
    yield state
    queue_manager.reset_for_tests(None)


def _params(**kw):
    p = {"action": "start", "_file": "/w/alpha/page.html", "session_id": "", "run_id": ""}
    p.update(kw)
    return p


def test_a_run_of_another_task_refuses_a_start(gate):
    gate["manager"].own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    assert "TASK-007" in run_router._folder_busy(AGENT, _params())
    assert run_router._folder_busy(
        AGENT, _params(action="send", session_id="sess-b", run_id="r-b"))


def test_the_chats_own_run_is_never_refused(gate):
    """Three names for one conversation and any of them is enough: the owner's
    task key, its session, and the run it is — which is the only name a chat
    that has not minted a session yet has to offer."""
    gate["manager"].own("/w/alpha", "sess-a", session_id="sess-a", run_id="r-a")
    assert run_router._folder_busy(AGENT, _params(session_id="sess-a")) == ""
    assert run_router._folder_busy(AGENT, _params(action="send", run_id="r-a")) == ""


def test_an_anonymous_owner_is_recognised_by_its_run(gate):
    """A new chat's first send is admitted before Claude Code mints a session,
    so the owner is filed under the RUN. The second message carries that run and
    must not be told it is behind itself (Akshil, 2026-09-12)."""
    gate["manager"].own("/w/alpha", "r-a", session_id="", run_id="r-a")
    assert run_router._folder_busy(AGENT, _params(run_id="r-a")) == ""
    assert run_router._folder_busy(AGENT, _params(session_id="sess-b", run_id="r-b"))


def test_a_free_folder_is_always_open(gate):
    assert run_router._folder_busy(AGENT, _params()) == ""
    assert run_router._folder_busy(AGENT, _params(session_id="sess-b")) == ""


def test_off_or_not_the_agent_or_not_a_send_is_always_open(gate):
    gate["manager"].own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    gate["enabled"] = False
    assert run_router._folder_busy(AGENT, _params()) == ""
    gate["enabled"] = True
    assert run_router._folder_busy("/repo/some/other/page.py", _params()) == ""
    assert run_router._folder_busy(AGENT, _params(action="poll")) == ""
    assert run_router._folder_busy(AGENT, _params(_file="")) == ""


def test_an_undecidable_gate_is_an_open_one(gate, monkeypatch):
    """An index that will not read costs a gate, never a send."""
    def boom(*a, **k):
        raise RuntimeError("index unreadable")
    monkeypatch.setattr(queue_manager, "get", boom)
    assert run_router._folder_busy(AGENT, _params()) == ""
