"""One task in progress per folder, over HTTP (server/routers/tasks.py).

The router's half of the project queue: the `queued` status and the four fields
that say where a row stands, the three verbs the client calls (`admit`, `force`,
`decide`), run-now's queued answer next door, and the two flag-agnostic wins
that came with them (a scoped `gone` on the changes long-poll, and a notify ring
on the read endpoint).

BOTH FLAG STATES, everywhere it can differ. The whole feature is behind
`project_queue_enabled` (prefs, default off) and the promise is that with the
flag off every path behaves exactly as it did before it existed: `queued` never
appears, admission always says run, and nothing is stored.

Who HOLDS a folder is `queue_manager.owner()` — an event-driven index with its
own suite (tests/test_queue_manager.py). Here it is stood in for (`FakeManager`,
`_holders`), because what is under test is what the router does with the answer.

Nothing reads the real ~/.claude or the developer's prefs — every path is under
tmp_path.
"""
import json
import os
import time
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from fused_render import (
    drafts,
    project_queue,
    queue_manager,
    schedule,
    tasks_store,
    tasks_watch,
)
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod

HEADERS = {"X-Fused": "1"}

# The name an admission files a NAMELESS send under (`queue_manager`).
PLACEHOLDER = queue_manager.PLACEHOLDER_PREFIX


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A machine of our own. `HOME` as well as `FUSED_RENDER_HOME`, because
    `queue_key` refuses the user's home as a key and resolves it through
    `expanduser` — a test folder that happened to sit under the developer's real
    home would key differently on their machine than in CI."""
    house = tmp_path / "home"
    (house / ".fused-render").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(house))
    monkeypatch.setenv("USERPROFILE", str(house))
    monkeypatch.setenv("FUSED_RENDER_HOME", str(house / ".fused-render"))
    return house


@pytest.fixture(autouse=True)
def projects_dir(tmp_path, monkeypatch):
    d = tmp_path / "claude-projects"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "PROJECTS_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "PROJECTS_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state" / "claude-sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    monkeypatch.setattr(sessions_mod, "STATE_DIR", str(d))
    # ...AND THE DRAFT STORE, which shares the dir. `drafts.STATE_DIR` is derived
    # from the env at import, like the two above, so conftest's per-RUN tmp home
    # is one store for every test in the process — a draft written by one case
    # would be listed (and numbered) by the next (tests/test_drafts.py redirects
    # it in exactly this seat, for exactly this reason).
    monkeypatch.setattr(drafts, "STATE_DIR", str(d))
    return d


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both caches, both ends. The router memoizes transcript scans; the queue
    memoizes folder keys, the agent module and the live reservations — all facts
    it is entitled to believe never change inside one process, and all of which
    every case here moves."""
    tasks_mod.reset_cache()
    project_queue.reset_cache()
    yield
    tasks_mod.reset_cache()
    project_queue.reset_cache()


@pytest.fixture(autouse=True)
def no_real_wake(monkeypatch):
    from fused_render import schedule_wake
    monkeypatch.setattr(schedule_wake, "sync", lambda due: None)


@pytest.fixture(autouse=True)
def no_agent(monkeypatch):
    """No claude template by default: nothing is parked and no run holds
    anything, which is the state every case here starts from and then says how it
    differs."""
    monkeypatch.setattr(project_queue, "agent_module", lambda: None)


def _page_names(*names):
    """The thing in front, spelled every way the page might have filed it —
    `queue_manager._page_names`'s rule, stated here the way the fake states
    every other read: best first, no blanks, no repeats, and never the
    `admit:<token>` of a placeholder, which names nothing anywhere."""
    out = []
    for name in names:
        name = str(name or "")
        if name and not name.startswith("admit:") and name not in out:
            out.append(name)
    return out


class FakeManager:
    """The queue manager, standing in for the real index.

    WHERE A TASK STANDS IS NOT DERIVED HERE ANY MORE (PR 2, 2026-09-17): the
    order is an event-driven table in `fused_render/queue_manager.py` with its
    own suite (tests/test_queue_manager.py), and the router's job is to READ it.
    So the cases below STATE the line as a fact — `manager.line(folder, "a",
    "b", holder="c")` — and assert what the row, the status and the endpoints
    make of it. A suite that staged stamps and held answers to provoke an order
    would be testing the layer this PR deletes, in the file that no longer owns
    it.

    The events are here too, and they do the things the contract says and
    nothing else: `enqueue` appends to a folder's line, `skip` moves a task to
    index 0 and marks it promoted. That is enough for a door to be tested end
    to end (press, then read the row), and it is deliberately not a second
    implementation of the manager — every rule about idempotency, blocked
    lists, reconcile and persistence belongs to its own suite.

    `skip` HAS NO DOOR OF ITS OWN ANY MORE (`/api/tasks/queue/skip` deleted,
    2026-09-22), but it is not dead: `schedule._run_now_managed`, behind
    `/api/schedule/run-now` and exercised by this file too, calls
    `manager.skip` directly when a Run now has to wait on a busy folder — a
    deferred Run now IS a promotion to the head of the line, just without a
    button of its own.
    """

    def __init__(self):
        self.lines: dict[str, list[str]] = {}
        self.owners: dict[str, dict | None] = {}
        self.promoted: set[str] = set()
        self.answers: dict[str, dict] = {}
        # WHICH MESSAGE PUTS A TASK IN A LINE, which is the one thing
        # `forget_entry` needs and `remove` deliberately does not know
        # (`queue_manager.forget_entry` drops an ITEM, never a task).
        self.entry_of: dict[str, str] = {}
        # THE TASKS FORCE START TOOK OUT OF THE QUEUE FOR GOOD
        # (`queue_manager.mark_forced`) — every name they answer to, which is
        # what the two doors and the run gate read.
        self.forced: set[str] = set()
        self.events: list[tuple] = []

    # -- the way a case states a line ------------------------------------
    def line(self, folder, *task_keys, holder="", priority=(),
             holder_session=None, holder_run=None, entries=None):
        """`folder`'s line, in order: `task_keys[0]` is #1.

        `holder` is the task key that OWNS the folder — what position 1 stands
        behind, and what makes the folder busy — and `priority` the keys that
        got where they are by a skip or an answer, which is the only thing that
        lights `queue_priority`. No keys at all is a folder that is owned with
        nobody waiting on it yet.

        `holder_session` / `holder_run` state the holder's OTHER names where a
        case needs them to differ from its key — a brand-new chat the index
        still files under its run id, a dispatched message filed under
        `pending:<entry>`. By default they are the same name spelled the way the
        ordinary case spells it, which is what every case that says nothing
        means."""
        self.lines[folder] = list(task_keys)
        self.promoted.update(priority)
        # `{task key: entry id}` where a case needs the MESSAGE behind a place —
        # what `forget_entry` names. Stated, because the real index learns it on
        # the enqueue this case is standing in for.
        self.entry_of.update(entries or {})
        session = (("" if holder.startswith("pending:") else holder)
                   if holder_session is None else holder_session)
        self.owners[folder] = ({"task": holder, "task_key": holder,
                                "session_id": session,
                                "run_id": ("r-" + holder if holder_run is None
                                           else holder_run),
                                "since": time.time()}
                               if holder else None)
        return self

    def hold(self, task_key, run_id="", request_id="", raw=None):
        """An answer the user has given that the manager is holding until the
        folder frees — what `_parked_runs` asks about per run."""
        self.answers[task_key] = {"run_id": run_id, "request_id": request_id,
                                  "raw": raw or {}, "at": time.time()}
        return self

    def _folder_of(self, task_key):
        for folder, keys in self.lines.items():
            if task_key in keys:
                return folder
        return ""

    # -- the reads the router makes --------------------------------------
    def positions(self):
        out = {}
        for folder, keys in self.lines.items():
            owner = self.owners.get(folder) or {}
            ahead = str(owner.get("task") or "")
            names = _page_names(owner.get("task"), owner.get("session_id"),
                                owner.get("run_id"))
            for position, task_key in enumerate(keys, start=1):
                out[task_key] = {
                    "key": folder, "position": position, "ahead_key": ahead,
                    "ahead_names": names,
                    "priority": position == 1 and task_key in self.promoted}
                ahead = task_key
                names = _page_names(task_key)
        return out

    def place(self, task_key):
        spot = self.positions().get(task_key)
        if spot is None:
            return {"position": 0, "ahead_key": ""}
        return dict(spot)

    def held_answer(self, task_key):
        answer = self.answers.get(task_key)
        return dict(answer) if answer else None

    def held_answers(self, task_key):
        """The whole list, which is what a door that DELIVERS reads — the fake
        holds at most one per task (`hold` is keyed by task), and that is enough
        for a door to be tested: how several answers stack on one task is
        `queue_manager`'s subject."""
        answer = self.answers.get(task_key)
        return [dict(answer)] if answer else []

    def holds_live(self, task_key):
        """`queue_manager.QueueManager.holds_live` — is a RUN of this task in
        flight: it owns a folder, or a decision is held for it, and the status
        sync agrees the process is still there.

        The fake asks the router's own sync (`_queue_running`/`_queue_blocked`,
        what the real manager is wired to), so a case says "this run is alive"
        the way every other case here does — with a real run dir (`park`) or a
        registry row — rather than by setting a flag on the fake. A task merely
        standing in a line holds nothing, which is the whole distinction the
        delete door needs."""
        records = [owner for owner in self.owners.values()
                   if owner and task_key in self._names(owner)]
        answer = self.answers.get(task_key)
        if answer:
            records.append({"task": task_key, "run_id": answer["run_id"],
                            "session_id": task_key})
        return any(tasks_mod._queue_running(record)
                   or tasks_mod._queue_blocked(record)
                   for record in records)

    def owner(self, folder):
        return self.owners.get(folder)

    def is_free(self, folder, task_key=""):
        owner = self.owners.get(folder)
        if owner is None:
            return True
        if str(owner.get("task") or "").startswith(PLACEHOLDER):
            # A placeholder is an ADMISSION, not a run: it settles a race
            # between two nameless sends and never refuses one at the gate.
            return True
        return bool(self._names(owner) & ({task_key} - {""}))

    @staticmethod
    def _names(owner):
        return {str((owner or {}).get(field) or "")
                for field in ("task", "run_id", "session_id")} - {""}

    def claim(self, folder, task_key, run_id="", session_id=""):
        """The check and the filing under one lock — what a door that is about
        to ACT on the answer asks instead of `is_free` then `started`."""
        self.events.append(("claim", folder, task_key, run_id, session_id))
        if not folder or not task_key:
            return False
        owner = self.owners.get(folder)
        if owner is not None:
            names = self._names(owner)
            if {task_key, run_id, session_id} - {""} & names:
                return True
            if not (str(owner.get("task") or "").startswith(PLACEHOLDER)
                    and not task_key.startswith(PLACEHOLDER)):
                return False
        self.remove_from_line(task_key)
        self.owners[folder] = {"task": task_key, "task_key": task_key,
                               "session_id": session_id or task_key,
                               "run_id": run_id, "since": time.time()}
        return True

    def claim_for_send(self, folder, task_key, run_id="", session_id=""):
        """`claim`, plus the fresh per-send claim token that call recorded on
        the owner (Bugbot, PR #1194) — mirrors
        `queue_manager.QueueManager.claim_for_send`. "" when `claim` refused.
        `took` is not something admit reads (the door only checks `ok` and the
        token), so this answers `ok` again there rather than re-deriving it."""
        ok = self.claim(folder, task_key, run_id, session_id)
        if not ok:
            return False, False, ""
        owner = self.owners.get(folder) or {}
        token = f"claim-{len(self.events)}"
        owner.setdefault("claims", []).append(token)
        return True, True, token

    def consume_claim(self, folder, token):
        """Remove `token` from `folder`'s owner, once — what the run gate
        calls to tell an admitted send from one that skipped admission."""
        owner = self.owners.get(folder)
        claims = owner.get("claims") if owner else None
        if not claims or token not in claims:
            return False
        claims.remove(token)
        return True

    # -- the events a door fires -----------------------------------------
    def enqueue(self, folder, task_key, entry_id=""):
        self.events.append(("enqueue", folder, task_key, entry_id))
        keys = self.lines.setdefault(folder, [])
        if task_key not in keys:
            keys.append(task_key)
        if entry_id:
            self.entry_of[task_key] = entry_id
        return self.place(task_key)

    def forget_entry(self, entry_id):
        """ONE MESSAGE leaves every line — AND NO OWNER IS TOUCHED, which is the
        whole difference from `remove` (`queue_manager.forget_entry`).

        A task that stands NOWHERE afterwards loses its held answers with it
        (`queue_manager._forget_answers`), mirrored here because a door that
        reads them has to read them BEFORE it forgets (Bugbot, 2026-09-21)."""
        self.events.append(("forget_entry", entry_id))
        if not entry_id:
            return
        for task_key, named in list(self.entry_of.items()):
            if named != entry_id:
                continue
            self.remove_from_line(task_key)
            self.entry_of.pop(task_key, None)
            if not self._folder_of(task_key) and not self._owned_folder(task_key):
                self.answers.pop(task_key, None)

    def skip(self, task_key):
        """STILL LIVE (2026-09-22): `/api/tasks/queue/skip` is gone, but
        `_run_now_managed` (schedule.py, behind `/api/schedule/run-now`, tested
        in this file too) calls `manager.skip` directly when a Run now has to
        wait on a busy folder — promoting the task to the head of its line is
        still exactly what a deferred Run now does."""
        self.events.append(("skip", task_key))
        folder = self._folder_of(task_key)
        started = False
        if folder:
            keys = self.lines[folder]
            keys.remove(task_key)
            keys.insert(0, task_key)
            self.promoted.add(task_key)
            if self.owners.get(folder) is None:
                # The folder was free: the pump handed it straight over, and
                # this task stands in no line at all now.
                keys.remove(task_key)
                self.owners[folder] = {"task": task_key, "task_key": task_key,
                                       "session_id": task_key, "run_id": "",
                                       "since": time.time()}
                started = True
        return {**self.place(task_key), "started": started}

    def remove(self, task_key):
        self.events.append(("remove", task_key))
        self.entry_of.pop(task_key, None)
        self.remove_from_line(task_key)
        self.promoted.discard(task_key)
        self.answers.pop(task_key, None)
        self._release(task_key)

    def learn_forced(self, *names):
        clean = [n for n in names if n]
        if len(clean) < 2 or not any(n in self.forced for n in clean):
            return False
        self.events.append(("learn_forced", *clean))
        self.forced.update(clean)
        return True

    def forget_forced(self, *names):
        self.events.append(("forget_forced", *names))
        for name in names:
            self.forced.discard(name)

    def _release(self, task_key):
        for folder, owner in list(self.owners.items()):
            if owner and str(owner.get("task") or "") == task_key:
                self.owners[folder] = None

    def pump(self, folder):
        self.events.append(("pump", folder))

    def started(self, folder, task_key, run_id="", session_id=""):
        """The one spawn site says who owns the folder now — which is what makes
        the NEXT send into it queue rather than run."""
        self.events.append(("started", folder, task_key, run_id, session_id))
        if not folder or not task_key:
            # A brand-new chat's first send names neither a session nor a run,
            # so there is no task to file — the real manager returns here too.
            return
        self.remove_from_line(task_key)
        self.owners[folder] = {"task": task_key, "task_key": task_key,
                               "session_id": session_id or task_key,
                               "run_id": run_id, "since": time.time()}

    def remove_from_line(self, task_key):
        for keys in self.lines.values():
            if task_key in keys:
                keys.remove(task_key)

    def mark_forced(self, *names):
        self.events.append(("mark_forced", tuple(n for n in names if n)))
        self.forced.update(str(n) for n in names if n)

    def is_forced(self, *names):
        return any(str(n) in self.forced for n in names if n)

    def card_raised(self, task_key, run_id=""):
        self.events.append(("card_raised", task_key, run_id))

    def card_answered(self, task_key, run_id, request_id, raw):
        """Held only when somebody ELSE owns the folder — an answer to the run
        in flight goes straight through, which is the decide door's whole
        fork."""
        self.events.append(("card_answered", task_key, run_id, request_id))
        folder = self._folder_of(task_key) or self._owned_folder(task_key)
        owner = self.owners.get(folder) if folder else None
        if owner is None or str(owner.get("task") or "") == task_key:
            return {"held": False, "position": 0}
        standing = self.answers.get(task_key)
        if (standing is not None and standing["run_id"] == run_id
                and standing["request_id"] == request_id):
            # First writer wins on `(run_id, request_id)`, like the real one: a
            # double-click is two calls about one question.
            return {"held": True, "position": max(1, self.place(task_key)["position"])}
        self.hold(task_key, run_id=run_id, request_id=request_id, raw=raw)
        keys = self.lines.setdefault(folder, [])
        if task_key in keys:
            keys.remove(task_key)
        keys.insert(0, task_key)
        self.promoted.add(task_key)
        return {"held": True, "position": 1}

    def _owned_folder(self, task_key):
        for folder, owner in self.owners.items():
            if owner and str(owner.get("task") or "") == task_key:
                return folder
        return ""

    def turn_ended(self, task_key, run_id=""):
        self.events.append(("turn_ended", task_key, run_id))
        self._release(task_key)

    def exited(self, task_key, run_id="", code=None):
        self.events.append(("exited", task_key, run_id, code))
        self._release(task_key)

    def reconcile(self):
        self.events.append(("reconcile",))

    def snapshot(self):
        return {"folders": {f: {"owner": self.owners.get(f), "line": list(k),
                                "blocked": []}
                            for f, k in self.lines.items()},
                "answers": dict(self.answers)}


