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
        # PER-SEND CLAIM TOKENS (Bugbot, PR #1194, second round): what admit
        # would have handed back on `run: true`, mirrored here so a case can
        # present one as `queue_claim` — see `mint_claim`/`consume_claim`.
        self.tokens: dict[str, list[str]] = {}

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

    def claim_took(self, folder, task_key, run_id="", session_id=""):
        """Atomic check-and-own, and — like the real one — an owner that is
        already this conversation keeps the names it was filed with. `(ok,
        took)`, mirroring `QueueManager.claim_took` — what the gate calls for
        a send with no (or no longer good) `queue_claim`."""
        self.claims.append((folder, task_key, run_id, session_id))
        owner = self.owners.get(folder)
        if owner is None:
            self.own(folder, task_key, session_id=session_id, run_id=run_id)
            return True, True
        if not any(self.is_free(folder, name)
                   for name in (task_key, run_id, session_id) if name):
            return False, False
        owner["run_id"] = owner.get("run_id") or run_id
        owner["session_id"] = owner.get("session_id") or session_id
        return True, False

    def claim(self, folder, task_key, run_id="", session_id=""):
        return self.claim_took(folder, task_key, run_id, session_id)[0]

    def claim_for_send(self, folder, task_key, run_id="", session_id=""):
        """`claim_took`, plus the token — mirrors
        `QueueManager.claim_for_send`. Both admit and the gate's own
        tokenless-nameless-free-folder claim (fix 2, PR #1194 fourth round)
        go through this."""
        ok, took = self.claim_took(folder, task_key, run_id, session_id)
        if not ok:
            return ok, took, ""
        token = f"tok-{len(self.claims)}"
        self.tokens.setdefault(folder, []).append(token)
        return ok, took, token

    def mint_claim(self, folder, task_key, run_id="", session_id=""):
        """Test helper standing in for admit's `claim_for_send`: claims the
        folder exactly as `claim_took` does and hands back a token a case can
        present as `queue_claim`. "" when the claim failed."""
        _ok, _took, token = self.claim_for_send(folder, task_key, run_id,
                                                session_id)
        return token

    def consume_claim(self, folder, token):
        """`QueueManager.consume_claim`, mirrored: remove `token` once, False
        if it names nothing (already spent, or never minted)."""
        toks = self.tokens.get(folder)
        if not toks or token not in toks:
            return False
        toks.remove(token)
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
    # The nameless probe above now CLAIMS a placeholder (fix 2, PR #1194
    # fourth round) rather than only looking, so a fresh manager stands in
    # for a second, unrelated free folder here.
    gate["manager"] = _Manager()
    queue_manager.reset_for_tests(gate["manager"])
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
    the session Claude Code minted for it — goes straight through.

    `body` is threaded through both calls, the way `api_run` hands the SAME
    dict to `_folder_busy` and then `_file_owner` within one request — the
    carrier for the placeholder claim this gate call now mints (fix 2,
    Bugbot PR #1194, fourth round)."""
    body: dict = {}
    assert run_router._folder_busy(AGENT, _params(), body) == ""
    run_router._file_owner(AGENT, _params(), _started("r-1", "sess-1"), body)
    assert real_gate.owner("/w/alpha")["run_id"] == "r-1"

    assert run_router._folder_busy(AGENT, _params())
    assert run_router._folder_busy(AGENT, _params(action="send"))
    assert run_router._folder_busy(AGENT, _params(action="send", run_id="r-1")) == ""
    assert run_router._folder_busy(
        AGENT, _params(action="send", session_id="sess-1")) == ""
    assert run_router._folder_busy(
        AGENT, _params(action="send", session_id="sess-1", run_id="r-1")) == ""


def test_a_second_nameless_tokenless_start_is_refused_against_a_live_admission(
        real_gate):
    """CORRECTED 2026-09-17, Bugbot PR #1194, third round: `is_free` used to
    call a live `admit:` placeholder free to EVERY caller, so a second
    brand-new chat's first `/api/run start` — no session, no run, and no
    `queue_claim` because it never called admit at all — passed a folder
    another admission had already reserved, and BOTH ended up spawning. The
    first admission's `claim_for_send` (what `/api/tasks/queue/admit` does)
    reserves the placeholder the instant the composer asks; the gate must now
    refuse a second nameless stranger against that live reservation rather
    than reading it as an open folder."""
    _ok, _took, _token = real_gate.claim_for_send(
        "/w/alpha", queue_manager.PLACEHOLDER_PREFIX + "one")
    assert run_router._folder_busy(AGENT, _params())


def test_a_stranger_replacing_the_placeholder_voids_its_token(real_gate):
    """FAILING-FIRST for Bugbot PR #1194, fourth round (fix 1). Admit's
    `claim_for_send` mints a placeholder and a token for a brand-new chat's
    first send; before that send's own `/api/run` lands, a NAMED stranger's
    `claim_took` (a different chat's admit, or a tokenless send) replaces the
    placeholder. `_inherit_placeholder` used to copy the placeholder's
    unconsumed claims onto that stranger's fresh owner, so the ORIGINAL
    nameless send's token still consumed here — `admitted` alone then let it
    through and it spawned into a tree the stranger now owns. Once the
    stranger's claim drops rather than inherits those claims, the token no
    longer consumes and the nameless send's gate call must re-admit — with
    nothing to consume it falls to `is_free` and is refused."""
    _ok, _took, token = real_gate.claim_for_send(
        "/w/alpha", queue_manager.PLACEHOLDER_PREFIX + "one")
    real_gate.claim_took("/w/alpha", "sess-stranger", "r-stranger",
                         "sess-stranger")
    assert real_gate.consume_claim("/w/alpha", token) is False
    assert run_router._folder_busy(AGENT, _params(queue_claim=token))


def test_a_tokenless_nameless_start_claims_and_keeps_the_placeholder(
        real_gate):
    """FAILING-FIRST for Bugbot PR #1194, fourth round (fix 2). A tokenless
    nameless start on a FREE folder used to only LOOK (`is_free`) and pass
    without claiming anything — leaving the folder unowned while its spawn
    was in flight. Another chat's admission then found the folder free too
    and minted its own placeholder; when the first send's spawn came back,
    `started` refused to clobber that LIVE placeholder (it cannot prove it
    minted it), and the process the first send just started was left
    unowned while the second chat spawned into the same tree. The gate must
    now claim a placeholder itself, on the free folder, and hand its own
    token forward on `body` so `_file_owner` can consume-proof it to
    `started` before anybody else's admission gets a look."""
    body: dict = {}
    assert run_router._folder_busy(AGENT, _params(), body) == ""
    owner = real_gate.owner("/w/alpha")
    assert owner is not None
    assert owner["task"].startswith(queue_manager.PLACEHOLDER_PREFIX)
    assert body.get("_queue_admit_token")

    # A concurrent admission from another chat is refused/queued against
    # this live reservation, exactly like any other live placeholder.
    ok, _took, _tok = real_gate.claim_for_send(
        "/w/alpha", queue_manager.PLACEHOLDER_PREFIX + "other")
    assert ok is False

    # The spawn returns; `_file_owner` consumes the token this gate call
    # minted and `started` replaces the placeholder it proved it minted.
    run_router._file_owner(AGENT, _params(), _started("r-1", "sess-1"), body)
    owner = real_gate.owner("/w/alpha")
    assert (owner["task"], owner["run_id"], owner["session_id"]) == (
        "sess-1", "r-1", "sess-1")


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
#
# A PER-SEND CLAIM TOKEN DECIDES WHICH OF THE TWO THIS SEND GETS (Bugbot, PR
# #1194, second round). Claiming unconditionally double-counted every ordinary
# admit→run send (admit's own claim, this gate's, `_file_owner`'s refile on
# top); looking only reopened the window for a send that skipped admission
# outright (a stale flag read) to slip into a folder nobody had claimed for it.
# So: a `queue_claim` admit minted, and `consume_claim` still finds — this send
# was already counted, and the gate only LOOKS. No token, or one that no longer
# proves anything — this send never went through admit, and the gate CLAIMS the
# folder itself, exactly as it always had to before admission existed.


def test_an_admitted_send_only_looks(gate):
    """WITH a valid `queue_claim`, the gate never claims — admit already did,
    and `consume_claim` finding the token is proof enough. Only a LOOK,
    refusing if the owner has somehow changed since."""
    manager = gate["manager"]
    manager.own("/w/alpha", "sess-1", session_id="sess-1", run_id="r-1")
    token = manager.mint_claim("/w/alpha", "sess-1", "r-1", "sess-1")
    manager.claims.clear()  # admission's own claim attempt, not this gate's
    assert run_router._folder_busy(
        AGENT, _params(session_id="sess-1", run_id="r-1",
                      queue_claim=token)) == ""
    assert manager.claims == []
    # The token is spent: presenting it again finds nothing to consume, and
    # falls back to an ordinary tokenless claim attempt.
    assert manager.consume_claim("/w/alpha", token) is False


def test_a_tokenless_send_claims_the_folder(gate):
    """WITHOUT a `queue_claim` (a send that skipped admission), the gate
    claims the folder itself — exactly as a tokenless send always had to
    before admission existed. A different name than the one that took it is
    then refused."""
    manager = gate["manager"]
    assert run_router._folder_busy(
        AGENT, _params(session_id="sess-1", run_id="r-1")) == ""
    assert manager.claims == [("/w/alpha", "sess-1", "r-1", "sess-1")]
    assert manager.owner("/w/alpha")["task"] == "sess-1"
    # …and once something owns it, a different name is refused
    assert run_router._folder_busy(AGENT, _params(session_id="sess-2",
                                                  run_id="r-2"))


def test_a_tokenless_claim_that_finds_another_owner_is_refused(gate):
    """A tokenless send still cannot steal a folder another task holds —
    `claim_took` refuses it, and the owner is left exactly as it was."""
    manager = gate["manager"]
    manager.own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    assert run_router._folder_busy(AGENT, _params(session_id="sess-b",
                                                  run_id="r-b"))
    assert manager.owner("/w/alpha")["task"] == "TASK-007"
    assert manager.claims == [("/w/alpha", "sess-b", "r-b", "sess-b")]


def test_an_admitted_claim_that_finds_another_owner_is_refused(gate):
    """A `queue_claim` only proves the send was admitted once — not that it
    still holds. A folder somebody else has since taken still refuses it, and
    the gate never claims on the strength of a spent token."""
    manager = gate["manager"]
    manager.own("/w/alpha", "sess-1", session_id="sess-1", run_id="r-1")
    token = manager.mint_claim("/w/alpha", "sess-1", "r-1", "sess-1")
    manager.own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    assert run_router._folder_busy(
        AGENT, _params(session_id="sess-1", run_id="r-1", queue_claim=token))
    # The token is consumed on the way — it proved nothing more than that this
    # send once passed admission, and the gate never claims on its strength.
    assert manager.consume_claim("/w/alpha", token) is False


def test_an_anonymous_first_send_now_claims_a_placeholder(gate):
    """CORRECTED 2026-09-17, Bugbot PR #1194, fourth round (fix 2): there is
    no name to own under yet — Claude Code has not minted the session and
    `_start` has not returned the run — but a tokenless nameless start on a
    free folder must still CLAIM rather than only look, or a spawn it makes
    can be orphaned by another chat's admission landing in the gap and
    minting its own placeholder first. `_file_owner` consumes this gate's own
    token and refiles the real names the instant the spawn answers."""
    manager = gate["manager"]
    body: dict = {}
    assert run_router._folder_busy(AGENT, _params(), body) == ""
    assert len(manager.claims) == 1
    owner = manager.owner("/w/alpha")
    assert owner is not None
    assert owner["task"].startswith(queue_manager.PLACEHOLDER_PREFIX)
    assert body.get("_queue_admit_token")


def test_a_send_files_the_owner_when_it_lands(real_gate):
    """H4: a follow-up into an existing conversation ran a whole turn in a tree
    the index still read as free, because only `start` ever filed anybody."""
    params = _params(action="send", session_id="sess-1", run_id="r-2")
    run_router._file_owner(AGENT, params, _started("r-2", "sess-1"))

    owner = real_gate.owner("/w/alpha")
    assert (owner["task"], owner["run_id"], owner["session_id"]) == (
        "sess-1", "r-2", "sess-1")


def test_a_send_also_refiles_over_a_different_owner_now(gate):
    """Bugbot, PR #1194: `_file_owner` no longer re-checks ownership for a
    `send` — that used to call `claim`, a second increment of `owner.turns`
    for the one send `_folder_busy`'s gate had already looked at and admit had
    already claimed once. Protection against stealing a folder now lives
    entirely in the gate, which runs BEFORE the turn spawns; by the time a
    `send` reaches here the turn already happened; this only writes its name
    down — the same trust `start` has always been given."""
    manager = gate["manager"]
    manager.own("/w/alpha", "TASK-007", session_id="sess-a", run_id="r-a")
    run_router._file_owner(AGENT, _params(action="send", session_id="sess-b",
                                          run_id="r-b"),
                           _started("r-b", "sess-b"))
    assert manager.owner("/w/alpha")["task"] == "sess-b"


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
    """M8, 2026-09-17, updated for Bugbot PR #1194 (second round): the
    guarantee that a turn's owner is on record before it can end now comes
    from admit's `claim_for_send` — the door the native chat calls before
    `/api/run`, simulated here the way it would be for an ordinary send — and
    the TOKEN it hands back, echoed on the run request as `queue_claim`. A
    send that already claimed the folder at admission still finds it owned
    the moment `run_python` runs, and the gate consuming that claim (a LOOK,
    not another claim) must not add a second turn on top of admit's one."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app
    from fused_render.shell import prefs as shell_prefs

    _ok, _took, token = real_gate.claim_for_send(
        "/w/alpha", "sess-1", "r-1", "sess-1")  # what admit did

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
                                            run_id="r-1", queue_claim=token)})

    assert r.status_code == 200
    assert seen["owner"] is not None, \
        "the turn ran in a folder the index still read as free"
    assert seen["owner"]["task"] == "sess-1"
    assert real_gate.owner("/w/alpha")["turns"] == 1, \
        "the gate consuming admit's claim and the refile after the spawn " \
        "must not add a turn"


def test_a_named_send_through_api_run_keeps_one_turn(tmp_path, monkeypatch,
                                                      real_gate):
    """Bugbot, PR #1194: one send crossing admit, `_folder_busy` and
    `_file_owner` used to increment `owner.turns` three times — admit's
    `claim`, the gate's own (now removed) claim, and `_file_owner`'s (now a
    refile). The SECOND round found the same bug's mirror image: a gate that
    never claims lets a send that skipped admission through unchecked. A
    `queue_claim` token tells the two apart — WITH one, the gate only looks,
    and a single `turn_ended` still frees the folder after one send."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app
    from fused_render.shell import prefs as shell_prefs

    _ok, _took, token = real_gate.claim_for_send(
        "/w/alpha", "sess-1", "r-1", "sess-1")  # what admit did

    monkeypatch.setattr(run_router, "resolve_py", lambda py, html: (AGENT, None))
    monkeypatch.setattr(
        run_router, "run_python",
        lambda resolved, params: {
            "ok": True, "result": {"run_id": "r-1", "session_id": "sess-1"}})
    monkeypatch.setattr(shell_prefs, "effective_engine", lambda: "builtin")

    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/run", headers={"X-Fused": "1"},
                    json={"py": "agent.py",
                          "params": _params(action="send", session_id="sess-1",
                                            run_id="r-1", queue_claim=token)})

    assert r.status_code == 200
    assert real_gate.owner("/w/alpha")["turns"] == 1

    real_gate.turn_ended("sess-1", "r-1")
    assert real_gate.owner("/w/alpha") is None


