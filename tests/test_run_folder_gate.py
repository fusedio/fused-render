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

from fused_render import project_queue, queue_manager, tasks_store
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


# ------------------------------------------------- the anonymous first send


@pytest.fixture()
def real_gate(tmp_path, monkeypatch):
    """The gate over a REAL manager, because what is under test is the pair —
    `_file_owner` filing the owner and `_folder_busy` reading it back. A fake
    that answers both sides could agree with itself about a rule neither
    implements."""
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(project_queue, "enabled", lambda: True)
    monkeypatch.setattr(project_queue, "queue_key",
                        lambda target: "/w/alpha" if target else "")
    manager = queue_manager.QueueManager(
        spawn=lambda folder, key: None, deliver=lambda answer: None,
        running=lambda key: False, blocked=lambda key: False,
        pending_due=lambda: [])
    queue_manager.reset_for_tests(manager)
    yield manager
    queue_manager.reset_for_tests(None)


def _started(run_id, session_id=""):
    """What `/api/run` hands back from a claude-agent `start`."""
    return {"ok": True, "result": {"run_id": run_id, "session_id": session_id}}


def test_a_second_anonymous_start_is_refused_once_the_first_has_spawned(real_gate):
    """A brand-new chat's first send names nothing — no session, no run — so the
    admission next door filed nobody and the folder still read free. The SPAWN
    is where both names exist, so that is where the owner is filed (T3's
    handoff, 2026-09-17): a second nameless send into the same folder is now
    behind it, and the same chat's next message — carrying the run it started or
    the session Claude Code minted for it — goes straight through."""
    assert run_router._folder_busy(AGENT, _params()) == ""
    run_router._file_owner(AGENT, _params(), _started("r-1", "sess-1"))
    assert real_gate.owner("/w/alpha")["run_id"] == "r-1"

    assert run_router._folder_busy(AGENT, _params())
    assert run_router._folder_busy(AGENT, _params(action="send"))
    assert run_router._folder_busy(AGENT, _params(action="send", run_id="r-1")) == ""
    assert run_router._folder_busy(
        AGENT, _params(action="send", session_id="sess-1")) == ""
    assert run_router._folder_busy(
        AGENT, _params(action="send", session_id="sess-1", run_id="r-1")) == ""


def test_a_start_that_mints_no_session_is_still_filed_under_its_run(real_gate):
    """The run id is a name on its own, and `is_free` answers to it."""
    run_router._file_owner(AGENT, _params(), _started("r-1"))
    owner = real_gate.owner("/w/alpha")
    assert (owner["task"], owner["run_id"]) == ("r-1", "r-1")
    assert run_router._folder_busy(AGENT, _params(run_id="r-1")) == ""
    assert run_router._folder_busy(AGENT, _params(run_id="r-2"))


def test_nothing_is_filed_for_a_start_that_did_not_start(real_gate):
    """A refusal, a poll, a non-agent page and the flag being off all file
    nobody — a gate closed by a run that never happened is worse than no gate."""
    run_router._file_owner(AGENT, _params(), {"result": {"error": "(empty message)"}})
    run_router._file_owner(AGENT, _params(), {"result": {"run_id": ""}})
    run_router._file_owner(AGENT, _params(action="poll"), _started("r-1"))
    run_router._file_owner("/repo/other/page.py", _params(), _started("r-1"))
    run_router._file_owner(AGENT, _params(_file=""), _started("r-1"))
    assert real_gate.owner("/w/alpha") is None


def test_filing_the_owner_never_breaks_a_run_that_already_started(monkeypatch,
                                                                  real_gate):
    def boom(*a, **k):
        raise RuntimeError("index unreadable")
    monkeypatch.setattr(queue_manager, "get", boom)
    run_router._file_owner(AGENT, _params(), _started("r-1", "sess-1"))