@pytest.fixture(autouse=True)
def _fresh_manager():
    """A manager of this test's own, both ends. It is process-wide and lazily
    built, so one left standing would hold the PREVIOUS test's tmp state dir —
    and the index it persists would be read back by the next case."""
    queue_manager.reset_for_tests(None)
    yield
    queue_manager.reset_for_tests(None)


@pytest.fixture(autouse=True)
def manager():
    """The fake in place of the real index, with an empty line on every folder —
    the state a case starts from and then says how it differs, the same posture
    as `no_agent` above.

    AUTOUSE, like `no_agent` above: the manager is process-wide and lazily
    built, and one left standing would hold the PREVIOUS test's tmp state dir —
    so every case gets its own either way, and this is the one that also gives
    the case a handle on it."""
    fake = FakeManager()
    queue_manager.reset_for_tests(fake)
    yield fake
    queue_manager.reset_for_tests(None)


@pytest.fixture()
def client(tmp_path):
    return TestClient(create_app(start_dir=str(tmp_path)))


@pytest.fixture()
def flag(home):
    """Turn the project queue on through the REAL pref, not a patched
    `enabled()`: the flag is the feature's one promise and a suite that stubbed
    it would never once read the switch it is all gated on."""
    def on(value=True):
        path = home / ".fused-render" / "prefs.json"
        path.write_text(json.dumps({"project_queue_enabled": value}))
        assert project_queue.enabled() is value
    return on


@pytest.fixture()
def folders(tmp_path):
    """Two real working trees, spelled the way the queue spells a folder.

    Real, because `queue_key` reads the disk to tell a folder from a file target
    and a path that does not exist answers with its parent — which would
    silently key two tests' folders the same.

    CANONICAL, because these strings are used at BOTH ends: as a target/project
    handed to the router (raw input, any spelling) and as the folder KEY a
    stubbed `holders()` is filed under and a row's `queue_key` is compared with.
    `queue_key` answers `canonical_fs_path` — forward slashes, always — so on
    Windows `str(WindowsPath)` would key the fixture on a backslashed spelling
    the router never produces and every lookup would miss. On POSIX this is
    `str(path)` unchanged."""
    a = tmp_path / "trees" / "alpha"
    b = tmp_path / "trees" / "beta"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    return canonical_fs_path(str(a)), canonical_fs_path(str(b))


@pytest.fixture()
def rings(monkeypatch):
    """Every key the router rang the long-poll with, in order."""
    seen = []
    real = tasks_watch.notify
    monkeypatch.setattr(tasks_watch, "notify",
                        lambda keys=None: (seen.append(set(keys or ())),
                                           real(keys))[0])
    return seen


# --------------------------------------------------------------- the scaffold


def _iso(delta=0.0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta))


def _entry(entry_id, message, target, due=None, **fields):
    entry = {"id": entry_id, "target": target, "message": message,
             "due": due or _iso(-60), "session_id": "", "permission_mode": "auto",
             "state": schedule.PENDING, "repeats": "", "rule": None,
             "created": due or _iso(-60), "fired": "", "run_id": "", "error": "",
             "turn": "", "claude_session_id": "", "priority": False}
    entry.update(fields)
    return entry


def _transcript(projects_dir, session_id, cwd, prompt="go"):
    d = projects_dir / ("-encoded-" + session_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{session_id}.jsonl"
    path.write_text(json.dumps({
        "type": "user", "timestamp": _iso(-600), "cwd": cwd,
        "sessionId": session_id, "uuid": session_id + "-0",
        "message": {"role": "user", "content": [{"type": "text",
                                                 "text": prompt}]}}) + "\n")
    return path


def _holders(monkeypatch, mapping):
    """One folder is busy with one task: `{folder: holder task key}`.

    ONE PLACE, because there is one record of it (PR 2, 2026-09-17): who owns a
    folder is the queue manager's index and every door asks it (`is_free`,
    `owner`). The derived-holder scan that used to answer the same question
    beside it is gone, and with it the case where a row could be drawn as queued
    while an admission about the same tree answered `run: true`."""
    built = {key: {"session_id": task_key, "run_id": "r-" + task_key,
                   "task_key": task_key, "kind": "run"}
             for key, task_key in mapping.items()}
    manager = queue_manager.peek()
    if isinstance(manager, FakeManager):
        for folder, task_key in mapping.items():
            manager.line(folder, *manager.lines.get(folder, ()), holder=task_key)
    return built


class _RunsAgent:
    """The parts of the claude template's agent.py the listing reads, over a
    REAL run dir: `_permissions` lists the perm directory and calls a
    `.req.json` with no `.res.json` beside it unanswered, exactly as agent.py
    does. A canned list would not prove the thing the cases below are about —
    that a card the user HAS answered still reads as unanswered on disk while
    its decision is held."""

    ANSWERABLE_TOOL = "AskUserQuestion"

    def __init__(self, runs_dir):
        self.RUNS = str(runs_dir)

    def _perm_dir(self, run_dir):
        return os.path.join(run_dir, "perm")

    def _permissions(self, run_dir):
        perm_dir = self._perm_dir(run_dir)
        try:
            names = sorted(n for n in os.listdir(perm_dir)
                           if n.endswith(".req.json"))
        except OSError:
            return []
        out = []
        for name in names:
            with open(os.path.join(perm_dir, name), encoding="utf-8") as fh:
                req = json.load(fh)
            answered = os.path.exists(
                os.path.join(perm_dir, req["id"] + ".res.json"))
            out.append({"id": req["id"], "tool": str(req.get("tool") or ""),
                        "input": req.get("input") or {},
                        "decision": "allow" if answered else ""})
        return out

    def _alive(self, run_dir):
        return os.path.exists(os.path.join(run_dir, "alive"))

    def _session_from_out(self, run_dir):
        return ""

    def _tool_detail(self, name, inp):
        return str((inp or {}).get("command") or "")


@pytest.fixture()
def park(tmp_path, monkeypatch):
    """A real runs tree, and a function that parks one live run on one
    unanswered card in a folder."""
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(project_queue, "agent_module",
                        lambda: _RunsAgent(runs))

    def stage(run_id, session_id, project, request_id="req-1", tool="Bash"):
        run_dir = runs / run_id
        (run_dir / "perm").mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"file": project, "resumed_from": ""}))
        (run_dir / "session").write_text(session_id)
        (run_dir / "alive").write_text("1")
        (run_dir / "perm" / (request_id + ".req.json")).write_text(
            json.dumps({"id": request_id, "tool": tool,
                        "input": {"command": "rm -rf build"}}))
        return str(run_dir)

    return stage


@pytest.fixture()
def chat_run(tmp_path, monkeypatch):
    """A real runs tree and a chat's own live run in it — the run a new chat's
    first message starts, which names no session until something polls it.

    The `pid` file is the one the session host overwrites with the CLI's own
    pid, and it is what lets the live registry name the run (`session_for_pid`).
    """
    runs = tmp_path / "chat-runs"
    runs.mkdir()
    monkeypatch.setattr(project_queue, "agent_module",
                        lambda: _RunsAgent(runs))

    def stage(run_id, project, pid=None, session_id=""):
        run_dir = runs / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "meta.json").write_text(
            json.dumps({"file": project, "resumed_from": ""}))
        if session_id:
            (run_dir / "session").write_text(session_id)
        if pid is not None:
            (run_dir / "pid").write_text(str(pid))
        (run_dir / "alive").write_text("1")
        return run_dir

    return stage


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """The live-session registry Claude Code writes, under tmp — one row per
    call, then a tick to read it in."""
    sessions = tmp_path / "claude-sessions-registry"
    sessions.mkdir()
    monkeypatch.setattr(tasks_watch, "SESSIONS_DIR", str(sessions))
    tasks_watch.reset()

    stamp = [time.time() + 10]

    def write(session_id, status="busy", pid=None, name="p"):
        path = sessions / (name + ".json")
        path.write_text(json.dumps(
            {"pid": os.getpid() if pid is None else pid,
             "sessionId": session_id, "cwd": "/proj", "status": status,
             "updatedAt": int(time.time() * 1000)}), encoding="utf-8")
        stamp[0] += 1
        os.utime(path, (stamp[0], stamp[0]))
        tasks_watch.tick()
        return path

    yield write
    tasks_watch.reset()