def test_a_tokenless_named_send_claims_a_free_folder_and_blocks_a_second(
        tmp_path, monkeypatch, real_gate):
    """WITHOUT a `queue_claim` — this send skipped admission outright, a
    stale flag read — the gate claims the folder itself: one turn for the
    first send, and a second tokenless send naming a different conversation
    into the same folder is refused rather than spawning beside it."""
    from fastapi.testclient import TestClient

    from fused_render.server import create_app
    from fused_render.shell import prefs as shell_prefs

    monkeypatch.setattr(run_router, "resolve_py", lambda py, html: (AGENT, None))
    monkeypatch.setattr(
        run_router, "run_python",
        lambda resolved, params: {
            "ok": True, "result": {"run_id": "r-1", "session_id": "sess-1"}})
    monkeypatch.setattr(shell_prefs, "effective_engine", lambda: "builtin")

    client = TestClient(create_app(start_dir=str(tmp_path)))
    r = client.post("/api/run", headers={"X-Fused": "1"},
                    json={"py": "agent.py",
                          "params": _params(action="send", session_id="sess-1",
                                            run_id="r-1")})
    assert r.status_code == 200
    assert real_gate.owner("/w/alpha")["turns"] == 1

    r2 = client.post("/api/run", headers={"X-Fused": "1"},
                     json={"py": "agent.py",
                           "params": _params(action="start", session_id="sess-2",
                                             run_id="r-2")})
    assert r2.status_code == 200
    assert r2.json()["result"].get("error"), \
        "a second tokenless send naming somebody else was not refused"


