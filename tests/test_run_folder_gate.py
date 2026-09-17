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
    """One folder, one owner, and the atomic check-and-own the door now makes."""

    def __init__(self):
        self.owners: dict[str, dict] = {}
        self.claims: list[tuple] = []

    def own(self, folder, task, session_id="", run_id=""):
        self.owners[folder] = {"task": task, "session_id": session_id,
                               "run_id": run_id}
        return self

    def owner(self, folder):
        return self.owners.get(folder)

    def is_free(self, folder, task_key=""):
        owner = self.owners.get(folder)
        if owner is None:
            return True
        if not task_key:
            return False
        return task_key in (str(owner.get("task") or ""),
                            str(owner.get("run_id") or ""),
                            str(owner.get("session_id") or ""))

    def claim(self, folder, task_key, run_id="", session_id=""):
        """Atomic check-and-own, and — like the real one — an owner that is
        already this conversation keeps the names it was filed with."""
        self.claims.append((folder, task_key, run_id, session_id))
        owner = self.owners.get(folder)
        if owner is None:
            self.own(folder, task_key, session_id=session_id, run_id=run_id)
            return True
        if not any(self.is_free(folder, name)
                   for name in (task_key, run_id, session_id) if name):
            return False
        owner["run_id"] = owner.get("run_id") or run_id
        owner["session_id"] = owner.get("session_id") or session_id
        return True

    def started(self, folder, task_key, run_id="", session_id=""):
        self.own(folder, task_key, session_id=session_id, run_id=run_id)


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


# ------------------------------------------------- the gate owns what it lets
#
# H3/H4, 2026-09-17. Both doors here used to LOOK and act later — `_folder_busy`
# asked `is_free` and filed nobody, `_file_owner` filed only after a `start` had
# spawned, and a `send` filed nothing at all, ever. Every question asked in the
# gap was answered "the tree is free".


def test_the_gate_claims_the_folder_it_opens(gate):
    """A named send does not just pass the door — it takes the tree on the way
    through, in the one call. Nothing can slip between the two halves because
    there are no two halves."""
    manager = gate["manager"]
    assert run_router._folder_busy(
        AGENT, _params(session_id="sess-1", run_id="r-1")) == ""
    assert manager.claims == [("/w/alpha", "sess-1", "r-1", "sess-1")]
    assert manager.owner("/w/alpha")["task"] == "sess-1"
    # …and the next task really is behind it now
    assert run_router._folder_busy(AGENT, _params(session_id="sess-2",
                                                  run_id="r-2"))


def test_a_refused_claim_files_nobody_and_keeps_the_owner(gate):
    manager = gate["manager"]
    manager.own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    assert run_router._folder_busy(AGENT, _params(session_id="sess-b",
                                                  run_id="r-b"))
    assert manager.owner("/w/alpha")["task"] == "TASK-007"


def test_an_anonymous_first_send_claims_nothing(gate):
    """There is no name to own under — Claude Code has not minted the session
    and `_start` has not returned the run. The look is all the gate can do, and
    `_file_owner` files it the instant the spawn answers."""
    manager = gate["manager"]
    assert run_router._folder_busy(AGENT, _params()) == ""
    assert manager.claims == []
    assert manager.owner("/w/alpha") is None


def test_a_send_files_the_owner_when_it_lands(real_gate):
    """H4: a follow-up into an existing conversation ran a whole turn in a tree
    the index still read as free, because only `start` ever filed anybody."""
    params = _params(action="send", session_id="sess-1", run_id="r-2")
    run_router._file_owner(AGENT, params, _started("r-2", "sess-1"))

    owner = real_gate.owner("/w/alpha")
    assert (owner["task"], owner["run_id"], owner["session_id"]) == (
        "sess-1", "r-2", "sess-1")


def test_a_send_never_steals_a_folder_another_task_holds(gate):
    """The gate refused that case already; this is the receipt, and a receipt
    must not overwrite a live turn's owner."""
    manager = gate["manager"]
    manager.own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    run_router._file_owner(AGENT, _params(action="send", session_id="sess-b",
                                          run_id="r-b"),
                           _started("r-b", "sess-b"))
    assert manager.owner("/w/alpha")["task"] == "TASK-007"


def test_a_spawn_always_leaves_an_owner_behind(gate):
    """The one asymmetry. A `start` that returned a run id IS a process in that
    tree, whatever the index believed a moment ago — a live turn with no owner
    is the single state this index exists to prevent."""
    manager = gate["manager"]
    manager.own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    run_router._file_owner(AGENT, _params(), _started("r-9", "sess-9"))
    assert manager.owner("/w/alpha")["task"] == "sess-9"


# ------------------------------------------------------ M8: before the spawn


def test_the_folder_is_owned_before_the_run_starts(tmp_path, monkeypatch,
                                                   real_gate):
    """M8, 2026-09-17: the session host could post `turn_ended` for a turn the
    index had never been told about, because the owner was filed only once
    `/api/run` came back — and a turn can end (a refusal, a rate limit) before
    that. A send that names itself is filed by the GATE, which runs before the
    work is awaited."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app
    from fused_render.shell import prefs as shell_prefs

    seen = {}

    def fake_run_python(resolved, params):
        seen["owner"] = real_gate.owner("/w/alpha")
        return {"ok": True, "result": {"run_id": "r-1", "session_id": "sess-1"}}

    monkeypatch.setattr(run_router, "resolve_py", lambda py, html: (AGENT, None))
    monkeypatch.setattr(run_router, "run_python", fake_run_python)
    monkeypatch.setattr(shell_prefs, "effective_engine", lambda: "builtin")

    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/run", headers={"X-Fused": "1"},
                    json={"py": "agent.py",
                          "params": _params(action="send", session_id="sess-1",
                                            run_id="r-1")})

    assert r.status_code == 200
    assert seen["owner"] is not None, \
        "the turn ran in a folder the index still read as free"
    assert seen["owner"]["task"] == "sess-1"