def _rows(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return {task["key"]: task for task in r.json()["tasks"]}


def _post(client, path, body):
    return client.post(path, json=body, headers=HEADERS)


def _admitted(body):
    """An admission's `run: true` body, with the per-send CLAIM TOKEN peeled
    off (Bugbot, PR #1194) — minted on every admitted send with the flag on,
    it is a fresh value every call (`FakeManager.claim_for_send`) and no case
    below asserts what it IS, only that the send ran. `routers/run.py` and its
    own suite are what exercise the token travelling onto the run request."""
    body = dict(body)
    body.pop("claim", None)
    return body


# ======================================================== the queued status


def _waiting_pair(projects_dir, folders, monkeypatch, **entry_fields):
    """One task holding `alpha`, one task with a due message into it."""
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    schedule._write([_entry("e-wait", "run the report", alpha,
                            session_id="sess-wait", **entry_fields)])
    _holders(monkeypatch, {alpha: "sess-holder"})
    return alpha


def test_a_task_waiting_on_a_busy_folder_reads_queued(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """The whole feature in one row. The message is DUE — it would be running
    this second — and the only thing between it and a process is another task
    holding the same working tree."""
    flag()
    alpha = _waiting_pair(projects_dir, folders, monkeypatch)
    manager.line(alpha, "sess-wait", holder="sess-holder")

    rows = _rows(client)
    row = rows["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_key"] == alpha
    assert row["queue_position"] == 1
    assert row["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert row["queue_ahead_title"] == "holding the folder"
    # …and the click that follows it: `tasks-lib.taskHref`'s own pair, read off
    # the very row the reader would land on.
    assert row["queue_ahead_session"] == "sess-holder"
    assert row["queue_ahead_target"] == rows["sess-holder"]["target"]
    assert row["queue_priority"] is False
    # The card says how much is waiting without counting entries client-side —
    # and this one IS a scheduled message aimed at this session, which is the
    # case that still shuts its composer. (The chat's own queued send is the
    # other half, and has its own case below.)
    assert row["queue_waiting"] == 1
    assert row["queue_blocking"] is True
    # The holder is not in its own line.
    assert rows["sess-holder"]["queue_position"] == 0


def test_the_flag_off_never_derives_queued(
        client, projects_dir, folders, monkeypatch, flag):
    """Flag off is main, FIELD FOR FIELD: the same fixture that reads `queued`
    above reads `upcoming`, and every queue field on the row is empty — the
    folder included. Resolving `queue_key` walks a task's ancestors looking for
    a `.git`, once per row, and main never did that; it appears the moment the
    feature is turned on."""
    flag(False)
    _waiting_pair(projects_dir, folders, monkeypatch)

    row = _rows(client)["sess-wait"]
    assert row["status"] == "upcoming"
    assert row["queue_key"] == ""
    assert row["queue_position"] == 0
    assert row["queue_ahead"] == ""
    assert row["queue_ahead_session"] == ""
    assert row["queue_ahead_target"] == ""
    assert row["queue_priority"] is False
    # The card's summary is a queue fact too: with the flag off the composer
    # blocks on every pending entry the way it always did, and the client has
    # no use for a count it is not drawing.
    assert row["queue_waiting"] == 0
    assert row["queue_blocking"] is False


def test_the_holder_of_a_folder_does_not_queue_behind_itself(
        client, projects_dir, folders, monkeypatch, flag):
    """A second message into a conversation that is already running is the
    inbox-absorb case the chat has always had. Gating it would be this feature
    refusing a send that touches nothing new."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    schedule._write([_entry("e1", "and one more thing", alpha,
                            session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-a"})

    assert _rows(client)["sess-a"]["status"] != "queued"


def test_work_due_next_week_is_upcoming_and_not_queued(
        client, projects_dir, folders, monkeypatch, flag):
    """Queued is about the machine; upcoming is about the clock. A message
    scheduled for Tuesday is not waiting on a folder and must not appear in a
    lane that reads as "about to run"."""
    flag()
    alpha, _beta = folders
    schedule._write([_entry("e1", "next week", alpha, due=_iso(7 * 86400),
                            session_id="sess-later")])
    _holders(monkeypatch, {alpha: "sess-holder"})

    assert _rows(client)["sess-later"]["status"] == "upcoming"


def test_a_task_waiting_on_a_free_folder_is_not_queued(
        client, folders, monkeypatch, flag):
    """Nothing is holding anything, so nothing is waiting — whatever is due."""
    flag()
    alpha, _beta = folders
    schedule._write([_entry("e1", "go", alpha, session_id="sess-a")])
    _holders(monkeypatch, {})

    assert _rows(client)["sess-a"]["status"] != "queued"


def test_a_held_answer_reads_queued_and_not_needs_attention(
        client, projects_dir, folders, monkeypatch, flag, park, manager):
    """The row the whole held-answer path exists to paint, over a REAL run dir.

    The user has answered, and nothing is written to the run: the decision is
    held until the folder frees, so the request is still sitting in the perm
    directory with no `.res.json` beside it. Read naively that is a parked run,
    `needs_attention` sits above `queued` in `_status`, and "Answer queued —
    runs next" could never be reached — the status would say the user is being
    waited on when the user is the one waiting."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-a", alpha, "run the build")
    park("r-a", "sess-a", alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})

    # Unanswered, it IS a needs-attention row — the stub is the real thing.
    assert _rows(client)["sess-a"]["status"] == "needs_attention"

    # THE ANSWER IS THE MANAGER'S NOW (PR 2): the decision is filed against the
    # task key, and `_parked_runs` asks `held_answer` per run whether this very
    # run's card has already been answered.
    manager.hold("sess-a", run_id="r-a", request_id="req-1",
                 raw={"decision": "allow"})
    manager.line(alpha, "sess-a", holder="sess-holder", priority=("sess-a",))

    rows = _rows(client)
    row = rows["sess-a"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_priority"] is True
    assert row["queue_ahead"] == rows["sess-holder"]["task_id"]


def test_a_queued_row_is_titled_by_the_queued_message_and_says_queued(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """A queued task IS the message in the line (Akshil, 2026-09-21): the row's
    title is that message, not the send before it, and where the last reply
    would go it says `Queued` — the old answer is not about this message."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    path = _transcript(projects_dir, "sess-wait", alpha, "the first ask")
    with path.open("a") as fh:
        fh.write(json.dumps({
            "type": "assistant", "timestamp": _iso(-500),
            "sessionId": "sess-wait", "uuid": "sess-wait-1",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "done the first"}]},
        }) + "\n")
    schedule._write([_entry("e-wait", "the queued ask", alpha,
                            session_id="sess-wait")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-wait", holder="sess-holder")

    row = _rows(client)["sess-wait"]
    assert row["status"] == "queued"
    assert row["last_message"]["text"] == "the queued ask"
    assert row["last_reply"] == "Queued"


def test_a_message_scheduled_for_later_does_not_title_a_queued_row(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """Only the message whose time has COME is the queued one: a second message
    scheduled into the same conversation for tomorrow is newer, and is not what
    is waiting to run."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-wait", alpha, "the first ask")
    schedule._write([
        _entry("e-wait", "the queued ask", alpha, session_id="sess-wait"),
        _entry("e-later", "tomorrow's ask", alpha, due=_iso(86400),
               session_id="sess-wait"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-wait", holder="sess-holder")

    row = _rows(client)["sess-wait"]
    assert row["status"] == "queued"
    assert row["last_message"]["text"] == "the queued ask"


def test_a_queued_task_that_still_reads_live_is_queued_not_running(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """The manager's line outranks a stale `live` signal (review round,
    2026-09-18): an answered-blocked task the manager just promoted to
    line[0] can have a session that still READS live — the registry has not
    caught up, or the sender's own mark has not expired — and `_running_now`
    answers True for exactly the reason `_status`'s running rule exists. But
    standing in a line is proof this task is not the folder's owner, so
    `queued` has to win over that stale reading. The task that actually holds
    the folder, with the identical live facts, still reads `in_progress` —
    only the one the manager calls a bystander flips."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-a", alpha, "say c")
    manager.line(alpha, "sess-a", holder="sess-holder", priority=("sess-a",))
    tasks_watch.mark_running("sess-a")
    tasks_watch.mark_running("sess-holder")

    rows = _rows(client)
    assert rows["sess-a"]["status"] == "queued"
    assert rows["sess-a"]["queue_position"] == 1
    assert rows["sess-a"]["queue_priority"] is True
    assert rows["sess-holder"]["status"] == "in_progress"


def test_a_parked_task_stays_needs_attention_even_when_the_manager_queued_it(
        client, projects_dir, folders, monkeypatch, flag, park, manager):
    """Parked still outranks queued after the reorder: `parked` is asked
    first in `_status` no matter where the queued check moved to, because a
    card nobody has answered is a stronger fact than a place in line."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-a", alpha, "run the build")
    park("r-a", "sess-a", alpha)
    manager.line(alpha, "sess-a", holder="sess-holder")

    assert _rows(client)["sess-a"]["status"] == "needs_attention"


def test_behind_names_the_task_directly_ahead_and_not_always_the_holder(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """"Behind TASK-041" ON EVERY CARD IN THE LANE SAID THE SAME THING FOUR
    TIMES (Akshil, 2026-09-12), and the one fact a reader wants out of the
    sentence — who do I actually have to wait for — was the one it could not
    give. Position 1 is behind the holder (the run in flight, which is what it
    is genuinely waiting on); everything after that is behind the task in front
    of it, so reading down the lane the sentences chain."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    for name in ("sess-1", "sess-2", "sess-3"):
        _transcript(projects_dir, name, alpha, "waiting: " + name)
    schedule._write([
        _entry("e1", "first in line", alpha, due=_iso(-300),
               session_id="sess-1"),
        _entry("e2", "second in line", alpha, due=_iso(-200),
               session_id="sess-2"),
        _entry("e3", "third in line", alpha, due=_iso(-100),
               session_id="sess-3"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-1", "sess-2", "sess-3", holder="sess-holder")

    rows = _rows(client)
    assert [rows[k]["queue_position"] for k in ("sess-1", "sess-2", "sess-3")] \
        == [1, 2, 3]
    assert rows["sess-1"]["queue_ahead_key"] == "sess-holder"
    assert rows["sess-2"]["queue_ahead_key"] == "sess-1"
    assert rows["sess-3"]["queue_ahead_key"] == "sess-2"
    # …and the words and the link agree with the key, all four off one pass.
    assert rows["sess-3"]["queue_ahead"] == rows["sess-2"]["task_id"]
    assert rows["sess-3"]["queue_ahead_title"] == "waiting: sess-2"
    assert rows["sess-3"]["queue_ahead_session"] == "sess-2"


def test_a_holder_the_index_files_under_its_run_is_still_named_by_its_session(
        client, projects_dir, folders, flag, manager):
    """"1ST IN LINE" WITH NOTHING BEHIND IT (Akshil, QA of PR #1194). A
    brand-new chat holds the folder under the only name it had when its turn
    started — its run id — while its row on the page is filed under the session
    Claude Code minted a moment later. The two names are the same chat, so the
    sentence the reader wants ("behind TASK-041 · holding the folder") is
    available; it was simply looked up under the one name that names no row.

    `queue_ahead_key` is deliberately unchanged: it is what the index holds, and
    the fix is the LOOKUP (`ahead_names`), not the field."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-a", alpha, "waiting my turn")
    manager.line(alpha, "sess-a", holder="run-h", holder_session="sess-holder",
                 holder_run="run-h")

    rows = _rows(client)
    assert rows["sess-a"]["queue_position"] == 1
    assert rows["sess-a"]["queue_ahead_key"] == "run-h"
    assert rows["sess-a"]["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert rows["sess-a"]["queue_ahead_title"] == "holding the folder"
    assert rows["sess-a"]["queue_ahead_session"] == "sess-holder"
    assert rows["sess-a"]["queue_ahead_target"] == alpha


def test_a_dispatched_holder_is_named_through_the_entry_its_row_rekeyed_off(
        client, projects_dir, folders, flag, manager):
    """The other half of the same miss: the pump dispatched a scheduled message
    and the index holds it as `pending:<entry>`, but the listing has ALREADY
    rekeyed that entry's row onto the session its run published
    (`_entry_key` → `_entry_session`). The row is right there — under a
    different name — and the entry id is the one name of it that never moves."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "the dispatched message")
    _transcript(projects_dir, "sess-a", alpha, "waiting my turn")
    schedule._write([_entry("e1", "the dispatched message", alpha,
                            session_id="sess-holder")])
    manager.line(alpha, "sess-a", holder="pending:e1")

    rows = _rows(client)
    assert "pending:e1" not in rows          # the row rekeyed onto the session
    assert rows["sess-a"]["queue_ahead_key"] == "pending:e1"
    assert rows["sess-a"]["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert rows["sess-a"]["queue_ahead_session"] == "sess-holder"


def test_a_holder_no_name_of_which_is_a_row_still_names_nobody(
        client, projects_dir, folders, flag, manager):
    """The honest empty is kept. A holder the collection does not contain under
    ANY of its names — a terminal `claude` the app never started — leaves the
    fields empty and the client says "behind a run in this folder", which is
    true, where half an answer would offer a click that goes nowhere."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "waiting my turn")
    manager.line(alpha, "sess-a", holder="run-h", holder_session="sess-gone",
                 holder_run="run-h")

    row = _rows(client)["sess-a"]
    assert row["queue_position"] == 1
    assert (row["queue_ahead"], row["queue_ahead_title"],
            row["queue_ahead_session"]) == ("", "", "")


def test_a_second_unanswered_card_still_needs_attention(
        client, projects_dir, folders, monkeypatch, flag, park, manager):
    """One held answer does not speak for the whole run: a second card nobody
    has answered is still somebody being waited on."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-a", alpha, "run the build")
    run_dir = park("r-a", "sess-a", alpha)
    with open(os.path.join(run_dir, "perm", "req-2.req.json"), "w") as fh:
        json.dump({"id": "req-2", "tool": "Bash", "input": {"command": "ls"}}, fh)
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.hold("sess-a", run_id="r-a", request_id="req-1",
                 raw={"decision": "allow"})

    assert _rows(client)["sess-a"]["status"] == "needs_attention"


def test_two_folders_have_two_independent_lines(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """No cap across folders — worktrees run in parallel, which is the whole
    point of cutting one."""
    flag()
    alpha, beta = folders
    _transcript(projects_dir, "sess-ha", alpha)
    _transcript(projects_dir, "sess-hb", beta)
    schedule._write([
        _entry("e1", "into alpha", alpha, session_id="sess-a"),
        _entry("e2", "into beta", beta, session_id="sess-b"),
    ])
    _holders(monkeypatch, {alpha: "sess-ha", beta: "sess-hb"})
    manager.line(alpha, "sess-a", holder="sess-ha")
    manager.line(beta, "sess-b", holder="sess-hb")

    rows = _rows(client)
    assert rows["sess-a"]["queue_position"] == 1
    assert rows["sess-b"]["queue_position"] == 1
    assert rows["sess-a"]["queue_key"] != rows["sess-b"]["queue_key"]


def test_a_pending_row_queues_under_its_own_key(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """A brand-new task — a message that names no session at all — is still a
    row, keyed `pending:<entry id>` (§5), and still takes a place in the line."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([_entry("e-new", "a brand new task", alpha)])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, tasks_store.pending_key("e-new"), holder="sess-holder")

    row = _rows(client)[tasks_store.pending_key("e-new")]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1


def test_an_archived_task_is_archived_and_not_queued(
        client, projects_dir, folders, monkeypatch, flag):
    """Archiving cancels the pending work, so a filed task has nothing left in
    any line — and the status says filed, which is what the user did."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    schedule._write([_entry("e1", "go", alpha, session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    sessions_mod.write_triage("sess-a", "archived")

    assert _rows(client)["sess-a"]["status"] == "archived"


def test_the_pulse_carries_where_a_row_stands(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """The sidebar prints "n queued" beside "n running" and a queued row has to
    be able to say whether it runs next — without downloading the Tasks page."""
    flag()
    alpha = _waiting_pair(projects_dir, folders, monkeypatch)
    manager.line(alpha, "sess-wait", holder="sess-holder")

    pulse = {row["key"]: row for row in client.get("/api/tasks/pulse").json()["tasks"]}
    row = pulse["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_ahead"].startswith("TASK-")
    assert row["queue_priority"] is False
    # The sidebar's rows are links too, and one message is waiting on this one.
    assert row["queue_ahead_session"] == "sess-holder"
    assert row["queue_waiting"] == 1
    # Compact stays compact: the folder, the holder's title and the holder's
    # own path are not here.
    assert "queue_key" not in row
    assert "queue_ahead_title" not in row
    assert "queue_ahead_target" not in row


def test_the_changes_answer_names_a_holder_it_was_not_asked_about(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """A narrowed long-poll answer is about one row, and the task in front of it
    is by definition another. Naming it from the stored table is what keeps the
    chip from reading "behind " with nothing after it."""
    flag()
    alpha = _waiting_pair(projects_dir, folders, monkeypatch)
    manager.line(alpha, "sess-wait", holder="sess-holder")
    ahead = _rows(client)["sess-holder"]["task_id"]

    since = tasks_watch.generation()
    tasks_watch.notify({"sess-wait"})
    r = client.get(f"/api/tasks/changes?since={since}&wait=0")
    assert r.status_code == 200, r.text
    rows = {row["key"]: row for row in r.json()["rows"]}
    assert rows["sess-wait"]["queue_position"] == 1
    assert rows["sess-wait"]["queue_ahead"] == ahead
    assert rows["sess-wait"]["queue_ahead_title"] == "holding the folder"


# =============================================================== admission


def test_admit_with_the_flag_off_always_runs_and_stores_nothing(
        client, folders, flag):
    """The client asks unconditionally; with the flag off the answer is a
    constant and the send path is main's, byte for byte."""
    flag(False)
    alpha, _beta = folders
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-a", "message": "go"})
    assert r.status_code == 200, r.text
    assert r.json() == {"run": True}
    assert schedule.list_entries() == []


def test_admit_into_a_free_folder_runs_and_takes_the_folder(
        client, folders, flag, manager):
    """Between "run" and the CLI registering a session there is nothing on disk
    saying the folder is taken. THE OWNER THE ADMISSION FILES is what closes
    that window (PR 2, 2026-09-17): `is_free` and `started` are one decision,
    so the next send into this tree finds an owner and queues instead of
    starting a second process in it."""
    flag()
    alpha, _beta = folders
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-a", "message": "go"})
    assert _admitted(r.json()) == {"run": True}
    assert (manager.owner(alpha) or {})["task"] == "sess-a"
    assert schedule.list_entries() == []


def test_a_second_session_queues_behind_the_first_ones_reservation(
        client, projects_dir, folders, flag, manager):
    """The real derivation, end to end and unmocked: one admission reserves the
    folder, and the next send into it from another task is stored instead of
    spawned."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "holding the folder")
    ahead = _rows(client)["sess-a"]["task_id"]

    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "session_id": "sess-a",
                           "message": "go"}).json()) == {"run": True}
    # …and where the stored send then stands is the manager's answer, which the
    # reply reads back through `_queue_place`.
    manager.line(alpha, "sess-b", holder="sess-a")
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-b", "message": "me too"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"] is False
    assert body["key"] == "sess-b"
    assert body["position"] == 1
    assert body["ahead"] == ahead
    assert body["ahead_title"] == "holding the folder"
    # The chip under the bubble links to the chat in front, off the same two
    # fields every other thread link in this app is built from.
    assert body["ahead_session"] == "sess-a"
    assert body["ahead_target"] == alpha
    assert body["entry"]["message"] == "me too"
    assert body["entry"]["state"] == schedule.PENDING
    # WHO ASKED: this chat did, which is what keeps its own composer open.
    assert body["entry"]["origin"] == "chat"
    assert _rows(client)["sess-b"]["status"] == "queued"


def test_a_new_chats_second_message_is_not_queued_behind_its_own_run(
        client, folders, flag, chat_run, manager):
    """AKSHIL'S BUG, the whole sequence at the router (folder qa-folder-b,
    2026-09-12).

    A new chat has no session, so its first message is admitted with
    `session_id: ""` and reserves the folder for nobody. The run that message
    starts writes no session id into its run dir for a while, so the derivation
    can only call it `starting` — and the SECOND message, which by then carries
    the real session, used to be told `#1 in line · behind a run in this
    folder`: queued behind itself for the whole `STARTING_GRACE`.

    The client sends the run it started, and that is the name the conversation
    has before it has a session."""
    flag()
    alpha, _beta = folders
    first = _post(client, "/api/tasks/queue/admit",
                  {"project": alpha, "session_id": "", "message": "first"}).json()
    assert first["run"] is True
    # …and the folder is filed under the placeholder this send was given, so a
    # second NAMELESS send would queue rather than race into it.
    assert first["owner_token"].startswith(PLACEHOLDER)
    # The run appears: alive, in that folder, and it has not named itself.
    chat_run("run-1", alpha, pid=os.getpid())
    project_queue.invalidate_holders()
    # The session alone still cannot match it — that is the state the bug was
    # reported from, and what the run id is for.

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-new", "run_id": "run-1",
               "message": "second"})
    assert r.status_code == 200, r.text
    assert _admitted(r.json()) == {"run": True}
    assert schedule.list_entries() == []          # nothing was queued
    # …and the folder now carries the name the chat finally has.
    owner = manager.owner(alpha) or {}
    assert owner["task"] == "sess-new" and owner["run_id"] == "run-1"


def test_the_registry_names_the_new_chats_run_by_its_pid(
        client, folders, flag, chat_run, registry):
    """The other half of the same fix, and the one that needs no client change:
    the CLI registers its session against its pid, the run dir has carried that
    pid since it spawned, so the run is named seconds after it starts and the
    ordinary session match answers."""
    flag()
    alpha, _beta = folders
    _post(client, "/api/tasks/queue/admit",
          {"project": alpha, "session_id": "", "message": "first"})
    chat_run("run-1", alpha, pid=os.getpid())
    registry("sess-new", status="busy")
    project_queue.invalidate_holders()
    # The run answers to the session the CLI registered against its pid…
    assert "sess-new" in project_queue.run_sessions(
        project_queue.agent_module(),
        os.path.join(str(project_queue.agent_module().RUNS), "run-1"), {})
    # …so no run_id is needed: the conversation is named now.
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-new", "message": "second"})
    assert _admitted(r.json()) == {"run": True}
    assert schedule.list_entries() == []


def test_a_run_id_does_not_admit_a_chat_into_somebody_elses_folder(
        client, folders, flag, chat_run, manager):
    """The self-match is about identity, not a skeleton key: a run id that
    names nothing in this folder queues like anything else."""
    flag()
    alpha, _beta = folders
    chat_run("run-other", alpha, pid=os.getpid(), session_id="sess-holder")
    project_queue.invalidate_holders()
    manager.line(alpha, "sess-mine", holder="sess-holder")

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-mine", "run_id": "run-9",
               "message": "me too"})
    assert r.status_code == 200, r.text
    assert r.json()["run"] is False
    assert _rows(client)["sess-mine"]["status"] == "queued"


def test_admit_absorbs_a_send_into_this_chats_own_live_run(
        client, folders, flag, manager, chat_run):
    """A FORCED CHAT'S FOLLOW-UP (PR 2, 2026-09-21). `queue/force` starts a run
    BESIDE the folder's owner on purpose, so the index goes on naming somebody
    else as that tree's owner — and this chat is a stranger in it for the rest
    of its life. Its second message was told to queue behind a task its own
    process is running beside; a chat can always talk to its own live run, and
    that send spawns nothing (`agent._send` absorbs it).

    THE RUN IS REAL HERE, not a flag on the fake: `own_run_alive` reads the same
    status sync the manager is wired to, and the run dir is the only channel
    that answers for a chat with no session yet."""
    flag()
    alpha, _beta = folders
    chat_run("r-forced", alpha)
    manager.line(alpha, holder="sess-holder")

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "and one more thing",
               "run_id": "r-forced"})
    assert r.status_code == 200, r.text
    # No claim token and no owner token: nothing was taken and nothing stored.
    assert r.json() == {"run": True}
    assert schedule.list_entries() == []
    assert (manager.owner(alpha) or {})["task"] == "sess-holder"
    assert _kinds(manager, "enqueue", "started") == []


def test_admit_still_queues_a_stranger_whose_run_is_gone(
        client, folders, flag, manager, chat_run):
    """The other half of the rule above, and the one that keeps the feature: a
    run id that names no live process is not a conversation of one's own."""
    flag()
    alpha, _beta = folders
    chat_run("r-forced", alpha)
    manager.line(alpha, holder="sess-holder")

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "and one more thing",
               "run_id": "r-gone"})
    assert r.status_code == 200, r.text
    assert r.json()["run"] is False


def test_an_admitted_send_changes_no_row_by_itself(
        client, projects_dir, folders, flag, rings, manager):
    """INSTANT STATUS IS THE SENDER'S MARK, NOT THE RESERVATION (2026-09-16).
    The queue used to read its own admission reservation as `in_progress` for
    the seconds before `claude` registered (browser QA, 2026-09-12). #1163 now
    answers the same instant for every send, flag or no flag: the page marks the
    session as it sends (`tasks_watch.mark_running`) and `_live` believes it. So
    admission itself moves nothing and rings nothing — the owner it files is the
    folder gate's record and only that — and the mark is what flips the row."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "hello")
    before = _rows(client)["sess-a"]["status"]
    assert before != "in_progress"

    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "session_id": "sess-a",
                           "message": "go"}).json()) == {"run": True}
    assert rings == []
    # the gate's record stands, and it is the manager's now
    assert (manager.owner(alpha) or {})["task"] == "sess-a"
    assert _rows(client)["sess-a"]["status"] == before

    tasks_watch.mark_running("sess-a")
    assert _rows(client)["sess-a"]["status"] == "in_progress"


def test_running_now_is_false_for_an_ended_session_even_when_live():
    """The queue manager's word off a `turn_ended`/`exited` event
    (`tasks_watch.mark_turn_ended`) outranks `live` — the registry row and the
    transcript tail can both still look running for a beat after a folder
    handoff, and this is the one path that must not believe them (see
    `_running_now`'s docstring for why `busy` alone is left alone)."""
    tasks_watch.reset()
    tasks_watch.mark_turn_ended("sess-ended")
    assert tasks_mod._running_now("sess-ended", True, set()) is False
    # An independent scheduler claim is unaffected either way.
    assert tasks_mod._running_now("sess-ended", True, {"sess-ended"}) is True
    # A session nothing said ended reads exactly as `live` says.
    assert tasks_mod._running_now("sess-untouched", True, set()) is True
    assert tasks_mod._running_now("sess-untouched", False, set()) is False


def test_with_the_flag_off_an_admitted_send_changes_no_row(
        client, projects_dir, folders, flag, rings):
    """The control. With the queue off nothing is reserved, so nothing reads as
    running that main would not — the endpoint is a constant and the row is
    whatever the transcript says."""
    flag(False)
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "hello")
    before = _rows(client)["sess-a"]["status"]

    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a", "message": "go"}
                 ).json() == {"run": True}
    assert rings == []
    assert _rows(client)["sess-a"]["status"] == before


def test_a_run_id_that_is_a_path_is_refused(client, folders, flag):
    """Run ids name a directory under the runs tree; one carrying a separator
    is a client bug, and a 400 is what makes it visible."""
    flag()
    alpha, _beta = folders
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "s", "run_id": "../etc",
               "message": "go"})
    assert r.status_code == 400
    assert "run_id" in r.json()["error"]


def test_a_queued_send_keeps_every_choice_the_user_made(
        client, projects_dir, folders, monkeypatch, flag, tmp_path):
    """The queue DELAYS work, it never changes it. A stored send that dropped
    the model, the effort or the attachments would be a different message from
    the one that would have gone had the folder been free."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})
    shot = os.path.join(schedule.shots_dir(), "shot.png")
    os.makedirs(schedule.shots_dir(), mode=0o700, exist_ok=True)
    with open(shot, "wb") as fh:
        fh.write(b"x")

    r = _post(client, "/api/tasks/queue/admit", {
        "project": alpha, "session_id": "sess-b", "message": "look at this",
        "model": "opus", "effort": "high", "permission_mode": "plan",
        "images": [shot],
        "attachments": [{"path": shot, "name": "shot.png", "kind": "image"}],
        "title": "A queued send", "description": "with everything on it"})
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert entry["model"] == "opus"
    assert entry["effort"] == "high"
    assert entry["permission_mode"] == "plan"
    assert entry["title"] == "A queued send"
    assert entry["description"] == "with everything on it"
    # The store keeps the canonical forward-slash spelling of a path (schedule
    # `_images`), whatever spelling the request used — on Windows the two differ.
    assert entry["images"] == [canonical_fs_path(shot)]
    assert entry["attachments"][0]["name"] == "shot.png"


def test_admit_refuses_a_missing_project(client, folders, flag):
    flag()
    r = _post(client, "/api/tasks/queue/admit",
              {"project": "", "session_id": "s", "message": "go"})
    assert r.status_code == 400
    assert "project" in r.json()["error"]


def test_a_wordless_send_runs_on_a_free_folder_and_is_refused_on_a_busy_one(
        client, projects_dir, folders, monkeypatch, flag):
    """The composer lets a user send pictures with no words. Into a free folder
    that is an ordinary send and this endpoint must not stand in the way of it;
    into a busy one it would have to become a scheduled entry, and
    `schedule.create` refuses one with no message whatever it is carrying. So it
    is a 400 with the store's own sentence rather than a `run: true` that would
    put a second turn in a folder another task is holding."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {})
    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "session_id": "sess-a",
                           "message": "",
                           "images": ["/tmp/shot.png"]}).json()) == {"run": True}

    _transcript(projects_dir, "sess-holder", alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-b", "message": "  ",
               "images": ["/tmp/shot.png"]})
    assert r.status_code == 400
    assert "cannot be empty" in r.json()["error"]
    assert schedule.list_entries() == []