def test_a_failed_nameless_start_gives_its_placeholder_back(real_gate):
    """Bugbot PR #1194: the gate mints a placeholder for a tokenless nameless
    start, and a start that then FAILS produced nothing to own the folder with.
    Left standing, it locked the folder for `PLACEHOLDER_TTL` against the user's
    own retry. The failure path releases it, so the retry is admitted."""
    body: dict = {}
    assert run_router._folder_busy(AGENT, _params(), body) == ""
    assert body.get("_queue_admit_token")
    assert real_gate.owner("/w/alpha") is not None
    run_router._file_owner(AGENT, _params(), {"ok": True, "result": {"error": "boom"}}, body)
    assert real_gate.owner("/w/alpha") is None
    retry: dict = {}
    assert run_router._folder_busy(AGENT, _params(), retry) == ""


def test_an_empty_start_result_gives_its_placeholder_back_too(real_gate):
    """Bugbot PR #1194: a start that answers a dict with neither an error nor
    a run/session id produced nothing to own the folder with either."""
    body: dict = {}
    assert run_router._folder_busy(AGENT, _params(), body) == ""
    assert real_gate.owner("/w/alpha") is not None
    run_router._file_owner(AGENT, _params(), {"ok": True, "result": {}}, body)
    assert real_gate.owner("/w/alpha") is None