def test_admit_rings_the_long_poll_for_the_row_it_queued(
        client, projects_dir, folders, monkeypatch, flag, rings):
    """Every queue verb rings, so `/api/tasks/changes` answers in milliseconds
    instead of on the next 20-second listing."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})

    _post(client, "/api/tasks/queue/admit",
          {"project": alpha, "session_id": "sess-b", "message": "go"})
    assert {"sess-b"} in rings


def test_every_queue_verb_needs_the_fused_header(client, folders):
    """These START WORK, unlike the read next door — the exact pair D3's header
    guard covers."""
    alpha, _beta = folders
    for path, body in (("/api/tasks/queue/admit",
                        {"project": alpha, "session_id": "s", "message": "go"}),
                       ("/api/tasks/queue/force", {"key": "sess-a"}),
                       ("/api/tasks/queue/decide",
                        {"run_id": "r", "request_id": "q", "session_id": "s",
                         "project": alpha, "decision": "allow", "scope": "once"})):
        r = client.post(path, json=body)
        assert r.status_code == 403, path


# ==================================================================== force
#
# `POST /api/tasks/queue/force` — run this WAITING message now, beside whatever
# owns the folder. It is the FLAG-OFF behaviour for one message: the manager
# never owns it, the owner is never interrupted, and the dispatch is the
# scheduler's ordinary one. So every case here asserts two things at once — the
# message went, and the folder's owner did not move.


@pytest.fixture()
def dispatched(monkeypatch):
    """Every `schedule.dispatch_entry` the router made, and what it answers.

    The ONE spawn site, stubbed so this suite pins the CALL rather than starting
    a CLI — the dispatch itself, its session gates and its `SpawnBusy` are
    tests/test_schedule_project_queue.py's subject. `answer["value"]` is what it
    hands back, or an exception to raise."""
    calls = []
    answer = {"value": {"run_id": "r-new", "session_id": "sess-new"}}

    def dispatch_entry(entry_id, now=None):
        calls.append(entry_id)
        value = answer["value"]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(schedule, "dispatch_entry", dispatch_entry)
    return calls, answer


def test_force_starts_the_waiting_message_beside_the_folders_owner(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched,
        rings):
    """The whole verb in one press: the message goes NOW, it leaves the line, and
    the task holding the folder keeps it."""
    flag()
    calls, _answer = dispatched
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([
        _entry("e-due", "the queued one", alpha, session_id="sess-a"),
        _entry("e-later", "next week", alpha, due=_iso(7 * 86400),
               session_id="sess-a"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder",
                 entries={"sess-a": "e-due"})

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "started": True, "run_id": "r-new",
                        "session_id": "sess-new"}
    # THE OLDEST DUE MESSAGE AND NOTHING ELSE. Next week's is not waiting, and
    # forcing it would be this verb silently rescheduling work nobody asked
    # about.
    assert calls == ["e-due"]
    # OUT OF THE LINE BY ENTRY, and the owner untouched — the two halves of
    # "beside whatever owns the folder".
    assert _kinds(manager, "forget_entry") == [("forget_entry", "e-due")]
    assert manager.lines[alpha] == []
    assert (manager.owner(alpha) or {})["task"] == "sess-holder"
    assert _kinds(manager, "remove", "skip", "claim") == []
    # The row it is, and the row it is about to become once the dispatch names
    # itself.
    assert {"sess-a", "sess-new"} in rings


def test_force_names_the_entry_when_the_key_has_moved(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched):
    """The chip's own road: a queued chat is `pending:<leader entry>` until its
    leader's run mints a session, and a button holding that key 404s a second
    later. The entry id never moves."""
    flag()
    calls, _answer = dispatched
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([
        _entry("e-lead", "first thing", alpha, state=schedule.SENT,
               turn="done", claude_session_id="sess-lead"),
        _entry("e-follow", "second thing", alpha, follow_of="e-lead"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-lead", holder="sess-holder",
                 entries={"sess-lead": "e-follow"})

    stale = _post(client, "/api/tasks/queue/force",
                  {"key": tasks_store.pending_key("e-lead")})
    assert stale.status_code == 404

    r = _post(client, "/api/tasks/queue/force", {"entry_id": "e-follow"})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    assert calls == ["e-follow"]


def test_force_leaves_the_message_out_of_the_line_when_the_chat_is_busy(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched):
    """`SpawnBusy` is "not yet", not "no" — a send already in flight, or a live
    turn the user is typing into.

    NOTHING GOES BACK IN THE LINE (Bugbot, 2026-09-21). Re-enqueueing used to be
    what kept the message safe; with the task FORCED it is the one thing that
    would undo the force, and the message needs no rescuing — its entry is still
    `pending` in the scheduler's store, `reconcile` leaves a forced task out of
    every line and the next tick dispatches it down the flag-off road. So it is
    a 200 carrying the scheduler's own sentence rather than a 409 that hands the
    user back a place they had just left."""
    flag()
    _calls, answer = dispatched
    alpha, _beta = folders
    answer["value"] = schedule.SpawnBusy(
        "e-due: session sess-a has a live turn")
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([_entry("e-due", "the queued one", alpha,
                            session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder",
                 entries={"sess-a": "e-due"})

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    # The scheduler's OWN sentence, so the chat says what the wait actually is.
    assert r.json() == {"ok": True, "started": False, "delivered": 0,
                        "reason": "e-due: session sess-a has a live turn"}
    assert _kinds(manager, "enqueue") == []
    assert manager.lines[alpha] == []
    # …and the task is out of the queue for good, which is what makes the next
    # tick send this very message rather than re-queueing it.
    assert manager.is_forced("sess-a")
    assert (manager.owner(alpha) or {})["task"] == "sess-holder"


def test_force_answers_started_false_when_there_is_nothing_left_to_send(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched):
    """Cancelled in the window, or the pump got there first. A 200, because what
    the user asked for is no longer theirs to ask for — and nothing goes back in
    the line, because there is no message behind it any more."""
    flag()
    _calls, answer = dispatched
    answer["value"] = None
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([_entry("e-due", "the queued one", alpha,
                            session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder",
                 entries={"sess-a": "e-due"})

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "started": False,
                        "reason": "already started"}
    assert _kinds(manager, "enqueue") == []


def test_force_that_starts_nothing_unmarks_only_a_chat_it_marked_itself(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched):
    """`dispatch_entry` → None twice. First press on an unforced chat: the
    mark it wrote comes off (a no-op press must not leave a permanent bypass).
    Second press on a chat that was ALREADY forced: the mark stays (a retry or
    a second surface must not undo the press that started something)."""
    flag()
    _calls, answer = dispatched
    answer["value"] = None
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([_entry("e-due", "the queued one", alpha,
                            session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder",
                 entries={"sess-a": "e-due"})

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert manager.is_forced("sess-a") is False
    assert manager.is_forced("pending:e-due") is False

    manager.mark_forced("sess-a")
    manager.line(alpha, "sess-a", holder="sess-holder",
                 entries={"sess-a": "e-due"})
    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert manager.is_forced("sess-a") is True


def test_force_on_a_task_that_is_owed_an_answer_delivers_it(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched,
        agent):
    """A held card decision is the OTHER kind of waiting a line holds, and there
    is no message to dispatch for it. "Run it now" means deliver it — the same
    flag-off road, `agent._decide` against the run as it now is."""
    flag()
    calls, _answer = dispatched
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _transcript(projects_dir, "sess-a", alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.hold("sess-a", run_id="run-1", request_id="req-1",
                 raw={"run_id": "run-1", "request_id": "req-1",
                      "decision": "allow", "scope": "once"})
    manager.line(alpha, "sess-a", holder="sess-holder")

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "started": True, "delivered": 1}
    assert [call["request_id"] for call in agent.calls] == ["req-1"]
    assert calls == []
    # It owns nothing, so `remove` here only clears its line and its answers —
    # and the owner of the folder is still the owner of the folder.
    assert _kinds(manager, "remove") == [("remove", "sess-a")]
    assert (manager.owner(alpha) or {})["task"] == "sess-holder"


def test_force_takes_EVERY_waiting_message_of_the_task_out_of_the_line(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched):
    """STICKY, AND PER TASK (Akshil, 2026-09-21): "once a task is force-started
    it never enters the queue again — no matter if it is blocked or has multiple
    messages".

    So a chat with two messages waiting loses BOTH places in the line, not the
    one that is about to go: the oldest is dispatched here and the rest are left
    `pending` in the store for the scheduler's tick, which sends a forced task's
    messages down the flag-off road in order (`schedule._tick_queued`). Next
    week's message is untouched — it is waiting on a clock, not on a folder."""
    flag()
    calls, _answer = dispatched
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([
        _entry("e-old", "first thing", alpha, due=_iso(-120),
               session_id="sess-a"),
        _entry("e-new", "second thing", alpha, due=_iso(-30),
               session_id="sess-a"),
        _entry("e-later", "next week", alpha, due=_iso(7 * 86400),
               session_id="sess-a"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder",
                 entries={"sess-a": "e-old"})

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    # THE OLDEST GOES NOW, and only it.
    assert calls == ["e-old"]
    # …and BOTH due messages left the line, oldest first.
    assert _kinds(manager, "forget_entry") == [("forget_entry", "e-old"),
                                               ("forget_entry", "e-new")]
    assert manager.lines[alpha] == []
    # THE TASK IS FORCED under every name it answers to — the row's key, each
    # waiting message's own `pending:` key, and the two the dispatch minted.
    for name in ("sess-a", tasks_store.pending_key("e-old"),
                 tasks_store.pending_key("e-new"), "sess-new", "r-new"):
        assert manager.is_forced(name), name
    # …and never the message that is waiting on the CLOCK.
    assert not manager.is_forced(tasks_store.pending_key("e-later"))
    # The owner of the folder is still the owner of the folder.
    assert (manager.owner(alpha) or {})["task"] == "sess-holder"


def test_force_delivers_a_held_answer_AND_dispatches_the_waiting_message(
        client, projects_dir, folders, monkeypatch, flag, manager, dispatched,
        agent):
    """BOTH KINDS OF WAITING, AND NEITHER IS LOST (Bugbot, 2026-09-21).

    `forget_entry` drops a task's held answers the moment it stands nowhere, so
    reading them after the entries had gone would have thrown the user's answer
    away. The decision goes first, then the message."""
    flag()
    calls, _answer = dispatched
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _transcript(projects_dir, "sess-a", alpha)
    schedule._write([_entry("e-due", "the queued one", alpha,
                            session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.hold("sess-a", run_id="run-1", request_id="req-1",
                 raw={"run_id": "run-1", "request_id": "req-1",
                      "decision": "allow", "scope": "once"})
    manager.line(alpha, "sess-a", holder="sess-holder",
                 entries={"sess-a": "e-due"})

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json()["started"] is True
    assert [call["request_id"] for call in agent.calls] == ["req-1"]
    assert calls == ["e-due"]
    assert manager.held_answer("sess-a") is None


def test_a_forced_chat_is_never_admitted_into_a_line_again(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """THE DOOR THE FORCE HAS TO KEEP OPEN. The folder belongs to somebody else
    and always will — that is what Force start left behind — so the claim can
    only ever refuse, and refusing would queue a conversation the user has
    explicitly taken out of the queue."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.mark_forced("sess-a")

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-a", "message": "again"})
    assert r.status_code == 200, r.text
    assert _admitted(r.json()) == {"run": True}
    # NOTHING WAS STORED and nothing was claimed: the send goes down the
    # client's ordinary road, into the folder the holder keeps.
    assert schedule.list_entries() == []
    assert (manager.owner(alpha) or {})["task"] == "sess-holder"


def test_a_forced_chat_still_never_overtakes_itself(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """AFTER `behind_own`, NEVER BEFORE IT. Message order in a conversation is
    the conversation: a forced chat with an earlier message of its own still
    waiting queues behind it, and the tick is what sends the pair in order."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([_entry("e1", "asked first", alpha, session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.mark_forced("sess-a")

    body = _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a",
                  "message": "and then"}).json()
    assert body["run"] is False and body["behind_own"] is True
    assert [e["message"] for e in schedule.list_entries()] == ["asked first",
                                                              "and then"]


def test_force_refuses_a_task_with_nothing_waiting(
        client, projects_dir, folders, flag, manager, dispatched):
    """A client looking at a stale row, and the refusal is what makes it
    refetch."""
    flag()
    calls, _answer = dispatched
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "mine")

    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 400
    assert r.json()["error"] == "nothing waiting to start"
    assert calls == []


def test_force_refuses_a_disabled_queue_and_a_name_that_is_nothing(
        client, folders, flag, dispatched):
    """The flag is the feature's one promise: with it off this verb does not
    exist, and nothing is read or resolved on the way to saying so."""
    calls, _answer = dispatched
    flag(False)
    r = _post(client, "/api/tasks/queue/force", {"key": "sess-a"})
    assert r.status_code == 409
    assert r.json()["error"] == "project queue is off"
    flag()
    assert _post(client, "/api/tasks/queue/force",
                 {"key": "nobody"}).status_code == 404
    assert _post(client, "/api/tasks/queue/force",
                 {"entry_id": "no-such"}).status_code == 404
    assert _post(client, "/api/tasks/queue/force", {}).status_code == 400
    assert calls == []


# =================================================================== decide


class _FakeAgent:
    """The three things the decide endpoint touches on the claude template's
    agent.py. A stand-in because agent.py is a TEMPLATE outside the package's
    import graph (SPEC PY-15), and because what is under test is WHETHER the
    call happens, never agent.py's own decision rules."""

    def __init__(self, runs_dir):
        self.RUNS = str(runs_dir)
        self.calls = []

    def _alive(self, run_dir):
        return os.path.exists(os.path.join(run_dir, "alive"))

    def _perm_dir(self, run_dir):
        return os.path.join(run_dir, "perm")

    def _decide(self, **kwargs):
        self.calls.append(kwargs)
        return {"decided": kwargs["request_id"], "decision": "allow",
                "scope": "once", "mode": "", "answers": {}}


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    (runs / "run-1").mkdir(parents=True)
    (runs / "run-1" / "alive").write_text("1")
    fake = _FakeAgent(runs)
    monkeypatch.setattr(project_queue, "agent_module", lambda: fake)
    return fake


def _decide_body(project, **over):
    body = {"run_id": "run-1", "request_id": "req-1", "session_id": "sess-a",
            "project": project, "decision": "allow", "scope": "session",
            "mode": "acceptEdits", "answers": "", "note": "", "custom": ""}
    body.update(over)
    return body


def test_a_card_answered_on_a_free_folder_goes_straight_through(
        client, folders, monkeypatch, flag, agent):
    """Today's behaviour, and the one this must not change: nothing is holding
    the tree, so the parked run can have its answer now."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {})

    r = _post(client, "/api/tasks/queue/decide", _decide_body(alpha))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["held"] is False
    assert body["decided"] == "req-1"
    assert agent.calls[0]["decision"] == "allow"


def test_the_flag_off_answers_a_card_the_way_main_does(
        client, folders, monkeypatch, flag, agent):
    flag(False)
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"})

    assert _post(client, "/api/tasks/queue/decide",
                 _decide_body(alpha)).json()["held"] is False
    assert len(agent.calls) == 1


def test_a_dead_run_is_answered_now_and_never_held(
        client, folders, monkeypatch, flag, agent, tmp_path):
    """`_decide` records an `expired` verdict for a run that is over; holding it
    would park a decision for a process that can never read it."""
    flag()
    alpha, _beta = folders
    os.remove(tmp_path / "runs" / "run-1" / "alive")
    _holders(monkeypatch, {alpha: "sess-holder"})

    assert _post(client, "/api/tasks/queue/decide",
                 _decide_body(alpha)).json()["held"] is False
    assert len(agent.calls) == 1


def test_a_card_answered_while_another_task_holds_the_folder_is_held(
        client, projects_dir, folders, monkeypatch, flag, agent, rings,
        manager):
    """The one door into a busy folder that is not a message. A parked run given
    its answer now would wake up and start editing a tree another task owns.

    What is stored is the ARGUMENTS, not a decision payload: every rule in
    `_decide` (the scope downgrade, the mode switch, the answer validation) reads
    the live run, so they are evaluated at delivery and not here."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder", priority=("sess-a",))
    ahead = _rows(client)["sess-holder"]["task_id"]

    r = _post(client, "/api/tasks/queue/decide", _decide_body(alpha))
    assert r.status_code == 200, r.text
    assert r.json() == {"held": True, "position": 1, "ahead": ahead,
                        "ahead_title": "holding the folder",
                        "ahead_key": "sess-holder"}
    assert agent.calls == []
    held = manager.held_answer("sess-a")
    assert held is not None
    assert held["run_id"] == "run-1" and held["request_id"] == "req-1"
    assert held["raw"] == {
        "run_id": "run-1", "request_id": "req-1", "decision": "allow",
        "scope": "session", "mode": "acceptEdits", "answers": "", "note": "",
        "custom": ""}
    assert {"sess-a"} in rings


def test_a_second_click_on_a_held_card_does_not_queue_a_second_answer(
        client, folders, monkeypatch, flag, agent, manager):
    """First writer wins on `(run_id, request_id)` — the same latch
    `_write_decision` applies on disk, applied in the manager so a double-click
    cannot replace an answer that is already waiting to be delivered."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder")

    _post(client, "/api/tasks/queue/decide", _decide_body(alpha))
    _post(client, "/api/tasks/queue/decide", _decide_body(alpha, decision="deny"))
    held = manager.held_answer("sess-a")
    assert held is not None and held["raw"]["decision"] == "allow"


def test_a_card_that_already_has_a_verdict_is_answered_now_and_never_held(
        client, tmp_path, folders, monkeypatch, flag, agent):
    """THE STALE SECOND TAB (round-3 review, 2026-09-12). A card answered in one
    window and answered again in another that never saw it would have the second
    answer HELD — parked against a question that has a verdict, replayed into a
    run that moved on, and reported to that tab as "runs next". `_decide` is
    what should answer it: it reads the decision that won and hands it back,
    which is what the card should be showing."""
    flag()
    alpha, _beta = folders
    perm = tmp_path / "runs" / "run-1" / "perm"
    perm.mkdir(parents=True)
    (perm / "req-1.res.json").write_text(json.dumps({"decision": "allow"}))
    _holders(monkeypatch, {alpha: "sess-holder"})

    body = _post(client, "/api/tasks/queue/decide",
                 _decide_body(alpha, decision="deny")).json()

    assert body["held"] is False
    assert len(agent.calls) == 1          # straight through, latch and all


def test_decide_refuses_a_body_with_no_run(client, folders, flag, agent):
    flag()
    alpha, _beta = folders
    assert _post(client, "/api/tasks/queue/decide",
                 _decide_body(alpha, run_id="")).status_code == 400


# =================================================================== run now


def test_run_now_maps_a_queued_hold_onto_a_200_that_names_who_is_ahead(
        client, projects_dir, folders, monkeypatch, flag):
    """Queued is not a refusal. An error status would be a lie the client has to
    undo — the dragged card would snap back over work that is going out the
    moment the folder frees."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    entry = _entry("e1", "run it", alpha, session_id="sess-a")
    schedule._write([entry])
    ahead = _rows(client)["sess-holder"]["task_id"]
    monkeypatch.setattr(schedule, "run_now", lambda entry_id, now=None: {
        "ok": False, "found": True, "entry": dict(entry), "reason": "queued",
        "queued": True, "position": 2, "ahead_session": "sess-holder",
        "ahead_run": "r-1"})

    r = _post(client, "/api/schedule/run-now", {"entry_id": "e1"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": False, "reason": "queued", "entry": entry,
                        "position": 2, "ahead": ahead,
                        "ahead_title": "holding the folder",
                        "ahead_key": "sess-holder",
                        # …and where "behind TASK-041" goes when it is clicked.
                        "ahead_session": "sess-holder",
                        "ahead_target": alpha,
                        # …and what the row that just queued is CALLED, so the
                        # dragged card can name itself without a listing.
                        "task_id": tasks_store.task_number("sess-a")}


def test_run_now_still_refuses_everything_it_used_to(
        client, folders, monkeypatch, flag):
    """The other refusals are untouched: 404 for no such entry, 409 for one that
    cannot run."""
    flag()
    monkeypatch.setattr(schedule, "run_now", lambda entry_id, now=None: {
        "ok": False, "found": False, "entry": None, "reason": "no such message"})
    assert _post(client, "/api/schedule/run-now",
                 {"entry_id": "nope"}).status_code == 404
    monkeypatch.setattr(schedule, "run_now", lambda entry_id, now=None: {
        "ok": False, "found": True, "entry": {}, "reason": "already sent"})
    assert _post(client, "/api/schedule/run-now",
                 {"entry_id": "e1"}).status_code == 409


# ============================================== the scoped gone, and the ring


def _gone_the_old_way(listed):
    """What `api_tasks_changes` used to compute with a second full `_collect()`
    — kept here as the oracle the scoped derivation is compared against."""
    gone = set()
    tasks = tasks_mod._collect()
    for key in listed:
        task = tasks.get(key)
        for entry in (task or {}).get("entries", ()):
            pending = tasks_store.pending_key(str(entry.get("id") or ""))
            if pending != key:
                gone.add(pending)
    return gone


def test_the_scoped_gone_answers_exactly_what_the_full_collect_did(
        client, projects_dir, folders, flag):
    """The changes long-poll no longer pays a glob over every transcript on the
    machine to look up the entries of the keys it already names. Same answer, and
    the fixture is the shape that produces one: a pending row that has just RUN
    and is now filed under its session id."""
    flag(False)
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    schedule._write([
        # Ran: filed under the session, so its `pending:<id>` row is nobody's key.
        _entry("e-ran", "went", alpha, state=schedule.SENT, fired=_iso(-60),
               claude_session_id="sess-a"),
        # Never ran and names no session: its own pending row, and NOT gone.
        _entry("e-new", "waiting", alpha),
        # Names a session that has no transcript: filed under it all the same.
        _entry("e-named", "later", alpha, session_id="sess-b"),
    ])
    listed = {"sess-a", "sess-b", tasks_store.pending_key("e-new")}

    entries = tasks_mod._entries_for(listed)
    scoped = set()
    for key in listed:
        for entry in entries.get(key, ()):
            pending = tasks_store.pending_key(str(entry.get("id") or ""))
            if pending != key:
                scoped.add(pending)
    assert scoped == _gone_the_old_way(listed)
    assert scoped == {tasks_store.pending_key("e-ran"),
                      tasks_store.pending_key("e-named")}


def test_the_scoped_gone_reaches_the_changes_endpoint(
        client, projects_dir, folders, flag):
    """End to end: the pending row of a message that has just run is named gone,
    so two rows do not stand for one task until the next full poll."""
    flag(False)
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    schedule._write([_entry("e-ran", "went", alpha, state=schedule.SENT,
                            fired=_iso(-60), claude_session_id="sess-a")])

    since = tasks_watch.generation()
    tasks_watch.notify({"sess-a"})
    r = client.get(f"/api/tasks/changes?since={since}&wait=0")
    assert r.status_code == 200, r.text
    assert tasks_store.pending_key("e-ran") in r.json()["gone"]


def _stage_run(runs, projects_dir, run_id, session_id, project, prompt="first words"):
    """A run whose CLI has come up: the run dir names the session and the
    transcript is on disk — and the scheduler has not stamped the entry yet."""
    run_dir = runs / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_dir.joinpath("meta.json").write_text(
        json.dumps({"file": project, "resumed_from": ""}))
    run_dir.joinpath("session").write_text(session_id)
    run_dir.joinpath("alive").write_text("1")
    _transcript(projects_dir, session_id, project, prompt)
    return run_dir


def test_a_rung_pending_key_the_run_has_rekeyed_is_not_gone_it_is_the_session_row(
        client, projects_dir, folders, tmp_path, monkeypatch, flag):
    """Akshil, QA 2026-09-16: the Recent chats list LOSES tasks when the queue
    moves forward.

    The watcher keys a promoted leader off the scheduler alone
    (`schedule._task_key` -> `pending:<id>`, the stamp lands two seconds later)
    while the listing already keys it off the run dir (`_run_session` ->
    `<session>`). The rung key built no row, was answered `gone`, and the
    client deleted a LIVE row whose replacement was never rung — and a batch
    promotion rings several at once. So the answer must CARRY THE SESSION ROW;
    the rung key still leaves in `gone`, which with the row in the same payload
    is the rekey swap (`mergeTaskChanges` folds both in one pass) rather than
    the deletion it used to be."""
    flag()
    alpha, _beta = folders
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(project_queue, "agent_module", lambda: _RunsAgent(runs))
    schedule._write([_entry("e-lead", "first words", alpha, session_id="",
                            state=schedule.SENT, run_id="r-1")])
    pending = tasks_store.pending_key("e-lead")
    assert list(_rows(client)) == [pending]  # the row the client is showing

    _stage_run(runs, projects_dir, "r-1", "sess-new", alpha)
    since = tasks_watch.generation()
    tasks_watch.notify({pending})  # what the watcher can spell, and only that

    body = client.get(f"/api/tasks/changes?since={since}&wait=0").json()
    assert [row["key"] for row in body["rows"]] == ["sess-new"], \
        "the replacement row, which the rung key alone never produced"
    assert body["gone"] == [pending], "and the old name leaves with it"


def test_with_the_flag_off_a_rung_pending_key_stays_a_pending_row(
        client, projects_dir, folders, tmp_path, monkeypatch, flag):
    """Parity: `_run_session` is gated on the flag, so with the queue off the
    listing keys this entry exactly as the watcher did and the translation is a
    no-op — the pending row comes back, nothing is gone."""
    flag(False)
    alpha, _beta = folders
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(project_queue, "agent_module", lambda: _RunsAgent(runs))
    schedule._write([_entry("e-lead", "first words", alpha, session_id="",
                            state=schedule.SENT, run_id="r-1")])
    pending = tasks_store.pending_key("e-lead")
    _stage_run(runs, projects_dir, "r-1", "sess-new", alpha)

    since = tasks_watch.generation()
    tasks_watch.notify({pending})

    body = client.get(f"/api/tasks/changes?since={since}&wait=0").json()
    assert body["gone"] == []
    assert [row["key"] for row in body["rows"]] == [pending]


def test_marking_a_message_read_rings_the_long_poll(
        client, projects_dir, folders, state_dir, rings):
    """The unread count is on the row and on the pulse, so a mark read in one
    window is a change every other window has to see. Nothing in ~/.claude moved
    — only our own store — so the watcher cannot find this on its own."""
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    (state_dir / "read.json").write_text(json.dumps({tasks_store.INIT_KEY: 0.0}))
    _rows(client)

    r = client.post("/api/tasks/read", json={"key": "sess-a", "all": True})
    assert r.status_code == 200, r.text
    assert {"sess-a"} in rings


# ============================================ the two verbs, against the store
# The two tests above that matter most stub the model's write (`set_priority`)
# and the model's refusal (`run_now`), because what they pin is the ARGUMENTS
# the router sends and the shape it answers with. These two run the same
# gestures through the real store, so a router that agreed with a stub and not
# with the module cannot pass both.


def test_run_now_into_a_busy_folder_queues_through_the_real_model(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """The drag, all the way down: nothing is claimed, the entry gains
    `priority`, and the row the Board redraws reads `queued` at position 1."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    schedule._write([_entry("e1", "run it", alpha, session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, holder="sess-holder")
    ahead = _rows(client)["sess-holder"]["task_id"]

    r = _post(client, "/api/schedule/run-now", {"entry_id": "e1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "queued"
    assert body["position"] == 1
    assert body["ahead"] == ahead
    assert body["ahead_title"] == "holding the folder"
    assert body["ahead_session"] == "sess-holder"
    assert body["ahead_target"] == alpha
    assert body["entry"]["state"] == schedule.PENDING
    assert body["entry"]["priority"] is True

    row = _rows(client)["sess-a"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_priority"] is True


def test_run_now_on_a_far_future_message_puts_the_row_in_the_queued_lane(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """Run now on a message due TOMORROW, in a folder somebody else is holding.

    The answer said `queued` and the row did not: every reader of the line asks
    "due ≤ now", tomorrow is not, so the moment the optimistic paint cleared the
    card fell back to Upcoming and the scheduler would not have sent it until
    tomorrow (browser QA, 2026-09-12). `run_now_at` is the stamp that makes the
    two agree — without moving `due`, which is what the calendar draws."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    tomorrow = _iso(86400)
    schedule._write([_entry("e1", "tomorrow", alpha, due=tomorrow,
                            session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    # Owned, with nobody waiting on it yet: the press is what puts this message
    # in the line, and the row is what the manager then says about it.
    manager.line(alpha, holder="sess-holder")
    # Before the gesture it is waiting on the CLOCK, not on the folder.
    assert _rows(client)["sess-a"]["status"] == "upcoming"

    body = _post(client, "/api/schedule/run-now", {"entry_id": "e1"}).json()

    assert body["reason"] == "queued"
    assert body["position"] == 1
    row = _rows(client)["sess-a"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_priority"] is True
    # …and the ask itself is untouched.
    assert schedule.list_entries()[0]["due"] == tomorrow


def test_run_now_names_a_holder_that_has_no_session_yet(
        client, folders, monkeypatch, flag, manager):
    """A scheduler entry the tick has claimed but not spawned holds the folder
    with no conversation to its name. It is still a TASK — `pending:<entry>`,
    with a number of its own — and naming the holder by its session id would
    print "behind " with nothing after it on exactly that row."""
    flag()
    alpha, _beta = folders
    schedule._write([
        _entry("e-x", "the claimed one", alpha, state=schedule.SENDING),
        _entry("e1", "mine", alpha, session_id="sess-a"),
    ])
    manager.line(alpha, holder="pending:e-x")
    ahead = _rows(client)["pending:e-x"]["task_id"]
    assert ahead

    body = _post(client, "/api/schedule/run-now", {"entry_id": "e1"}).json()
    assert body["reason"] == "queued"
    assert body["ahead"] == ahead
    assert body["ahead_title"] == "the claimed one"
    # NAMEABLE BUT NOT OPENABLE. There is no conversation yet, so there is
    # nothing to link to — `tasks-lib.taskHref` answers null for exactly this —
    # and the folder it will run in is no substitute for one.
    assert body["ahead_session"] == ""
    assert body["ahead_target"] == alpha
    row = _rows(client)["sess-a"]
    assert row["queue_ahead"] == ahead
    assert row["queue_ahead_session"] == ""


# =========================================== follow-ups into a queued new chat
# A chat with no session yet whose first message was queued is a task —
# `pending:<entry id>` — and the second message typed into that composer has to
# join THAT task. Browser QA found it forking a second `pending:` row instead
# (2026-09-12); `follow_of` is the field that ties the two together.


def _leader_and_follower(alpha, **fields):
    schedule._write([
        _entry("e-lead", "first thing", alpha, due=_iso(-300)),
        _entry("e-follow", "second thing", alpha, due=_iso(-60),
               follow_of="e-lead", **fields),
    ])


def test_a_follower_shows_one_row_and_not_two(
        client, folders, flag):
    """THE BUG, pinned. Two messages typed into one brand-new chat are one task
    with two messages, never two tasks with one each."""
    flag()
    alpha, _beta = folders
    _leader_and_follower(alpha)

    rows = _rows(client)
    assert list(rows) == [tasks_store.pending_key("e-lead")]
    row = rows[tasks_store.pending_key("e-lead")]
    assert row["message_count"] == 2
    assert tasks_store.pending_key("e-follow") not in rows


def test_a_follower_moves_onto_the_session_its_leader_opened(
        client, folders, flag):
    """And the row keeps its NUMBER across the move: `pending:<entry>` rekeys to
    the session on the first run (§5), and a follower filed under the leader's
    key travels with it rather than allocating a second one."""
    flag()
    alpha, _beta = folders
    _leader_and_follower(alpha)
    number = _rows(client)[tasks_store.pending_key("e-lead")]["task_id"]
    assert number

    # The leader's run reports the session it landed in — the one tick of the
    # watcher that fills `claude_session_id` in.
    leader = next(e for e in schedule.list_entries() if e["id"] == "e-lead")
    schedule._update("e-lead", state=schedule.SENT, run_id="r-1")
    schedule._turn_tick(dict(leader), "r-1", None,
                        {"session_id": "sess-lead", "done": True})
    tasks_mod.reset_cache()

    rows = _rows(client)
    assert list(rows) == ["sess-lead"]
    assert rows["sess-lead"]["message_count"] == 2
    assert rows["sess-lead"]["task_id"] == number
    assert tasks_store.task_number(tasks_store.pending_key("e-lead")) == ""


def test_a_follower_takes_no_place_of_its_own_in_the_line(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """One slot per TASK, however many messages it has queued — the follower is
    in its leader's slot, not behind it. Both messages key onto the leader
    (`_entry_key`), so the manager only ever hears about one task."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _leader_and_follower(alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, tasks_store.pending_key("e-lead"), holder="sess-holder")

    row = _rows(client)[tasks_store.pending_key("e-lead")]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1


# ------------------------------------------------------- admission, in order


def test_admit_queues_behind_this_chats_own_waiting_message(
        client, folders, flag, rings):
    """EVEN THOUGH THE FOLDER IS FREE. Message order in a conversation is the
    conversation, and a client that spawned a turn into a session the scheduler
    is about to claim for the earlier message would put two runs on one
    transcript."""
    flag()
    alpha, _beta = folders
    schedule._write([_entry("e1", "asked first", alpha, session_id="sess-a")])

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-a", "message": "and then"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"] is False
    assert body["behind_own"] is True
    assert body["key"] == "sess-a"
    # Nobody is HOLDING the folder, so there is no task to name — "after your
    # previous message" is the whole sentence the chip has.
    assert body["ahead"] == ""
    assert [e["message"] for e in schedule.list_entries()] == ["asked first",
                                                               "and then"]
    assert rings[-1] == {"sess-a"}
    # …and the folder was not reserved for a send that never happened.


def test_admit_runs_when_this_chats_own_work_is_not_due_yet(
        client, folders, flag):
    """A message scheduled for next Tuesday is waiting on the clock, not on the
    folder — queueing behind it would park this send until Tuesday."""
    flag()
    alpha, _beta = folders
    schedule._write([_entry("e1", "next week", alpha, due=_iso(86_400),
                            session_id="sess-a")])

    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "session_id": "sess-a",
                           "message": "now"}).json()) == {"run": True}


def test_the_flag_off_never_looks_at_this_chats_own_work(
        client, folders, flag):
    """The control: with the flag off the answer is a constant, pending work or
    not, and the send path is main's."""
    flag(False)
    alpha, _beta = folders
    schedule._write([_entry("e1", "asked first", alpha, session_id="sess-a")])

    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a",
                  "message": "and then"}).json() == {"run": True}
    assert len(schedule.list_entries()) == 1


def test_admit_with_a_follow_of_queues_under_the_leader(
        client, folders, flag, rings):
    """The second message typed into a chat whose first is still queued. It has
    no session to name, so it names the entry — and lands in that task's row."""
    flag()
    alpha, _beta = folders
    schedule._write([_entry("e-lead", "first thing", alpha)])

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "second thing",
               "follow_of": "e-lead"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"] is False
    assert body["behind_own"] is True
    assert body["key"] == tasks_store.pending_key("e-lead")
    assert body["entry"]["follow_of"] == "e-lead"
    assert rings[-1] == {tasks_store.pending_key("e-lead")}

    rows = _rows(client)
    assert list(rows) == [tasks_store.pending_key("e-lead")]
    assert rows[tasks_store.pending_key("e-lead")]["message_count"] == 2


def test_admit_refuses_a_follow_of_that_names_nothing(client, folders, flag):
    """A client looking at a queue that has moved on. Refused rather than filed
    under a row that is not there — the 400 is what makes it refetch."""
    flag()
    alpha, _beta = folders

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "second", "follow_of": "no-such"})
    assert r.status_code == 400
    assert "follow_of" in r.json()["error"]
    assert schedule.list_entries() == []


# ============================================ round-2 review, 2026-09-12


@pytest.mark.parametrize("verb", ["archive", "delete", "erase"])
def test_filing_a_leader_takes_its_followers_work_with_it(
        client, folders, flag, verb, rings):
    """They share the task key (`_entry_key`), so the cancel-pending-entries
    step every one of the three runs already reaches both — and it has to: a
    follower left pending would surface a second later as a `pending:<follower>`
    row for a chat the user has just put away."""
    flag()
    alpha, _beta = folders
    schedule._write([
        _entry("e-lead", "first thing", alpha),
        _entry("e-follow", "second thing", alpha, follow_of="e-lead"),
    ])
    key = tasks_store.pending_key("e-lead")
    assert list(_rows(client)) == [key]

    r = _post(client, "/api/tasks/" + verb, {"key": key})
    assert r.status_code == 200, r.text
    assert r.json()["cancelled"] == 2

    assert {e["id"]: e["state"] for e in schedule.list_entries()} == {
        "e-lead": "cancelled", "e-follow": "cancelled"}
    # No row survives under a key of its own — not the leader's, and not one the
    # follower would have minted the moment it stopped following a live leader.
    assert _rows(client) == {}
    assert rings[-1] == {key}


def test_the_flag_off_resolves_no_folder_for_a_parked_run(
        client, projects_dir, folders, flag, park, monkeypatch):
    """The listing still wants the parked runs with the queue turned off, and
    the walk it reads them from is the queue's. A folder key per run dir — a
    registry read, a mount check and a climb for `.git` — would be flag-off work
    for an answer nothing on this path asks for."""
    flag(False)
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    park("run-1", "sess-a", alpha)

    def never(project):
        raise AssertionError(f"queue_key({project!r}) with the flag off")

    monkeypatch.setattr(project_queue, "queue_key", never)

    assert _rows(client)["sess-a"]["status"] == "needs_attention"


def test_one_listing_reads_each_runs_cards_once(
        client, projects_dir, folders, monkeypatch, flag, park, tmp_path):
    """`holders()` asks whether a run is parked and `_parked_runs` asks what it
    is parked ON — the same question of the same runs on one listing, and a
    directory listing plus a file per card each time it is asked."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    park("run-1", "sess-a", alpha)
    agent = project_queue.agent_module()
    reads = []
    real = agent._permissions
    monkeypatch.setattr(agent, "_permissions",
                        lambda run_dir: (reads.append(run_dir), real(run_dir))[1])
    monkeypatch.setattr(project_queue, "agent_module", lambda: agent)

    assert _rows(client)["sess-a"]["status"] == "needs_attention"
    assert reads == [str(tmp_path / "runs" / "run-1")]


def test_run_now_collects_the_tasks_once(
        client, projects_dir, folders, monkeypatch, flag):
    """Naming who is ahead and placing this row are two facts about ONE set of
    tasks, and collecting is a glob over every transcript on the machine."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    entry = _entry("e1", "run it", alpha, session_id="sess-a")
    schedule._write([entry])
    monkeypatch.setattr(schedule, "run_now", lambda entry_id, now=None: {
        "ok": False, "found": True, "entry": dict(entry), "reason": "queued",
        "queued": True, "position": 2, "ahead_task_key": "sess-holder",
        "ahead_session": "sess-holder", "ahead_run": "r-1"})
    collects = []
    real = tasks_mod._collect
    monkeypatch.setattr(tasks_mod, "_collect",
                        lambda *a, **kw: (collects.append(1), real(*a, **kw))[1])

    r = _post(client, "/api/schedule/run-now", {"entry_id": "e1"})
    assert r.status_code == 200, r.text
    assert r.json()["ahead_title"] == "holding the folder"
    assert len(collects) == 1


# ------------------------------------------------------- decide, hardened


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("over", [
    {"run_id": "../../etc/passwd"}, {"run_id": ".hidden"}, {"run_id": "a/b"},
    {"run_id": ""}, {"request_id": "../../../etc/passwd"},
    {"request_id": ".hidden"}, {"request_id": "a\\b"},
    {"request_id": "c:evil"}, {"request_id": ""}])
def test_decide_refuses_an_id_that_would_become_a_path(
        client, folders, monkeypatch, flag, agent, over, busy):
    """BOTH ARMS, and the held one is why. `_decide` has always refused these
    (`_bad_id`), so the straight-through arm was covered — but the held arm
    wrote them to a file first, and `<request_id>.res.json` is joined under the
    perm directory by a scheduler tick minutes later, or by
    `validate_held_answers` on the next launch. A traversal written by one
    request and walked by another process is the shape nothing downstream can
    trace."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"} if busy else {})

    r = _post(client, "/api/tasks/queue/decide", _decide_body(alpha, **over))
    assert r.status_code == 400, r.text
    assert agent.calls == []


def test_a_card_on_a_run_with_no_session_is_delivered_not_held(
        client, folders, monkeypatch, flag, agent, rings):
    """A held record is keyed by `session_id` — it is how delivery finds the
    row, how the chip finds its place and how a promotion is recognised at the
    head of the line — so an empty one parks a decision no view can reach and
    rings the long-poll about nothing (`notify(None)`). A run with no session
    cannot be queued behind anything anyway; delivering now is the honest
    fallback."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"})

    r = _post(client, "/api/tasks/queue/decide",
              _decide_body(alpha, session_id=""))
    assert r.status_code == 200, r.text
    assert r.json()["held"] is False
    assert len(agent.calls) == 1
    assert set() not in rings


# ================================= the chat in front, and the card's summary
#
# Three decisions from 2026-09-12, all about what a QUEUED row has to be able to
# say without a second request:
#
# * "behind TASK-041" is a LINK — the chat in front is one click away, and the
#   two fields that open it (`tasks-lib.taskHref`) ride on the row, the admit
#   answer and run-now's queued answer alike;
# * the card says "2 messages waiting" off `queue_waiting`, so it never counts
#   entries client-side;
# * and `queue_blocking` says whether that waiting work shuts the composer —
#   true only for a message somebody SCHEDULED into this conversation, never for
#   one the chat itself queued.


def test_a_starting_run_has_no_chat_to_link_to_until_it_names_itself(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """A brand-new chat's run owns its folder before it has said who it is:
    alive, in that tree, and anonymous. There is nothing to name and nothing to
    open — "behind a run in this folder" is the whole of what a reader can be
    told — and the naming pass is run on every listing, so the moment the
    manager learns whose run it is the row fills in by itself.

    The queued row is unchanged across the two: what it is waiting for never
    moved, only what can be said about it. WHEN the owner gets a name is the
    manager's business (`started`, and its own suite); what the row does with a
    nameless one is this."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    schedule._write([_entry("e-wait", "run the report", alpha,
                            session_id="sess-wait")])
    manager.line(alpha, "sess-wait", holder="")

    row = _rows(client)["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_ahead"] == ""
    assert row["queue_ahead_key"] == ""
    assert row["queue_ahead_session"] == ""
    assert row["queue_ahead_target"] == ""

    # Seconds later: the run says who it is, and the owner has a task key.
    manager.line(alpha, "sess-wait", holder="sess-holder")

    rows = _rows(client)
    row = rows["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert row["queue_ahead_session"] == "sess-holder"
    assert row["queue_ahead_target"] == alpha


def test_the_card_counts_every_message_waiting_and_not_the_ones_that_are_not(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """ONE NUMBER, THE CARD'S OWN. The line places a TASK (one slot, however
    many messages it has queued); this says how much that slot is holding, which
    is the sentence the card prints. Only work that is waiting RIGHT NOW counts:
    a message due next week is waiting on the clock, not on the folder."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-wait", alpha, "two of mine")
    schedule._write([
        _entry("e1", "first", alpha, due=_iso(-300), session_id="sess-wait"),
        _entry("e2", "second", alpha, due=_iso(-200), session_id="sess-wait"),
        _entry("e3", "next week", alpha, due=_iso(86400 * 7),
               session_id="sess-wait"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-wait", holder="sess-holder")

    row = _rows(client)["sess-wait"]
    # One slot in the line, two messages in it.
    assert row["queue_position"] == 1
    assert row["queue_waiting"] == 2


def test_a_scheduled_message_blocks_this_chats_composer_and_its_own_does_not(
        client, projects_dir, folders, monkeypatch, flag):
    """THE DECISION THIS FIELD EXISTS FOR (Akshil, 2026-09-12). A message the
    calendar or the Tasks page aimed at this conversation is one the chat cannot
    order itself against — the scheduler is about to send it into this very
    session — so the composer still shuts on it, exactly as it did before the
    queue existed. A message the chat itself queued went through admission and
    IS the conversation's next line, so it never shuts anything."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-wait", alpha, "mine")
    _holders(monkeypatch, {alpha: "sess-holder"})

    schedule._write([_entry("e1", "typed here", alpha, session_id="sess-wait",
                            origin="chat")])
    row = _rows(client)["sess-wait"]
    assert row["queue_waiting"] == 1
    assert row["queue_blocking"] is False

    # The same row, with a message somebody SCHEDULED into it beside the one it
    # queued itself.
    schedule._write([
        _entry("e1", "typed here", alpha, session_id="sess-wait",
               origin="chat"),
        _entry("e2", "from the calendar", alpha, session_id="sess-wait"),
    ])
    row = _rows(client)["sess-wait"]
    assert row["queue_waiting"] == 2
    assert row["queue_blocking"] is True


def test_a_message_scheduled_for_next_week_still_shuts_the_composer(
        client, projects_dir, folders, flag):
    """The two halves of the answer are two different questions. Waiting is
    about NOW — the card counts what the folder is between the user and — while
    the block is about the session: a chat holding next Tuesday's message is
    every bit as blocked as one holding the next thirty seconds', which is the
    rule the client has always applied (`sched/scheduled.schedPendingHere`).

    Nothing is holding this folder at all, which is the point: the block has
    never been a queue fact."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-wait", alpha, "mine")
    schedule._write([_entry("e1", "next week", alpha, due=_iso(86400 * 7),
                            session_id="sess-wait")])

    row = _rows(client)["sess-wait"]
    assert row["status"] != "queued"        # nothing is holding this folder
    assert row["queue_waiting"] == 0
    assert row["queue_blocking"] is True


def test_a_message_aimed_at_another_conversation_blocks_nothing_here(
        client, projects_dir, folders, monkeypatch, flag):
    """`queue_blocking` is about THIS session and no other. The entry below is
    filed under this task's row — it runs in the same folder — and it names
    somebody else's conversation, which is a composer this one has no business
    shutting."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-wait", alpha, "mine")
    schedule._write([_entry("e1", "someone else's", alpha,
                            session_id="sess-other")])

    rows = _rows(client)
    assert rows["sess-wait"]["queue_blocking"] is False
    assert rows["sess-other"]["queue_blocking"] is True


def test_an_admitted_queued_message_never_blocks_the_chat_that_typed_it(
        client, projects_dir, folders, flag, manager):
    """END TO END, through the endpoint that writes the word: the send the chat
    queued comes back as an ordinary pending entry, and the row it lands on does
    not ask its own composer to shut."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "holding the folder")
    _transcript(projects_dir, "sess-b", alpha, "me too")
    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "session_id": "sess-a",
                           "message": "go"}).json()) == {"run": True}

    body = _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-b",
                  "message": "me too"}).json()
    assert body["run"] is False
    assert body["entry"]["origin"] == "chat"

    manager.line(alpha, "sess-b", holder="sess-a")
    row = _rows(client)["sess-b"]
    assert row["status"] == "queued"
    assert row["queue_waiting"] == 1
    assert row["queue_blocking"] is False


# ============ the number a queued chat is given, and the entry that opens it
#                                        (Akshil's findings B and C, 2026-09-12)


def test_a_queued_new_chat_is_named_the_moment_it_queues(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """A brand-new chat whose first message queues has no session, so its row is
    `pending:<entry>` — and until this it had no NUMBER either, because numbers
    are minted by the listing and the listing had not run yet. The bubble said
    "queued · behind TASK-001" about a task it could not call anything.

    So admission mints it, and the proof is that the listing agrees: the same
    key, the same number, no second allocation."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, holder="sess-holder")

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "brand new chat"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"] is False
    number = body["task_id"]
    assert number.startswith("TASK-")
    assert number != _rows(client)["sess-holder"]["task_id"]

    key = tasks_store.pending_key(body["entry"]["id"])
    assert body["key"] == key
    manager.line(alpha, key, holder="sess-holder")
    rows = _rows(client)
    assert rows[key]["task_id"] == number
    assert rows[key]["status"] == "queued"
    # …and the same key still answers the same number on every listing after
    # that: allocation is once, keyed by the task key (`ensure_ids`).
    assert _rows(client)[key]["task_id"] == number


def test_a_queued_send_carries_the_chat_drafts_name_and_takes_the_draft_with_it(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """THE NAME SURVIVES THE QUEUE. A composer with no session autosaves under
    `new:<file>` and the listing numbers that key, so the reader is watching
    TASK-001 before a word of it has gone anywhere. Queueing the send is the
    same event for that row as scheduling a form is for a `draft:<id>` one — the
    thing keeps going, under a new key — so the number is REKEYED onto
    `pending:<entry-id>` rather than a second one being minted, and the spent
    draft goes in the same request (review, PR #1124)."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, holder="sess-holder")

    key = drafts.NEW_CHAT_PREFIX + alpha + "/app.py"
    assert drafts.put_chat(key, "half a thought") is not None
    named = _rows(client)[key]
    assert named["kind"] == "draft"
    number = named["task_id"]
    assert number.startswith("TASK-")

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "half a thought", "draft_key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"] is False
    # THE SAME NAME the draft row wore, in the answer that queued it...
    assert body["task_id"] == number
    pending = tasks_store.pending_key(body["entry"]["id"])
    assert body["key"] == pending
    # ...and on every listing after it.
    manager.line(alpha, pending, holder="sess-holder")
    rows = _rows(client)
    assert rows[pending]["task_id"] == number
    assert rows[pending]["status"] == "queued"
    # THE DRAFT IS OVER: no row, no record, and no number record left behind —
    # which is also `_settle_new_chats`' whole waiting set, so no later listing
    # scans the runs tree for a draft nothing will ever settle.
    assert key not in rows
    assert drafts.get_chat(key) is None
    assert [k for k in tasks_store.task_ids()
            if drafts.is_new_chat_key(k)] == []


def test_a_queued_send_from_a_task_draft_carries_its_name_and_takes_the_form(
        client, projects_dir, folders, monkeypatch, flag):
    """THE SAME SPEND, FOR THE OTHER DRAFT SHAPE. A session-less composer can be
    typing into a TASK draft rather than a `new:<file>` chat draft, and then the
    key its send carries is that form's own listing key (`draft:<id>`). Queueing
    it is the same event scheduling the form is — the row keeps going under
    `pending:<entry-id>` — so it goes through the same spend
    (`schedule_api.spend_task_draft`) and mints no second number. Before that
    branch the key fell through `drafts.chat_key`, which refuses the shape: the
    number stayed on a row nobody would ever read again and the entry got a
    fresh one."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _holders(monkeypatch, {alpha: "sess-holder"})

    record, _canonical = drafts.put_task(
        "draft-00000001", {"title": "half a thought", "target": alpha})
    assert record is not None
    key = drafts.task_key("draft-00000001")
    named = _rows(client)[key]
    assert named["kind"] == "draft"
    number = named["task_id"]
    assert number.startswith("TASK-")

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "half a thought", "draft_key": key})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"] is False
    assert body["task_id"] == number, "the name the form wore, in the answer"
    pending = tasks_store.pending_key(body["entry"]["id"])
    assert body["key"] == pending
    rows = _rows(client)
    assert rows[pending]["task_id"] == number
    assert rows[pending]["status"] == "queued"
    # THE FORM IS OVER: no row, no record, and no number left on its key.
    assert key not in rows
    assert drafts.get_task("draft-00000001") is None
    assert [k for k in tasks_store.task_ids() if drafts.task_draft_id(k)] == []


def test_a_queued_send_from_a_chat_that_has_a_session_moves_no_number(
        client, projects_dir, folders, monkeypatch, flag):
    """A chat that HAS a session is numbered under that session, and this
    message is landing in it — so the draft record (still holding the sent
    words) goes and nothing is carried
    anywhere. Moving a number onto `pending:<entry-id>` here would invent a
    second identity for a task that already has one (the schedule form's own
    rule, read through the other door)."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-b", alpha, "a conversation of its own")
    _holders(monkeypatch, {alpha: "sess-holder"})
    # The store holds exactly the words that are about to queue — the ordinary
    # shape; a record holding OTHER words is a follow-up and is kept (see
    # `test_a_follow_up_typed_during_the_admit_survives_the_spend`).
    assert drafts.put_chat("sess-b", "go") is not None
    before = _rows(client)["sess-b"]["task_id"]

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-b", "message": "go",
               "draft_key": "sess-b"})
    assert r.json()["run"] is False
    rows = _rows(client)
    # The conversation keeps its own number and loses only the `Draft` chip.
    assert rows["sess-b"]["task_id"] == before
    assert rows["sess-b"]["draft"] is None
    assert drafts.get_chat("sess-b") is None
    assert tasks_store.pending_key(r.json()["entry"]["id"]) not in tasks_store.task_ids()


def test_an_admitted_send_leaves_its_chat_draft_alone(
        client, folders, flag):
    """`run: true` spends nothing. Such a send starts a RUN, and that run
    carries the key in its own `meta.json` (`_settle_new_chats`) — so a server
    that also deleted the draft here would destroy the words of a send that then
    failed to spawn."""
    flag()
    alpha, _beta = folders
    key = drafts.NEW_CHAT_PREFIX + alpha + "/app.py"
    drafts.put_chat(key, "half a thought")
    # The draft key is the name a session-less composer already has, so the
    # placeholder is filed under it rather than under a fresh uuid.
    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "message": "half a thought",
                           "draft_key": key}).json()) == {
        "run": True, "owner_token": PLACEHOLDER + key}
    assert drafts.get_chat(key) is not None


def test_two_admissions_of_one_task_in_two_threads_mint_one_number(
        client, projects_dir, folders, monkeypatch, flag):
    """ALLOCATE-ONCE HOLDS UNDER CONCURRENT ADMITS, and the lock is
    `tasks_store._update`'s — a sibling `.lock` file held with `flock` for the
    whole read-modify-write, which is per open-file-description and therefore
    excludes two THREADS of this process as surely as two processes. FastAPI
    serves sync routes from a threadpool, so two sends landing together is not
    exotic; a second number for one key would mean the chip and the row calling
    the same conversation different things for ever (numbers are never
    released).

    Asked of `_task_number` rather than of `ensure_ids` directly, because that
    is the seam the admission answers through and the one that also has to walk
    `_place` and `_numbers` on the way in."""
    import threading

    flag()
    alpha, _beta = folders
    schedule._write([_entry("e1", "queued", alpha, due=_iso(-1))])
    key = tasks_store.pending_key("e1")
    tasks = tasks_mod._collect()
    assert key in tasks

    minted, start = [], threading.Barrier(2)

    def admit():
        start.wait()
        minted.append(tasks_mod._task_number(key, dict(tasks)))

    threads = [threading.Thread(target=admit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(minted) == 2
    assert minted[0].startswith("TASK-")
    assert minted[0] == minted[1], "one key, one number, however many askers"
    assert list(tasks_store.task_ids()) == [key], "and one record on disk"


def test_a_followers_admission_answers_its_leaders_number(
        client, projects_dir, folders, monkeypatch, flag):
    """A second message typed into a chat whose first is still queued joins the
    LEADER's task, so it must answer the leader's number rather than mint a
    second one for a row that does not exist."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _holders(monkeypatch, {alpha: "sess-holder"})

    lead = _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "message": "first thing"}).json()
    follow = _post(client, "/api/tasks/queue/admit",
                   {"project": alpha, "message": "second thing",
                    "follow_of": lead["entry"]["id"]}).json()

    assert follow["key"] == lead["key"]
    assert follow["task_id"] == lead["task_id"]
    # ONE record, for the one chat: minting is for the task that was admitted,
    # not a listing in disguise — the holder is numbered when something lists it.
    assert list(tasks_store.task_ids()) == [lead["key"]]


def test_an_admitted_send_that_runs_answers_no_number_and_mints_none(
        client, folders, flag):
    """`run: true` is main's own answer and stores nothing — including no
    number. A chat that is running is one the listing will name on its next
    pass, which is the behaviour with the flag off too."""
    flag()
    alpha, _beta = folders
    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "session_id": "sess-a",
                           "message": "go"}).json()) == {"run": True}
    assert tasks_store.task_ids() == {}


def test_a_pending_row_carries_the_entry_that_opens_it(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """`entry_id` on the row: the leader entry a waiting chat is re-entered by.

    A `pending:<entry>` task has no session and no transcript, so the entry id
    is the only handle a client has on it — and it is the one name that does not
    move when the row rekeys onto the session its leader opens."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    schedule._write([
        _entry("e-lead", "first thing", alpha),
        _entry("e-follow", "second thing", alpha, follow_of="e-lead"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, tasks_store.pending_key("e-lead"), holder="sess-holder")

    rows = _rows(client)
    key = tasks_store.pending_key("e-lead")
    row = rows[key]
    # The LEADER's id, not the follower's: both messages are one task.
    assert row["entry_id"] == "e-lead"
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_waiting"] == 2
    assert row["title"] == "first thing"
    assert row["project"] == alpha
    assert row["last_active"] > 0
    # A task with a conversation behind it is opened by its session, so it has
    # no entry to name.
    assert rows["sess-holder"]["entry_id"] == ""
    assert rows["sess-holder"]["entry_origin"] == ""


def test_the_entry_on_a_pending_row_survives_the_flag_being_off(
        client, folders):
    """`entry_id` is a fact about the KEY and not about the queue, so it is not
    one of the fields the flag guards: a pending row is opened the same way
    whether or not anything is gating folders."""
    alpha, _beta = folders
    schedule._write([_entry("e-lead", "first thing", alpha)])

    row = _rows(client)[tasks_store.pending_key("e-lead")]
    assert row["entry_id"] == "e-lead"
    assert row["status"] != "queued"
    assert row["queue_position"] == 0


def test_a_pending_row_says_whether_a_chat_or_a_calendar_asked_for_it(
        client, projects_dir, folders, monkeypatch, flag):
    """Two `pending:<entry>` rows, identical in shape and very different things.

    One is a chat whose first line queued through admission — a conversation
    waiting to start, which belongs in a list of chats. The other is a message
    somebody scheduled from the calendar, which is a job with a date on it and
    does not. `origin` is the only thing that tells them apart, and the row is
    where a reader of rows has to find it."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _holders(monkeypatch, {alpha: "sess-holder"})

    chat = _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "message": "a chat that queued"}).json()
    schedule.create(target=alpha, message="a job somebody scheduled",
                    due=_iso(-30))

    rows = _rows(client)
    assert rows[chat["key"]]["entry_origin"] == "chat"
    scheduled = [row for key, row in rows.items()
                 if key.startswith("pending:") and key != chat["key"]]
    assert len(scheduled) == 1
    assert scheduled[0]["entry_origin"] == ""
    # …and the entry that opens each is its own leader either way.
    assert rows[chat["key"]]["entry_id"] == chat["entry"]["id"]
    assert scheduled[0]["entry_id"] == \
        tasks_store.pending_entry(scheduled[0]["key"])


def test_a_follow_up_typed_during_the_admit_survives_the_spend(
        client, folders, flag, monkeypatch, projects_dir):
    """Bugbot (1d50d4303): the composer autosaves under `new:<file>` while the
    first send is still being admitted, so the record can already hold the NEXT
    message when the spend runs. The number moves; the words stay."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})
    key = drafts.NEW_CHAT_PREFIX + alpha + "/app.py"
    # What the store holds by the time the spend runs is the follow-up, not
    # the message that queued.
    assert drafts.put_chat(key, "and then the second thought") is not None
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "first thought", "draft_key": key})
    assert r.status_code == 200, r.text
    assert r.json()["run"] is False
    kept = drafts.get_chat(key)
    assert kept is not None and kept["text"] == "and then the second thought"
    # …while a record that still holds exactly the sent words is spent.
    key2 = drafts.NEW_CHAT_PREFIX + alpha + "/other.py"
    assert drafts.put_chat(key2, "first thought") is not None
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "first thought", "draft_key": key2})
    assert r.status_code == 200, r.text
    assert drafts.get_chat(key2) is None


# ================================ a queued chat keeps its number when it starts


def test_a_queued_chats_number_moves_onto_its_session_the_moment_the_run_names_it(
        client, projects_dir, folders, tmp_path, monkeypatch, flag):
    """Browser QA, 2026-09-16: TASK-056 waiting became TASK-057 running. The
    transcript is on disk within a second of the spawn; the scheduler stamps
    `claude_session_id` on the entry two seconds later. A listing in between
    saw a session with no entry and numbered it afresh. The run dir names the
    session sooner (`_run_session`), so the pending row and the transcript are
    ONE task from the first listing after the CLI comes up, and the rekey
    keeps the number."""
    flag()
    alpha, _beta = folders
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(project_queue, "agent_module", lambda: _RunsAgent(runs))

    # The queued message, numbered while it waits.
    schedule._write([_entry("e-lead", "first words", alpha, session_id="",
                            state=schedule.SENT, run_id="r-1")])
    # No transcript yet: the row is the pending key.
    before = _rows(client)
    assert before[tasks_store.pending_key("e-lead")]["task_id"] == "TASK-001"

    # The run comes up: run dir names the session, the transcript lands with
    # its cwd — and the scheduler has NOT written `claude_session_id` yet.
    run_dir = runs / "r-1"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": alpha, "resumed_from": ""}))
    (run_dir / "session").write_text("sess-new")
    (run_dir / "alive").write_text("1")
    _transcript(projects_dir, "sess-new", alpha, "first words")

    rows = _rows(client)
    assert tasks_store.pending_key("e-lead") not in rows
    assert rows["sess-new"]["task_id"] == "TASK-001"


def test_a_follower_joins_the_session_its_leaders_run_named(
        client, projects_dir, folders, tmp_path, monkeypatch, flag):
    """Bugbot: `_run_session` answered for the leader alone, so in the window
    before the scheduler stamped `claude_session_id` the leader joined the live
    session while its follow-ups stayed on `pending:` — one chat split in two
    rows, and a number burnt."""
    flag()
    alpha, _beta = folders
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(project_queue, "agent_module", lambda: _RunsAgent(runs))
    schedule._write([
        _entry("e-lead", "first words", alpha, session_id="",
               state=schedule.SENT, run_id="r-1"),
        _entry("e-follow", "second words", alpha, follow_of="e-lead"),
    ])
    lead_key = tasks_store.pending_key("e-lead")
    assert list(_rows(client)) == [lead_key]
    number = _rows(client)[lead_key]["task_id"]

    run_dir = runs / "r-1"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({"file": alpha, "resumed_from": ""}))
    (run_dir / "session").write_text("sess-new")
    (run_dir / "alive").write_text("1")
    _transcript(projects_dir, "sess-new", alpha, "first words")

    rows = _rows(client)
    assert set(rows) == {"sess-new"}
    assert rows["sess-new"]["task_id"] == number


# ================================================ the doors fire the events
#
# WHAT EACH ENDPOINT SAYS TO THE MANAGER, as opposed to what it answers the
# client (which every case above pins). The two are separate promises and the
# second is worthless without the first: a force that answered `started: true`
# and never told the manager to forget the entry would satisfy the reply's
# contract exactly once, and then the entry would sit in its folder's line
# forever, unstarted from the manager's own point of view.
#
# `FakeManager.events` is the tape. Nothing here asserts an ORDER inside the
# manager — that is tests/test_queue_manager.py's whole subject — only that the
# door reached it, with the keys the rest of the app files this task under.


def _kinds(manager, *names):
    return [event for event in manager.events if event[0] in names]


def test_admit_takes_the_folder_through_the_manager(
        client, folders, flag, manager):
    """ONE CALL, not a look and then a write: the folder was free, so this send
    runs and the same call that asked is the one that filed it.

    `is_free` then `started` was two acquisitions of the manager's lock with a
    gap in between, and two sends into one free folder arriving on two request
    threads both heard "free" and both spawned."""
    flag()
    alpha, _beta = folders
    assert _admitted(_post(client, "/api/tasks/queue/admit",
                          {"project": alpha, "session_id": "sess-a",
                           "message": "go"}).json()) == {"run": True}
    assert _kinds(manager, "claim") == [("claim", alpha, "sess-a", "", "sess-a")]
    assert _kinds(manager, "started", "enqueue") == []
    assert (manager.owner(alpha) or {})["task"] == "sess-a"


def test_a_second_nameless_send_queues_behind_the_first_ones_placeholder(
        client, folders, flag, manager):
    """A brand-new chat's first send names neither a session nor a run — the
    spawn has not happened, because this is the call that says it may — so the
    admission used to file NOBODY and the folder went on reading free. The
    second nameless send was let straight into it.

    The placeholder is that missing name. It is not a run (`is_free` never
    refuses a send for one) and it expires on its own, because no process exists
    yet to send the event that would free it."""
    flag()
    alpha, _beta = folders
    first = _post(client, "/api/tasks/queue/admit",
                  {"project": alpha, "message": "first"}).json()
    assert first["run"] is True
    assert (manager.owner(alpha) or {})["task"] == first["owner_token"]

    second = _post(client, "/api/tasks/queue/admit",
                   {"project": alpha, "message": "second"}).json()
    assert second["run"] is False
    assert second["entry"]["message"] == "second"


def test_the_real_spawn_replaces_the_placeholder(
        client, folders, flag, manager):
    """`routers/run._file_owner` files the owner the moment `_start` returns,
    with both names — which is the event that retires the placeholder."""
    flag()
    alpha, _beta = folders
    token = _post(client, "/api/tasks/queue/admit",
                  {"project": alpha, "message": "first"}).json()["owner_token"]
    assert (manager.owner(alpha) or {})["task"] == token

    manager.started(alpha, "sess-new", "run-1", "sess-new")
    owner = manager.owner(alpha) or {}
    assert (owner["task"], owner["run_id"]) == ("sess-new", "run-1")


def test_admit_enqueues_the_entry_it_stored(
        client, projects_dir, folders, monkeypatch, flag, manager):
    """The other road: the folder is owned, so the message is written to the
    store AND put in the line. A message that was only written would sit there
    until some later pass happened to notice it."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding it")
    _holders(monkeypatch, {alpha: "sess-holder"})

    body = _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-b", "message": "me too"}
                 ).json()
    assert body["run"] is False
    assert _kinds(manager, "enqueue") == [
        ("enqueue", alpha, "sess-b", body["entry"]["id"])]
    assert _kinds(manager, "started") == []


def test_a_follow_up_into_a_queued_chat_adds_no_second_slot(
        client, folders, monkeypatch, flag, manager):
    """A follow-up is a MESSAGE ON A TASK, not another task in the line. The
    entry still carries `follow_of` — the store files the row by it — but the
    manager keys by task, so the second admission names the key that is already
    standing there and the line does not grow."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"})
    leader = _post(client, "/api/tasks/queue/admit",
                   {"project": alpha, "message": "first"}).json()
    follower = _post(client, "/api/tasks/queue/admit",
                     {"project": alpha, "message": "second",
                      "follow_of": leader["entry"]["id"]}).json()

    assert follower["key"] == leader["key"]
    assert [event[2] for event in _kinds(manager, "enqueue")] == [
        leader["key"], leader["key"]]
    assert manager.lines[alpha] == [leader["key"]]


# --------------------------------------- what the manager asks this process
#
# `running`/`blocked` are injected into the manager and they are asked with the
# OWNER RECORD, not a task key: half the index knows a conversation by a name
# the registry never heard.


def test_the_running_read_answers_to_a_run_id_when_that_is_all_there_is(
        folders, chat_run):
    """THE BUG: every scheduled message the pump starts owns its folder under
    `pending:<entry id>` — no session, no registry row, no mark — so the status
    read answered "dead" and `reconcile` dropped a LIVE owner on the next tick.
    The record carries the run the spawn returned, and the run dir has a pid."""
    alpha, _beta = folders
    chat_run("run-1", alpha, pid=os.getpid())
    assert tasks_mod._queue_running(
        {"task": "pending:e1", "run_id": "run-1", "session_id": ""}) is True
    assert tasks_mod._queue_running(
        {"task": "pending:e1", "run_id": "run-9", "session_id": ""}) is False
    # …and the bare key, which is all an older caller has, still answers.
    assert tasks_mod._queue_running("pending:e1") is False
    assert tasks_mod._queue_running("run-1") is True


def test_a_placeholder_owner_is_never_asked_about(folders, chat_run):
    """`admit:<token>` names no process — that is why it exists — so it answers
    to nothing but its own expiry inside the manager."""
    alpha, _beta = folders
    chat_run("run-1", alpha, pid=os.getpid())
    assert tasks_mod._queue_running(
        {"task": PLACEHOLDER + "one", "run_id": "", "session_id": ""}) is False


def test_the_parked_read_answers_to_a_run_id_too(folders, park):
    """The cards belong to the RUN. An owner the index knows only by its run id
    would otherwise read as "not parked" the moment it put a card up, and get
    its folder taken away while a human was looking at the card."""
    alpha, _beta = folders
    park("run-1", "sess-1", alpha)
    assert tasks_mod._queue_blocked(
        {"task": "pending:e1", "run_id": "run-1", "session_id": ""}) is True
    assert tasks_mod._queue_blocked(
        {"task": "pending:e1", "run_id": "run-2", "session_id": ""}) is False
    # …and the session half, unchanged.
    assert tasks_mod._queue_blocked(
        {"task": "pending:e1", "run_id": "", "session_id": "sess-1"}) is True


def test_own_run_alive_answers_for_this_chats_process_and_no_other(
        folders, chat_run, registry):
    """The one question both doors ask a forced chat. Either name is enough — a
    chat with no session yet has only the run it started — and NEITHER id is
    tried as the other: a session id tried as a run id would name a stranger's
    run dir and answer yes."""
    alpha, _beta = folders
    chat_run("r-forced", alpha)
    assert tasks_mod.own_run_alive("", "r-forced") is True
    assert tasks_mod.own_run_alive("r-forced", "") is False
    assert tasks_mod.own_run_alive("", "r-gone") is False
    assert tasks_mod.own_run_alive("", "") is False
    # A path where a run id belongs is not a run id.
    assert tasks_mod.own_run_alive("", "../r-forced") is False
    # …and the registry half, which is what a chat that HAS a session offers.
    registry("sess-forced", status="busy")
    assert tasks_mod.own_run_alive("sess-forced", "") is True


def test_decide_hands_the_card_to_the_manager(
        client, projects_dir, folders, monkeypatch, flag, agent, manager):
    """`card_answered` is the fork: it knows who owns the folder, so the
    endpoint does not re-derive it."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding it")
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder")

    body = _post(client, "/api/tasks/queue/decide", _decide_body(alpha)).json()
    assert body["held"] is True
    assert _kinds(manager, "card_answered") == [
        ("card_answered", "sess-a", "run-1", "req-1")]
    assert agent.calls == []          # nothing written to the run yet


def test_decide_on_a_free_folder_delivers_and_holds_nothing(
        client, folders, monkeypatch, flag, agent, manager):
    """The manager says not-held, and the endpoint is what delivers — a human
    pressing Allow must not wait on a pump."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {})

    body = _post(client, "/api/tasks/queue/decide", _decide_body(alpha)).json()
    assert body["held"] is False and body["decided"] == "req-1"
    assert _kinds(manager, "card_answered") == [
        ("card_answered", "sess-a", "run-1", "req-1")]
    assert agent.calls[0]["decision"] == "allow"
    assert manager.held_answer("sess-a") is None


def test_a_forced_tasks_card_is_never_held(
        client, projects_dir, folders, monkeypatch, flag, agent, manager):
    """A forced task runs BESIDE whatever owns the folder, by the user's own
    instruction, so holding its card decision would park an answer for a process
    that is running right now and waiting for it — the queue taking back,
    through the one door that is not a message, what Force start gave."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding it")
    _holders(monkeypatch, {alpha: "sess-holder"})
    manager.line(alpha, "sess-a", holder="sess-holder")
    manager.mark_forced("sess-a")

    body = _post(client, "/api/tasks/queue/decide", _decide_body(alpha)).json()
    assert body["held"] is False and body["decided"] == "req-1"
    assert agent.calls[0]["decision"] == "allow"
    # The manager is never even asked to fork: the answer is not the folder's
    # business any more.
    assert _kinds(manager, "card_answered") == []


def test_with_the_flag_off_no_door_touches_the_manager(
        client, projects_dir, folders, flag, agent, manager):
    """The promise the whole feature is behind. Off, admission is a constant,
    force is a 409 before anything is read, and a card goes straight through —
    and the manager hears about none of it."""
    flag(False)
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "mine")

    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a", "message": "go"}
                 ).json() == {"run": True}
    assert _post(client, "/api/tasks/queue/force",
                 {"key": "sess-a"}).status_code == 409
    assert _post(client, "/api/tasks/queue/decide",
                 _decide_body(alpha)).json()["held"] is False
    assert manager.events == []


def test_a_queue_dispatched_turn_marks_its_session_running(monkeypatch):
    """Akshil's list audit (2026-09-18): the watcher may still hold the previous
    turn's `turn_ended` stamp for this session, and a hand-off inside the same
    second cleared nothing — the row read done while the next turn ran. The
    queue is the one caller that knows a turn just began, so it marks it."""
    from fused_render import tasks_watch as tw

    tw.reset()
    monkeypatch.setattr(tasks_mod, "_oldest_due_entry", lambda key: "e-1")
    monkeypatch.setattr(tasks_mod.schedule, "dispatch_entry",
                        lambda entry_id, now=None: {"run_id": "r-1",
                                                    "session_id": "sess-1"})
    tw.mark_turn_ended("sess-1", "r-0", time.time())
    assert tw.is_turn_ended("sess-1")
    assert tasks_mod._queue_spawn("/w/alpha", "sess-1") == {"run_id": "r-1",
                                                            "session_id": "sess-1"}
    assert tw.is_marked_running("sess-1")
    # …even when the mark lands in the SAME clock tick as the ended stamp,
    # which on Windows (~15 ms `time.time()` resolution) it routinely does.
    assert not tw.is_turn_ended("sess-1")


def test_a_running_mark_in_the_same_tick_as_the_ended_stamp_still_wins():
    """Windows CI (2026-09-18): `time.time()` there ticks every ~15 ms, so the
    queue's `mark_running` right after dispatch carried the SAME stamp as the
    `turn_ended` it followed, and a strict "newer" compare read the fresh turn
    as ended. Equal is newer here — dispatch follows the end by causality."""
    from fused_render import tasks_watch as tw

    tw.reset()
    at = time.time()
    tw.mark_turn_ended("sess-2", "r-0", at)
    with mock.patch.object(tw.time, "time", return_value=at):
        tw.mark_running("sess-2")
    assert not tw.is_turn_ended("sess-2")


# ============================== delete and erase, and the queue that outlives
#
# THE TWO VERBS THAT END A TASK (`/api/tasks/delete`, `/api/tasks/erase`) never
# learnt about the queue (Akshil, 2026-09-20). They cancel the task's pending
# entries — which reaches the line ONE MESSAGE at a time (`schedule._queue_forget`
# → `forget_entry`) — and say nothing about the task itself, so a held decision
# and a line item the queue minted for itself outlived the row: the next pump
# delivered the answer, the run wrote to its transcript, and `_deleted` (the
# transcript's mtime against the tombstone) put the task back on the page. The
# other half is the guard: #1194 made a run parked on an answered card read
# `queued` rather than `needs_attention`, and both endpoints refuse only the two
# older words.


def test_deleting_a_queued_task_drops_its_held_answer_and_its_place(
        client, projects_dir, folders, flag, manager):
    """The ghost row. The decision is filed under the TASK — no message names
    it, so no entry cancel can reach it — and the line item is the folder's
    slot, which is why everything behind a deleted task went on waiting for a
    row nobody could see."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-a", alpha, "run the build")
    _transcript(projects_dir, "sess-b", alpha, "and me after")
    manager.line(alpha, "sess-a", "sess-b", holder="sess-holder")
    # The run this answer was given to is gone — nothing is staged in the runs
    # tree — which is what makes the task deletable rather than a 409 below.
    manager.hold("sess-a", run_id="r-a", request_id="req-1",
                 raw={"decision": "allow"})
    assert _rows(client)["sess-a"]["status"] == "queued"

    r = _post(client, "/api/tasks/delete", {"key": "sess-a"})
    assert r.status_code == 200, r.text

    assert ("remove", "sess-a") in manager.events
    assert manager.held_answer("sess-a") is None
    assert manager.lines[alpha] == ["sess-b"]
    # …and the task behind it moves up, instead of queueing behind a row that
    # is not on the page any more.
    rows = _rows(client)
    assert "sess-a" not in rows
    assert rows["sess-b"]["queue_position"] == 1


def test_erasing_a_queued_task_leaves_the_queue_before_the_files_go(
        client, projects_dir, folders, flag, manager, monkeypatch):
    """Order, and it is the point: `remove` drops the held decision AND lets go
    of the folder, so no pump can deliver an answer into this session —
    resuming the run — in the window between the erase deciding to go ahead and
    the transcript leaving the disk."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    path = _transcript(projects_dir, "sess-a", alpha, "run the build")
    manager.line(alpha, "sess-a", holder="sess-holder")
    manager.hold("sess-a", run_id="r-a", request_id="req-1",
                 raw={"decision": "allow"})

    order: list[str] = []
    real_remove = manager.remove
    real_erase = tasks_mod._erase_session_files
    monkeypatch.setattr(manager, "remove",
                        lambda key: (order.append("remove"), real_remove(key))[1])
    monkeypatch.setattr(tasks_mod, "_erase_session_files",
                        lambda session_id, path: (order.append("files"),
                                                  real_erase(session_id, path))[1])

    r = _post(client, "/api/tasks/erase", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json()["erased_transcript"] is True

    assert order == ["remove", "files"]
    assert manager.held_answer("sess-a") is None
    assert manager.lines[alpha] == []
    assert not path.exists()


def test_deleting_a_run_parked_on_an_answered_card_is_refused(
        client, projects_dir, folders, flag, park, manager):
    """#1194's shape, and the hole it opened in both guards. The user has
    answered the card, so the decision is held and `_parked_runs` stops calling
    the run parked — the row reads `queued`, which neither endpoint refuses.
    The process is still very much alive, and tombstoning the row (or erasing
    its transcript) out from under it is the exact failure the 409 exists to
    prevent."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-a", alpha, "run the build")
    park("r-a", "sess-a", alpha)
    manager.hold("sess-a", run_id="r-a", request_id="req-1",
                 raw={"decision": "allow"})
    manager.line(alpha, "sess-a", holder="sess-holder", priority=("sess-a",))
    assert _rows(client)["sess-a"]["status"] == "queued"

    for verb in ("delete", "erase"):
        r = _post(client, "/api/tasks/" + verb, {"key": "sess-a"})
        assert r.status_code == 409, (verb, r.text)
        assert "stop the run first" in r.json()["detail"]

    # Nothing was taken away on the way to the refusal.
    assert manager.held_answer("sess-a") is not None
    assert manager.lines[alpha] == ["sess-a"]
    assert "sess-a" in _rows(client)


def test_admit_teaches_a_forced_run_its_session_and_lets_it_through(
        client, projects_dir, folders, flag, manager):
    """The forced new chat's second send is the first thing that carries both
    its run id and the session Claude Code minted (Bugbot, PR #1296)."""
    flag(True)
    alpha, _beta = folders
    manager.mark_forced("pending:e1", "run-1")
    manager.line(alpha, "sess-other", holder="sess-holder")
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "message": "again", "session_id": "sess-1",
               "run_id": "run-1"})
    assert r.status_code == 200 and r.json() == {"run": True}
    assert manager.is_forced("sess-1") is True


def test_deleting_a_task_is_what_ends_its_force_start(
        client, projects_dir, folders, flag, manager):
    """The delete door calls BOTH verbs: `remove` (line, answers, folder) and
    `forget_forced`. `remove` alone must not un-force — the force endpoint
    calls it after delivering a held answer (Bugbot, PR #1296)."""
    flag(True)
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "run the build")
    manager.mark_forced("sess-a")

    assert _post(client, "/api/tasks/delete",
                 {"key": "sess-a"}).status_code == 200
    assert ("forget_forced", "sess-a") in manager.events
    assert manager.is_forced("sess-a") is False


def test_the_flag_off_says_nothing_to_the_queue_on_delete(
        client, projects_dir, folders, flag, manager):
    """Flag off is main: the manager is not asked whether the task is live and
    not told that it is gone, because with the feature off nothing ever queued
    it."""
    flag(False)
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "run the build")
    manager.line(alpha, "sess-a", holder="sess-holder")

    assert _post(client, "/api/tasks/delete",
                 {"key": "sess-a"}).status_code == 200
    assert _kinds(manager, "remove") == []
